# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['Conference', 'SessionItem', 'SessionModel', 'SessionListView', 'SessionManager']

import bisect
import os
import cPickle as pickle
import re
from datetime import datetime, timedelta
from functools import partial

from PyQt4 import uic
from PyQt4.QtCore import Qt, QAbstractListModel, QByteArray, QEvent, QMimeData, QModelIndex, QObject, QSize, QStringList, QTimer, pyqtSignal
from PyQt4.QtGui  import QAction, QBrush, QColor, QDrag, QLinearGradient, QListView, QMenu, QPainter, QPen, QPixmap, QStyle, QStyledItemDelegate

from application.notification import IObserver, NotificationCenter
from application.python.util import Null, Singleton
from zope.interface import implements

from sipsimple.account import Account, AccountManager
from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer
from sipsimple.conference import AudioConference
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPCoreError, SIPURI, ToHeader
from sipsimple.lookup import DNSLookup
from sipsimple.session import Session
from sipsimple.streams import MediaStreamRegistry
from sipsimple.util import limit

from blink.configuration.datatypes import DefaultPath
from blink.resources import Resources
from blink.util import call_later, run_in_gui_thread
from blink.widgets.buttons import LeftSegment, MiddleSegment, RightSegment, SwitchViewButton


class Status(unicode):
    def __new__(cls, value, color='black'):
        instance = unicode.__new__(cls, value)
        instance.color = color
        return instance


