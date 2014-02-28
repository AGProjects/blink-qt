# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['ClientConference', 'ConferenceDialog', 'AudioSessionModel', 'AudioSessionListView', 'ChatSessionModel', 'ChatSessionListView', 'SessionManager']

import bisect
import cPickle as pickle
import os
import re
import string

from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import partial
from itertools import chain, izip, repeat
from operator import attrgetter

from PyQt4 import uic
from PyQt4.QtCore import Qt, QAbstractListModel, QByteArray, QEasingCurve, QEvent, QMimeData, QModelIndex, QObject, QPointF, QPropertyAnimation, QRect, QSize, QTimer, pyqtSignal
from PyQt4.QtGui  import QApplication, QBrush, QColor, QDrag, QLinearGradient, QListView, QMenu, QPainter, QPalette, QPen, QPixmap, QPolygonF, QShortcut, QStyle, QStyledItemDelegate

from application.notification import IObserver, NotificationCenter, NotificationData, ObserverWeakrefProxy
from application.python import Null, limit
from application.python.types import MarkerType, Singleton
from application.python.weakref import weakobjectmap
from zope.interface import implements

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioConference, WavePlayer
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPCoreError, SIPURI, ToHeader
from sipsimple.lookup import DNSLookup
from sipsimple.session import Session
from sipsimple.streams import MediaStreamRegistry

from blink.resources import Resources
from blink.util import call_later, run_in_gui_thread
from blink.widgets.buttons import LeftSegment, MiddleSegment, RightSegment
from blink.widgets.labels import Status
from blink.widgets.color import ColorHelperMixin
from blink.widgets.util import ContextMenuActions


class RTPStreamInfo(object):
    dataset_size = 5000
    average_interval = 10

    def __init__(self):
        self.ice_status = None
        self.encryption = None
        self.codec_name = None
        self.sample_rate = None
        self.local_address = None
        self.remote_address = None
        self.local_rtp_candidate = None
        self.remote_rtp_candidate = None
        self.latency = deque(maxlen=self.dataset_size)
        self.packet_loss = deque(maxlen=self.dataset_size)
        self.jitter = deque(maxlen=self.dataset_size)
        self.incoming_traffic = deque(maxlen=self.dataset_size)
        self.outgoing_traffic = deque(maxlen=self.dataset_size)
        self.bytes_sent = 0
        self.bytes_received = 0
        self._total_packets = 0
        self._total_packets_lost = 0
        self._total_packets_discarded = 0
        self._average_loss_queue = deque(maxlen=self.average_interval)

    @property
    def codec(self):
        return '%s %dkHz' % (self.codec_name, self.sample_rate/1000) if self.codec_name else None

    def _update(self, stream):
        if stream is not None:
            self.codec_name = stream.codec
            self.sample_rate = stream.sample_rate
            self.local_address = stream.local_rtp_address
            self.remote_address = stream.remote_rtp_address
            self.encryption = 'SRTP' if stream.srtp_active else None
            if stream.session and not stream.session.account.nat_traversal.use_ice:
                self.ice_status = 'disabled'

    def _update_statistics(self, statistics):
        if statistics:
            packets = statistics['rx']['packets'] - self._total_packets
            packets_lost = statistics['rx']['packets_lost'] - self._total_packets_lost
            packets_discarded = statistics['rx']['packets_discarded'] - self._total_packets_discarded
            self._average_loss_queue.append(100.0 * packets_lost / (packets + packets_lost - packets_discarded) if packets_lost else 0)
            self._total_packets += packets
            self._total_packets_lost += packets_lost
            self._total_packets_discarded += packets_discarded
            self.latency.append(statistics['rtt']['last'] / 1000 / 2)
            self.jitter.append(statistics['rx']['jitter']['last'] / 1000)
            self.incoming_traffic.append(float(statistics['rx']['bytes'] - self.bytes_received)) # bytes/second
            self.outgoing_traffic.append(float(statistics['tx']['bytes'] - self.bytes_sent))     # bytes/second
            self.bytes_sent = statistics['tx']['bytes']
            self.bytes_received = statistics['rx']['bytes']
            self.packet_loss.append(sum(self._average_loss_queue) / self.average_interval)

    def _reset(self):
        self.__init__()


class MSRPStreamInfo(object):
    def __init__(self):
        self.local_address = None
        self.remote_address = None
        self.transport = None
        self.full_local_path = []
        self.full_remote_path = []

    def _update(self, stream):
        if stream is not None:
            if stream.msrp:
                self.transport = stream.transport
                self.local_address = stream.msrp.local_uri.host
                self.remote_address = stream.msrp.next_host().host
                self.full_local_path = stream.msrp.full_local_path
                self.full_remote_path = stream.msrp.full_remote_path
            elif stream.session:
                self.transport = stream.transport
                self.local_address = stream.local_uri.host

    def _reset(self):
        self.__init__()


class StreamsInfo(object):
    __slots__ = 'audio', 'video', 'chat'

    def __init__(self):
        self.audio = RTPStreamInfo()
        self.video = RTPStreamInfo()
        self.chat = MSRPStreamInfo()

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def _update(self, streams):
        self.audio._update(streams.get('audio'))
        self.video._update(streams.get('video'))
        self.chat._update(streams.get('chat'))


class SessionInfo(object):
    def __init__(self):
        self.duration = timedelta(0)
        self.local_address = None
        self.remote_address = None
        self.transport = None
        self.remote_user_agent = None
        self.streams = StreamsInfo()

    def _update(self, session):
        sip_session = session.sip_session
        if sip_session is not None:
            self.transport = sip_session.transport
            self.local_address = session.account.contact[self.transport].host
            self.remote_address = sip_session.peer_address # consider reading from sip_session.route if peer_address is None (route can also be None) -Dan
            self.remote_user_agent = sip_session.remote_user_agent
        self.streams._update(session.streams)


class StreamDescription(object):
    def __init__(self, type, **kw):
        self.type = type
        self.attributes = kw

    def create_stream(self):
        registry = MediaStreamRegistry()
        cls = registry.get(self.type)
        return cls(**self.attributes)

    def __repr__(self):
        if self.attributes:
            return "%s(%r, %s)" % (self.__class__.__name__, self.type, ', '.join("%s=%r" % pair for pair in self.attributes.iteritems()))
        else:
            return "%s(%r)" % (self.__class__.__name__, self.type)


class StreamSet(object):
    def __init__(self, streams):
        self._stream_map = {stream.type: stream for stream in streams}

    def __getitem__(self, key):
        return self._stream_map[key]

    def __contains__(self, key):
        return key in self._stream_map or key in self._stream_map.values()

    def __iter__(self):
        return iter(sorted(self._stream_map.values(), key=attrgetter('type')))

    def __reversed__(self):
        return iter(sorted(self._stream_map.values(), key=attrgetter('type'), reverse=True))

    __hash__ = None

    def __len__(self):
        return len(self._stream_map)

    @property
    def types(self):
        return set(self._stream_map)

    def get(self, key, default=None):
        return self._stream_map.get(key, default)


class StreamContainer(object):
    def __init__(self, session, stream_map):
        self._session = session
        self._stream_map = stream_map

    def __getitem__(self, key):
        return self._stream_map[key]

    def __contains__(self, key):
        return key in self._stream_map or key in self._stream_map.values()

    def __iter__(self):
        return iter(sorted(self._stream_map.values(), key=attrgetter('type')))

    def __reversed__(self):
        return iter(sorted(self._stream_map.values(), key=attrgetter('type'), reverse=True))

    __hash__ = None

    def __len__(self):
        return len(self._stream_map)

    @property
    def types(self):
        return set(self._stream_map)

    def get(self, key, default=None):
        return self._stream_map.get(key, default)

    def add(self, stream):
        notification_center = NotificationCenter()
        old_stream = self._stream_map.get(stream.type, None)
        if old_stream is not None:
            notification_center.remove_observer(self._session, sender=old_stream)
        stream.blink_session = self._session
        self._stream_map[stream.type] = stream
        notification_center.add_observer(self._session, sender=stream)

    def remove(self, stream):
        # is it a good choice to silently ignore removing a stream that is not in the container? -Dan
        if stream in self:
            self._stream_map.pop(stream.type)
            notification_center = NotificationCenter()
            notification_center.remove_observer(self._session, sender=stream)

    def extend(self, iterable):
        for item in iterable:
            self.add(item)

    def clear(self):
        for stream in self._stream_map.values():
            self.remove(stream)


class defaultweakobjectmap(weakobjectmap):
    def __init__(self, factory, *args, **kw):
        self.default_factory = factory
        super(defaultweakobjectmap, self).__init__(*args, **kw)
    def __missing__(self, key):
        return self.setdefault(key.object, self.default_factory())


class StreamListDescriptor(object):
    def __init__(self):
        self.values = defaultweakobjectmap(dict)

    def __get__(self, obj, objtype):
        if obj is None:
            return self
        return StreamContainer(obj, self.values[obj])

    def __set__(self, obj, value):
        raise AttributeError("Attribute cannot be set")

    def __delete__(self, obj):
        raise AttributeError("Attribute cannot be deleted")


class BlinkSessionState(str):
    state    = property(lambda self: str(self.partition('/')[0]) or None)
    substate = property(lambda self: str(self.partition('/')[2]) or None)

    def __eq__(self, other):
        if isinstance(other, BlinkSessionState):
            return self.state == other.state and self.substate == other.substate
        elif isinstance(other, basestring):
            state    = other.partition('/')[0] or None
            substate = other.partition('/')[2] or None
            if state == '*':
                return substate in ('*', None) or self.substate == substate
            elif substate == '*':
                return self.state == state
            else:
                return self.state == state and self.substate == substate
        return NotImplemented

    def __ne__(self, other):
        return not (self == other)


class SessionItemsDescriptor(object):
    class SessionItems(object):
        def __getattr__(self, name):
            return None

    def __init__(self):
        self.values = defaultweakobjectmap(self.SessionItems)

    def __get__(self, obj, objtype):
        return self.values[obj] if obj is not None else None

    def __set__(self, obj, value):
        raise AttributeError("Attribute cannot be set")

    def __delete__(self, obj):
        raise AttributeError("Attribute cannot be deleted")


