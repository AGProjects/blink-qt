
import os
import sys

from collections import deque
from datetime import datetime
from pprint import pformat

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData, ObserverWeakrefProxy
from application.python.queue import EventQueue
from application.python import Null
from application.python.types import Singleton
from application.system import makedirs
from zope.interface import implementer

from sipsimple.configuration.settings import SIPSimpleSettings

from blink.resources import ApplicationData


__all__ = ['LogManager', 'MessagingTrace']


@implementer(IObserver)
class NotificationQueue(object):

    def __init__(self):
        self.notifications = deque()
        NotificationCenter().add_observer(ObserverWeakrefProxy(self))

    def handle_notification(self, notification):
        self.notifications.append(notification)


class LogFile(object):
    def __init__(self, filename):
        self.filename = filename

    def _get_filename(self):
        return self.__dict__['filename']

    def _set_filename(self, filename):
        if filename == self.__dict__.get('filename'):
            return
        old_file = self.__dict__.pop('file', Null)
        old_file.close()
        self.__dict__['filename'] = filename

    filename = property(_get_filename, _set_filename)
    del _get_filename, _set_filename

    @property
    def file(self):
        if 'file' not in self.__dict__:
            directory = os.path.dirname(self.filename)
            makedirs(directory)
            self.__dict__['file'] = open(self.filename, 'a')
        return self.__dict__['file']

    def write(self, string):
        self.file.write(string)

    def flush(self):
        file = self.__dict__.get('file', Null)
        file.flush()

    def close(self):
        file = self.__dict__.get('file', Null)
        file.close()


