
import bisect
import glob
import json
import pickle as pickle
import os
import re
import time
import uuid
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from application.system import host, makedirs, unlink

from datetime import date, datetime, timezone
from dateutil.parser import parse
from dateutil.tz import tzlocal
from zope.interface import implementer

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.addressbook import AddressbookManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads.iscomposing import IsComposingDocument
from sipsimple.payloads.imdn import IMDNDocument
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.configuration.settings import BlinkSettings
from blink.logging import ActivityLog, MessagingTrace as log
from blink.message_envelopes import FILE_TRANSFER_CONTENT_TYPES, LOCATION_CONTENT_TYPE, CALL_CONTENT_TYPE, LEGACY_CALL_CONTENT_TYPE, classify_category, has_link
from blink.message_envelopes import build_call_record, call_summary, dominant_media, legacy_call_record, this_device_id
from blink.message_envelopes import METADATA_CONTENT_TYPE, reply_metadata
from blink.messages import BlinkMessage
from blink.resources import ApplicationData, Resources
from blink.sessions import BlinkSession

from blink.uris import BONJOUR_ACCOUNT_ID, bare_instance_id, is_instance_id, placeholder_instance_id
from blink.util import run_in_gui_thread, translate
import traceback

from sqlobject import SQLObject, StringCol, DateTimeCol, IntCol, UnicodeCol, DatabaseIndex, AND, OR
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
        notification_center.add_observer(self, name='BlinkMessageDidNotDecrypt')
        notification_center.add_observer(self, name='BlinkMessageWillDelete')
        notification_center.add_observer(self, name='BlinkConversationWillRemove')
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
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, name='SIPAccountManagerDidRemoveAccount')
        notification_center.add_observer(self, name='BlinkSessionConfirmReadMessages')
        notification_center.add_observer(self, name='BlinkConfirmReadMessagesOnOtherDevice')

    @run_in_thread('file-io')
    def save(self):
        with open(ApplicationData.get('calls_history'), 'wb+') as history_file:
            pickle.dump(self.calls, history_file)

    def load(self, uri, session, entries=100):
        return self.message_history.load(uri, session, entries=entries)

    def reload_pending_encrypted(self, uri, session, entries=100):
        return self.message_history.reload_pending_encrypted(uri, session, entries=entries)

    def get_last_contacts(self, number=10, unread=False):
        return self.message_history.get_last_contacts(number, unread=unread)

    def get_decrypted_filename(self, file):
        return self.download_history.get_decrypted_filename(file)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if isinstance(notification.sender, (Account, BonjourAccount)):
            account_manager = AccountManager()
            account = notification.sender
            if 'sms.private_key' in notification.data.modified:
                self.message_history.reset_decryption(str(account.id))
            if 'enabled' in notification.data.modified:
                self.message_history.get_unread_messages()


    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        account = notification.data.account
        if account is BonjourAccount():
            return
        ActivityLog().info('[db] Account %s was deleted, removing its history' % account.id)
        self.message_history.remove(account)
        self.download_history.remove_account_files(account)
        calls = [entry for entry in self.calls if entry.account_id != str(account.id)]
        if len(calls) != len(self.calls):
            ActivityLog().info('[db] Removed %d call history entries of %s' % (len(self.calls) - len(calls), account.id))
            self.calls = calls
            self.save()
        self._remove_account_keys(account)
        # the db thread runs these after the removal above
        self.message_history.get_unread_messages()
        if BlinkSettings().interface.show_messages_group:
            self.message_history.get_all_contacts()

    @run_in_thread('file-io')
    def _remove_account_keys(self, account):
        # own PGP keys: keys/private/<account>.{privkey,pubkey}, keys replaced earlier
        # (<account>-<timestamp>-old.*), whatever the account settings point to,
        # and our own public key if it was saved among the peer keys
        directory = SIPSimpleSettings().chat.keys_directory.normalized
        private_directory = os.path.join(directory, 'private')
        filenames = set()
        for name in {account.id, account.id.replace('/', '_')}:
            base = glob.escape(os.path.join(private_directory, name))
            filenames.update(glob.glob(base + '.privkey'))
            filenames.update(glob.glob(base + '.pubkey'))
            filenames.update(glob.glob(base + '-*-old.privkey'))
            filenames.update(glob.glob(base + '-*-old.pubkey'))
            filenames.add(os.path.join(directory, name + '.pubkey'))
        for setting in (account.sms.private_key, account.sms.public_key):
            if setting is not None:
                filenames.add(setting.normalized)
        removed = 0
        for filename in filenames:
            if os.path.isfile(filename):
                unlink(filename)
                removed += 1
        ActivityLog().info('[db] Removed %d PGP key files of %s' % (removed, account.id))

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
        self.message_history.add_call_history_entry(entry, session, status=notification.data.code, failure_reason=notification.data.failure_reason)

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
        data = notification.data
        self.message_history.remove_contact_messages(notification.sender, data.contact, data.timestamp)
        self.download_history.remove_contact_files(notification.sender, data.contact)
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
        status = 'failed-local' if data.originator == 'local' else 'failed'
        self.message_history.update(data.id, status)

    def _NH_BlinkMessageWillDelete(self, notification):
        data = notification.data
        self.message_history.update(data.id, 'deleted')
        self.download_history.remove(data.id)

    def _NH_BlinkMessageDidDecrypt(self, notification):
        self.message_history.update_encryption(notification, decrypted=True)

    def _NH_BlinkMessageDidNotDecrypt(self, notification):
        self.message_history.update_encryption(notification, decrypted=False)

    def _NH_BlinkMessageDidEncrypt(self, notification):
        self.message_history.update_encryption(notification, decrypted=None)

    def _NH_BlinkConversationWillRemove(self, notification):
        data = notification.data
        # a Bonjour conversation is filed under the neighbour's instance id, not its address
        contact = bare_instance_id(getattr(notification.sender, 'remote_instance_id', None)) or str(data.contact)
        if getattr(data, 'all_accounts', False):
            # removed by the user: the conversation is shown as one whatever account each message was filed under
            self.message_history.remove_conversation(contact, data.timestamp, notification.sender)
            self.download_history.remove_contact_files(None, contact)
        else:
            self.message_history.remove_contact_messages(notification.sender.account, contact, data.timestamp, notification.sender)
            self.download_history.remove_contact_files(notification.sender.account, contact)
        settings = BlinkSettings()
        if settings.interface.show_messages_group:
            self.message_history.get_all_contacts()

    def _NH_BlinkSessionConfirmReadMessages(self, notification):
        # the user has the conversation in front of them; keyed as the chat window loads it
        session = notification.sender
        key = bare_instance_id(session.remote_instance_id) if session.remote_instance_id else str(session.contact.uri.uri)
        self.message_history.mark_conversation_read(key)

    def _NH_BlinkConfirmReadMessagesOnOtherDevice(self, notification):
        self.message_history.mark_conversation_read(str(notification.data.remote_uri), source='another device')

    def _NH_BlinkGotDispositionNotification(self, notification):
        data = notification.data
        self.message_history.update(data.id, data.status)

    def _NH_BlinkDidSendDispositionNotification(self, notification):
        data = notification.data
        self.message_history.update(data.id, data.status)

    def _NH_BlinkFileTransferDidEnd(self, notification):
        if not notification.data.error:
            if type(notification.sender) is not BlinkSession:
                # this was a HTTP transfer
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
    decrypted       = StringCol(default='0')
    decryption_error= StringCol(sqlType='LONGTEXT')
    disposition     = StringCol(default='')
    # version 5, names shared with Blink for macOS and Sylk Mobile
    read            = IntCol(default=1, defaultSQL='1')  # 0 = incoming and not yet read
    category        = StringCol(default=None)                 # text, audio, image, video, location, call, other
    has_link        = IntCol(default=0, defaultSQL='0')  # text contains a link (Links filter)
    metadata        = UnicodeCol(sqlType='LONGTEXT', default=None)  # cleartext envelope (location v2, call record, CPIM agp.Metadata)
    related_msg_id  = StringCol(default=None)                 # owner of a location tick or metadata companion
    related_action  = StringCol(default=None)
    deleted         = IntCol(default=0, defaultSQL='0')  # tombstone
    deleted_time    = IntCol(default=0, defaultSQL='0')  # epoch when the tombstone was set
    journal_id      = StringCol(default=None)                 # SylkServer journal entry id
    sip_callid      = StringCol(default=None)                 # call record merge key
    media_type      = StringCol(default=None)                 # sms, chat, audio, video, ...
    cpim_from       = UnicodeCol(length=128, default=None)
    cpim_to         = UnicodeCol(length=128, default=None)
    cpim_timestamp  = StringCol(default=None)                 # sender timestamp as received
    private         = IntCol(default=0, defaultSQL='0')
    expire_time     = IntCol(default=0, defaultSQL='0')  # reserved, Sylk Mobile 'expire'
    remote_idx      = DatabaseIndex('remote_uri')
    id_idx          = DatabaseIndex('message_id')
    unq_idx         = DatabaseIndex(message_id, account_id, remote_uri, unique=True)
    account_idx     = DatabaseIndex('account_id')
    remote_time_idx = DatabaseIndex('remote_uri', 'timestamp')
    category_idx    = DatabaseIndex('remote_uri', 'category', 'timestamp')
    link_idx        = DatabaseIndex('remote_uri', 'category', 'has_link', 'timestamp')
    read_idx        = DatabaseIndex('read')
    related_idx     = DatabaseIndex('related_msg_id')