class BlinkSession(QObject):
    implements(IObserver)

    # check what should be a signal and what a notification -Dan
    clientConferenceChanged = pyqtSignal(object, object) # old_conference, new_conference

    streams = StreamListDescriptor()
    items = SessionItemsDescriptor()

    def __init__(self):
        super(BlinkSession, self).__init__()
        self._initialize()

    def _initialize(self, reinitialize=False):
        if not reinitialize:
            self.state = None

            self.account = None
            self.contact = None
            self.contact_uri = None
            self.uri = None
            self.server_conference = ServerConference(self)

            self._delete_when_done = False
            self._delete_requested = False

            self.timer = QTimer()
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self._SH_TimerFired)
        else:
            self.timer.stop()

        self.direction = None
        self.__dict__['active'] = False

        self.lookup = None
        self.client_conference = None
        self.sip_session = None
        self.stream_descriptions = None
        self.streams.clear()

        self.local_hold = False
        self.remote_hold = False
        self.recording = False

        self.info = SessionInfo()

        self._sibling = None

    def _get_state(self):
        return self.__dict__['state']

    def _set_state(self, value):
        if value is not None and not isinstance(value, BlinkSessionState):
            value = BlinkSessionState(value)
        old_state = self.__dict__.get('state', None)
        new_state = self.__dict__['state'] = value
        if new_state != old_state:
            NotificationCenter().post_notification('BlinkSessionDidChangeState', sender=self, data=NotificationData(old_state=old_state, new_state=new_state))

    state = property(_get_state, _set_state)
    del _get_state, _set_state

    def _get_contact(self):
        return self.__dict__['contact']

    def _set_contact(self, value):
        old_contact = self.__dict__.get('contact', None)
        new_contact = self.__dict__['contact'] = value
        if new_contact != old_contact:
            notification_center = NotificationCenter()
            if old_contact is not None:
                notification_center.remove_observer(self, sender=old_contact)
            if new_contact is not None:
                notification_center.add_observer(self, sender=new_contact)

    contact = property(_get_contact, _set_contact)
    del _get_contact, _set_contact

    def _get_sip_session(self):
        return self.__dict__['sip_session']

    def _set_sip_session(self, value):
        old_session = self.__dict__.get('sip_session', None)
        new_session = self.__dict__['sip_session'] = value
        if new_session != old_session:
            notification_center = NotificationCenter()
            if old_session is not None:
                notification_center.remove_observer(self, sender=old_session)
            if new_session is not None:
                notification_center.add_observer(self, sender=new_session)

    sip_session = property(_get_sip_session, _set_sip_session)
    del _get_sip_session, _set_sip_session

    def _get_active(self):
        return self.__dict__['active']

    def _set_active(self, value):
        value = bool(value)
        if self.__dict__.get('active', None) == value:
            return
        self.__dict__['active'] = value
        if self.state in ('connecting/*', 'connected/*') and self.streams.types.intersection({'audio', 'video'}):
            entity = self.client_conference or self
            if value:
                entity.unhold()
            else:
                entity.hold()

    active = property(_get_active, _set_active)
    del _get_active, _set_active

    def _get_account(self):
        account_manager = AccountManager()
        account_id = self.__dict__.get('account', None)
        if account_id is not None:
            try:
                account = account_manager.get_account(account_id)
                if account.enabled:
                    return account
            except KeyError:
                pass
        account = account_manager.default_account
        self.__dict__['account'] = account.id
        return account

    def _set_account(self, account):
        self.__dict__['account'] = account.id if account is not None else None

    account = property(_get_account, _set_account)
    del _get_account, _set_account

    def _get_client_conference(self):
        return self.__dict__['client_conference']

    def _set_client_conference(self, value):
        old_conference = self.__dict__.get('client_conference', None)
        new_conference = self.__dict__['client_conference'] = value
        if old_conference is new_conference:
            return
        if old_conference is not None:
            old_conference.remove_session(self)
        if new_conference is not None:
            new_conference.add_session(self)
            self.unhold()
        elif not self.active:
            self.hold()
        self.clientConferenceChanged.emit(old_conference, new_conference)

    client_conference = property(_get_client_conference, _set_client_conference)
    del _get_client_conference, _set_client_conference

    @property
    def persistent(self):
        return not self._delete_when_done and not self._delete_requested

    @property
    def reusable(self):
        return self.persistent and self.state in (None, 'initialized', 'ended')

    @property
    def duration(self):
        return self.info.duration

    @property
    def transport(self):
        return self.sip_session.transport if self.sip_session is not None else None

    @property
    def on_hold(self):
        return self.local_hold or self.remote_hold

    @property
    def remote_focus(self):
        return self.sip_session is not None and self.sip_session.remote_focus

    def init_incoming(self, sip_session, streams, contact, contact_uri, reinitialize=False):
        assert self.state in (None, 'initialized', 'ended')
        assert self.contact is None or contact.settings is self.contact.settings
        notification_center = NotificationCenter()
        if reinitialize:
            notification_center.post_notification('BlinkSessionWillReinitialize', sender=self)
            self._initialize(reinitialize=True)
        else:
            self._delete_when_done = len(streams)==1 and streams[0].type=='audio'
        self.direction = 'incoming'
        self.sip_session = sip_session
        self.account = sip_session.account
        self.contact = contact
        self.contact_uri = contact_uri
        self.uri = self._parse_uri(contact_uri.uri)
        self.streams.extend(streams)
        self.info._update(self)
        self.state = 'connecting'
        if reinitialize:
            notification_center.post_notification('BlinkSessionDidReinitializeForIncoming', sender=self)
        else:
            notification_center.post_notification('BlinkSessionNewIncoming', sender=self)
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media', 'statistics'}))
        notification_center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='connecting'))
        self.sip_session.accept(streams)

    def init_outgoing(self, account, contact, contact_uri, stream_descriptions, sibling=None, reinitialize=False):
        assert self.state in (None, 'initialized', 'ended')
        assert self.contact is None or contact.settings is self.contact.settings
        notification_center = NotificationCenter()
        if reinitialize:
            notification_center.post_notification('BlinkSessionWillReinitialize', sender=self)
            self._initialize(reinitialize=True)
        else:
            self._delete_when_done = len(stream_descriptions)==1 and stream_descriptions[0].type=='audio'
        self.direction = 'outgoing'
        self.account = account
        self.contact = contact
        self.contact_uri = contact_uri
        self.uri = self._normalize_uri(contact_uri.uri)
        # reevaluate later, after we add the .active/.proposed attributes to streams, if creating the sip session and the streams at this point is desirable -Dan
        # note: creating the sip session early also need the test in hold/unhold/end to change from sip_session is (not) None to sip_session.state is (not) None -Dan
        self.stream_descriptions = StreamSet(stream_descriptions)
        self._sibling = sibling
        self.state = 'initialized'
        self.info._update(self)
        if reinitialize:
            notification_center.post_notification('BlinkSessionDidReinitializeForOutgoing', sender=self)
        else:
            notification_center.post_notification('BlinkSessionNewOutgoing', sender=self)
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media', 'statistics'}))

    def connect(self):
        assert self.direction == 'outgoing' and self.state == 'initialized'
        notification_center = NotificationCenter()
        self.streams.extend(stream_description.create_stream() for stream_description in self.stream_descriptions)
        self.state = 'connecting/dns_lookup'
        notification_center.post_notification('BlinkSessionWillConnect', sender=self, data=NotificationData(sibling=self._sibling))
        self.stream_descriptions = None
        self._sibling = None
        notification_center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='dns_lookup'))
        account = self.account
        settings = SIPSimpleSettings()
        if isinstance(account, Account):
            if account.sip.outbound_proxy is not None:
                proxy = account.sip.outbound_proxy
                uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
            elif account.sip.always_use_my_proxy:
                uri = SIPURI(host=account.id.domain)
            else:
                uri = self.uri
        else:
            uri = self.uri
        self.lookup = DNSLookup()
        notification_center.add_observer(self, sender=self.lookup)
        self.lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

    def add_stream(self, stream_description):
        assert self.state == 'connected'
        if stream_description.type in self.streams:
            raise RuntimeError('session already has a stream of type %s' % stream_description.type)
        self.info.streams[stream_description.type]._reset()
        stream = stream_description.create_stream()
        self.sip_session.add_stream(stream)
        self.streams.add(stream)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWillAddStream', sender=self, data=NotificationData(stream=stream))
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media', 'statistics'}))

    def remove_stream(self, stream):
        assert self.state == 'connected'
        if stream not in self.streams:
            raise RuntimeError('stream is not part of the current session')
        self.sip_session.remove_stream(stream)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWillRemoveStream', sender=self, data=NotificationData(stream=stream))

    def accept_proposal(self, streams):
        assert self.state == 'connected/received_proposal'
        duplicate_types = sorted(stream.type for stream in streams if stream.type in self.streams)
        if duplicate_types:
            raise RuntimeError('accepting proposal would result in duplicated streams for: %s' % ', '.join(duplicate_types))
        self.sip_session.accept_proposal(streams)
        notification_center = NotificationCenter()
        for stream in streams:
            self.info.streams[stream.type]._reset()
            self.streams.add(stream)
            notification_center.post_notification('BlinkSessionWillAddStream', sender=self, data=NotificationData(stream=stream))
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media', 'statistics'}))

    def hold(self):
        if self.sip_session is not None and not self.local_hold:
            self.local_hold = True
            self.sip_session.hold()
            NotificationCenter().post_notification('BlinkSessionDidChangeHoldState', sender=self, data=NotificationData(local_hold=self.local_hold, remote_hold=self.remote_hold))

    def unhold(self):
        if self.sip_session is not None and self.local_hold:
            self.local_hold = False
            self.sip_session.unhold()
            NotificationCenter().post_notification('BlinkSessionDidChangeHoldState', sender=self, data=NotificationData(local_hold=self.local_hold, remote_hold=self.remote_hold))

    def send_dtmf(self, digit):
        audio_stream = self.streams.get('audio')
        if audio_stream is None:
            return
        try:
            audio_stream.send_dtmf(digit)
        except RuntimeError:
            pass
        else:
            digit_map = {'*': 'star'}
            filename = 'sounds/dtmf_%s_tone.wav' % digit_map.get(digit, digit)
            player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get(filename))
            if self.account.rtp.inband_dtmf:
                audio_stream.bridge.add(player)
            SIPApplication.voice_audio_bridge.add(player)
            player.start()

    def start_recording(self):
        # I'd like to start recording before the call starts -Dan
        audio_stream = self.streams.get('audio')
        if audio_stream is not None and not self.recording:
            settings = SIPSimpleSettings()
            direction = self.sip_session.direction
            remote = "%s@%s" % (self.sip_session.remote_identity.uri.user, self.sip_session.remote_identity.uri.host)
            filename = "%s-%s-%s.wav" % (datetime.now().strftime("%Y%m%d-%H%M%S"), remote, direction)
            path = os.path.join(settings.audio.recordings_directory.normalized, self.account.id)
            try:
                audio_stream.start_recording(os.path.join(path, filename))
            except (SIPCoreError, IOError, OSError), e:
                print 'Failed to record: %s' % e

    def stop_recording(self):
        audio_stream = self.streams.get('audio')
        if audio_stream is not None:
            audio_stream.stop_recording()

    def end(self, delete=False):
        if self.state == 'ending':
            self._delete_requested = delete
        elif self.state == 'ended':
            self._delete_requested = delete
            if delete:
                self._delete()
        elif self.state in ('initialized', 'connecting/*', 'connected/*'):
            self._delete_requested = delete
            self.state = 'ending'
            notification_center = NotificationCenter()
            notification_center.post_notification('BlinkSessionWillEnd', sender=self)
            if self.sip_session is None:
                self._terminate(reason='Call cancelled', error=True)
            else:
                self.sip_session.end()

    def _delete(self):
        if self.state != 'ended':
            return
        self.state = 'deleted'

        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWasDeleted', sender=self)

        self.account = None
        self.contact = None
        self.contact_uri = None

    def _terminate(self, reason, error=False):
        notification_center = NotificationCenter()

        if self.state != 'ending':
            self.state = 'ending'
            notification_center.post_notification('BlinkSessionWillEnd', sender=self)

        self.timer.stop()
        self.streams.clear()

        self.lookup = None
        self.sip_session = None
        self.stream_descriptions = None
        self._sibling = None

        self.local_hold = False
        self.remote_hold = False
        self.recording = False

        state = BlinkSessionState('ended')
        state.reason = reason
        state.error = error

        self.state = state
        notification_center.post_notification('BlinkSessionDidEnd', sender=self, data=NotificationData(reason=reason, error=error))

        if not self.persistent:
            self._delete()

    def _parse_uri(self, uri):
        if '@' not in uri:
            uri += '@' + self.account.id.domain
        if not uri.startswith(('sip:', 'sips:')):
            uri = 'sip:' + uri
        return SIPURI.parse(str(uri))

    def _normalize_uri(self, uri):
        from blink.contacts import URIUtils

        if '@' not in uri:
            uri += '@' + self.account.id.domain
        if not uri.startswith(('sip:', 'sips:')):
            uri = 'sip:' + uri
        uri = SIPURI.parse(str(uri).translate(None, ' \t'))
        if URIUtils.is_number(uri.user):
            uri.user = URIUtils.trim_number(uri.user)
            if isinstance(self.account, Account):
                if self.account.pstn.idd_prefix is not None:
                    uri.user = re.sub(r'^\+', self.account.pstn.idd_prefix, uri.user)
                if self.account.pstn.prefix is not None:
                    uri.user = self.account.pstn.prefix + uri.user
        return uri

    def _SH_TimerFired(self):
        self.info.duration += timedelta(seconds=1)
        self.info.streams.audio._update_statistics(self.streams.get('audio', Null).statistics)
        self.info.streams.video._update_statistics(self.streams.get('video', Null).statistics)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'statistics'}))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if notification.sender is self.lookup:
            routes = notification.data.result
            if routes:
                self.sip_session = Session(self.account)
                self.sip_session.connect(ToHeader(self.uri), routes, list(self.streams))
            else:
                self._terminate(reason='Destination not found', error=True)

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if notification.sender is self.lookup:
            self._terminate(reason='Destination not found', error=True)

    def _NH_SIPSessionNewOutgoing(self, notification):
        self.state = 'connecting'
        self.info._update(self)
        notification.center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='connecting'))
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session'}))

    def _NH_SIPSessionGotProvisionalResponse(self, notification):
        if notification.data.code == 180:
            self.state = 'connecting/ringing'
            notification.center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='ringing'))
        elif notification.data.code == 183:
            self.state = 'connecting/early_media'
            notification.center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='early_media'))
        self.info._update(self)
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media'}))

    def _NH_SIPSessionWillStart(self, notification):
        self.state = 'connecting/starting'
        self.info._update(self)
        notification.center.post_notification('BlinkSessionConnectionProgress', sender=self, data=NotificationData(stage='starting'))
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media'}))

    def _NH_SIPSessionDidStart(self, notification):
        for stream in set(self.streams).difference(notification.data.streams):
            self.streams.remove(stream)
        if self.state not in ('ending', 'ended', 'deleted'):
            self.state = 'connected'
            self.timer.start()
            self.info._update(self)
            notification.center.post_notification('BlinkSessionDidConnect', sender=self)
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'session', 'media'}))

    def _NH_SIPSessionDidFail(self, notification):
        if notification.data.failure_reason == 'user request':
            if notification.data.code == 487:
                reason = 'Call cancelled'
            else:
                reason = notification.data.reason
        else:
            reason = notification.data.failure_reason
        self._terminate(reason=reason, error=True)

    def _NH_SIPSessionDidEnd(self, notification):
        self._terminate('Call ended' if notification.data.originator=='local' else 'Call ended by remote')

    def _NH_SIPSessionDidChangeHoldState(self, notification):
        if notification.data.originator == 'remote':
            self.remote_hold = notification.data.on_hold
        notification.center.post_notification('BlinkSessionDidChangeHoldState', sender=self, data=NotificationData(local_hold=self.local_hold, remote_hold=self.remote_hold))

    def _NH_SIPSessionNewProposal(self, notification):
        if self.state not in ('ending', 'ended', 'deleted'):
            if notification.data.originator == 'local':
                self.state = 'connected/sent_proposal'
            else:
                self.state = 'connected/received_proposal'

    def _NH_SIPSessionProposalAccepted(self, notification):
        accepted_streams = notification.data.accepted_streams
        proposed_streams = notification.data.proposed_streams
        if self.state not in ('ending', 'ended', 'deleted'):
            self.state = 'connected'
        for stream in proposed_streams:
            if stream in accepted_streams:
                notification.center.post_notification('BlinkSessionDidAddStream', sender=self, data=NotificationData(stream=stream))
            else:
                self.streams.remove(stream)
                notification.center.post_notification('BlinkSessionDidNotAddStream', sender=self, data=NotificationData(stream=stream))
        if accepted_streams:
            self.info.streams._update(self.streams)
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_SIPSessionProposalRejected(self, notification):
        for stream in set(notification.data.proposed_streams).intersection(self.streams):
            self.streams.remove(stream)
            notification.center.post_notification('BlinkSessionDidNotAddStream', sender=self, data=NotificationData(stream=stream))
        if self.state not in ('ending', 'ended', 'deleted'):
            self.state = 'connected'

    def _NH_SIPSessionHadProposalFailure(self, notification):
        for stream in set(notification.data.proposed_streams).intersection(self.streams):
            self.streams.remove(stream)
            notification.center.post_notification('BlinkSessionDidNotAddStream', sender=self, data=NotificationData(stream=stream))
        if self.state not in ('ending', 'ended', 'deleted'):
            self.state = 'connected'

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        if notification.data.added_streams:
            self._delete_when_done = False
        for stream in set(notification.data.removed_streams).intersection(self.streams):
            self.streams.remove(stream)
            notification.center.post_notification('BlinkSessionDidRemoveStream', sender=self, data=NotificationData(stream=stream))
        if not self.streams:
            self.end()
        elif self.streams.types.isdisjoint({'audio', 'video'}):
            self.unhold()

    def _NH_AudioStreamICENegotiationStateDidChange(self, notification):
        state = notification.data.state
        if state == 'GATHERING':
            self.info.streams.audio.ice_status = 'gathering'
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))
        if state == 'GATHERING_COMPLETE':
            self.info.streams.audio.ice_status = 'gathering_complete'
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))
        elif state == 'NEGOTIATING':
            self.info.streams.audio.ice_status = 'negotiating'
            notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_AudioStreamICENegotiationDidSucceed(self, notification):
        self.info.streams.audio.ice_status = 'succeeded'
        self.info.streams.audio.local_rtp_candidate = notification.sender.local_rtp_candidate
        self.info.streams.audio.remote_rtp_candidate = notification.sender.remote_rtp_candidate
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_AudioStreamICENegotiationDidFail(self, notification):
        self.info.streams.audio.ice_status = 'failed'
        notification.center.post_notification('BlinkSessionInfoUpdated', sender=self, data=NotificationData(elements={'media'}))

    def _NH_AudioStreamGotDTMF(self, notification):
        digit_map = {'*': 'star'}
        filename = 'sounds/dtmf_%s_tone.wav' % digit_map.get(notification.data.digit, notification.data.digit)
        player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get(filename))
        SIPApplication.voice_audio_bridge.add(player)
        player.start()

    def _NH_AudioStreamDidStartRecordingAudio(self, notification):
        self.recording = True
        notification.center.post_notification('BlinkSessionDidChangeRecordingState', sender=self, data=NotificationData(recording=self.recording))

    def _NH_AudioStreamWillStopRecordingAudio(self, notification):
        self.recording = False
        notification.center.post_notification('BlinkSessionDidChangeRecordingState', sender=self, data=NotificationData(recording=self.recording))

    def _NH_BlinkContactDidChange(self, notification):
        notification.center.post_notification('BlinkSessionContactDidChange', sender=self)


class ClientConference(object):
    def __init__(self):
        self.sessions = []
        self.stream_map = {}
        self.audio_conference = AudioConference()
        self.audio_conference.hold()

    def add_session(self, session):
        audio_stream = session.streams.get('audio')
        self.sessions.append(session)
        self.stream_map[session] = audio_stream
        if audio_stream is not None:
            self.audio_conference.add(audio_stream)

    def remove_session(self, session):
        self.sessions.remove(session)
        audio_stream = self.stream_map.pop(session)
        if audio_stream is not None:
            self.audio_conference.remove(audio_stream)

    def hold(self):
        self.audio_conference.hold()

    def unhold(self):
        self.audio_conference.unhold()


class ConferenceParticipant(object):
    implements(IObserver)

    def __init__(self, contact, contact_uri):
        self.contact = contact
        self.contact_uri = contact_uri
        self.uri = contact_uri.uri

        self.active_media = set()
        self.display_name = None
        self.on_hold = False
        self.is_composing = False    # TODO: set this from the chat stream -Saul
        self.request_status = None

        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=contact)

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.contact, self.contact_uri)

    @property
    def pending_request(self):
        return self.request_status is not None

    def _get_is_composing(self):
        return self.__dict__['is_composing']

    def _set_is_composing(self, value):
        old_value = self.__dict__.get('is_composing', False)
        self.__dict__['is_composing'] = value
        if old_value != value:
            NotificationCenter().post_notification('ConferenceParticipantDidChange', sender=self)

    is_composing = property(_get_is_composing, _set_is_composing)
    del _get_is_composing, _set_is_composing

    def _get_request_status(self):
        return self.__dict__['request_status']

    def _set_request_status(self, value):
        old_value = self.__dict__.get('request_status', None)
        self.__dict__['request_status'] = value
        if old_value != value:
            NotificationCenter().post_notification('ConferenceParticipantDidChange', sender=self)

    request_status = property(_get_request_status, _set_request_status)
    del _get_request_status, _set_request_status

    def _update(self, data):
        old_values = dict(active_media=self.active_media.copy(), display_name=self.display_name, on_hold=self.on_hold)
        self.display_name = data.display_text.value if data.display_text else None
        self.active_media.clear()
        for media in chain(*data):
            if media.media_type.value == 'message':
                self.active_media.add('chat')
            else:
                self.active_media.add(media.media_type.value)
        audio_endpoints = [endpt for endpt in data if any(media.media_type=='audio' for media in endpt)]
        self.on_hold = all(endpt.status=='on-hold' for endpt in audio_endpoints) if audio_endpoints else False
        for attr, value in old_values.iteritems():
            if value != getattr(self, attr):
                NotificationCenter().post_notification('ConferenceParticipantDidChange', sender=self)
                break

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkContactDidChange(self, notification):
        notification.center.post_notification('ConferenceParticipantDidChange', sender=self)


