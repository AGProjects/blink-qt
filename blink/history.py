
import bisect
import pickle as pickle
import re

from PyQt5.QtGui import QIcon

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton

from datetime import date, timezone
from dateutil.parser import parse
from dateutil.tz import tzlocal
from zope.interface import implementer

from sipsimple.account import BonjourAccount
from sipsimple.addressbook import AddressbookManager
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.configuration.settings import BlinkSettings
from blink.resources import ApplicationData, Resources
from blink.messages import BlinkMessage
from blink.util import run_in_gui_thread
import traceback

from sqlobject import SQLObject, StringCol, DateTimeCol, IntCol, UnicodeCol, DatabaseIndex
from sqlobject import connectionForURI
from sqlobject import dberrors

__all__ = ['HistoryManager']


@implementer(IObserver)
class HistoryManager(object, metaclass=Singleton):

    history_size = 20
    sip_prefix_re = re.compile('^sips?:')

    def __init__(self):
        self.calls = []
        self.message_history = MessageHistory()

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='SIPSessionDidEnd')
        notification_center.add_observer(self, name='SIPSessionDidFail')
        notification_center.add_observer(self, name='ChatStreamGotMessage')
        notification_center.add_observer(self, name='ChatStreamWillSendMessage')
        notification_center.add_observer(self, name='ChatStreamDidSendMessage')
        notification_center.add_observer(self, name='ChatStreamDidDeliverMessage')
        notification_center.add_observer(self, name='ChatStreamDidNotDeliverMessage')
        notification_center.add_observer(self, name='BlinkMessageIsParsed')
        notification_center.add_observer(self, name='BlinkMessageIsPending')
        notification_center.add_observer(self, name='BlinkMessageDidSucceed')
        notification_center.add_observer(self, name='BlinkMessageDidFail')
        notification_center.add_observer(self, name='BlinkGotDispositionNotification')
        notification_center.add_observer(self, name='BlinkDidSendDispositionNotification')

    @run_in_thread('file-io')
    def save(self):
        with open(ApplicationData.get('calls_history'), 'wb+') as history_file:
            pickle.dump(self.calls, history_file)

    def load(self, uri, session):
        return self.message_history.load(uri, session)

    def get_last_contacts(self, number=5):
        return self.message_history.get_last_contacts(number)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        try:
            data = pickle.load(open(ApplicationData.get('calls_history'), "rb"))
            if not isinstance(data, list) or not all(isinstance(item, HistoryEntry) and item.text and isinstance(item.call_time, ISOTimestamp) for item in data):
                raise ValueError("invalid save data")
        except Exception as e:
            traceback.print_exc()
        else:
            self.calls = data[-self.history_size:]

    def _NH_SIPSessionDidEnd(self, notification):
        if notification.sender.account is BonjourAccount():
            return
        session = notification.sender
        entry = HistoryEntry.from_session(session)
        bisect.insort(self.calls, entry)
        self.calls = self.calls[-self.history_size:]
        self.save()

    def _NH_SIPSessionDidFail(self, notification):
        if notification.sender.account is BonjourAccount():
            return
        session = notification.sender
        entry = HistoryEntry.from_session(session)

        if session.direction == 'incoming':
            if notification.data.code != 487 or notification.data.failure_reason != 'Call completed elsewhere':
                entry.failed = True
        else:
            if notification.data.code == 0:
                entry.reason = 'Internal Error'
            elif notification.data.code == 487:
                entry.reason = 'Cancelled'
            else:
                entry.reason = notification.data.reason or notification.data.failure_reason
            entry.failed = True
        bisect.insort(self.calls, entry)
        self.calls = self.calls[-self.history_size:]
        self.save()

    def _NH_ChatStreamGotMessage(self, notification):
        message = notification.data.message

        if notification.sender.blink_session.remote_focus and self.sip_prefix_re.sub('', str(message.sender.uri)) not in notification.sender.blink_session.server_conference.participants:
            return

        is_status_message = any(h.name == 'Message-Type' and h.value == 'status' and h.namespace == 'urn:ag-projects:xml:ns:cpim' for h in message.additional_headers)
        if not is_status_message:
            blink_message = BlinkMessage(**{slot: getattr(message, slot) for slot in message.__slots__})
            self.message_history.add_with_session(notification.sender.blink_session, blink_message, 'incoming', 'delivered')

    def _NH_ChatStreamWillSendMessage(self, notification):
        self.message_history.add_with_session(notification.sender, notification.data, 'outgoing')

    def _NH_ChatStreamDidSendMessage(self, notification):
        self.message_history.update(notification.data.message.message_id, 'accepted')

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        self.message_history.update(notification.data.message.message_id, 'delivered')

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        self.message_history.update(notification.data.message.message_id, 'failed')

    def _NH_BlinkMessageIsParsed(self, notification):
        session = notification.sender
        message = notification.data

        self.message_history.add_with_session(session, message, 'incoming')

    def _NH_BlinkMessageIsPending(self, notification):
        session = notification.sender
        data = notification.data

        self.message_history.add_with_session(session, data.message, 'outgoing')

    def _NH_BlinkMessageDidSucceed(self, notification):
        data = notification.data
        self.message_history.update(data.id, 'accepted')

    def _NH_BlinkMessageDidFail(self, notification):
        data = notification.data
        self.message_history.update(data.id, 'failed')

    def _NH_BlinkGotDispositionNotification(self, notification):
        data = notification.data
        self.message_history.update(data.id, data.status)

    def _NH_BlinkDidSendDispositionNotification(self, notification):
        data = notification.data
        self.message_history.update(data.id, data.status)


