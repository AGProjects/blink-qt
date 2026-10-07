
import bisect
import glob
import json
import pickle as pickle
import os
import re
import threading
import time
import uuid

from collections import Counter
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from application.system import host, makedirs, unlink

from datetime import date, datetime, timedelta, timezone
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
from blink.journal import FIRST_SYNC_MARKER
from blink.logging import ActivityLog, JournalLog, MessagingTrace as log
from blink.message_envelopes import FILE_TRANSFER_CONTENT_TYPE, FILE_TRANSFER_CONTENT_TYPES, LOCATION_CONTENT_TYPE, CALL_CONTENT_TYPE, LEGACY_CALL_CONTENT_TYPE, classify_category, has_link
from blink.message_envelopes import build_call_record, call_record, call_summary, dominant_media, legacy_call_record, merge_call_records, this_device_id
from blink.message_envelopes import METADATA_CONTENT_TYPE, metadata_link, reply_metadata
from blink.message_envelopes import conversation_preview, is_pgp_armoured
from blink.location import storage_fields as location_storage_fields
from blink.messages import BlinkMessage
from blink.resources import ApplicationData, Resources
from blink.sessions import BlinkSession

from blink.uris import BONJOUR_ACCOUNT_ID, bare_instance_id, canonical_uri, is_instance_id, placeholder_instance_id
from blink.util import call_in_gui_thread, call_later, run_in_gui_thread, translate
import traceback

from sqlobject import SQLObject, StringCol, DateTimeCol, IntCol, UnicodeCol, DatabaseIndex, AND, OR
from sqlobject import connectionForURI
from sqlobject import dberrors

__all__ = ['HistoryManager', 'ConversationPreviews', 'ConversationTyping']


@implementer(IObserver)
class HistoryManager(object, metaclass=Singleton):

    history_size = 20
    sip_prefix_re = re.compile('^sips?:')

    def __init__(self):
        self.calls = []
        self.message_history = MessageHistory()
        self.download_history = DownloadHistory()
        ConversationPreviews()
        ConversationTyping()
        ConversationLocations()

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
        notification_center.add_observer(self, name='BlinkGotHistoryCallRecord')
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
        notification_center.add_observer(self, name='BlinkJournalDidApply')
        notification_center.add_observer(self, name='BlinkConfirmReadMessagesOnOtherDevice')
        notification_center.add_observer(self, name='AddressbookContactWasDeleted')

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
        self.message_history.rekey_conversations()      # once, now that the accounts' dial rules are known
        self.message_history.drop_file_transfer_notices()
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

    def _NH_BlinkGotHistoryCallRecord(self, notification):
        data = notification.data
        self.message_history.store_call_record(notification.sender, data.record, message_id=data.message_id, origin=data.origin)

    def _NH_BlinkGotHistoryMessageDelete(self, notification):
        # removed on another device: hidden, not erased (the file stays on disk). A removal
        # that comes before its message is kept and applied when the message is stored
        data = notification.data
        account_id = str(notification.sender.id) if isinstance(notification.sender, (Account, BonjourAccount)) else None
        self.message_history.tombstone_message(data.message_id, when=data.timestamp, account_id=account_id,
                                               remote_uri=data.remote_uri, source=data.source)
        settings = BlinkSettings()
        if settings.interface.show_messages_group:
            self.message_history.get_all_contacts()

    def _NH_BlinkGotHistoryConversationRemove(self, notification):
        # removed on another device: hidden up to the removal time, not erased
        data = notification.data
        self.message_history.tombstone_conversation(str(data.contact), before_time=data.timestamp, account_id=str(notification.sender.id))
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
            # removed on another device while the conversation is open: hidden up to the removal time
            self.message_history.tombstone_conversation(contact, before_time=data.timestamp, account_id=str(notification.sender.account.id), session=notification.sender)
        settings = BlinkSettings()
        if settings.interface.show_messages_group:
            self.message_history.get_all_contacts()

    def _NH_BlinkJournalDidApply(self, notification):
        # after a journal run: unread counts and the Messages group come from history,
        # and the database is counted (queued after the run's writes on the db thread); after a
        # first sync the read state is settled first, which also ends the first sync (its marker)
        if getattr(notification.data, 'first_sync', False):
            self.message_history.settle_first_sync_read(str(notification.sender.id), marker=getattr(notification.data, 'first_sync_marker', None))
        self.message_history.get_unread_messages()
        self.message_history.log_unread(str(notification.sender.id))
        if BlinkSettings().interface.show_messages_group:
            self.message_history.get_all_contacts()
        self.message_history.journal_db_check(str(notification.sender.id), getattr(notification.data, 'stats_path', None))

    def _NH_BlinkSessionConfirmReadMessages(self, notification):
        # the user has the conversation in front of them; keyed as the chat window loads it
        session = notification.sender
        key = bare_instance_id(session.remote_instance_id) if session.remote_instance_id else str(session.contact.uri.uri)
        self.message_history.mark_conversation_read(key)

    def _NH_AddressbookContactWasDeleted(self, notification):
        # Removed on another device (or by applying the server document): the history stays,
        # it may still be wanted here. Deleted here: the conversations under its addresses go,
        # but only those no other contact still claims, and never a Bonjour neighbour's.
        contact = notification.sender
        name = getattr(contact, 'name', None) or contact.id
        try:
            addresses = [str(uri.uri) for uri in contact.uris]
        except Exception:
            addresses = []
        if getattr(notification.data, 'remote', False):
            ActivityLog().info(f'[db] Contact {name} ({contact.id}) was removed on another device, its history is kept ({len(addresses)} addresses)')
            return
        keys = {conversation_key(address) for address in addresses}
        keys = {key for key in keys if key and not is_instance_id(key)}
        claimed = set()
        for other in AddressbookManager().get_contacts():
            if other.id == contact.id:
                continue
            claimed.update(conversation_key(str(uri.uri)) for uri in other.uris)
        kept = sorted(keys & claimed)
        purge = sorted(keys - claimed)
        ActivityLog().info(f'[db] Contact {name} ({contact.id}) deleted on this device: removing the history of {", ".join(purge) or "no address"}' +
                           (f', keeping {", ".join(kept)} (another contact has it)' if kept else ''))
        if purge:
            self.message_history.purge_conversations(purge, reason=f'contact {name} deleted')

    def _NH_BlinkConfirmReadMessagesOnOtherDevice(self, notification):
        data = notification.data
        self.message_history.mark_conversation_read(str(data.remote_uri), source='another device', before_time=getattr(data, 'timestamp', None))

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


class MessageAgent(SQLObject):
    """Which client sent a message, or answered it with a receipt, as the SIP request said.

    kind is 'sent' for the message itself, else the receipt's status (delivered,
    displayed, error, ...); the newest of each is kept. user_agent is the client's
    (X-Sylk-User-Agent when a SylkServer relayed it, else the SIP User-Agent), relay
    the SIP User-Agent of the relay when there was one.
    """
    __version__ = 1

    class sqlmeta:
        table = 'message_agents'
    message_id         = StringCol()
    kind               = StringCol()
    user_agent         = UnicodeCol(length=255, default=None)
    relay              = UnicodeCol(length=255, default=None)
    received_at        = DateTimeCol(default=None)            # UTC
    unq_idx            = DatabaseIndex(message_id, kind, unique=True)


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


def conversation_key(raw_uri, account=None):
    """The key a conversation is filed under: one for every spelling of a party.

    A Bonjour neighbour (an instance id, or the placeholder standing in for one)
    is its bare instance id, as it is stored; anything else is
    blink.uris.canonical_uri: no scheme or parameters, lowercased, a phone
    number in E.164 by the account's dial rules (0031..., +31..., 020... and
    +31...@domain are one conversation), a withheld caller one address.
    """
    text = str(raw_uri or '').strip()
    if not text:
        return text
    instance_id = placeholder_instance_id(text) or (bare_instance_id(text) if is_instance_id(bare_instance_id(text)) else None)
    if instance_id:
        return instance_id
    return canonical_uri(text, account) or text


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
        remote_uri = bare_instance_id(getattr(session, 'remote_instance_id', None)) or conversation_key(session.contact_uri.uri, session.account)
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
        remote_uri = bare_instance_id(getattr(session, 'remote_instance_id', None)) or conversation_key(session.contact_uri.uri, session.account)
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
        # file_transfers/<account>/<peer>/<id>/ (or downloads/<id>/ before that layout): the folder the file was stored in
        folder = os.path.dirname(file.filename) if os.path.isabs(file.filename) else os.path.join(ApplicationData.get('downloads'), file.file_id)
        cached_file = os.path.join(folder, filename)
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
            if message.direction == 'outgoing' and state == 'error' and message.state in ('delivered', 'displayed'):
                # the peer's devices answer each on its own: one that cannot show it does not undo one that did
                log.info(f'Message {id} to {message.remote_uri} error disposition ignored, already {message.state}')
                continue

            if message.state != 'displayed' and message.state != state:
                log.info(f'Update {message.direction} {id} {message.state} -> {state}')
                message.state = state