class ServerConference(object):
    implements(IObserver)

    sip_prefix_re = re.compile('^sips?:')

    def __init__(self, session):
        self.session = session
        self.sip_session = None

        self.participants = {}
        self.pending_additions = set()
        self.pending_removals = set()

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)

    def add_participant(self, contact, contact_uri):
        if contact_uri.uri in self.participants:
            raise ValueError('%r is already part of the conference' % contact_uri.uri)
        participant = ConferenceParticipant(contact, contact_uri)
        participant.request_status = 'Joining'
        self.session.sip_session.conference.add_participant(participant.uri)
        self.participants[participant.uri] = participant
        self.pending_additions.add(participant)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWillAddParticipant', sender=self.session, data=NotificationData(participant=participant))

    def remove_participant(self, participant):
        if participant.uri not in self.participants:
            raise ValueError('participant %r is not part of the conference' % participant)
        if participant in self.pending_removals:
            return
        participant.request_status = 'Leaving'
        self.session.sip_session.conference.remove_participant(participant.uri)
        self.pending_removals.add(participant)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkSessionWillRemoveParticipant', sender=self.session, data=NotificationData(participant=participant))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidConnect(self, notification):
        self.sip_session = notification.sender.sip_session
        notification.center.add_observer(self, sender=self.sip_session)

    def _NH_BlinkSessionDidEnd(self, notification):
        if self.sip_session is not None:
            notification.center.remove_observer(self, sender=self.sip_session)
        self.sip_session = None
        self.participants.clear()

    def _NH_BlinkSessionWasDeleted(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        self.session = None

    def _NH_SIPSessionGotConferenceInfo(self, notification):
        from blink.contacts import URIUtils
        users = dict((self.sip_prefix_re.sub('', str(user.entity)), user) for user in notification.data.conference_info.users)

        removed_participants = [participant for participant in self.participants.itervalues() if participant.uri not in users and participant not in self.pending_additions]
        confirmed_participants = [participant for participant in self.participants.itervalues() if participant in self.pending_additions and participant.uri in users]
        updated_participants = [self.participants[uri] for uri in users if uri in self.participants]
        added_users = set(users.keys()).difference(self.participants.keys())

        for participant in removed_participants:
            self.participants.pop(participant.uri)
            if participant in self.pending_removals:
                self.pending_removals.remove(participant)
            else:
                notification.center.post_notification('BlinkSessionWillRemoveParticipant', sender=self.session, data=NotificationData(participant=participant))
            notification.center.post_notification('BlinkSessionDidRemoveParticipant', sender=self.session, data=NotificationData(participant=participant))
            participant.request_status = None

        for participant in confirmed_participants:
            participant.request_status = None
            participant._update(users[participant.uri])
            self.pending_additions.remove(participant)
            notification.center.post_notification('BlinkSessionDidAddParticipant', sender=self.session, data=NotificationData(participant=participant))

        for participant in updated_participants:
            participant._update(users[participant.uri])

        for uri in added_users:
            contact, contact_uri = URIUtils.find_contact(uri)
            participant = ConferenceParticipant(contact, contact_uri)
            participant._update(users[participant.uri])
            self.participants[participant.uri] = participant
            notification.center.post_notification('BlinkSessionWillAddParticipant', sender=self.session, data=NotificationData(participant=participant))
            notification.center.post_notification('BlinkSessionDidAddParticipant', sender=self.session, data=NotificationData(participant=participant))

    def _NH_SIPConferenceDidNotAddParticipant(self, notification):
        uri = self.sip_prefix_re.sub('', str(notification.data.participant))
        try:
            participant = self.participants[uri]
        except KeyError:
            return
        if participant not in self.pending_additions:
            return
        participant.request_status = None
        del self.participants[uri]
        self.pending_additions.remove(participant)
        notification.center.post_notification('BlinkSessionDidNotAddParticipant', sender=self.session, data=NotificationData(participant=participant, reason=notification.data.reason))

    def _NH_SIPConferenceDidNotRemoveParticipant(self, notification):
        uri = self.sip_prefix_re.sub('', str(notification.data.participant))
        try:
            participant = self.participants[uri]
        except KeyError:
            return
        if participant not in self.pending_removals:
            return
        participant.request_status = None
        self.pending_removals.remove(participant)
        notification.center.post_notification('BlinkSessionDidNotRemoveParticipant', sender=self.session, data=NotificationData(participant=participant, reason=notification.data.reason))

    def _NH_SIPConferenceGotAddParticipantProgress(self, notification):
        uri = self.sip_prefix_re.sub('', str(notification.data.participant))
        try:
            participant = self.participants[uri]
        except KeyError:
            return
        if participant not in self.pending_additions:
            return
        participant.request_status = notification.data.reason

    def _NH_SIPConferenceGotRemoveParticipantProgress(self, notification):
        uri = self.sip_prefix_re.sub('', str(notification.data.participant))
        try:
            participant = self.participants[uri]
        except KeyError:
            return
        if participant not in self.pending_removals:
            return
        participant.request_status = notification.data.reason


class ConferenceParticipantItem(object):
    implements(IObserver)

    size_hint = QSize(200, 36)

    def __init__(self, participant):
        self.participant = participant
        self.widget = ConferenceParticipantWidget(None)
        self.widget.update_content(self)
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=participant)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.participant)

    @property
    def pending_request(self):
        return self.participant.pending_request

    @property
    def name(self):
        if self.participant.contact.type == 'dummy':
            return self.participant.display_name or self.participant.contact.name
        else:
            return self.participant.contact.name

    @property
    def info(self):
        return self.participant.request_status or self.participant.contact.info

    @property
    def state(self):
        return self.participant.contact.state

    @property
    def on_hold(self):
        return self.participant.on_hold

    @property
    def is_composing(self):
        return self.participant.is_composing

    @property
    def active_media(self):
        return self.participant.active_media

    @property
    def icon(self):
        return self.participant.contact.icon

    @property
    def pixmap(self):
        return self.participant.contact.pixmap

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_ConferenceParticipantDidChange(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ConferenceParticipantItemDidChange', sender=self)


ui_class, base_class = uic.loadUiType(Resources.get('chat_session.ui'))

class ConferenceParticipantWidget(base_class, ui_class):
    class StandardDisplayMode:  __metaclass__ = MarkerType
    class AlternateDisplayMode: __metaclass__ = MarkerType
    class SelectedDisplayMode:  __metaclass__ = MarkerType

    def __init__(self, parent=None):
        super(ConferenceParticipantWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.palettes = Palettes()
        self.palettes.standard = self.palette()
        self.palettes.alternate = self.palette()
        self.palettes.selected = self.palette()
        self.palettes.standard.setColor(QPalette.Window,  self.palettes.standard.color(QPalette.Base))          # We modify the palettes because only the Oxygen theme honors the BackgroundRole if set
        self.palettes.alternate.setColor(QPalette.Window, self.palettes.standard.color(QPalette.AlternateBase)) # AlternateBase set to #f0f4ff or #e0e9ff by designer
        self.palettes.selected.setColor(QPalette.Window,  self.palettes.standard.color(QPalette.Highlight))     # #0066cc #0066d5 #0066dd #0066aa (0, 102, 170) '#256182' (37, 97, 130), #2960a8 (41, 96, 168), '#2d6bbc' (45, 107, 188), '#245897' (36, 88, 151) #0044aa #0055d4
        self.display_mode = self.StandardDisplayMode
        self.hold_icon.installEventFilter(self)
        self.is_composing_icon.installEventFilter(self)
        self.audio_icon.installEventFilter(self)
        self.chat_icon.installEventFilter(self)
        self.video_icon.installEventFilter(self)
        self.screen_sharing_icon.installEventFilter(self)
        self.widget_layout.invalidate()
        self.widget_layout.activate()
        #self.setAttribute(103) # Qt.WA_DontShowOnScreen == 103 and is missing from pyqt, but is present in qt and pyside -Dan
        #self.show()

    def _get_display_mode(self):
        return self.__dict__['display_mode']

    def _set_display_mode(self, value):
        if value not in (self.StandardDisplayMode, self.AlternateDisplayMode, self.SelectedDisplayMode):
            raise ValueError("invalid display_mode: %r" % value)
        old_mode = self.__dict__.get('display_mode', None)
        new_mode = self.__dict__['display_mode'] = value
        if new_mode == old_mode:
            return
        if new_mode is self.StandardDisplayMode:
            self.setPalette(self.palettes.standard)
            self.name_label.setForegroundRole(QPalette.WindowText)
            self.info_label.setForegroundRole(QPalette.Dark)
        elif new_mode is self.AlternateDisplayMode:
            self.setPalette(self.palettes.alternate)
            self.name_label.setForegroundRole(QPalette.WindowText)
            self.info_label.setForegroundRole(QPalette.Dark)
        elif new_mode is self.SelectedDisplayMode:
            self.setPalette(self.palettes.selected)
            self.name_label.setForegroundRole(QPalette.HighlightedText)
            self.info_label.setForegroundRole(QPalette.HighlightedText)

    display_mode = property(_get_display_mode, _set_display_mode)
    del _get_display_mode, _set_display_mode

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.ShowToParent, QEvent.HideToParent):
            self.widget_layout.invalidate()
            self.widget_layout.activate()
        return False

    def update_content(self, participant):
        self.setDisabled(participant.pending_request)
        self.name_label.setText(participant.name)
        self.info_label.setText(participant.info)
        self.icon_label.setPixmap(participant.pixmap)
        self.state_label.state = participant.state
        self.hold_icon.setVisible(participant.on_hold)
        self.is_composing_icon.setVisible(participant.is_composing)
        self.chat_icon.setVisible('chat' in participant.active_media)
        self.video_icon.setVisible('video' in participant.active_media)
        self.screen_sharing_icon.setVisible('screen-sharing' in participant.active_media)
        self.audio_icon.setVisible(participant.active_media.intersection(('audio', 'video', 'screen-sharing')) == {'audio'})

del ui_class, base_class


class ConferenceParticipantDelegate(QStyledItemDelegate, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ConferenceParticipantDelegate, self).__init__(parent)

    def editorEvent(self, event, model, option, index):
        if event.type()==QEvent.MouseButtonRelease and event.button()==Qt.LeftButton and event.modifiers()==Qt.NoModifier:
            cross_rect = option.rect.adjusted(option.rect.width()-14, 0, 0, -option.rect.height()/2) # top half of the rightmost 14 pixels
            if cross_rect.contains(event.pos()):
                item = index.data(Qt.UserRole)
                model.session.server_conference.remove_participant(item.participant)
                return True
        return super(ConferenceParticipantDelegate, self).editorEvent(event, model, option, index)

    def paint(self, painter, option, index):
        participant = index.data(Qt.UserRole)
        if option.state & QStyle.State_Selected:
            participant.widget.display_mode = participant.widget.SelectedDisplayMode
        elif index.row() % 2 == 0:
            participant.widget.display_mode = participant.widget.StandardDisplayMode
        else:
            participant.widget.display_mode = participant.widget.AlternateDisplayMode
        participant.widget.setFixedSize(option.rect.size())

        painter.save()
        painter.drawPixmap(option.rect, QPixmap.grabWidget(participant.widget))
        if option.state & QStyle.State_MouseOver:
            self.drawRemoveIndicator(participant, option, painter, participant.widget)
        if 0 and (option.state & QStyle.State_MouseOver):
            painter.setRenderHint(QPainter.Antialiasing, True)
            if option.state & QStyle.State_Selected:
                painter.fillRect(option.rect, QColor(240, 244, 255, 40))
            else:
                painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                painter.fillRect(option.rect, QColor(240, 244, 255, 230))
        painter.restore()

    def drawRemoveIndicator(self, participant, option, painter, widget):
        pen_thickness = 1.6

        color = option.palette.color(QPalette.Normal, QPalette.WindowText)
        if widget.state_label.state in ('available', 'away', 'busy', 'offline'):
            window_color = widget.state_label.state_colors[widget.state_label.state]
        else:
            window_color = option.palette.color(QPalette.Window)
        background_color = self.background_color(window_color, 0.5)

        pen = QPen(self.deco_color(background_color, color), pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        contrast_pen = QPen(self.calc_light_color(background_color), pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)

        # draw the remove indicator at the top (works best with a state_label of width 14)
        cross_rect = QRect(0, 0, 14, 14)
        cross_rect.moveTopRight(widget.state_label.geometry().topRight())
        cross_rect.translate(option.rect.topLeft())

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.translate(cross_rect.center())
        painter.translate(+1.5, +1)
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawLine(-3.5, -3.5, 3.5, 3.5)
        painter.drawLine(-3.5, 3.5, 3.5, -3.5)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawLine(-3.5, -3.5, 3.5, 3.5)
        painter.drawLine(-3.5, 3.5, 3.5, -3.5)
        painter.restore()

    def sizeHint(self, option, index):
        return index.data(Qt.SizeHintRole)


class ConferenceParticipantModel(QAbstractListModel):
    implements(IObserver)

    participantAboutToBeAdded = pyqtSignal(ConferenceParticipantItem)
    participantAboutToBeRemoved = pyqtSignal(ConferenceParticipantItem)
    participantAdded = pyqtSignal(ConferenceParticipantItem)
    participantRemoved = pyqtSignal(ConferenceParticipantItem)

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-contact-list', 'application/x-blink-contact-uri-list', 'text/uri-list']

    def __init__(self, session, parent=None):
        super(ConferenceParticipantModel, self).__init__(parent)
        self.session = session
        self.participants = []

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session)

    def flags(self, index):
        if index.isValid():
            return QAbstractListModel.flags(self, index) | Qt.ItemIsDropEnabled
        else:
            return QAbstractListModel.flags(self, index) | Qt.ItemIsDropEnabled

    def rowCount(self, parent=QModelIndex()):
        return len(self.participants)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.participants[index.row()]
        if role == Qt.UserRole:
            return item
        elif role == Qt.SizeHintRole:
            return item.size_hint
        elif role == Qt.DisplayRole:
            return unicode(item)
        return None

    def supportedDropActions(self):
        return Qt.CopyAction# | Qt.MoveAction

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

    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        try:
            contacts = pickle.loads(str(mime_data.data('application/x-blink-contact-list')))
        except Exception:
            return False
        for contact in contacts:
            self.session.server_conference.add_participant(contact, contact.uri)
        return True

    def _DH_ApplicationXBlinkContactUriList(self, mime_data, action, index):
        try:
            contact, contact_uris = pickle.loads(str(mime_data.data('application/x-blink-contact-uri-list')))
        except Exception:
            return False
        for contact_uri in contact_uris:
            self.session.server_conference.add_participant(contact, contact_uri.uri)
        return True

    def _DH_TextUriList(self, mime_data, action, index):
        return False

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidEnd(self, notification):
        self.clear()

    def _NH_BlinkSessionWasDeleted(self, notification):
        notification.center.remove_observer(self, sender=self.session)
        self.session = None

    def _NH_BlinkSessionWillAddParticipant(self, notification):
        self.addParticipant(ConferenceParticipantItem(notification.data.participant))

    def _NH_BlinkSessionDidRemoveParticipant(self, notification):
        try:
            participant = next(item for item in self.participants if item.participant is notification.data.participant) # review this (check if it's worth keeping a mapping) -Dan
        except StopIteration:
            return
        self.removeParticipant(participant)

    def _NH_ConferenceParticipantItemDidChange(self, notification):
        index = self.index(self.participants.index(notification.sender))
        self.dataChanged.emit(index, index)

    def _find_insertion_point(self, participant):
        for position, item in enumerate(self.participants):
            if item.name > participant.name:
                break
        else:
            position = len(self.participants)
        return position

    def _add_participant(self, participant):
        position = self._find_insertion_point(participant)
        self.beginInsertRows(QModelIndex(), position, position)
        self.participants.insert(position, participant)
        self.endInsertRows()

    def _pop_participant(self, participant):
        position = self.participants.index(participant)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.participants[position]
        self.endRemoveRows()
        return participant

    def addParticipant(self, participant):
        if participant in self.participants:
            return
        self.participantAboutToBeAdded.emit(participant)
        self._add_participant(participant)
        self.participantAdded.emit(participant)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=participant)

    def removeParticipant(self, participant):
        if participant not in self.participants:
            return
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=participant)
        self.participantAboutToBeRemoved.emit(participant)
        self._pop_participant(participant)
        self.participantRemoved.emit(participant)

    def clear(self):
        notification_center = NotificationCenter()
        self.beginResetModel()
        for participant in self.participants:
            notification_center.remove_observer(self, sender=participant)
        self.participants = []
        self.endResetModel()