class SessionItem(QObject):
    implements(IObserver)

    activated = pyqtSignal()
    deactivated = pyqtSignal()
    ended = pyqtSignal()

    def __init__(self, name, uri, session, audio_stream=None, video_stream=None):
        super(SessionItem, self).__init__()
        if (audio_stream, video_stream) == (None, None):
            raise ValueError('SessionItem must represent at least one audio or video stream')
        self.name = name
        self.uri = uri
        self.session = session
        self.audio_stream = audio_stream
        self.video_stream = video_stream
        self.widget = Null
        self.conference = None
        self.type = 'Video' if video_stream else 'Audio'
        self.codec_info = ''
        self.tls = False
        self.srtp = False
        self.duration = timedelta(0)
        self.latency = 0
        self.packet_loss = 0
        self.status = None
        self.active = False
        self.timer = QTimer()
        self.offer_in_progress = False
        self.local_hold = False
        self.remote_hold = False
        self.terminated = False
        self.outbound_ringtone = Null
        if self.audio_stream is None:
            self.hold_tone = Null

        from blink import Blink
        self.remote_party_name = None
        for contact in Blink().main_window.contact_model.iter_contacts():
            if uri.matches(contact.uri) or any(uri.matches(alias) for alias in contact.sip_aliases):
                self.remote_party_name = contact.name
                break
        if not self.remote_party_name:
            address = '%s@%s' % (uri.user, uri.host)
            match = re.match(r'^(?P<number>(\+|00)[1-9][0-9]\d{5,15})@(\d{1,3}\.){3}\d{1,3}$', address)
            self.remote_party_name = name or (match.group('number') if match else address)

        self.timer.timeout.connect(self._SH_TimerFired)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)

    def __reduce__(self):
        return (self.__class__, (self.name, self.uri, Null, Null, Null), None)

    @property
    def pending_removal(self):
        return self.audio_stream is None and self.video_stream is None

    def _get_audio_stream(self):
        return self.__dict__['audio_stream']

    def _set_audio_stream(self, stream):
        notification_center = NotificationCenter()
        old_stream = self.__dict__.get('audio_stream', None)
        self.__dict__['audio_stream'] = stream
        if old_stream is not None:
            notification_center.remove_observer(self, sender=old_stream)
            self.hold_tone = Null
        if stream is not None:
            notification_center.add_observer(self, sender=stream)
            self.hold_tone = WavePlayer(stream.bridge.mixer, Resources.get('sounds/hold_tone.wav'), loop_count=0, pause_time=45, volume=30)
            stream.bridge.add(self.hold_tone)

    audio_stream = property(_get_audio_stream, _set_audio_stream)
    del _get_audio_stream, _set_audio_stream

    def _get_video_stream(self):
        return self.__dict__['video_stream']

    def _set_video_stream(self, stream):
        notification_center = NotificationCenter()
        old_stream = self.__dict__.get('video_stream', None)
        self.__dict__['video_stream'] = stream
        if old_stream is not None:
            notification_center.remove_observer(self, sender=old_stream)
        if stream is not None:
            notification_center.add_observer(self, sender=stream)

    video_stream = property(_get_video_stream, _set_video_stream)
    del _get_video_stream, _set_video_stream

    def _get_conference(self):
        return self.__dict__['conference']

    def _set_conference(self, conference):
        old_conference = self.__dict__.get('conference', Null)
        if old_conference is conference:
            return
        self.__dict__['conference'] = conference
        if old_conference is not None:
            old_conference.remove_session(self)
        if conference is not None:
            conference.add_session(self)
        elif self.widget.mute_button.isChecked():
            self.widget.mute_button.click()

    conference = property(_get_conference, _set_conference)
    del _get_conference, _set_conference

    def _get_type(self):
        return self.__dict__['type']

    def _set_type(self, value):
        if self.__dict__.get('type', Null) == value:
            return
        self.__dict__['type'] = value
        self.widget.stream_info_label.session_type = value

    type = property(_get_type, _set_type)
    del _get_type, _set_type

    def _get_codec_info(self):
        return self.__dict__['codec_info']

    def _set_codec_info(self, value):
        if self.__dict__.get('codec_info', None) == value:
            return
        self.__dict__['codec_info'] = value
        self.widget.stream_info_label.codec_info = value

    codec_info = property(_get_codec_info, _set_codec_info)
    del _get_codec_info, _set_codec_info

    def _get_tls(self):
        return self.__dict__['tls']

    def _set_tls(self, value):
        if self.__dict__.get('tls', None) == value:
            return
        self.__dict__['tls'] = value
        self.widget.tls_label.setVisible(bool(value))

    tls = property(_get_tls, _set_tls)
    del _get_tls, _set_tls

    def _get_srtp(self):
        return self.__dict__['srtp']

    def _set_srtp(self, value):
        if self.__dict__.get('srtp', None) == value:
            return
        self.__dict__['srtp'] = value
        self.widget.srtp_label.setVisible(bool(value))

    srtp = property(_get_srtp, _set_srtp)
    del _get_srtp, _set_srtp

    def _get_duration(self):
        return self.__dict__['duration']
    
    def _set_duration(self, value):
        if self.__dict__.get('duration', None) == value:
            return
        self.__dict__['duration'] = value
        self.widget.duration_label.value = value

    duration = property(_get_duration, _set_duration)
    del _get_duration, _set_duration

    def _get_latency(self):
        return self.__dict__['latency']

    def _set_latency(self, value):
        if self.__dict__.get('latency', None) == value:
            return
        self.__dict__['latency'] = value
        self.widget.latency_label.value = value

    latency = property(_get_latency, _set_latency)
    del _get_latency, _set_latency

    def _get_packet_loss(self):
        return self.__dict__['packet_loss']

    def _set_packet_loss(self, value):
        if self.__dict__.get('packet_loss', None) == value:
            return
        self.__dict__['packet_loss'] = value
        self.widget.packet_loss_label.value = value

    packet_loss = property(_get_packet_loss, _set_packet_loss)
    del _get_packet_loss, _set_packet_loss

    def _get_status(self):
        return self.__dict__['status']

    def _set_status(self, value):
        if self.__dict__.get('status', Null) == value:
            return
        self.__dict__['status'] = value
        self.widget.status_label.value = value

    status = property(_get_status, _set_status)
    del _get_status, _set_status

    def _get_active(self):
        return self.__dict__['active']

    def _set_active(self, value):
        value = bool(value)
        if self.__dict__.get('active', None) == value:
            return
        self.__dict__['active'] = value
        if self.audio_stream:
            self.audio_stream.device.output_muted = not value
        if value:
            self.activated.emit()
        else:
            self.deactivated.emit()

    active = property(_get_active, _set_active)
    del _get_active, _set_active

    def _get_widget(self):
        return self.__dict__['widget']

    def _set_widget(self, widget):
        old_widget = self.__dict__.get('widget', Null)
        self.__dict__['widget'] = widget
        if old_widget is not Null:
            old_widget.mute_button.clicked.disconnect(self._SH_MuteButtonClicked)
            old_widget.hold_button.clicked.disconnect(self._SH_HoldButtonClicked)
            old_widget.record_button.clicked.disconnect(self._SH_RecordButtonClicked)
            old_widget.hangup_button.clicked.disconnect(self._SH_HangupButtonClicked)
            widget.mute_button.setEnabled(old_widget.mute_button.isEnabled())
            widget.mute_button.setChecked(old_widget.mute_button.isChecked())
            widget.hold_button.setEnabled(old_widget.hold_button.isEnabled())
            widget.hold_button.setChecked(old_widget.hold_button.isChecked())
            widget.record_button.setEnabled(old_widget.record_button.isEnabled())
            widget.record_button.setChecked(old_widget.record_button.isChecked())
            widget.hangup_button.setEnabled(old_widget.hangup_button.isEnabled())
        widget.mute_button.clicked.connect(self._SH_MuteButtonClicked)
        widget.hold_button.clicked.connect(self._SH_HoldButtonClicked)
        widget.record_button.clicked.connect(self._SH_RecordButtonClicked)
        widget.hangup_button.clicked.connect(self._SH_HangupButtonClicked)

    widget = property(_get_widget, _set_widget)
    del _get_widget, _set_widget

    def connect(self):
        self.offer_in_progress = True
        account = self.session.account
        settings = SIPSimpleSettings()
        if isinstance(account, Account) and account.sip.outbound_proxy is not None:
            proxy = account.sip.outbound_proxy
            uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
        else:
            uri = self.uri
        self.status = Status('Looking up destination')
        lookup = DNSLookup()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=lookup)
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

    def hold(self):
        if not self.pending_removal and not self.local_hold:
            self.local_hold = True
            self.session.hold()
            self.hold_tone.start()
            self.widget.hold_button.setChecked(True)
            if not self.offer_in_progress:
                self.status = Status('On hold', color='#000090')

    def unhold(self):
        if not self.pending_removal and self.local_hold:
            self.local_hold = False
            self.widget.hold_button.setChecked(False)
            self.session.unhold()

    def send_dtmf(self, digit):
        if self.audio_stream is not None:
            try:
                self.audio_stream.send_dtmf(digit)
            except RuntimeError:
                pass
            else:
                digit_map = {'*': 'star'}
                filename = 'sounds/dtmf_%s_tone.wav' % digit_map.get(digit, digit)
                player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get(filename))
                notification_center = NotificationCenter()
                notification_center.add_observer(self, sender=player)
                SIPApplication.voice_audio_bridge.add(player)
                player.start()

    def end(self):
        if self.session.state is None:
            self.audio_stream = None
            self.video_stream = None
            self.status = Status('Call canceled', color='#900000')
            self._cleanup()
        else:
            self.session.end()

    def _cleanup(self):
        self.timer.stop()
        self.widget.mute_button.setEnabled(False)
        self.widget.hold_button.setEnabled(False)
        self.widget.record_button.setEnabled(False)
        self.widget.hangup_button.setEnabled(False)

        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.session)

        player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get('sounds/hangup_tone.wav'), volume=60)
        notification_center.add_observer(self, sender=player)
        SIPApplication.voice_audio_bridge.add(player)
        player.start()

        self.ended.emit()

    def _reset_status(self):
        if self.pending_removal or self.offer_in_progress:
            return
        if self.local_hold:
            self.status = Status('On hold', color='#000090')
        elif self.remote_hold:
            self.status = Status('Hold by remote', color='#000090')
        else:
            self.status = None

    def _SH_HangupButtonClicked(self):
        self.end()

    def _SH_HoldButtonClicked(self, checked):
        if checked:
            self.hold()
        else:
            self.unhold()

    def _SH_MuteButtonClicked(self, checked):
        if self.audio_stream is not None:
            self.audio_stream.muted = checked

    def _SH_RecordButtonClicked(self, checked):
        if self.audio_stream is not None:
            if checked:
                settings = SIPSimpleSettings()
                direction = self.session.direction
                remote = "%s@%s" % (self.session.remote_identity.uri.user, self.session.remote_identity.uri.host)
                filename = "%s-%s-%s.wav" % (datetime.now().strftime("%Y%m%d-%H%M%S"), remote, direction)
                path = os.path.join(settings.audio.recordings_directory.normalized, self.session.account.id)
                try:
                    self.audio_stream.start_recording(os.path.join(path, filename))
                except (SIPCoreError, IOError, OSError), e:
                    print 'Failed to record: %s' % e
            else:
                self.audio_stream.stop_recording()

    def _SH_TimerFired(self):
        stats = self.video_stream.statistics if self.video_stream else self.audio_stream.statistics
        self.latency = stats['rtt']['avg'] / 1000
        self.packet_loss = int(stats['rx']['packets_lost']*100.0/stats['rx']['packets']) if stats['rx']['packets'] else 0
        self.duration += timedelta(seconds=1)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AudioStreamICENegotiationStateDidChange(self, notification):
        if notification.data.state == 'ICE Candidates Gathering':
            self.status = Status('Gathering ICE candidates')
        elif notification.data.state == 'ICE Session Initialized':
            self.status = Status('Connecting...')
        elif notification.data.state == 'ICE Negotiation In Progress':
            self.status = Status('Negotiating ICE')

    def _NH_AudioStreamGotDTMF(self, notification):
        digit_map = {'*': 'star'}
        filename = 'sounds/dtmf_%s_tone.wav' % digit_map.get(notification.data.digit, notification.data.digit)
        player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get(filename))
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=player)
        SIPApplication.voice_audio_bridge.add(player)
        player.start()

    def _NH_AudioStreamDidStartRecordingAudio(self, notification):
        self.widget.record_button.setChecked(True)

    def _NH_AudioStreamWillStopRecordingAudio(self, notification):
        self.widget.record_button.setChecked(False)

    def _NH_DNSLookupDidSucceed(self, notification):
        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        if self.pending_removal:
            return
        streams = []
        if self.audio_stream:
            streams.append(self.audio_stream)
            outbound_ringtone = settings.sounds.outbound_ringtone
            if outbound_ringtone:
                self.outbound_ringtone = WavePlayer(self.audio_stream.mixer, outbound_ringtone.path, outbound_ringtone.volume, loop_count=0, pause_time=5)
                self.audio_stream.bridge.add(self.outbound_ringtone)
        if self.video_stream:
            streams.append(self.video_stream)
        self.status = Status('Connecting...')
        self.session.connect(ToHeader(self.uri), notification.data.result, streams)

    def _NH_DNSLookupDidFail(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)
        if self.pending_removal:
            return
        self.audio_stream = None
        self.video_stream = None
        self.status = Status('Destination not found', color='#900000')
        self._cleanup()

    def _NH_MediaStreamDidStart(self, notification):
        if notification.sender is self.audio_stream:
            self.widget.mute_button.setEnabled(True)
            self.widget.hold_button.setEnabled(True)
            self.widget.record_button.setEnabled(True)

    def _NH_SIPSessionGotRingIndication(self, notification):
        self.status = Status('Ringing...')
        self.outbound_ringtone.start()

    def _NH_SIPSessionWillStart(self, notification):
        self.outbound_ringtone.stop()

    def _NH_SIPSessionDidStart(self, notification):
        if self.audio_stream not in notification.data.streams:
            self.audio_stream = None
        if self.video_stream not in notification.data.streams:
            self.video_stream = None
        if not self.local_hold:
            self.offer_in_progress = False
        if not self.pending_removal:
            self.timer.start(1000)
            self.status = None
            if self.video_stream is not None:
                self.type = 'HD Video' if self.video_stream.bit_rate/1024 >= 512 else 'Video'
            else:
                self.type = 'HD Audio' if self.audio_stream.sample_rate/1000 >= 16 else 'Audio'
            codecs = []
            if self.video_stream is not None:
                codecs.append('%s %dkbit' % (self.video_stream.codec, self.video_stream.bit_rate/1024))
            if self.audio_stream is not None:
                codecs.append('%s %dkHz' % (self.audio_stream.codec, self.audio_stream.sample_rate/1000))
            self.codec_info = ', '.join(codecs)
            self.status = Status('Connected')
            call_later(1, self._reset_status)
        else:
            self.status = Status('%s refused' % self.type, color='#900000')
            self._cleanup()

    def _NH_SIPSessionDidFail(self, notification):
        self.audio_stream = None
        self.video_stream = None
        self.offer_in_progress = False
        if notification.data.failure_reason == 'user request':
            if notification.data.code == 487:
                reason = 'Call canceled'
            else:
                reason = notification.data.reason
        else:
            reason = notification.data.failure_reason
        self.status = Status(reason, color='#900000')
        self.outbound_ringtone.stop()
        self._cleanup()

    def _NH_SIPSessionDidEnd(self, notification):
        self.audio_stream = None
        self.video_stream = None
        self.offer_in_progress = False
        self.status = Status('Call ended' if notification.data.originator=='local' else 'Call ended by remote')
        self._cleanup()

    def _NH_SIPSessionDidChangeHoldState(self, notification):
        if notification.data.originator == 'remote':
            self.remote_hold = notification.data.on_hold
        if self.local_hold:
            if not self.offer_in_progress:
                self.status = Status('On hold', color='#000090')
        elif self.remote_hold:
            if not self.offer_in_progress:
                self.status = Status('Hold by remote', color='#000090')
            self.hold_tone.start()
        else:
            self.status = None
            self.hold_tone.stop()
        self.offer_in_progress = False

    def _NH_SIPSessionGotAcceptProposal(self, notification):
        if self.audio_stream not in notification.data.proposed_streams and self.video_stream not in notification.data.proposed_streams:
            return
        if self.audio_stream in notification.data.proposed_streams and self.audio_stream not in notification.data.streams:
            self.audio_stream = None
        if self.video_stream in notification.data.proposed_streams and self.video_stream not in notification.data.streams:
            self.video_stream = None
        self.offer_in_progress = False
        if not self.pending_removal:
            if not self.timer.isActive():
                self.timer.start()
            if self.video_stream is not None:
                self.type = 'HD Video' if self.video_stream.bit_rate/1024 >= 512 else 'Video'
            else:
                self.type = 'HD Audio' if self.audio_stream.sample_rate/1000 >= 16 else 'Audio'
            codecs = []
            if self.video_stream is not None:
                codecs.append('%s %dkbit' % (self.video_stream.codec, self.video_stream.bit_rate/1024))
            if self.audio_stream is not None:
                codecs.append('%s %dkHz' % (self.audio_stream.codec, self.audio_stream.sample_rate/1000))
            self.codec_info = ', '.join(codecs)
            self.status = Status('Connected')
            call_later(1, self._reset_status)
        else:
            self.status = Status('%s refused' % self.type, color='#900000')
            self._cleanup()

    def _NH_SIPSessionGotRejectProposal(self, notification):
        if self.audio_stream not in notification.data.streams and self.video_stream not in notification.data.streams:
            return
        if self.audio_stream in notification.data.streams:
            self.audio_stream = None
        if self.video_stream in notification.data.streams:
            video_refused = True
            self.video_stream = None
        else:
            video_refused = False
        self.offer_in_progress = False
        if not self.pending_removal:
            if self.video_stream is not None:
                self.type = 'HD Video' if self.video_stream.bit_rate/1024 >= 512 else 'Video'
            else:
                self.type = 'HD Audio' if self.audio_stream.sample_rate/1000 >= 16 else 'Audio'
            codecs = []
            if self.video_stream is not None:
                codecs.append('%s %dkbit' % (self.video_stream.codec, self.video_stream.bit_rate/1024))
            if self.audio_stream is not None:
                codecs.append('%s %dkHz' % (self.audio_stream.codec, self.audio_stream.sample_rate/1000))
            self.codec_info = ', '.join(codecs)
            self.status = Status('Video refused' if video_refused else 'Audio refused', color='#900000')
            call_later(1, self._reset_status)
        else:
            self.status = Status('%s refused' % self.type, color='#900000')
            self._cleanup()

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        if notification.data.action != 'remove':
            return
        if self.audio_stream not in notification.data.streams and self.video_stream not in notification.data.streams:
            return
        if self.audio_stream in notification.data.streams:
            self.audio_stream = None
        if self.video_stream in notification.data.streams:
            video_removed = True
            self.video_stream = None
        else:
            video_removed = False
        self.offer_in_progress = False
        if not self.pending_removal:
            if self.video_stream is not None:
                self.type = 'HD Video' if self.video_stream.bit_rate/1024 >= 512 else 'Video'
            else:
                self.type = 'HD Audio' if self.audio_stream.sample_rate/1000 >= 16 else 'Audio'
            codecs = []
            if self.video_stream is not None:
                codecs.append('%s %dkbit' % (self.video_stream.codec, self.video_stream.bit_rate/1024))
            if self.audio_stream is not None:
                codecs.append('%s %dkHz' % (self.audio_stream.codec, self.audio_stream.sample_rate/1000))
            self.codec_info = ', '.join(codecs)
            self.status = Status('Video removed' if video_removed else 'Audio removed', color='#900000')
            call_later(1, self._reset_status)
        else:
            self.status = Status('%s removed' % self.type, color='#900000')
            self._cleanup()

    def _NH_WavePlayerDidFail(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)

    def _NH_WavePlayerDidEnd(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=notification.sender)


