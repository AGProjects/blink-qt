"""Conference info: every participant of a video room with its feed's data, refreshed while open.

From the session (state, type, muted, speaking), the window (grid, thumbnail, carousel, speaker,
video lost) and each webrtcbin's own statistics (get-stats): what comes in from every feed
(inbound-rtp: bytes, packets lost, jitter) and what we send (outbound-rtp, and remote-inbound-rtp:
what the server reports of it, loss and round trip). Rates are the difference between two
refreshes.
"""

from time import monotonic

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QAbstractItemView, QDialog, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout


__all__ = ['ConferenceInfoPanel']


REFRESH = 2000      # ms

COLUMNS = ('Participant', 'Type', 'State', 'Audio', 'Audio kbit/s', 'Audio lost', 'Video', 'Resolution', 'fps',
           'Video kbit/s', 'Video lost', 'Jitter', 'Where')


class ConferenceInfoPanel(QDialog):
    panels = {}         # window -> panel

    @classmethod
    def show_for(cls, window):
        panel = cls.panels.get(window)
        if panel is None:
            panel = cls.panels[window] = cls(window)
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def __init__(self, window):
        super(ConferenceInfoPanel, self).__init__(window)
        self.window_ = window
        self.session = window.session
        self.previous = {}      # key -> (time, value) for the rates
        self.setWindowTitle(f'Conference info: {window.title}')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(1080, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.summary = QLabel(self)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        note = QLabel('Rates over the last 2 s. Lost: packets lost in total. SIP callers come mixed through the audio bridge: '
                      'their own audio has no statistics here.', self)
        note.setWordWrap(True)
        note.setEnabled(False)
        layout.addWidget(note)
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()
        self.session.request_stats()
        QTimer.singleShot(300, self._refresh)
        self.session.stateChanged.connect(self._SH_StateChanged)

    def closeEvent(self, event):
        self.timer.stop()
        self.panels.pop(self.window_, None)
        super(ConferenceInfoPanel, self).closeEvent(event)

    def _SH_StateChanged(self, state, reason):
        if state == 'terminated':
            self.timer.stop()
            self.summary.setText(f'The room ended: {reason or "left"}')

    # rates

    def _rate(self, key, value, now, scale=1.0):
        """value per second since the previous refresh (scaled), or None the first time."""
        if value is None:
            return None
        before = self.previous.get(key)
        self.previous[key] = (now, value)
        if before is None or now <= before[0]:
            return None
        return (value - before[1]) * scale / (now - before[0])

    @staticmethod
    def _kbits(rate):
        return '' if rate is None else '%d' % round(rate)

    # the table

    def _refresh(self):
        session = self.session
        if session.state != 'terminated':
            session.request_stats()     # for the next refresh
        now = monotonic()
        rows = [self._own_row(now)]
        window = self.window_
        grid = window.grid
        for participant, tile in list(window.tiles.items()):
            rows.append(self._participant_row(participant, tile, grid, now))
        for participant in session.participants.values():
            if participant.type == 'bridge':
                rows.append(self._bridge_row(participant, now))
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem('' if value is None else str(value))
                if column == 0 and row == 0:
                    font = QFont(item.font())
                    font.setBold(True)
                    item.setFont(font)
                if column > 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
        self._summary(now)

    def _summary(self, now):
        session = self.session
        stats = session.stats
        outbound = stats.get(('outbound-rtp', 'video'), {})
        remote = stats.get(('remote-inbound-rtp', 'video'), {})
        parts = [f'Room {session.room}', f'state {session.state}']
        if session.video_codec:
            parts.append(f'sending {session.video_codec} ({session.video_encoder_name})')
        ssrc = getattr(session, '_offer_video_ssrc', None)
        if ssrc is not None:
            parts.append(f'video SSRC {ssrc}')
        if remote:
            rtt = remote.get('round-trip-time')
            parts.append('server: %s lost%s' % (remote.get('packets-lost', '?'), ', RTT %d ms' % round(rtt * 1000) if rtt else ''))
        elif outbound:
            parts.append('no RTCP report from the server about our video')
        if session.video_paused:
            parts.append('feeds paused (window out of sight)')
        self.summary.setText(' · '.join(parts))

    def _own_row(self, now):
        session = self.session
        window = self.window_
        stats = session.stats
        audio_out = stats.get(('outbound-rtp', 'audio'), {})
        video_out = stats.get(('outbound-rtp', 'video'), {})
        video_remote = stats.get(('remote-inbound-rtp', 'video'), {})
        audio_remote = stats.get(('remote-inbound-rtp', 'audio'), {})
        audio = 'muted' if session.audio_muted else ('speaking' if getattr(session, 'speaking', False) else 'on')
        from blink.webrtc.videoroom import VideoroomSession, VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_FPS, SCREEN_CHANGES_SIZE
        screen = VideoroomSession.sending_screen_device()
        if session.video_muted:
            video, resolution, fps = 'off (black)', '', ''
        elif screen and SCREEN_CHANGES_SIZE:
            video, resolution, fps = f'screen {session.video_codec or ""}', f'{SCREEN_WIDTH}x{SCREEN_HEIGHT}', SCREEN_FPS
        else:
            video, resolution, fps = f'{"screen" if screen else "camera"} {session.video_codec or ""}', f'{VIDEO_WIDTH}x{VIDEO_HEIGHT}', VIDEO_FPS
        grid = window.grid
        where = 'hidden' if window.local_tile.isHidden() else ('thumbnail' if grid.floating else 'grid')
        if session.id in session.speakers:
            where += ', speaker'
        if session.id in session.raised_hands:
            where += ', hand up'
        return ['Me (sending)', 'webrtc', session.state, audio,
                self._kbits(self._rate(('me', 'audio'), audio_out.get('bytes-sent'), now, 8 / 1000)), audio_remote.get('packets-lost', ''),
                video.strip(), resolution, fps,
                self._kbits(self._rate(('me', 'video'), video_out.get('bytes-sent'), now, 8 / 1000)), video_remote.get('packets-lost', ''),
                '', where]

    def _participant_row(self, participant, tile, grid, now):
        session = self.session
        stats = participant.stats
        audio_in = stats.get(('inbound-rtp', 'audio'), {})
        video_in = stats.get(('inbound-rtp', 'video'), {})
        key = participant.id
        audio = 'muted' if participant.muted else ('speaking' if participant.speaking else 'on')
        if participant.video is False:
            video = 'none'
        elif participant.video is None:
            video = '…'
        else:
            video = participant.video_codec or 'yes'
            if tile.degraded is not None:
                video += ', lost'
        image = tile.image
        resolution = f'{image.width()}x{image.height()}' if image is not None and not image.isNull() and participant.video else ''
        if participant.rotation:
            resolution += f' ↻{participant.rotation}°'
        fps = self._rate((key, 'frames'), getattr(tile, 'frames', None), now)
        if grid.carousel is not None and tile in grid.carousel.tiles:
            where = 'carousel'
        elif tile.isHidden():
            where = 'hidden'
        else:
            where = 'grid'
        if tile.demoted:
            where += ' (video lost)'
        if participant.id in session.speakers:
            where += ', speaker'
        if tile.hand:
            where += ', hand up'
        if tile.pinned:
            where += ', placed'
        jitter = video_in.get('jitter') if video_in else audio_in.get('jitter')
        return [f'{participant.name} <{participant.uri}>', participant.type, participant.state or ('in the mix' if participant.type == 'sip' else ''),
                audio,
                self._kbits(self._rate((key, 'audio'), audio_in.get('bytes-received'), now, 8 / 1000)), audio_in.get('packets-lost', ''),
                video, resolution, '' if fps is None else '%.0f' % fps,
                self._kbits(self._rate((key, 'video'), video_in.get('bytes-received'), now, 8 / 1000)), video_in.get('packets-lost', ''),
                '' if jitter is None else '%d ms' % round(jitter * 1000), where]

    def _bridge_row(self, participant, now):
        stats = participant.stats
        audio_in = stats.get(('inbound-rtp', 'audio'), {})
        return [f'SIP callers, mixed <{participant.uri}>', 'bridge', participant.state or '', 'mix',
                self._kbits(self._rate((participant.id, 'audio'), audio_in.get('bytes-received'), now, 8 / 1000)),
                audio_in.get('packets-lost', ''), '', '', '', '', '',
                '' if audio_in.get('jitter') is None else '%d ms' % round(audio_in['jitter'] * 1000), '']