class ConferenceParticipantListView(QListView, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ConferenceParticipantListView, self).__init__(parent)
        self.setItemDelegate(ConferenceParticipantDelegate(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.paint_drop_indicator = False

    def setModel(self, model):
        selection_model = self.selectionModel()
        if selection_model is not None:
            selection_model.deleteLater()
        super(ConferenceParticipantListView, self).setModel(model)

    def contextMenuEvent(self, event):
        pass

    def hideEvent(self, event):
        self.context_menu.hide()

    def paintEvent(self, event):
        super(ConferenceParticipantListView, self).paintEvent(event)
        if self.paint_drop_indicator:
            rect = self.viewport().rect() # or should this be self.contentsRect() ? -Dan
            #color = QColor('#b91959')
            #color = QColor('#00aaff')
            #color = QColor('#55aaff')
            #color = QColor('#00aa00')
            #color = QColor('#aa007f')
            #color = QColor('#dd44aa')
            color = QColor('#aa007f')
            pen_color = self.color_with_alpha(color, 120)
            brush_color = self.color_with_alpha(color, 10)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(brush_color)
            painter.setPen(QPen(pen_color, 1.6))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()

    def dragEnterEvent(self, event):
        model = self.model()
        accepted_mime_types = set(model.accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if not acceptable_mime_types:
            event.ignore()
        else:
            event.accept()
            self.setState(self.DraggingState)

    def dragLeaveEvent(self, event):
        super(ConferenceParticipantListView, self).dragLeaveEvent(event)
        self.paint_drop_indicator = False
        self.viewport().update()

    def dragMoveEvent(self, event):
        super(ConferenceParticipantListView, self).dragMoveEvent(event)
        model = self.model()
        for mime_type in model.accepted_mime_types:
            if event.provides(mime_type):
                handler = getattr(self, '_DH_%s' % mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', ''))
                handler(event)
                self.viewport().update()
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if event.source() is self:
            event.setDropAction(Qt.MoveAction)
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        super(ConferenceParticipantListView, self).dropEvent(event)
        self.paint_drop_indicator = False
        self.viewport().update()

    def _DH_ApplicationXBlinkContactList(self, event):
        event.accept(self.viewport().rect())
        self.paint_drop_indicator = True

    def _DH_ApplicationXBlinkContactUriList(self, event):
        event.accept(self.viewport().rect())
        self.paint_drop_indicator = True

    def _DH_TextUriList(self, event):
        event.ignore(self.viewport().rect())
        #event.accept(self.viewport().rect())
        #self.paint_drop_indicator = True


# Positions for sessions in a client conference.
#
class Top(object): pass
class Middle(object): pass
class Bottom(object): pass


# Audio sessions
#

class AudioSessionItem(object):
    implements(IObserver)

    def __init__(self, session):
        assert session.items.audio is None
        self.name = session.contact.name
        self.uri = session.uri
        self.blink_session = session
        self.blink_session.items.audio = self

        self.widget = Null
        self.status = None
        self.type = 'Audio'
        self.codec_info = ''
        self.tls = False
        self.srtp = False
        self.latency = 0
        self.packet_loss = 0
        self.pending_removal = False

        self.__deleted__ = False

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.blink_session)

    @property
    def audio_stream(self):
        return self.blink_session.streams.get('audio')

    def _get_active(self):
        return self.blink_session.active

    def _set_active(self, value):
        self.blink_session.active = bool(value)

    active = property(_get_active, _set_active)
    del _get_active, _set_active

    def _get_client_conference(self):
        return self.blink_session.client_conference

    def _set_client_conference(self, value):
        self.blink_session.client_conference = value

    client_conference = property(_get_client_conference, _set_client_conference)
    del _get_client_conference, _set_client_conference

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

    def _get_srtp(self):
        return self.__dict__['srtp']

    def _set_srtp(self, value):
        if self.__dict__.get('srtp', None) == value:
            return
        self.__dict__['srtp'] = value
        self.widget.srtp_label.setVisible(bool(value))

    srtp = property(_get_srtp, _set_srtp)
    del _get_srtp, _set_srtp

    def _get_tls(self):
        return self.__dict__['tls']

    def _set_tls(self, value):
        if self.__dict__.get('tls', None) == value:
            return
        self.__dict__['tls'] = value
        self.widget.tls_label.setVisible(bool(value))

    tls = property(_get_tls, _set_tls)
    del _get_tls, _set_tls

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

    @property
    def duration(self):
        return self.blink_session.info.duration

    def end(self):
        # this needs to consider the case where the audio stream is being added. in that case we need to cancel the proposal -Dan
        # however that information is not yet available (need the proposed flag on the streams) -Dan
        if len(self.blink_session.streams) > 1 and self.blink_session.state == 'connected':
            self.blink_session.remove_stream(self.audio_stream)
        else:
            self.blink_session.end()

    def delete(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.blink_session)
        self.blink_session.items.audio = None
        self.blink_session = None
        self.widget = Null

    def send_dtmf(self, digit):
        self.blink_session.send_dtmf(digit)

    def _cleanup(self):
        if self.__deleted__:
            return
        self.__deleted__ = True
        self.widget.mute_button.setEnabled(False)
        self.widget.hold_button.setEnabled(False)
        self.widget.record_button.setEnabled(False)
        self.widget.hangup_button.setEnabled(False)

    def _reset_status(self):
        if not self.blink_session.on_hold:
            self.status = None

    def _SH_HangupButtonClicked(self):
        self.end()

    def _SH_HoldButtonClicked(self, checked):
        if checked:
            self.blink_session.hold()
        else:
            self.blink_session.unhold()

    def _SH_MuteButtonClicked(self, checked):
        if self.audio_stream is not None:
            self.audio_stream.muted = checked

    def _SH_RecordButtonClicked(self, checked):
        if checked:
            self.blink_session.start_recording()
        else:
            self.blink_session.stop_recording()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionConnectionProgress(self, notification):
        stage = notification.data.stage
        if stage == 'dns_lookup':
            self.status = Status('Looking up destination...')
        elif stage == 'connecting':
            self.tls = self.blink_session.transport=='tls'
            self.status = Status('Connecting...')
        elif stage == 'ringing':
            self.status = Status('Ringing...')
        elif stage == 'starting':
            self.status = Status('Starting media...')
        else:
            self.status = None

    def _NH_BlinkSessionInfoUpdated(self, notification):
        if 'media' in notification.data.elements:
            audio_info = self.blink_session.info.streams.audio
            self.type = 'HD Audio' if audio_info.sample_rate >= 16000 else 'Audio'
            self.codec_info = audio_info.codec
            self.srtp = audio_info.encryption == 'SRTP'
        if 'statistics' in notification.data.elements:
            self.widget.duration_label.value = self.blink_session.info.duration
            # TODO: compute packet loss and latency statistics -Saul

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        self.widget.hold_button.setChecked(notification.data.local_hold)
        if self.blink_session.state == 'connected':
            if notification.data.local_hold:
                self.status = Status('On hold', color='#000090')
            elif notification.data.remote_hold:
                self.status = Status('Hold by remote', color='#000090')
            else:
                self.status = None

    def _NH_BlinkSessionDidChangeRecordingState(self, notification):
        self.widget.record_button.setChecked(notification.data.recording)

    def _NH_BlinkSessionDidConnect(self, notification):
        session = notification.sender
        self.tls = session.transport=='tls'
        if 'audio' in session.streams:
            self.widget.mute_button.setEnabled(True)
            self.widget.hold_button.setEnabled(True)
            self.widget.record_button.setEnabled(True)
            self.widget.hangup_button.setEnabled(True)
            self.status = Status('Connected')
            call_later(3, self._reset_status)
        else:
            self.status = Status('Audio refused', color='#900000')
            self._cleanup()

    def _NH_BlinkSessionDidAddStream(self, notification):
        if notification.data.stream.type == 'audio':
            self.widget.mute_button.setEnabled(True)
            self.widget.hold_button.setEnabled(True)
            self.widget.record_button.setEnabled(True)
            self.widget.hangup_button.setEnabled(True)
            self.status = Status('Connected')
            call_later(3, self._reset_status)

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'audio':
            self.status = Status('Audio refused', color='#900000') # where can we get the reason from? (rejected, cancelled, failed, ...) -Dan
            self._cleanup()

    def _NH_BlinkSessionWillRemoveStream(self, notification):
        if notification.data.stream.type == 'audio':
            self.widget.mute_button.setEnabled(False)
            self.widget.hold_button.setEnabled(False)
            self.widget.record_button.setEnabled(False)
            self.widget.hangup_button.setEnabled(False)
            self.status = Status('Ending...')

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'audio':
            self.status = Status('Call ended')
            self._cleanup()

    def _NH_BlinkSessionWillEnd(self, notification):
        self.widget.mute_button.setEnabled(False)
        self.widget.hold_button.setEnabled(False)
        self.widget.record_button.setEnabled(False)
        self.widget.hangup_button.setEnabled(False)
        self.status = Status('Ending...')

    def _NH_BlinkSessionDidEnd(self, notification):
        if not self.__deleted__: # may have been removed by BlinkSessionDidRemoveStream less than 5 seconds before the session ended.
            if notification.data.error:
                self.status = Status(notification.data.reason, color='#900000')
            else:
                self.status = Status(notification.data.reason)
            self._cleanup()


ui_class, base_class = uic.loadUiType(Resources.get('audio_session.ui'))

class AudioSessionWidget(base_class, ui_class):
    def __init__(self, session, parent=None):
        super(AudioSessionWidget, self).__init__(parent)
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
        self.position_in_conference = None
        self._disable_dnd = False
        self.mute_button.hidden.connect(self._SH_MuteButtonHidden)
        self.mute_button.shown.connect(self._SH_MuteButtonShown)
        self.mute_button.pressed.connect(self._SH_ToolButtonPressed)
        self.hold_button.pressed.connect(self._SH_ToolButtonPressed)
        self.record_button.pressed.connect(self._SH_ToolButtonPressed)
        self.hangup_button.pressed.connect(self._SH_ToolButtonPressed)
        self.mute_button.hide()
        self.mute_button.setEnabled(False)
        self.hold_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.address_label.setText(session.name)
        self.stream_info_label.session_type = session.type
        self.stream_info_label.codec_info = session.codec_info
        self.duration_label.value = session.duration
        self.latency_label.value = session.latency
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

    def _get_position_in_conference(self):
        return self.__dict__['position_in_conference']

    def _set_position_in_conference(self, value):
        if self.__dict__.get('position_in_conference', Null) == value:
            return
        self.__dict__['position_in_conference'] = value
        self.update()

    position_in_conference = property(_get_position_in_conference, _set_position_in_conference)
    del _get_position_in_conference, _set_position_in_conference

    def _SH_MuteButtonHidden(self):
        self.hold_button.type = LeftSegment

    def _SH_MuteButtonShown(self):
        self.hold_button.type = MiddleSegment

    def _SH_ToolButtonPressed(self):
        self._disable_dnd = True

    def mousePressEvent(self, event):
        self._disable_dnd = False
        super(AudioSessionWidget, self).mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._disable_dnd:
            return
        super(AudioSessionWidget, self).mouseMoveEvent(event)

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
            painter.setPen(QPen(QBrush(QColor('#606060' if self.position_in_conference is None else '#b0b0b0')), 2.0))
        elif self.position_in_conference is not None:
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
        if self.position_in_conference is not None:
            painter.setPen(Qt.NoPen)
            left_rect = rect.adjusted(0, 0, 10-rect.width(), 0)
            if self.position_in_conference is Top:
                painter.drawRect(left_rect.adjusted(2, 5, 0, 5))
            elif self.position_in_conference is Middle:
                painter.drawRect(left_rect.adjusted(2, -5, 0, 5))
            elif self.position_in_conference is Bottom:
                painter.drawRect(left_rect.adjusted(2, -5, 0, -5))

        # draw outer border
        #
        if self.selected or self.drop_indicator:
            painter.setBrush(Qt.NoBrush)
            if self.drop_indicator:
                painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            elif self.selected:
                painter.setPen(QPen(QBrush(QColor('#3075c0')), 2.0)) # or #2070c0 (next best look) or gray: #606060

            if self.position_in_conference is Top:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, 5), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, 5), 3, 3)
            elif self.position_in_conference is Middle:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, 5), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, -5, -1, 5), 3, 3)
            elif self.position_in_conference is Bottom:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, -2), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, -5, -1, -1), 3, 3)
            else:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
        elif self.position_in_conference is not None:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#309030')), 2.0)) # or 237523, #2b8f2b
            if self.position_in_conference is Top:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, 5), 3, 3)
            elif self.position_in_conference is Middle:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, 5), 3, 3)
            elif self.position_in_conference is Bottom:
                painter.drawRoundedRect(rect.adjusted(2, -5, -2, -2), 3, 3)
            else:
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)

        painter.end()
        super(AudioSessionWidget, self).paintEvent(event)


class DraggedAudioSessionWidget(base_class, ui_class):
    """Used to draw a dragged session item"""
    def __init__(self, session_widget, parent=None):
        super(DraggedAudioSessionWidget, self).__init__(parent)
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
        self.in_conference = session_widget.position_in_conference is not None
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
        super(DraggedAudioSessionWidget, self).paintEvent(event)

del ui_class, base_class


class AudioSessionDelegate(QStyledItemDelegate):
    size_hint = QSize(200, 62)

    def __init__(self, parent=None):
        super(AudioSessionDelegate, self).__init__(parent)

    def createEditor(self, parent, options, index):
        session = index.data(Qt.UserRole)
        session.widget = AudioSessionWidget(session, parent)
        session.widget.hold_button.clicked.connect(partial(self._SH_HoldButtonClicked, session)) # this partial still creates a memory cycle -Dan
        return session.widget

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def paint(self, painter, option, index):
        session = index.data(Qt.UserRole)
        if session.widget.size() != option.rect.size():
            # For some reason updateEditorGeometry only receives the peak value
            # of the size that the widget ever had, so it will never shrink it.
            session.widget.resize(option.rect.size())

    def sizeHint(self, option, index):
        return self.size_hint

    def _SH_HoldButtonClicked(self, session, checked):
        if session.client_conference is None and not session.active and not checked:
            session_list = self.parent()
            model = session_list.model()
            selection_model = session_list.selectionModel()
            selection_model.select(model.index(model.sessions.index(session)), selection_model.ClearAndSelect)