class DownloadedFiles(SQLObject):
    class sqlmeta:
        table = 'downloaded_files'
    file_id            = StringCol()
    account_id         = UnicodeCol(length=128)
    remote_uri         = UnicodeCol(length=128)
    filename           = UnicodeCol()
    id_idx             = DatabaseIndex('file_id')
    unq_idx            = DatabaseIndex(file_id, filename, account_id, unique=True)


# Every reader of the messages table hides tombstones with this.
NOT_DELETED_SQL = '(deleted is null or deleted = 0)'


class PendingRemoval(SQLObject):
    """A message removal whose target message has not been stored yet.

    Removal notices can arrive before the message they remove (journal
    order, replication). They are kept here and applied when the target
    message is stored.
    """
    __version__ = 1

    class sqlmeta:
        table = 'pending_removals'
    message_id         = StringCol()
    account_id         = UnicodeCol(length=128)
    remote_uri         = UnicodeCol(length=128, default=None)
    removed_at         = DateTimeCol(default=None)            # when the removal was made (UTC)
    source             = StringCol(default=None)              # 'journal' or 'live'
    unq_idx            = DatabaseIndex(message_id, account_id, unique=True)


class TableVersions(object, metaclass=Singleton):
    __version__ = 1
    __versions__ = {}

    def __init__(self):
        db_file = ApplicationData.get('message_history.db')
        db_uri = f'sqlite:{db_file}'
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
        db_uri = f'sqlite:{db_file}'
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
        remote_uri = bare_instance_id(getattr(session, 'remote_instance_id', None)) or str(session.contact_uri.uri)
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
        remote_uri = bare_instance_id(getattr(session, 'remote_instance_id', None)) or str(session.contact_uri.uri)
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
        cached_file = os.path.join(ApplicationData.get('downloads'), file.file_id, filename)
        file_in_cache = os.path.exists(cached_file)
        if not file_in_cache:
            #log.info(f'== Not removing file, not present in cache: {file.file_id} {cached_file}')
            return
        log.info(f'== Removing file from cache: {file.file_id} {cached_file}')
        unlink(cached_file)
        try:
            os.rmdir(os.path.dirname(cached_file))
        except OSError:
            pass

    @run_in_thread('db')
    def remove_account_files(self, account):
        result = list(DownloadedFiles.selectBy(account_id=str(account.id)))
        for file in result:
            self.remove_cache_file(file)
            file.destroySelf()
        ActivityLog().info('[db] Removed %d downloaded files of %s' % (len(result), account.id))

    @run_in_thread('db')
    def remove_contact_files(self, account, contact):
        contact = str(contact)
        where = f'account {account.id}' if account is not None else 'all accounts'
        log.info(f'== Removing file entries and files from cache between {where} <-> {contact}')
        if account is not None:
            result = list(DownloadedFiles.selectBy(remote_uri=contact, account_id=str(account.id)))
        else:
            result = list(DownloadedFiles.selectBy(remote_uri=contact))
        for file in result:
            self.remove_cache_file(file)
            file.destroySelf()
        ActivityLog().info(f'[db] Removed {len(result)} downloaded files of the conversation with {contact} for {where}')

    @run_in_thread('db')
    def update(self, id, state):
        messages = Message.selectBy(message_id=id)
        for message in messages:
            if message.direction == 'outgoing' and state == 'received':
                continue

            if message.state != 'displayed' and message.state != state:
                log.info(f'Update {message.direction} {id} {message.state} -> {state}')
                message.state = state