@implementer(IObserver)
class MessageHistory(object, metaclass=Singleton):
    __version__ = 11
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

        MessageAgent._connection = self.db
        if not MessageAgent.tableExists():
            try:
                MessageAgent.createTable()
            except Exception as e:
                ActivityLog().error('[db] Could not create table %s: %s' % (MessageAgent.sqlmeta.table, e))
            else:
                self.table_versions.set_version(MessageAgent.sqlmeta.table, MessageAgent.__version__)
                ActivityLog().info('[db] Created table %s' % MessageAgent.sqlmeta.table)

        self._vacuum_if_needed()

    @run_in_thread('db')
    def record_agent(self, message_id, kind, user_agent, relay=None):
        """Remember the client that sent a message (kind 'sent') or a receipt of it (kind: its status)."""
        if not message_id or not user_agent:
            return
        try:
            rows = list(MessageAgent.selectBy(message_id=message_id, kind=kind))
            if rows:
                rows[0].set(user_agent=user_agent, relay=relay, received_at=datetime.now(timezone.utc).replace(tzinfo=None))
            else:
                MessageAgent(message_id=message_id, kind=kind, user_agent=user_agent, relay=relay, received_at=datetime.now(timezone.utc).replace(tzinfo=None))
        except Exception as e:
            log.warning(f'Cannot store the user agent of {kind} {message_id}: {e!r}')

    def agents(self, message_id):
        """[(kind, user agent, relay, received at)] of a message, oldest first. In the db thread."""
        try:
            rows = MessageAgent.selectBy(message_id=message_id).orderBy('received_at')
            return [(row.kind, row.user_agent, row.relay, row.received_at) for row in rows]
        except Exception as e:
            log.warning(f'Cannot read the user agents of {message_id}: {e!r}')
            return []

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

    @staticmethod
    def _related_fields(content_type, content, metadata=None):
        """related_msg_id and related_action of a metadata companion (reply, label,
        peaks, call recording): filed against its message, never a bubble or unread.
        For a location tick also category and metadata (blink.location.storage_fields):
        filed against its share, update ticks being the share's trail."""
        if content_type == LOCATION_CONTENT_TYPE:
            try:
                return location_storage_fields(content, metadata, content_type)
            except Exception as e:
                log.warning(f'Location message could not be classified: {e!r}')
                return {}
        if content_type != METADATA_CONTENT_TYPE:
            return {}
        link = metadata_link(content)
        if link is None:
            return {}
        return {'related_msg_id': link[0], 'related_action': link[1]}

    @classmethod
    def _stored_location(cls, message_id, fields, remote_uri):
        """After a location tick is stored: log it (trail ticks only at debug level,
        they come every few seconds). Caller is in the db thread."""
        action = fields.get('related_action')
        # a share's bubble draws its trail: tell it (blink.messagepane.locations)
        NotificationCenter().post_notification('BlinkMessageHistoryLocationDidStore',
                                               data=NotificationData(remote_uri=remote_uri, message_id=message_id, action=action, session_id=fields.get('related_msg_id') or message_id))
        if action is None:
            ActivityLog().info(f'[db] Location message {message_id} with {remote_uri} stored, it cannot be read without decrypting')
        elif action in cls.__trail_actions__:
            log.debug(f'Location {action} {message_id} stored in the trail of share {fields.get("related_msg_id")}')
        else:
            ActivityLog().info(f'[db] Location {action} {message_id} with {remote_uri} stored for share {fields.get("related_msg_id")}')

    @classmethod
    def _stored_companion(cls, message_id, fields):
        """After a metadata companion is stored: log it, and hide it at once if its
        message was removed already. Caller is in the db thread."""
        target = fields.get('related_msg_id')
        if not target:
            log.debug(f'Metadata message {message_id} stored, it names no message it belongs to')
            return
        db = Message._connection
        removed = db.queryOne(f'select deleted_time from {Message.sqlmeta.table} where message_id = {db.sqlrepr(target)} and deleted = 1')
        if removed is not None:
            cls._set_deleted(f'message_id = {db.sqlrepr(str(message_id))}', True, removed[0] or None)
        # one line per companion is a flood during a journal import: the messaging trace has them
        log.debug(f'Metadata message {message_id} ({fields["related_action"]}) stored for message {target}' + (', which was removed: hidden too' if removed is not None else ''))
        NotificationCenter().post_notification('BlinkMessageHistoryCompanionDidStore', data=NotificationData(message_id=str(message_id), related_msg_id=str(target), related_action=fields['related_action']))

    @staticmethod
    def _content_fields(content_type, content):
        """category and has_link for a new row, from its cleartext (an encrypted body
        gets them when it is decrypted, update_decrypted_message)."""
        fields = {}
        try:
            category = classify_category(content_type, content)
        except Exception:
            category = None
        if category is not None:
            fields['category'] = category
        if category == 'text':
            link = has_link(content_type, content)
            fields['has_link'] = link
        return fields

    @classmethod
    def _initial_read(cls, direction, content_type, state=None):
        """0 for an incoming message the user has not seen yet, else 1."""
        if direction == 'incoming' and cls._readable(content_type) and state not in ('displayed', 'deleted'):
            return 0
        return 1

    @staticmethod
    def first_sync_accounts():
        """Ids of the accounts whose first journal sync is not finished (journal/<account>/first-sync.marker)."""
        directory = ApplicationData.get('journal')
        return [str(account.id) for account in AccountManager().iter_accounts()
                if account is not BonjourAccount() and os.path.exists(os.path.join(directory, str(account.id), FIRST_SYNC_MARKER))]

    def unread_counts(self):
        """{conversation key: unread incoming messages}, for enabled accounts. Caller is in the db thread.

        An account in its first sync counts nothing: its messages arrive unread and are
        settled at the end (settle_first_sync_read), a count before that means nothing.
        """
        table = Message.sqlmeta.table
        syncing = self.first_sync_accounts()
        query = (f"select remote_uri, count(*) from {table}"
                 f" where direction = 'incoming' and read = 0 and {NOT_DELETED_SQL}"
                 f" and state != 'deleted' and {self.__readable_sql__} and {self._get_enabled_account_filter()}"
                 + (f" and account_id not in ({', '.join(self.db.sqlrepr(account_id) for account_id in syncing)})" if syncing else '')
                 + f" group by remote_uri")
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
        # a plain datetime: sqlrepr knows no subclass (ISOTimestamp from the journal or a live removal)
        return datetime(value.year, value.month, value.day, value.hour, value.minute, value.second, value.microsecond)

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
        not stored yet (journal order, replication) is kept and applied on arrival.
        `when` is when the removal was made: epoch seconds, a datetime or an ISO string."""
        try:
            removed_at = self._storage_time(when)
        except (ValueError, OverflowError):
            ActivityLog().warning(f'[db] Removal of message {message_id} has an unreadable time {when!r}, using now')
            removed_at = None
        if removed_at is None:
            removed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        when = removed_at.replace(tzinfo=timezone.utc).timestamp()
        try:
            rows, sidecars = self._tombstone_message(message_id, when)
        except Exception as e:
            ActivityLog().error(f'[db] Removing message {message_id} failed: {e}')
            return
        if rows or sidecars:
            ActivityLog().info(f'[db] Message {message_id} marked deleted: {rows} rows' + (f', {sidecars} sidecars' if sidecars else '') + (f' ({source})' if source else ''))
            return
        try:
            if not list(PendingRemoval.selectBy(message_id=str(message_id), account_id=str(account_id or ''))):
                PendingRemoval(message_id=str(message_id), account_id=str(account_id or ''), remote_uri=str(remote_uri) if remote_uri else None,
                               removed_at=removed_at, source=source)
        except Exception as e:
            ActivityLog().error(f'[db] Keeping the removal of message {message_id} failed: {e}')
            return
        ActivityLog().info(f'[db] Message {message_id} is not stored yet, its removal is kept until it arrives' + (f' ({source})' if source else ''))

    @run_in_thread('db')
    def tombstone_conversation(self, remote_uri, before_time=None, when=None, account_id=None, session=None):
        """Hide a conversation up to the moment it was removed.

        `before_time` is when the removal was made, not when it arrived: a removal
        replayed from the journal must not hide messages exchanged after it. Without
        `account_id` every account's rows are hidden, as the conversation is shown.
        The tombstones are stamped with the removal time (`when`, else `before_time`):
        it is the clock a later message is read against to bring the conversation back.
        """
        db = Message._connection
        keys = self._conversation_keys(remote_uri)
        where = f"remote_uri in ({', '.join(db.sqlrepr(key) for key in keys)})"
        if account_id:
            where += f' and account_id = {db.sqlrepr(str(account_id))}'
        floor = self._storage_time(before_time)
        if floor is not None:
            where += f' and timestamp <= {db.sqlrepr(floor)}'
            if when is None:
                when = floor.replace(tzinfo=timezone.utc).timestamp()
        try:
            count = self._set_deleted(where, True, when)
        except Exception as e:
            ActivityLog().error(f'[db] Removing the conversation with {remote_uri} failed: {e}')
            return
        ActivityLog().info(f'[db] Conversation with {remote_uri} marked deleted: {count} rows' + (f' up to {floor}' if floor is not None else '') + (f' for account {account_id}' if account_id else ''))
        if session is not None:
            self.load(remote_uri, session)

    @classmethod
    def _revive_or_bury(cls, message_id, remote_uri, timestamp):
        """A message was stored in a removed conversation (every other row a tombstone).
        Newer than the removal, the conversation comes back: the rows that removal hid
        are shown again. Not newer (a journal replay, a late delivery of something that
        was there when it was removed), it is hidden too: an old message must not undo
        a removal. Same rule as macOS and Sylk Mobile. Caller is in the db thread."""
        db = Message._connection
        table = Message.sqlmeta.table
        others = f'remote_uri = {db.sqlrepr(str(remote_uri))} and message_id != {db.sqlrepr(str(message_id))}'
        live, removed_at = db.queryOne(f'select sum(case when {NOT_DELETED_SQL} then 1 else 0 end), max(deleted_time) from {table} where {others}')
        if live or not removed_at:
            return
        try:
            stamp = timestamp.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return
        if stamp > removed_at:
            count = cls._set_deleted(f'{others} and deleted_time = {int(removed_at)}', False)
            ActivityLog().info(f'[db] Conversation with {remote_uri} came back: message {message_id} from {timestamp} is newer than its removal, {count} rows shown again')
        else:
            cls._set_deleted(f'message_id = {db.sqlrepr(str(message_id))} and remote_uri = {db.sqlrepr(str(remote_uri))}', True, removed_at)
            ActivityLog().info(f'[db] Message {message_id} from {timestamp} hidden: the conversation with {remote_uri} was removed after it')

    @run_in_thread('db')
    def restore_conversation(self, remote_uri):
        """Un-hide every tombstoned row of a conversation, under every spelling of its key."""
        db = Message._connection
        keys = self._conversation_keys(remote_uri)
        try:
            count = self._set_deleted(f"remote_uri in ({', '.join(db.sqlrepr(key) for key in keys)})", False)
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

    def _remote_uri_sql(self, remote_uri):
        """remote_uri = x, or for a contact with several addresses (a list or tuple of
        conversation keys) remote_uri in (...): its conversations shown as one."""
        if isinstance(remote_uri, (list, tuple, set, frozenset)):
            keys = sorted({str(key) for key in remote_uri if key})
            if len(keys) != 1:
                return f"remote_uri in ({', '.join(self.db.sqlrepr(key) for key in keys)})" if keys else '0'
            remote_uri = keys[0]
        return f'remote_uri = {self.db.sqlrepr(str(remote_uri))}'

    @staticmethod
    def _category_sql(category):
        # 'links' is text with a link in it (has_link), not a category of its own
        if not category:
            return ''
        if category == 'links':
            return " and category = 'text' and has_link = 1"
        return f" and category = {Message.sqlrepr(category)}"

    def stored_message_ids(self, message_ids, timeout=30):
        """The ones of these message ids history already holds (removed ones included).

        For callers outside the db thread (the journal's sync thread): the query
        is queued on the db thread, after the writes before it, and waited for to
        the end (a large first sync queues a page of writes before it: an answer
        given early would be missing ids, and duplicates would be stored as new).
        """
        message_ids = sorted({str(message_id) for message_id in message_ids if message_id})
        found = set()
        if not message_ids:
            return found
        done = threading.Event()

        @run_in_thread('db')
        def query():
            try:
                table = Message.sqlmeta.table
                for start in range(0, len(message_ids), 500):
                    chunk = message_ids[start:start + 500]
                    rows = self.db.queryAll(f"select distinct message_id from {table} where message_id in ({', '.join(self.db.sqlrepr(message_id) for message_id in chunk)})")
                    found.update(str(message_id) for (message_id,) in rows)
            except Exception as e:
                ActivityLog().error(f'[db] Looking up stored message ids failed: {e}')
            finally:
                done.set()
        query()
        if not done.wait(timeout):
            started = time.monotonic() - timeout
            done.wait()
            ActivityLog().info(f'[db] Looking up {len(message_ids)} stored message ids waited {time.monotonic() - started:.0f}s for the writes before it')
        return found

    def wait_for_writes(self):
        """For callers outside the db thread: returns once what was queued on the db thread before it is done."""
        done = threading.Event()
        run_in_thread('db')(done.set)()
        done.wait()

    @run_in_thread('db')
    def apply_receipts(self, receipts, account_id=None, page=None):
        """{message id: state} from a first sync's journal, applied in one go.

        Outgoing messages take the state (an error only when not delivered or
        displayed yet); incoming ones this account reported displayed are read.
        """
        table = Message.sqlmeta.table
        by_state = {}
        for message_id, state in receipts.items():
            if state in ('delivered', 'displayed', 'error', 'failed'):
                by_state.setdefault('error' if state == 'failed' else state, []).append(message_id)
        changed = Counter()
        try:
            for state, message_ids in by_state.items():
                for start in range(0, len(message_ids), 500):
                    ids = ', '.join(self.db.sqlrepr(message_id) for message_id in message_ids[start:start + 500])
                    if state == 'displayed':
                        changed['incoming read'] += self.db.queryOne(f"select count(*) from {table} where message_id in ({ids}) and direction = 'incoming' and read = 0")[0]
                        self.db.queryAll(f"update {table} set read = 1, state = 'displayed' where message_id in ({ids}) and direction = 'incoming' and state != 'deleted'")
                        allowed = "state not in ('displayed', 'deleted')"
                    elif state == 'delivered':
                        allowed = "state not in ('delivered', 'displayed', 'deleted')"
                    else:
                        allowed = "state not in ('delivered', 'displayed', 'deleted', 'error')"
                    where = f"message_id in ({ids}) and direction = 'outgoing' and {allowed}"
                    changed[f'outgoing {state}'] += self.db.queryOne(f'select count(*) from {table} where {where}')[0]
                    self.db.queryAll(f'update {table} set state = {self.db.sqlrepr(state)} where {where}')
        except Exception as e:
            ActivityLog().error(f'[db] Applying {len(receipts)} journal receipts failed: {e}')
            JournalLog()(account_id, 'receipts failed', receipts=len(receipts), error=str(e)[:200])
            return
        JournalLog()(account_id, 'receipts', page=page, received=len(receipts), **{what.replace(' ', '_'): count for what, count in sorted(changed.items())})
        ActivityLog().info(f'[db] Applied {len(receipts)} journal receipts: ' + (', '.join(f'{count} {what}' for what, count in sorted(changed.items()) if count) or 'nothing changed'))

    @run_in_thread('db')
    def settle_first_sync_read(self, account_id, days=7, marker=None):
        """After a first sync: an incoming message older than the newest outgoing one in its
        conversation, or older than `days`, was read (on another device, before this one).
        Then the first sync is finished: its marker file (journal/<account>/first-sync.marker) goes."""
        table = Message.sqlmeta.table
        account = self.db.sqlrepr(str(account_id))
        cutoff = self.db.sqlrepr(self._storage_time(datetime.now(timezone.utc) - timedelta(days=days)))
        where = (f"account_id = {account} and direction = 'incoming' and read = 0 and (timestamp < {cutoff} or timestamp < "
                 f"(select max(o.timestamp) from {table} as o where o.remote_uri = {table}.remote_uri and o.direction = 'outgoing'))")
        try:
            count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
            if count:
                self.db.queryAll(f'update {table} set read = 1 where {where}')
        except Exception as e:
            ActivityLog().error(f'[db] Settling the read state of {account_id} after the first sync failed: {e}')
            JournalLog()(account_id, 'settle failed', error=str(e)[:200])
            return
        if marker:
            unlink(marker)
        JournalLog()(account_id, 'settle first_sync', incoming_read=count, rule=f'older than the newest outgoing or {days} days', finished='yes')
        ActivityLog().info(f'[db] First sync of {account_id}: {count} older incoming messages marked read')

    @run_in_thread('db')
    def log_unread(self, account_id):
        """The unread counts of an account, for the journal log after a run."""
        table = Message.sqlmeta.table
        try:
            conversations, messages = self.db.queryOne(f"select count(distinct remote_uri), count(*) from {table}"
                                                       f" where account_id = {self.db.sqlrepr(str(account_id))} and direction = 'incoming' and read = 0"
                                                       f" and {NOT_DELETED_SQL} and state != 'deleted' and {self.__readable_sql__}")
        except Exception as e:
            JournalLog()(account_id, 'unread failed', error=str(e)[:200])
            return
        JournalLog()(account_id, 'unread', conversations=conversations, messages=messages)

    def last_message_times(self, accounts=None, include_calls=False, remote_uri=None):
        """{conversation key: newest message time} for ordering conversations."""
        query = (f'select remote_uri, max(timestamp) from {Message.sqlmeta.table}'
                 f' where {NOT_DELETED_SQL} and category is not null' + ('' if include_calls else " and category != 'call'")
                 + self._in_sql('account_id', accounts) + self._in_sql('remote_uri', remote_uri) + ' group by remote_uri')
        return {str(remote_uri): str(newest) for remote_uri, newest in self.db.queryAll(query) if remote_uri and newest}

    def last_call_times(self, accounts=None, remote_uri=None):
        """{conversation key: start time of the newest call}."""
        query = (f'select remote_uri, max(timestamp) from {Message.sqlmeta.table}'
                 f" where {NOT_DELETED_SQL} and category = 'call'"
                 + self._in_sql('account_id', accounts) + self._in_sql('remote_uri', remote_uri) + ' group by remote_uri')
        return {str(remote_uri): str(newest) for remote_uri, newest in self.db.queryAll(query) if remote_uri and newest}

    def last_message_accounts(self, accounts=None, remote_uri=None):
        """{conversation key: account id of its newest message}, the account a conversation continues on."""
        table = Message.sqlmeta.table
        where = f' where {NOT_DELETED_SQL} and category is not null' + self._in_sql('account_id', accounts) + self._in_sql('remote_uri', remote_uri)
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

    def search_messages(self, remote_uri, text, limit=200, accounts=None):
        """A conversation's text messages containing `text` (case-insensitive for ASCII), newest first.
        Encrypted bodies not yet decrypted cannot match."""
        pattern = '%' + str(text).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        query = (f"{self._remote_uri_sql(remote_uri)} and {NOT_DELETED_SQL} and category = 'text'"
                 f" and content like {self.db.sqlrepr(pattern)} escape '\\'" + self._in_sql('account_id', accounts))
        return list(Message.select(query, orderBy=['-timestamp', '-id'], limit=int(limit)))

    def present_categories(self, remote_uri, accounts=None):
        """The category filters a conversation has messages for, 'links' included."""
        where = (f' where {NOT_DELETED_SQL} and {self._remote_uri_sql(remote_uri)}' + self._in_sql('account_id', accounts))
        table = Message.sqlmeta.table
        found = {str(category) for (category,) in self.db.queryAll(f'select distinct category from {table}{where} and category is not null') if category}
        if 'text' in found and self.db.queryAll(f"select 1 from {table}{where} and category = 'text' and has_link = 1 limit 1"):
            found.add('links')
        return found

    def get_messages(self, remote_uri, before=None, after=None, category=None, limit=100, accounts=None, include_trail=False, oldest_first=False):
        """A page of a conversation, newest first: up to `limit` messages older than
        `before` (and newer than `after`), of one category if given; with oldest_first,
        the oldest `limit` of them, oldest first (paging forwards). Location trail
        ticks are left out unless asked for (they belong to their share's bubble) and
        metadata sidecars always are."""
        query = f'{self._remote_uri_sql(remote_uri)} and {NOT_DELETED_SQL}' + self._category_sql(category) + self._in_sql('account_id', accounts)
        # sidecars (reply links, captions, waveforms) are not bubbles: they come with related_messages()
        query += f" and content_type != '{METADATA_CONTENT_TYPE}'"
        if not include_trail:
            actions = ', '.join(self.db.sqlrepr(action) for action in self.__trail_actions__)
            query += f' and (related_action is null or related_action not in ({actions}))'
        if before is not None:
            query += f' and timestamp < {self.db.sqlrepr(self._storage_time(before))}'
        if after is not None:
            query += f' and timestamp > {self.db.sqlrepr(self._storage_time(after))}'
        order = ['timestamp', 'id'] if oldest_first else ['-timestamp', '-id']
        return list(Message.select(query, orderBy=order, limit=int(limit)))

    def day_counts(self, remote_uri, accounts=None):
        """{'YYYY-MM-DD' (local time): number of messages shown that day} for a conversation's calendar."""
        table = Message.sqlmeta.table
        actions = ', '.join(self.db.sqlrepr(action) for action in self.__trail_actions__)
        query = (f"select date(timestamp, 'localtime') as day, count(*) from {table}"
                 f" where {self._remote_uri_sql(remote_uri)} and {NOT_DELETED_SQL} and category is not null"
                 f" and content_type != '{METADATA_CONTENT_TYPE}' and (related_action is null or related_action not in ({actions}))"
                 + self._in_sql('account_id', accounts) + ' group by day')
        return {str(day): int(count) for day, count in self.db.queryAll(query) if day}

    def related_messages(self, message_ids):
        """The rows filed against a page of messages (location ticks, sidecars keyed by related_msg_id)."""
        message_ids = [str(message_id) for message_id in message_ids if message_id]
        if not message_ids:
            return []
        query = f"related_msg_id in ({', '.join(self.db.sqlrepr(message_id) for message_id in message_ids)}) and {NOT_DELETED_SQL}"
        return list(Message.select(query, orderBy=['timestamp', 'id']))

    # Updates

    _missing_message_bodies = set()  # ids reported missing once, not once per location tick

    @run_in_thread('db')
    def update_message_body(self, message_id, body, merge=None):
        """Replace the stored body of a message; with `merge`, store merge(stored, new) instead.

        The read-modify-write runs in the db thread, where writers cannot interleave:
        a live location share is written by whichever path sees a tick first (live,
        replicated, journal) and only the stored row holds the accumulated trail
        (blink.location.merge_location_bodies).
        """
        rows = list(Message.selectBy(message_id=str(message_id)))
        if not rows:
            if message_id not in self._missing_message_bodies:
                self._missing_message_bodies.add(message_id)
                log.debug(f'No stored message {message_id} to update the body of')
            return
        for row in rows:
            new_body = body
            if merge is not None:
                try:
                    new_body = merge(row.content, body)
                except Exception as e:
                    ActivityLog().error(f'[db] Merging the body of message {message_id} failed: {e}')
                    continue
            if new_body is not None and new_body != row.content:
                row.content = new_body

    @run_in_thread('db')
    def update_decrypted_message(self, message_id, plaintext):
        """Store the plaintext of a decrypted message in place of its ciphertext.

        This is the first moment an encrypted file transfer or text can be
        classified, so a missing category and the link flag are filled in here.
        """
        rows = list(Message.selectBy(message_id=str(message_id)))
        if not rows:
            log.debug(f'No stored message {message_id} to store the decrypted body of')
            return
        for row in rows:
            row.content = plaintext
            row.decrypted = '1'
            row.decryption_error = ''
            if row.category is None:
                category = classify_category(row.content_type, plaintext, row.related_action, row.metadata)
                if category is not None:
                    row.category = category
            if row.category == 'text' and not row.has_link:
                row.has_link = has_link(row.content_type, plaintext) or 0

    rekey_marker = 'history-canonical-keys.done'

    @run_in_thread('db')
    def drop_file_transfer_notices(self):
        """Remove the server's plain text notices of file transfers stored before they were
        skipped (blink.journal.is_file_transfer_notice): the transfers are stored on their own."""
        table = Message.sqlmeta.table
        where = ("content_type = 'text/plain' and content like 'File transfer available at %'"
                 " and content like '%/webrtcgateway/filetransfer/%'")
        try:
            count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
            if count:
                self.db.queryAll(f'delete from {table} where {where}')
        except Exception as e:
            ActivityLog().error(f'[db] Removing file transfer notices failed: {e}')
            return
        if count:
            ActivityLog().info(f'[db] Removed {count} file transfer notices (the transfers are stored on their own)')
            ConversationPreviews().invalidate()

    @run_in_thread('db')
    def rekey_conversations(self):
        """File every conversation under its canonical key, once (conversation_key).

        Run after the accounts are loaded, so a national number is put in E.164
        by its own account's dial rules: before that 020... could not be told
        from +31 20.... Messages in two spellings of one party end up in one
        conversation; a message stored under both is kept once. Downloaded files
        follow. A marker file makes it run once.
        """
        marker = ApplicationData.get(self.rekey_marker)
        if os.path.exists(marker):
            return
        from sipsimple.account import AccountManager
        account_manager = AccountManager()
        table = Message.sqlmeta.table
        moved = duplicates = conversations = 0
        try:
            pairs = self.db.queryAll(f'select distinct remote_uri, account_id from {table}')
            for old_key, account_id in pairs:
                account = account_manager.get_account(account_id) if account_manager.has_account(account_id) else None
                new_key = conversation_key(old_key, account)
                if not old_key or not new_key or new_key == old_key:
                    continue
                where = f'remote_uri = {self.db.sqlrepr(old_key)} and account_id = {self.db.sqlrepr(account_id)}'
                count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
                self.db.queryAll(f'update or ignore {table} set remote_uri = {self.db.sqlrepr(new_key)} where {where}')
                left = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
                self.db.queryAll(f'delete from {table} where {where}')
                self.db.queryAll(f'update or ignore {DownloadedFiles.sqlmeta.table} set remote_uri = {self.db.sqlrepr(new_key)} where {where}')
                ActivityLog().info(f'[db] Conversation {old_key} of {account_id} filed under {new_key}: {count - left} messages' + (f', {left} duplicates dropped' if left else ''))
                moved += count - left
                duplicates += left
                conversations += 1
        except Exception as e:
            ActivityLog().exception(f'[db] Filing conversations under their canonical key failed, retried at the next start: {e!r}')
            return
        with open(marker, 'w') as marker_file:
            marker_file.write('1\n')
        ActivityLog().info(f'[db] Conversations filed under their canonical key: {conversations} keys changed, {moved} messages moved, {duplicates} duplicates dropped')
        if conversations:
            self.get_unread_messages()

    @run_in_thread('db')
    def purge_conversations(self, keys, reason=''):
        """Erase the conversations filed under these keys, every spelling of each, in every
        account: rows and downloaded file records, not tombstones. Only for a deletion made
        here by the user; a removal from another device tombstones instead."""
        table = Message.sqlmeta.table
        total = 0
        for key in keys:
            spellings = self._conversation_keys(key)
            where = f"remote_uri in ({', '.join(self.db.sqlrepr(spelling) for spelling in spellings)})"
            try:
                count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
                self.db.queryAll(f'delete from {table} where {where}')
                self.db.queryAll(f'delete from {DownloadedFiles.sqlmeta.table} where {where}')
            except Exception as e:
                ActivityLog().error(f'[db] Removing the history of {key} failed: {e!r}')
                continue
            total += count
            ActivityLog().info(f'[db] Removed {count} messages of the conversation with {key}' + (f' ({reason})' if reason else ''))
        if total:
            self.get_unread_messages()
            if BlinkSettings().interface.show_messages_group:
                self.get_all_contacts()

    @run_in_thread('db')
    def move_conversation(self, old_key, new_key, account_id=None):
        """File every message of one conversation under another key.

        For keys that turn out to be one party: a neighbour back under a new instance
        id, a number stored in two spellings. A message present under both keys is
        kept once. Downloaded files follow.
        """
        old_key, new_key = str(old_key or ''), str(new_key or '')
        if not old_key or not new_key or old_key == new_key:
            return
        table = Message.sqlmeta.table
        where = f'remote_uri = {self.db.sqlrepr(old_key)}' + (f' and account_id = {self.db.sqlrepr(str(account_id))}' if account_id else '')
        try:
            count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
            self.db.queryAll(f'update or ignore {table} set remote_uri = {self.db.sqlrepr(new_key)} where {where}')
            duplicates = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
            self.db.queryAll(f'delete from {table} where {where}')
            files_where = f'remote_uri = {self.db.sqlrepr(old_key)}' + (f' and account_id = {self.db.sqlrepr(str(account_id))}' if account_id else '')
            self.db.queryAll(f'update or ignore {DownloadedFiles.sqlmeta.table} set remote_uri = {self.db.sqlrepr(new_key)} where {files_where}')
        except Exception as e:
            ActivityLog().error(f'[db] Moving the conversation {old_key} to {new_key} failed: {e}')
            return
        ActivityLog().info(f'[db] Conversation {old_key} moved to {new_key}: {count - duplicates} messages' + (f', {duplicates} duplicates dropped' if duplicates else ''))

    @run_in_thread('db')
    def move_message(self, message_id, old_key, new_key):
        """File one message under another conversation (a call recording whose
        placement note arrived after it)."""
        moved = 0
        for row in Message.selectBy(message_id=str(message_id), remote_uri=str(old_key)):
            try:
                row.remote_uri = str(new_key)
                moved += 1
            except dberrors.DuplicateEntryError:
                row.destroySelf()
        if moved:
            ActivityLog().info(f'[db] Message {message_id} moved from {old_key} to {new_key}')

    def _vacuum_if_needed(self):
        """Compact the database at startup when much of it is free pages.

        Never after each delete (a macOS defect): a VACUUM rewrites the whole
        file, which for a large history is seconds of a blocked db thread.
        """
        try:
            pages = self.db.queryOne('PRAGMA page_count')[0]
            free = self.db.queryOne('PRAGMA freelist_count')[0]
        except Exception:
            return
        if free < 1000 or free * 4 < pages:
            return
        started = time.monotonic()
        try:
            self.db.queryAll('VACUUM')
        except Exception as e:
            ActivityLog().warning(f'[db] Compacting the message history failed: {e}')
            return
        ActivityLog().info(f'[db] Compacted the message history: {free} of {pages} pages were free, {time.monotonic() - started:.2f}s')

    @run_in_thread('db')
    def journal_db_check(self, account_id, stats_path=None):
        """Count what the database holds for an account after a journal run (plan §1.4),
        log it and add it to the run's import statistics file."""
        table = Message.sqlmeta.table
        account = self.db.sqlrepr(str(account_id))
        try:
            total = self.db.queryOne(f'select count(*) from {table} where account_id = {account} and {NOT_DELETED_SQL}')[0]
            categories = {str(category or '(none)'): count for category, count in
                          self.db.queryAll(f'select category, count(*) from {table} where account_id = {account} and {NOT_DELETED_SQL} group by category')}
            content_types = {str(content_type): count for content_type, count in
                             self.db.queryAll(f'select content_type, count(*) from {table} where account_id = {account} and {NOT_DELETED_SQL} group by content_type')}
            unread = self.db.queryOne(f"select count(*) from {table} where account_id = {account} and direction = 'incoming' and read = 0 and {NOT_DELETED_SQL}")[0]
            tombstoned = self.db.queryOne(f'select count(*) from {table} where account_id = {account} and deleted = 1')[0]
            conversations = self.db.queryOne(f'select count(distinct remote_uri) from {table} where account_id = {account} and {NOT_DELETED_SQL}')[0]
            pending = self.db.queryOne(f'select count(*) from {PendingRemoval.sqlmeta.table} where account_id = {account}')[0]
        except Exception as e:
            ActivityLog().error(f'[db] Counting the messages of {account_id} failed: {e}')
            return
        activity = ActivityLog()
        activity.info(f'[db] {account_id} now has {total} messages in {conversations} conversations, {unread} unread, {tombstoned} hidden, {pending} pending removals')
        activity.info('[db]   by category: ' + ', '.join(f'{category} {count}' for category, count in sorted(categories.items(), key=lambda item: -item[1])))
        activity.info('[db]   by content type: ' + ', '.join(f'{content_type} {count}' for content_type, count in sorted(content_types.items(), key=lambda item: -item[1])))
        if stats_path:
            try:
                with open(stats_path, encoding='utf-8') as stats_file:
                    stats = json.load(stats_file)
                stats['database'] = {'messages': total, 'conversations': conversations, 'unread': unread, 'hidden': tombstoned,
                                     'pending_removals': pending, 'categories': categories, 'content_types': content_types}
                with open(stats_path, 'w', encoding='utf-8') as stats_file:
                    json.dump(stats, stats_file, indent=1, sort_keys=True)
            except (OSError, ValueError) as e:
                activity.warning(f'[db] Cannot add the database counts to {stats_path}: {e}')

    def _conversation_keys(self, remote_uri):
        """The keys a conversation may be filed under, for a peer address as another device
        spells it (sip:alice@example.com;transport=tls, a phone number, a Bonjour id)."""
        text = str(remote_uri or '').strip()
        keys = {text}
        address = text
        for scheme in ('sips:', 'sip:'):
            if address.lower().startswith(scheme):
                address = address[len(scheme):]
        address = address.split(';', 1)[0].split('?', 1)[0]
        keys.add(address)
        match = self.phone_number_re.match(address)
        if match:
            keys.add(match.group('number'))
        try:
            keys.add(canonical_uri(text))
        except Exception:
            pass
        # a conference room is stored under Sylk Mobile's videoconference.X spelling as
        # well as under the bridge domain conference.X the addressbook keeps: both are it
        for key in list(keys):
            user, at, domain = key.rpartition('@')
            if not at:
                continue
            if domain.lower().startswith('videoconference.'):
                keys.add(f'{user}@{domain[len("video"):]}')
            elif domain.lower().startswith('conference.'):
                keys.add(f'{user}@video{domain}')
        return sorted(key for key in keys if key)

    @run_in_thread('db')
    def mark_conversation_read(self, remote_uri, source=None, before_time=None):
        """Mark the incoming messages of a conversation read, whatever account it was filed
        under. With `before_time` (when it was read on another device) only those up to then:
        what arrived after it is still unread here."""
        table = Message.sqlmeta.table
        keys = self._conversation_keys(remote_uri)
        where = f"remote_uri in ({', '.join(self.db.sqlrepr(key) for key in keys)}) and direction = 'incoming' and read = 0"
        try:
            floor = self._storage_time(before_time)
        except (ValueError, OverflowError):
            floor = None
        if floor is not None:
            where += f' and timestamp <= {self.db.sqlrepr(floor)}'
        try:
            count = self.db.queryOne(f'select count(*) from {table} where {where}')[0]
            if count:
                self.db.queryAll(f'update {table} set read = 1 where {where}')
        except Exception as e:
            ActivityLog().error(f'[db] Marking the conversation with {remote_uri} read failed: {e}')
            return
        if count:
            ActivityLog().info(f'[db] Marked {count} messages read in the conversation with {remote_uri}' + (f' up to {floor}' if floor is not None else '') + (f' (read on {source})' if source else ''))
        NotificationCenter().post_notification('BlinkMessageHistoryConversationWasRead', data=NotificationData(remote_uri=str(remote_uri), count=count))
        if count and source:
            # the badge is keyed as history files the conversation, not as the other device spelled it
            self.get_unread_messages()

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

    def _upgrade_to_v10(self):
        """file metadata companions against their message"""
        # Companions stored before this version (from the journal or live, as inert rows)
        # carry no related_msg_id. Those whose message is removed are hidden as well.
        table = Message.sqlmeta.table
        rows = self.db.queryAll(f"select id, message_id, content from {table}"
                                f" where content_type = '{METADATA_CONTENT_TYPE}' and (related_msg_id is null or related_msg_id = '')")
        linked = hidden = 0
        for row_id, message_id, content in rows:
            link = metadata_link(content)
            if link is None:
                continue
            target, action = link
            self.db.queryAll(f'update {table} set related_msg_id = {self.db.sqlrepr(target)}, related_action = {self.db.sqlrepr(action)} where id = {int(row_id)}')
            linked += 1
            removed = self.db.queryOne(f'select deleted_time from {table} where message_id = {self.db.sqlrepr(target)} and deleted = 1')
            if removed is not None:
                hidden += self._set_deleted(f'id = {int(row_id)}', True, removed[0] or None)
        ActivityLog().info(f'[db] {linked} of {len(rows)} metadata messages filed against their message, {hidden} hidden with their removed message')
        return linked

    def _upgrade_to_v11(self):
        """file location ticks against their share"""
        # Location messages stored before this version have no related_* columns, so
        # update ticks show as bubbles and nothing groups a share. A tick stored without
        # its version 2 side-band cannot be read without decrypting and stays as it is.
        table = Message.sqlmeta.table
        rows = self.db.queryAll(f"select id, content, metadata from {table}"
                                f" where content_type = '{LOCATION_CONTENT_TYPE}' and (related_action is null or related_action = '')")
        filed = 0
        for row_id, content, metadata in rows:
            fields = location_storage_fields(content, metadata, LOCATION_CONTENT_TYPE)
            if not fields:
                continue
            assignments = ', '.join(f'{name} = {self.db.sqlrepr(value)}' for name, value in fields.items())
            self.db.queryAll(f'update {table} set {assignments} where id = {int(row_id)}')
            filed += 1
        ActivityLog().info(f'[db] {filed} of {len(rows)} location messages filed against their share' + (f', {len(rows) - filed} cannot be read without decrypting' if len(rows) > filed else ''))
        return filed

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
            message = Message(remote_uri=conversation_key(entry.uri),
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
    def add_call_recording(cls, path, transfer_id, key, account, uri, display_name, timestamp):
        """A call this device recorded, as an audio message of the conversation with the other
        party. The file is already where local_file looks (file_transfers/<account>/<peer>/<id>/);
        the envelope has no url: the recording stays on this device."""
        content = json.dumps({'filename': os.path.basename(path),
                              'filesize': os.path.getsize(path),
                              'filetype': 'audio/wav',
                              'transfer_id': transfer_id,
                              'call_recording': True})
        fields = cls._content_fields(FILE_TRANSFER_CONTENT_TYPE, content)
        try:
            Message(remote_uri=key,
                    display_name=display_name or '',
                    uri=str(uri),
                    content=content,
                    content_type=FILE_TRANSFER_CONTENT_TYPE,
                    message_id=transfer_id,
                    account_id=str(account.id),
                    direction='outgoing',
                    timestamp=timestamp,
                    decrypted='0',
                    decryption_error='',
                    disposition='',
                    state='displayed',
                    read=1,
                    **fields)
        except dberrors.DuplicateEntryError:
            return
        except Exception as e:
            ActivityLog().error(f'[db] Storing the call recording {path} failed: {e!r}')
            return
        ActivityLog().info(f'[db] Call recording with {key} stored as message {transfer_id}')
        NotificationCenter().post_notification('BlinkMessageHistoryMessageDidStore', sender=account,
                                               data=NotificationData(remote_uri=key, state='displayed', direction='outgoing'))

    @run_in_thread('db')
    def store_call_record(self, account, record, message_id=None, origin=''):
        """A call another of the user's devices took part in (or the server saw),
        merged with this device's row of the same call: one row per (account, Call-ID).
        merge_call_records decides which view wins each field, by source rank."""
        account_id = str(account.id)
        call_id = str(record.get('sessionId') or '').strip()
        what = f"{record.get('direction') or '?'} call {call_id or '(no Call-ID)'} with {record.get('remoteParty') or '?'}" + (f' ({origin})' if origin else '')
        try:
            rows = list(Message.select(f'account_id = {self.db.sqlrepr(account_id)} and sip_callid = {self.db.sqlrepr(call_id)}'
                                       f" and content_type = '{CALL_CONTENT_TYPE}'", limit=1)) if call_id else []
            if rows:
                row = rows[0]
                stored = call_record(row.content, row.metadata)
                merged = merge_call_records(stored, record)
                if merged == stored:
                    return      # replayed (the server call history every 5 minutes): nothing to do, nothing to log
                row.set(metadata=json.dumps(merged), content=call_summary(merged) or '',
                        media_type=dominant_media(merged.get('media') or []) or row.media_type)
                ActivityLog().info(f"[db] Call record of {what} merged into the stored one: {(stored or {}).get('outcome')} -> {merged.get('outcome')}, source {merged.get('source')}")
                self._call_record_stored(account, row.remote_uri)
                return
            remote_party = str(record.get('remoteParty') or '')
            timestamp = self._storage_time(record.get('startTime')) or datetime.now(timezone.utc).replace(tzinfo=None)
            Message(remote_uri=canonical_uri(remote_party, account) or remote_party,
                    display_name=str(record.get('displayName') or ''),
                    uri=remote_party,
                    content=call_summary(record) or '',
                    content_type=CALL_CONTENT_TYPE,
                    metadata=json.dumps(record),
                    category='call',
                    media_type=dominant_media(record.get('media') or []),
                    sip_callid=call_id or None,
                    message_id=str(message_id or uuid.uuid4()),
                    account_id=account_id,
                    direction=str(record.get('direction') or 'outgoing'),
                    timestamp=timestamp,
                    decrypted='0',
                    decryption_error='',
                    disposition='',
                    state='displayed',
                    read=1)
        except dberrors.DuplicateEntryError:
            ActivityLog().info(f'[db] Call record message {message_id} of {what} already stored')
        except Exception as e:
            ActivityLog().error(f'[db] Storing the call record of {what} failed: {e!r}')
        else:
            ActivityLog().info(f"[db] Call record of {what} stored: {record.get('outcome')}, source {record.get('source')}")
            self._call_record_stored(account, canonical_uri(remote_party, account) or remote_party)

    @staticmethod
    def _call_record_stored(account, remote_uri):
        """Tell the transcript and the previews that a call row of remote_uri was added or changed
        (not BlinkMessageHistoryMessageDidStore: a call is not a message that files its party in Messages)."""
        NotificationCenter().post_notification('BlinkMessageHistoryCallRecordDidStore', sender=account,
                                               data=NotificationData(remote_uri=remote_uri))

    @classmethod
    @run_in_thread('db')
    def add_from_server_history(cls, account, remote_uri, message, state=None, encryption=None):
        if message.content.startswith('?OTRv'):
            return

        remote_uri = conversation_key(remote_uri, account)
        log.info(f"== Adding {message.direction} history message to storage: {message.id} {state} {remote_uri}")

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
        optional_fields.update(cls._content_fields(message.content_type, message.content))
        optional_fields.update(cls._related_fields(message.content_type, message.content, getattr(message, 'metadata', None)))

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
            if message.content_type != METADATA_CONTENT_TYPE:
                cls._revive_or_bury(message.id, remote_uri, timestamp)
            if message.content_type == METADATA_CONTENT_TYPE:
                cls._stored_companion(message.id, optional_fields)     # not a bubble: nothing to refresh
            elif message.content_type == LOCATION_CONTENT_TYPE and optional_fields.get('related_action') in cls.__trail_actions__:
                cls._stored_location(message.id, optional_fields, remote_uri)     # moves a pin: nothing to refresh
            elif message.content_type not in {IsComposingDocument.content_type, IMDNDocument.content_type, 'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-message-remove'}:
                notification_center = NotificationCenter()
                notification_center.post_notification('BlinkMessageHistoryMessageDidStore', sender=account, data=NotificationData(remote_uri=remote_uri, state=state, direction=message.direction))
                if message.content_type == LOCATION_CONTENT_TYPE:
                    cls._stored_location(message.id, optional_fields, remote_uri)

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

            # a neighbour who is away is addressed by placeholder; the conversation is its instance id
            remote_uri = conversation_key('%s@%s' % (user, domain), session.account)

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
        optional_fields.update(cls._content_fields(message.content_type, message.content))
        optional_fields.update(cls._related_fields(message.content_type, message.content, getattr(message, 'metadata', None)))
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
            if message.content_type != METADATA_CONTENT_TYPE:
                cls._revive_or_bury(message.id, remote_uri, timestamp)

            if message.content_type == METADATA_CONTENT_TYPE:
                cls._stored_companion(message.id, optional_fields)     # not a bubble: nothing to refresh
            elif message.content_type == LOCATION_CONTENT_TYPE and optional_fields.get('related_action') in cls.__trail_actions__:
                cls._stored_location(message.id, optional_fields, remote_uri)     # moves a pin: nothing to refresh
            elif message.content_type not in {IsComposingDocument.content_type, IMDNDocument.content_type, 'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-message-remove'}:
                notification_center = NotificationCenter()
                notification_center.post_notification('BlinkMessageHistoryMessageDidStore', sender=session.account, data=NotificationData(remote_uri=remote_uri, state=state, direction=direction))
                if message.content_type == LOCATION_CONTENT_TYPE:
                    cls._stored_location(message.id, optional_fields, remote_uri)

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
            if message.direction == 'outgoing' and state == 'error' and message.state in ('delivered', 'displayed'):
                # the peer's devices answer each on its own: one that cannot show it does not undo one that did
                log.info(f'Message {id} to {message.remote_uri} error disposition ignored, already {message.state}')
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
        remote_uri = bare_instance_id(session.remote_instance_id) if session.remote_instance_id else conversation_key(uri, session.account)
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
        remote_uri = bare_instance_id(session.remote_instance_id) if session.remote_instance_id else conversation_key(uri, session.account)
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
        pending = PendingRemoval.selectBy(account_id=account_id).count()
        if pending:
            PendingRemoval.deleteBy(account_id=account_id)
            ActivityLog().info('[db] Removed %d pending message removals of %s' % (pending, account_id))

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


def _row_time(value):
    """A stored timestamp (naive UTC, as SQLite hands it back) as an aware UTC datetime, or None."""
    try:
        value = parse(str(value))
    except (ValueError, OverflowError):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@implementer(IObserver)
class ConversationPreviews(object, metaclass=Singleton):
    """What a conversation's contact row shows of it: the last typed message (the
    second line) and the times of the last message and the last call (on the right).

    previews {conversation key: (time, text)}, message_times and call_times
    {conversation key: time}, aware UTC datetimes, filled from history in the db thread
    (queued after the writes that prompted it) and read in the GUI thread. An
    encrypted candidate is decrypted with its account's key and the plaintext
    written back, as opening the conversation would. Changes are coalesced and
    announced as BlinkConversationPreviewsDidChange with data.keys (None: all).
    """

    delay = 0.5

    def __init__(self):
        self.previews = {}
        self.message_times = {}
        self.call_times = {}
        self._dirty = set()
        self._dirty_all = False
        self._scheduled = False
        self._keys = {}             # account id: (path, mtime, PGPKey or None)
        self._undecryptable = set()
        notification_center = NotificationCenter()
        for name in ('SIPApplicationDidStart', 'BlinkMessageHistoryMessageDidStore', 'BlinkMessageHistoryAllContactsDidSucceed',
                     'BlinkMessageHistoryConversationDidRemove', 'BlinkMessageDidDecrypt', 'BlinkMessageWillDelete',
                     'BlinkGotHistoryMessageUpdate', 'BlinkMessageHistoryCallHistoryDidStore', 'BlinkGotHistoryCallRecord',
                     'BlinkMessageHistoryCallRecordDidStore'):
            notification_center.add_observer(self, name=name)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, None)
        if handler is not None:
            handler(notification)
        else:
            self.invalidate()

    def _NH_BlinkMessageHistoryMessageDidStore(self, notification):
        self.invalidate([notification.data.remote_uri])

    def _NH_BlinkMessageHistoryConversationDidRemove(self, notification):
        self.invalidate([notification.data.contact])

    def _NH_BlinkMessageHistoryCallHistoryDidStore(self, notification):
        self.invalidate([notification.data.message.remote_uri])

    def _NH_BlinkMessageHistoryCallRecordDidStore(self, notification):
        self.invalidate([notification.data.remote_uri])

    def preview(self, keys):
        """The newest preview among a contact's conversation keys, or None."""
        found = [self.previews[key] for key in keys if key in self.previews]
        return max(found)[1] if found else None

    def message_time(self, keys):
        """When the newest message (not call) among a contact's conversation keys was, or None."""
        return max((self.message_times[key] for key in keys if key in self.message_times), default=None)

    def call_time(self, keys):
        """When the newest call among a contact's conversation keys was, or None."""
        return max((self.call_times[key] for key in keys if key in self.call_times), default=None)

    @run_in_gui_thread
    def invalidate(self, keys=None):
        if keys is None:
            self._dirty_all = True
        else:
            self._dirty.update(str(key) for key in keys if key)
        if not self._scheduled:
            self._scheduled = True
            call_later(self.delay, self._flush)

    def _flush(self):
        keys = None if self._dirty_all else sorted(self._dirty)
        self._dirty.clear()
        self._dirty_all = False
        self._scheduled = False
        if keys == []:
            return
        self._load(keys)

    @run_in_thread('db')
    def _load(self, keys):
        try:
            history = MessageHistory()
            rows, reaction_ids = history.last_text_messages(remote_uri=keys)
            message_times = {key: _row_time(value) for key, value in history.last_message_times(remote_uri=keys).items()}
            call_times = {key: _row_time(value) for key, value in history.last_call_times(remote_uri=keys).items()}
        except Exception as e:
            ActivityLog().error(f'[db] Loading conversation previews failed: {e}')
            return
        found = {}
        for row in rows:        # newest first within each conversation
            remote = row['remote_uri']
            if remote in found:
                continue
            body = row['content']
            if isinstance(body, bytes):
                body = body.decode('utf-8', 'replace')
            if is_pgp_armoured(body):
                body = self._decrypt(row, body)
            text = conversation_preview(body, row['content_type'], row['message_id'], reaction_ids)
            if text:
                found[remote] = (_row_time(row['timestamp']) or datetime.fromtimestamp(0, timezone.utc), text)
        self._apply(keys, {'previews': found, 'message_times': message_times, 'call_times': call_times})

    @run_in_gui_thread
    def _apply(self, keys, loaded):
        changed = set()
        for name, found in loaded.items():
            current = getattr(self, name)
            found = {key: value for key, value in found.items() if value is not None}
            if keys is None:
                changed.update(key for key in set(current) | set(found) if current.get(key) != found.get(key))
                setattr(self, name, found)
                continue
            for key in keys:
                value = found.get(key)
                if current.get(key) != value:
                    changed.add(key)
                    if value is None:
                        current.pop(key, None)
                    else:
                        current[key] = value
        if changed:
            NotificationCenter().post_notification('BlinkConversationPreviewsDidChange', sender=self, data=NotificationData(keys=changed))

    # Decryption (db thread)

    def _private_key(self, account_id):
        if account_id == BONJOUR_ACCOUNT_ID:
            account = BonjourAccount()
        else:
            try:
                account = AccountManager().get_account(account_id)
            except KeyError:
                return None
        setting = account.sms.private_key
        path = setting.normalized if setting is not None else None
        if not path or not os.path.exists(path):
            return None
        mtime = os.path.getmtime(path)
        cached = self._keys.get(account_id)
        if cached is not None and cached[:2] == (path, mtime):
            return cached[2]
        try:
            import pgpy
            key, _ = pgpy.PGPKey.from_file(path)
        except Exception as e:
            ActivityLog().warning(f'[pgp] Cannot read the private key of {account_id} for conversation previews: {e}')
            key = None
        self._keys[account_id] = (path, mtime, key)
        self._undecryptable.clear()     # a new key may open what the old one could not
        return key

    def _decrypt(self, row, body):
        message_id = row['message_id']
        if not message_id or message_id in self._undecryptable:
            return None
        key = self._private_key(row['account_id'])
        if key is None:
            return None
        try:
            import pgpy
            plaintext = key.decrypt(pgpy.PGPMessage.from_blob(body)).message
            if isinstance(plaintext, (bytes, bytearray)):
                plaintext = bytes(plaintext).decode('utf-8')
        except Exception as e:
            self._undecryptable.add(message_id)
            log.debug(f'Message {message_id} could not be decrypted for its preview: {e}')
            return None
        MessageHistory().update_decrypted_message(message_id, plaintext)
        return plaintext


@implementer(IObserver)
class ConversationTyping(object, metaclass=Singleton):
    """Which conversations the other party is typing in, by conversation key.

    From is-composing indications (replicated ones, our own typing on another
    device, never get here). An active state lasts its refresh interval plus a
    second unless renewed; idle, or a message stored from them, ends it.
    Changes are announced as BlinkConversationPreviewsDidChange with data.keys,
    as the contact row's second line is what they change.
    """

    grace = 1

    def __init__(self):
        self.typing = {}            # key: expiry (time.monotonic())
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkGotComposingIndication')
        notification_center.add_observer(self, name='BlinkMessageHistoryMessageDidStore')

    def is_typing(self, keys):
        now = time.monotonic()
        return any(self.typing.get(key, 0) > now for key in keys)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkGotComposingIndication(self, notification):
        session = notification.sender
        try:
            key = bare_instance_id(session.remote_instance_id) if session.remote_instance_id else conversation_key(str(session.contact.uri.uri), session.account)
        except AttributeError:
            return
        if not key:
            return
        data = notification.data
        if data.state == 'active':
            refresh = data.refresh or 120
            was_typing = self.is_typing([key])
            self.typing[key] = time.monotonic() + refresh + self.grace
            call_later(refresh + self.grace, self._expire, key)
            if not was_typing:
                self._changed(key)
        else:
            self._stop(key)

    def _NH_BlinkMessageHistoryMessageDidStore(self, notification):
        if notification.data.direction == 'incoming':
            self._stop(str(notification.data.remote_uri))

    def _stop(self, key):
        if self.typing.pop(key, None) is not None:
            self._changed(key)

    def _expire(self, key):
        expiry = self.typing.get(key)
        if expiry is not None and expiry <= time.monotonic() + 0.05:
            del self.typing[key]
            self._changed(key)

    def _changed(self, key):
        NotificationCenter().post_notification('BlinkConversationPreviewsDidChange', sender=self, data=NotificationData(keys={key}))


@implementer(IObserver)
class ConversationLocations(object, metaclass=Singleton):
    """Which conversations the other party is sharing their location in, by conversation key.

    A share is running from its start (location_start, meeting_start, received)
    until a teardown tick (location_stop, meeting_end, meeting_reject), its
    expiry, or, when it says none, until it has been quiet for idle_limit.
    Read from history at start and again when a location tick is stored; while
    any share runs it is looked at every check_interval seconds for expiry.
    Changes are announced as BlinkConversationPreviewsDidChange with data.keys:
    the contact row's second line says it.
    """

    start_actions = ('location_start', 'meeting_start')
    teardown_actions = ('location_stop', 'meeting_end', 'meeting_reject')
    window = 24 * 3600              # seconds: a share older than this is not looked at
    idle_limit = 30 * 60            # seconds without a tick for a share that has no expiry
    check_interval = 60

    def __init__(self):
        self.active = {}            # key: 'live' or 'meet'
        self._checking = False
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='BlinkMessageHistoryLocationDidStore')

    def sharing(self, keys):
        """'live' or 'meet' when the other party shares their location in one of these conversations, else None."""
        return next((self.active[key] for key in keys if key in self.active), None)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        self._scan(None)

    def _NH_BlinkMessageHistoryLocationDidStore(self, notification):
        self._scan([str(notification.data.remote_uri)])

    def _tick(self):
        self._checking = False
        if self.active:
            self._scan(list(self.active))

    @run_in_thread('db')
    def _scan(self, keys):
        from blink.location import location_envelope, row_metadata
        try:
            db = Message._connection
            table = Message.sqlmeta.table
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            since = datetime.fromtimestamp(time.time() - self.window, timezone.utc).replace(tzinfo=None)
            starts = ', '.join(db.sqlrepr(action) for action in self.start_actions)
            where = (f"direction = 'incoming' and related_action in ({starts}) and timestamp > {db.sqlrepr(since)} and {NOT_DELETED_SQL}"
                     + (f" and remote_uri in ({', '.join(db.sqlrepr(key) for key in keys)})" if keys else ''))
            found = {}
            for row in Message.select(where, orderBy='timestamp'):
                session = row.related_msg_id or row.message_id
                ticks = db.queryAll(f'select related_action, max(timestamp) from {table} where related_msg_id = {db.sqlrepr(session)} and {NOT_DELETED_SQL} group by related_action')
                if any(action in self.teardown_actions for action, _ in ticks):
                    continue
                expires = None
                try:
                    envelope = location_envelope(row.content, row_metadata(row.metadata, row.related_action, row.related_msg_id))
                    expires = self._time(envelope.get('expires')) if envelope else None
                except Exception:
                    pass
                if expires is not None:
                    if expires <= now:
                        continue
                else:
                    last = max([when for when in (self._time(when) for _, when in ticks) if when is not None] + [row.timestamp])
                    if (now - last).total_seconds() > self.idle_limit:
                        continue
                found[str(row.remote_uri)] = 'meet' if row.related_action.startswith('meeting') else 'live'
        except Exception as e:
            log.warning(f'Cannot read the running location shares: {e!r}')
            return
        call_in_gui_thread(self._apply, keys, found)

    @staticmethod
    def _time(value):
        if value is None or value == '':
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).replace(tzinfo=None)
        text = str(value).strip().replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed

    def _apply(self, keys, found):
        scope = set(keys) if keys is not None else set(self.active) | set(found)
        changed = set()
        for key in scope:
            kind = found.get(key)
            if self.active.get(key) != kind:
                changed.add(key)
                if kind is None:
                    self.active.pop(key, None)
                    ActivityLog().info(f'[location] {key} is no longer sharing their location')
                else:
                    self.active[key] = kind
                    ActivityLog().info(f'[location] {key} is sharing their location' + (' (meet-up)' if kind == 'meet' else ''))
        if changed:
            NotificationCenter().post_notification('BlinkConversationPreviewsDidChange', sender=self, data=NotificationData(keys=changed))
        if self.active and not self._checking:
            self._checking = True
            call_later(self.check_interval, self._tick)


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