class AudioSessionModel(QAbstractListModel):
    implements(IObserver)

    sessionAboutToBeAdded = pyqtSignal(AudioSessionItem)
    sessionAboutToBeRemoved = pyqtSignal(AudioSessionItem)
    sessionAdded = pyqtSignal(AudioSessionItem)
    sessionRemoved = pyqtSignal(AudioSessionItem)
    structureChanged = pyqtSignal()

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-session-list', 'application/x-blink-contact-list', 'application/x-blink-contact-uri-list']

    def __init__(self, parent=None):
        super(AudioSessionModel, self).__init__(parent)
        self.sessions = []
        self.session_list = parent.session_list

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkSessionNewIncoming')
        notification_center.add_observer(self, name='BlinkSessionWillReinitialize')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForIncoming')
        notification_center.add_observer(self, name='BlinkSessionWillConnect')
        notification_center.add_observer(self, name='BlinkSessionDidConnect')
        notification_center.add_observer(self, name='BlinkSessionWillAddStream')
        notification_center.add_observer(self, name='BlinkSessionDidNotAddStream')
        notification_center.add_observer(self, name='BlinkSessionDidRemoveStream')
        notification_center.add_observer(self, name='BlinkSessionDidEnd')

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
        if not index.isValid():
            return None
        item = self.sessions[index.row()]
        if role == Qt.UserRole:
            return item
        elif role == Qt.DisplayRole:
            return unicode(item)
        return None

    def supportedDropActions(self):
        return Qt.CopyAction | Qt.MoveAction

    def mimeTypes(self):
        return ['application/x-blink-session-list']

    def mimeData(self, indexes):
        mime_data = QMimeData()
        sessions = [self.sessions[index.row()] for index in indexes if index.isValid()]
        if sessions:
            # TODO: pass a session id which can then be fetched from the SessionManager -Saul
            mime_data.setData('application/x-blink-session-list', QByteArray())
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
        source = session_list.dragged_session
        target = self.sessions[index.row()] if index.isValid() else None
        if source.client_conference is None:  # the dragged session is not in a conference yet
            if target.client_conference is not None:
                source_row = self.sessions.index(source)
                target_row = self.sessions.index(target.client_conference.sessions[-1].items.audio) + 1
                if self.beginMoveRows(QModelIndex(), source_row, source_row, QModelIndex(), target_row):
                    insert_point = target_row if source_row >= target_row else target_row-1
                    self.sessions.remove(source)
                    self.sessions.insert(insert_point, source)
                    self.endMoveRows()
                source.client_conference = target.client_conference
                session_list.scrollTo(self.index(self.sessions.index(source)), session_list.EnsureVisible) # is this even needed? -Dan
            else:
                target_row = self.sessions.index(target)
                if self.beginMoveRows(QModelIndex(), target_row, target_row, QModelIndex(), 0):
                    self.sessions.remove(target)
                    self.sessions.insert(0, target)
                    self.endMoveRows()
                source_row = self.sessions.index(source)
                if self.beginMoveRows(QModelIndex(), source_row, source_row, QModelIndex(), 1):
                    self.sessions.remove(source)
                    self.sessions.insert(1, source)
                    self.endMoveRows()
                conference = ClientConference()
                target.client_conference = conference # must add them to the conference in the same order they are in the list (target is first, source is last)
                source.client_conference = conference
                session_list.scrollToTop()
            for session in source.client_conference.sessions:
                session.items.audio.widget.selected = source.widget.selected or target.widget.selected
                session.active = source.active or target.active
            if source.active:
                source.client_conference.unhold()
        else:  # the dragged session is in a conference
            dragged = source
            sibling = next(session.items.audio for session in dragged.client_conference.sessions if session.items.audio is not dragged)
            if selection_model.isSelected(self.index(self.sessions.index(dragged))):
                selection_model.select(self.index(self.sessions.index(sibling)), selection_model.ClearAndSelect)
            if len(dragged.client_conference.sessions) == 2:
                dragged.client_conference = None
                sibling.client_conference = None
                ## eventually only move past the last conference to minimize movement. see how this feels during usage. (or sort them alphabetically with conferences at the top) -Dan
                #for position, session in enumerate(self.sessions):
                #    if session not in (dragged, sibling) and session.client_conference is None:
                #        move_point = position
                #        break
                #else:
                #    move_point = len(self.sessions)
                move_point = len(self.sessions)
                dragged_row = self.sessions.index(dragged)
                if self.beginMoveRows(QModelIndex(), dragged_row, dragged_row, QModelIndex(), move_point):
                    self.sessions.remove(dragged)
                    self.sessions.insert(move_point-1, dragged)
                    self.endMoveRows()
                move_point -= 1
                sibling_row = self.sessions.index(sibling)
                if self.beginMoveRows(QModelIndex(), sibling_row, sibling_row, QModelIndex(), move_point):
                    self.sessions.remove(sibling)
                    self.sessions.insert(move_point-1, sibling)
                    self.endMoveRows()
                session_list.scrollToBottom()
            else:
                dragged.client_conference = None
                move_point = len(self.sessions)
                dragged_row = self.sessions.index(dragged)
                if self.beginMoveRows(QModelIndex(), dragged_row, dragged_row, QModelIndex(), move_point):
                    self.sessions.remove(dragged)
                    self.sessions.append(dragged)
                    self.endMoveRows()
                session_list.scrollTo(self.index(self.sessions.index(sibling)), session_list.PositionAtCenter)
            dragged.widget.selected = False
            dragged.active = False
        self.structureChanged.emit()
        return True

    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        if not index.isValid():
            return
        try:
            contacts = pickle.loads(str(mime_data.data('application/x-blink-contact-list')))
        except Exception:
            return False
        session = self.sessions[index.row()]
        session_manager = SessionManager()
        for contact in contacts:
            session_manager.create_session(contact, contact.uri, [StreamDescription('audio')], sibling=session.blink_session)
        return True

    def _DH_ApplicationXBlinkContactUriList(self, mime_data, action, index):
        if not index.isValid():
            return
        try:
            contact, contact_uris = pickle.loads(str(mime_data.data('application/x-blink-contact-uri-list')))
        except Exception:
            return False
        session = self.sessions[index.row()]
        session_manager = SessionManager()
        for contact_uri in contact_uris:
            session_manager.create_session(contact, contact_uri.uri, [StreamDescription('audio')], sibling=session.blink_session)
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
        session.blink_session.clientConferenceChanged.connect(self._SH_BlinkSessionClientConferenceChanged)
        self.sessionAboutToBeAdded.emit(session)
        self._add_session(session)
        # not the right place to do this. the list should do it (else the model needs a backreference to the list), however in addSessionAndConference we can't avoid doing it -Dan
        selection_model = self.session_list.selectionModel()
        selection_model.select(self.index(self.rowCount()-1), selection_model.ClearAndSelect)
        self.sessionAdded.emit(session)
        self.structureChanged.emit()

    def addSessionAndConference(self, session, sibling):
        if session in self.sessions:
            return
        if sibling not in self.sessions:
            raise ValueError('sibling %r not in sessions list' % sibling)
        session.blink_session.clientConferenceChanged.connect(self._SH_BlinkSessionClientConferenceChanged)
        self.sessionAboutToBeAdded.emit(session)
        session_list = self.session_list
        if sibling.client_conference is not None:
            position = self.sessions.index(sibling.client_conference.sessions[-1].items.audio) + 1
            self.beginInsertRows(QModelIndex(), position, position)
            self.sessions.insert(position, session)
            self.endInsertRows()
            session_list.openPersistentEditor(self.index(position))
            session.client_conference = sibling.client_conference
            session_list.scrollTo(self.index(position), session_list.EnsureVisible) # or PositionAtBottom (is this even needed? -Dan)
        else:
            sibling_row = self.sessions.index(sibling)
            if self.beginMoveRows(QModelIndex(), sibling_row, sibling_row, QModelIndex(), 0):
                self.sessions.remove(sibling)
                self.sessions.insert(0, sibling)
                self.endMoveRows()
            self.beginInsertRows(QModelIndex(), 1, 1)
            self.sessions.insert(1, session)
            self.endInsertRows()
            session_list.openPersistentEditor(self.index(1))
            conference = ClientConference()
            sibling.client_conference = conference # must add them to the conference in the same order they are in the list (sibling first, new session last)
            session.client_conference = conference
            if sibling.active:
                conference.unhold()
            session_list.scrollToTop()
        session.widget.selected = sibling.widget.selected
        session.active = sibling.active
        self.sessionAdded.emit(session)
        self.structureChanged.emit()

    def removeSession(self, session):
        if session not in self.sessions:
            return
        self.sessionAboutToBeRemoved.emit(session)
        session_list = self.session_list
        selection_mode = session_list.selectionMode()
        session_list.setSelectionMode(session_list.NoSelection)
        if session.client_conference is not None:
            sibling = next(s.items.audio for s in session.client_conference.sessions if s.items.audio is not session)
            session_index = self.index(self.sessions.index(session))
            sibling_index = self.index(self.sessions.index(sibling))
            selection_model = session_list.selectionModel()
            if selection_model.isSelected(session_index):
                selection_model.select(sibling_index, selection_model.ClearAndSelect)
        self._remove_session(session)
        session_list.setSelectionMode(selection_mode)
        if session.client_conference is not None:
            if len(session.client_conference.sessions) == 2:
                first, last = session.client_conference.sessions
                first.client_conference = None
                last.client_conference = None
            else:
                session.client_conference = None

        session.blink_session.clientConferenceChanged.disconnect(self._SH_BlinkSessionClientConferenceChanged)
        session.delete()

        self.sessionRemoved.emit(session)
        self.structureChanged.emit()

    def conferenceSessions(self, sessions):
        session_list = self.session_list
        selected = any(session.widget.selected for session in sessions)
        active = any(session.active for session in sessions)
        conference = ClientConference()
        for position, session in enumerate(sessions):
            session_row = self.sessions.index(session)
            if self.beginMoveRows(QModelIndex(), session_row, session_row, QModelIndex(), position):
                self.sessions.remove(session)
                self.sessions.insert(position, session)
                self.endMoveRows()
            session.client_conference = conference
            session.widget.selected = selected
            session.active = active
        if active:
            conference.unhold()
        session_list.scrollToTop()
        self.structureChanged.emit()

    def breakConference(self, conference): # replace this by an endConference (or termninate/hangupConference) functionality -Dan
        sessions = [blink_session.items.audio for blink_session in conference.sessions]
        session_list = self.session_list
        selection_model = session_list.selectionModel()
        selection = selection_model.selection()
        selected_session = selection[0].topLeft().data(Qt.UserRole) if selection else None
        move_point = len(self.sessions)
        for index, session in enumerate(reversed(sessions)):
            session_row = self.sessions.index(session)
            if self.beginMoveRows(QModelIndex(), session_row, session_row, QModelIndex(), move_point-index):
                self.sessions.remove(session)
                self.sessions.insert(move_point-index-1, session)
                self.endMoveRows()
            session.client_conference = None
            session.widget.selected = session is selected_session
            session.active = session is selected_session
        session_list.scrollToBottom()
        self.structureChanged.emit()

    def _SH_BlinkSessionClientConferenceChanged(self, old_conference, new_conference): # would this better be handled by the audio session item itself? (apparently not) -Dan
        blink_session = self.sender()
        session = blink_session.items.audio

        if not new_conference:
            session.widget.position_in_conference = None
            session.widget.mute_button.hide()
        if session.widget.mute_button.isChecked():
            session.widget.mute_button.click()

        for conference in (conference for conference in (old_conference, new_conference) if conference):
            session_count = len(conference.sessions)
            if session_count == 1:
                blink_session = conference.sessions[0]
                session = blink_session.items.audio
                session.widget.position_in_conference = None
                session.widget.mute_button.hide()
            elif session_count > 1:
                for blink_session in conference.sessions:
                    session = blink_session.items.audio
                    session.widget.position_in_conference = Top if blink_session is conference.sessions[0] else Bottom if blink_session is conference.sessions[-1] else Middle
                    session.widget.mute_button.show()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionNewIncoming(self, notification):
        session = notification.sender
        if 'audio' in session.streams:
            session_item = AudioSessionItem(session)
            self.addSession(session_item)

    def _NH_BlinkSessionDidReinitializeForIncoming(self, notification):
        session = notification.sender
        if 'audio' in session.streams:
            session_item = AudioSessionItem(session)
            self.addSession(session_item)

    def _NH_BlinkSessionWillConnect(self, notification):
        session = notification.sender
        if 'audio' in session.streams:
            session_item = AudioSessionItem(session)
            if notification.data.sibling is not None:
                self.addSessionAndConference(session_item, notification.data.sibling.items.audio)
            else:
                self.addSession(session_item)

    def _NH_BlinkSessionDidConnect(self, notification):
        session = notification.sender
        session_item = session.items.audio
        if session_item is not None and 'audio' not in session.streams:
            session_item.pending_removal = True
            call_later(5, self.removeSession, session_item)
            self.structureChanged.emit()

    def _NH_BlinkSessionWillAddStream(self, notification):
        if notification.data.stream.type == 'audio':
            if notification.sender.items.audio is not None:
                self.removeSession(notification.sender.items.audio)
            session_item = AudioSessionItem(notification.sender)
            self.addSession(session_item)

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'audio':
            session_item = notification.sender.items.audio
            session_item.pending_removal = True
            call_later(5, self.removeSession, session_item)
            self.structureChanged.emit()

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'audio':
            session_item = notification.sender.items.audio
            session_item.pending_removal = True
            call_later(5, self.removeSession, session_item)
            self.structureChanged.emit()

    def _NH_BlinkSessionDidEnd(self, notification):
        session_item = notification.sender.items.audio
        if session_item is not None and not session_item.pending_removal:
            session_item.pending_removal = True
            call_later(5, self.removeSession, session_item)
            self.structureChanged.emit()

    def _NH_BlinkSessionWillReinitialize(self, notification):
        session_item = notification.sender.items.audio
        if session_item is not None:
            self.removeSession(session_item)


# workaround class because passing context to the QShortcut constructor segfaults (fixed upstreams on 09-Apr-2013) -Dan
class QShortcut(QShortcut):
    def __init__(self, key, parent, member=None, ambiguousMember=None, context=Qt.WindowShortcut):
        super(QShortcut, self).__init__(key, parent, member, ambiguousMember)
        self.setContext(context)