@implementer(IObserver)
class MessageHistory(object, metaclass=Singleton):
    __version__ = 9
    phone_number_re = re.compile(r'^(?P<number>(0|00|\+)[1-9]\d{7,14})@')

    def __init__(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='NetworkConditionsDidChange')

        db_file = ApplicationData.get('message_history.db')
        db_uri = f'sqlite:{db_file}'
        makedirs(ApplicationData.directory)
        self._initialize(db_uri)
        self._retry_timer = QTimer()
        self._retry_timer.setInterval(60 * 1000)  # a minute (in milliseconds)
        self._retry_timer.timeout.connect(self._retry_failed_messages)
        self._retry_timer.start()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_NetworkConditionsDidChange(self, notification):
        self._retry_failed_messages()

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

        PendingRemoval._connection = self.db
        if not PendingRemoval.tableExists():
            try:
                PendingRemoval.createTable()
            except Exception as e:
                ActivityLog().error('[db] Could not create table %s: %s' % (PendingRemoval.sqlmeta.table, e))
            else:
                self.table_versions.set_version(PendingRemoval.sqlmeta.table, PendingRemoval.__version__)
                ActivityLog().info('[db] Created table %s' % PendingRemoval.sqlmeta.table)

    def _check_table_version(self):
        """Upgrade the messages table one version at a time.

        Each step is idempotent and the stored version is bumped after every
        successful step, so an interrupted upgrade resumes where it stopped.
        A failing step is logged and the upgrade stops there; it is retried
        at the next start.
        """
        table = Message.sqlmeta.table
        version = self.table_versions.version(table)
        if version is None:
            # table created by a build that did not record its version: run every step
            version = 1
        if version == self.__version__:
            ActivityLog().info('[db] Table %s is at version %d' % (table, version))
            return
        ActivityLog().info('[db] Upgrading table %s from version %d to %d' % (table, version, self.__version__))
        while version < self.__version__:
            next_version = version + 1
            step = getattr(self, '_upgrade_to_v%d' % next_version)
            started = time.monotonic()
            try:
                rows = step()
            except Exception as e:
                ActivityLog().exception('[db] Upgrade of %s to version %d failed: %s' % (table, next_version, e))
                return
            duration = time.monotonic() - started
            self.table_versions.set_version(table, next_version)
            ActivityLog().info('[db] Upgraded %s to version %d (%s) in %.2fs, %d rows changed' % (table, next_version, step.__doc__, duration, rows))
            version = next_version

    def _upgrade_to_v2(self):
        """unique index on message_id, account_id, remote_uri"""
        table = Message.sqlmeta.table
        removed = 0
        duplicates = self.db.queryAll(f'select message_id from {table} group by message_id having count(id) > 1')
        for (message_id,) in duplicates:
            for message in list(Message.selectBy(message_id=message_id))[1:]:
                message.destroySelf()
                removed += 1
        self.db.queryAll(f'CREATE UNIQUE INDEX IF NOT EXISTS messages_msg_id ON {table} (message_id, account_id, remote_uri)')
        return removed

    def _upgrade_to_v3(self):
        """remove stored PGP key lookup requests"""
        table = Message.sqlmeta.table
        content_type = 'application/sylk-api-pgp-key-lookup'
        count = self.db.queryOne(f"select count(*) from {table} where content_type='{content_type}'")[0]
        self.db.queryAll(f"delete from {table} where content_type='{content_type}'")
        return count

    def _upgrade_to_v4(self):
        """decryption state columns"""
        self._add_column('decrypted', "TEXT DEFAULT '0'")
        self._add_column('decryption_error', "LONGTEXT DEFAULT ''")
        return 0

    # columns and indexes added in version 5, see Message
    __v5_columns__ = (('read', 'INTEGER DEFAULT 1'),
                      ('category', 'TEXT DEFAULT NULL'),
                      ('has_link', 'INTEGER DEFAULT 0'),
                      ('metadata', 'LONGTEXT DEFAULT NULL'),
                      ('related_msg_id', 'TEXT DEFAULT NULL'),
                      ('related_action', 'TEXT DEFAULT NULL'),
                      ('deleted', 'INTEGER DEFAULT 0'),
                      ('deleted_time', 'INTEGER DEFAULT 0'),
                      ('journal_id', 'TEXT DEFAULT NULL'),
                      ('sip_callid', 'TEXT DEFAULT NULL'),
                      ('media_type', 'TEXT DEFAULT NULL'),
                      ('cpim_from', 'VARCHAR(128) DEFAULT NULL'),
                      ('cpim_to', 'VARCHAR(128) DEFAULT NULL'),
                      ('cpim_timestamp', 'TEXT DEFAULT NULL'),
                      ('private', 'INTEGER DEFAULT 0'),
                      ('expire_time', 'INTEGER DEFAULT 0'))
    __v5_indexes__ = (('account_idx', 'account_id'),
                      ('remote_time_idx', 'remote_uri, timestamp'),
                      ('category_idx', 'remote_uri, category, timestamp'),
                      ('link_idx', 'remote_uri, category, has_link, timestamp'),
                      ('read_idx', 'read'),
                      ('related_idx', 'related_msg_id'))

    def _upgrade_to_v5(self):
        """read state, category, tombstone, metadata and CPIM columns"""
        table = Message.sqlmeta.table
        for name, definition in self.__v5_columns__:
            self._add_column(name, definition)
        for name, columns in self.__v5_indexes__:
            # same index names sqlobject uses when it creates the table
            self.db.queryAll(f'CREATE INDEX IF NOT EXISTS {table}_{name} ON {table} ({columns})')
        return 0

    # content types that are messages a user reads (counted as unread, filed in the Messages group)
    __readable_sql__ = ("(content_type like 'text/%' and content_type not in ('text/pgp-public-key', 'text/pgp-private-key')"
                        " or content_type in ('application/sylk-file-transfer', 'application/vnd.gsma.rcs-ft-http+xml'))")

    def _upgrade_to_v6(self):
        """backfill read state, tombstones, media type and CPIM parties"""
        table = Message.sqlmeta.table
        statements = [
            # unread: incoming readable messages never displayed
            (f"read = 0 where direction = 'incoming' and state != 'displayed' and state != 'deleted' and {self.__readable_sql__}"),
            # tombstones: the old soft delete
            ("deleted = 1, deleted_time = cast(strftime('%s', 'now') as integer) where state = 'deleted' and deleted = 0"),
            # media type: SIP messages and files; call rows get theirs when converted to call records
            (f"media_type = 'sms' where media_type is null and {self.__readable_sql__}"),
            # CPIM parties by direction
            ("cpim_from = remote_uri, cpim_to = account_id where cpim_from is null and direction = 'incoming'"),
            ("cpim_from = account_id, cpim_to = remote_uri where cpim_from is null and direction = 'outgoing'"),
        ]
        changed = 0
        for statement in statements:
            assignments, where = statement.split(' where ', 1)
            count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
            self.db.queryAll(f'update {table} set {assignments} where {where}')
            ActivityLog().info(f'[db] {count} rows: set {assignments}')
            changed += count
        return changed

    __backfill_chunk__ = 500

    __key_content_types__ = ('text/pgp-public-key', 'text/pgp-private-key')
    __file_transfer_content_types__ = ('application/sylk-file-transfer', 'application/vnd.gsma.rcs-ft-http+xml')

    @classmethod
    def _readable(cls, content_type):
        """Python twin of __readable_sql__: a message a user reads."""
        content_type = str(content_type or '').lower()
        if content_type.startswith('text/'):
            return content_type not in cls.__key_content_types__
        return content_type in cls.__file_transfer_content_types__

    @classmethod
    def _initial_read(cls, direction, content_type, state=None):
        """0 for an incoming message the user has not seen yet, else 1."""
        if direction == 'incoming' and cls._readable(content_type) and state not in ('displayed', 'deleted'):
            return 0
        return 1

    def unread_counts(self):
        """{conversation key: unread incoming messages}, for enabled accounts. Caller is in the db thread."""
        table = Message.sqlmeta.table
        query = (f"select remote_uri, count(*) from {table}"
                 f" where direction = 'incoming' and read = 0 and {NOT_DELETED_SQL}"
                 f" and state != 'deleted' and {self.__readable_sql__} and {self._get_enabled_account_filter()}"
                 f" group by remote_uri")
        return {remote_uri: count for remote_uri, count in self.db.queryAll(query)}

    # Tombstones
    #
    # A removed message or conversation is hidden (deleted = 1, deleted_time =
    # when), not erased: the removal may have come from another device, files
    # on disk are untouched, and a conversation can be restored. Every reader
    # filters NOT_DELETED_SQL.

    @staticmethod
    def _storage_time(value):
        """A timestamp as stored in the timestamp column (naive UTC), or None."""
        if value is None:
            return None
        if isinstance(value, str):
            value = parse(value)
        if isinstance(value, (int, float)):
            value = datetime.fromtimestamp(value, timezone.utc)
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @staticmethod
    def _set_deleted(where, deleted, when=None):
        """Set or clear the tombstone on the rows matching `where` that change; return how many. Caller is in the db thread."""
        db = Message._connection
        table = Message.sqlmeta.table
        state = NOT_DELETED_SQL if deleted else 'deleted = 1'
        count = db.queryOne(f'select count(*) from {table} where ({where}) and {state}')[0]
        if count:
            stamp = int(when if when is not None else time.time()) if deleted else 0
            db.queryAll(f'update {table} set deleted = {1 if deleted else 0}, deleted_time = {stamp} where ({where}) and {state}')
        return count

    @classmethod
    def _tombstone_message(cls, message_id, when=None):
        """(rows, sidecars) hidden for a message: the row, rows filed against it (location
        ticks, by related_msg_id) and metadata sidecars naming it in their envelope
        (reply, label, peaks), matched in the compact JSON spelling senders use."""
        db = Message._connection
        identifier = db.sqlrepr(str(message_id))
        rows = cls._set_deleted(f'message_id = {identifier} or related_msg_id = {identifier}', True, when)
        sidecars = cls._set_deleted(f"content_type = 'application/sylk-message-metadata' and content like {db.sqlrepr('%%"messageId":"%s"%%' % message_id)}", True, when)
        return rows, sidecars

    @classmethod
    def _apply_pending_removal(cls, message_id):
        """Apply a removal that arrived before its message. Caller is in the db thread."""
        try:
            pending = list(PendingRemoval.selectBy(message_id=str(message_id)))
        except Exception as e:
            ActivityLog().error(f'[db] Reading pending removals of {message_id} failed: {e}')
            return
        for removal in pending:
            when = removal.removed_at.replace(tzinfo=timezone.utc).timestamp() if removal.removed_at else None
            rows, sidecars = cls._tombstone_message(message_id, when)
            removal.destroySelf()
            ActivityLog().info(f'[db] Applied the pending removal of message {message_id} ({removal.source or "unknown"}): {rows} rows, {sidecars} sidecars')

    @run_in_thread('db')
    def tombstone_message(self, message_id, when=None, account_id=None, remote_uri=None, source=None):
        """Hide a message and everything filed against it. A removal whose message is
        not stored yet (journal order, replication) is kept and applied on arrival."""
        try:
            rows, sidecars = self._tombstone_message(message_id, when)
        except Exception as e:
            ActivityLog().error(f'[db] Removing message {message_id} failed: {e}')
            return
        if rows or sidecars:
            ActivityLog().info(f'[db] Message {message_id} marked deleted: {rows} rows' + (f', {sidecars} sidecars' if sidecars else '') + (f' ({source})' if source else ''))
            return
        removed_at = self._storage_time(when) if when is not None else datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            if not list(PendingRemoval.selectBy(message_id=str(message_id), account_id=str(account_id or ''))):
                PendingRemoval(message_id=str(message_id), account_id=str(account_id or ''), remote_uri=str(remote_uri) if remote_uri else None,
                               removed_at=removed_at, source=source)
        except Exception as e:
            ActivityLog().error(f'[db] Keeping the removal of message {message_id} failed: {e}')
            return
        ActivityLog().info(f'[db] Message {message_id} is not stored yet, its removal is kept until it arrives' + (f' ({source})' if source else ''))

    @run_in_thread('db')
    def tombstone_conversation(self, remote_uri, before_time=None, when=None, account_id=None):
        """Hide a conversation up to the moment it was removed.

        `before_time` is when the removal was made, not when it arrived: a removal
        replayed from the journal must not hide messages exchanged after it. Without
        `account_id` every account's rows are hidden, as the conversation is shown.
        """
        db = Message._connection
        where = f'remote_uri = {db.sqlrepr(str(remote_uri))}'
        if account_id:
            where += f' and account_id = {db.sqlrepr(str(account_id))}'
        floor = self._storage_time(before_time)
        if floor is not None:
            where += f' and timestamp <= {db.sqlrepr(floor)}'
        try:
            count = self._set_deleted(where, True, when)
        except Exception as e:
            ActivityLog().error(f'[db] Removing the conversation with {remote_uri} failed: {e}')
            return
        ActivityLog().info(f'[db] Conversation with {remote_uri} marked deleted: {count} rows' + (f' up to {floor}' if floor is not None else '') + (f' for account {account_id}' if account_id else ''))

    @run_in_thread('db')
    def restore_conversation(self, remote_uri):
        """Un-hide every tombstoned row of a conversation."""
        db = Message._connection
        try:
            count = self._set_deleted(f'remote_uri = {db.sqlrepr(str(remote_uri))}', False)
        except Exception as e:
            ActivityLog().error(f'[db] Restoring the conversation with {remote_uri} failed: {e}')
            return
        ActivityLog().info(f'[db] Conversation with {remote_uri} restored: {count} rows')

    def deleted_conversations(self):
        """{conversation key: (rows, when removed)} for conversations whose every row is a
        tombstone. Derived, not stored: one live row and it is a conversation again.
        Caller is in the db thread."""
        table = Message.sqlmeta.table
        query = (f'select remote_uri, count(*), max(deleted_time) from {table}'
                 f' where deleted = 1 and remote_uri not in (select remote_uri from {table} where {NOT_DELETED_SQL})'
                 f' group by remote_uri')
        return {remote_uri: (int(count or 0), int(removed or 0)) for remote_uri, count, removed in self.db.queryAll(query) if remote_uri}

    # Paging and previews
    #
    # Plain queries for the db thread (callers come with the journal and UI
    # patches). Conversation keys are remote_uri; `accounts` narrows to account
    # ids: None means every account, an empty list means none.

    # rows that hang off another row and are never bubbles of their own
    __trail_actions__ = ('location_update', 'meeting_update')
    # how many of a conversation's newest text rows a preview looks at
    __preview_candidates__ = 5

    def _in_sql(self, column, values):
        if values is None:
            return ''
        if isinstance(values, str):
            return f' and {column} = {self.db.sqlrepr(values)}'
        values = list(values)
        if not values:
            return ' and 0'
        return f" and {column} in ({', '.join(self.db.sqlrepr(str(value)) for value in values)})"

    @staticmethod
    def _category_sql(category):
        # 'links' is text with a link in it (has_link), not a category of its own
        if not category:
            return ''
        if category == 'links':
            return " and category = 'text' and has_link = 1"
        return f" and category = {Message.sqlrepr(category)}"

    def last_message_times(self, accounts=None, include_calls=False):
        """{conversation key: newest message time} for ordering conversations."""
        query = (f'select remote_uri, max(timestamp) from {Message.sqlmeta.table}'
                 f' where {NOT_DELETED_SQL} and category is not null' + ('' if include_calls else " and category != 'call'")
                 + self._in_sql('account_id', accounts) + ' group by remote_uri')
        return {str(remote_uri): str(newest) for remote_uri, newest in self.db.queryAll(query) if remote_uri and newest}

    def last_message_accounts(self, accounts=None):
        """{conversation key: account id of its newest message}, the account a conversation continues on."""
        table = Message.sqlmeta.table
        where = f' where {NOT_DELETED_SQL} and category is not null' + self._in_sql('account_id', accounts)
        query = (f'select m.remote_uri, m.account_id from {table} m'
                 f' join (select remote_uri, max(timestamp) as newest from {table}{where} group by remote_uri) latest'
                 f' on m.remote_uri = latest.remote_uri and m.timestamp = latest.newest'
                 f' group by m.remote_uri')
        return {str(remote_uri): str(account_id) for remote_uri, account_id in self.db.queryAll(query) if remote_uri and account_id}

    def last_text_messages(self, accounts=None, remote_uri=None):
        """([{remote_uri, account_id, message_id, timestamp, content_type, content}, ...], reaction ids)

        The newest few text rows of every conversation, newest first, the
        candidates for the preview line (blink.message_envelopes.conversation_preview
        picks one; an encrypted body has to be decrypted first). Reaction ids are the
        reply ids of reply links, so a one-tap emoji reply can be passed over.
        """
        table = Message.sqlmeta.table
        where = (f" where {NOT_DELETED_SQL} and category = 'text'"
                 + self._in_sql('account_id', accounts) + self._in_sql('remote_uri', remote_uri))
        columns = 'remote_uri, account_id, message_id, timestamp, content_type, content'
        try:
            rows = self.db.queryAll(f'select {columns} from (select {columns}, row_number() over'
                                    f' (partition by remote_uri order by timestamp desc, id desc) as rn from {table}{where})'
                                    f' where rn <= {self.__preview_candidates__} order by remote_uri, timestamp desc')
        except Exception as e:
            # an SQLite without window functions (< 3.25): the newest row only
            ActivityLog().warning(f'[db] Preview query fell back to the newest message only: {e}')
            rows = self.db.queryAll(f'select m.remote_uri, m.account_id, m.message_id, m.timestamp, m.content_type, m.content from {table} m'
                                    f' join (select remote_uri, max(timestamp) as newest from {table}{where} group by remote_uri) latest'
                                    f' on m.remote_uri = latest.remote_uri and m.timestamp = latest.newest'
                                    f" where m.category = 'text' and (m.deleted is null or m.deleted = 0)")
        result = [dict(remote_uri=str(remote), account_id=str(account or ''), message_id=str(message_id or ''), timestamp=str(stamp),
                       content_type=str(content_type or ''), content=content)
                  for remote, account, message_id, stamp, content_type, content in rows if remote and stamp]
        reaction_ids = set()
        if result:
            query = (f"select content from {table} where content_type = '{METADATA_CONTENT_TYPE}' and {NOT_DELETED_SQL} and content like '%reply%'"
                     + self._in_sql('account_id', accounts) + self._in_sql('remote_uri', remote_uri))
            for (content,) in self.db.queryAll(query):
                link = reply_metadata(content)
                if link:
                    reaction_ids.add(link['reply_id'])
        return result, reaction_ids

    def present_categories(self, remote_uri, accounts=None):
        """The category filters a conversation has messages for, 'links' included."""
        where = (f' where {NOT_DELETED_SQL} and remote_uri = {self.db.sqlrepr(str(remote_uri))}' + self._in_sql('account_id', accounts))
        table = Message.sqlmeta.table
        found = {str(category) for (category,) in self.db.queryAll(f'select distinct category from {table}{where} and category is not null') if category}
        if 'text' in found and self.db.queryAll(f"select 1 from {table}{where} and category = 'text' and has_link = 1 limit 1"):
            found.add('links')
        return found

    def get_messages(self, remote_uri, before=None, after=None, category=None, limit=100, accounts=None, include_trail=False):
        """A page of a conversation, newest first: up to `limit` messages older than
        `before` (and newer than `after`), of one category if given. Location trail
        ticks are left out unless asked for (they belong to their share's bubble) and
        metadata sidecars always are."""
        query = f'remote_uri = {self.db.sqlrepr(str(remote_uri))} and {NOT_DELETED_SQL}' + self._category_sql(category) + self._in_sql('account_id', accounts)
        # sidecars (reply links, captions, waveforms) are not bubbles: they come with related_messages()
        query += f" and content_type != '{METADATA_CONTENT_TYPE}'"
        if not include_trail:
            actions = ', '.join(self.db.sqlrepr(action) for action in self.__trail_actions__)
            query += f' and (related_action is null or related_action not in ({actions}))'
        if before is not None:
            query += f' and timestamp < {self.db.sqlrepr(self._storage_time(before))}'
        if after is not None:
            query += f' and timestamp > {self.db.sqlrepr(self._storage_time(after))}'
        return list(Message.select(query, orderBy=['-timestamp', '-id'], limit=int(limit)))

    def related_messages(self, message_ids):
        """The rows filed against a page of messages (location ticks, sidecars keyed by related_msg_id)."""
        message_ids = [str(message_id) for message_id in message_ids if message_id]
        if not message_ids:
            return []
        query = f"related_msg_id in ({', '.join(self.db.sqlrepr(message_id) for message_id in message_ids)}) and {NOT_DELETED_SQL}"
        return list(Message.select(query, orderBy=['timestamp', 'id']))

    @run_in_thread('db')
    def mark_conversation_read(self, remote_uri, source=None):
        """Mark every incoming message of a conversation read, whatever account it was filed under."""
        table = Message.sqlmeta.table
        where = f"remote_uri = {self.db.sqlrepr(str(remote_uri))} and direction = 'incoming' and read = 0"
        try:
            count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
            if count:
                self.db.queryAll(f'update {table} set read = 1 where {where}')
        except Exception as e:
            ActivityLog().error(f'[db] Marking the conversation with {remote_uri} read failed: {e}')
            return
        if count:
            ActivityLog().info(f'[db] Marked {count} messages read in the conversation with {remote_uri}' + (f' (read on {source})' if source else ''))
        NotificationCenter().post_notification('BlinkMessageHistoryConversationWasRead', data=NotificationData(remote_uri=str(remote_uri), count=count))

    def _upgrade_to_v7(self):
        """backfill message categories and links"""
        table = Message.sqlmeta.table
        changed = 0

        # text and calls need no parsing: one statement each
        statements = [
            ("category = 'text' where category is null and (content_type = 'text' or content_type like 'text/%')"
             " and content_type not in ('text/pgp-public-key', 'text/pgp-private-key')"),
            (f"category = 'call' where category is null and content_type in ('{CALL_CONTENT_TYPE}', '{LEGACY_CALL_CONTENT_TYPE}')"),
        ]
        for statement in statements:
            assignments, where = statement.split(' where ', 1)
            count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
            self.db.queryAll(f'update {table} set {assignments} where {where}')
            ActivityLog().info(f"[db] {count} rows: set {assignments}")
            changed += count

        # files and locations are classified from their envelope, in chunks
        content_types = ', '.join(f"'{content_type}'" for content_type in FILE_TRANSFER_CONTENT_TYPES + (LOCATION_CONTENT_TYPE,))
        counts = {}
        unclassified = 0
        last_id = 0
        while True:
            rows = self.db.queryAll(f'select id, content_type, content, related_action, metadata from {table}'
                                    f' where id > {last_id} and category is null and content_type in ({content_types})'
                                    f' order by id limit {self.__backfill_chunk__}')
            if not rows:
                break
            last_id = rows[-1][0]
            by_category = {}
            for row_id, content_type, content, related_action, metadata in rows:
                category = classify_category(content_type, content, related_action, metadata)
                if category is None:
                    unclassified += 1  # an encrypted envelope, classified when it is decrypted
                else:
                    by_category.setdefault(category, []).append(row_id)
            for category, ids in by_category.items():
                self.db.queryAll(f"update {table} set category = '{category}' where id in ({', '.join(map(str, ids))})")
                counts[category] = counts.get(category, 0) + len(ids)
        for category, count in sorted(counts.items()):
            ActivityLog().info(f"[db] {count} rows: set category = '{category}'")
            changed += count
        if unclassified:
            ActivityLog().info(f'[db] {unclassified} file or location rows left unclassified (encrypted or unreadable envelope)')

        # links in plain text and HTML; an encrypted body is unknown (null) until decrypted.
        # Only rows still at the default 0 are looked at, so a re-run changes nothing.
        linked = encrypted = 0
        last_id = 0
        while True:
            rows = self.db.queryAll(f"select id, content_type, content from {table}"
                                    f" where id > {last_id} and category = 'text' and has_link = 0 and content_type in ('text/plain', 'text/html')"
                                    f" order by id limit {self.__backfill_chunk__}")
            if not rows:
                break
            last_id = rows[-1][0]
            with_link, unknown = [], []
            for row_id, content_type, content in rows:
                value = has_link(content_type, content)
                if value is None:
                    unknown.append(str(row_id))
                elif value:
                    with_link.append(str(row_id))
            if with_link:
                self.db.queryAll(f"update {table} set has_link = 1 where id in ({', '.join(with_link)})")
            if unknown:
                self.db.queryAll(f"update {table} set has_link = null where id in ({', '.join(unknown)})")
            linked += len(with_link)
            encrypted += len(unknown)
        ActivityLog().info(f'[db] {linked} rows: set has_link = 1, {encrypted} encrypted rows: set has_link = null')
        changed += linked + encrypted

        return changed

    def _upgrade_to_v8(self):
        """convert call history rows to call detail records"""
        table = Message.sqlmeta.table
        converted = unreadable = 0
        last_id = 0
        while True:
            rows = self.db.queryAll(f"select id, message_id, direction, timestamp, remote_uri, display_name, content from {table}"
                                    f" where id > {last_id} and content_type = '{LEGACY_CALL_CONTENT_TYPE}'"
                                    f" order by id limit {self.__backfill_chunk__}")
            if not rows:
                break
            last_id = rows[-1][0]
            for row_id, message_id, direction, timestamp, remote_uri, display_name, content in rows:
                record = legacy_call_record(content, direction, message_id, timestamp=timestamp, remote_party=remote_uri,
                                            display_name=display_name if display_name != remote_uri else '')
                if record is None:
                    unreadable += 1
                    continue
                self.db.queryAll(f"update {table} set content_type = {self.db.sqlrepr(CALL_CONTENT_TYPE)},"
                                 f" content = {self.db.sqlrepr(call_summary(record) or '')},"
                                 f" metadata = {self.db.sqlrepr(json.dumps(record))},"
                                 f" category = 'call', media_type = {self.db.sqlrepr(dominant_media(record.get('media')))}"
                                 f" where id = {row_id}")
                converted += 1
        ActivityLog().info(f'[db] {converted} call history rows converted to call detail records')
        if unreadable:
            ActivityLog().warning(f'[db] {unreadable} call history rows could not be read and were left as they are')
        return converted

    def _upgrade_to_v9(self):
        """key Bonjour conversations by the bare neighbour instance id"""
        # Older builds filed a neighbour under '<instance id>@local', and under
        # 'urn:uuid:<instance id>@local' when the conversation was opened from
        # the contact list: two conversations for one neighbour. Both become the
        # bare id, filed under the Bonjour account, as on macOS.
        table = Message.sqlmeta.table
        moved = duplicates = 0
        keys = self.db.queryAll(f"select distinct remote_uri from {table}"
                                f" where (remote_uri like '%@local' or remote_uri like 'urn:uuid:%') and remote_uri != '{BONJOUR_ACCOUNT_ID}'")
        for (old_key,) in keys:
            new_key = bare_instance_id(old_key[:-len('@local')] if old_key.endswith('@local') else old_key)
            if new_key == old_key or not is_instance_id(new_key):
                continue
            count = self.db.queryOne(f'select count(*) from {table} where remote_uri = {self.db.sqlrepr(old_key)}')[0]
            # a message stored under both spellings is kept once
            self.db.queryAll(f"update or ignore {table} set remote_uri = {self.db.sqlrepr(new_key)}, account_id = '{BONJOUR_ACCOUNT_ID}'"
                             f" where remote_uri = {self.db.sqlrepr(old_key)}")
            left = self.db.queryOne(f'select count(*) from {table} where remote_uri = {self.db.sqlrepr(old_key)}')[0]
            self.db.queryAll(f'delete from {table} where remote_uri = {self.db.sqlrepr(old_key)}')
            ActivityLog().info(f'[db] Bonjour conversation {old_key} moved to {new_key}: {count - left} messages, {left} duplicates removed')
            moved += count - left
            duplicates += left
        return moved + duplicates

    def _add_column(self, name, definition):
        try:
            self.db.queryAll(f'ALTER TABLE {Message.sqlmeta.table} ADD COLUMN {name} {definition}')
        except dberrors.OperationalError as e:
            if 'duplicate column name' not in str(e):
                raise

    def _get_enabled_account_filter(self, prefix=None):
        account_manager = AccountManager()
        enabled_accounts = [account.id for account in account_manager.iter_accounts() if account.enabled]
        table = f"{prefix}.account_id" if prefix else "account_id"

        return f"{table} IN ({','.join([repr(account) for account in enabled_accounts])})"

    @run_in_thread('db')
    def _retry_failed_messages(self):
        if host.default_ip is None:
            return

        messages = Message.selectBy(state='failed-local')
        if len(list(messages)) > 0:
            log.debug(f"==  {len(list(messages))} failed local messages from history")
            NotificationCenter().post_notification('BlinkMessageHistoryFailedLocalFound', data=NotificationData(messages=list(messages)))

    @classmethod
    @run_in_thread('db')
    def add_call_history_entry(cls, entry, session, status=None, failure_reason=None):
        """Store a call as a call detail record (application/blink-call-detail-record)."""
        timestamp_native = entry.call_time
        timestamp_utc = timestamp_native.replace(tzinfo=timezone.utc)
        timestamp_fixed = timestamp_utc - entry.call_time.utcoffset()
        timestamp = parse(str(timestamp_fixed))

        if not session.streams and not session.proposed_streams:
            return

        streams = [stream.type for stream in session.streams] if session.streams else [stream.type for stream in session.proposed_streams]
        if 'audio' not in streams and 'video' not in streams:
            return

        duration = int(entry.duration.total_seconds()) if entry.duration else 0
        if duration > 0:
            outcome = 'completed'
        elif entry.direction == 'incoming':
            # not failed: cancelled with "Call completed elsewhere"
            outcome = 'missed' if entry.failed else 'answered_elsewhere'
        elif status == 487:
            outcome = 'cancelled'
        elif entry.failed:
            outcome = 'failed'
        else:
            outcome = 'cancelled'

        invitation = getattr(session, '_invitation', None)
        call_id = getattr(invitation, 'call_id', None)
        call_id = call_id.decode() if isinstance(call_id, bytes) else call_id
        stop_time = entry.call_time + entry.duration if entry.duration else None
        device_id = this_device_id()
        local = {'streams': streams}
        if device_id:
            local['deviceId'] = device_id
        record = build_call_record(call_id or str(uuid.uuid4()), entry.direction, outcome, duration=duration,
                                   status=status if status else None,
                                   reason=(entry.reason or failure_reason) if outcome == 'failed' else None,
                                   remote_party=str(entry.uri), display_name=entry.name or '',
                                   start_time=entry.call_time, stop_time=stop_time, media=streams,
                                   source='local', local=local,
                                   answered_by=device_id if outcome == 'completed' and entry.direction == 'incoming' else None)
        media_type = dominant_media(streams)

        log.info(f"== Adding call detail record to storage: {entry.direction} {media_type} {outcome} to {entry.uri}")

        try:
            message = Message(remote_uri=entry.uri,
                              display_name=entry.name,
                              uri=str(entry.uri),
                              content=call_summary(record) or '',
                              content_type=CALL_CONTENT_TYPE,
                              metadata=json.dumps(record),
                              category='call',
                              media_type=media_type,
                              sip_callid=call_id,
                              message_id=str(uuid.uuid4()),
                              account_id=str(entry.account_id),
                              direction=entry.direction,
                              timestamp=timestamp,
                              decrypted='0',
                              decryption_error='',
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
        optional_fields['read'] = cls._initial_read(message.direction, message.content_type, state)

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
                    decrypted='0',
                    decryption_error='',
                    disposition=str(message.disposition),
                    **optional_fields)
        except dberrors.DuplicateEntryError:
            pass
        else:
            cls._apply_pending_removal(message.id)
            if message.content_type not in {IsComposingDocument.content_type, IMDNDocument.content_type, 'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-message-remove'}:
                notification_center = NotificationCenter()
                notification_center.post_notification('BlinkMessageHistoryMessageDidStore', sender=account, data=NotificationData(remote_uri=remote_uri, state=state, direction=message.direction))

    @classmethod
    @run_in_thread('db')
    def add_from_session(cls, session, message, direction, state=None):
        if message.content.startswith('?OTRv'):
            return

        if session.remote_instance_id:
            remote_uri = bare_instance_id(session.remote_instance_id)  # a Bonjour neighbour, keyed by its instance id
        else:
            user = session.uri.user
            domain = session.uri.host

            user = user.decode() if isinstance(user, bytes) else user
            domain = domain.decode() if isinstance(domain, bytes) else domain

            remote_uri = '%s@%s' % (user, domain)
            # a neighbour who is away is addressed by placeholder; the conversation is its instance id
            remote_uri = placeholder_instance_id(remote_uri) or remote_uri
            match = cls.phone_number_re.match(remote_uri)
            if match:
                remote_uri = match.group('number')

        if direction == 'outgoing':
            display_name = message.sender.display_name
        else:
            try:
                contact = next(contact for contact in AddressbookManager().get_contacts() if remote_uri in (addr.uri for addr in contact.uris))
            except StopIteration:
                display_name = message.sender.display_name
            else:
                display_name = contact.name if contact.name != remote_uri else message.sender.display_name

        timestamp_native = message.timestamp
        timestamp_utc = timestamp_native.replace(tzinfo=timezone.utc)
        timestamp_fixed = timestamp_utc - message.timestamp.utcoffset()
        timestamp = parse(str(timestamp_fixed))

        optional_fields = {}
        if state is not None:
            optional_fields['state'] = state
        optional_fields['read'] = cls._initial_read(direction, message.content_type, state)
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
                    decrypted='0',
                    decryption_error='',
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
            if direction == 'outgoing':
                log.info(f"Message {message.id} to {remote_uri} stored")
            else:
                log.info(f"Message {message.id} from {remote_uri} stored")
            cls._apply_pending_removal(message.id)

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
                if message.direction == 'outgoing':
                    log.info(f'Message {id} to {message.remote_uri} state changed {message.state} -> {state}')
                else:
                    log.info(f'Message {id} from {message.remote_uri} state changed {message.state} -> {state}')
                message.state = state
            if state == 'displayed' and message.direction == 'incoming' and not message.read:
                message.read = 1
            if state == 'deleted' and not message.deleted:
                message.deleted = 1
                message.deleted_time = int(time.time())

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
            pass
            # log.info('Conversation with %s read saved to history' % remote_uri)

    @run_in_thread('db')
    def reset_decryption(self, account):
        query = f"""
            update messages set decrypted = '3', decryption_error = ''
            where account_id = {Message.sqlrepr(account)} and decrypted = '2'
            """
        try:
            result = self.db.queryAll(query)
        except Exception as e:
            print('SQL Error: %s' % str(e))
        else:
            notification_center = NotificationCenter()
            notification_center.post_notification('BlinkMessageHistoryMustReload', data=NotificationData(account=account))

    @run_in_thread('db')
    def update_encryption(self, notification, decrypted=None):
        message = notification.data.message
        session = notification.sender
        message_info = session.info.streams.messages

        try:
            message_id = message.message_id
        except AttributeError:
            # is a BlinkMessage from active session
            message_id = message.id

        if message_info.encryption is not None and message.is_secure:
            db_messages = Message.selectBy(message_id=message_id)
            for db_message in db_messages:
                encryption_type = str(f'{message_info.encryption}')
                if db_message.encryption_type != encryption_type:
                    db_message.encryption_type = encryption_type

                # 0 not encrypted message
                # 1 decrypted incoming messages
                # 2 failed to decrypt incoming messages
                decrypted_sql = '1' if decrypted else '2'
                if decrypted is not None:
                    # log.debug(f'Update {message.direction} {message_id} decrypted to {decrypted_sql}')
                    db_message.decrypted = decrypted_sql
                    if not decrypted:
                        db_message.decryption_error = notification.data.error

    @run_in_thread('db')
    def load(self, uri, session, entries=100):
        notification_center = NotificationCenter()
        remote_uri = bare_instance_id(session.remote_instance_id) if session.remote_instance_id else str(uri)
        try:
            query = Message.select(AND(Message.q.remote_uri == remote_uri, Message.q.state != 'deleted', OR(Message.q.deleted == None, Message.q.deleted == 0)))
            total = query.count()
            result = list(query.orderBy('timestamp')[-entries:])
        except Exception as e:
            ActivityLog().error(f'[Message with {remote_uri}] Loading the conversation from history failed: {e}')
            notification_center.post_notification('BlinkMessageHistoryLoadDidFail', sender=session, data=NotificationData(uri=uri))
            return
        ActivityLog().info(f'[Message with {remote_uri}] Loaded {len(result)} of {total} messages from history')
        notification_center.post_notification('BlinkMessageHistoryLoadDidSucceed', sender=session, data=NotificationData(messages=list(result), uri=uri))

    @run_in_thread('db')
    def reload_pending_encrypted(self, uri, session, entries=100):
        notification_center = NotificationCenter()
        remote_uri = bare_instance_id(session.remote_instance_id) if session.remote_instance_id else str(uri)
        try:
            result = Message.select(AND(Message.q.remote_uri == remote_uri, Message.q.state != 'deleted', OR(Message.q.deleted == None, Message.q.deleted == 0), Message.q.decrypted == '3')).orderBy('timestamp')[-entries:]
        except Exception as e:
            return
        log.debug(f"== ReLoaded {len(list(result))} messages for {remote_uri} from history")
        notification_center.post_notification('BlinkMessageHistoryLoadDidSucceed', sender=session, data=NotificationData(messages=list(result), uri=uri))

    @run_in_thread('db')
    def get_last_contacts(self, number=25, unread=False):
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
                and am.content_type not in ("application/blink-call-history", "application/blink-call-detail-record")
                and am.read = 0
                and am.state != 'deleted' and (am.deleted is null or am.deleted = 0)
                and {self._get_enabled_account_filter('am')}
                group by am.remote_uri order by am.timestamp desc"""
        else:
            query = f"""
                select im.display_name, am.remote_uri, max(am.timestamp) from messages as am
                left join (select display_name, remote_uri from messages where direction="incoming" group by remote_uri) as im
                on am.remote_uri = im.remote_uri
                where
                am.content_type not like "%pgp%"
                and am.content_type not like "%sylk-api%"
                and am.content_type not in ("application/blink-call-history", "application/blink-call-detail-record")
                and am.state != 'deleted' and (am.deleted is null or am.deleted = 0)
                and {self._get_enabled_account_filter('am')}
                group by am.remote_uri order by am.timestamp desc limit {Message.sqlrepr(number)}"""

        notification_center = NotificationCenter()
        try:
            result = self.db.queryAll(query)
        except Exception as e:
            return

        #log.debug(f"== Contacts fetched: {len(list(result))}")
        #result = [(display_name, uri) for (display_name, uri, timestamp) in result]
        #results = list(result)

        results = []
        for r in result:
            # find the display name sent by remote party
            display_name = r[0]
            uri = r[1]
            timestamp = r[2]
            query = f"""select display_name from messages
                where remote_uri = '{uri}'
                and direction = 'incoming' and remote_uri != display_name"""
            try:
                c = self.db.queryAll(query)
            except Exception as e:
                pass
            else:
                if len(list(c)) > 0:
                    display_name = list(c)[0][0]

            results.append((display_name, uri, timestamp))

        results = sorted(results, key=lambda tup: tup[2])
        results.reverse()
        notification_center.post_notification('BlinkMessageHistoryLastContactsDidSucceed', data=NotificationData(contacts=results))

    @run_in_thread('db')
    def get_unread_messages(self):
        try:
            unread_messages = self.unread_counts()
        except Exception as e:
            ActivityLog().error(f'[db] Counting unread messages failed: {e}')
            return

        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageHistoryUnreadMessagesDidLoad', data=NotificationData(unread_messages=unread_messages))

    @run_in_thread('db')
    def get_all_contacts(self):
        log.debug('== Getting all contacts with messages')

        query = f"""
            select im.display_name, am.remote_uri from messages as am
            left join (select display_name, remote_uri from messages where direction='incoming' group by remote_uri) as im
            on (am.remote_uri = im.remote_uri)
            where
            am.content_type not like '%pgp%'
            and not am.content_type like '%sylk-api%'
            and am.content_type not in ("application/blink-call-history", "application/blink-call-detail-record")
            and am.state != 'deleted' and (am.deleted is null or am.deleted = 0)
            and {self._get_enabled_account_filter('am')}
            group by am.remote_uri
            """

        notification_center = NotificationCenter()
        try:
            result = self.db.queryAll(query)
        except Exception as e:
            return

        results = []
        for r in result:
            # find the display name sent by remote party
            display_name = r[0]
            uri = r[1]
            query = f"""select display_name from messages
                where remote_uri = '{uri}'
                and direction = 'incoming' and remote_uri != display_name"""
            try:
                c = self.db.queryAll(query)
            except Exception as e:
                pass
            else:
                if len(list(c)) > 0:
                    display_name = list(c)[0][0]

            results.append((display_name, uri))

        log.debug(f"== Contacts fetched: {len(list(result))}")
        notification_center.post_notification('BlinkMessageHistoryAllContactsDidSucceed', data=NotificationData(contacts=results))

    @run_in_thread('db')
    def remove(self, account):
        account_id = str(account.id)
        count = Message.selectBy(account_id=account_id).count()
        Message.deleteBy(account_id=account_id)
        ActivityLog().info('[db] Removed %d messages of %s' % (count, account_id))

    @run_in_thread('db')
    def remove_contact_messages(self, account, contact, timestamp=None, session=None):
        if not timestamp:
            timestamp = ISOTimestamp.now()

        timestamp_native = timestamp
        timestamp_utc = timestamp_native.replace(tzinfo=timezone.utc)
        timestamp_fixed = timestamp_utc - timestamp.utcoffset()
        timestamp = parse(str(timestamp_fixed))

        contact = str(contact)
        log.info(f'== Removing conversation between {account.id} <-> {contact} < {timestamp}')
        result = Message.selectBy(remote_uri=contact, account_id=str(account.id))
        removed = kept = 0
        for message in result:
            if message.timestamp.replace(tzinfo=timezone.utc) <= timestamp:
                message.destroySelf()
                removed += 1
            else:
                kept += 1
        ActivityLog().info(f'[db] Removed {removed} messages of the conversation with {contact} for account {account.id}' + (f', kept {kept} newer than {timestamp}' if kept else ''))
        if session:
            self.load(contact, session)


    @run_in_thread('db')
    def remove_conversation(self, contact, timestamp=None, session=None):
        """Remove a conversation whatever account its messages were filed under."""
        contact = str(contact)
        if not timestamp:
            timestamp = ISOTimestamp.now()
        timestamp_utc = timestamp.replace(tzinfo=timezone.utc)
        timestamp = parse(str(timestamp_utc - timestamp.utcoffset()))

        removed = {}
        kept = 0
        for message in Message.selectBy(remote_uri=contact):
            if message.timestamp.replace(tzinfo=timezone.utc) <= timestamp:
                removed[message.account_id] = removed.get(message.account_id, 0) + 1
                message.destroySelf()
            else:
                kept += 1
        for account_id, count in sorted(removed.items()):
            ActivityLog().info(f'[db] Removed {count} messages of the conversation with {contact} for account {account_id}')
        if not removed:
            ActivityLog().info(f'[db] Removed 0 messages of the conversation with {contact}')
        if kept:
            ActivityLog().info(f'[db] Kept {kept} messages of the conversation with {contact} newer than {timestamp}')
        if session:
            self.load(contact, session)
        NotificationCenter().post_notification('BlinkMessageHistoryConversationDidRemove', data=NotificationData(contact=contact, accounts=sorted(removed)))

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