class TableVersion(SQLObject):
    class sqlmeta:
        table = 'table_versions'
    table_name        = StringCol(alternateID=True)
    version           = IntCol()


class Message(SQLObject):
    class sqlmeta:
        table = 'messages'
    message_id      = StringCol()
    account_id      = UnicodeCol(length=128)
    remote_uri      = UnicodeCol(length=128)
    display_name    = UnicodeCol(length=128)
    uri             = UnicodeCol(length=128, default='')
    timestamp       = DateTimeCol()
    direction       = StringCol()
    content         = UnicodeCol(sqlType='LONGTEXT')
    content_type    = StringCol(default='text')
    state           = StringCol(default='pending')
    encryption_type = StringCol(default='')
    disposition     = StringCol(default='')
    remote_idx      = DatabaseIndex('remote_uri')
    id_idx          = DatabaseIndex('message_id')
    unq_idx         = DatabaseIndex(message_id, account_id, remote_uri, unique=True)


class TableVersions(object, metaclass=Singleton):
    __version__ = 1
    __versions__ = {}

    def __init__(self):
        db_file =  ApplicationData.get('message_history.db')
        db_uri = f'sqlite://{db_file}'
        self._initialize(db_uri)

    @run_in_thread('db')
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        TableVersion._connection = self.db

        if not TableVersion.tableExists():
            try:
                TableVersion.createTable()
            except Exception as e:
                pass
            else:
                self.set_version(TableVersion.sqlmeta.table, self.__version__)
        else:
            self._load_versions()

    @run_in_thread('db')
    def _load_versions(self):
        contents = TableVersion.select()
        for table_version in list(contents):
            self.__versions__[table_version.table_name] = table_version.version

    def version(self, table):
        try:
            return self.__versions__[table]
        except KeyError:
            return None

    @run_in_thread('db')
    def set_version(self, table, version):
        try:
            TableVersion(table_name=table, version=version)
        except (dberrors.DuplicateEntryError, dberrors.IntegrityError):
            try:
                record = TableVersion.selectBy(table_name=table).getOne()
                record.version = version
            except Exception as e:
                pass
        except Exception as e:
            pass
        self.__versions__[table] = version