class AudioSessionListView(QListView):
    implements(IObserver)

    def __init__(self, parent=None):
        super(AudioSessionListView, self).__init__(parent)
        self.setItemDelegate(AudioSessionDelegate(self))
        self.setDropIndicatorShown(False)
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.dragged_session = None
        self.ignore_selection_changes = False
        self._pressed_position = None
        self._pressed_index = None
        self._hangup_shortcuts = []
        self._hangup_shortcuts.append(QShortcut('Ctrl+Esc', self, member=self._SH_HangupShortcutActivated, context=Qt.ApplicationShortcut))
        self._hangup_shortcuts.append(QShortcut('Ctrl+Delete', self, member=self._SH_HangupShortcutActivated, context=Qt.ApplicationShortcut))
        self._hangup_shortcuts.append(QShortcut('Ctrl+Backspace', self, member=self._SH_HangupShortcutActivated, context=Qt.ApplicationShortcut))
        self._hold_shortcut = QShortcut('Ctrl+Space', self, member=self._SH_HoldShortcutActivated, context=Qt.ApplicationShortcut)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkActiveSessionDidChange')

    def contextMenuEvent(self, event):
        pass

    def hideEvent(self, event):
        self.context_menu.hide()

    def keyPressEvent(self, event):
        digit = chr(event.key()) if event.key() < 256 else None
        if digit is not None and digit in string.digits+string.uppercase+'#*':
            letter_map = {'2': 'ABC', '3': 'DEF', '4': 'GHI', '5': 'JKL', '6': 'MNO', '7': 'PQRS', '8': 'TUV', '9': 'WXYZ'}
            letter_map = dict(chain(*(izip(letters, repeat(digit)) for digit, letters in letter_map.iteritems())))
            for session in (s for s in self.model().sessions if s.active):
                session.send_dtmf(letter_map.get(digit, digit))
        elif event.key() in (Qt.Key_Up, Qt.Key_Down):
            selection_model = self.selectionModel()
            current_index = selection_model.currentIndex()
            if current_index.isValid():
                step = 1 if event.key() == Qt.Key_Down else -1
                conference = current_index.data(Qt.UserRole).client_conference
                new_index = current_index.sibling(current_index.row()+step, current_index.column())
                while conference is not None and new_index.isValid() and new_index.data(Qt.UserRole).client_conference is conference:
                    new_index = new_index.sibling(new_index.row()+step, new_index.column())
                if new_index.isValid():
                    selection_model.select(new_index, selection_model.ClearAndSelect)
        else:
            super(AudioSessionListView, self).keyPressEvent(event)

    def mousePressEvent(self, event):
        self._pressed_position = event.pos()
        self._pressed_index = self.indexAt(self._pressed_position)
        super(AudioSessionListView, self).mousePressEvent(event)
        selection_model = self.selectionModel()
        selected_indexes = selection_model.selectedIndexes()
        if selected_indexes:
            selection_model.setCurrentIndex(selected_indexes[0], selection_model.Select)
        else:
            selection_model.setCurrentIndex(self.model().index(-1), selection_model.Select)

    def mouseReleaseEvent(self, event):
        self._pressed_position = None
        self._pressed_index = None
        super(AudioSessionListView, self).mouseReleaseEvent(event)

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
            return super(AudioSessionListView, self).selectionCommand(index, event)

    def selectionChanged(self, selected, deselected):
        super(AudioSessionListView, self).selectionChanged(selected, deselected)
        selected_indexes = selected.indexes()
        deselected_indexes = deselected.indexes()
        for session in (index.data(Qt.UserRole) for index in deselected_indexes):
            if session.client_conference is not None:
                for sibling in session.client_conference.sessions:
                    sibling.items.audio.widget.selected = False
            else:
                session.widget.selected = False
        for session in (index.data(Qt.UserRole) for index in selected_indexes):
            if session.client_conference is not None:
                for sibling in session.client_conference.sessions:
                    sibling.items.audio.widget.selected = True
            else:
                session.widget.selected = True
        if selected_indexes:
            self.setCurrentIndex(selected_indexes[0])
        else:
            self.setCurrentIndex(self.model().index(-1))
        self.context_menu.hide()
        #print "-- audio selection changed %s -> %s (ignore=%s)" % ([x.row() for x in deselected.indexes()], [x.row() for x in selected.indexes()], self.ignore_selection_changes)
        if self.ignore_selection_changes:
            return
        notification_center = NotificationCenter()
        selected_blink_session = selected[0].topLeft().data(Qt.UserRole).blink_session if selected else None
        deselected_blink_session = deselected[0].topLeft().data(Qt.UserRole).blink_session if deselected else None
        notification_data = NotificationData(selected_session=selected_blink_session, deselected_session=deselected_blink_session)
        notification_center.post_notification('BlinkSessionListSelectionChanged', sender=self, data=notification_data)

    def startDrag(self, supported_actions):
        if self._pressed_index is not None and self._pressed_index.isValid():
            self.dragged_session = self._pressed_index.data(Qt.UserRole)
            rect = self.visualRect(self._pressed_index)
            rect.adjust(1, 1, -1, -1)
            pixmap = QPixmap(rect.size())
            pixmap.fill(Qt.transparent)
            widget = DraggedAudioSessionWidget(self.dragged_session.widget, None)
            widget.resize(rect.size())
            widget.render(pixmap)
            drag = QDrag(self)
            drag.setPixmap(pixmap)
            drag.setMimeData(self.model().mimeData([self._pressed_index]))
            drag.setHotSpot(self._pressed_position - rect.topLeft())
            drag.exec_(supported_actions, Qt.CopyAction)
            self.dragged_session = None
            self._pressed_position = None
            self._pressed_index = None

    def dragEnterEvent(self, event):
        event_source = event.source()
        accepted_mime_types = set(self.model().accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
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
        super(AudioSessionListView, self).dragLeaveEvent(event)
        for session in self.model().sessions:
            session.widget.drop_indicator = False

    def dragMoveEvent(self, event):
        super(AudioSessionListView, self).dragMoveEvent(event)
        if event.source() is self:
            event.setDropAction(Qt.MoveAction)

        model = self.model()

        for session in model.sessions:
            session.widget.drop_indicator = False

        for mime_type in model.accepted_mime_types:
            if event.provides(mime_type):
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                session = index.data(Qt.UserRole)
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
        super(AudioSessionListView, self).dropEvent(event)

    def _DH_ApplicationXBlinkSessionList(self, event, index, rect, session):
        dragged_session = self.dragged_session
        if not index.isValid():
            model = self.model()
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
            if dragged_session.client_conference is not None:
                event.accept(rect)
            else:
                event.ignore(rect)
        else:
            conference = dragged_session.client_conference or Null
            if dragged_session is session or session.blink_session in conference.sessions:
                event.ignore(rect)
            else:
                if dragged_session.client_conference is None:
                    if session.client_conference is not None:
                        for sibling in session.client_conference.sessions:
                            sibling.items.audio.widget.drop_indicator = True
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
            if session.client_conference is not None:
                for sibling in session.client_conference.sessions:
                    sibling.items.audio.widget.drop_indicator = True
            else:
                session.widget.drop_indicator = True

    def _DH_ApplicationXBlinkContactUriList(self, event, index, rect, session):
        model = self.model()
        if not index.isValid():
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
            event.ignore(rect)
        else:
            event.accept(rect)
            if session.client_conference is not None:
                for sibling in session.client_conference.sessions:
                    sibling.items.audio.widget.drop_indicator = True
            else:
                session.widget.drop_indicator = True

    def _SH_HangupShortcutActivated(self):
        session = self.selectedIndexes()[0].data(Qt.UserRole)
        if session.client_conference is None:
            session.widget.hangup_button.click()

    def _SH_HoldShortcutActivated(self):
        session = self.selectedIndexes()[0].data(Qt.UserRole)
        if session.client_conference is None:
            session.widget.hold_button.click()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkActiveSessionDidChange(self, notification):
        self.ignore_selection_changes = True
        selection_model = self.selectionModel()
        if notification.data.active_session is None:
            selection = selection_model.selection()
            # check the code in this if branch if it's needed -Dan
            #selected_blink_session = selection[0].topLeft().data(Qt.UserRole).blink_session if selection else None
            #if notification.data.previous_active_session is selected_blink_session:
            #    print "-- audio session list updating selection to None None"
            #    selection_model.clearSelection()
        else:
            model = self.model()
            position = model.sessions.index(notification.data.active_session.items.audio)
            #print "-- audio session list updating selection to", position, notification.data.active_session
            selection_model.select(model.index(position), selection_model.ClearAndSelect)
        self.ignore_selection_changes = False


# Chat sessions
#

class ChatSessionItem(object):
    implements(IObserver)

    size_hint = QSize(200, 36)

    def __init__(self, blink_session):
        self.blink_session = blink_session
        self.blink_session.items.chat = self
        self.remote_composing = False
        self.remote_composing_timer = QTimer()
        self.remote_composing_timer.timeout.connect(self._SH_RemoteComposingTimerTimeout)
        self.participants_model = ConferenceParticipantModel(blink_session)
        self.widget = ChatSessionWidget(None)
        self.widget.update_content(self)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=blink_session)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.blink_session)

    @property
    def name(self):
        return self.blink_session.contact.name

    @property
    def info(self):
        return self.blink_session.contact.note or self.blink_session.contact_uri.uri

    @property
    def state(self):
        return self.blink_session.contact.state

    @property
    def icon(self):
        return self.blink_session.contact.icon

    @property
    def pixmap(self):
        return self.blink_session.contact.pixmap

    @property
    def chat_stream(self):
        return self.blink_session.streams.get('chat')

    def _get_remote_composing(self):
        return self.__dict__['remote_composing']

    def _set_remote_composing(self, value):
        old_value = self.__dict__.get('remote_composing', False)
        self.__dict__['remote_composing'] = value
        if value != old_value and self.widget is not None:
            self.widget.is_composing_icon.setVisible(value)
            notification_center = NotificationCenter()
            notification_center.post_notification('ChatSessionItemDidChange', sender=self)

    remote_composing = property(_get_remote_composing, _set_remote_composing)
    del _get_remote_composing, _set_remote_composing

    def end(self, delete=False):
        self.blink_session.end(delete=delete)

    def delete(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.blink_session)
        self.participants_model = None
        self.blink_session.items.chat = None
        self.blink_session = None
        self.widget = None

    def update_composing_indication(self, data):
        if data.state == 'active':
            self.remote_composing = True
            refresh_rate = data.refresh if data.refresh else 120
            self.remote_composing_timer.start(refresh_rate*1000)
        elif data.state == 'idle':
            self.remote_composing = False
            self.remote_composing_timer.stop()

    def _SH_RemoteComposingTimerTimeout(self):
        self.remote_composing_timer.stop()
        self.remote_composing = False

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionContactDidChange(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidReinitializeForIncoming(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidReinitializeForOutgoing(self, notification):
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionWillConnect(self, notification):
        self.widget.chat_icon.setEnabled(False)
        self.widget.audio_icon.setEnabled(False)
        self.widget.video_icon.setEnabled(False)
        self.widget.screen_sharing_icon.setEnabled(False)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidConnect(self, notification):
        self.widget.chat_icon.setEnabled(True)
        self.widget.audio_icon.setEnabled(True)
        self.widget.video_icon.setEnabled(True)
        self.widget.screen_sharing_icon.setEnabled(True)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionWillAddStream(self, notification):
        icon_label = getattr(self.widget, "%s_icon" % notification.data.stream.type.replace('-', '_'))
        icon_label.setEnabled(False)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidAddStream(self, notification):
        icon_label = getattr(self.widget, "%s_icon" % notification.data.stream.type.replace('-', '_'))
        icon_label.setEnabled(True)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        icon_label = getattr(self.widget, "%s_icon" % notification.data.stream.type.replace('-', '_'))
        icon_label.setEnabled(True)
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'chat':
            self.remote_composing = False
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidEnd(self, notification):
        self.remote_composing = False
        self.widget.update_content(self)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        self.widget.hold_icon.setVisible(self.blink_session.on_hold)
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)

    def _NH_BlinkSessionDidChangeRecordingState(self, notification):
        notification.center.post_notification('ChatSessionItemDidChange', sender=self)


class Palettes(object):
    pass

ui_class, base_class = uic.loadUiType(Resources.get('chat_session.ui'))

class ChatSessionWidget(base_class, ui_class):
    class StandardDisplayMode:  __metaclass__ = MarkerType
    class AlternateDisplayMode: __metaclass__ = MarkerType
    class SelectedDisplayMode:  __metaclass__ = MarkerType

    def __init__(self, parent=None):
        super(ChatSessionWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.palettes = Palettes()
        self.palettes.standard = self.palette()
        self.palettes.alternate = self.palette()
        self.palettes.selected = self.palette()
        self.palettes.standard.setColor(QPalette.Window,  self.palettes.standard.color(QPalette.Base))          # We modify the palettes because only the Oxygen theme honors the BackgroundRole if set
        self.palettes.alternate.setColor(QPalette.Window, self.palettes.standard.color(QPalette.AlternateBase)) # AlternateBase set to #f0f4ff or #e0e9ff by designer
        self.palettes.selected.setColor(QPalette.Window,  self.palettes.standard.color(QPalette.Highlight))     # #0066cc #0066d5 #0066dd #0066aa (0, 102, 170) '#256182' (37, 97, 130), #2960a8 (41, 96, 168), '#2d6bbc' (45, 107, 188), '#245897' (36, 88, 151) #0044aa #0055d4
        self.display_mode = self.StandardDisplayMode
        self.hold_icon.installEventFilter(self)
        self.is_composing_icon.installEventFilter(self)
        self.audio_icon.installEventFilter(self)
        self.chat_icon.installEventFilter(self)
        self.video_icon.installEventFilter(self)
        self.screen_sharing_icon.installEventFilter(self)
        self.widget_layout.invalidate()
        self.widget_layout.activate()
        #self.setAttribute(103) # Qt.WA_DontShowOnScreen == 103 and is missing from pyqt, but is present in qt and pyside -Dan
        #self.show()

    def _get_display_mode(self):
        return self.__dict__['display_mode']

    def _set_display_mode(self, value):
        if value not in (self.StandardDisplayMode, self.AlternateDisplayMode, self.SelectedDisplayMode):
            raise ValueError("invalid display_mode: %r" % value)
        old_mode = self.__dict__.get('display_mode', None)
        new_mode = self.__dict__['display_mode'] = value
        if new_mode == old_mode:
            return
        if new_mode is self.StandardDisplayMode:
            self.setPalette(self.palettes.standard)
            self.name_label.setForegroundRole(QPalette.WindowText)
            self.info_label.setForegroundRole(QPalette.Dark)
        elif new_mode is self.AlternateDisplayMode:
            self.setPalette(self.palettes.alternate)
            self.name_label.setForegroundRole(QPalette.WindowText)
            self.info_label.setForegroundRole(QPalette.Dark)
        elif new_mode is self.SelectedDisplayMode:
            self.setPalette(self.palettes.selected)
            self.name_label.setForegroundRole(QPalette.HighlightedText)
            self.info_label.setForegroundRole(QPalette.HighlightedText)

    display_mode = property(_get_display_mode, _set_display_mode)
    del _get_display_mode, _set_display_mode

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.ShowToParent, QEvent.HideToParent):
            self.widget_layout.invalidate()
            self.widget_layout.activate()
        return False

    def update_content(self, session):
        self.name_label.setText(session.name)
        self.info_label.setText(session.info)
        self.icon_label.setPixmap(session.pixmap)
        self.state_label.state = session.state
        self.hold_icon.setVisible(session.blink_session.on_hold)
        self.is_composing_icon.setVisible(session.remote_composing)
        self.chat_icon.setVisible('chat' in session.blink_session.streams)
        self.video_icon.setVisible('video' in session.blink_session.streams)
        self.screen_sharing_icon.setVisible('screen-sharing' in session.blink_session.streams)
        self.audio_icon.setVisible(session.blink_session.streams.types.intersection(('audio', 'video', 'screen-sharing')) == {'audio'})

del ui_class, base_class


class ChatSessionDelegate(QStyledItemDelegate, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ChatSessionDelegate, self).__init__(parent)

    def editorEvent(self, event, model, option, index):
        if event.type()==QEvent.MouseButtonRelease and event.button()==Qt.LeftButton and event.modifiers()==Qt.NoModifier:
            arrow_rect = option.rect.adjusted(option.rect.width()-14, option.rect.height()/2, 0, 0)  # bottom half of the rightmost 14 pixels
            cross_rect = option.rect.adjusted(option.rect.width()-14, 0, 0, -option.rect.height()/2) # top half of the rightmost 14 pixels
            if arrow_rect.contains(event.pos()):
                session_list = self.parent()
                session_list.animation.setDirection(QPropertyAnimation.Backward)
                session_list.animation.start()
                return True
            elif cross_rect.contains(event.pos()):
                session = index.data(Qt.UserRole)
                session.end(delete=True)
                return True
        return super(ChatSessionDelegate, self).editorEvent(event, model, option, index)

    def paint(self, painter, option, index):
        session = index.data(Qt.UserRole)
        if option.state & QStyle.State_Selected:
            session.widget.display_mode = session.widget.SelectedDisplayMode
        elif index.row() % 2 == 0:
            session.widget.display_mode = session.widget.StandardDisplayMode
        else:
            session.widget.display_mode = session.widget.AlternateDisplayMode
        session.widget.setFixedSize(option.rect.size())

        painter.save()
        painter.drawPixmap(option.rect, QPixmap.grabWidget(session.widget))
        if option.state & QStyle.State_MouseOver:
            self.drawSessionIndicators(session, option, painter, session.widget)
        if 0 and (option.state & QStyle.State_MouseOver):
            painter.setRenderHint(QPainter.Antialiasing, True)
            if option.state & QStyle.State_Selected:
                painter.fillRect(option.rect, QColor(240, 244, 255, 40))
            else:
                painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                painter.fillRect(option.rect, QColor(240, 244, 255, 230))
        painter.restore()

    def drawSessionIndicators(self, session, option, painter, widget):
        pen_thickness = 1.6

        color = option.palette.color(QPalette.Normal, QPalette.WindowText)
        if widget.state_label.state in ('available', 'away', 'busy', 'offline'):
            window_color = widget.state_label.state_colors[widget.state_label.state]
        else:
            window_color = option.palette.color(QPalette.Window)
        background_color = self.background_color(window_color, 0.5)

        pen = QPen(self.deco_color(background_color, color), pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        contrast_pen = QPen(self.calc_light_color(background_color), pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)

        # draw the expansion indicator at the bottom (works best with a state_label of width 14)
        arrow_rect = QRect(0, 0, 14, 14)
        arrow_rect.moveBottomRight(widget.state_label.geometry().bottomRight())
        arrow_rect.translate(option.rect.topLeft())

        arrow = QPolygonF([QPointF(3, 1.5), QPointF(-0.5, -2.5), QPointF(-4, 1.5)])
        arrow.translate(2, 1)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.translate(arrow_rect.center())
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawPolyline(arrow)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawPolyline(arrow)
        painter.restore()

        # draw the close indicator at the top (works best with a state_label of width 14)
        cross_rect = QRect(0, 0, 14, 14)
        cross_rect.moveTopRight(widget.state_label.geometry().topRight())
        cross_rect.translate(option.rect.topLeft())

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.translate(cross_rect.center())
        painter.translate(+1.5, +1)
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawLine(-3.5, -3.5, 3.5, 3.5)
        painter.drawLine(-3.5, 3.5, 3.5, -3.5)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawLine(-3.5, -3.5, 3.5, 3.5)
        painter.drawLine(-3.5, 3.5, 3.5, -3.5)
        painter.restore()

    def sizeHint(self, option, index):
        return index.data(Qt.SizeHintRole)


class ChatSessionModel(QAbstractListModel):
    implements(IObserver)

    sessionAboutToBeAdded = pyqtSignal(ChatSessionItem)
    sessionAboutToBeRemoved = pyqtSignal(ChatSessionItem)
    sessionAdded = pyqtSignal(ChatSessionItem)
    sessionRemoved = pyqtSignal(ChatSessionItem)

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-contact-list', 'text/uri-list']

    def __init__(self, parent=None):
        super(ChatSessionModel, self).__init__(parent)
        self.sessions = []

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkSessionNewIncoming')
        notification_center.add_observer(self, name='BlinkSessionNewOutgoing')
        notification_center.add_observer(self, name='BlinkSessionWasDeleted')
        notification_center.add_observer(self, name='ChatSessionItemDidChange')

    def flags(self, index):
        if index.isValid():
            return QAbstractListModel.flags(self, index) | Qt.ItemIsDropEnabled
        else:
            return QAbstractListModel.flags(self, index) | Qt.ItemIsDropEnabled

    def rowCount(self, parent=QModelIndex()):
        return len(self.sessions)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.sessions[index.row()]
        if role == Qt.UserRole:
            return item
        elif role == Qt.SizeHintRole:
            return item.size_hint
        elif role == Qt.DisplayRole:
            return unicode(item)
        return None

    def supportedDropActions(self):
        return Qt.CopyAction# | Qt.MoveAction

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

    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        return True

    def _DH_TextUriList(self, mime_data, action, index):
        return False

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionNewIncoming(self, notification):
        self.addSession(ChatSessionItem(notification.sender))

    def _NH_BlinkSessionNewOutgoing(self, notification):
        self.addSession(ChatSessionItem(notification.sender))

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.removeSession(notification.sender.items.chat)

    def _NH_ChatSessionItemDidChange(self, notification):
        index = self.index(self.sessions.index(notification.sender))
        self.dataChanged.emit(index, index)

    def _find_insertion_point(self, session):
        for position, item in enumerate(self.sessions):
            if item.name > session.name:
                break
        else:
            position = len(self.sessions)
        return position

    def _add_session(self, session):
        position = self._find_insertion_point(session)
        self.beginInsertRows(QModelIndex(), position, position)
        self.sessions.insert(position, session)
        self.endInsertRows()

    def _pop_session(self, session):
        position = self.sessions.index(session)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.sessions[position]
        self.endRemoveRows()
        return session

    def addSession(self, session):
        if session in self.sessions:
            return
        self.sessionAboutToBeAdded.emit(session)
        self._add_session(session)
        self.sessionAdded.emit(session)

    def removeSession(self, session):
        if session not in self.sessions:
            return
        self.sessionAboutToBeRemoved.emit(session)
        self._pop_session(session).delete()
        self.sessionRemoved.emit(session)


class ChatSessionListView(QListView):
    implements(IObserver)

    def __init__(self, chat_window):
        super(ChatSessionListView, self).__init__(chat_window.session_panel)
        self.chat_window = chat_window
        self.setItemDelegate(ChatSessionDelegate(self))

        self.setMouseTracking(True)
        self.setAlternatingRowColors(True)
        self.setAutoFillBackground(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        #self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded) # default
        self.setDragEnabled(False) # default
        #self.setDropIndicatorShown(True)
        self.setDragDropMode(QListView.DropOnly)
        self.setSelectionMode(QListView.SingleSelection) # default

        self.setStyleSheet("""QListView { border: 1px inset palette(dark); border-radius: 3px; }""")
        self.animation = QPropertyAnimation(self, 'geometry')
        self.animation.setDuration(250)
        self.animation.setEasingCurve(QEasingCurve.Linear)
        self.animation.finished.connect(self._SH_AnimationFinished)
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.drop_indicator_index = QModelIndex()
        self.ignore_selection_changes = False
        self.doubleClicked.connect(self._SH_DoubleClicked) # activated is emitted on single click
        chat_window.session_panel.installEventFilter(self)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkActiveSessionDidChange')

    def selectionChanged(self, selected, deselected):
        super(ChatSessionListView, self).selectionChanged(selected, deselected)
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1)
            selection_model.setCurrentIndex(index, selection_model.Select)
        self.context_menu.hide()
        if self.ignore_selection_changes:
            return
        notification_center = NotificationCenter()
        selected_blink_session = selected[0].topLeft().data(Qt.UserRole).blink_session if selected else None
        deselected_blink_session = deselected[0].topLeft().data(Qt.UserRole).blink_session if deselected else None
        notification_data = NotificationData(selected_session=selected_blink_session, deselected_session=deselected_blink_session)
        notification_center.post_notification('BlinkSessionListSelectionChanged', sender=self, data=notification_data)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            new_size = event.size()
            geometry = self.animation.endValue()
            if geometry is not None:
                old_size = geometry.size()
                geometry.setSize(new_size)
                self.animation.setEndValue(geometry)
                geometry = self.animation.startValue()
                geometry.setWidth(geometry.width() + new_size.width() - old_size.width())
                self.animation.setStartValue(geometry)
            self.resize(new_size)
        return False

    def contextMenuEvent(self, event):
        pass

    def hideEvent(self, event):
        self.context_menu.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.selectionModel().selection():
            self.animation.setDirection(QPropertyAnimation.Backward)
            self.animation.start()
        else:
            super(ChatSessionListView, self).keyPressEvent(event)

    def paintEvent(self, event):
        super(ChatSessionListView, self).paintEvent(event)
        if self.drop_indicator_index.isValid():
            rect = self.visualRect(self.drop_indicator_index)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()

    def dragEnterEvent(self, event):
        model = self.model()
        accepted_mime_types = set(model.accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if not acceptable_mime_types:
            event.ignore() # no acceptable mime types found
        else:
            event.accept()
            self.setState(self.DraggingState)

    def dragLeaveEvent(self, event):
        super(ChatSessionListView, self).dragLeaveEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def dragMoveEvent(self, event):
        super(ChatSessionListView, self).dragMoveEvent(event)
        model = self.model()
        for mime_type in model.accepted_mime_types:
            if event.provides(mime_type):
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                self.drop_indicator_index = QModelIndex()
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                item = index.data(Qt.UserRole)
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                handler(event, index, rect, item)
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if event.source() is self:
            event.setDropAction(Qt.MoveAction)
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        super(ChatSessionListView, self).dropEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def _DH_ApplicationXBlinkContactList(self, event, index, rect, item):
        event.accept(rect)

    def _DH_TextUriList(self, event, index, rect, item):
        model = self.model()
        if not index.isValid():
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.sessions)-1)).bottom())
        event.accept(rect)
        self.drop_indicator_index = index

    def _SH_AnimationFinished(self):
        if self.animation.direction() == QPropertyAnimation.Forward:
            self.setFocus(Qt.OtherFocusReason)
        else:
            self.hide()
            current_tab = self.chat_window.tab_widget.currentWidget()
            current_tab.chat_input.setFocus(Qt.OtherFocusReason)

    def _SH_DoubleClicked(self, index):
        self.animation.setDirection(QPropertyAnimation.Backward)
        self.animation.start()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkActiveSessionDidChange(self, notification):
        self.ignore_selection_changes = True
        selection_model = self.selectionModel()
        if notification.data.active_session is None:
            selection = selection_model.selection()
            # check the code in this if branch if it's needed -Dan (if not also remove previous_active_session maybe)
            #selected_blink_session = selection[0].topLeft().data(Qt.UserRole).blink_session if selection else None
            #if notification.data.previous_active_session is selected_blink_session:
            #    print "-- chat session list updating selection to None None"
            #    selection_model.clearSelection()
        else:
            model = self.model()
            position = model.sessions.index(notification.data.active_session.items.chat)
            #print "-- chat session list updating selection to", position, notification.data.active_session
            selection_model.select(model.index(position), selection_model.ClearAndSelect)
        self.ignore_selection_changes = False


