# Copyright (C) 2013 AG Projects. See LICENSE for details.
#

__all__ = ['HistoryManager']

import re
import cPickle as pickle

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from collections import deque
from datetime import datetime
from zope.interface import implements

from sipsimple.account import BonjourAccount
from sipsimple.addressbook import AddressbookManager
from sipsimple.threading import run_in_thread

from blink.resources import ApplicationData
from blink.util import run_in_gui_thread


class HistoryManager(object):
    __metaclass__ = Singleton
    implements(IObserver)

    def __init__(self):
        try:
            data = pickle.load(open(ApplicationData.get('calls_history')))
        except Exception:
            self.missed_calls = deque(maxlen=10)
            self.placed_calls = deque(maxlen=10)
            self.received_calls = deque(maxlen=10)
        else:
            self.missed_calls, self.placed_calls, self.received_calls = data
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPSessionDidEnd')
        notification_center.add_observer(self, name='SIPSessionDidFail')

    @run_in_thread('file-io')
    def save(self):
        with open(ApplicationData.get('calls_history'), 'wb+') as f:
            pickle.dump((self.missed_calls, self.placed_calls, self.received_calls), f)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidEnd(self, notification):
        if notification.sender.account is BonjourAccount():
            return
        session = notification.sender
        entry = HistoryEntry.from_session(session)
        if session.direction == 'incoming':
            self.received_calls.append(entry)
        else:
            self.placed_calls.append(entry)
        self.save()

    def _NH_SIPSessionDidFail(self, notification):
        if notification.sender.account is BonjourAccount():
            return
        session = notification.sender
        entry = HistoryEntry.from_session(session)
        if session.direction == 'incoming':
            if notification.data.code == 487 and notification.data.failure_reason == 'Call completed elsewhere':
                self.received_calls.append(entry)
            else:
                self.missed_calls.append(entry)
        else:
            if notification.data.code == 487:
                entry.reason = 'cancelled'
            else:
                entry.reason = '%s (%s)' % (notification.data.reason or notification.data.failure_reason, notification.data.code)
            self.placed_calls.append(entry)
        self.save()


class HistoryEntry(object):
    phone_number_re = re.compile(r'^(?P<number>(0|00|\+)[1-9]\d{7,14})@')

    def __init__(self, remote_identity, target_uri, account_id, call_time, duration, reason=None):
        self.remote_identity = remote_identity
        self.target_uri = target_uri
        self.account_id = account_id
        self.call_time = call_time
        self.duration = duration
        self.reason = reason

    @classmethod
    def from_session(cls, session):
        if session.start_time is None and session.end_time is not None:
            # Session may have anded before it fully started
            session.start_time = session.end_time
        call_time = session.start_time or datetime.now()
        if session.start_time and session.end_time:
            duration = session.end_time - session.start_time
        else:
            duration = None
        remote_identity = session.remote_identity
        remote_uri_str = '%s@%s' % (remote_identity.uri.user, remote_identity.uri.host)
        try:
            contact = next(contact for contact in AddressbookManager().get_contacts() if remote_uri_str in (addr.uri for addr in contact.uris))
        except StopIteration:
            display_name = remote_identity.display_name
        else:
            display_name = contact.name
        match = self.phone_number_re.match(remote_uri_str)
        if match:
            remote_uri_str = match.group('number')
        if display_name and display_name != remote_uri_str:
            remote_identity_str = '%s <%s>' % (display_name, remote_uri_str)
        else:
            remote_identity_str = remote_uri_str
        return cls(remote_identity_str, remote_uri_str, unicode(session.account.id), call_time, duration)

    def __unicode__(self):
        if self.call_time:
            time = ' %s' % format_date(self.call_time)
        else:
            time = ''
        if self.duration:
            duration = ' for '
            if self.duration.days > 0 or self.duration.seconds > 3600:
                duration += '%i hours, ' % (self.duration.days*3600*24 + int(self.duration.seconds/3600))
            secs = self.duration.seconds % 3600
            duration += '%02i:%02i' % (int(secs/60), secs % 60)
        else:
            duration = ''
        reason = ' %s' % self.reason.title() if self.reason else ''
        return u'%s%s%s%s' % (self.remote_identity, time, duration, reason)


def format_date(dt):
    now = datetime.now()
    delta = now - dt
    if (dt.year, dt.month, dt.day) == (now.year, now.month, now.day):
        return dt.strftime("at %H:%M")
    elif delta.days <= 1:
        return "Yesterday at %s" % dt.strftime("%H:%M")
    elif delta.days < 7:
        return dt.strftime("on %A")
    elif delta.days < 300:
        return dt.strftime("on %B %d")
    else:
        return dt.strftime("on %Y-%m-%d")