class MessageHistory(object, metaclass=Singleton):
    __version__ = 2
    phone_number_re = re.compile(r'^(?P<number>(0|00|\+)[1-9]\d{7,14})@')

    def __init__(self):
        db_file = ApplicationData.get('message_history.db')
        db_uri = f'sqlite://{db_file}'
        self._initialize(db_uri)

    @run_in_thread('db')
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        Message._connection = self.db
        self.table_versions = TableVersions()
        if not Message.tableExists():
            try:
                Message.createTable()
            except Exception as e:
                pass
            else:
                self.table_versions.set_version(Message.sqlmeta.table, self.__version__)
        else:
            self._check_table_version()

    def _check_table_version(self):
        db_table_version = self.table_versions.version(Message.sqlmeta.table)
        if self.__version__ != db_table_version:
            if db_table_version == 1:
                query = f'CREATE UNIQUE INDEX messages_msg_id ON {Message.sqlmeta.table} (message_id, account_id, remote_uri)'
                try:
                    self.db.queryAll(query)
                except (dberrors.IntegrityError, dberrors.DuplicateEntryError):
                    fix_query = f'select message_id from {Message.sqlmeta.table} group by message_id having count(id) > 1'
                    result = self.db.queryAll(fix_query)
                    for row in result:
                        messages = Message.selectBy(message_id=row[0])
                        for message in list(messages)[1:]:
                            message.destroySelf()
                    try:
                        self.db.queryAll(query)
                    except (dberrors.IntegrityError, dberrors.DuplicateEntryError):
                        pass
                    else:
                        self.table_versions.set_version(Message.sqlmeta.table, self.__version__)
                else:
                    self.table_versions.set_version(Message.sqlmeta.table, self.__version__)

    @classmethod
    @run_in_thread('db')
    def add_with_session(cls, session, message, direction, state=None):
        if message.content.startswith('?OTRv'):
            return

        # print(f"-- Adding message to storage: {message.id}")
        user = session.uri.user
        domain = session.uri.host

        user = user.decode() if isinstance(user, bytes) else user
        domain = domain.decode() if isinstance(domain, bytes) else domain

        remote_uri = '%s@%s' % (user, domain)
        match = cls.phone_number_re.match(remote_uri)
        if match:
            remote_uri = match.group('number')

        try:
            contact = next(contact for contact in AddressbookManager().get_contacts() if remote_uri in (addr.uri for addr in contact.uris))
        except StopIteration:
            display_name = message.sender.display_name
        else:
            display_name = contact.name

        timestamp_native = message.timestamp
        timestamp_utc = timestamp_native.replace(tzinfo=timezone.utc)
        message.timestamp = timestamp_utc - message.timestamp.utcoffset()
        timestamp = parse(str(message.timestamp))

        optional_fields = {}
        if state is not None:
            optional_fields['state'] = state
        if session.chat_type is not None:
            chat_info = session.info.streams.chat

            if chat_info.encryption is not None and chat_info.transport == 'tls':
                optional_fields['encryption_type'] = str(['TLS', '{0.encryption} ({0.encryption_cipher}'.format(chat_info)])
            elif chat_info.encryption is not None:
                optional_fields['encryption_type'] = str(['{0.encryption} ({0.encryption_cipher}'.format(chat_info)])
            elif chat_info.transport == 'tls':
                optional_fields['encryption_type'] = str(['TLS'])
        try:
            Message(remote_uri=remote_uri,
                    display_name=display_name,
                    uri=str(message.sender.uri),
                    content=message.content,
                    content_type=message.content_type,
                    message_id=message.id,
                    account_id=str(session.account.id),
                    direction=direction,
                    timestamp=timestamp,
                    disposition=str(message.disposition),
                    **optional_fields)
        except dberrors.DuplicateEntryError:
            try:
                dbmessage = Message.selectBy(message_id=message.id)[0]
            except IndexError:
                pass
            else:
                if message.content != dbmessage.content:
                    dbmessage.content = message.content

    @run_in_thread('db')
    def update(self, id, state):
        try:
            message = Message.selectBy(message_id=id)[0]
        except IndexError:
            pass
        else:
            # print(f'-- Updating {id} {message.state} -> {state}')
            message.state = state

    @run_in_thread('db')
    def load(self, uri, session):
        # print('-- Loading messages')
        notification_center = NotificationCenter()
        try:
            result = Message.selectBy(remote_uri=uri)[-100:]
        except Exception as e:
            notification_center.post_notification('BlinkMessageHistoryLoadDidFail', sender=session, data=NotificationData(uri=uri))
            return
        # print(f"-- Messages loaded: {len(list(result))}")
        notification_center.post_notification('BlinkMessageHistoryLoadDidSucceed', sender=session, data=NotificationData(messages=list(result), uri=uri))

    @run_in_thread('db')
    def get_last_contacts(self, number=5):
        # print(f'-- Getting last {number} contacts wtih messages')

        query = f'select distinct(remote_uri) from messages order by id desc limit {Message.sqlrepr(number)}'
        notification_center = NotificationCenter()
        try:
            result = self.db.queryAll(query)
        except Exception as e:
            return

        # print(f"-- Contacts fetched: {len(list(result))}")
        result = [' '.join(item) for item in result]
        notification_center.post_notification('BlinkMessageHistoryLastContactsDidSucceed', data=NotificationData(contacts=list(result)))

    @run_in_thread('db')
    def remove(self, account):
        Message.deleteBy(account=account)

    @run_in_thread('db')
    def remove_contact_messages(self, contact):
        Message.deleteBy(remote_uri=contact)

    @run_in_thread('db')
    def remove_message(self, id):
        try:
            result = Message.selectBy(message_id=id)[0]
        except IndexError:
            return
        result.destroySelf()