# Session management
#

ui_class, base_class = uic.loadUiType(Resources.get('incoming_dialog.ui'))

class IncomingDialog(base_class, ui_class):
    def __init__(self, parent=None):
        super(IncomingDialog, self).__init__(parent)
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
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
        self.screensharing_stream.hidden.connect(self.screensharing_label.hide)
        self.screensharing_stream.shown.connect(self.screensharing_label.show)
        for stream in self.streams:
            stream.hide()
        self.position = None

    def show(self, activate=True, position=1):
        blink = QApplication.instance()
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
        return (self.audio_stream, self.chat_stream, self.screensharing_stream, self.video_stream)

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
            self.screensharing_stream.active = True
            self.video_stream.active = True
            self.note_label.setText(u'To refuse a stream click its icon')
        else:
            self.audio_stream.active = False
            self.chat_stream.active = False
            self.screensharing_stream.active = False
            self.video_stream.active = False
            if self.audio_stream.in_use:
                self.note_label.setText(u'Audio call')
            elif self.chat_stream.in_use:
                self.note_label.setText(u'Chat session')
            elif self.video_stream.in_use:
                self.note_label.setText(u'Video call')
            elif self.screensharing_stream.in_use:
                self.note_label.setText(u'Screen sharing request')
            else:
                self.note_label.setText(u'')
        self._update_accept_button()

del ui_class, base_class


class IncomingRequest(QObject):
    accepted = pyqtSignal(object)
    rejected = pyqtSignal(object, str)

    def __init__(self, dialog, session, contact, contact_uri, proposal=False, audio_stream=None, video_stream=None, chat_stream=None, screensharing_stream=None):
        super(IncomingRequest, self).__init__()
        self.dialog = dialog
        self.session = session
        self.contact = contact
        self.contact_uri = contact_uri
        self.proposal = proposal
        self.audio_stream = audio_stream
        self.video_stream = video_stream
        self.chat_stream = chat_stream
        self.screensharing_stream = screensharing_stream

        if proposal:
            self.dialog.setWindowTitle(u'Incoming Session Update')
            self.dialog.setWindowIconText(u'Incoming Session Update')
            self.dialog.busy_button.hide()
        else:
            self.dialog.setWindowTitle(u'Incoming Session Request')
            self.dialog.setWindowIconText(u'Incoming Session Request')
        address = u'%s@%s' % (session.remote_identity.uri.user, session.remote_identity.uri.host)
        self.dialog.uri_label.setText(address)
        self.dialog.username_label.setText(contact.name or session.remote_identity.display_name or address)
        if contact.pixmap:
            self.dialog.user_icon.setPixmap(contact.pixmap)
        if self.audio_stream:
            self.dialog.audio_stream.show()
        if self.video_stream:
            self.dialog.video_stream.show()
        if self.chat_stream:
            self.dialog.chat_stream.show()
        if self.screensharing_stream:
            if self.screensharing_stream.handler.type == 'active':
                self.dialog.screensharing_label.setText(u'is offering to share his screen')
            else:
                self.dialog.screensharing_label.setText(u'is asking to share your screen')
            self.dialog.screensharing_stream.accepted = False # Remove when implemented later -Luci
            self.dialog.screensharing_stream.show()
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
        if self.screensharing_accepted:
            streams.append(self.screensharing_stream)
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
    def screensharing_accepted(self):
        return self.dialog.screensharing_stream.in_use and self.dialog.screensharing_stream.accepted

    @property
    def priority(self):
        if self.audio_stream:
            return 0
        elif self.video_stream:
            return 1
        elif self.screensharing_stream:
            return 2
        elif self.chat_stream:
            return 3
        else:
            return 4

    def _SH_DialogAccepted(self):
        self.accepted.emit(self)

    def _SH_DialogRejected(self):
        self.rejected.emit(self, self.dialog.reject_mode)


ui_class, base_class = uic.loadUiType(Resources.get('conference_dialog.ui'))