class Conference(object):
    def __init__(self):
        self.sessions = []
        self.audio_conference = AudioConference()
        self.audio_conference.hold()

    def add_session(self, session):
        if self.sessions:
            self.sessions[-1].widget.conference_position = Top if len(self.sessions)==1 else Middle
            session.widget.conference_position = Bottom
        else:
            session.widget.conference_position = None
        session.widget.mute_button.show()
        self.sessions.append(session)
        if session.audio_stream is not None:
            self.audio_conference.add(session.audio_stream)
        session.unhold()

    def remove_session(self, session):
        session.widget.conference_position = None
        session.widget.mute_button.hide()
        self.sessions.remove(session)
        session_count = len(self.sessions)
        if session_count == 1:
            self.sessions[0].widget.conference_position = None
            self.sessions[0].widget.mute_button.hide()
        elif session_count > 1:
            self.sessions[0].widget.conference_position = Top
            self.sessions[-1].widget.conference_position = Bottom
            for sessions in self.sessions[1:-1]:
                session.widget.conference_position = Middle
        if not session.active:
            session.hold()
        if session.audio_stream is not None:
            self.audio_conference.remove(session.audio_stream)

    def hold(self):
        self.audio_conference.hold()

    def unhold(self):
        self.audio_conference.unhold()


# Positions for sessions in conferences.
#
class Top(object): pass
class Middle(object): pass
class Bottom(object): pass


ui_class, base_class = uic.loadUiType(Resources.get('session.ui'))

class SessionWidget(base_class, ui_class):
    def __init__(self, session, parent=None):
        super(SessionWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        # add a left margin for the colored band
        self.address_layout.setContentsMargins(8, -1, -1, -1)
        self.stream_layout.setContentsMargins(8, -1, -1, -1)
        self.bottom_layout.setContentsMargins(8, -1, -1, -1)
        font = self.latency_label.font()
        font.setPointSizeF(self.status_label.fontInfo().pointSizeF() - 1)
        self.latency_label.setFont(font)
        font = self.packet_loss_label.font()
        font.setPointSizeF(self.status_label.fontInfo().pointSizeF() - 1)
        self.packet_loss_label.setFont(font)
        self.mute_button.type = LeftSegment
        self.hold_button.type = MiddleSegment
        self.record_button.type = MiddleSegment
        self.hangup_button.type = RightSegment
        self.selected = False
        self.drop_indicator = False
        self.conference_position = None
        self._disable_dnd = False
        self.mute_button.hidden.connect(self._mute_button_hidden)
        self.mute_button.shown.connect(self._mute_button_shown)
        self.mute_button.pressed.connect(self._tool_button_pressed)
        self.hold_button.pressed.connect(self._tool_button_pressed)
        self.record_button.pressed.connect(self._tool_button_pressed)
        self.hangup_button.pressed.connect(self._tool_button_pressed)
        self.mute_button.hide()
        self.mute_button.setEnabled(False)
        self.hold_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.address_label.setText(session.remote_party_name)
        self.stream_info_label.session_type = session.type
        self.stream_info_label.codec_info = session.codec_info
        self.duration_label.value = session.duration
        self.latency_label.value = session.latency
        self.packet_loss_label.threshold = 0
        self.packet_loss_label.value = session.packet_loss
        self.status_label.value = session.status
        self.tls_label.setVisible(bool(session.tls))
        self.srtp_label.setVisible(bool(session.srtp))

    def _get_selected(self):
        return self.__dict__['selected']

    def _set_selected(self, value):
        if self.__dict__.get('selected', None) == value:
            return
        self.__dict__['selected'] = value
        self.update()

    selected = property(_get_selected, _set_selected)
    del _get_selected, _set_selected

    def _get_drop_indicator(self):
        return self.__dict__['drop_indicator']

    def _set_drop_indicator(self, value):
        if self.__dict__.get('drop_indicator', None) == value:
            return
        self.__dict__['drop_indicator'] = value
        self.update()

    drop_indicator = property(_get_drop_indicator, _set_drop_indicator)
    del _get_drop_indicator, _set_drop_indicator

    def _get_conference_position(self):
        return self.__dict__['conference_position']

    def _set_conference_position(self, value):
        if self.__dict__.get('conference_position', Null) == value:
            return
        self.__dict__['conference_position'] = value
        self.update()

    conference_position = property(_get_conference_position, _set_conference_position)
    del _get_conference_position, _set_conference_position

    def _mute_button_hidden(self):
        self.hold_button.type = LeftSegment

    def _mute_button_shown(self):
        self.hold_button.type = MiddleSegment

    def _tool_button_pressed(self):
        self._disable_dnd = True

    def mousePressEvent(self, event):
        self._disable_dnd = False
        super(SessionWidget, self).mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._disable_dnd:
            return
        super(SessionWidget, self).mouseMoveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = self.rect()

        # draw inner rect and border
        #
        if self.selected:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#75c0ff'))
            background.setColorAt(0.99, QColor('#75c0ff'))
            background.setColorAt(1.00, QColor('#ffffff'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#606060' if self.conference_position is None else '#b0b0b0')), 2.0))
        elif self.conference_position is not None:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#95ff95'))
            background.setColorAt(0.99, QColor('#95ff95'))
            background.setColorAt(1.00, QColor('#ffffff'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#b0b0b0')), 2.0))
        else:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#d0d0d0'))
            background.setColorAt(0.99, QColor('#d0d0d0'))
            background.setColorAt(1.00, QColor('#ffffff'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#b0b0b0')), 2.0))
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)

        # for conferences extend the left marker over the whole conference
        #
        if self.conference_position is not None:
            painter.setPen(Qt.NoPen)
            left_rect = rect.adjusted(0, 0, 10-rect.width(), 0)
            if self.conference_position is Top:
                painter.drawRect(left_rect.adjusted(2, 5, 0, 5))
            elif self.conference_position is Middle:
                painter.drawRect(left_rect.adjusted(2, -5, 0, 5))
            elif self.conference_position is Bottom:
                painter.drawRect(left_rect.adjusted(2, -5, 0, -5))

        # draw outer border
        #
        if self.selected or self.drop_indicator:
            painter.setBrush(Qt.NoBrush)
            if self.drop_indicator:
                painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            elif self.selected:
                painter.setPen(QPen(QBrush(QColor('#3075c0')), 2.0)) # or #2070c0 (next best look) or gray: #606060

            if self.conference_position is Top:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, 5), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, 5), 3, 3)
            elif self.conference_position is Middle:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, 5), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, -5, -1, 5), 3, 3)
            elif self.conference_position is Bottom:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, -2), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, -5, -1, -1), 3, 3)
            else:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
        elif self.conference_position is not None:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#309030')), 2.0)) # or 237523, #2b8f2b
            if self.conference_position is Top:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, 5), 3, 3)
            elif self.conference_position is Middle:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, 5), 3, 3)
            elif self.conference_position is Bottom:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, -2), 3, 3)
            else:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)

        painter.end()
        super(SessionWidget, self).paintEvent(event)