class IconDescriptor(object):
    def __init__(self, filename):
        self.filename = filename
        self.icon = None

    def __get__(self, instance, owner):
        if self.icon is None:
            self.icon = QIcon(self.filename)
            self.icon.filename = self.filename
        return self.icon

    def __set__(self, obj, value):
        raise AttributeError("attribute cannot be set")

    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


class HistoryEntry(object):
    phone_number_re = re.compile(r'^(?P<number>(0|00|\+)[1-9]\d{7,14})@')

    incoming_normal_icon = IconDescriptor(Resources.get('icons/arrow-inward-blue.svg'))
    outgoing_normal_icon = IconDescriptor(Resources.get('icons/arrow-outward-green.svg'))
    incoming_failed_icon = IconDescriptor(Resources.get('icons/arrow-inward-red.svg'))
    outgoing_failed_icon = IconDescriptor(Resources.get('icons/arrow-outward-red.svg'))

    def __init__(self, direction, name, uri, account_id, call_time, duration, failed=False, reason=None):
        self.direction = direction
        self.name = name
        self.uri = uri
        self.account_id = account_id
        self.call_time = call_time
        self.duration = duration
        self.failed = failed
        self.reason = reason

    def __reduce__(self):
        return self.__class__, (self.direction, self.name, self.uri, self.account_id, self.call_time, self.duration, self.failed, self.reason)

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return self is not other

    def __lt__(self, other):
        return self.call_time < other.call_time

    def __le__(self, other):
        return self.call_time <= other.call_time

    def __gt__(self, other):
        return self.call_time > other.call_time

    def __ge__(self, other):
        return self.call_time >= other.call_time

    @property
    def icon(self):
        if self.failed:
            return self.incoming_failed_icon if self.direction == 'incoming' else self.outgoing_failed_icon
        else:
            return self.incoming_normal_icon if self.direction == 'incoming' else self.outgoing_normal_icon

    @property
    def text(self):
        result = str(self.name or self.uri)
        blink_settings = BlinkSettings()
        if blink_settings.interface.show_history_name_and_uri:
            result = f'{str(self.name)} ({str(self.uri)})'

        if self.call_time:
            call_time = self.call_time.astimezone(tzlocal())
            call_date = call_time.date()
            today = date.today()
            days = (today - call_date).days
            if call_date == today:
                result += call_time.strftime(" at %H:%M")
            elif days == 1:
                result += call_time.strftime(" Yesterday at %H:%M")
            elif days < 7:
                result += call_time.strftime(" on %A")
            elif call_date.year == today.year:
                result += call_time.strftime(" on %B %d")
            else:
                result += call_time.strftime(" on %Y-%m-%d")
        if self.duration:
            seconds = int(self.duration.total_seconds())
            if seconds >= 3600:
                result += """ (%dh%02d'%02d")""" % (seconds / 3600, (seconds % 3600) / 60, seconds % 60)
            else:
                result += """ (%d'%02d")""" % (seconds / 60, seconds % 60)
        elif self.reason:
            result += ' (%s)' % self.reason.title()
        return result

    @classmethod
    def from_session(cls, session):
        if session.start_time is None and session.end_time is not None:
            # Session may have ended before it fully started
            session.start_time = session.end_time
        call_time = session.start_time or ISOTimestamp.now()
        if session.start_time and session.end_time:
            duration = session.end_time - session.start_time
        else:
            duration = None
        user = session.remote_identity.uri.user
        domain = session.remote_identity.uri.host

        user = user.decode() if isinstance(user, bytes) else user
        domain = domain.decode() if isinstance(domain, bytes) else domain

        remote_uri = '%s@%s' % (user, domain)
        match = cls.phone_number_re.match(remote_uri)
        if match:
            remote_uri = match.group('number')
        try:
            contact = next(contact for contact in AddressbookManager().get_contacts() if remote_uri in (addr.uri for addr in contact.uris))
        except StopIteration:
            display_name = session.remote_identity.display_name
        else:
            display_name = contact.name
        return cls(session.direction, display_name, remote_uri, str(session.account.id), call_time, duration)
