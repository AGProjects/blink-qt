
import bisect
import pickle as pickle
import os
import re
import uuid

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from application.system import makedirs, unlink

from datetime import date, timezone
from dateutil.parser import parse
from dateutil.tz import tzlocal
from zope.interface import implementer

from sipsimple.account import BonjourAccount
from sipsimple.addressbook import AddressbookManager
from sipsimple.payloads.iscomposing import IsComposingDocument
from sipsimple.payloads.imdn import IMDNDocument
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.configuration.settings import BlinkSettings
from blink.logging import MessagingTrace as log
from blink.messages import BlinkMessage
from blink.resources import ApplicationData, Resources
from blink.util import run_in_gui_thread, translate
import traceback

from sqlobject import SQLObject, StringCol, DateTimeCol, IntCol, UnicodeCol, DatabaseIndex, AND
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
        self.download_history = DownloadHistory()

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
        notification_center.add_observer(self, name='BlinkMessageDidEncrypt')
        notification_center.add_observer(self, name='BlinkMessageDidDecrypt')
        notification_center.add_observer(self, name='BlinkMessageWillDelete')
        notification_center.add_observer(self, name='BlinkGotDispositionNotification')
        notification_center.add_observer(self, name='BlinkDidSendDispositionNotification')
        notification_center.add_observer(self, name='BlinkGotHistoryMessage')
        notification_center.add_observer(self, name='BlinkGotHistoryMessageDelete')
        notification_center.add_observer(self, name='BlinkGotHistoryMessageUpdate')
        notification_center.add_observer(self, name='BlinkGotHistoryConversationRemove')
        notification_center.add_observer(self, name='BlinkFileTransferDidEnd')
        notification_center.add_observer(self, name='BlinkHTTPFileTransferDidEnd')
        notification_center.add_observer(self, name='BlinkMessageContactsDidChange')
        notification_center.add_observer(self, name='MessageContactsManagerDidActivate')

    @run_in_thread('file-io')
    def save(self):
        with open(ApplicationData.get('calls_history'), 'wb+') as history_file:
            pickle.dump(self.calls, history_file)

    def load(self, uri, session, entries=100):
        return self.message_history.load(uri, session, entries=entries)

    def get_last_contacts(self, number=10, unread=False):
        return self.message_history.get_last_contacts(number, unread=unread)

    def get_decrypted_filename(self, file):
        return self.download_history.get_decrypted_filename(file)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        try:
            data = pickle.load(open(ApplicationData.get('calls_history'), "rb"))
            if not isinstance(data, list) or not all(isinstance(item, HistoryEntry) and item.text and isinstance(item.call_time, ISOTimestamp) for item in data):
                raise ValueError("invalid save data")
        except FileNotFoundError:
            pass
        except Exception as e:
            traceback.print_exc()
        else:
            self.calls = data[-self.history_size:]
        self.message_history._retry_failed_messages()
        self.message_history.get_unread_messages()

    def _NH_SIPSessionDidEnd(self, notification):
        if notification.sender.account is BonjourAccount():
            return
        session = notification.sender
        entry = HistoryEntry.from_session(session)
        bisect.insort(self.calls, entry)
        self.calls = self.calls[-self.history_size:]
        self.save()
        self.message_history.add_call_history_entry(entry, session)

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
        self.message_history.add_call_history_entry(entry, session)

    def _NH_ChatStreamGotMessage(self, notification):
        message = notification.data.message

        if notification.sender.blink_session.remote_focus and self.sip_prefix_re.sub('', str(message.sender.uri)) not in notification.sender.blink_session.server_conference.participants:
            return

        is_status_message = any(h.name == 'Message-Type' and h.value == 'status' and h.namespace == 'urn:ag-projects:xml:ns:cpim' for h in message.additional_headers)
        if not is_status_message:
            blink_message = BlinkMessage(**{slot: getattr(message, slot) for slot in message.__slots__})
            self.message_history.add_from_session(notification.sender.blink_session, blink_message, 'incoming', 'delivered')

    def _NH_ChatStreamWillSendMessage(self, notification):
        self.message_history.add_from_session(notification.sender, notification.data, 'outgoing')

    def _NH_ChatStreamDidSendMessage(self, notification):
        self.message_history.update(notification.data.message.message_id, 'accepted')

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        self.message_history.update(notification.data.message.message_id, 'delivered')

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        self.message_history.update(notification.data.message_id, 'failed')

    def _NH_BlinkMessageIsParsed(self, notification):
        session = notification.sender
        message = notification.data

        self.message_history.add_from_session(session, message, 'incoming')

    def _NH_BlinkMessageIsPending(self, notification):
        session = notification.sender
        data = notification.data

        self.message_history.add_from_session(session, data.message, 'outgoing')

    def _NH_BlinkGotHistoryMessage(self, notification):
        account = notification.sender
        self.message_history.add_from_server_history(account, **notification.data.__dict__)

    def _NH_BlinkGotHistoryMessageDelete(self, notification):
        self.message_history.remove_message(notification.data)
        self.download_history.remove(notification.data)
        settings = BlinkSettings()
        if settings.interface.show_messages_group:
            self.message_history.get_all_contacts()

    def _NH_BlinkGotHistoryConversationRemove(self, notification):
        self.message_history.remove_contact_messages(notification.sender, notification.data)
        settings = BlinkSettings()
        if settings.interface.show_messages_group:
            self.message_history.get_all_contacts()

    def _NH_BlinkGotHistoryMessageUpdate(self, notification):
        self.message_history.update_message(notification)

    def _NH_BlinkMessageDidSucceed(self, notification):
        data = notification.data
        self.message_history.update(data.id, 'accepted')

    def _NH_BlinkMessageDidFail(self, notification):
        data = notification.data
        try:
            status = 'failed-local' if data.data.originator == 'local' else 'failed'
        except AttributeError:
            status = 'failed'
        self.message_history.update(data.id, status)

    def _NH_BlinkMessageWillDelete(self, notification):
        data = notification.data
        self.message_history.update(data.id, 'deleted')
        self.download_history.remove(data.id)

    def _NH_BlinkMessageDidDecrypt(self, notification):
        self.message_history.update_encryption(notification)

    def _NH_BlinkMessageDidEncrypt(self, notification):
        self.message_history.update_encryption(notification)

    def _NH_BlinkGotDispositionNotification(self, notification):
        data = notification.data
        self.message_history.update(data.id, data.status)

    def _NH_BlinkDidSendDispositionNotification(self, notification):
        data = notification.data
        self.message_history.update(data.id, data.status)

    def _NH_BlinkFileTransferDidEnd(self, notification):
        if not notification.data.error:
            self.download_history.add(notification.sender)

    def _NH_BlinkHTTPFileTransferDidEnd(self, notification):
        self.download_history.add_file(notification.sender, notification.data.file)

    def _NH_BlinkMessageContactsDidChange(self, notification):
        self.message_history.get_all_contacts()

    def _NH_MessageContactsManagerDidActivate(self, notification):
        self.message_history.get_all_contacts()


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