class DraggedSessionWidget(base_class, ui_class):
    """Used to draw a dragged session item"""
    def __init__(self, session_widget, parent=None):
        super(DraggedSessionWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        # add a left margin for the colored band
        self.address_layout.setContentsMargins(8, -1, -1, -1)
        self.stream_layout.setContentsMargins(8, -1, -1, -1)
        self.bottom_layout.setContentsMargins(8, -1, -1, -1)
        self.mute_button.hide()
        self.hold_button.hide()
        self.record_button.hide()
        self.hangup_button.hide()
        self.tls_label.hide()
        self.srtp_label.hide()
        self.latency_label.hide()
        self.packet_loss_label.hide()
        self.duration_label.hide()
        self.stream_info_label.setText(u'')
        self.address_label.setText(session_widget.address_label.text())
        self.selected = session_widget.selected
        self.in_conference = session_widget.conference_position is not None
        if self.in_conference:
            self.status_label.setText(u'Drop outside the conference to detach')
        else:
            self.status_label.setText(u'Drop over a session to conference them')
        self.status_label.show()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.in_conference:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#95ff95'))
            background.setColorAt(0.99, QColor('#95ff95'))
            background.setColorAt(1.00, QColor('#f8f8f8'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#309030')), 2.0))
        elif self.selected:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#75c0ff'))
            background.setColorAt(0.99, QColor('#75c0ff'))
            background.setColorAt(1.00, QColor('#f8f8f8'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#3075c0')), 2.0))
        else:
            background = QLinearGradient(0, 0, 10, 0)
            background.setColorAt(0.00, QColor('#d0d0d0'))
            background.setColorAt(0.99, QColor('#d0d0d0'))
            background.setColorAt(1.00, QColor('#f8f8f8'))
            painter.setBrush(QBrush(background))
            painter.setPen(QPen(QBrush(QColor('#808080')), 2.0))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 3, 3)
        painter.end()
        super(DraggedSessionWidget, self).paintEvent(event)

del ui_class, base_class


class SessionDelegate(QStyledItemDelegate):
    size_hint = QSize(200, 62)

    def __init__(self, parent=None):
        super(SessionDelegate, self).__init__(parent)

    def createEditor(self, parent, options, index):
        session = index.model().data(index, Qt.DisplayRole)
        session.widget = SessionWidget(session, parent)
        session.widget.hold_button.clicked.connect(partial(self._SH_HoldButtonClicked, session))
        return session.widget

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def paint(self, painter, option, index):
        session = index.model().data(index, Qt.DisplayRole)
        if session.widget.size() != option.rect.size():
            # For some reason updateEditorGeometry only receives the peak value
            # of the size that the widget ever had, so it will never shrink it.
            session.widget.resize(option.rect.size())

    def sizeHint(self, option, index):
        return self.size_hint

    def _SH_HoldButtonClicked(self, session, checked):
        if session.conference is None and not session.active and not checked:
            session_list = self.parent()
            model = session_list.model()
            selection_model = session_list.selectionModel()
            selection_model.select(model.index(model.sessions.index(session)), selection_model.ClearAndSelect)


class SessionModel(QAbstractListModel):
    sessionAdded = pyqtSignal(SessionItem)
    sessionRemoved = pyqtSignal(SessionItem)
    structureChanged = pyqtSignal()

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-session-list', 'application/x-blink-contact-list']

    def __init__(self, parent=None):
        super(SessionModel, self).__init__(parent)
        self.sessions = []
        self.main_window = parent
        self.session_list = parent.session_list
        self.ignore_selection_changes = False

    @property
    def active_sessions(self):
        return [session for session in self.sessions if not session.pending_removal]

    def flags(self, index):
        if index.isValid():
            return QAbstractListModel.flags(self, index) | Qt.ItemIsDropEnabled | Qt.ItemIsDragEnabled | Qt.ItemIsEditable
        else:
            return QAbstractListModel.flags(self, index)

    def rowCount(self, parent=QModelIndex()):
        return len(self.sessions)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        return self.sessions[index.row()]

    def supportedDropActions(self):
        return Qt.CopyAction | Qt.MoveAction

    def mimeTypes(self):
        return QStringList(['application/x-blink-session-list'])

    def mimeData(self, indexes):
        mime_data = QMimeData()
        sessions = [self.sessions[index.row()] for index in indexes if index.isValid()]
        if sessions:
            mime_data.setData('application/x-blink-session-list', QByteArray(pickle.dumps(sessions)))
        return mime_data

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # this is here just to keep the default Qt DnD API happy
        # the custom handler is in handleDroppedData
        return False

    def handleDroppedData(self, mime_data, action, index):
        if action == Qt.IgnoreAction:
            return True

        for mime_type in self.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                return handler(mime_data, action, index)
        else:
            return False

    def _DH_ApplicationXBlinkSessionList(self, mime_data, action, index):
        session_list = self.session_list
        selection_model = session_list.selectionModel()
        selection_mode = session_list.selectionMode()
        session_list.setSelectionMode(session_list.NoSelection)
        self.ignore_selection_changes = True
        source = session_list.dragged_session
        target = self.sessions[index.row()] if index.isValid() else None
        if source.conference is None:
            # the dragged session is not in a conference yet
            source_selected = source.widget.selected
            target_selected = target.widget.selected
            if target.conference is not None:
                self._remove_session(source)
                position = self.sessions.index(target.conference.sessions[-1]) + 1
                self.beginInsertRows(QModelIndex(), position, position)
                self.sessions.insert(position, source)
                self.endInsertRows()
                session_list.openPersistentEditor(self.index(position))
                source.conference = target.conference
                source_index = self.index(position)
                if source_selected:
                    selection_model.select(source_index, selection_model.Select)
                elif target_selected:
                    source.widget.selected = True
                session_list.scrollTo(source_index, session_list.EnsureVisible) # or PositionAtBottom
            else:
                source_row = self.sessions.index(source)
                target_row = index.row()
                first, last = (source, target) if source_row < target_row else (target, source)
                self._remove_session(source)
                self._remove_session(target)
                self.beginInsertRows(QModelIndex(), 0, 1)
                self.sessions[0:0] = [first, last]
                self.endInsertRows()
                session_list.openPersistentEditor(self.index(0))
                session_list.openPersistentEditor(self.index(1))
                conference = Conference()
                first.conference = conference
                last.conference = conference
                if source_selected:
                    selection_model.select(self.index(self.sessions.index(source)), selection_model.Select)
                    conference.unhold()
                elif target_selected:
                    selection_model.select(self.index(self.sessions.index(target)), selection_model.Select)
                    conference.unhold()
                session_list.scrollToTop()
            active = source.active or target.active
            for session in source.conference.sessions:
                session.active = active
        else:
            # the dragged session is in a conference
            conference = source.conference
            if len(conference.sessions) == 2:
                conference_selected = source.widget.selected
                first, last = conference.sessions
                sibling = first if source is last else last
                source.conference = None
                sibling.conference = None
                self._remove_session(first)
                self._remove_session(last)
                self._add_session(first)
                self._add_session(last)
                if conference_selected:
                    selection_model.select(self.index(self.sessions.index(sibling)), selection_model.Select)
                session_list.scrollToBottom()
            else:
                selected_index = selection_model.selectedIndexes()[0]
                if self.sessions[selected_index.row()] is source:
                    sibling = (session for session in source.conference.sessions if session is not source).next()
                    selection_model.select(self.index(self.sessions.index(sibling)), selection_model.ClearAndSelect)
                source.conference = None
                self._remove_session(source)
                self._add_session(source)
                position = self.sessions.index(conference.sessions[0])
                session_list.scrollTo(self.index(position), session_list.PositionAtCenter)
            source.active = False
        self.ignore_selection_changes = False
        session_list.setSelectionMode(selection_mode)
        self.structureChanged.emit()
        return True

    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        if not index.isValid():
            return
        session = self.sessions[index.row()]
        contacts = pickle.loads(str(mime_data.data('application/x-blink-contact-list')))
        session_manager = SessionManager()
        for contact in contacts:
            session_manager.start_call(contact.name, contact.uri, conference_sibling=session)
        return True

    def _add_session(self, session):
        position = len(self.sessions)
        self.beginInsertRows(QModelIndex(), position, position)
        self.sessions.append(session)
        self.endInsertRows()
        self.session_list.openPersistentEditor(self.index(position))

    def _remove_session(self, session):
        position = self.sessions.index(session)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.sessions[position]
        self.endRemoveRows()

    def addSession(self, session):
        if session in self.sessions:
            return
        self._add_session(session)
        session.ended.connect(self.structureChanged.emit)
        self.sessionAdded.emit(session)
        self.structureChanged.emit()

    def addSessionAndConference(self, session, sibling):
        if session in self.sessions:
            return
        if sibling not in self.sessions:
            raise ValueError('sibling %r not in sessions list' % sibling)
        self.ignore_selection_changes = True
        session_list = self.session_list
        selection_model = session_list.selectionModel()
        selection_mode = session_list.selectionMode()
        session_list.setSelectionMode(session_list.NoSelection)
        sibling_selected = sibling.widget.selected
        if sibling.conference is not None:
            position = self.sessions.index(sibling.conference.sessions[-1]) + 1
            self.beginInsertRows(QModelIndex(), position, position)
            self.sessions.insert(position, session)
            self.endInsertRows()
            session_list.openPersistentEditor(self.index(position))
            session.conference = sibling.conference
            if sibling_selected:
                session.widget.selected = True
            session_list.scrollTo(self.index(position), session_list.EnsureVisible) # or PositionAtBottom
        else:
            self._remove_session(sibling)
            self.beginInsertRows(QModelIndex(), 0, 1)
            self.sessions[0:0] = [sibling, session]
            self.endInsertRows()
            session_list.openPersistentEditor(self.index(0))
            session_list.openPersistentEditor(self.index(1))
            conference = Conference()
            sibling.conference = conference
            session.conference = conference
            if sibling_selected:
                selection_model.select(self.index(self.sessions.index(sibling)), selection_model.Select)
                conference.unhold()
            session_list.scrollToTop()
        session.active = sibling.active
        session_list.setSelectionMode(selection_mode)
        self.ignore_selection_changes = False
        session.ended.connect(self.structureChanged.emit)
        self.sessionAdded.emit(session)
        self.structureChanged.emit()

    def removeSession(self, session):
        if session not in self.sessions:
            return
        self._remove_session(session)
        if session.conference is not None:
            if len(session.conference.sessions) == 2:
                first, last = session.conference.sessions
                first.conference = None
                last.conference = None
            else:
                session.conference = None
        self.sessionRemoved.emit(session)
        self.structureChanged.emit()

    def conferenceSessions(self, sessions):
        self.ignore_selection_changes = True
        session_list = self.session_list
        selection_model = session_list.selectionModel()
        selection_mode = session_list.selectionMode()
        session_list.setSelectionMode(session_list.NoSelection)
        selected = any(session.widget.selected for session in sessions)
        selected_session = self.data(selection_model.selectedIndexes()[0]) if selected else None
        for session in sessions:
            self._remove_session(session)
        self.beginInsertRows(QModelIndex(), 0, len(sessions)-1)
        self.sessions[0:0] = sessions
        self.endInsertRows()
        for row in xrange(len(sessions)):
            session_list.openPersistentEditor(self.index(row))
        conference = Conference()
        for session in sessions:
            session.conference = conference
            session.active = selected
        if selected_session is not None:
            selection_model.select(self.index(self.sessions.index(selected_session)), selection_model.Select)
            conference.unhold()
        session_list.scrollToTop()
        session_list.setSelectionMode(selection_mode)
        self.ignore_selection_changes = False
        self.structureChanged.emit()

    def breakConference(self, conference):
        self.ignore_selection_changes = True
        sessions = [session for session in self.sessions if session.conference is conference]
        session_list = self.session_list
        selection_model = session_list.selectionModel()
        selection_mode = session_list.selectionMode()
        session_list.setSelectionMode(session_list.NoSelection)
        active_session = sessions[0]
        for session in sessions:
            session.conference = None
            self._remove_session(session)
            self._add_session(session)
            session.active = session is active_session
        selection_model.select(self.index(self.sessions.index(active_session)), selection_model.Select)
        self.ignore_selection_changes = False
        session_list.scrollToBottom()
        session_list.setSelectionMode(selection_mode)
        self.structureChanged.emit()


class ContextMenuActions(object):
    pass


class SessionListView(QListView):
    def __init__(self, parent=None):
        super(SessionListView, self).__init__(parent)
        self.setItemDelegate(SessionDelegate(self))
        self.setDropIndicatorShown(False)
        self.actions = ContextMenuActions()
        self.dragged_session = None
        self._pressed_position = None
        self._pressed_index = None

    def setModel(self, model):
        selection_model = self.selectionModel() or Null
        selection_model.selectionChanged.disconnect(self._selection_changed)
        super(SessionListView, self).setModel(model)
        self.selectionModel().selectionChanged.connect(self._selection_changed)

    def _selection_changed(self, selected, deselected):
        model = self.model()
        for session in (model.data(index) for index in deselected.indexes()):
            if session.conference is not None:
                for sibling in session.conference.sessions:
                    sibling.widget.selected = False
            else:
                session.widget.selected = False
        for session in (model.data(index) for index in selected.indexes()):
            if session.conference is not None:
                for sibling in session.conference.sessions:
                    sibling.widget.selected = True
            else:
                session.widget.selected = True
        if not selected.isEmpty():
            self.setCurrentIndex(selected.indexes()[0])
        else:
            self.setCurrentIndex(model.index(-1))

    def contextMenuEvent(self, event):
        pass

    def keyPressEvent(self, event):
        digit = chr(event.key()) if event.key() < 256 else None
        selection_model = self.selectionModel()
        selected_indexes = selection_model.selectedIndexes()
        if digit is not None and digit in '0123456789ABCD#*' and selected_indexes:
            self.model().data(selected_indexes[0]).send_dtmf(digit)
        elif event.key() in (Qt.Key_Up, Qt.Key_Down):
            current_index = selection_model.currentIndex()
            if current_index.isValid():
                step = 1 if event.key() == Qt.Key_Down else -1
                conference = current_index.data().toPyObject().conference
                new_index = current_index.sibling(current_index.row()+step, current_index.column())
                while conference is not None and new_index.isValid() and new_index.data().toPyObject().conference is conference:
                    new_index = new_index.sibling(new_index.row()+step, new_index.column())
                if new_index.isValid():
                    selection_model.select(new_index, selection_model.ClearAndSelect)
        else:
            super(SessionListView, self).keyPressEvent(event)

    def mousePressEvent(self, event):
        self._pressed_position = event.pos()
        self._pressed_index = self.indexAt(self._pressed_position)
        super(SessionListView, self).mousePressEvent(event)
        selection_model = self.selectionModel()
        selected_indexes = selection_model.selectedIndexes()
        if selected_indexes:
            selection_model.setCurrentIndex(selected_indexes[0], selection_model.Select)
        else:
            selection_model.setCurrentIndex(self.model().index(-1), selection_model.Select)

    def mouseReleaseEvent(self, event):
        self._pressed_position = None
        self._pressed_index = None
        super(SessionListView, self).mouseReleaseEvent(event)

    def selectionCommand(self, index, event=None):
        selection_model = self.selectionModel()
        if self.selectionMode() == self.NoSelection:
            return selection_model.NoUpdate
        elif not index.isValid() or event is None:
            return selection_model.NoUpdate
        elif event.type() == QEvent.MouseButtonPress and not selection_model.selectedIndexes():
            return selection_model.ClearAndSelect
        elif event.type() in (QEvent.MouseButtonPress, QEvent.MouseMove):
            return selection_model.NoUpdate
        elif event.type() == QEvent.MouseButtonRelease:
            return selection_model.ClearAndSelect
        else:
            return super(SessionListView, self).selectionCommand(index, event)

    def startDrag(self, supported_actions):
        if self._pressed_index is not None and self._pressed_index.isValid():
            model = self.model()
            self.dragged_session = model.data(self._pressed_index)
            rect = self.visualRect(self._pressed_index)
            rect.adjust(1, 1, -1, -1)
            pixmap = QPixmap(rect.size())
            pixmap.fill(Qt.transparent)
            widget = DraggedSessionWidget(self.dragged_session.widget, None)
            widget.resize(rect.size())
            widget.render(pixmap)
            drag = QDrag(self)
            drag.setPixmap(pixmap)
            drag.setMimeData(model.mimeData([self._pressed_index]))
            drag.setHotSpot(self._pressed_position - rect.topLeft())
            drag.exec_(supported_actions, Qt.CopyAction)
            self.dragged_session = None
            self._pressed_position = None
            self._pressed_index = None

    def dragEnterEvent(self, event):
        event_source = event.source()
        accepted_mime_types = set(self.model().accepted_mime_types)
        provided_mime_types = set(str(x) for x in event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if not acceptable_mime_types:
            event.ignore() # no acceptable mime types found
        elif event_source is not self and 'application/x-blink-session-list' in provided_mime_types:
            event.ignore() # we don't handle drops for blink sessions from other sources
        else:
            if event_source is self:
                event.setDropAction(Qt.MoveAction)
            event.accept()
            self.setState(self.DraggingState)

    def dragLeaveEvent(self, event):
        super(SessionListView, self).dragLeaveEvent(event)
        for session in self.model().sessions:
            session.widget.drop_indicator = False

    def dragMoveEvent(self, event):
        super(SessionListView, self).dragMoveEvent(event)
        if event.source() is self:
            event.setDropAction(Qt.MoveAction)

        model = self.model()

        for session in model.sessions:
            session.widget.drop_indicator = False

        for mime_type in model.accepted_mime_types:
            if event.provides(mime_type):
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                session = self.model().data(index)
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                handler(event, index, rect, session)
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if event.source() is self:
            event.setDropAction(Qt.MoveAction)
        for session in self.model().sessions:
            session.widget.drop_indicator = False
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        super(SessionListView, self).dropEvent(event)

    def _DH_ApplicationXBlinkSessionList(self, event, index, rect, session):
        dragged_session = self.dragged_session
        if not index.isValid():
            model = self.model()
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
            if dragged_session.conference is not None:
                event.accept(rect)
            else:
                event.ignore(rect)
        else:
            conference = dragged_session.conference or Null
            if dragged_session is session or session in conference.sessions:
                event.ignore(rect)
            else:
                if dragged_session.conference is None:
                    if session.conference is not None:
                        for sibling in session.conference.sessions:
                            sibling.widget.drop_indicator = True
                    else:
                        session.widget.drop_indicator = True
                event.accept(rect)

    def _DH_ApplicationXBlinkContactList(self, event, index, rect, session):
        model = self.model()
        if not index.isValid():
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
            event.ignore(rect)
        else:
            event.accept(rect)
            if session.conference is not None:
                for sibling in session.conference.sessions:
                    sibling.widget.drop_indicator = True
            else:
                session.widget.drop_indicator = True


ui_class, base_class = uic.loadUiType(Resources.get('incoming_dialog.ui'))

class IncomingDialog(base_class, ui_class):
    def __init__(self, parent=None):
        super(IncomingDialog, self).__init__(parent, flags=Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)
        font = self.username_label.font()
        font.setPointSizeF(self.uri_label.fontInfo().pointSizeF() + 3)
        font.setFamily("Sans Serif")
        self.username_label.setFont(font)
        font = self.note_label.font()
        font.setPointSizeF(self.uri_label.fontInfo().pointSizeF() - 1)
        self.note_label.setFont(font)
        self.reject_mode = 'ignore'
        self.busy_button.released.connect(self._set_busy_mode)
        self.reject_button.released.connect(self._set_reject_mode)
        for stream in self.streams:
            stream.toggled.connect(self._update_accept_button)
            stream.hidden.connect(self._update_streams_layout)
            stream.shown.connect(self._update_streams_layout)
        self.desktopsharing_stream.hidden.connect(self.desktopsharing_label.hide)
        self.desktopsharing_stream.shown.connect(self.desktopsharing_label.show)
        for stream in self.streams:
            stream.hide()
        self.position = None

    def show(self, activate=True, position=1):
        from blink import Blink
        blink = Blink()
        screen_geometry = blink.desktop().screenGeometry(self)
        available_geometry = blink.desktop().availableGeometry(self)
        main_window_geometry = blink.main_window.geometry()
        main_window_framegeometry = blink.main_window.frameGeometry()

        horizontal_decorations = main_window_framegeometry.width() - main_window_geometry.width()
        vertical_decorations = main_window_framegeometry.height() - main_window_geometry.height()
        width = limit(self.sizeHint().width(), min=self.minimumSize().width(), max=min(self.maximumSize().width(), available_geometry.width()-horizontal_decorations))
        height = limit(self.sizeHint().height(), min=self.minimumSize().height(), max=min(self.maximumSize().height(), available_geometry.height()-vertical_decorations))
        total_width = width + horizontal_decorations
        total_height = height + vertical_decorations
        x = limit(screen_geometry.center().x() - total_width/2, min=available_geometry.left(), max=available_geometry.right()-total_width)
        if position is None:
            y = -1
        elif position % 2 == 0:
            y = screen_geometry.center().y() + (position-1)*total_height/2
        else:
            y = screen_geometry.center().y() - position*total_height/2
        if available_geometry.top() <= y <= available_geometry.bottom() - total_height:
            self.setGeometry(x, y, width, height)
        else:
            self.resize(width, height)

        self.position = position
        self.setAttribute(Qt.WA_ShowWithoutActivating, not activate)
        super(IncomingDialog, self).show()

    @property
    def streams(self):
        return (self.audio_stream, self.chat_stream, self.desktopsharing_stream, self.video_stream)

    @property
    def accepted_streams(self):
        return [stream for stream in self.streams if stream.in_use and stream.accepted]

    def _set_busy_mode(self):
        self.reject_mode = 'busy'

    def _set_reject_mode(self):
        self.reject_mode = 'reject'

    def _update_accept_button(self):
        was_enabled = self.accept_button.isEnabled()
        self.accept_button.setEnabled(len(self.accepted_streams) > 0)
        if self.accept_button.isEnabled() != was_enabled:
            self.accept_button.setFocus()

    def _update_streams_layout(self):
        if len([stream for stream in self.streams if stream.in_use]) > 1:
            self.audio_stream.active = True
            self.chat_stream.active = True
            self.desktopsharing_stream.active = True
            self.video_stream.active = True
            self.note_label.setText(u'To refuse a stream click its icon')
        else:
            self.audio_stream.active = False
            self.chat_stream.active = False
            self.desktopsharing_stream.active = False
            self.video_stream.active = False
            if self.audio_stream.in_use:
                self.note_label.setText(u'Audio call')
            elif self.chat_stream.in_use:
                self.note_label.setText(u'Chat session')
            elif self.video_stream.in_use:
                self.note_label.setText(u'Video call')
            elif self.desktopsharing_stream.in_use:
                self.note_label.setText(u'Desktop sharing request')
            else:
                self.note_label.setText(u'')
        self._update_accept_button()

del ui_class, base_class


class IncomingSession(QObject):
    accepted = pyqtSignal()
    rejected = pyqtSignal(str)

    def __init__(self, dialog, session, proposal=False, audio_stream=None, video_stream=None, chat_stream=None, desktopsharing_stream=None):
        super(IncomingSession, self).__init__()
        self.dialog = dialog
        self.session = session
        self.proposal = proposal
        self.audio_stream = audio_stream
        self.video_stream = video_stream
        self.chat_stream = chat_stream
        self.desktopsharing_stream = desktopsharing_stream

        if proposal:
            self.dialog.setWindowTitle(u'Incoming session update')
            self.dialog.setWindowIconText(u'Incoming session update')
            self.dialog.busy_button.hide()
        else:
            self.dialog.setWindowTitle(u'Incoming session request')
            self.dialog.setWindowIconText(u'Incoming session request')
        from blink import Blink
        address = u'%s@%s' % (session.remote_identity.uri.user, session.remote_identity.uri.host)
        self.dialog.uri_label.setText(address)
        for contact in Blink().main_window.contact_model.iter_contacts():
            if session.remote_identity.uri.matches(contact.uri) or any(session.remote_identity.uri.matches(alias) for alias in contact.sip_aliases):
                self.dialog.username_label.setText(contact.name or session.remote_identity.display_name or address)
                self.dialog.user_icon.setPixmap(contact.icon)
                break
        else:
            self.dialog.username_label.setText(session.remote_identity.display_name or address)
        if self.audio_stream:
            self.dialog.audio_stream.show()
        if self.video_stream:
            self.dialog.video_stream.show()
        if self.chat_stream:
            self.dialog.chat_stream.accepted = False # Remove when implemented later -Luci
            self.dialog.chat_stream.show()
        if self.desktopsharing_stream:
            if self.desktopsharing_stream.handler.type == 'active':
                self.dialog.desktopsharing_label.setText(u'is offering to share his desktop')
            else:
                self.dialog.desktopsharing_label.setText(u'is asking to share your desktop')
            self.dialog.desktopsharing_stream.accepted = False # Remove when implemented later -Luci
            self.dialog.desktopsharing_stream.show()
        self.dialog.audio_device_label.setText(u'Selected audio device is: %s' % SIPApplication.voice_audio_bridge.mixer.real_output_device)

        self.dialog.accepted.connect(self._SH_DialogAccepted)
        self.dialog.rejected.connect(self._SH_DialogRejected)

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

    def __lt__(self, other):
        return self.priority < other.priority

    def __le__(self, other):
        return self.priority <= other.priority

    def __gt__(self, other):
        return self.priority > other.priority

    def __ge__(self, other):
        return self.priority >= other.priority

    @property
    def accepted_streams(self):
        streams = []
        if self.audio_accepted:
            streams.append(self.audio_stream)
        if self.video_accepted:
            streams.append(self.video_stream)
        if self.chat_accepted:
            streams.append(self.chat_stream)
        if self.desktopsharing_accepted:
            streams.append(self.desktopsharing_stream)
        return streams

    @property
    def audio_accepted(self):
        return self.dialog.audio_stream.in_use and self.dialog.audio_stream.accepted

    @property
    def video_accepted(self):
        return self.dialog.video_stream.in_use and self.dialog.video_stream.accepted

    @property
    def chat_accepted(self):
        return self.dialog.chat_stream.in_use and self.dialog.chat_stream.accepted

    @property
    def desktopsharing_accepted(self):
        return self.dialog.desktopsharing_stream.in_use and self.dialog.desktopsharing_stream.accepted

    @property
    def priority(self):
        if self.audio_stream:
            return 0
        elif self.video_stream:
            return 1
        elif self.desktopsharing_stream:
            return 2
        elif self.chat_stream:
            return 3
        else:
            return 4

    @property
    def ringtone(self):
        if 'ringtone' not in self.__dict__:
            if self.audio_stream or self.video_stream or self.desktopsharing_stream:
                sound_file = self.session.account.sounds.inbound_ringtone
                if sound_file is not None and sound_file.path is DefaultPath:
                    settings = SIPSimpleSettings()
                    sound_file = settings.sounds.inbound_ringtone
                ringtone = WavePlayer(SIPApplication.alert_audio_mixer, sound_file.path, volume=sound_file.volume, loop_count=0, pause_time=6) if sound_file is not None else Null
                ringtone.bridge = SIPApplication.alert_audio_bridge
            else:
                ringtone = WavePlayer(SIPApplication.alert_audio_mixer, Resources.get('sounds/beeping_ringtone.wav'), volume=70, loop_count=0, pause_time=6)
                ringtone.bridge = SIPApplication.alert_audio_bridge
            self.__dict__['ringtone'] = ringtone
        return self.__dict__['ringtone']

    def _SH_DialogAccepted(self):
        self.accepted.emit()

    def _SH_DialogRejected(self):
        self.rejected.emit(self.dialog.reject_mode)


class SessionManager(object):
    __metaclass__ = Singleton

    implements(IObserver)

    def __init__(self):
        self.main_window = None
        self.session_model = None
        self.incoming_sessions = []
        self.dialog_positions = range(1, 100)
        self.current_ringtone = Null

    def initialize(self, main_window, session_model):
        self.main_window = main_window
        self.session_model = session_model
        session_model.structureChanged.connect(self.update_ringtone)
        session_model.session_list.selectionModel().selectionChanged.connect(self._SH_SessionListSelectionChanged)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPSessionNewIncoming')
        notification_center.add_observer(self, name='SIPSessionGotProposal')
        notification_center.add_observer(self, name='SIPSessionDidFail')
        notification_center.add_observer(self, name='SIPSessionGotRejectProposal')
        notification_center.add_observer(self, name='SIPSessionDidRenegotiateStreams')

    def start_call(self, name, address, account=None, conference_sibling=None, audio=True, video=False):
        account_manager = AccountManager()
        account = account or account_manager.default_account
        if account is None or not account.enabled:
            return
        try:
            remote_uri = self.create_uri(account, address)
        except Exception, e:
            print 'Invalid URI: %s' % e # Replace with pop-up
        else:
            session = Session(account)
            audio_stream = self.create_stream(account, 'audio') if audio else None
            video_stream = self.create_stream(account, 'vidoe') if video else None
            session_item = SessionItem(name, remote_uri, session, audio_stream=audio_stream, video_stream=video_stream)
            session_item.activated.connect(partial(self._SH_SessionActivated, session_item))
            session_item.deactivated.connect(partial(self._SH_SessionDeactivated, session_item))
            session_item.ended.connect(partial(self._SH_SessionEnded, session_item))
            if conference_sibling is not None:
                self.session_model.addSessionAndConference(session_item, conference_sibling)
            else:
                self.session_model.addSession(session_item)
                selection_model = self.session_model.session_list.selectionModel()
                selection_model.select(self.session_model.index(self.session_model.rowCount()-1), selection_model.ClearAndSelect)
            self.main_window.switch_view_button.view = SwitchViewButton.SessionView
            self.session_model.session_list.setFocus()
            self.main_window.search_box.update()
            session_item.connect()

    def update_ringtone(self):
        if not self.incoming_sessions:
            self.current_ringtone = Null
        elif self.session_model.active_sessions:
            self.current_ringtone = self.beeping_ringtone
        else:
            self.current_ringtone = self.incoming_sessions[0].ringtone

    @property
    def beeping_ringtone(self):
        if 'beeping_ringtone' not in self.__dict__:
            ringtone = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get('sounds/beeping_ringtone.wav'), volume=70, loop_count=0, pause_time=6)
            ringtone.bridge = SIPApplication.voice_audio_bridge
            self.__dict__['beeping_ringtone'] = ringtone
        return self.__dict__['beeping_ringtone']

    def _get_current_ringtone(self):
        return self.__dict__['current_ringtone']

    def _set_current_ringtone(self, ringtone):
        old_ringtone = self.__dict__.get('current_ringtone', Null)
        if ringtone is not Null and ringtone is old_ringtone:
            return
        old_ringtone.stop()
        old_ringtone.bridge.remove(old_ringtone)
        ringtone.bridge.add(ringtone)
        ringtone.start()
        self.__dict__['current_ringtone'] = ringtone

    current_ringtone = property(_get_current_ringtone, _set_current_ringtone)
    del _get_current_ringtone, _set_current_ringtone

    @staticmethod
    def create_stream(account, type):
        for cls in MediaStreamRegistry():
            if cls.type == type:
                return cls(account)
        else:
            raise ValueError('unknown stream type: %s' % type)

    @staticmethod
    def create_uri(account, address):
        if not address.startswith('sip:') and not address.startswith('sips:'):
            address = 'sip:' + address
        if '@' not in address:
            if isinstance(account, Account):
                address = address + '@' + account.id.domain
            else:
                raise ValueError('SIP address without domain')
        return SIPURI.parse(str(address))

    def _remove_session(self, session):
        session_list = self.session_model.session_list
        selection_mode = session_list.selectionMode()
        session_list.setSelectionMode(session_list.NoSelection)
        if session.conference is not None:
            sibling = (s for s in session.conference.sessions if s is not session).next()
            session_index = self.session_model.index(self.session_model.sessions.index(session))
            sibling_index = self.session_model.index(self.session_model.sessions.index(sibling))
            selection_model = session_list.selectionModel()
            if selection_model.isSelected(session_index):
                selection_model.select(sibling_index, selection_model.ClearAndSelect)
        self.session_model.removeSession(session)
        session_list.setSelectionMode(selection_mode)
        if not self.session_model.rowCount():
            self.main_window.switch_view_button.view = SwitchViewButton.ContactView

    def _SH_IncomingSessionAccepted(self, incoming_session):
        if incoming_session.dialog.position is not None:
            bisect.insort_left(self.dialog_positions, incoming_session.dialog.position)
        self.incoming_sessions.remove(incoming_session)
        self.update_ringtone()
        session = incoming_session.session
        if incoming_session.audio_accepted and incoming_session.video_accepted:
            session_item = SessionItem(session.remote_identity.display_name, session.remote_identity.uri, session, audio_stream=incoming_session.audio_stream, video_stream=incoming_session.video_stream)
        elif incoming_session.audio_accepted:
            try:
                session_item = (session_item for session_item in self.session_model.active_sessions if session_item.session is session and session_item.audio_stream is None).next()
                session_item.audio_stream = incoming_session.audio_stream
            except StopIteration:
                session_item = SessionItem(session.remote_identity.display_name, session.remote_identity.uri, session, audio_stream=incoming_session.audio_stream)
        elif incoming_session.video_accepted:
            try:
                session_item = (session_item for session_item in self.session_model.active_sessions if session_item.session is session and session_item.video_stream is None).next()
                session_item.video_stream = incoming_session.video_stream
            except StopIteration:
                session_item = SessionItem(session.remote_identity.display_name, session.remote_identity.uri, session, video_stream=incoming_session.video_stream)
        else: # Handle other streams -Luci
            if incoming_session.proposal:
                session.reject_proposal(488)
            else:
                session.reject(488)
            return
        session_item.activated.connect(partial(self._SH_SessionActivated, session_item))
        session_item.deactivated.connect(partial(self._SH_SessionDeactivated, session_item))
        session_item.ended.connect(partial(self._SH_SessionEnded, session_item))
        selection_model = self.session_model.session_list.selectionModel()
        if session_item in self.session_model.sessions:
            selection_model.select(self.session_model.index(self.session_model.sessions.index(session_item)), selection_model.ClearAndSelect)
        else:
            self.session_model.addSession(session_item)
            selection_model.select(self.session_model.index(self.session_model.rowCount()-1), selection_model.ClearAndSelect)
        self.main_window.switch_view_button.view = SwitchViewButton.SessionView
        self.session_model.session_list.setFocus()
        # Remove when implemented later -Luci
        accepted_streams = incoming_session.accepted_streams
        if incoming_session.chat_stream in accepted_streams:
            accepted_streams.remove(incoming_session.chat_stream)
        if incoming_session.desktopsharing_stream in accepted_streams:
            accepted_streams.remove(incoming_session.desktopsharing_stream)
        if incoming_session.proposal:
            session.accept_proposal(accepted_streams)
        else:
            session.accept(accepted_streams)
        self.main_window.activateWindow()
        self.main_window.raise_()

    def _SH_IncomingSessionRejected(self, incoming_session, mode):
        if incoming_session.dialog.position is not None:
            bisect.insort_left(self.dialog_positions, incoming_session.dialog.position)
        self.incoming_sessions.remove(incoming_session)
        self.update_ringtone()
        if incoming_session.proposal:
            incoming_session.session.reject_proposal(488)
        elif mode == 'busy':
            incoming_session.session.reject(486)
        elif mode == 'reject':
            incoming_session.session.reject(603)

    def _SH_SessionActivated(self, session):
        item = session.conference if session.conference is not None else session
        item.unhold()

    def _SH_SessionDeactivated(self, session):
        item = session.conference if session.conference is not None else session
        item.hold()

    def _SH_SessionEnded(self, session):
        call_later(5, self._remove_session, session)

    def _SH_SessionListSelectionChanged(self, selected, deselected):
        if self.session_model.ignore_selection_changes:
            return
        selected_indexes = selected.indexes()
        deselected_indexes = deselected.indexes()
        old_active_session = self.session_model.data(deselected_indexes[0]) if deselected_indexes else Null
        new_active_session = self.session_model.data(selected_indexes[0]) if selected_indexes else Null
        if old_active_session.conference and old_active_session.conference is not new_active_session.conference:
            for session in old_active_session.conference.sessions:
                session.active = False
        elif old_active_session.conference is None:
            old_active_session.active = False
        if new_active_session.conference and new_active_session.conference is not old_active_session.conference:
            for session in new_active_session.conference.sessions:
                session.active = True
        elif new_active_session.conference is None:
            new_active_session.active = True

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionNewIncoming(self, notification):
        session = notification.sender
        audio_streams = [stream for stream in notification.data.streams if stream.type=='audio']
        video_streams = [stream for stream in notification.data.streams if stream.type=='video']
        chat_streams = [stream for stream in notification.data.streams if stream.type=='chat']
        desktopsharing_streams = [stream for stream in notification.data.streams if stream.type=='desktop-sharing']
        filetransfer_streams = [stream for stream in notification.data.streams if stream.type=='file-transfer']
        if not audio_streams and not video_streams and not chat_streams and not desktopsharing_streams and not filetransfer_streams:
            session.reject(488)
            return
        if filetransfer_streams and (audio_streams or video_streams or chat_streams or desktopsharing_streams):
            session.reject(488)
            return
        session.send_ring_indication()
        if filetransfer_streams:
            filetransfer_stream = filetransfer_streams[0]
        else:
            audio_stream = audio_streams[0] if audio_streams else None
            video_stream = video_streams[0] if video_streams else None
            chat_stream = chat_streams[0] if chat_streams else None
            desktopsharing_stream = desktopsharing_streams[0] if desktopsharing_streams else None
            dialog = IncomingDialog()
            incoming_session = IncomingSession(dialog, session, proposal=False, audio_stream=audio_stream, video_stream=video_stream, chat_stream=chat_stream, desktopsharing_stream=desktopsharing_stream)
            bisect.insort_right(self.incoming_sessions, incoming_session)
            incoming_session.accepted.connect(partial(self._SH_IncomingSessionAccepted, incoming_session))
            incoming_session.rejected.connect(partial(self._SH_IncomingSessionRejected, incoming_session))
            from blink import Blink
            try:
                position = self.dialog_positions.pop(0)
            except IndexError:
                position = None
            incoming_session.dialog.show(activate=Blink().activeWindow() is not None and self.incoming_sessions.index(incoming_session)==0, position=position)
            self.update_ringtone()

    def _NH_SIPSessionGotProposal(self, notification):
        session = notification.sender
        audio_streams = [stream for stream in notification.data.streams if stream.type=='audio']
        video_streams = [stream for stream in notification.data.streams if stream.type=='video']
        chat_streams = [stream for stream in notification.data.streams if stream.type=='chat']
        desktopsharing_streams = [stream for stream in notification.data.streams if stream.type=='desktop-sharing']
        filetransfer_streams = [stream for stream in notification.data.streams if stream.type=='file-transfer']
        if not audio_streams and not video_streams and not chat_streams and not desktopsharing_streams and not filetransfer_streams:
            session.reject_proposal(488)
            return
        if filetransfer_streams and (audio_streams or video_streams or chat_streams or desktopsharing_streams):
            session.reject_proposal(488)
            return
        session.send_ring_indication()
        if filetransfer_streams:
            filetransfer_stream = filetransfer_streams[0]
        else:
            audio_stream = audio_streams[0] if audio_streams else None
            video_stream = video_streams[0] if video_streams else None
            chat_stream = chat_streams[0] if chat_streams else None
            desktopsharing_stream = desktopsharing_streams[0] if desktopsharing_streams else None
            dialog = IncomingDialog()
            incoming_session = IncomingSession(dialog, session, proposal=True, audio_stream=audio_stream, video_stream=video_stream, chat_stream=chat_stream, desktopsharing_stream=desktopsharing_stream)
            bisect.insort_right(self.incoming_sessions, incoming_session)
            incoming_session.accepted.connect(partial(self._SH_IncomingSessionAccepted, incoming_session))
            incoming_session.rejected.connect(partial(self._SH_IncomingSessionRejected, incoming_session))
            from blink import Blink
            try:
                position = self.dialog_positions.pop(0)
            except IndexError:
                position = None
            incoming_session.dialog.show(activate=Blink().activeWindow() is not None and self.incoming_sessions.index(incoming_session)==0, position=position)
            self.update_ringtone()

    def _NH_SIPSessionDidFail(self, notification):
        if notification.data.code != 487:
            return
        try:
            incoming_session = (incoming_session for incoming_session in self.incoming_sessions if incoming_session.session is notification.sender).next()
        except StopIteration:
            pass
        else:
            if incoming_session.dialog.position is not None:
                bisect.insort_left(self.dialog_positions, incoming_session.dialog.position)
            incoming_session.dialog.hide()
            self.incoming_sessions.remove(incoming_session)
            self.update_ringtone()

    def _NH_SIPSessionGotRejectProposal(self, notification):
        if notification.data.code != 487:
            return
        try:
            incoming_session = (incoming_session for incoming_session in self.incoming_sessions if incoming_session.session is notification.sender).next()
        except StopIteration:
            pass
        else:
            if incoming_session.dialog.position is not None:
                bisect.insort_left(self.dialog_positions, incoming_session.dialog.position)
            incoming_session.dialog.hide()
            self.incoming_sessions.remove(incoming_session)
            self.update_ringtone()