@implementer(IObserver)
class LogManager(object, metaclass=Singleton):

    def __init__(self):
        self.name = os.path.basename(sys.argv[0]).rsplit('.py', 1)[0]
        self.pid = os.getpid()
        self.msrp_level = log.level.INFO
        self.siptrace_file = Null
        self.massagestrace_file = Null
        self.msrptrace_file = Null
        self.pjsiptrace_file = Null
        self.notifications_file = Null
        self.xcaptrace_file = Null
        self.event_queue = Null
        self.notification_queue = NotificationQueue()
        self._siptrace_start_time = None
        self._siptrace_packet_count = None

    def start(self):
        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()
        notification_center.add_observer(self)
        if settings.logs.trace_sip:
            self.siptrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'sip_trace.txt'))
        if settings.logs.trace_messaging:
            self.messagingtrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'messaging_trace.txt'))
        if settings.logs.trace_msrp:
            self.msrptrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'msrp_trace.txt'))
        if settings.logs.trace_pjsip:
            self.pjsiptrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'pjsip_trace.txt'))
        if settings.logs.trace_notifications:
            self.notifications_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'notifications_trace.txt'))
        if settings.logs.trace_xcap:
            self.xcaptrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'xcap_trace.txt'))
        self._siptrace_start_time = datetime.now()
        self._siptrace_packet_count = 0
        self.event_queue = EventQueue(handler=self._process_notification, name='Blink LogManager')
        self.event_queue.start()
        while settings.logs.trace_notifications and self.notification_queue and self.notification_queue.notifications:
            notification = self.notification_queue.notifications.popleft()
            self.handle_notification(notification)
        self.notification_queue = None

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self)

        self.event_queue.stop()
        self.event_queue.join()
        self.event_queue = Null

        self.siptrace_file = Null
        self.massagestrace_file = Null
        self.msrptrace_file = Null
        self.pjsiptrace_file = Null
        self.notifications_file = Null
        self.xcaptrace_file = Null

    def handle_notification(self, notification):
        self.event_queue.put(notification)

    def _process_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

        handler = getattr(self, '_LH_%s' % notification.name, Null)
        handler(notification)

        settings = SIPSimpleSettings()
        if notification.name not in ('SIPEngineLog', 'SIPEngineSIPTrace', 'MessagingTrace') and settings.logs.trace_notifications:
            message = 'Notification name=%s sender=%s data=%s' % (notification.name, notification.sender, pformat(notification.data))
            msg = '%s [%s %d]: %s\n' % (datetime.now(), self.name, self.pid, message)
            try:
                self.notifications_file.write(msg)
                self.notifications_file.flush()
            except Exception:
                pass

            NotificationCenter().post_notification('UILogNotifications', data=msg)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.sender is settings:
            if 'logs.trace_sip' in notification.data.modified:
                self.siptrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'sip_trace.txt')) if settings.logs.trace_sip else Null
            if 'logs.trace_messaging' in notification.data.modified:
                self.messagingtrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'messaging_trace.txt')) if settings.logs.trace_messaging else Null
            if 'logs.trace_msrp' in notification.data.modified:
                self.msrptrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'msrp_trace.txt')) if settings.logs.trace_msrp else Null
            if 'logs.trace_pjsip' in notification.data.modified:
                self.pjsiptrace_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'pjsip_trace.txt')) if settings.logs.trace_pjsip else Null
            if 'logs.trace_notifications' in notification.data.modified:
                self.notifications_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'notifications_trace.txt')) if settings.logs.trace_notifications else Null
            if 'logs.trace_xcap' in notification.data.modified:
                self.notifications_file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'xcap_trace.txt')) if settings.logs.trace_xcap else Null

    def _LH_SIPEngineSIPTrace(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_sip:
            return
        self._siptrace_packet_count += 1
        if notification.data.received:
            direction = "RECEIVED"
        else:
            direction = "SENDING"
        data = notification.data.data.decode() if isinstance(notification.data.data, bytes) else notification.data.data
        buf = ["%s: Packet %d, +%s" % (direction, self._siptrace_packet_count, (notification.datetime - self._siptrace_start_time)),
               "%(source_ip)s:%(source_port)d -(SIP over %(transport)s)-> %(destination_ip)s:%(destination_port)d" % notification.data.__dict__,
               data,
               '--']
        message = '\n'.join(buf)
        msg = '%s [%s %d]: %s\n' % (notification.datetime, self.name, self.pid, message)
        try:
            self.siptrace_file.write(msg)
            self.siptrace_file.flush()
        except Exception:
            pass

        NotificationCenter().post_notification('UILogSip', data=msg)

    def _LH_SIPEngineLog(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_pjsip:
            return
        message = "(%(level)d) %(message)s" % notification.data.__dict__
        msg = '[%s %d] %s\n' % (self.name, self.pid, message)
        try:
            self.pjsiptrace_file.write(msg)
            self.pjsiptrace_file.flush()
        except Exception:
            pass

        NotificationCenter().post_notification('UILogPjsip', data=msg)

    def _LH_DNSLookupTrace(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_sip:
            return
        message = 'DNS lookup %(query_type)s %(query_name)s' % notification.data.__dict__
        if notification.data.error is None:
            message += ' succeeded, ttl=%d: ' % notification.data.answer.ttl
            if notification.data.query_type == 'A':
                message += ", ".join(record.address for record in notification.data.answer)
            elif notification.data.query_type == 'SRV':
                message += ", ".join('%d %d %d %s' % (record.priority, record.weight, record.port, record.target) for record in notification.data.answer)
            elif notification.data.query_type == 'NAPTR':
                message += ", ".join('%d %d "%s" "%s" "%s" %s' % (record.order, record.preference, record.flags, record.service, record.regexp, record.replacement) for record in notification.data.answer)
        else:
            import dns.resolver
            message_map = {dns.resolver.NXDOMAIN: 'DNS record does not exist',
                           dns.resolver.NoAnswer: 'DNS response contains no answer',
                           dns.resolver.NoNameservers: 'no DNS name servers could be reached',
                           dns.resolver.Timeout: 'no DNS response received, the query has timed out'}
            message += ' failed: %s' % message_map.get(notification.data.error.__class__, '')

        msg = '%s [%s %d]: %s\n' % (notification.datetime, self.name, self.pid, message)
        try:
            self.siptrace_file.write(msg)
            self.siptrace_file.flush()
        except Exception:
            pass

        NotificationCenter().post_notification('UILogSip', data=msg)

    def _LH_MessagingTrace(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_messaging:
            return
        message = "(%(level)s) %(message)s" % notification.data.__dict__
        msg = '%s [%s %d] %s\n' % (notification.datetime, self.name, self.pid, message)
        try:
            self.messagingtrace_file.write(msg)
            self.messagingtrace_file.flush()
        except Exception:
            pass

        NotificationCenter().post_notification('UILogMessaging', data=msg)

    def _LH_MSRPTransportTrace(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_msrp:
            return
        arrow = {'incoming': '<--', 'outgoing': '-->'}[notification.data.direction]
        local_address = '%s:%d' % (notification.data.local_address.host, notification.data.local_address.port)
        remote_address = '%s:%d' % (notification.data.remote_address.host, notification.data.remote_address.port)
        message = '%s %s %s\n' % (local_address, arrow, remote_address) + notification.data.data
        prefix = '[Illegal request!] ' if notification.data.illegal else ''
        msg = '%s [%s %d]: %s%s\n' % (notification.datetime, self.name, self.pid, prefix, message)
        try:
            self.msrptrace_file.write(msg)
            self.msrptrace_file.flush()
        except Exception:
            pass

        NotificationCenter().post_notification('UILogMsrp', data=msg)

    def _LH_MSRPLibraryLog(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_msrp:
            return
        if notification.data.level < self.msrp_level:
            return
        message = '%s %s' % (notification.data.level, notification.data.message)
        msg = '%s [%s %d]: %s\n' % (notification.datetime, self.name, self.pid, message)

        try:
            self.msrptrace_file.write(msg)
            self.msrptrace_file.flush()
        except Exception:
            pass

        NotificationCenter().post_notification('UILogMsrp', data=msg)

    def log_xcap(self, notification, message):
        msg = '%s [%s %d]: %s\n' % (notification.datetime, self.name, self.pid, message)
        try:
            self.xcaptrace_file.write(msg)
            self.xcaptrace_file.flush()
        except Exception:
            pass

        NotificationCenter().post_notification('UILogXcap', data=msg)

    def _LH_XCAPTrace(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = None
        data = notification.data
        if data.result == 'failure':
            message = ("%s %s %s failed: %s (%s)" % (notification.datetime, data.method, data.url, data.reason, data.code))
        elif data.result == 'success':
            if data.code == 304:
                message = ("%s %s %s with etag=%s did not change (304)" % (notification.datetime, data.method, data.url, data.etag))
            else:
                message = ("%s %s %s changed to etag=%s (%d bytes)" % (notification.datetime, data.method, data.url, data.etag, data.size))
        elif data.result == 'fetch':
            message = ("%s %s %s with etag=%s" % (notification.datetime, data.method, data.url, data.etag))

        if message:
            self.log_xcap(notification, message)

    def _LH_XCAPDocumentsDidChange(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        data = notification.data
        for k in list(data.notified_etags.keys()):
            if k not in data.documents:
                pass
                #message = ("%s %s etag has changed on server to %s but is already stored locally" % (notification.datetime, data.notified_etags[k]['url'], data.notified_etags[k]['new_etag']))
            else:
                message = ("%s %s etag has changed: %s -> %s" % (notification.datetime, data.notified_etags[k]['url'], data.notified_etags[k]['new_etag'], data.notified_etags[k]['previous_etag']))
                self.log_xcap(notification, message)    

    def _LH_XCAPManagerDidDiscoverServerCapabilities(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        account = notification.sender.account
        xcap_root = notification.sender.xcap_root
        if xcap_root is None:
            # The XCAP manager might be stopped because this notification is processed in a different
            # thread from which it was posted
            return
        message = "%s Using XCAP root %s for account %s" % (notification.datetime, xcap_root, account.id)
        self.log_xcap(notification, message)
        message = ("%s XCAP server capabilities: %s" % (notification.datetime, ", ".join(notification.data.auids)))
        self.log_xcap(notification, message)    
          
    def _LH_XCAPManagerDidStart(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager of account %s started" % (notification.datetime, notification.sender.account.id))
        self.log_xcap(notification, message)
            
    def _LH_XCAPManagerDidChangeState(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager of account %s changed state from %s to %s" % (notification.datetime, notification.sender.account.id, notification.data.prev_state.capitalize(), notification.data.state.capitalize()))
        self.log_xcap(notification, message)
            
    def _LH_XCAPManagerDidAddContact(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager added contact %s" % (notification.datetime, notification.data.contact.id))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerDidUpdateContact(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager updated contact %s" % (notification.datetime, notification.data.contact.id))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerDidRemoveContact(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager removed contact %s" % (notification.datetime, notification.data.contact.id))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerDidAddGroup(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager added group %s" % (notification.datetime, notification.data.group.id))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerDidUpdateGroup(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager updated group %s" % (notification.datetime, notification.data.group.id))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerDidRemoveGroup(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager removed group %s" % (notification.datetime, notification.data.group.id))
        self.log_xcap(notification, message)

    def _LH_XCAPManageDidAddGroupMember(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager added member %s to group %s" % (notification.datetime,  notification.data.contact.id, notification.data.group.id))
        self.log_xcap(notification, message)

    def _LH_XCAPManageDidRemoveGroupMember(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager removed member %s from group %s" % (notification.datetime, notification.data.contact.id, notification.data.group.id))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerClientWillInitialize(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager client will initialized for XCAP root %s" % (notification.datetime, notification.data.root))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerDidInitialize(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager initialized with XCAP client %s" % (notification.datetime, notification.data.client))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerClientDidInitialize(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager client %s initialized for XCAP root %s" % (notification.datetime, notification.data.client, notification.data.root))
        self.log_xcap(notification, message)

    def _LH_XCAPManagerClientDidNotInitialize(self, notification):
        settings = SIPSimpleSettings()
        if not settings.logs.trace_xcap:
            return

        message = ("%s XCAP manager client did not initialize: %s" % (notification.datetime, notification.data.error))
        self.log_xcap(notification, message)


class MessagingTrace(object, metaclass=Singleton):
    @classmethod
    def debug(cls, message, *args, **kw):
        cls._log('DEBUG', message, *args, **kw)

    @classmethod
    def info(cls, message, *args, **kw):
        cls._log('INFO', message, *args, **kw)

    @classmethod
    def warning(cls, message, *args, **kw):
        cls._log('WARNING', message, *args, **kw)

    warn = warning

    @classmethod
    def error(cls, message, *args, **kw):
        cls._log('ERROR', message, *args, **kw)

    @classmethod
    def exception(cls, message='', *args, **kw):
        cls._log('EXCEPTION', message, *args, **kw)

    @classmethod
    def critical(cls, message, *args, **kw):
        cls._log('CRITICAL', message, *args, **kw)

    fatal = critical

    @classmethod
    def _log(cls, level, message):
        try:
            level = getattr(log.level, level)
        except AttributeError:
            return
        data = NotificationData(level=level, message=message)
        notification_center = NotificationCenter()
        notification_center.post_notification('MessagingTrace', data=data)