class DownloadedFiles(SQLObject):
    class sqlmeta:
        table = 'downloaded_files'
    file_id            = StringCol()
    account_id         = UnicodeCol(length=128)
    remote_uri         = UnicodeCol(length=128)
    filename           = UnicodeCol()
    id_idx             = DatabaseIndex('file_id')
    unq_idx            = DatabaseIndex(file_id, filename, account_id, unique=True)


class TableVersions(object, metaclass=Singleton):
    __version__ = 1
    __versions__ = {}

    def __init__(self):
        db_file = ApplicationData.get('message_history.db')
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


class DownloadHistory(object, metaclass=Singleton):
    __version__ = 1
    phone_number_re = re.compile(r'^(?P<number>(0|00|\+)[1-9]\d{7,14})@')

    def __init__(self):
        db_file = ApplicationData.get('message_history.db')
        db_uri = f'sqlite://{db_file}'
        self._initialize(db_uri)

    @run_in_thread('db')
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        DownloadedFiles._connection = self.db
        self.table_versions = TableVersions()

        if not DownloadedFiles.tableExists():
            try:
                DownloadedFiles.createTable()
            except Exception as e:
                pass
            else:
                self.table_versions.set_version(DownloadedFiles.sqlmeta.table, self.__version__)
        else:
            self._check_table_version()

    def _check_table_version(self):
        pass

    @classmethod
    @run_in_thread('db')
    def add(cls, session):
        remote_uri = str(session.contact_uri.uri)
        match = cls.phone_number_re.match(remote_uri)
        if match:
            remote_uri = match.group('number')
        try:
            DownloadedFiles(file_id=session.id,
                            account_id=str(session.account.id),
                            remote_uri=remote_uri,
                            filename=session.file_selector.name)
        except dberrors.DuplicateEntryError:
            pass

    @classmethod
    @run_in_thread('db')
    def add_file(cls, session, file):
        remote_uri = str(session.contact_uri.uri)
        match = cls.phone_number_re.match(remote_uri)
        if match:
            remote_uri = match.group('number')
        try:
            DownloadedFiles(file_id=file.id,
                            account_id=str(session.account.id),
                            remote_uri=remote_uri,
                            filename=file.name)
        except dberrors.DuplicateEntryError:
            pass

    def get_decrypted_filename(self, file):
        try:
            return DownloadedFiles.selectBy(file_id=file.id).getOne().filename
        except Exception as e:
            return file.name

    @run_in_thread('db')
    def remove(self, id):
        log.debug(f'== Trying to remove download cache: {id}')
        result = DownloadedFiles.selectBy(file_id=id)
        for file in result:
            self.remove_cache_file(file)
            log.info(f'== Removing file entry: {file.file_id}')
            file.destroySelf()

    @run_in_thread('file-io')
    def remove_cache_file(self, file):
        filename = os.path.basename(file.filename)
        if filename.endswith('.asc'):
            filename = filename.rsplit('.', 1)[0]
        cached_file = os.path.join(ApplicationData.get('transfer_images'), file.file_id, filename)
        file_in_cache = os.path.exists(cached_file)
        if not file_in_cache:
            log.info(f'== Not removing file, not present in cache: {file.file_id} {cached_file}')
            return
        log.info(f'== Removing file from cache: {file.file_id} {cached_file}')
        unlink(cached_file)
        try:
            os.rmdir(os.path.dirname(cached_file))
        except OSError:
            pass

    @run_in_thread('db')
    def remove_contact_files(self, account, contact):
        log.info(f'== Removing file entries and files from cache between {account.id} <-> {contact}')
        result = DownloadedFiles.selectBy(remote_uri=contact, account_id=str(account.id))
        for file in result:
            self.remove_cache_file(file)
            file.destroySelf()

    @run_in_thread('db')
    def update(self, id, state):
        messages = Message.selectBy(message_id=id)
        for message in messages:
            if message.direction == 'outgoing' and state == 'received':
                continue

            if message.state != 'displayed' and message.state != state:
                log.info(f'== Updating {message.direction} {id} {message.state} -> {state}')
                message.state = state


