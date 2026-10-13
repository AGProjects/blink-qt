"""The session info panel shown in the main window under the audio call it describes."""

from PyQt6 import uic
from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QColor, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from blink.configuration.datatypes import GraphTimeScale
from blink.configuration.settings import BlinkSettings
from blink.resources import Resources
from blink.util import run_in_gui_thread, translate
from blink.widgets.graph import Graph
from blink.widgets.labels import ElidedLabel


__all__ = ['SessionInfoPanel']


class Container(object):
    pass


ui_class, base_class = uic.loadUiType(Resources.get('session_info_panel.ui'))


@implementer(IObserver)
class SessionInfoPanel(base_class, ui_class):
    """Status, media and network statistics of one session. There is one panel: the session
    list puts it under the call whose info button was pressed and points it at that session."""

    def __init__(self, parent=None):
        super(SessionInfoPanel, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)

        self.pixmaps = Container()
        self.pixmaps.direct_connection = QPixmap(Resources.get('icons/connection-direct.svg'))
        self.pixmaps.relay_connection = QPixmap(Resources.get('icons/connection-relay.svg'))
        self.pixmaps.unknown_connection = QPixmap(Resources.get('icons/connection-unknown.svg'))
        self.pixmaps.grey_lock = QPixmap(Resources.get('icons/lock-grey-12.svg'))
        self.pixmaps.green_lock = QPixmap(Resources.get('icons/lock-green-12.svg'))
        self.pixmaps.orange_lock = QPixmap(Resources.get('icons/lock-orange-12.svg'))

        self.audio_latency_graph = Graph([], color=QColor(0, 100, 215), over_boundary_color=QColor(255, 0, 100))
        self.video_latency_graph = Graph([], color=QColor(0, 215, 100), over_boundary_color=QColor(255, 100, 0), enabled=False)
        self.audio_packet_loss_graph = Graph([], color=QColor(0, 100, 215), over_boundary_color=QColor(255, 0, 100))
        self.video_packet_loss_graph = Graph([], color=QColor(0, 215, 100), over_boundary_color=QColor(255, 100, 0), enabled=False)
        self.incoming_traffic_graph = Graph([], color=QColor(255, 50, 50))
        self.outgoing_traffic_graph = Graph([], color=QColor(0, 100, 215))

        self.latency_graph.add_graph(self.audio_latency_graph)
        self.latency_graph.add_graph(self.video_latency_graph)
        self.packet_loss_graph.add_graph(self.audio_packet_loss_graph)
        self.packet_loss_graph.add_graph(self.video_packet_loss_graph)
        # the graph added 2nd is displayed on top
        self.traffic_graph.add_graph(self.incoming_traffic_graph)
        self.traffic_graph.add_graph(self.outgoing_traffic_graph)

        self.latency_graph.updated.connect(self._SH_LatencyGraphUpdated)
        self.packet_loss_graph.updated.connect(self._SH_PacketLossGraphUpdated)
        self.traffic_graph.updated.connect(self._SH_TrafficGraphUpdated)
        for graph in (self.latency_graph, self.packet_loss_graph, self.traffic_graph):
            graph.installEventFilter(self)

        # screen sharing is now a video call with the screen as camera, the MSRP screen row is obsolete
        self.screen_title_label.hide()
        self.screen_value_widget.hide()

        # the call tile above the panel already shows the duration
        self.duration_title_label.hide()
        self.duration_value_label.hide()

        self.audio_encryption_label.stream_type = 'audio'
        self.video_encryption_label.stream_type = 'video'

        self.remote_agent_title_label.setText(translate('chat_window', 'Remote UA'))

        self._add_rtp_path_rows()

        # titles left aligned in a column as wide as the longest title, values take all the remaining width
        layout = self.info_panel_container_layout
        layout.setColumnStretch(0, 0)
        layout.setColumnStretch(1, 1)
        for index in range(layout.count()):
            row, column, row_span, column_span = layout.getItemPosition(index)
            widget = layout.itemAt(index).widget()
            if column == 0 and column_span == 1 and isinstance(widget, QLabel):
                widget.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                widget.setSizePolicy(QSizePolicy.Policy.Fixed, widget.sizePolicy().verticalPolicy())

        self._apply_settings(scale=True)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange', sender=BlinkSettings())

        self.blink_session = None

    def _get_blink_session(self):
        return self.__dict__['blink_session']

    def _set_blink_session(self, blink_session):
        old_session = self.__dict__.get('blink_session', None)
        self.__dict__['blink_session'] = blink_session
        if blink_session is old_session:
            return
        notification_center = NotificationCenter()
        if old_session is not None:
            notification_center.remove_observer(self, sender=old_session)
        for graph in (self.audio_latency_graph, self.video_latency_graph, self.audio_packet_loss_graph,
                      self.video_packet_loss_graph, self.incoming_traffic_graph, self.outgoing_traffic_graph):
            graph.data = []
        if blink_session is not None:
            notification_center.add_observer(self, sender=blink_session)
            self.update_info(elements={'session', 'media', 'statistics', 'status'}, update_visibility=True)

    blink_session = property(_get_blink_session, _set_blink_session)
    del _get_blink_session, _set_blink_session

    def close_panel(self):
        self.blink_session = None
        self.hide()

    def _add_rtp_path_rows(self):
        # one row under audio and one under video with the remote RTP address and the ICE candidate pair
        layout = self.info_panel_container_layout
        moved = []
        for index in reversed(range(layout.count())):
            row, column, row_span, column_span = layout.getItemPosition(index)
            if row >= 7:
                moved.append((layout.takeAt(index), row, column, row_span, column_span))
        for item, row, column, row_span, column_span in moved:
            layout.addItem(item, row + 1, column, row_span, column_span)  # video goes to 8, the free row 8 to 9
        for stream_type, row in (('audio', 7), ('video', 9)):
            title_label = QLabel(translate('chat_window', 'Audio RTP' if stream_type == 'audio' else 'Video RTP'), self.info_panel_container_widget)
            title_label.setProperty('role', 'title')
            value_label = ElidedLabel(self.info_panel_container_widget)
            value_label.setProperty('role', 'value')
            value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            value_label.setIndent(0)
            layout.addWidget(title_label, row, 0)
            layout.addWidget(value_label, row, 1)
            setattr(self, '%s_path_title_label' % stream_type, title_label)
            setattr(self, '%s_path_value_label' % stream_type, value_label)

    @staticmethod
    def _format_address(address, port):
        if address is None:
            return None
        address = '[%s]' % address if ':' in address else address
        return '%s:%s' % (address, port) if port else address

    def _update_rtp_path(self, blink_session, stream_type, stream_info):
        title_label = getattr(self, '%s_path_title_label' % stream_type)
        value_label = getattr(self, '%s_path_value_label' % stream_type)
        remote = self._format_address(stream_info.remote_address, stream_info.remote_port)
        visible = stream_type in blink_session.streams and remote is not None
        title_label.setVisible(visible)
        value_label.setVisible(visible)
        if not visible:
            return
        local = self._format_address(stream_info.local_address, stream_info.local_port)
        tooltip = [translate('chat_window', 'Local: %s') % local, translate('chat_window', 'Remote: %s') % remote]
        if stream_info.ice_status == 'succeeded' and stream_info.local_rtp_candidate is not None and stream_info.remote_rtp_candidate is not None:
            local_type = stream_info.local_rtp_candidate.type.lower()
            remote_type = stream_info.remote_rtp_candidate.type.lower()
            topology = 'ICE %s \u2194 %s' % (local_type, remote_type)
            for name, candidate in ((translate('chat_window', 'Local candidate'), stream_info.local_rtp_candidate), (translate('chat_window', 'Remote candidate'), stream_info.remote_rtp_candidate)):
                address = self._format_address(getattr(candidate, 'address', None), getattr(candidate, 'port', None))
                tooltip.append('%s: %s%s' % (name, candidate.type.lower(), ' %s' % address if address else ''))
        elif stream_info.ice_status == 'disabled':
            topology = translate('chat_window', 'no ICE')
        elif stream_info.ice_status == 'failed':
            topology = translate('chat_window', 'ICE failed')
        elif stream_info.ice_status is None:
            topology = translate('chat_window', 'no ICE')
        else:
            topology = translate('chat_window', 'ICE %s') % stream_info.ice_status.replace('_', ' ')
        value_label.setText('%s  %s' % (remote, topology))
        value_label.setToolTip('\n'.join(tooltip))

    @staticmethod
    def _sum_traffic(first, second):
        # samples are taken at the same time for both streams; a stream added later has fewer, so align at the end
        first, second = list(first), list(second)
        if len(first) < len(second):
            first, second = second, first
        offset = len(first) - len(second)
        return first[:offset] + [a + b for a, b in zip(first[offset:], second)]

    # settings
    #
    def _apply_settings(self, scale=False):
        blink_settings = BlinkSettings()
        if scale:
            for graph in (self.latency_graph, self.packet_loss_graph, self.traffic_graph):
                graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
                graph.update()

    # content
    #
    def update_info(self, elements=set(), update_visibility=False):
        blink_session = self.blink_session
        if blink_session is None:
            return
        have_session = blink_session.state in ('connecting/*', 'connected/*', 'ending')

        if update_visibility:
            self.status_value_label.setEnabled(have_session)
            self.account_value_label.setEnabled(have_session)
            self.remote_agent_value_label.setEnabled(have_session)
            self.audio_value_widget.setEnabled('audio' in blink_session.streams)
            self.video_value_widget.setEnabled('video' in blink_session.streams)
            self.chat_value_widget.setEnabled('chat' in blink_session.streams)
            self.screen_value_widget.setEnabled('screen-sharing' in blink_session.streams)

        session_info = blink_session.info
        audio_info = session_info.streams.audio
        video_info = session_info.streams.video
        chat_info = session_info.streams.chat
        messages_info = session_info.streams.messages
        screen_info = session_info.streams.screen_sharing
        state = "%s" % blink_session.state

        if 'status' in elements and blink_session.state in ('initialized', 'connecting/*', 'connected/*', 'ended'):
            state_map = {'initialized': translate('chat_window', 'Disconnected'),
                         'connecting/dns_lookup': translate('chat_window', "Finding destination..."),
                         'connecting': translate('chat_window', "Connecting..."),
                         'connecting/ringing': translate('chat_window', "Ringing"),
                         'connecting/starting': translate('chat_window', "Starting media..."),
                         'connected': translate('chat_window', "Connected")}
            if blink_session.state == 'ended':
                self.status_value_label.setForegroundRole(QPalette.ColorRole.AlternateBase if blink_session.state.error else QPalette.ColorRole.WindowText)
                self.status_value_label.setText(blink_session.state.reason)
            elif state in state_map:
                self.status_value_label.setForegroundRole(QPalette.ColorRole.WindowText)
                self.status_value_label.setText(state_map[state])


        if 'session' in elements:
            self.account_value_label.setText(blink_session.account.id)
            self.remote_agent_value_label.setText(session_info.remote_user_agent or translate('chat_window', 'N/A'))

        if 'media' in elements:
            self._update_rtp_stream(blink_session, audio_info, self.audio_value_label, self.audio_connection_label, self.audio_encryption_label)
            self._update_rtp_stream(blink_session, video_info, self.video_value_label, self.video_connection_label, self.video_encryption_label)
            self._update_rtp_path(blink_session, 'audio', audio_info)
            self._update_rtp_path(blink_session, 'video', video_info)

            if any(len(path) > 1 for path in (chat_info.full_local_path, chat_info.full_remote_path)):
                self.chat_value_label.setText(translate('chat_window', "Using relay"))
                self.chat_connection_label.setPixmap(self.pixmaps.relay_connection)
                self.chat_connection_label.setToolTip(translate('chat_window', "Using relay"))
            elif chat_info.full_local_path and chat_info.full_remote_path:
                self.chat_value_label.setText(translate('chat_window', "Peer to peer"))
                self.chat_connection_label.setPixmap(self.pixmaps.direct_connection)
                self.chat_connection_label.setToolTip(translate('chat_window', "Peer to peer"))
            else:
                self.chat_value_label.setText(translate('chat_window', "N/A"))

            if chat_info.encryption is not None and chat_info.transport == 'tls':
                self.chat_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using TLS and {0.encryption} ({0.encryption_cipher})").format(chat_info))
            elif chat_info.encryption is not None:
                self.chat_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using {0.encryption} ({0.encryption_cipher})").format(chat_info))
            elif chat_info.transport == 'tls':
                self.chat_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using TLS"))
            else:
                self.chat_encryption_label.setToolTip(translate('chat_window', "Media is not encrypted"))
            if chat_info.encryption == 'OTR':
                self.chat_encryption_label.setPixmap(self.pixmaps.green_lock if chat_info.otr_verified else self.pixmaps.orange_lock)
            else:
                self.chat_encryption_label.setPixmap(self.pixmaps.grey_lock)
            self.chat_connection_label.setVisible(chat_info.remote_address is not None)
            self.chat_encryption_label.setVisible(chat_info.remote_address is not None and (chat_info.encryption is not None or chat_info.transport == 'tls'))

            if screen_info.remote_address is not None and screen_info.mode == 'active':
                self.screen_value_label.setText(translate('chat_window', "Viewing remote"))
            elif screen_info.remote_address is not None and screen_info.mode == 'passive':
                self.screen_value_label.setText(translate('chat_window', "Sharing local"))
            else:
                self.screen_value_label.setText(translate('chat_window', "N/A"))
            if any(len(path) > 1 for path in (screen_info.full_local_path, screen_info.full_remote_path)):
                self.screen_connection_label.setPixmap(self.pixmaps.relay_connection)
                self.screen_connection_label.setToolTip(translate('chat_window', "Using relay"))
            elif screen_info.full_local_path and screen_info.full_remote_path:
                self.screen_connection_label.setPixmap(self.pixmaps.direct_connection)
                self.screen_connection_label.setToolTip(translate('chat_window', "Peer to peer"))
            self.screen_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using TLS"))
            self.screen_connection_label.setVisible(screen_info.remote_address is not None)
            self.screen_encryption_label.setVisible(screen_info.remote_address is not None and screen_info.transport == 'tls')

        if 'statistics' in elements:
            self.audio_latency_graph.data = audio_info.latency
            self.video_latency_graph.data = video_info.latency
            self.audio_packet_loss_graph.data = audio_info.packet_loss
            self.video_packet_loss_graph.data = video_info.packet_loss
            self.incoming_traffic_graph.data = self._sum_traffic(audio_info.incoming_traffic, video_info.incoming_traffic)
            self.outgoing_traffic_graph.data = self._sum_traffic(audio_info.outgoing_traffic, video_info.outgoing_traffic)
            self.latency_graph.update()
            self.packet_loss_graph.update()
            self.traffic_graph.update()

    def _update_rtp_stream(self, blink_session, stream_info, value_label, connection_label, encryption_label):
        value_label.setText(stream_info.codec or translate('chat_window', 'N/A'))
        if stream_info.ice_status == 'succeeded':
            if 'relay' in {candidate.type.lower() for candidate in (stream_info.local_rtp_candidate, stream_info.remote_rtp_candidate)}:
                connection_label.setPixmap(self.pixmaps.relay_connection)
                connection_label.setToolTip(translate('chat_window', "Using relay"))
            else:
                connection_label.setPixmap(self.pixmaps.direct_connection)
                connection_label.setToolTip(translate('chat_window', "Peer to peer"))
        elif stream_info.ice_status == 'failed':
            connection_label.setPixmap(self.pixmaps.unknown_connection)
            connection_label.setToolTip(translate('chat_window', "Couldn't negotiate ICE"))
        elif stream_info.ice_status == 'disabled':
            if blink_session.contact is not None and blink_session.contact.type == 'bonjour':
                connection_label.setPixmap(self.pixmaps.direct_connection)
                connection_label.setToolTip(translate('chat_window', "Peer to peer"))
            else:
                connection_label.setPixmap(self.pixmaps.unknown_connection)
                connection_label.setToolTip(translate('chat_window', "ICE is disabled"))
        elif stream_info.ice_status is None:
            connection_label.setPixmap(self.pixmaps.unknown_connection)
            connection_label.setToolTip(translate('chat_window', "ICE is unavailable"))
        else:
            connection_label.setPixmap(self.pixmaps.unknown_connection)
            connection_label.setToolTip(translate('chat_window', "Negotiating ICE"))

        if stream_info.encryption is not None:
            encryption_label.setToolTip(translate('chat_window', "Media is encrypted using %s (%s)") % (stream_info.encryption, stream_info.encryption_cipher))
        else:
            encryption_label.setToolTip(translate('chat_window', "Media is not encrypted"))
        if stream_info.encryption == 'ZRTP':
            encryption_label.setPixmap(self.pixmaps.green_lock if stream_info.zrtp_verified else self.pixmaps.orange_lock)
        elif stream_info.encryption is not None:
            encryption_label.setPixmap(self.pixmaps.green_lock)
        else:
            encryption_label.setPixmap(self.pixmaps.grey_lock)

        connection_label.setVisible(stream_info.remote_address is not None)
        encryption_label.setVisible(stream_info.encryption is not None)

    # events
    #
    def eventFilter(self, watched, event):
        if watched in (self.latency_graph, self.packet_loss_graph, self.traffic_graph):
            if event.type() == QEvent.Type.Wheel and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                settings = BlinkSettings()
                wheel_delta = event.angleDelta().y()
                if wheel_delta > 0 and settings.chat_window.session_info.graph_time_scale > GraphTimeScale.min_value:
                    settings.chat_window.session_info.graph_time_scale -= 1
                    settings.save()
                elif wheel_delta < 0 and settings.chat_window.session_info.graph_time_scale < GraphTimeScale.max_value:
                    settings.chat_window.session_info.graph_time_scale += 1
                    settings.save()
                return True
        return False

    def _SH_LatencyGraphUpdated(self):
        self.latency_label.setText(translate('chat_window', 'Network Latency: %dms, max=%dms') % (max(self.audio_latency_graph.last_value, self.video_latency_graph.last_value), self.latency_graph.max_value))

    def _SH_PacketLossGraphUpdated(self):
        self.packet_loss_label.setText(translate('chat_window', 'Packet Loss: %.1f%%, max=%.1f%%') % (max(self.audio_packet_loss_graph.last_value, self.video_packet_loss_graph.last_value), self.packet_loss_graph.max_value))

    def _SH_TrafficGraphUpdated(self):
        from blink.chatwindow import TrafficNormalizer  # blink.chatwindow imports blink.sessions, which imports us
        if BlinkSettings().chat_window.session_info.bytes_per_second:
            incoming_traffic = TrafficNormalizer.normalize(self.incoming_traffic_graph.last_value)
            outgoing_traffic = TrafficNormalizer.normalize(self.outgoing_traffic_graph.last_value)
        else:
            incoming_traffic = TrafficNormalizer.normalize(self.incoming_traffic_graph.last_value * 8, bits_per_second=True)
            outgoing_traffic = TrafficNormalizer.normalize(self.outgoing_traffic_graph.last_value * 8, bits_per_second=True)
        self.traffic_label.setText(translate('chat_window', """<p>Traffic: <span style="font-family: sans-serif; color: #d70000;">%s</span> %s <span style="font-family: sans-serif; color: #0064d7;">%s</span> %s</p>""") % ("↓", incoming_traffic, "↑", outgoing_traffic))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        modified = notification.data.modified
        if 'chat_window.session_info.bytes_per_second' in modified:
            self.traffic_graph.update()
        self._apply_settings(scale='chat_window.session_info.graph_time_scale' in modified)

    def _NH_BlinkSessionInfoUpdated(self, notification):
        self.update_info(elements=notification.data.elements)

    def _NH_BlinkSessionDidChangeState(self, notification):
        self.update_info(elements={'status'}, update_visibility=True)

    def _NH_BlinkSessionDidAddStream(self, notification):
        self.update_info(elements={'media'}, update_visibility=True)

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        self.update_info(elements={'media'}, update_visibility=True)

    def _NH_BlinkSessionDidReinitializeForOutgoing(self, notification):
        self.update_info(elements={'session', 'media', 'statistics', 'status'}, update_visibility=True)

    _NH_BlinkSessionDidReinitializeForIncoming = _NH_BlinkSessionDidReinitializeForOutgoing


del ui_class, base_class
