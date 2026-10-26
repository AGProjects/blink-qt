"""Conference info: every participant of a video room with its feed's data, refreshed while open.

From the session (state, type, muted, speaking), the window (grid, thumbnail, carousel, speaker,
video lost) and each webrtcbin's own statistics (get-stats): what comes in from every feed
(inbound-rtp: bytes, packets lost, jitter) and what we send, from our camera and from each of our
extra videos (outbound-rtp, and remote-inbound-rtp: what the server reports of it, loss and round
trip). Rates are the difference between two refreshes; the totals up and down are their sums.

A narrow, tall window beside the room's window (not over it): the room and the totals on top, then
one entry per participant, its audio, video and place in the window under it. It is listed in the
Window menu while open (blink.mainwindow).
"""

from time import monotonic

from PyQt6.QtCore import Qt, QRect, QTimer
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import QFormLayout, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget


__all__ = ['ConferenceInfoPanel']


REFRESH = 2000      # ms
WIDTH, HEIGHT = 420, 640
GAP = 8             # px between the room's window and the panel


class ConferenceInfoPanel(QWidget):
    panels = {}         # window -> panel

    @classmethod
    def show_for(cls, window):
        panel = cls.panels.get(window)
        if panel is None:
            panel = cls.panels[window] = cls(window)
            panel.place()
        if panel.isMinimized():
            panel.showNormal()
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def __init__(self, window):
        # an ordinary window of its own, not a dialog: window managers keep a dialog without a parent above
        # every window of the application (the room's too); this one goes behind the room's when that is clicked
        super(ConferenceInfoPanel, self).__init__(None, Qt.WindowType.Window)
        self.window_ = window
        self.session = window.session
        self.previous = {}      # key -> (time, value) for the rates
        self.items = {}         # participant key -> its tree item
        self.totals = dict(up=0.0, down=0.0)
        self.setWindowTitle(f'Conference info: {window.title}')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(WIDTH, HEIGHT)
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(3)
        self.fields = {}
        for name in ('Room', 'State', 'Sending', 'Server', 'Upload', 'Download'):
            label = QLabel(self)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            if name in ('Upload', 'Download'):
                font = QFont(label.font())
                font.setBold(True)
                label.setFont(font)
            form.addRow(name, label)
            self.fields[name] = label
        layout.addLayout(form)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setIndentation(14)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tree.setColumnWidth(0, 110)
        layout.addWidget(self.tree, 1)

        note = QLabel('Rates over the last 2 s; lost: packets lost in total. SIP callers come mixed through the audio bridge.', self)
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
        window.destroyed.connect(self.close)

    def place(self):
        """Beside the room's window, on the side with room for it; over its edge only when the screen has no room either side."""
        frame = self.window_.frameGeometry()
        screen = (self.window_.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        width = self.width()
        height = min(max(HEIGHT, frame.height()), screen.height())
        y = max(screen.top(), min(frame.top(), screen.bottom() - height))
        if frame.right() + GAP + width <= screen.right():
            x = frame.right() + GAP
        elif frame.left() - GAP - width >= screen.left():
            x = frame.left() - GAP - width
        else:
            x = screen.right() - width      # no room: at the screen's edge, over as little of the window as can be
        self.setGeometry(QRect(x, y, width, height))

    def closeEvent(self, event):
        self.timer.stop()
        self.panels.pop(self.window_, None)
        super(ConferenceInfoPanel, self).closeEvent(event)

    def _SH_StateChanged(self, state, reason):
        if state == 'terminated':
            self.timer.stop()
            self.fields['State'].setText(f'ended: {reason or "left"}')

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

    def _kbits(self, key, value, now, direction):
        """The rate of a byte counter in kbit/s, added to the total up or down; None the first time."""
        rate = self._rate(key, value, now, 8 / 1000)
        if rate is not None and rate > 0:
            self.totals[direction] += rate
        return rate

    @staticmethod
    def _join(*parts):
        return ' · '.join(str(part) for part in parts if part not in (None, ''))

    @staticmethod
    def _rate_text(rate):
        return None if rate is None else '%d kbit/s' % round(rate)

    @staticmethod
    def _lost(stats):
        lost = stats.get('packets-lost')
        return None if lost is None else f'{lost} lost'

    # the tree

    def _refresh(self):
        session = self.session
        if session.state != 'terminated':
            session.request_stats()     # for the next refresh
        now = monotonic()
        self.totals = dict(up=0.0, down=0.0)
        window = self.window_
        grid = window.grid
        # me, my extra videos right under me, then the others
        entries = [self._own_entry(now)]
        others = []
        for participant, tile in list(window.tiles.items()):
            if getattr(participant, 'extra_video', None) is not None:
                entries.append(self._extra_entry(participant, tile, grid, now))
            else:
                others.append(self._participant_entry(participant, tile, grid, now))
        entries += others
        for participant in session.participants.values():
            if participant.type == 'bridge':
                entries.append(self._bridge_entry(participant, now))
        self._show(entries)
        self._summary()

    def _show(self, entries):
        """entries: [(key, title, status, [(field, value)], bold)], in order; items kept (and their expansion) across refreshes."""
        keys = [entry[0] for entry in entries]
        for key in list(self.items):
            if key not in keys:
                item = self.items.pop(key)
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        for position, (key, title, status, fields, bold) in enumerate(entries):
            item = self.items.get(key)
            if item is None:
                item = self.items[key] = QTreeWidgetItem()
                item.setFirstColumnSpanned(False)
                if bold:
                    font = QFont(item.font(0))
                    font.setBold(True)
                    item.setFont(0, font)
                self.tree.insertTopLevelItem(position, item)
                item.setExpanded(True)
            elif self.tree.indexOfTopLevelItem(item) != position:
                expanded = item.isExpanded()
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
                self.tree.insertTopLevelItem(position, item)
                item.setExpanded(expanded)
            item.setText(0, title)
            item.setToolTip(0, title)
            item.setText(1, status or '')
            item.setForeground(1, self.palette().placeholderText())
            fields = [(field, value) for field, value in fields if value not in (None, '')]
            while item.childCount() > len(fields):
                item.removeChild(item.child(item.childCount() - 1))
            for number, (field, value) in enumerate(fields):
                child = item.child(number) if number < item.childCount() else QTreeWidgetItem(item)
                child.setText(0, field)
                child.setText(1, str(value))
                child.setToolTip(1, str(value))
        self.tree.resizeColumnToContents(0)
        if self.tree.columnWidth(0) > self.tree.viewport().width() // 2:
            self.tree.setColumnWidth(0, self.tree.viewport().width() // 2)

    def _summary(self):
        session = self.session
        stats = session.stats
        outbound = stats.get(('outbound-rtp', 'video'), {})
        remote = stats.get(('remote-inbound-rtp', 'video'), {})
        self.fields['Room'].setText(session.room)
        state = session.state
        if session.video_paused:
            state += ' (feeds paused: the window is out of sight)'
        if session.state != 'terminated':
            self.fields['State'].setText(state)
        sending = None
        if session.video_codec:
            sending = f'{session.video_codec} with {session.video_encoder_name}'
            ssrc = getattr(session, '_offer_video_ssrc', None)
            if ssrc is not None:
                sending += f', SSRC {ssrc}'
        self.fields['Sending'].setText(sending or '…')
        if remote:
            rtt = remote.get('round-trip-time')
            self.fields['Server'].setText(self._join(f'{remote.get("packets-lost", "?")} of our video packets lost', 'RTT %d ms' % round(rtt * 1000) if rtt else None))
        else:
            self.fields['Server'].setText('no RTCP report about our video yet' if outbound else '…')
        self.fields['Upload'].setText('↑ %d kbit/s' % round(self.totals['up']))
        self.fields['Download'].setText('↓ %d kbit/s' % round(self.totals['down']))

    def _own_entry(self, now):
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
            video = 'off (black)'
        elif screen and SCREEN_CHANGES_SIZE:
            video = self._join(f'screen {session.video_codec or ""}'.strip(), f'{SCREEN_WIDTH}x{SCREEN_HEIGHT}', f'{SCREEN_FPS} fps')
        else:
            video = self._join(f'{"screen" if screen else "camera"} {session.video_codec or ""}'.strip(), f'{VIDEO_WIDTH}x{VIDEO_HEIGHT}', f'{VIDEO_FPS} fps')
        grid = window.grid
        where = ['hidden' if window.local_tile.isHidden() else ('thumbnail' if grid.floating else 'grid')]
        if session.id in session.speakers:
            where.append('speaker')
        if session.id in session.raised_hands:
            where.append('hand up')
        audio_rate = self._kbits(('me', 'audio'), audio_out.get('bytes-sent'), now, 'up')
        video_rate = self._kbits(('me', 'video'), video_out.get('bytes-sent'), now, 'up')
        fields = [('Audio', self._join(audio, self._rate_text(audio_rate), self._lost(audio_remote))),
                  ('Video', video),
                  ('Video sent', self._join(self._rate_text(video_rate), self._lost(video_remote))),
                  ('Shown', ', '.join(where))]
        return ('me', 'Me', self._join(session.state, f'↑ {round((audio_rate or 0) + (video_rate or 0))} kbit/s'), fields, True)

    def _extra_entry(self, participant, tile, grid, now):
        """One of our extra videos: what it sends."""
        session = self.session
        extra = participant.extra_video
        stats = participant.stats
        video_out = stats.get(('outbound-rtp', 'video'), {})
        video_remote = stats.get(('remote-inbound-rtp', 'video'), {})
        width, height = extra.size
        fps = self._rate((participant.id, 'frames'), getattr(tile, 'frames', None), now)
        where = ['hidden' if tile.isHidden() else ('carousel' if grid.carousel is not None and tile in grid.carousel.tiles else 'grid')]
        if participant.id in session.speakers:
            where.append('speaker')
        kind = 'RTSP camera' if extra.source['kind'] == 'rtsp' else 'camera'
        rate = self._kbits((participant.id, 'video-sent'), video_out.get('bytes-sent'), now, 'up')
        fields = [('Source', extra.source['url'] if extra.source['kind'] == 'rtsp' else extra.source.get('name')),
                  ('Video', self._join(session.video_codec, f'{width}x{height}', None if fps is None else '%.0f fps' % fps)),
                  ('Video sent', self._join(self._rate_text(rate), self._lost(video_remote))),
                  ('Shown', ', '.join(where))]
        return (participant.id, f'{extra.label} (mine)', self._join(extra.state, kind, None if rate is None else f'↑ {round(rate)} kbit/s'), fields, True)

    def _participant_entry(self, participant, tile, grid, now):
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
        resolution = f'{image.width()}x{image.height()}' if image is not None and not image.isNull() and participant.video else None
        if resolution and participant.rotation:
            resolution += f' ↻{participant.rotation}°'
        fps = self._rate((key, 'frames'), getattr(tile, 'frames', None), now)
        if grid.carousel is not None and tile in grid.carousel.tiles:
            where = ['carousel']
        elif tile.isHidden():
            where = ['hidden']
        else:
            where = ['grid']
        if tile.demoted:
            where.append('video lost')
        if participant.id in session.speakers:
            where.append('speaker')
        if tile.hand:
            where.append('hand up')
        if tile.pinned:
            where.append('placed')
        jitter = video_in.get('jitter') if video_in else audio_in.get('jitter')
        audio_rate = self._kbits((key, 'audio'), audio_in.get('bytes-received'), now, 'down')
        video_rate = self._kbits((key, 'video'), video_in.get('bytes-received'), now, 'down')
        fields = [('Address', participant.uri),
                  ('Audio', self._join(audio, self._rate_text(audio_rate), self._lost(audio_in))),
                  ('Video', self._join(video, resolution, None if fps is None or not participant.video else '%.0f fps' % fps)),
                  ('Video received', self._join(self._rate_text(video_rate), self._lost(video_in))),
                  ('Jitter', None if jitter is None else '%d ms' % round(jitter * 1000)),
                  ('Shown', ', '.join(where))]
        state = participant.state or ('in the mix' if participant.type == 'sip' else '')
        down = (audio_rate or 0) + (video_rate or 0)
        status = self._join(state, 'SIP' if participant.type == 'sip' else None, f'↓ {round(down)} kbit/s' if down else None)
        return (key, participant.name, status, fields, False)

    def _bridge_entry(self, participant, now):
        stats = participant.stats
        audio_in = stats.get(('inbound-rtp', 'audio'), {})
        rate = self._kbits((participant.id, 'audio'), audio_in.get('bytes-received'), now, 'down')
        fields = [('Address', participant.uri),
                  ('Audio', self._join('mix', self._rate_text(rate), self._lost(audio_in))),
                  ('Jitter', None if audio_in.get('jitter') is None else '%d ms' % round(audio_in['jitter'] * 1000))]
        return (participant.id, 'SIP callers, mixed', self._join(participant.state, None if rate is None else f'↓ {round(rate)} kbit/s'), fields, False)