class MessageHistory(object, metaclass=Singleton):
    __version__ = 3
    phone_number_re = re.compile(r'^(?P<number>(0|00|\+)[1-9]\d{7,14})@')

    def __init__(self):
        db_file = ApplicationData.get('message_history.db')
        db_uri = f'sqlite://{db_file}'
        makedirs(ApplicationData.directory)
        self._initialize(db_uri)
        self._retry_timer = QTimer()
        self._retry_timer.setInterval(60 * 1000)  # a minute (in milliseconds)
        self._retry_timer.timeout.connect(self._retry_failed_messages)
        self._retry_timer.start()

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
            elif db_table_version == 2:
                query = "delete from messages where content_type='application/sylk-api-pgp-key-lookup'"
                try:
                    self.db.queryAll(query)
                except (dberrors.IntegrityError, dberrors.DuplicateEntryError):
                    pass
                else:
                    self.table_versions.set_version(Message.sqlmeta.table, self.__version__)

    @run_in_thread('db')
    def _retry_failed_messages(self):
        messages = Message.selectBy(state='failed-local')
        if len(list(messages)) > 0:
            log.debug(f"==  {len(list(messages))} failed local messages from history")
            NotificationCenter().post_notification('BlinkMessageHistoryFailedLocalFound', data=NotificationData(messages=list(messages)))

    @classmethod
    @run_in_thread('db')
    def add_call_history_entry(cls, entry, session):
        timestamp_native = entry.call_time
        timestamp_utc = timestamp_native.replace(tzinfo=timezone.utc)
        timestamp_fixed = timestamp_utc - entry.call_time.utcoffset()
        timestamp = parse(str(timestamp_fixed))
        media = "audio"

        if not session.streams and not session.proposed_streams:
            return

        streams = [stream.type for stream in session.streams] if session.streams else [stream.type for stream in session.proposed_streams]
        if 'audio' not in streams and 'video' not in streams:
            return

        media = 'video' if 'video' in streams else 'audio'
        media = 'file-transfer' if 'file-transfer' in streams else media

        log.info(f"== Adding call history message to storage: {entry.direction} {media} to {entry.uri}")

        uri = str(entry.uri)

        result = 0
        if entry.duration:
            seconds = int(entry.duration.total_seconds())
            if seconds >= 3600:
                result = """ (%dh%02d'%02d")""" % (seconds / 3600, (seconds % 3600) / 60, seconds % 60)
            else:
                result = """ (%d'%02d")""" % (seconds / 60, seconds % 60)
        try:
            message = Message(remote_uri=entry.uri,
                              display_name=entry.name,
                              uri=uri,
                              content=str([result, entry.reason.title() if entry.reason else '', media]),
                              content_type='application/blink-call-history',
                              message_id=str(uuid.uuid4()),
                              account_id=str(entry.account_id),
                              direction=entry.direction,
                              timestamp=timestamp,
                              disposition='',
                              state='displayed')
        except dberrors.DuplicateEntryError:
            pass
        else:
            NotificationCenter().post_notification('BlinkMessageHistoryCallHistoryDidStore', sender=session, data=NotificationData(message=message))

    @classmethod
    @run_in_thread('db')
    def add_from_server_history(cls, account, remote_uri, message, state=None, encryption=None):
        if message.content.startswith('?OTRv'):
            return

        log.info(f"== Adding {message.direction} history message to storage: {message.id} {state} {remote_uri}")

        match = cls.phone_number_re.match(remote_uri)
        if match:
            remote_uri = match.group('number')

        if message.direction == 'outgoing':
            display_name = message.sender.display_name
        else:
            try:
                contact = next(contact for contact in AddressbookManager().get_contacts() if remote_uri in (addr.uri for addr in contact.uris))
            except StopIteration:
                display_name = ''
            else:
                display_name = contact.name

        timestamp_native = message.timestamp
        timestamp_utc = timestamp_native.replace(tzinfo=timezone.utc)
        timestamp_fixed = timestamp_utc - message.timestamp.utcoffset()
        timestamp = parse(str(timestamp_fixed))

        optional_fields = {}
        if state is not None:
            optional_fields['state'] = state

        if encryption is not None:
            optional_fields['encryption_type'] = str([f'{encryption}'])

        uri = str(message.sender.uri)
        if not uri.startswith(('sip:', 'sips:')):
            uri = f'sip:{uri}'

        try:
            Message(remote_uri=remote_uri,
                    display_name=display_name,
                    uri=uri,
                    content=message.content,
                    content_type=message.content_type,
                    message_id=message.id,
                    account_id=str(account.id),
                    direction=message.direction,
                    timestamp=timestamp,
                    disposition=str(message.disposition),
                    **optional_fields)
        except dberrors.DuplicateEntryError:
            pass
        else:
            if message.content_type not in {IsComposingDocument.content_type, IMDNDocument.content_type, 'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-message-remove'}:
                notification_center = NotificationCenter()
                notification_center.post_notification('BlinkMessageHistoryMessageDidStore', sender=account, data=NotificationData(remote_uri=remote_uri, state=state, direction=message.direction))

    @classmethod
    @run_in_thread('db')
    def add_from_session(cls, session, message, direction, state=None):
        if message.content.startswith('?OTRv'):
            return

        if session.remote_instance_id:
            remote_uri = '%s@local' % session.remote_instance_id
        else:
            user = session.uri.user
            domain = session.uri.host

            user = user.decode() if isinstance(user, bytes) else user
            domain = domain.decode() if isinstance(domain, bytes) else domain

            remote_uri = '%s@%s' % (user, domain)
            match = cls.phone_number_re.match(remote_uri)
            if match:
                remote_uri = match.group('number')

        log.info(f"Storing {direction} message {message.id} of account {session.account.id} with {remote_uri} ")

        if direction == 'outgoing':
            display_name = message.sender.display_name
        else:
            try:
                contact = next(contact for contact in AddressbookManager().get_contacts() if remote_uri in (addr.uri for addr in contact.uris))
            except StopIteration:
                display_name = message.sender.display_name
            else:
                display_name = contact.name

        timestamp_native = message.timestamp
        timestamp_utc = timestamp_native.replace(tzinfo=timezone.utc)
        timestamp_fixed = timestamp_utc - message.timestamp.utcoffset()
        timestamp = parse(str(timestamp_fixed))

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
        else:
            message_info = session.info.streams.messages
            if message_info.encryption is not None and message.is_secure:
                optional_fields['encryption_type'] = str([f'{message_info.encryption}'])
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
        else:
            if message.content_type not in {IsComposingDocument.content_type, IMDNDocument.content_type, 'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-message-remove'}:
                notification_center = NotificationCenter()
                notification_center.post_notification('BlinkMessageHistoryMessageDidStore', sender=session.account, data=NotificationData(remote_uri=remote_uri, state=state, direction=direction))

    @run_in_thread('db')
    def update_message(self, notification):
        message = notification.data

        db_message = Message.selectBy(message_id=message.id)[0]
        if db_message.content != message.content:
            db_message.content = message.content

    @run_in_thread('db')
    def update(self, id, state):
        messages = Message.selectBy(message_id=id)
        for message in messages:
            if message.direction == 'outgoing' and state == 'received':
                continue

            if (state == 'deleted' or message.state != 'displayed') and message.state != state:
                log.info(f'== Updating {message.direction} {id} {message.state} -> {state}')
                message.state = state

    @run_in_thread('db')
    def update_displayed_for_uri(self, remote_uri):
        query = f"""update messages set state = 'displayed' where direction = 'incoming'
        and remote_uri = {Message.sqlrepr(remote_uri)} and state != 'displayed'
        """
        try:
            result = self.db.queryAll(query)
        except Exception as e:
            pass
        else:
            log.info('Conversation with %s read saved to history' % remote_uri)

    @run_in_thread('db')
    def update_encryption(self, notification):
        message = notification.data.message
        session = notification.sender
        message_info = session.info.streams.messages

        if message_info.encryption is not None and message.is_secure:
            db_messages = Message.selectBy(message_id=message.id)
            for db_message in db_messages:
                encryption_type = str(f'{message_info.encryption}')
                if db_message.encryption_type != encryption_type:
                    log.debug(f'== Updating {message.direction} {message.id} encryption to {encryption_type}')
                    db_message.encryption_type = encryption_type

    @run_in_thread('db')
    def load(self, uri, session, entries=100):
        notification_center = NotificationCenter()
        remote_uri = '%s@local' % session.remote_instance_id if session.remote_instance_id else uri
        try:
            result = Message.select(AND(Message.q.remote_uri == remote_uri, Message.q.state != 'deleted')).orderBy('timestamp')[-entries:]
        except Exception as e:
            notification_center.post_notification('BlinkMessageHistoryLoadDidFail', sender=session, data=NotificationData(uri=uri))
            return
        log.debug(f"== Loaded {len(list(result))} messages for {remote_uri} from history")
        notification_center.post_notification('BlinkMessageHistoryLoadDidSucceed', sender=session, data=NotificationData(messages=list(result), uri=uri))

    @run_in_thread('db')
    def get_last_contacts(self, number=10, unread=False):
        log.info(f'== Getting last {number} contacts with messages unread={unread}')
        if unread:
            query = f"""
                select im.display_name, am.remote_uri, max(am.timestamp) from messages as am
                left join (select display_name, remote_uri from messages where direction="incoming" group by remote_uri) as im
                on am.remote_uri = im.remote_uri
                where
                am.content_type not like "%pgp%"
                and am.direction="incoming"
                and am.content_type not like "%sylk-api%"
                and am.content_type != "application/blink-call-history"
                and am.state not in ('deleted', 'displayed')
                group by am.remote_uri order by am.timestamp desc"""
        else:
            query = f"""
                select im.display_name, am.remote_uri, max(am.timestamp) from messages as am
                left join (select display_name, remote_uri from messages where direction="incoming" group by remote_uri) as im
                on am.remote_uri = im.remote_uri
                where
                am.content_type not like "%pgp%"
                and am.content_type not like "%sylk-api%"
                and am.content_type != "application/blink-call-history"
                and am.state != 'deleted'
                group by am.remote_uri order by am.timestamp desc limit {Message.sqlrepr(number)}"""

        notification_center = NotificationCenter()
        try:
            result = self.db.queryAll(query)
        except Exception as e:
            return

        log.debug(f"== Contacts fetched: {len(list(result))}")
        result = [(display_name, uri) for (display_name, uri, timestamp) in result]
        results = list(result)
        results.reverse()
        notification_center.post_notification('BlinkMessageHistoryLastContactsDidSucceed', data=NotificationData(contacts=results))

    @run_in_thread('db')
    def get_unread_messages(self):
        query = """select remote_uri, count(*) as c from messages where state != 'displayed' and direction='incoming' group by remote_uri"""
        try:
            result = self.db.queryAll(query)
        except Exception as e:
            return

        unread_messages = {}
        for (remote_uri, c) in result:
            unread_messages[remote_uri] = c

        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageHistoryUnreadMessagesDidLoad', data=NotificationData(unread_messages=unread_messages))

    @run_in_thread('db')
    def get_all_contacts(self):
        log.debug(f'== Getting all contacts with messages')

        query = """
            select im.display_name, am.remote_uri from messages as am
            left join (select display_name, remote_uri from messages where direction='incoming' group by remote_uri) as im
            on (am.remote_uri = im.remote_uri)
            where
            am.content_type not like '%pgp%'
            and not am.content_type like '%sylk-api%'
            and am.content_type != "application/blink-call-history"
            and am.state != 'deleted'
            group by am.remote_uri
            """
        notification_center = NotificationCenter()
        try:
            result = self.db.queryAll(query)
        except Exception as e:
            return

        log.debug(f"== Contacts fetched: {len(list(result))}")
        notification_center.post_notification('BlinkMessageHistoryAllContactsDidSucceed', data=NotificationData(contacts=list(result)))

    @run_in_thread('db')
    def remove(self, account):
        Message.deleteBy(account=account)

    @run_in_thread('db')
    def remove_contact_messages(self, account, contact):
        log.info(f'== Removing conversation between {account.id} <-> {contact}')
        Message.deleteBy(remote_uri=contact, account_id=str(account.id))

    @run_in_thread('db')
    def remove_message(self, id):
        log.debug(f'== Trying to removing message: {id}')
        result = Message.selectBy(message_id=id)
        for message in result:
            log.info(f'== Removing message: {id}')
            message.destroySelf()


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
                result += call_time.strftime(translate("history", " at %H:%M"))
            elif days == 1:
                result += call_time.strftime(translate("history", " Yesterday at %H:%M"))
            elif days < 7:
                result += call_time.strftime(translate("history", " on %A"))
            elif call_date.year == today.year:
                result += call_time.strftime(translate("history", " on %B %d"))
            else:
                result += call_time.strftime(translate("history", " on %Y-%m-%d"))
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