class ConferenceDialog(base_class, ui_class):
    def __init__(self, parent=None):
        super(ConferenceDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.audio_button.clicked.connect(self._SH_MediaButtonClicked)
        self.chat_button.clicked.connect(self._SH_MediaButtonClicked)
        self.room_button.editTextChanged.connect(self._SH_RoomButtonEditTextChanged)
        self.accepted.connect(self.join_conference)

    def _SH_MediaButtonClicked(self, checked):
        self.accept_button.setEnabled(self.room_button.currentText() != u'' and any(button.isChecked() for button in (self.audio_button, self.chat_button)))

    def _SH_RoomButtonEditTextChanged(self, text):
        self.accept_button.setEnabled(text != u'' and any(button.isChecked() for button in (self.audio_button, self.chat_button)))

    def show(self):
        self.room_button.setCurrentIndex(-1)
        self.audio_button.setChecked(True)
        self.chat_button.setChecked(True)
        self.accept_button.setEnabled(False)
        super(ConferenceDialog, self).show()

    def join_conference(self):
        from blink.contacts import URIUtils

        account_manager = AccountManager()
        session_manager = SessionManager()
        account = account_manager.default_account
        if account is not BonjourAccount():
            conference_uri = u'%s@%s' % (self.room_button.currentText(), account.server.conference_server or 'conference.sip2sip.info')
        else:
            conference_uri = u'%s@%s' % (self.room_button.currentText(), 'conference.sip2sip.info')
        contact, contact_uri = URIUtils.find_contact(conference_uri, display_name='Conference')
        streams = []
        if self.audio_button.isChecked():
            streams.append(StreamDescription('audio'))
        if self.chat_button.isChecked():
            streams.append(StreamDescription('chat'))
        session_manager.create_session(contact, contact_uri, streams, account=account)

del ui_class, base_class


class RingtoneDescriptor(object):
    def __init__(self):
        self.values = weakobjectmap()

    def __get__(self, obj, objtype):
        if obj is None:
            return self
        return self.values[obj]

    def __set__(self, obj, ringtone): # review this again -Dan
        old_ringtone = self.values.get(obj, Null)
        if ringtone is not Null and ringtone.type == old_ringtone.type:
            return
        old_ringtone.stop()
        old_ringtone.bridge.remove(old_ringtone)
        ringtone.bridge.add(ringtone)
        ringtone.start()
        self.values[obj] = ringtone

    def __delete__(self, obj):
        raise AttributeError("Attribute cannot be deleted")


class SessionManager(object):
    __metaclass__ = Singleton

    implements(IObserver)

    class PrimaryRingtone:   __metaclass__ = MarkerType
    class SecondaryRingtone: __metaclass__ = MarkerType

    inbound_ringtone  = RingtoneDescriptor()
    outbound_ringtone = RingtoneDescriptor()
    hold_tone         = RingtoneDescriptor()
    # have the hangup tone also a descriptor that is not reset to Null when it ends playing, but after the cooldown period -Dan

    def __init__(self):
        self.sessions = []
        self.incoming_requests = []
        self.dialog_positions = range(1, 100)
        self.last_dialed_uri = None
        self.active_session = None

        self.inbound_ringtone = Null
        self.outbound_ringtone = Null
        self.hold_tone = Null

        self._hangup_tone_timer = QTimer() # we should consider replacing this with a timestamp -Dan
        self._hangup_tone_timer.setInterval(1000)
        self._hangup_tone_timer.setSingleShot(True)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPSessionNewIncoming')
        notification_center.add_observer(self, name='SIPSessionDidFail')
        notification_center.add_observer(self, name='SIPSessionProposalRejected')
        notification_center.add_observer(self, name='SIPSessionHadProposalFailure')

        notification_center.add_observer(self, name='BlinkSessionNewIncoming')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForIncoming')
        notification_center.add_observer(self, name='BlinkSessionDidEnd')
        notification_center.add_observer(self, name='BlinkSessionWasDeleted')
        notification_center.add_observer(self, name='BlinkSessionDidChangeState')
        notification_center.add_observer(self, name='BlinkSessionDidChangeHoldState')

        notification_center.add_observer(self, name='BlinkSessionListSelectionChanged')

    def create_session(self, contact, contact_uri, streams, account=None, connect=True, sibling=None):
        if account is None:
            if contact.type == 'bonjour':
                account = BonjourAccount()
            else:
                account = AccountManager().default_account

        assert account is not None

        try:
            session = next(session for session in self.sessions if session.reusable and session.contact.settings is contact.settings)
            reinitialize = True
        except StopIteration:
            session = BlinkSession()
            self.sessions.append(session)
            reinitialize = False

        session.init_outgoing(account, contact, contact_uri, streams, sibling=sibling, reinitialize=reinitialize)
        self.last_dialed_uri = session.uri
        if connect:
            session.connect()

        return session

    def update_ringtone(self):
        # Outgoing ringtone
        outgoing_sessions_or_proposals = [session for session in self.sessions if session.state=='connecting/ringing' and session.direction=='outgoing' or session.state=='connected/sent_proposal']
        if any(not session.on_hold for session in outgoing_sessions_or_proposals):
            settings = SIPSimpleSettings()
            outbound_ringtone = settings.sounds.outbound_ringtone
            if outbound_ringtone:
                if any('audio' in session.streams and not session.on_hold for session in outgoing_sessions_or_proposals):
                    ringtone_path = outbound_ringtone.path
                    ringtone_type = self.PrimaryRingtone
                else:
                    ringtone_path = Resources.get('sounds/beeping_ringtone.wav')
                    ringtone_type = self.SecondaryRingtone
                outbound_ringtone = WavePlayer(SIPApplication.voice_audio_mixer, ringtone_path, outbound_ringtone.volume, loop_count=0, pause_time=5)
                outbound_ringtone.bridge = SIPApplication.voice_audio_bridge
                outbound_ringtone.type = ringtone_type
            else:
                outbound_ringtone = Null
        else:
            outbound_ringtone = Null

        if outbound_ringtone.type is self.PrimaryRingtone and self.inbound_ringtone.type is self.PrimaryRingtone:
            self.inbound_ringtone = Null
        if outbound_ringtone is not Null and self.hold_tone is not Null:
            self.hold_tone = Null
        self.outbound_ringtone = outbound_ringtone

        # Incoming ringtone
        if self.incoming_requests:
            try:
                request = next(req for req in self.incoming_requests if req.audio_stream or req.video_stream)
                ringtone_type = self.PrimaryRingtone
            except StopIteration:
                request = self.incoming_requests[0]
                ringtone_type = self.SecondaryRingtone

            if self.active_session is not None and self.active_session.state in ('connecting/ringing', 'connected/*'):
                ringtone_type = self.SecondaryRingtone
                initial_delay = 1 # have a small delay to avoid sounds overlapping
            else:
                initial_delay = 0

            settings = SIPSimpleSettings()
            sound_file = request.session.account.sounds.inbound_ringtone or settings.sounds.inbound_ringtone
            if sound_file:
                if ringtone_type is self.PrimaryRingtone:
                    ringtone_path = sound_file.path
                else:
                    ringtone_path = Resources.get('sounds/beeping_ringtone.wav')
                inbound_ringtone = WavePlayer(SIPApplication.alert_audio_mixer, ringtone_path, volume=sound_file.volume, loop_count=0, pause_time=3, initial_delay=initial_delay)
                inbound_ringtone.bridge = SIPApplication.alert_audio_bridge
                inbound_ringtone.type = ringtone_type
            else:
                inbound_ringtone = Null
        else:
            inbound_ringtone = Null

        if inbound_ringtone is not Null and self.hold_tone is not Null:
            self.hold_tone = Null
        self.inbound_ringtone = inbound_ringtone

        # Hold tone
        # we need to beep every 15 seconds only when we put all calls on hold. If all are on hold but not all by local, we ring at 45 seconds -Dan
        connected_sessions = [session for session in self.sessions if session.state=='connected/*']
        connected_on_hold_sessions = [session for session in connected_sessions if session.on_hold]
        if self.outbound_ringtone is Null and self.inbound_ringtone is Null and connected_sessions:
            if len(connected_sessions) == len(connected_on_hold_sessions):
                hold_tone = WavePlayer(SIPApplication.alert_audio_mixer, Resources.get('sounds/hold_tone.wav'), loop_count=0, pause_time=15, volume=30, initial_delay=15)
                hold_tone.bridge = SIPApplication.alert_audio_bridge
                hold_tone.type = None
            elif len(connected_on_hold_sessions) > 0:
                hold_tone = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get('sounds/hold_tone.wav'), loop_count=0, pause_time=45, volume=30, initial_delay=15)
                hold_tone.bridge = SIPApplication.voice_audio_bridge
                hold_tone.type = None
            else:
                hold_tone = Null
        else:
            hold_tone = Null
        self.hold_tone = hold_tone

    def _process_remote_proposal(self, blink_session):
        sip_session = blink_session.sip_session

        current_stream_types = set(stream.type for stream in sip_session.streams)
        stream_map = defaultdict(list)
        # TODO: we should fetch the proposed streams from the BlinkSession -Saul
        for stream in (stream for stream in sip_session.proposed_streams if stream.type not in current_stream_types):
            stream_map[stream.type].append(stream)
        proposed_stream_types = set(stream_map)

        audio_streams = stream_map['audio']
        video_streams = stream_map['video']
        chat_streams = stream_map['chat']
        screensharing_streams = stream_map['screen-sharing']

        if not proposed_stream_types or proposed_stream_types == {'file-transfer'}:
            session.reject_proposal(488)
            return

        if proposed_stream_types == {'chat'}:
            blink_session.accept_proposal([chat_streams[0]])
            return

        sip_session.send_ring_indication()

        contact = blink_session.contact
        contact_uri = blink_session.contact_uri

        audio_stream = audio_streams[0] if audio_streams else None
        video_stream = video_streams[0] if video_streams else None
        chat_stream = chat_streams[0] if chat_streams else None
        screensharing_stream = screensharing_streams[0] if screensharing_streams else None

        dialog = IncomingDialog() # The dialog is constructed without the main window as parent so that on Linux it is displayed on the current workspace rather than the one where the main window is.
        incoming_request = IncomingRequest(dialog, sip_session, contact, contact_uri, proposal=True, audio_stream=audio_stream, video_stream=video_stream, chat_stream=chat_stream, screensharing_stream=screensharing_stream)
        bisect.insort_right(self.incoming_requests, incoming_request)
        incoming_request.accepted.connect(self._SH_IncomingRequestAccepted)
        incoming_request.rejected.connect(self._SH_IncomingRequestRejected)
        try:
            position = self.dialog_positions.pop(0)
        except IndexError:
            position = None
        incoming_request.dialog.show(activate=QApplication.activeWindow() is not None and self.incoming_requests.index(incoming_request)==0, position=position)

    def _SH_IncomingRequestAccepted(self, incoming_request):
        if incoming_request.dialog.position is not None:
            bisect.insort_left(self.dialog_positions, incoming_request.dialog.position)
        self.incoming_requests.remove(incoming_request)
        self.update_ringtone()
        accepted_streams = incoming_request.accepted_streams
        if incoming_request.proposal:
            blink_session = next(session for session in self.sessions if session.sip_session is incoming_request.session)
            blink_session.accept_proposal(accepted_streams)
        else:
            try:
                blink_session = next(session for session in self.sessions if session.reusable and session.contact.settings is incoming_request.contact.settings)
                reinitialize = True
            except StopIteration:
                blink_session = BlinkSession()
                self.sessions.append(blink_session)
                reinitialize = False
            blink_session.init_incoming(incoming_request.session, accepted_streams, incoming_request.contact, incoming_request.contact_uri, reinitialize=reinitialize)

    def _SH_IncomingRequestRejected(self, incoming_request, mode):
        if incoming_request.dialog.position is not None:
            bisect.insort_left(self.dialog_positions, incoming_request.dialog.position)
        self.incoming_requests.remove(incoming_request)
        self.update_ringtone()
        if incoming_request.proposal:
            incoming_request.session.reject_proposal(488)
        elif mode == 'busy':
            incoming_request.session.reject(486)
        elif mode == 'reject':
            incoming_request.session.reject(603)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionNewIncoming(self, notification):
        from blink.contacts import URIUtils

        session = notification.sender

        stream_map = defaultdict(list)
        for stream in notification.data.streams:
            stream_map[stream.type].append(stream)

        audio_streams = stream_map['audio']
        video_streams = stream_map['video']
        chat_streams = stream_map['chat']
        screensharing_streams = stream_map['screen-sharing']
        filetransfer_streams = stream_map['file-transfer']

        if not audio_streams and not video_streams and not chat_streams and not screensharing_streams and not filetransfer_streams:
            session.reject(488)
            return
        if filetransfer_streams and not (audio_streams or video_streams or chat_streams or screensharing_streams):
            # TODO: add support for this with different type of session -Saul
            session.reject(488)
            return

        session.send_ring_indication()

        contact, contact_uri = URIUtils.find_contact(session.remote_identity.uri, display_name=session.remote_identity.display_name, exact=False)

        audio_stream = audio_streams[0] if audio_streams else None
        video_stream = video_streams[0] if video_streams else None
        chat_stream = chat_streams[0] if chat_streams else None
        screensharing_stream = screensharing_streams[0] if screensharing_streams else None

        dialog = IncomingDialog() # The dialog is constructed without the main window as parent so that on Linux it is displayed on the current workspace rather than the one where the main window is.
        incoming_request = IncomingRequest(dialog, session, contact, contact_uri, proposal=False, audio_stream=audio_stream, video_stream=video_stream, chat_stream=chat_stream, screensharing_stream=screensharing_stream)
        bisect.insort_right(self.incoming_requests, incoming_request)
        incoming_request.accepted.connect(self._SH_IncomingRequestAccepted)
        incoming_request.rejected.connect(self._SH_IncomingRequestRejected)
        try:
            position = self.dialog_positions.pop(0)
        except IndexError:
            position = None
        incoming_request.dialog.show(activate=QApplication.activeWindow() is not None and self.incoming_requests.index(incoming_request)==0, position=position)
        self.update_ringtone()

    def _NH_SIPSessionDidFail(self, notification):
        try:
            incoming_request = next(incoming_request for incoming_request in self.incoming_requests if incoming_request.session is notification.sender)
        except StopIteration:
            return
        if incoming_request.dialog.position is not None:
            bisect.insort_left(self.dialog_positions, incoming_request.dialog.position)
        incoming_request.dialog.hide()
        self.incoming_requests.remove(incoming_request)
        self.update_ringtone()

    def _NH_SIPSessionProposalRejected(self, notification):
        try:
            incoming_request = next(incoming_request for incoming_request in self.incoming_requests if incoming_request.session is notification.sender)
        except StopIteration:
            return
        if incoming_request.dialog.position is not None:
            bisect.insort_left(self.dialog_positions, incoming_request.dialog.position)
        incoming_request.dialog.hide()
        self.incoming_requests.remove(incoming_request)
        self.update_ringtone()

    def _NH_SIPSessionHadProposalFailure(self, notification):
        try:
            incoming_request = next(incoming_request for incoming_request in self.incoming_requests if incoming_request.session is notification.sender)
        except StopIteration:
            return
        if incoming_request.dialog.position is not None:
            bisect.insort_left(self.dialog_positions, incoming_request.dialog.position)
        incoming_request.dialog.hide()
        self.incoming_requests.remove(incoming_request)
        self.update_ringtone()

    def _NH_BlinkSessionDidChangeState(self, notification):
        new_state = notification.data.new_state
        if new_state == 'connected/received_proposal':
            self._process_remote_proposal(notification.sender)
        if new_state in ('connecting/ringing', 'connecting/early_media', 'connected/*'):
            self.update_ringtone()
        elif new_state == 'ending':
            notification.sender._play_hangup_tone = notification.data.old_state in ('connecting/*', 'connected/*')

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        if notification.data.remote_hold and not notification.data.local_hold: # check if this could be integrated in update_ringtone -Dan
            player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get('sounds/hold_tone.wav'), loop_count=1, volume=30)
            SIPApplication.voice_audio_bridge.add(player)
            player.start()
        self.update_ringtone()

    def _NH_BlinkSessionNewIncoming(self, notification):
        self.update_ringtone()

    def _NH_BlinkSessionDidReinitializeForIncoming(self, notification):
        self.update_ringtone()

    def _NH_BlinkSessionDidEnd(self, notification):
        self.update_ringtone()
        if notification.sender._play_hangup_tone and not self._hangup_tone_timer.isActive():
            self._hangup_tone_timer.start()
            player = WavePlayer(SIPApplication.voice_audio_bridge.mixer, Resources.get('sounds/hangup_tone.wav'), volume=60)
            SIPApplication.voice_audio_bridge.add(player)
            player.start()

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.sessions.remove(notification.sender)

    def _NH_BlinkSessionListSelectionChanged(self, notification):
        selected_session = notification.data.selected_session
        deselected_session = notification.data.deselected_session
        old_active_session = self.active_session

        if selected_session is self.active_session: # both None or both the same session. nothing to do in either case.
            return
        elif selected_session is None and deselected_session is old_active_session is not None:
            self.active_session = None
            sessions = deselected_session.client_conference.sessions if deselected_session.client_conference is not None else [deselected_session]
            for session in sessions:
                session.active = False
            notification.center.post_notification('BlinkActiveSessionDidChange', sender=self, data=NotificationData(previous_active_session=old_active_session, active_session=None))
        elif selected_session is not None and selected_session.state in ('connecting/*', 'connected/*') and selected_session.streams.types.intersection({'audio', 'video'}):
            old_active_session = old_active_session or Null
            new_active_session = selected_session
            if old_active_session.client_conference is not None and old_active_session.client_conference is not new_active_session.client_conference:
                for session in old_active_session.client_conference.sessions:
                    session.active = False
            elif old_active_session.client_conference is None:
                old_active_session.active = False
            if new_active_session.client_conference is not None and new_active_session.client_conference is not old_active_session.client_conference:
                for session in new_active_session.client_conference.sessions:
                    session.active = True
            elif new_active_session.client_conference is None:
                new_active_session.active = True
            self.active_session = selected_session
            notification.center.post_notification('BlinkActiveSessionDidChange', sender=self, data=NotificationData(previous_active_session=old_active_session or None, active_session=selected_session))


