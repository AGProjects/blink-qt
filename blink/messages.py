import bisect
import dns.resolver
import json
import os
import re
import requests
import threading
import time
import urllib3
import random
import urllib
import uuid
import pgpy

from collections import Counter, OrderedDict, deque

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QStyle, QDialog, QMessageBox

from pgpy import PGPMessage
from pgpy.errors import PGPEncryptionError, PGPDecryptionError

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import makedirs, host, unlink
from application.python.types import Singleton
from datetime import datetime, timezone, timedelta
from dateutil.tz import tzlocal, tzutc
from urllib.parse import urlsplit, urlunsplit, quote
from zope.interface import implementer

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.addressbook import AddressbookManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, FromHeader, Header, ToHeader, Message, RouteHeader
from sipsimple.core._core import PJSIPError
from sipsimple.lookup import DNSLookup
from sipsimple.payloads import ParserError
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.payloads.imdn import IMDNDocument, DeliveryNotification, DisplayNotification
from sipsimple.payloads.rcsfthttp import FTHTTPDocument, FileInfo
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError, CPIMNamespace, CPIMHeader, ChatIdentity, ChatStream, Message as MSRPChatMessage, SimplePayload
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.configuration.datatypes import File
from blink.file_transfer import FILE_TRANSFER_PATH, base_url_from_transfer, derive_base_url, upload_url
from blink.message_envelopes import API_MESSAGES_BULK_DELETE_CONTENT_TYPE, DATA_EXPORT_CONTENT_TYPE, MESSAGES_BULK_DELETE_CONTENT_TYPE
from blink.message_envelopes import ADDRESSBOOK_UPDATE_CONTENT_TYPE, CALL_CONTENT_TYPE, call_recording_envelope, FILE_TRANSFER_CONTENT_TYPES, file_transfer_envelope, LOCATION_CONTENT_TYPE, METADATA_CONTENT_TYPE, conversation_read_envelope, conversation_read_marker, foreign_call_record, metadata_link, this_device_id
from blink.location import storage_fields as location_storage_fields
from blink import key_escrow
from blink.journal import FIRST_SYNC_MARKER, KNOWN_INERT_CONTENT_TYPES, is_file_transfer_notice, JournalCache, JournalStats, OwnMarkers, SeenMessageIds, journal_action, parse_payload
from blink.logging import ActivityLog, JournalLog, MessagingTrace as log
from blink.resources import ApplicationData, Resources, load_ui_type
from blink.sessions import SessionManager, StreamDescription, IncomingDialogBase
from blink.uris import bare_instance_id, canonical_uri, placeholder_instance_id
from blink.util import call_in_gui_thread, call_later, run_in_gui_thread, translate

__all__ = ['MessageManager', 'BlinkMessage', 'msrp_message_id', 'send_msrp_message']

dns_error_map = {dns.resolver.NXDOMAIN: 'Domain not found in DNS',
                 dns.resolver.NoAnswer: 'DNS response contains no answer',
                 dns.resolver.NoNameservers: 'no DNS name servers could be reached',
                 dns.resolver.Timeout: 'no DNS response received, the query has timed out'}

ui_class, base_class = load_ui_type('generate_pgp_key_dialog.ui')


class GeneratePGPKeyDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(GeneratePGPKeyDialog, self).__init__(parent)

        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)

        self.slot = None
        self.generate_button = self.dialog_button_box.addButton(translate("generate_pgp_key_dialog", "Generate"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.generate_button.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))

    def show(self, activate=True):
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        super(GeneratePGPKeyDialog, self).show()


class GeneratePGPKeyRequest(QObject):
    finished = pyqtSignal(object)
    accepted = pyqtSignal(object)
    rejected = pyqtSignal(object)
    sip_prefix_re = re.compile('^sips?:')
    priority = 0

    def __init__(self, dialog, account, scenario=0, session=None):
        super(GeneratePGPKeyRequest, self).__init__()
        self.account = account
        self.dialog = dialog
        self.session = session
        self.dialog.finished.connect(self._SH_DialogFinished)

        uri = self.sip_prefix_re.sub('', str(account.uri))
        replaced1 = self.dialog.key_maybe_present_label.text().replace('ACCOUNT', uri)
        replaced2 = self.dialog.key_present_label.text().replace('ACCOUNT', uri)

        self.dialog.key_maybe_present_label.setText(replaced1)
        self.dialog.key_present_label.setText(replaced2)

        if scenario == 2:
            # the server keeps a public key, nothing on this device nor in the addressbook: another device has it
            self.dialog.key_maybe_present_label.setText(translate('generate_pgp_key_dialog',
                'There is no private key for %s on this computer and no backup of it on the server, but the server '
                'keeps its public key: the private key exists on another device where you use this account.\n\n'
                "Choose 'Export private key' on that device to import it here.") % uri)
            self.dialog.key_maybe_present_label.show()
            self.dialog.key_present_label.hide()
        elif scenario == 1:
            self.dialog.key_maybe_present_label.show()
            self.dialog.key_present_label.hide()
        else:
            self.dialog.key_maybe_present_label.hide()
            self.dialog.key_present_label.show()

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

    def _SH_DialogFinished(self, result):
        self.finished.emit(self)
        if result == QDialog.DialogCode.Accepted:
            self.accepted.emit(self)
        elif result == QDialog.DialogCode.Rejected:
            self.rejected.emit(self)


del ui_class, base_class
ui_class, base_class = load_ui_type('import_private_key_dialog.ui')


class ImportDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(ImportDialog, self).__init__(parent)

        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)

        self.slot = None
        self.import_button = self.dialog_button_box.addButton(translate("import_key_dialog", "Import"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.import_button.setEnabled(False)

    def show(self, activate=True):
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        super(ImportDialog, self).show()


class ImportPrivateKeyRequest(QObject):
    finished = pyqtSignal(object)
    accepted = pyqtSignal(object, str)
    rejected = pyqtSignal(object)
    sip_prefix_re = re.compile('^sips?:')
    priority = 6

    def __init__(self, dialog, body, account):
        super(ImportPrivateKeyRequest, self).__init__()
        self.account = account
        self.dialog = dialog
        self.dialog.pin_code_input.textChanged.connect(self._SH_ChatInputTextChanged)
        self.stylesheet = self.dialog.pin_code_input.styleSheet()
        self.reset = False
        self.dialog.finished.connect(self._SH_DialogFinished)

        uri = self.sip_prefix_re.sub('', str(account.uri))
        self.dialog.account_value_label.setText(uri)
        regex = "(?P<before>.*)(?P<pgp_message>-----BEGIN PGP MESSAGE-----.*-----END PGP MESSAGE-----)(?P<after>.*)"
        matches = re.search(regex, body, re.DOTALL)

        pgp_message = matches.group('pgp_message')
        self.before = matches.group('before')
        self.after = matches.group('after')
        self.pgp_message = PGPMessage.from_blob(pgp_message.encode())

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

    def _SH_ChatInputTextChanged(self, text):
        if len(text) == 6:
            try:
                decrypted_pgp_key = self.pgp_message.decrypt(text.strip())
                self.private_key = decrypted_pgp_key.message
            except PGPDecryptionError as e:
                log.warning(f'Decryption of public_key import failed: {e}')
                new_stylesheet = f"color: #800000; background-color: #ffcfcf; {self.stylesheet}"
                self.dialog.pin_code_input.setStyleSheet(new_stylesheet)
                self.reset = True
            else:
                self.dialog.import_button.setEnabled(True)
                self.dialog.pin_code_input.setEnabled(False)
                new_stylesheet = f"color: #00a000; background-color: #d8ffd8; {self.stylesheet}"
                self.dialog.pin_code_input.setStyleSheet(new_stylesheet)
        else:
            self.dialog.import_button.setEnabled(False)
            if self.reset:
                self.dialog.pin_code_input.setStyleSheet(self.stylesheet)
                self.reset = False

    def _SH_DialogFinished(self, result):
        self.finished.emit(self)
        if result == QDialog.DialogCode.Accepted:
            self.accepted.emit(self, f'{self.before}{self.private_key}{self.after}')
        elif result == QDialog.DialogCode.Rejected:
            self.rejected.emit(self)


del ui_class, base_class
ui_class, base_class = load_ui_type('export_private_key_dialog.ui')


class ExportDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(ExportDialog, self).__init__(parent)

        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)

        self.slot = None
        self.export_button = self.dialog_button_box.addButton(translate("export_key_dialog", "Export"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.export_button.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.export_button.setEnabled(False)

    def accept(self):
        pass

    def show(self, activate=True):
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        super(ExportDialog, self).show()


class ExportPrivateKeyRequest(QObject):
    finished = pyqtSignal(object)
    accepted = pyqtSignal(object, str)
    rejected = pyqtSignal(object)
    sip_prefix_re = re.compile('^sips?:')
    priority = 5

    def __init__(self, dialog, account):
        super(ExportPrivateKeyRequest, self).__init__()
        self.account = account
        self.dialog = dialog
        self.dialog.finished.connect(self._SH_DialogFinished)

        uri = self.sip_prefix_re.sub('', str(account.uri))
        self.dialog.account_value_label.setText(uri)
        self.pincode = ''.join([str(random.randint(0, 99)).zfill(2) for _ in range(3)])
        self.dialog.pincode_value_label.setText(self.pincode)

        settings = SIPSimpleSettings()
        id = account.id.replace('/', '_')

        directory = os.path.join(settings.chat.keys_directory.normalized, 'private')
        filename = os.path.join(directory, f'{id}')

        with open(f'{filename}.privkey', 'rb') as f:
            private_key = f.read().decode()

        with open(f'{filename}.pubkey', 'rb') as f:
            self.public_key = f.read().decode()

        self.dialog.export_button.clicked.connect(self._SH_ExportButtonClicked)
        try:
            pgp_message = PGPMessage.new(private_key)
            self.enc_message = pgp_message.encrypt(self.pincode)
        except PGPEncryptionError:
            pass
        else:
            self.dialog.export_button.setEnabled(True)

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

    def _SH_ExportButtonClicked(self):
        self.accepted.emit(self, f'{self.public_key}{str(self.enc_message)}')
        self.dialog.export_button.setEnabled(False)

    def _SH_DialogFinished(self, result):
        self.finished.emit(self)
        if result == QDialog.DialogCode.Rejected:
            self.rejected.emit(self)


del ui_class, base_class


def _bare(uri):
    """An address for the logs: no sip: or sips: in front."""
    text = str(uri)
    for scheme in ('sips:', 'sip:'):
        if text.lower().startswith(scheme):
            return text[len(scheme):]
    return text


def _header_text(headers, name):
    """A SIP header's value as text, or None."""
    header = headers.get(name, None)
    if header is None or header is Null:
        return None
    value = getattr(header, 'body', None)
    if value is None:
        value = str(header)
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    value = ' '.join(str(value).split())[:255]
    return value or None


class BlinkMessage(MSRPChatMessage):
    __slots__ = 'id', 'disposition', 'is_secure', 'direction', 'metadata'

    def __init__(self, content, content_type, sender=None, recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None, disposition=None, is_secure=False, direction=None, metadata=None):
        super(BlinkMessage, self).__init__(content, content_type, sender, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)
        self.id = id if id is not None else str(uuid.uuid4())
        self.disposition = disposition
        self.is_secure = is_secure
        self.direction = direction
        self.metadata = metadata    # cleartext side-band (CPIM agp.Metadata, journal metadata): location v2, call records


# MSRP chat messages are kept in history like SIP messages, by the same id on both ends: the
# sender puts it in the CPIM imdn.Message-ID header (send_msrp_message), the receiver reads it
# from there (msrp_message_id). The MSRP message-id is the same value, so delivery reports find
# the row; it is at most 32 characters (RFC 4975), hence an uuid without dashes.

def msrp_message_id(message):
    """The id of a received MSRP chat message: its imdn.Message-ID, else one made from what it
    is (sender, time, content), the same for everyone who asks (history and the MSRP window)."""
    for header in getattr(message, 'additional_headers', None) or ():
        if header.name == 'Message-ID' and header.value:
            return str(header.value)
    content = message.content if isinstance(message.content, bytes) else str(message.content or '').encode('utf-8', 'replace')
    import hashlib
    digest = hashlib.sha1(b'%s|%s|%s|' % (str(message.sender.uri if message.sender is not None else '').encode(), str(message.timestamp).encode(), str(message.content_type).encode()) + content).hexdigest()
    return str(uuid.UUID(digest[:32]).hex)


# A displayed receipt on an MSRP chat comes back over the stream as a CPIM wrapped IMDN
# (delivery is the MSRP success report): advertise it in a=accept-wrapped-types.
if IMDNDocument.content_type not in ChatStream.accept_wrapped_types:
    ChatStream.accept_wrapped_types = ChatStream.accept_wrapped_types + [IMDNDocument.content_type]


def send_msrp_message(chat_stream, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None):
    """Send a chat message over MSRP with an id of ours (also its imdn.Message-ID when the stream
    wraps it in CPIM; a header would force CPIM on a peer without it and fail), returned."""
    from msrplib.session import contains_mime_type
    from sipsimple.streams.msrp.chat import QueuedMessage
    message_id = uuid.uuid4().hex
    headers = [header for header in (additional_headers or ()) if header.name != 'Message-ID']
    if getattr(chat_stream, 'cpim_enabled', False) and contains_mime_type(getattr(chat_stream, 'remote_accept_wrapped_types', None) or [], content_type):
        imdn_ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
        headers.append(CPIMHeader('Message-ID', imdn_ns, message_id))
        # one to one only: in a room every participant would answer
        blink_session = getattr(chat_stream, 'blink_session', None)
        account = getattr(blink_session, 'account', None)
        if content_type.startswith('text/') and not getattr(chat_stream.session, 'remote_focus', False) and getattr(getattr(account, 'sms', None), 'enable_imdn', False):
            headers.append(CPIMHeader('Disposition-Notification', imdn_ns, 'display'))
    message = QueuedMessage(content, content_type, recipients=recipients, courtesy_recipients=courtesy_recipients, subject=subject, timestamp=timestamp,
                            required=required, additional_headers=headers, id=message_id, notify_progress=True)
    chat_stream._enqueue_message(message)
    return message_id


class OTRInternalMessage(BlinkMessage):
    def __init__(self, content):
        super(OTRInternalMessage, self).__init__(content, 'text/plain')


def can_use_cpim(content_type):
    """Requests to the server API and public keys go out bare, as Blink for macOS and
    Sylk Mobile send them: the server reads their body, not a CPIM wrapper."""
    content_type = str(content_type or '').lower()
    return not (content_type.startswith('application/sylk-api') or content_type == 'text/pgp-public-key')


def skip_journal_headers(content, otr=False, skip=False):
    """[X-Sylk-Skip-Journal] for an OTR message or when asked to (skip), else []. An OTR
    ciphertext is bound to the session that made it: replayed from the journal on another
    device it can never be read. A notice for our other devices (addressbook update) is
    stale by the time an offline device would replay it. SylkServer checks for the
    header's presence, on both sides of the relay."""
    if skip or otr or (isinstance(content, bytes) and content.startswith(b'?OTR')):
        return [Header('X-Sylk-Skip-Journal', 'yes')]
    return []


@implementer(IObserver)
class OutgoingMessage(object):
    __ignored_content_types__ = {IsComposingDocument.content_type, IMDNDocument.content_type}  # Content types to ignore in notifications
    __disabled_imdn_content_types__ = {'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-api'}.union(__ignored_content_types__)  # Content types to ignore in notifications

    def __init__(self, account, contact, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None, session=None, use_cpim=True, skip_journal=False, sessionless=False, on_result=None):
        self.lookup = None
        self.on_result = on_result          # on_result(code, reason) once: the final answer, or code None when it could not be sent
        self.sessionless = sessionless     # sent without a conversation, straight to the account's proxy
        self.skip_journal = skip_journal
        self.account = account
        self.uri = contact.uri.uri
        self.content_type = content_type
        self.content = content
        self.id = id if id is not None else str(uuid.uuid4())
        self.timestamp = timestamp if timestamp is not None else ISOTimestamp.now()
        address = str(self.uri)
        if '@' not in address and isinstance(account, Account):
            address += '@' + account.id.domain      # a number or bare user (conversation key '+318008185'): at the account's domain, as a session does
        self.sip_uri = SIPURI.parse('sip:%s' % address)
        self.session = session
        self.contact = contact
        self.is_secure = False
        self.dns_failed_reason = None
        self.use_cpim = use_cpim and can_use_cpim(content_type)

    @property
    def message(self):
        return BlinkMessage(self.content, self.content_type, self.account, timestamp=self.timestamp, id=self.id, is_secure=self.is_secure, direction='outgoing')

    def _report(self, code, reason):
        callback, self.on_result = self.on_result, None
        if callback is not None:
            if isinstance(reason, bytes):
                reason = reason.decode('utf-8', 'replace')
            try:
                callback(code, reason)
            except Exception as e:
                ActivityLog().exception(f'[Message] Result handler of {self.content_type} message {self.id} failed: {e!r}')

    @property
    def _disabled_imdn_content_type(self):
        return any(self.content_type.lower().startswith(prefix) for prefix in self.__disabled_imdn_content_types__)

    def _lookup(self):
        settings = SIPSimpleSettings()
        if isinstance(self.account, Account):
            if self.account.sip.outbound_proxy is not None:
                proxy = self.account.sip.outbound_proxy
                uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
            elif self.account.sip.always_use_my_proxy:
                uri = SIPURI(host=self.account.id.domain)
            else:
                uri = self.sip_uri
        else:
            uri = self.sip_uri

        self.lookup = DNSLookup()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.lookup)
        self.lookup.lookup_sip_proxy(uri, settings.sip.transport_list, tls_name=self.account.sip.tls_name or uri.host)

    def _send(self, routes=None):
        if routes is not None or self.session.routes:
            notification_center = NotificationCenter()
            routes = routes if routes is not None else self.session.routes
            from_uri = self.account.uri
            content = self.content
            if self.account is BonjourAccount():
                settings = SIPSimpleSettings()
                from_uri.parameters['instance_id'] = settings.instance_id

            if self.session is not None:
                stream = self.session.fake_streams.get('messages')
                if not stream:
                    data = NotificationData(originator='remote', reason=f"No chat stream established", id=self.id, code=None)
                    notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=data)
                    log.error('Message %s failed: no chat stream established' % self.id)
                    return

                if not self._disabled_imdn_content_type:
                    if self.account.sms.enable_pgp and stream.can_encrypt:
                        try:
                            content = stream.encrypt(self.content, self.content_type)
                        except Exception as e:
                            reason = f"Encryption error {str(e)}"
                            data = NotificationData(originator='remote', reason=reason, id=self.id, code=None)
                            notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=data)
                            log.info(f'Message {self.id} to {str(self.sip_uri)[4:]} failed: {reason}')
                            return
                        self.is_secure = True
            content = content if isinstance(content, bytes) else content.encode()
            additional_sip_headers = skip_journal_headers(content, skip=self.skip_journal)
            if additional_sip_headers:
                why = 'a notice for the other devices' if self.skip_journal else 'OTR'
                ActivityLog().info(f'[Message with {self._peer}] Sending {self.content_type} message {self.id} without journalling it ({why})')
            if self.account.sms.use_cpim and self.use_cpim:
                ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
                additional_headers = [CPIMHeader('Message-ID', ns, self.id)]
                if self.account.sms.enable_imdn and not self._disabled_imdn_content_type:
                    additional_headers.append(CPIMHeader('Disposition-Notification', ns, 'positive-delivery, display'))
                payload = CPIMPayload(content,
                                      self.content_type,
                                      charset='utf-8',
                                      sender=ChatIdentity(from_uri, self.account.display_name),
                                      recipients=[ChatIdentity(self.sip_uri, None)],
                                      timestamp=str(self.timestamp),
                                      additional_headers=additional_headers)
                payload, content_type = payload.encode()
            else:
                payload = content
                content_type = self.content_type

            route = routes[0]
            message_request = Message(FromHeader(from_uri, self.account.display_name),
                                      ToHeader(self.sip_uri),
                                      RouteHeader(route.uri),
                                      content_type,
                                      payload,
                                      credentials=self.account.credentials,
                                      extra_headers=additional_sip_headers)
            notification_center.add_observer(self, sender=message_request)
            if self.is_secure:
                notification_center.post_notification('BlinkMessageDidEncrypt', sender=self.session, data=NotificationData(message=self.message))
            try:
                message_request.send()
            except PJSIPError as e:
                log.info(f'Message {self.id} to {str(self.sip_uri)[4:]} failed: {str(e)}')
                notification_center = NotificationCenter()
                data = NotificationData(originator='local', reason=str(e), id=self.id, code=None)
                notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=data)
            else:
                log.info(f'Message {self.id} to {str(self.sip_uri)[4:]} sending...')
        else:
            pass
            # TODO

    # sent without a conversation (no session): straight to the account's proxy
    __sessionless_content_types__ = ('text/pgp-private-key', 'application/sylk-api-token', 'application/sylk-api-pgp-key-lookup')

    def send(self):
        if self.sessionless or self.content_type.lower() in self.__sessionless_content_types__:
            self._lookup()
            return

        if self.session is None:
            return

        if not self._disabled_imdn_content_type:
            notification_center = NotificationCenter()
            notification_center.post_notification('BlinkMessageIsPending', sender=self.session, data=NotificationData(message=self.message, id=self.id))

        if self.session.routes and self.session.account == self.account:
            self._send()
        else:
            self._lookup()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_DNSLookupTrace(self, notification):
        if notification.data.error and notification.data.query_type == 'A':
            # isinstance, not the exact class: dnspython raises subclasses (LifetimeTimeout for Timeout)
            error = notification.data.error
            reason = next((text for kind, text in dns_error_map.items() if isinstance(error, kind)), '') or str(error)
            self.dns_failed_reason = reason

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if notification.sender is self.lookup:
            routes = notification.data.result
            if self.sessionless or self.content_type.lower() in self.__sessionless_content_types__:
                self._send(routes)
                return

            # TODO: Figure out how now to send a public key when required, not always on start of the first message in the session -Tijmen
            if self.content_type != 'text/pgp-public-key' and not self.session.routes:
                stream = self.session.fake_streams.get('messages')
                if stream and self.session.account.sms.enable_pgp and stream.can_decrypt:
                    directory = os.path.join(SIPSimpleSettings().chat.keys_directory.normalized, 'private')
                    filename = os.path.join(directory, f'{self.session.account.id}')

                    with open(f'{filename}.pubkey', 'rb') as f:
                        public_key = f.read().decode()
                    public_key_message = OutgoingMessage(self.session.account, self.contact, str(public_key), 'text/pgp-public-key', session=self.session)
                    MessageManager()._send_message(public_key_message)
                if stream and self.account is not BonjourAccount() and self.account.sms.enable_pgp and not stream.can_encrypt:
                    lookup_message = OutgoingMessage(self.account, self.contact, 'Public key request', 'application/sylk-api-pgp-key-lookup', session=self.session)
                    lookup_message.send()
            self.session.routes = routes
            self._send()

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        self._report(None, str(self.dns_failed_reason or notification.data.error))
        if self.content_type.lower() == IsComposingDocument.content_type:
            return

        if self.session is None:
            return

        reason = self.dns_failed_reason or notification.data.error
        # local (sent again later): no name server answered, or the lookup timed out; the SDK says
        # so in originator. remote (not sent again): the name does not exist or has no SIP records
        originator = getattr(notification.data, 'originator', None) or ('local' if 'no DNS' in str(reason) or 'Timeout' in str(reason) else 'remote')

        log.info(f'DNS lookup for message {self.id} failed {originator}ly: {reason}')

        data = NotificationData(reason=reason, originator=originator, id=self.id, code=None)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=data)

    @property
    def _peer(self):
        # a Bonjour neighbour by instance id, not by today's address
        return getattr(self.session, 'remote_instance_id', None) or _bare(self.uri)

    def _NH_SIPMessageDidSucceed(self, notification):
        self._report(getattr(notification.data, 'code', 200), getattr(notification.data, 'reason', ''))
        notification_center = NotificationCenter()
        if self.content_type.lower() in self.__ignored_content_types__:
            if self.content_type.lower() == IMDNDocument.content_type:
                document = IMDNDocument.parse(self.content)
                imdn_message_id = document.message_id.value
                imdn_status = document.notification.status.__str__()
                log.info(f'Disposition {imdn_status} of message {imdn_message_id} sent to {self._peer} (IMDN {self.id})')
                notification_center.post_notification('BlinkDidSendDispositionNotification', sender=self.session, data=NotificationData(id=imdn_message_id, status=imdn_status))
            return

        log.info(f'Message {self.id} {self.content_type.lower()} sent to {self._peer} from account {self.account.id}: {getattr(notification.data, "code", "")} {getattr(notification.data, "reason", "")}'.rstrip())
        if not self._disabled_imdn_content_type:
            from blink.history import MessageHistory
            MessageHistory().record_agent(self.id, 'sent', ' '.join(str(SIPSimpleSettings().user_agent or '').split())[:255] or None)    # this device
        if self.session is not None:
            notification_center.post_notification('BlinkMessageDidSucceed', sender=self.session, data=NotificationData(data=notification.data, id=self.id))
        if not self._disabled_imdn_content_type:
            ActivityLog().info(f'[Message with {self._peer}] Sent {self.content_type} message {self.id} from account {self.account.id}')

    def _NH_SIPMessageDidFail(self, notification):
        self._report(getattr(notification.data, 'code', None), getattr(notification.data, 'reason', ''))
        content_type = self.content_type.lower()
        if content_type in self.__disabled_imdn_content_types__ or content_type.startswith('application/sylk-api'):
            return

        if self.session is None:
            return

        originator = 'local'
        if hasattr(notification.data, 'headers'):
            originator = 'remote'

        reason = notification.data.reason.decode() if isinstance(notification.data.reason, bytes) else notification.data.reason
        data = NotificationData(reason=reason, originator=originator, id=self.id, code=notification.data.code)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=data)

        try:
            code = notification.data.code
        except AttributeError:
            code = ''

        log.info(f'Message {self.id} to {_bare(self.session.contact_uri.uri)} failed {originator}ly: {code} {reason}')
        ActivityLog().warning(f'[Message with {self._peer}] Sending {self.content_type} message {self.id} from account {self.account.id} failed {originator}ly: {code} {reason}')


@implementer(IObserver)
class InternalOTROutgoingMessage(OutgoingMessage):
    @property
    def message(self):
        return OTRInternalMessage(self.content, self.content_type)

    def _send(self, routes=None):
        if routes is not None or self.session.routes:
            notification_center = NotificationCenter()
            routes = routes if routes is not None else self.session.routes
            from_uri = self.account.uri
            content = self.content
            content = content if isinstance(content, bytes) else content.encode()
            additional_sip_headers = skip_journal_headers(content, otr=True)   # OTR protocol traffic
            if self.account.sms.use_cpim:
                ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
                additional_headers = [CPIMHeader('Message-ID', ns, self.id)]
                payload = CPIMPayload(content,
                                      self.content_type,
                                      charset='utf-8',
                                      sender=ChatIdentity(from_uri, self.account.display_name),
                                      recipients=[ChatIdentity(self.sip_uri, None)],
                                      timestamp=str(self.timestamp),
                                      additional_headers=additional_headers)
                payload, content_type = payload.encode()
            else:
                payload = content
                content_type = self.content_type

            route = routes[0]
            message_request = Message(FromHeader(from_uri, self.account.display_name),
                                      ToHeader(self.sip_uri),
                                      RouteHeader(route.uri),
                                      content_type,
                                      payload,
                                      credentials=self.account.credentials,
                                      extra_headers=additional_sip_headers)
            notification_center.add_observer(self, sender=message_request)
            message_request.send()
        else:
            pass
            # TODO

    def send(self):
        if self.session is None:
            return

        if self.session.routes:
            self._send()
        else:
            self._lookup()

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if notification.sender is self.lookup:
            routes = notification.data.result
            self.session.routes = routes
            self._send()

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        return

    def _NH_SIPMessageDidSucceed(self, notification):
        return

    def _NH_SIPMessageDidFail(self, notification):
        return


class RequestList(list):
    def __getitem__(self, key):
        if isinstance(key, int):
            return super(RequestList, self).__getitem__(key)
        elif isinstance(key, tuple):
            account, item_type = key
            return [item for item in self if item.account is account and isinstance(item, item_type)]
        else:
            return [item for item in self if item.account is key]


def contact_instance_id(contact, contact_uri):
    """The bare instance id of a Bonjour neighbour, or of the placeholder standing in for one; else None."""
    if contact.type == 'bonjour':
        return bare_instance_id(contact.settings.id) or None
    return placeholder_instance_id(contact_uri.uri)


class JournalInterrupted(Exception):
    """Blink is quitting: the journal page being applied is kept, to be applied again at the next start."""


@implementer(IObserver)
class MessageManager(object, metaclass=Singleton):
    __ignored_content_types__ = {IsComposingDocument.content_type, IMDNDocument.content_type,
                                 'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-message-remove', 'application/sylk-api'}

    # what the journal ignores and nothing below handles live
    __not_history_content_types__ = {'application/sylk-addressbook-update', 'application/sylk-data-export', 'application/sylk-contact-update',
                                     'application/sylk-storage-changed'}

    own_message_ids_size = 1000
    seen_message_ids_size = 10000

    def __init__(self):
        self.sessions = []
        self._own_message_ids = OrderedDict()  # ids of messages sent by this device, to recognise their replicated copies
        self._own_upload_ids = OrderedDict()   # transfer ids of files this device uploads: the server sends their message, back to us too
        self.journal_receipts = {}      # {account id: {message id: state}}, collected in a first sync
        self.quitting = False           # set at once on SIPApplicationWillEnd: the journal import stops (JournalInterrupted)
        self._bulk_key_lookups = set()  # addresses asked for in bulk after a first sync: their keys are counted, not listed
        self._bulk_keys_saved = 0
        self.seen_message_ids = SeenMessageIds(self.seen_message_ids_size)  # handled live or from the journal, whichever came first
        self._own_conversation_reads = OwnMarkers(ttl=30)  # read markers this device sent, to recognise their echo
        self._own_conversation_removes = OwnMarkers(ttl=60)  # conversation removals this device asked the server for
        self._outgoing_message_queue = deque()
        self._incoming_encrypted_message_queue = deque()
        self._snapshot_backlogs = set()  # account ids whose older first sync pages are being applied
        self._snapshot_points = {}      # account id: read points, while first sync entries are stored (_journal_store)
        self._journal_decrypts = set()  # ids of first sync entries decrypted for an open conversation: stored, never handled as live
        self._sync_queue = deque()
        self._token_requested = {}      # account id -> monotonic time of the last API token request
        self._token_not_asked = set()   # account ids whose domain does not run SylkServer (asked if it turns out to)
        self._token_retry_pending = set()
        self._syncing = set()           # account ids with a history download in progress
        self.pgp_requests = RequestList()

        self._removing_conversations = {}  # conversation key -> session, while the user removes it
        self._announce_after_sync = set()  # accounts added anew whose journal cursor was restored: announced when the sync ends

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPEngineGotMessage')
        notification_center.add_observer(self, name='SylkServerConfigurationDidChange')
        notification_center.add_observer(self, name='BlinkSessionWasCreated')
        notification_center.add_observer(self, name='BlinkSessionWasLoaded')
        notification_center.add_observer(self, name='BlinkSessionWasDeleted')
        notification_center.add_observer(self, name='PGPKeysDidGenerate')
        notification_center.add_observer(self, name='PGPMessageDidNotDecrypt')
        notification_center.add_observer(self, name='PGPMessageDidDecrypt')
        notification_center.add_observer(self, name='PGPKeysShouldReload')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='BlinkServerHistoryWasFetched')
        notification_center.add_observer(self, name='BlinkMessageHistoryFailedLocalFound')
        notification_center.add_observer(self, name='BlinkMessageHistoryConversationDidRemove')
        notification_center.add_observer(self, name='BlinkJournalFirstSyncDidFinish')
        notification_center.add_observer(self, name='BlinkConfirmReadMessagesOnOtherDevice')
        notification_center.add_observer(self, name='BlinkJournalProgress')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        KeyEscrowManager().start()

    @run_in_thread('file-io')
    def _save_pgp_key(self, data, uri):
        log.info(f'Saving public key for {str(uri)[4:]}')
        settings = SIPSimpleSettings()
        account_manager = AccountManager()

        id = str(uri).replace('/', '_').replace('sip:', '')
        try:
            account = account_manager.get_account(id)
        except KeyError:
            pass
        else:
            # don't process my own public key: the answer to the start's lookup (KeyEscrowManager)
            call_in_gui_thread(KeyEscrowManager().server_public_key_received, account, data)
            return

        directory = settings.chat.keys_directory.normalized
        filename = os.path.join(directory, id + '.pubkey')
        makedirs(directory)
        if self._quiet_public_key(None, id):
            self._bulk_keys_saved += 1
            log.debug(f'Saved the PGP public key of {id}')
        else:
            ActivityLog().info(f'[pgp] Saved the PGP public key of {id}')

        with open(filename, 'wb') as f:
            data = data if isinstance(data, bytes) else data.encode()
            f.write(data)
            try:
                from blink.contacts import URIUtils
                contact, contact_uri = URIUtils.find_contact(uri)
                blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)
            except StopIteration:
                pass
            else:
                notification_center = NotificationCenter()
                notification_center.post_notification('PGPKeysShouldReload', sender=blink_session)

    def check_encryption(self, content_type, body):
        if (content_type.lower().startswith('text/') and '-----BEGIN PGP MESSAGE-----' in body and body.strip().endswith('-----END PGP MESSAGE-----') and content_type != 'text/pgp-private-key'):
            return 'OpenPGP'
        else:
            return None

    def _compare_public_key(self, account, public_key):
        settings = SIPSimpleSettings()
        id = account.id.replace('/', '_')
        extension = 'pubkey'

        directory = os.path.join(settings.chat.keys_directory.normalized, 'private')

        filename = os.path.join(directory, f'{id}.{extension}')
        if os.path.exists(filename):
            try:
                key1, _ = pgpy.PGPKey.from_file(filename)
                key2, _ = pgpy.PGPKey.from_blob(public_key)
            except Exception as e:
                log.warning(f"Can't load PGP key for comparison: {str(e)}")
                pass
            else:
                fingerprint1 = key1.fingerprint
                fingerprint2 = key2.fingerprint

                if fingerprint1 == fingerprint2:
                    log.info(f'Private key import for {account.id} skipped because are the same')
                    return True
                else:
                    log.info('Show PGP import panel')

        return False

    def _handle_incoming_message(self, message, session, account=None):
        notification_center = NotificationCenter()
        if account is session.account:
            notification_center.post_notification('BlinkMessageIsParsed', sender=session, data=message)
        elif account is not None:
            notification_center.post_notification('BlinkGotHistoryMessageUpdate', sender=account, data=message)

        if message is not None and message.direction != 'outgoing' and message.disposition is not None and 'positive-delivery' in message.disposition:
            log.debug("-- Should send delivered imdn for incoming message")
            self.send_imdn_message(session, message.id, message.timestamp, 'delivered')

        notification_center.post_notification('BlinkGotMessage', sender=session, data=NotificationData(message=message, account=account))

    token_request_interval = 30     # seconds between API token requests of one account
    sync_registration_delay = 10    # seconds after registration before the history is fetched

    @run_in_gui_thread
    def _request_history_synchronization_token(self, account, reason='no token'):
        """Ask the server for an API token, at most once per interval per account.

        A request inside the interval is not dropped: one retry is scheduled for
        when the interval ends, so a 401 always leads to a new token.
        """
        if account is BonjourAccount() or not account.enabled:
            return
        from blink.sylk_discovery import has_sylkserver
        if not has_sylkserver(account):
            # the token comes from SylkServer: not asked of a server that is not one (asked
            # when its configuration is found, SylkServerConfigurationDidChange)
            self._token_not_asked.add(account.id)       # silently: most domains do not run SylkServer
            return
        self._token_not_asked.discard(account.id)
        now = time.monotonic()
        elapsed = now - self._token_requested.get(account.id, -self.token_request_interval)
        if elapsed < self.token_request_interval:
            if account.id not in self._token_retry_pending:
                self._token_retry_pending.add(account.id)
                delay = self.token_request_interval - elapsed
                log.debug(f'API token for {account.id} requested {elapsed:.0f}s ago, asking again in {delay:.0f}s')
                call_later(delay, self._retry_token_request, account, reason)
            return
        self._token_requested[account.id] = now
        ActivityLog().info(f'[journal] Requesting an API token for account {account.id} ({reason})')
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(account.uri)
        outgoing_message = OutgoingMessage(account, contact, 'Token request', 'application/sylk-api-token', use_cpim=False)
        self._send_message(outgoing_message)

    def _retry_token_request(self, account, reason):
        self._token_retry_pending.discard(account.id)
        self._request_history_synchronization_token(account, reason)

    def note_own_upload(self, transfer_id):
        """A file this device is uploading (blink.messagepane.uploads): its message, which the server
        sends and replicates back, confirms the local copy already in history."""
        self._own_upload_ids[transfer_id] = None
        while len(self._own_upload_ids) > self.own_message_ids_size:
            self._own_upload_ids.popitem(last=False)

    def _send_message(self, outgoing_message):
        self._own_message_ids[outgoing_message.id] = None
        while len(self._own_message_ids) > self.own_message_ids_size:
            self._own_message_ids.popitem(last=False)
        self._outgoing_message_queue.append(outgoing_message)
        self._send_outgoing_messages()

    def _send_outgoing_messages(self):
        while self._outgoing_message_queue:
            message = self._outgoing_message_queue.popleft()
            instance_id = placeholder_instance_id(message.uri)
            if instance_id:
                # A Bonjour neighbour who is not on the network has no address: keep the
                # message unsent (failed-local) and send it when the neighbour is back.
                log.info(f'Message {message.id} to Bonjour neighbour {instance_id} kept until the neighbour is on the network')
                if message.session is not None and not message._disabled_imdn_content_type:
                    NotificationCenter().post_notification('BlinkMessageDidFail', sender=message.session, data=NotificationData(reason='Neighbour is not on the network', originator='local', id=message.id, code=None))
                continue
            message.send()

    @run_in_thread('sync')
    def _sync_messages(self, account, reason=None):
        if account.id in self._syncing:
            log.debug(f'History synchronization for {account.id} already in progress')
            return
        self._syncing.add(account.id)
        try:
            self._fetch_server_history(account, reason)
        finally:
            self._syncing.discard(account.id)

    _journal_unverified_logged = False
    journal_since_years = 5     # how far back the history of a new device goes, as sylk mobile does
    journal_recent_days = 30    # a first sync downloads and applies these last days, then it is finished; the older
                                # history is downloaded and applied in the background after it (_apply_snapshot_backlog)
    journal_max_pages = 200     # pages per run; the next run continues from the cursor

    def _journal_directory(self, account):
        path = ApplicationData.get(f'journal/{account.id}')
        makedirs(path)
        return path

    journal_cursor_file = 'cursor'      # journal/<account>/cursor: the last fetched id, kept outside the configuration

    def _save_journal_cursor(self, account, cursor):
        path = os.path.join(self._journal_directory(account), self.journal_cursor_file)
        try:
            with open(path + '.tmp', 'w') as file:
                file.write(str(cursor))
            os.replace(path + '.tmp', path)
        except OSError as e:
            log.warning(f'Cannot write the journal cursor of {account.id}: {e}')

    def _restore_journal_cursor(self, account):
        """The cursor lives in the account's settings, in the configuration file; a configuration
        lost or made again (an account added anew, a profile deleted) would mean downloading the
        whole journal into a database that has it. Its copy in journal/<account>/cursor brings it back."""
        path = os.path.join(self._journal_directory(account), self.journal_cursor_file)
        if account.sms.history_synchronization_id:
            if not os.path.exists(path):
                self._save_journal_cursor(account, account.sms.history_synchronization_id)     # the copy, for accounts synced before it existed
            return
        try:
            with open(path) as file:
                cursor = file.read().strip()
        except OSError:
            return
        if cursor:
            account.sms.history_synchronization_id = cursor
            account.sms.history_synchronization_id_timestamp = None     # not in the copy
            account.save()
            ActivityLog().info(f'[journal] Journal cursor of {account.id} restored from {path}: {cursor}')
            # the account was added anew on this device (its configuration is new): no first sync
            # will follow, but it is a sign-in all the same, announced when this sync ends
            if not account.sms.activation_announced:
                self._announce_after_sync.add(account.id)

    journal_first_sync_marker = FIRST_SYNC_MARKER
    journal_snapshot_directory = 'snapshot'     # journal/<account>/snapshot/: the pages of a first sync
    journal_snapshot_complete = 'complete'      # journal/<account>/snapshot/complete: the recent days are downloaded
    journal_snapshot_backfill = 'backfill'      # journal/<account>/snapshot/backfill: {"until", "cursor"} of the older history (JSON)

    # A first sync is a snapshot of the journal: every message has its final state in it and
    # removed messages are not in it, so its entries are only inserts, applied in any order.
    # The last journal_recent_days days are downloaded and applied first (newest first), then
    # the first sync is finished; the older history is downloaded and applied in the background
    # after it, page by page, up to where the recent days started. Pages of later syncs (events
    # after the first sync started) are kept apart in journal/<account>/ and applied in order.

    def _journal_snapshot_cache(self, account):
        path = os.path.join(self._journal_directory(account), self.journal_snapshot_directory)
        makedirs(path)
        return JournalCache(path)

    def _journal_snapshot_downloaded(self, account):
        return os.path.exists(os.path.join(self._journal_directory(account), self.journal_snapshot_directory, self.journal_snapshot_complete))

    def _journal_backfill_state(self, account):
        """{'until': ISO time, 'cursor': last id applied}: the older history of a first sync still to
        download and apply, None when there is none left."""
        path = os.path.join(self._journal_directory(account), self.journal_snapshot_directory, self.journal_snapshot_backfill)
        try:
            with open(path) as file:
                state = json.load(file)
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) and state.get('until') else None

    def _save_journal_backfill_state(self, account, state):
        path = os.path.join(self._journal_snapshot_cache(account).directory, self.journal_snapshot_backfill)
        try:
            with open(path + '.tmp', 'w') as file:
                json.dump(state, file)
            os.replace(path + '.tmp', path)
        except OSError as e:
            ActivityLog().warning(f'[journal] Cannot write {path}: {e}')

    def _journal_snapshot_pending(self, account):
        """Whether the first sync still has history to bring in (its older history, in the background)."""
        return self._journal_backfill_state(account) is not None or self._journal_snapshot_downloaded(account)

    # The messages of a snapshot carry their delivery state, not whether the user read them: an
    # incoming message is 'received' for ever. That is in the read markers (conversation read up
    # to a time, on another device) and in the user's own answers (who answers has read). A
    # conversation's read point is the latest of them; incoming messages up to it are read.

    journal_snapshot_reads = 'read-points'      # journal/<account>/snapshot/read-points: {conversation: read point} (JSON, not a page)

    @staticmethod
    def _read_point_key(address):
        address = str(address or '').strip().lower()
        for scheme in ('sips:', 'sip:'):
            address = address.removeprefix(scheme)
        return address.split(';', 1)[0] or None

    def _snapshot_read_points(self, account, cache):
        """The read points of a first sync: made once from all its pages (they are all downloaded
        before any is applied), then kept in read-points, moved on by reads after it started."""
        path = os.path.join(cache.directory, self.journal_snapshot_reads)
        try:
            with open(path) as file:
                points = json.load(file)
            if isinstance(points, dict):
                return points
        except (OSError, ValueError):
            pass
        points = {}
        for name in cache.pages():
            try:
                self._merge_read_points(points, cache.load(name).get('messages') or [])
            except (OSError, ValueError, AttributeError):
                continue
        self._save_snapshot_read_points(cache, points)
        JournalLog()(account.id, 'read points made', conversations=len(points))
        return points

    def _merge_read_points(self, points, entries):
        """Move the read points on with the read markers and own answers among these entries."""
        for entry in entries:
            action = journal_action(entry.get('content_type'))
            if action == 'conversation_read':
                address = conversation_read_marker(entry.get('content'))[0] or entry.get('contact')
            elif entry.get('direction') == 'outgoing' and action in ('text', 'file_transfer'):
                address = entry.get('contact')
            else:
                continue
            key = self._read_point_key(address)
            if not key or not entry.get('timestamp'):
                continue
            try:
                when = ISOTimestamp(entry['timestamp']).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if key not in points or when > ISOTimestamp(points[key]):
                points[key] = when.isoformat()
        return points

    def _save_snapshot_read_points(self, cache, points):
        path = os.path.join(cache.directory, self.journal_snapshot_reads)
        try:
            with open(path + '.tmp', 'w') as file:
                json.dump(points, file)
            os.replace(path + '.tmp', path)
        except OSError as e:
            ActivityLog().warning(f'[journal] Cannot write {path}: {e}')

    def _NH_BlinkConfirmReadMessagesOnOtherDevice(self, notification):
        # read on another device while older pages of a first sync are still to come: they are
        # applied with it, else they would arrive unread
        data = notification.data
        key = self._read_point_key(getattr(data, 'remote_uri', None))
        if not key:
            return
        try:
            when = ISOTimestamp(data.timestamp).replace(tzinfo=timezone.utc) if getattr(data, 'timestamp', None) else datetime.now(timezone.utc)
        except Exception:
            return
        for account in AccountManager().iter_accounts():
            if account is BonjourAccount() or not account.enabled or not self._journal_snapshot_pending(account):
                continue
            cache = self._journal_snapshot_cache(account)
            points = self._snapshot_read_points(account, cache)
            if key not in points or when > ISOTimestamp(points[key]):
                points[key] = when.isoformat()
                self._save_snapshot_read_points(cache, points)

    journal_progress_interval = 0.25        # seconds between progress updates while a page is read

    def _journal_read_page(self, account, response, before, expected):
        """The body of a journal page, read as it arrives, the progress bar counting the entries
        in it so far: every entry has a "message_id" key, which in a JSON string payload is escaped."""
        chunks = []
        found = 0
        tail = b''
        marker = b'"message_id"'
        updated = time.monotonic()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if self.quitting:
                response.close()
                raise JournalInterrupted('quitting while a page was downloading')     # the page is fetched again, whole
            chunks.append(chunk)
            window = tail + chunk
            found += window.count(marker)
            tail = window[-(len(marker) - 1):]      # a key split between two chunks is counted once
            now = time.monotonic()
            if now - updated >= self.journal_progress_interval:
                updated = now
                done = before + found
                self._journal_progress(account, 'download', done, max(expected, done) if expected else None)
        return b''.join(chunks)

    def _journal_first_sync_marker(self, account):
        """journal/<account>/first-sync.marker: there while a first sync is not finished.

        Written when a first sync starts and removed by the history once the read
        state is settled (MessageHistory.settle_first_sync_read), so a first sync
        interrupted by a quit is resumed as one: its later pages have a cursor and
        would otherwise be applied as a catch-up, and the settling would never run.
        """
        return os.path.join(self._journal_directory(account), self.journal_first_sync_marker)

    def _journal_page_url(self, account):
        """The next journal page: after the cursor, or for a first sync `since` its last
        journal_recent_days days (the older history follows in the background, _run_snapshot_backlog).

        Without `since` and without a cursor the server answers with the last
        three days only, and the older entries are never asked for.
        """
        cursor = account.sms.history_synchronization_id
        if cursor:
            return self._journal_url(account, cursor=cursor)
        backfill = self._journal_backfill_state(account)
        since = ISOTimestamp(backfill['until']) if backfill is not None else datetime.now(timezone.utc) - timedelta(days=self.journal_recent_days)
        return self._journal_url(account, since=since)

    @staticmethod
    def _journal_url(account, cursor=None, since=None):
        """The journal: the entries after an id, or since a time."""
        base = str(account.sms.history_synchronization_url)
        if cursor:
            url = urllib.parse.urljoin(f'{base}/', cursor)
            query = ''
        else:
            since = since.astimezone(timezone.utc)
            url = base
            query = 'since=' + since.strftime('%Y-%m-%dT%H:%M:%S.') + f'{since.microsecond // 1000:03d}Z'
        scheme, netloc, path, _, fragment = urlsplit(url)
        return urlunsplit((scheme, netloc, quote(path), query, fragment))

    @staticmethod
    def _journal_file_name(messages):
        # sorts chronologically: the page's last timestamp, then its last id
        last = messages[-1]
        stamp = re.sub(r'[^0-9A-Za-z]', '-', str(last.get('timestamp') or '')) or 'unknown'
        return f"{stamp}-{last.get('message_id') or 'page'}.json"

    def _fetch_server_history(self, account, reason=None):
        """Download the journal page by page to journal/<account>/, then apply the cached pages.

        The download only writes files, so it runs at network speed. The cursor
        moves only once a page is safely on disk, so an interruption costs at most
        the page in flight; a page is deleted only once it has been applied.
        """
        if not account.sms.history_synchronization_token:
            JournalLog()(account.id, 'sync skipped', reason='no token', requested='yes')
            self._request_history_synchronization_token(account, 'no token')
            return

        if not account.sms.history_synchronization_url:
            return

        self._restore_journal_cursor(account)
        directory = self._journal_directory(account)
        headers = {'Authorization': f'Apikey {account.sms.history_synchronization_token}'}
        settings = SIPSimpleSettings()
        activity = ActivityLog()
        marker = self._journal_first_sync_marker(account)
        if self._journal_snapshot_downloaded(account) and os.path.exists(marker):
            # the recent days of a first sync were downloaded before a quit: applying them finishes it
            self._apply_snapshot_recent(account)
            if os.path.exists(marker):
                return      # stopped by quitting again
        elif self._journal_backfill_state(account) is not None and not os.path.exists(marker):
            self._apply_snapshot_backlog(account)       # the older history goes on in the background
        resumed = os.path.exists(marker)
        first_sync = resumed or not account.sms.history_synchronization_id
        if first_sync and not resumed:
            started_at = ISOTimestamp.now()
            try:
                with open(marker, 'w') as marker_file:
                    marker_file.write(f'{started_at}\n')
            except OSError as e:
                activity.warning(f'[journal] Cannot write {marker}: {e}')
            from blink.history import FIRST_SYNC_DATE
            date_path = os.path.join(directory, FIRST_SYNC_DATE)
            if not os.path.exists(date_path):
                # kept for ever: no receipt is ever sent for a message older than it (send_imdn_message)
                try:
                    with open(date_path, 'w') as date_file:
                        date_file.write(f'{started_at}\n')
                except OSError as e:
                    activity.warning(f'[journal] Cannot write {date_path}: {e}')
            # the first sync asks for its last days; what is older is fetched after it, up to there
            self._save_journal_backfill_state(account, {'until': (started_at.astimezone(timezone.utc) - timedelta(days=self.journal_recent_days)).isoformat(),
                                                        'cursor': ''})
        # the pages of a first sync are a snapshot, kept apart (applied newest first, _apply_snapshot_recent)
        page_directory = self._journal_snapshot_cache(account).directory if first_sync else directory
        jlog = JournalLog()
        jlog(account.id, 'sync start', reason=reason, first_sync=first_sync, resumed=resumed or None, cursor=account.sms.history_synchronization_id,
             since=f'{self.journal_recent_days} days' if first_sync else None, server=account.sms.history_synchronization_url)
        activity.info(f'[journal] Fetching the message journal of {account.id}' + (f' ({reason})' if reason else '')
                      + (f' after {account.sms.history_synchronization_id}' if account.sms.history_synchronization_id else f' of the last {self.journal_recent_days} days')
                      + (' (resuming the first sync)' if resumed else ''))
        if not settings.tls.verify_server and not self._journal_unverified_logged:
            # the user's choice (tls.verify_server); said once here instead of a urllib3 warning per request
            self._journal_unverified_logged = True
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            activity.warning('[journal] The certificate of the journal server is not verified (TLS verify server is off)')
        started = time.monotonic()
        pages = entries = transferred = 0
        expected = None     # entries the server has after the cursor (X-Sylk-Journal-Remaining of the first page)
        # pages downloaded by an earlier run that was stopped (quit) and not imported yet: the
        # progress counts them, so a resumed download goes on from where it was, not from 0
        cache = JournalCache(page_directory)
        cached = 0
        for name in cache.pages():
            try:
                cached += len(cache.load(name).get('messages') or [])
            except (OSError, ValueError, AttributeError):
                pass
        self._journal_progress(account, 'download', cached, None)
        complete = False
        stopped = None
        stats = JournalStats(account.id, first_sync=first_sync, cursor=account.sms.history_synchronization_id, reason=reason)

        # a first sync downloads all its recent days before applying them
        while first_sync or pages < self.journal_max_pages:
            if self.quitting:
                stopped = 'quitting'
                break
            cursor = account.sms.history_synchronization_id
            url = self._journal_page_url(account)
            page_started = time.monotonic()
            log.info(f'Fetching message history for {account.id} from server {url}')
            try:
                r = requests.get(url, headers=headers, timeout=20, verify=settings.tls.verify_server, stream=True)
                r.raise_for_status()
                if expected is None:
                    try:
                        expected = int(r.headers.get('X-Sylk-Journal-Remaining'))
                    except (TypeError, ValueError):
                        expected = 0        # an older server: the total is not known
                body = self._journal_read_page(account, r, cached + entries, cached + expected if expected else 0)
                data = json.loads(body)
            except JournalInterrupted:
                stopped = 'quitting'
                break
            except requests.HTTPError as e:
                stopped = f'HTTP {e.response.status_code}'
                if e.response.status_code == 401:
                    activity.info(f'[journal] The API token of {account.id} was refused (401)')
                    self._request_history_synchronization_token(account, 'token refused')
                else:
                    activity.warning(f'[journal] The message journal of {account.id} answered {e.response.status_code}, stopped after {pages} pages')
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                stopped = f'unreachable: {e.__class__.__name__}'
                activity.warning(f'[journal] Cannot reach the message journal of {account.id}, stopped after {pages} pages: {e}')
                break
            except (requests.RequestException, ValueError) as e:
                stopped = f'bad answer: {e.__class__.__name__}'
                activity.warning(f'[journal] Bad answer from the message journal of {account.id}, stopped after {pages} pages: {e}')
                break

            messages = data.get('messages') if isinstance(data, dict) else None
            if not messages:
                complete = True
                break

            name = self._journal_file_name(messages)
            path = os.path.join(page_directory, name)
            try:
                with open(path + '.part', 'w', encoding='utf-8') as page_file:
                    # the cursor as it stood before this page: the apply stage tells a
                    # first-ever backfill from a catch-up by it
                    json.dump({'cursor': cursor or '', 'messages': messages}, page_file)
                os.replace(path + '.part', path)
            except OSError as e:
                stopped = f'cannot save: {e.strerror or e}'
                activity.error(f'[journal] Cannot save journal page {path}: {e}')
                break

            pages += 1
            entries += len(messages)
            transferred += len(body)
            last_id = messages[-1].get('message_id')
            stats.page_downloaded(name, len(messages), len(body), time.monotonic() - page_started, last_id)
            self._journal_progress(account, 'download', cached + entries, cached + max(expected or 0, entries) if expected else None)
            log.info(f'Cached journal page {name} ({len(messages)} entries, {len(body)} bytes, cursor {last_id})')
            jlog(account.id, 'download page', page=pages, file=name, entries=len(messages), bytes=len(body),
                 took=f'{time.monotonic() - page_started:.1f}s', remaining=(expected - entries + len(messages)) if expected else None,
                 total=f'{entries}/{expected}' if expected else entries, cursor=last_id)

            if not last_id:
                break
            account.sms.history_synchronization_id = last_id
            account.sms.history_synchronization_id_timestamp = messages[-1].get('timestamp') or None
            account.save()
            self._save_journal_cursor(account, last_id)
        else:
            stopped = f'max {self.journal_max_pages} pages'
            activity.warning(f'[journal] Stopped the download of {account.id} at {self.journal_max_pages} pages, the rest follows on the next sync')
        jlog(account.id, 'download stopped' if stopped else 'download end', reason=stopped, pages=pages, entries=entries, bytes=transferred,
             took=f'{time.monotonic() - started:.1f}s', complete=complete if not stopped else None)

        if stopped == 'quitting':
            # the pages downloaded so far stay cached and the cursor after the last of them: the next
            # start downloads the rest first, then imports all of them (nothing is imported now)
            activity.info(f'[journal] Download of {account.id} stopped by quitting after {pages} pages ({entries} entries), resumed at the next start')
            self._journal_progress(account, 'done')
            return
        if pages:
            activity.info(f'[journal] Downloaded {entries} journal entries of {account.id} in {pages} pages ({transferred} bytes, {time.monotonic() - started:.1f}s)')
        elif complete:
            activity.info(f'[journal] The message journal of {account.id} has no new entries')
        if first_sync:
            if not complete:
                # its newest pages are not here yet: nothing is applied, the next sync downloads the rest
                activity.info(f'[journal] First sync of {account.id} not complete ({stopped}), the download goes on at the next sync')
                self._journal_progress(account, 'done')
                return
            try:
                with open(os.path.join(page_directory, self.journal_snapshot_complete), 'w') as complete_file:
                    complete_file.write(f'{ISOTimestamp.now()} {cached + entries} entries\n')
            except OSError as e:
                activity.error(f'[journal] Cannot mark the first sync of {account.id} as downloaded: {e}')
                self._journal_progress(account, 'done')
                return
            self._apply_snapshot_recent(account, stats)
            return
        self._apply_cached_journal(account, stats)

    journal_progress_every = 250    # entries between progress lines, and between short pauses
    # events of a snapshot have nothing to act on; public keys are asked from the server when the
    # whole snapshot is in (_fetch_public_keys_once), not taken from old journal entries
    journal_snapshot_skipped = frozenset(('receipt', 'conversation_remove', 'message_remove', 'messages_bulk_delete', 'conversation_read', 'public_key'))
    journal_throttle = 0.05         # seconds to pause, so the GUI and the db thread keep up

    def _apply_snapshot_page(self, account, cache, name, contacts, progress=None, **pace):
        """Apply one page of a first sync, newest entry first (they are only inserts). Returns
        the timestamp of its oldest entry, or None when it failed (kept for a later attempt, or
        quarantined). JournalInterrupted when quitting: the page is kept."""
        jlog = JournalLog()
        try:
            page = cache.load(name)
            entries = list(reversed(page.get('messages') or []))
            stats = self._apply_snapshot_entries(account, entries, contacts, progress, **pace)
        except JournalInterrupted:
            raise
        except Exception as e:
            attempts, quarantined = cache.failed(name)
            jlog(account.id, 'snapshot page quarantined' if quarantined else 'snapshot page failed', file=name, attempts=attempts, error=str(e)[:200])
            ActivityLog().error(f'[journal] Applying first sync page {name} of {account.id} failed (attempt {attempts})'
                                + (', moved to quarantine' if quarantined else '') + f': {e}')
            return None
        cache.applied(name)
        jlog(account.id, 'snapshot page applied', file=name, entries=len(entries),
             **{re.sub(r'[^0-9a-z]+', '_', outcome.lower()).strip('_'): count for outcome, count in sorted(stats.items())})
        stamps = [entry.get('timestamp') for entry in entries if entry.get('timestamp')]
        return min(stamps) if stamps else ''

    def _apply_snapshot_entries(self, account, entries, contacts, progress=None, **pace):
        # an incoming message up to its conversation's read point is stored read, so no badge
        # counts it while the rest of the page is stored (_journal_store)
        self._snapshot_points[account.id] = self._snapshot_read_points(account, self._journal_snapshot_cache(account))
        try:
            return self._apply_server_history_messages(account, entries, first_sync=True, contacts=contacts, progress=progress, mark_synced=False, **pace)
        finally:
            self._snapshot_points.pop(account.id, None)

    def _apply_snapshot_recent(self, account, stats=None):
        """The first sync, in the sync thread: its recent days, newest first, then the first sync
        is finished (read state, badges, the Messages group) and the older history is fetched
        and applied in the background."""
        from blink.history import MessageHistory
        cache = self._journal_snapshot_cache(account)
        names = list(reversed(cache.pages()))
        marker = self._journal_first_sync_marker(account)
        activity = ActivityLog()
        activity.info(f'[journal] Applying the last {self.journal_recent_days} days of {account.id} newest first: {len(names)} pages')
        JournalLog()(account.id, 'snapshot recent start', pages=len(names))
        state = self._journal_backfill_state(account)
        if state is not None and 'recent' not in state:
            # the older history's total is what the journal has from five years ago, less these
            recent = 0
            for name in names:
                try:
                    recent += len(cache.load(name).get('messages') or [])
                except (OSError, ValueError, AttributeError):
                    pass
            state['recent'] = recent
            self._save_journal_backfill_state(account, state)
        read_points = self._snapshot_read_points(account, cache)
        contacts = Counter()
        started = time.monotonic()
        applied = 0
        for name in names:
            if self.quitting:
                self._journal_progress(account, 'done')
                return
            try:
                oldest = self._apply_snapshot_page(account, cache, name, contacts,
                                                   progress=lambda index, total, db_wait=0.0: self._journal_progress(account, 'apply', index, total))
            except JournalInterrupted:
                self._journal_progress(account, 'done')
                return
            if oldest is None:
                continue
            applied += 1
        MessageHistory().apply_read_points(account.id, read_points, page='recent days')
        MessageHistory().flush_batched_stores()
        MessageHistory().wait_for_writes()
        self._journal_progress(account, 'done')
        left = len(cache.pages())
        activity.info(f'[journal] First sync of {account.id}: the last {self.journal_recent_days} days applied ({applied} pages, '
                      f'{time.monotonic() - started:.1f}s), the older history follows in the background')
        JournalLog()(account.id, 'snapshot recent end', applied=applied, failed=left or None, took=f'{time.monotonic() - started:.1f}s')
        # the first sync is finished: what follows is counted and filed as usual; a page that failed
        # is left for the background, which applies what is cached before it downloads
        unlink(os.path.join(cache.directory, self.journal_snapshot_complete))
        unlink(marker)
        NotificationCenter().post_notification('BlinkJournalDidApply', sender=account, data=NotificationData(new_messages=dict(contacts), stats_path=None,
                                                                                                  first_sync=True, first_sync_marker=marker))
        self._apply_snapshot_backlog(account)

    journal_backlog_pause = 1.0     # seconds between two older pages of a first sync, so live work goes first
    # The speed of the older history import, chosen by the user (the button by the progress bar):
    # entries per batch, and the pause after a batch as a multiple of the time it took
    journal_import_speeds = {
        'fast':   (250, 0.0),       # the pace of a normal sync
        'normal': (50, 2.0),        # a third of the time
        'gentle': (20, 6.0),        # a seventh of the time
    }
    journal_import_speed_names = (('fast', 'Fast'), ('normal', 'Normal'), ('gentle', 'Gentle'), ('paused', 'Pause'))     # as in the menu
    journal_import_speed_descriptions = {
        'fast': 'As fast as it can, finishes soonest',
        'normal': 'Works a third of the time',
        'gentle': 'Hardly noticeable, takes longest',
        'paused': 'Stopped until resumed',
    }
    journal_import_speed_file = 'journal_import_speed'     # <data>/journal_import_speed: '<speed> <speed before a pause>'

    def _backlog_pace(self):
        return self.journal_import_speeds.get(self.journal_import_speed, self.journal_import_speeds['normal'])

    @property
    def journal_import_speed(self):
        try:
            return self._journal_import_speed
        except AttributeError:
            self._journal_import_speed, self._journal_import_resume = 'normal', 'normal'
            try:
                with open(ApplicationData.get(self.journal_import_speed_file)) as file:
                    words = file.read().split()
            except OSError:
                words = []
            if words and (words[0] in self.journal_import_speeds or words[0] == 'paused'):
                self._journal_import_speed = words[0]
            if len(words) > 1 and words[1] in self.journal_import_speeds:
                self._journal_import_resume = words[1]
            elif self._journal_import_speed in self.journal_import_speeds:
                self._journal_import_resume = self._journal_import_speed
            return self._journal_import_speed

    def set_journal_import_speed(self, speed):
        """The speed of the older history import; 'paused' stops it between batches until resumed."""
        current = self.journal_import_speed
        if speed == current or not (speed in self.journal_import_speeds or speed == 'paused'):
            return
        self._journal_import_speed = speed
        if speed in self.journal_import_speeds:
            self._journal_import_resume = speed
        try:
            with open(ApplicationData.get(self.journal_import_speed_file), 'w') as file:
                file.write(f'{speed} {self._journal_import_resume}\n')
        except OSError as e:
            ActivityLog().warning(f'[journal] Cannot keep the import speed: {e}')
        ActivityLog().info('[journal] Older history import paused' if speed == 'paused' else f'[journal] Older history import speed: {dict(self.journal_import_speed_names)[speed]}')
        NotificationCenter().post_notification('BlinkJournalImportSpeedDidChange', sender=self, data=NotificationData(speed=speed))

    def resume_journal_import(self):
        self.journal_import_speed
        self.set_journal_import_speed(self._journal_import_resume)

    @run_in_thread('journal-backlog')
    def _apply_snapshot_backlog(self, account):
        """The older history of a first sync, downloaded and applied page by page in the background.
        Live messages and later syncs go on meanwhile. When all is in: the contacts it mentions are
        filed, the badges counted, and the public keys of the contacts are asked from the server."""
        if account.id in self._snapshot_backlogs or not self._journal_snapshot_pending(account):
            return      # running, or finished by a call queued before this one
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)     # Linux: this thread only, the GUI keeps its priority
        except (AttributeError, OSError):
            pass
        self._snapshot_backlogs.add(account.id)
        try:
            self._run_snapshot_backlog(account)
        finally:
            self._snapshot_backlogs.discard(account.id)

    journal_snapshot_older = 'older'        # journal/<account>/snapshot/older/: the pages of the older history

    def _download_snapshot_older(self, account, state, cache, progress):
        """Download the older history of a first sync, from five years ago up to where its recent
        days start, into snapshot/older/. Returns whether all of it is here."""
        activity = ActivityLog()
        jlog = JournalLog()
        until = ISOTimestamp(state['until']).astimezone(timezone.utc)
        headers = {'Authorization': f'Apikey {account.sms.history_synchronization_token}'}
        verify = SIPSimpleSettings().tls.verify_server
        fetched = state.get('fetched') or 0
        activity.info(f'[journal] Downloading the history of {account.id} older than {until} in the background' + (' (resumed)' if state.get('cursor') else ''))
        jlog(account.id, 'backfill start', until=state['until'], cursor=state.get('cursor') or None)
        while True:
            if self.quitting or not account.enabled:
                return False
            if state.get('cursor'):
                url = self._journal_url(account, cursor=state['cursor'])
            else:
                url = self._journal_url(account, since=datetime.now(timezone.utc) - timedelta(days=365 * self.journal_since_years))
            try:
                response = requests.get(url, headers=headers, timeout=20, verify=verify)
                response.raise_for_status()
                messages = response.json().get('messages') or []
                if not state.get('total') and not state.get('cursor'):
                    # from five years ago: the whole journal, less the recent days applied already
                    try:
                        remaining = int(response.headers.get('X-Sylk-Journal-Remaining'))
                    except (TypeError, ValueError):
                        remaining = 0       # an older server: the count alone is shown
                    if remaining:
                        state['total'] = max(remaining - (state.get('recent') or 0), len(messages))
            except (requests.RequestException, ValueError, AttributeError) as e:
                activity.warning(f'[journal] Downloading the older history of {account.id} stopped, it goes on after the next sync: {e}')
                jlog(account.id, 'backfill stopped', error=str(e)[:200])
                return False
            entries = []
            finished = not messages
            for entry in messages:
                try:
                    when = ISOTimestamp(entry.get('timestamp')).replace(tzinfo=timezone.utc)
                except Exception:
                    when = None
                if when is not None and when >= until:
                    finished = True     # the recent days, applied already
                    break
                entries.append(entry)
            if entries:
                name = self._journal_file_name(entries)
                path = cache.path(name)
                try:
                    with open(path + '.part', 'w', encoding='utf-8') as page_file:
                        json.dump({'cursor': state.get('cursor') or '', 'messages': entries}, page_file)
                    os.replace(path + '.part', path)
                except OSError as e:
                    activity.error(f'[journal] Cannot save older history page {path}: {e}')
                    return False
            fetched += len(entries)
            if messages and messages[-1].get('message_id'):
                state['cursor'] = messages[-1]['message_id']
            state['fetched'] = fetched
            state['downloaded'] = finished
            self._save_journal_backfill_state(account, state)
            jlog(account.id, 'backfill download page', entries=len(entries), total=fetched, cursor=state.get('cursor') or None)
            progress('download', fetched)
            if finished:
                activity.info(f'[journal] Downloaded {fetched} older journal entries of {account.id}')
                return True

    def _run_snapshot_backlog(self, account):
        from blink.history import MessageHistory
        notification_center = NotificationCenter()
        activity = ActivityLog()
        jlog = JournalLog()
        cache = self._journal_snapshot_cache(account)
        points = self._snapshot_read_points(account, cache)
        contacts = Counter()
        state = self._journal_backfill_state(account)
        done = (state or {}).get('done') or 0      # resumed: the bar goes on from where it was
        started = time.monotonic()

        def progress(phase, count=None):
            total = (state or {}).get('total') or None
            notification_center.post_notification('BlinkJournalBacklogProgress', sender=account,
                                                  data=NotificationData(phase=phase, done=count, total=max(total, count or 0) if total else None))

        # recent pages that failed to apply are retried first
        for name in list(reversed(cache.pages())):
            if self.quitting:
                progress('done')
                return
            try:
                self._apply_snapshot_page(account, cache, name, contacts, pace=self._backlog_pace)
            except JournalInterrupted:
                progress('done')
                return
        if cache.pages():
            progress('done')
            return      # still failing: tried again after the next sync

        if state is not None:
            older = JournalCache(os.path.join(cache.directory, self.journal_snapshot_older))
            makedirs(older.directory)
            if not state.get('downloaded'):
                # all of it first: the read points need every page, so each message is stored with
                # its final read state at once, not fixed by a later page (a badge in between)
                if not self._download_snapshot_older(account, state, older, progress):
                    progress('done')
                    return      # goes on after the next sync
            if not state.get('points'):
                for name in older.pages():
                    try:
                        self._merge_read_points(points, older.load(name).get('messages') or [])
                    except (OSError, ValueError, AttributeError):
                        continue
                self._save_snapshot_read_points(cache, points)
                state['points'] = True
                self._save_journal_backfill_state(account, state)
                jlog(account.id, 'read points made', conversations=len(points), older=True)
            names = list(reversed(older.pages()))
            activity.info(f'[journal] Applying {len(names)} pages of the older history of {account.id} newest first in the background')
            for name in names:
                if self.quitting or not account.enabled:
                    progress('done')
                    return      # goes on after the next start
                try:
                    size = len(older.load(name).get('messages') or [])
                except (OSError, ValueError, AttributeError):
                    size = 0
                before = done
                try:
                    self._apply_snapshot_page(account, older, name, contacts,
                                              progress=lambda index, total, db_wait=0.0: progress('apply', before + index),
                                              pace=self._backlog_pace)
                except JournalInterrupted:
                    progress('done')
                    return
                done += size
                MessageHistory().flush_batched_stores()
                state['done'] = done
                self._save_journal_backfill_state(account, state)
                progress('apply', done)
                time.sleep(self.journal_backlog_pause)
            MessageHistory().apply_read_points(account.id, points, page='older history')     # pages quarantined meanwhile aside, a no-op
            if older.pages():
                progress('done')
                return      # pages that failed, tried again after the next sync

        MessageHistory().wait_for_writes()
        unlink(os.path.join(cache.directory, self.journal_snapshot_backfill))
        unlink(os.path.join(cache.directory, self.journal_snapshot_reads))
        unlink(os.path.join(cache.directory, self.journal_snapshot_complete))
        try:
            os.rmdir(os.path.join(cache.directory, self.journal_snapshot_older))
        except OSError:
            pass
        activity.info(f'[journal] First sync of {account.id} complete: {done} older entries applied in {time.monotonic() - started:.1f}s')
        jlog(account.id, 'snapshot complete', entries=done, took=f'{time.monotonic() - started:.1f}s')
        progress('done', done)
        # the contacts of the older history are filed and the badges counted, as after a journal run
        notification_center.post_notification('BlinkJournalDidApply', sender=account, data=NotificationData(new_messages=dict(contacts), stats_path=None,
                                                                                                         first_sync=False, first_sync_marker=None))
        self._fetch_public_keys_once(account)

    def _apply_cached_journal(self, account, stats=None):
        """Apply cached journal pages oldest first.

        A page is deleted only once applied. A page that cannot be applied stops
        the run (order matters) and is retried on the next sync; after
        MAX_PAGE_ATTEMPTS failed runs it is quarantined and the pages after it go on.
        """
        cache = JournalCache(self._journal_directory(account))
        names = cache.pages()
        jlog = JournalLog()
        marker = self._journal_first_sync_marker(account)
        if not names:
            self._journal_progress(account, 'done')
            if stats is not None:
                jlog(account.id, 'sync end', pages=0, entries=0)
            if os.path.exists(marker):
                # quit after the last page and before the read state was settled: settle it now
                jlog(account.id, 'first sync unfinished', pages=0, settle='now')
                NotificationCenter().post_notification('BlinkJournalDidApply', sender=account, data=NotificationData(new_messages={}, stats_path=None, first_sync=True, first_sync_marker=marker))
            return
        activity = ActivityLog()
        activity.info(f'[journal] Applying {len(names)} cached journal pages of {account.id}')
        if stats is None:
            resumed = os.path.exists(marker)
            stats = JournalStats(account.id, first_sync=resumed, reason='cached pages')
            jlog(account.id, 'sync start', reason='cached pages left from before', first_sync=resumed, resumed=resumed or None)
        # the entries to apply, for the progress: known for the pages just downloaded, counted for pages left from before
        sizes = {page['file']: page['entries'] for page in stats.pages}
        for name in names:
            if name not in sizes:
                try:
                    sizes[name] = len(cache.load(name).get('messages') or [])
                except (OSError, ValueError, AttributeError):
                    sizes[name] = 0
        total_entries = sum(sizes[name] for name in names)
        done_entries = 0
        quarantine = os.path.join(cache.directory, cache.quarantine_directory)
        try:
            in_quarantine = len([name for name in os.listdir(quarantine) if name.endswith('.json')])
        except OSError:
            in_quarantine = 0
        downloaded = {page['file'] for page in stats.pages}
        jlog(account.id, 'apply queue', pages=len(names), entries=total_entries, leftover=len([name for name in names if name not in downloaded]) or None,
             quarantine=in_quarantine or None)
        started = time.monotonic()
        totals = Counter()
        contacts = Counter()
        applied = 0
        interrupted = None
        for name in names:
            if self.quitting:
                interrupted = (name, 'before the page')
                break
            try:
                page = cache.load(name)
            except (OSError, ValueError) as e:
                page = None
                error = e
            if page is not None:
                before = done_entries
                page_started = time.monotonic()
                page_first_sync = stats.first_sync or not page.get('cursor')
                jlog(account.id, 'page open', file=name, entries=sizes.get(name), cursor=page.get('cursor') or None, first_sync=page_first_sync,
                     attempt=cache._attempts().get(name, 0) + 1)

                def progress(index, total, db_wait=0.0):
                    self._journal_progress(account, 'apply', before + index, total_entries)
                    if index and index % 1000 == 0:
                        elapsed = time.monotonic() - page_started
                        jlog(account.id, 'page progress', file=name, done=f'{index}/{total}', total=f'{before + index}/{total_entries}',
                             rate=f'{index / elapsed if elapsed else 0:.0f}/s', dbwait=f'{db_wait:.1f}s')
                progress(0, 0)
                try:
                    # a first sync is one for all its pages (only the first page has no cursor)
                    page_stats = self._apply_server_history_messages(account, page.get('messages') or [], first_sync=page_first_sync,
                                                                     contacts=contacts, stats=stats, progress=progress)
                except JournalInterrupted as e:
                    interrupted = (name, str(e))
                    break
                except Exception as e:
                    page_stats = None
                    error = e
            if page is None or page_stats is None:
                attempts, quarantined = cache.failed(name)
                if quarantined:
                    stats.quarantined.append(name)
                    self.journal_receipts.pop(account.id, None)     # collected from the bad page
                    jlog(account.id, 'page quarantined', file=name, attempts=attempts, error=str(error)[:200], moved=f'{cache.quarantine_directory}/')
                    activity.error(f'[journal] Journal page {name} of {account.id} failed {attempts} times and was moved to quarantine: {error}')
                    continue
                activity.exception(f'[journal] Applying journal page {name} of {account.id} failed (attempt {attempts}), kept for the next sync: {error}')
                self.journal_receipts.pop(account.id, None)         # collected again when the page is retried
                jlog(account.id, 'page failed', file=name, attempt=f'{attempts}/{cache.max_attempts}', error=str(error)[:200], kept='yes, the run stops here')
                break
            receipts = self.journal_receipts.pop(account.id, None)
            if receipts:
                # applied with the page, before it is deleted: a quit cannot lose them
                from blink.history import MessageHistory
                MessageHistory().apply_receipts(receipts, account.id, page=name)
                MessageHistory().wait_for_writes()
            cache.applied(name)
            applied += 1
            done_entries += sizes.get(name, 0)
            totals.update(page_stats)
            activity.info(f'[journal] Applied journal page {applied} of {len(names)} of {account.id} ({sizes.get(name, 0)} entries, '
                          f'{time.monotonic() - page_started:.1f}s) and removed it: {name}')
            jlog(account.id, 'page applied', file=name, took=f'{time.monotonic() - page_started:.1f}s',
                 **{re.sub(r'[^0-9a-z]+', '_', outcome.lower()).strip('_'): count for outcome, count in sorted(page_stats.items())}, deleted='yes')
            log.info(f'Applied journal page {name}: ' + ', '.join(f'{count} {outcome}' for outcome, count in sorted(page_stats.items())))
        stats.apply_seconds += time.monotonic() - started
        if interrupted is not None:
            # nothing is settled: the page is kept (not counted as a failed attempt), its receipts are
            # collected again with it, the first sync stays unfinished; what was stored of the page is
            # recognised as already stored when it is applied again
            name, where = interrupted
            self.journal_receipts.pop(account.id, None)
            left = len(names) - applied
            ActivityLog().info(f'[journal] Import of {account.id} stopped by quitting ({where}): {applied} of {len(names)} pages applied,'
                               f' {left} kept for the next start ({name} onwards)')
            jlog(account.id, 'apply interrupted', page=name, where=where, applied=f'{applied}/{len(names)}', kept=left)
            self._journal_progress(account, 'done')
            return
        summary = ', '.join(f'{count} {outcome}' for outcome, count in sorted(totals.items())) or 'nothing'
        activity.info(f'[journal] Applied {sum(totals.values())} journal entries of {account.id} from {applied} of {len(names)} pages in {stats.apply_seconds:.1f}s: {summary}')
        for line in stats.summary_lines():
            activity.info(f'[journal] {line}')
        try:
            stats_path = stats.write(ApplicationData.get('logs'))
        except OSError as e:
            stats_path = None
            activity.warning(f'[journal] Cannot write the import statistics: {e}')
        else:
            if stats_path is not None:
                activity.info(f'[journal] Import statistics written to {stats_path}')
        self._journal_progress(account, 'done')
        jlog(account.id, 'apply end', pages=f'{applied}/{len(names)}', entries=f'{done_entries}/{total_entries}', took=f'{stats.apply_seconds:.1f}s',
             failed=totals.get('failed') or None, quarantined=len(stats.quarantined) or None)
        jlog(account.id, 'sync end', downloaded=sum(page['entries'] for page in stats.pages), applied=done_entries,
             took=f'{stats.download_seconds + stats.apply_seconds:.1f}s', stats=os.path.basename(stats_path) if stats_path else None)
        if not stats.first_sync:
            self._fetch_public_keys_once(account)       # an account whose first sync was finished before this existed
        # one notification for the whole run: unread counts and the Messages group are refreshed from
        # history, then the database is counted against this run
        NotificationCenter().post_notification('BlinkJournalDidApply', sender=account, data=NotificationData(new_messages=dict(contacts), stats_path=stats_path,
                                                                                                  first_sync=stats.first_sync and applied == len(names) - len(stats.quarantined),
                                                                                                  first_sync_marker=marker))

    @run_in_thread('sync')
    def _process_server_history_messages(self, account, messages):
        try:
            self._apply_server_history_messages(account, messages)
        except JournalInterrupted:
            pass        # quitting

    @staticmethod
    def _journal_progress(account, phase, done=None, total=None):
        """For the main window's progress bar: 'download' or 'apply', done of total entries (total None: not known), then 'done'."""
        NotificationCenter().post_notification('BlinkJournalProgress', sender=account, data=NotificationData(phase=phase, done=done, total=total))

    def _apply_server_history_messages(self, account, messages, first_sync=False, contacts=None, stats=None, progress=None, mark_synced=True,
                                       batch=None, duty=None, pace=None):
        """Apply journal entries by content type (blink.journal.journal_action). Runs in the sync thread.

        Bulk mode: nothing here creates a conversation or decrypts; an entry for a
        conversation that is open is also shown there. Returns {outcome: count}.
        An entry that cannot be applied is counted as failed and logged; the page
        still counts as applied.
        """
        outcomes = Counter()
        contacts = contacts if contacts is not None else Counter()
        started = time.monotonic()
        # a first sync is a snapshot: every message has its final state in it and removed ones are not
        # in it, so the events (receipts, reads, removals) have nothing to act on (journal_snapshot_skipped)
        log.debug(f'-- {len(messages)} messages fetched from server for {account.id}')
        # what an earlier run of Blink stored (received live before quitting, say) is a duplicate
        # too: the in-memory seen ids start empty, and handling it again would show it as new
        from blink.history import MessageHistory
        stored = MessageHistory().stored_message_ids(message.get('message_id') for message in messages)
        db_wait = 0.0       # seconds waited for the db thread (stats of the journal log)
        # batch: entries between pauses; duty: the pause is this many times the time the batch took
        # (2.0: busy a third of the time), for work in the background that must not hog the CPU;
        # pace: a callable giving (batch, duty) again after every pause, as the user changes it
        if pace is not None:
            batch, duty = pace()
        batch = batch or self.journal_progress_every
        batch_started = time.monotonic()
        in_batch = 0
        for index, message in enumerate(messages, 1):
            if self.quitting:
                # what fails while Blink stops would be counted as failed and the page deleted with it
                raise JournalInterrupted(f'quitting at entry {index} of {len(messages)}')
            content_type = str(message.get('content_type') or '').lower()
            action = journal_action(content_type)
            if self.seen_message_ids.seen(message.get('message_id')) or str(message.get('message_id') or '') in stored:
                outcome = 'duplicates'      # already handled live (or earlier in this run), or already stored
            elif first_sync and action in self.journal_snapshot_skipped:
                outcome = f'{action.replace("_", " ")} skipped (final state)'
            else:
                try:
                    outcome = getattr(self, f'_journal_{action}')(account, message, content_type, first_sync, contacts)
                except Exception as e:
                    outcome = 'failed'
                    self.seen_message_ids.forget(message.get('message_id'))
                    log.warning(f'Journal entry {message.get("message_id")} ({content_type}) of {account.id} could not be applied: {e!r}')
            outcomes[outcome] += 1
            if stats is not None:
                stats.entry(content_type, outcome, message.get('contact'), message.get('direction'), message.get('timestamp'))
            in_batch += 1
            if in_batch >= batch:
                in_batch = 0
                # wait for the db thread to store what was queued: the queue stays short, so a
                # conversation opened meanwhile loads between batches, not after the whole run;
                # the contact list and the open conversation hear of the batch once per conversation
                waited = time.monotonic()
                MessageHistory().flush_batched_stores()
                MessageHistory().wait_for_writes()
                db_wait += time.monotonic() - waited
                elapsed = time.monotonic() - started
                log.info(f'Applied {index} of {len(messages)} journal entries of {account.id} ({index / elapsed if elapsed else 0:.0f}/s)')
                if progress is not None:
                    progress(index, len(messages), db_wait)
                busy = time.monotonic() - batch_started
                time.sleep(max(self.journal_throttle, busy * duty) if duty else self.journal_throttle)
                if pace is not None:
                    while self.journal_import_speed == 'paused' and not self.quitting:
                        time.sleep(0.5)     # paused by the user (the button by the progress bar)
                    batch, duty = pace()
                batch_started = time.monotonic()
        MessageHistory().flush_batched_stores()
        MessageHistory().wait_for_writes()
        if mark_synced:
            account.sms.history_synchronization_timestamp = ISOTimestamp.now()
            account.save()
        return outcomes

    def _journal_session(self, contact):
        return next((session for session in self.sessions if session.contact.settings is contact.settings), None)

    @staticmethod
    def _journal_timestamp(message):
        return ISOTimestamp(message['timestamp']).replace(tzinfo=timezone.utc).astimezone(tzlocal())

    def _journal_ignored(self, account, message, content_type, first_sync, contacts):
        return 'ignored'

    def _journal_receipt(self, account, message, content_type, first_sync, contacts):
        # the receipt's state is in its payload; the entry's own state is the journal's ('received')
        payload = parse_payload(message.get('content'))
        if not payload or not payload.get('message_id'):
            return 'failed'
        status = payload.get('state') or message.get('state')
        kwargs = {'data': NotificationData(id=payload['message_id'], status=status)}
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(message['contact'])
        session = self._journal_session(contact)
        if session is not None:
            kwargs['sender'] = session
        NotificationCenter().post_notification('BlinkGotDispositionNotification', **kwargs)
        return 'receipts'

    def _journal_conversation_remove(self, account, message, content_type, first_sync, contacts):
        from blink.contacts import URIUtils
        # a bare address, or {"contact", "timestamp"}: the same two shapes as a read marker
        address, device = conversation_read_marker(message.get('content'))
        if not address:
            return 'failed'
        contact, contact_uri = URIUtils.find_contact(address)
        timestamp = ISOTimestamp(message['timestamp'])
        text = f'[Message with {contact_uri.uri}] Conversation removed on another device (from the journal), messages up to {timestamp} for account {account.id}'
        log.debug(text)     # a journal entry: debug level
        session = self._journal_session(contact)
        if session is None:
            NotificationCenter().post_notification('BlinkGotHistoryConversationRemove', sender=account, data=NotificationData(contact=contact_uri.uri, timestamp=timestamp))
        else:
            NotificationCenter().post_notification('BlinkConversationWillRemove', sender=session, data=NotificationData(contact=session.contact_uri.uri, timestamp=timestamp))
        return 'conversations removed'

    def _journal_message_remove(self, account, message, content_type, first_sync, contacts):
        payload = parse_payload(message.get('content'))
        if not payload or not payload.get('message_id'):
            return 'failed'
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(message['contact'])
        # the entry time is when the removal was made: a removal kept for a message that is
        # not stored yet (it can come later in the journal) is applied with that time
        NotificationCenter().post_notification('BlinkGotHistoryMessageDelete', sender=account,
                                               data=NotificationData(message_id=payload['message_id'], timestamp=message.get('timestamp'),
                                                                     remote_uri=contact_uri.uri if contact_uri is not None else message.get('contact'),
                                                                     source='removed on another device (journal)'))
        session = self._journal_session(contact)
        if session is not None:
            NotificationCenter().post_notification('BlinkGotMessageDelete', sender=session, data=payload['message_id'])
        return 'messages removed'

    def _journal_messages_bulk_delete(self, account, message, content_type, first_sync, contacts):
        return self.apply_messages_bulk_delete(account, message.get('content'), 'from the journal')

    def _journal_conversation_read(self, account, message, content_type, first_sync, contacts):
        # the conversation is in the payload; the entry's own contact is the fallback
        contact, device = conversation_read_marker(message.get('content'))
        contact = contact or message.get('contact')
        if not contact:
            return 'failed'
        NotificationCenter().post_notification('BlinkConfirmReadMessagesOnOtherDevice', data=NotificationData(remote_uri=contact, timestamp=message.get('timestamp')))
        return 'conversations read'

    def _journal_public_key(self, account, message, content_type, first_sync, contacts):
        if message['contact'] == account.id:
            return 'own keys'
        self._save_pgp_key(message['content'], message['contact'])
        return 'public keys'

    def _journal_store(self, account, message, history_message, remote_uri, encryption=None, state=None):
        """Persist a journal entry (history decides read state and category) and show it if its conversation is open.

        Queued on the db thread from here, not through a notification handled in the GUI thread: an import
        is thousands of entries. What was stored is told once per conversation (flush_batched_stores)."""
        from blink.history import MessageHistory
        read = None
        points = self._snapshot_points.get(account.id)
        if points and history_message.direction == 'incoming':
            point = points.get(self._read_point_key(remote_uri))
            try:
                read = point is not None and history_message.timestamp <= ISOTimestamp(point)
            except (TypeError, ValueError):
                read = None
        MessageHistory.add_from_server_history(account, remote_uri, history_message, state=state, encryption=encryption, batched=True, read=read)

    def _journal_sender(self, account, message, contact):
        if message['direction'] == 'incoming':
            return ChatIdentity(SIPURI.parse(f'sip:{contact.uri.uri}'), contact.name)
        return account

    def _journal_file_transfer(self, account, message, content_type, first_sync, contacts):
        self.note_file_transfer_url(account, message.get('content'))
        document = parse_payload(message.get('content'))
        if not document or not document.get('filename'):
            return 'failed'
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(message['contact'])
        until = document.get('until') or str(ISOTimestamp(datetime.now() + timedelta(days=30)))
        file_hash = document.get('hash')
        new_body = FTHTTPDocument.create(file=[FileInfo(file_size=document.get('filesize'), file_name=document['filename'], content_type=document.get('filetype'),
                                                        url=document.get('url'), until=until, hash=file_hash)])
        is_secure = str(document['filename']).endswith('.asc')
        history_message = BlinkMessage(new_body.decode(), FTHTTPDocument.content_type, self._journal_sender(account, message, contact),
                                       timestamp=self._journal_timestamp(message), id=message['message_id'],
                                       disposition=message.get('disposition'), direction=message['direction'], is_secure=is_secure)
        # the entry's state is final (a file the user opened on another device is displayed)
        self._journal_store(account, message, history_message, contact.uri.uri, encryption='OpenPGP' if is_secure else None, state=message.get('state') or 'accepted')
        if message['direction'] == 'incoming':
            contacts[contact.uri.uri] += 1
        # a first sync only stores: an open conversation loads what was stored from history
        session = None if first_sync else self._journal_session(contact)
        if session is not None:
            NotificationCenter().post_notification('BlinkGotMessage', sender=session, data=NotificationData(message=history_message, history=True, first_sync=first_sync, account=account))
            file = File(document['filename'], document.get('filesize'), contact, file_hash, message['message_id'], ISOTimestamp(until),
                        document.get('url'), account=account, protocol='sylk')
            NotificationCenter().post_notification('BlinkSessionDidShareFile', sender=session, data=NotificationData(file=file, direction=message['direction'], history=True))
        return 'files'

    def _journal_text(self, account, message, content_type, first_sync, contacts):
        if message.get('contact') is None:
            return 'failed'
        content = message.get('content') or ''
        if content.startswith('?OTR:') or content.startswith('?OTRv3?'):
            return 'OTR skipped'
        if is_file_transfer_notice(message.get('content_type'), content):
            return 'file transfer notices skipped'
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(message['contact'])
        history_message = BlinkMessage(content, message['content_type'], self._journal_sender(account, message, contact),
                                       timestamp=self._journal_timestamp(message), id=message['message_id'],
                                       disposition=message.get('disposition'), direction=message['direction'])
        encryption = self.check_encryption(history_message.content_type, history_message.content)
        self._journal_store(account, message, history_message, message['contact'], encryption=encryption, state=message.get('state'))
        if message['direction'] == 'incoming':
            contacts[contact.uri.uri] += 1
        # a first sync only stores: an open conversation loads what was stored from history, and
        # the background decryption opens what is encrypted
        session = None if first_sync else self._journal_session(contact)
        if session is not None:
            # a first sync replays history: its senders got their receipts from the devices that were there
            if not first_sync and message['direction'] == 'incoming' and 'positive-delivery' in (history_message.disposition or ()):
                self.send_imdn_message(session, history_message.id, history_message.timestamp, 'delivered')
            NotificationCenter().post_notification('BlinkGotMessage', sender=session, data=NotificationData(message=history_message, history=True, first_sync=first_sync, account=account))
            if encryption == 'OpenPGP':
                if first_sync:
                    self._journal_decrypts.add(history_message.id)
                if session.fake_streams.get('messages').can_decrypt:
                    session.fake_streams.get('messages').decrypt(history_message)
                else:
                    self._incoming_encrypted_message_queue.append((history_message, account, contact))
        return 'texts'

    def _take_call_record(self, account, body, metadata, party, message_id, origin):
        """Hand a call detail record from another device to history; returns the journal outcome."""
        record, refused = foreign_call_record(body, metadata, party, account.id, this_device_id())
        if record is None:
            text = f'[Message] Call record message {message_id} for account {account.id} skipped ({origin}): {refused}'
            log.debug(text) if 'journal' in str(origin) else ActivityLog().info(text)     # journal entries: debug level
            return f'call records skipped ({refused})'
        NotificationCenter().post_notification('BlinkGotHistoryCallRecord', sender=account,
                                               data=NotificationData(record=record, message_id=message_id, origin=origin))
        return 'call records'

    def _journal_call_record(self, account, message, content_type, first_sync, contacts):
        return self._take_call_record(account, message.get('content'), message.get('metadata'), message.get('contact'),
                                      message.get('message_id'), 'from the journal')

    def _journal_inert(self, account, message, content_type, first_sync, contacts):
        """Stored as it is and never unread: locations, metadata companions, call records and
        types this version does not know. Their own handling comes with later patches."""
        if message.get('contact') is None:
            return 'failed'
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(message['contact'])
        history_message = BlinkMessage(message.get('content') or '', message['content_type'], self._journal_sender(account, message, contact),
                                       timestamp=self._journal_timestamp(message), id=message['message_id'],
                                       disposition=message.get('disposition'), direction=message['direction'], metadata=message.get('metadata'))
        self._journal_store(account, message, history_message, message['contact'], state=message.get('state'))
        if content_type == LOCATION_CONTENT_TYPE:
            tick = location_storage_fields(history_message.content, history_message.metadata)
            if (not first_sync and message['direction'] == 'incoming' and tick.get('related_action') == 'location_request'
                    and 'positive-delivery' in (history_message.disposition or ())):
                self._confirm_location_request(account, contact, history_message, open_only=True)
            return f'location {tick["related_action"]}' if tick else 'location (unreadable)'
        if content_type == METADATA_CONTENT_TYPE:
            link = metadata_link(history_message.content)
            return f'metadata {link[1]}' if link is not None else 'metadata (unlinked)'
        return f'stored as {content_type}'

    def handle_notification(self, notification):
        if notification.name == 'SIPApplicationWillEnd':
            self.quitting = True        # at once, in the posting thread: the import thread checks it between entries
            return
        self._handle_in_gui_thread(notification)

    @run_in_gui_thread
    def _handle_in_gui_thread(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @run_in_thread('file-io')
    def _SH_ImportPGPKeys(self, request, decrypted_message):
        public_key = None
        private_key = None

        regex = "(?P<public_key>-----BEGIN PGP PUBLIC KEY BLOCK-----.*-----END PGP PUBLIC KEY BLOCK-----).*(?P<private_key>-----BEGIN PGP PRIVATE KEY BLOCK-----.*-----END PGP PRIVATE KEY BLOCK-----)"
        matches = re.search(regex, decrypted_message, re.DOTALL)
        try:
            public_key = matches.group('public_key')
            private_key = matches.group('private_key')
        except AttributeError:
            return

        if private_key is None or public_key is None:
            return

        if self._compare_public_key(request.account, public_key):
            return

        settings = SIPSimpleSettings()
        directory = os.path.join(settings.chat.keys_directory.normalized, 'private')
        filename = os.path.join(directory, request.account.id)
        makedirs(directory)

        with open(f'{filename}.privkey', 'wb') as f:
            f.write(str(private_key).encode())

        with open(f'{filename}.pubkey', 'wb') as f:
            f.write(str(public_key).encode())

        request.account.sms.private_key = f'{filename}.privkey'
        request.account.sms.public_key = f'{filename}.pubkey'
        request.account.save()
        ActivityLog().info(f'[pgp] Imported the PGP private key of account {request.account.id} from another device')
        call_in_gui_thread(self._keys_installed, request.account)
        call_in_gui_thread(KeyEscrowManager().key_imported, request.account)

    def _keys_installed(self, account):
        """A keypair was adopted for this account (imported, or restored from the escrow):
        start using it and decrypt what waited for it."""
        for request in list(self.pgp_requests[account, GeneratePGPKeyRequest]):
            request.dialog.hide()
            self.pgp_requests.remove(request)

        for session in [session for session in self.sessions if session.account is account]:
            stream = session.fake_streams.get('messages')
            if stream and not stream.can_encrypt:
                stream.enable_pgp()

        while self._incoming_encrypted_message_queue:
            message, queued_account, contact = self._incoming_encrypted_message_queue.popleft()
            try:
                blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)
            except StopIteration:
                pass
            else:
                stream = blink_session.fake_streams.get('messages')
                if not stream.can_encrypt:
                    stream.enable_pgp()

                stream.decrypt(message)

        from blink.history import ConversationPreviews
        ConversationPreviews().invalidate()     # previews of messages the key can now open

    def _SH_ExportPGPKeys(self, request, message):
        account = request.account
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(account.uri)
        outgoing_message = OutgoingMessage(account, contact, message, 'text/pgp-private-key')
        self._send_message(outgoing_message)

    def _offer_key_generation(self, account, scenario, session=None):
        """This device has no key for the account. Generating one while the account already has
        a key elsewhere orphans every message encrypted to it, so nothing happens before
        KeyEscrowManager knows: the addressbook (escrow) has been fetched and the server has
        answered the public key lookup. Only when both say there is no key is one generated
        without asking; in every other case the user is asked."""
        KeyEscrowManager().when_answered(account, lambda: self._key_generation_decided(account, scenario, session))

    @staticmethod
    def _pgp_messaging_possible(account):
        """PGP messaging needs SylkServer (key lookup, escrow, journal): Bonjour aside, no key
        is made for an account whose domain has none (blink.sylk_discovery)."""
        if account is BonjourAccount():
            return True
        from blink.sylk_discovery import has_sylkserver
        return has_sylkserver(account)

    def _key_generation_decided(self, account, scenario, session=None):
        if not account.sms.enable_pgp or (account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized)):
            return      # restored from the escrow meanwhile, or PGP turned off
        if not self._pgp_messaging_possible(account):
            refused = self.__dict__.setdefault('_key_generation_refused', set())
            if account.id not in refused:
                refused.add(account.id)
                ActivityLog().info(f'[pgp] Not generating a PGP key for account {account.id}: {account.id.domain} has no SylkServer')
            return
        silent, reason = KeyEscrowManager().may_generate_silently(account)
        if silent:
            ActivityLog().info(f'[pgp] No PGP key for account {account.id} in the addressbook or on the server, generating one')
            self._generate_keys(account, session)
            return
        declined = self.__dict__.setdefault('_key_generation_declined', set())
        if account.id in declined:
            return      # asked already this run and the user said no
        ActivityLog().info(f'[pgp] Not generating a PGP key for account {account.id} without asking: {reason}')
        if KeyEscrowManager().server_key.get(account.id) == 'present' and not key_escrow.self_escrow_records(account):
            scenario = 2        # on another device: say so
        self._show_generate_dialog(account, scenario, session)

    def _generate_keys(self, account, session=None):
        """Make the account's keypair in the background: either nobody has one (decided by
        _key_generation_decided) or the user chose Generate."""
        if not account.sms.enable_pgp or (account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized)):
            return      # restored from the escrow meanwhile, or PGP turned off
        if not self._pgp_messaging_possible(account):
            ActivityLog().info(f'[pgp] Not generating a PGP key for account {account.id}: {account.id.domain} has no SylkServer')
            return
        generating = self.__dict__.setdefault('_keys_generating', set())
        if account.id in generating:
            return
        generating.add(account.id)

        def generate():
            from blink.streams.message import create_account_keys
            try:
                result = create_account_keys(account)
            except Exception as e:
                call_in_gui_thread(self._keys_not_generated, account, e)
            else:
                call_in_gui_thread(self._keys_generated, account, session, *result)

        import threading
        threading.Thread(target=generate, name='pgp-keygen', daemon=True).start()

    def _keys_not_generated(self, account, error):
        self._keys_generating.discard(account.id)
        ActivityLog().error(f'[pgp] Could not generate a PGP key for account {account.id}: {error!r}')

    def _keys_generated(self, account, session, private_key, private_file, public_file):
        self._keys_generating.discard(account.id)
        if account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized) and account.sms.private_key.normalized != os.path.realpath(private_file):
            return      # another key arrived meanwhile (escrow, import)
        account.sms.private_key = private_file
        account.sms.public_key = public_file
        account.save()
        for request in list(self.pgp_requests[account, GeneratePGPKeyRequest]):
            request.dialog.hide()
            self.pgp_requests.remove(request)
        sessions = [blink_session for blink_session in self.sessions if blink_session.account is account]
        for blink_session in sessions:
            stream = blink_session.fake_streams.get('messages')
            if stream is not None:
                stream._load_pgp_keys()
        if session is None or session not in sessions:
            session = sessions[0] if sessions else None
        if session is not None:
            NotificationCenter().post_notification('PGPKeysDidGenerate', sender=session, data=NotificationData(private_key=private_key, public_key=private_key.pubkey))
        else:
            ActivityLog().info(f'[pgp] Generated a new PGP key {private_key.fingerprint.keyid} for account {account.id}')
        self._keys_installed(account)
        try:
            from blink.contacts import AddressbookNotifier
            with AddressbookNotifier().quiet():
                KeyEscrowManager().repair(account)      # keep it on the server now, not on the next reload
        except Exception as e:
            ActivityLog().exception(f'[pgp] Key escrow repair failed for {account.id}: {e!r}')

    def _show_generate_dialog(self, account, scenario, session=None):
        if not account.sms.enable_pgp or (account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized)):
            return      # restored from the escrow meanwhile, or PGP turned off
        if self.pgp_requests[account, GeneratePGPKeyRequest]:
            return
        generate_dialog = GeneratePGPKeyDialog()
        generate_request = GeneratePGPKeyRequest(generate_dialog, account, scenario, session)
        generate_request.accepted.connect(self._SH_GeneratePGPKeys)
        generate_request.rejected.connect(self._SH_GeneratePGPKeysDeclined)
        generate_request.finished.connect(self._SH_PGPRequestFinished)
        bisect.insort_right(self.pgp_requests, generate_request)
        generate_request.dialog.show()

    def _SH_GeneratePGPKeys(self, request):
        ActivityLog().info(f'[pgp] Generating a new PGP key for account {request.account.id}, as the user chose')
        self._generate_keys(request.account, request.session)

    def _SH_GeneratePGPKeysDeclined(self, request):
        self.__dict__.setdefault('_key_generation_declined', set()).add(request.account.id)
        ActivityLog().info(f'[pgp] The user chose not to generate a PGP key for account {request.account.id}')

    def _SH_PGPRequestFinished(self, request):
        request.dialog.hide()
        self.pgp_requests.remove(request)

    def _NH_SylkServerConfigurationDidChange(self, notification):
        # a domain found to run SylkServer: its accounts that wanted a token and did not ask for one
        for account in AccountManager().get_accounts():
            if (account is not BonjourAccount() and account.enabled and account.id.domain == notification.data.domain
                    and account.id in self._token_not_asked and not account.sms.history_synchronization_token):
                self._request_history_synchronization_token(account, 'SylkServer found')

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        try:
            KeyEscrowManager().check_key_status(notification.sender)
        except Exception as e:
            ActivityLog().exception(f'[pgp] Checking the private key failed: {e!r}')
        account = notification.sender
        if account is not BonjourAccount():
            # give the registration a moment to settle (and the server to see the device)
            call_later(self.sync_registration_delay, self._sync_messages, account, 'registered')
            self.file_transfer_base_url(account)     # says where files will be uploaded, once per change

    # file transfer endpoint

    def file_transfer_base_url(self, account):
        """Where this account uploads files, or None if we cannot tell.

        The domain's SylkServer (its discovered configuration: wsServer, as mobile), else what a
        received transfer told us (the server's own URL, kept on the account), else what the
        journal URL implies, else nothing. The URL is logged when it changes.
        """
        if account is BonjourAccount():
            return None
        from blink.sylk_discovery import server_http_url
        server = server_http_url(account)
        if server:
            discovered = server + FILE_TRANSFER_PATH
            self._log_transfer_url(account, discovered, f'[transfer] File transfer URL of {account.id}: {discovered} (from the SylkServer configuration of {account.id.domain})')
            return discovered
        stored = account.sms.file_transfer_url
        if stored:
            self._log_transfer_url(account, str(stored), f'[transfer] File transfer URL of {account.id}: {stored} (learned from a received transfer)')
            return str(stored)
        history_url = account.sms.history_synchronization_url
        if not history_url:
            return None
        derived = derive_base_url(history_url)
        if derived is None:
            self._log_transfer_url(account, None, f'[transfer] Cannot derive the file transfer URL of {account.id} from {history_url}; it will be learned from the first file received')
        else:
            self._log_transfer_url(account, derived, f'[transfer] File transfer URL of {account.id} derived from the journal URL: {derived}')
        return derived

    def _log_transfer_url(self, account, url, message):
        logged = self.__dict__.setdefault('_transfer_url_logged', {})
        if logged.get(account.id, False) == url:
            return
        logged[account.id] = url
        ActivityLog().info(message)

    # files for our own devices

    def share_with_own_devices(self, account, path, transfer_id, party_uri, display_name, filename):
        """Upload a file made during a call (a screenshot) to this account itself, so that our
        other devices get it and the other party gets nothing. Only for an account that speaks
        SylkServer's API (a file transfer service and an API token); otherwise the file stays on
        this device, where it is already filed. Returns whether it is being uploaded.

        The server rebuilds the transfer message it broadcasts from the upload URL, which names
        us at both ends; which conversation the file belongs in travels first, as a
        call_recording metadata note sealed to our own key and sent to our own account.
        """
        if account is BonjourAccount() or not isinstance(account, Account):
            return False
        base = self.file_transfer_base_url(account)
        token = account.sms.history_synchronization_token
        if not base or not token:
            ActivityLog().info(f'[transfer] {filename} kept on this device only: {account.id} has ' + ('no file transfer service' if not base else 'no API token yet'))
            return False
        public_key = self._own_public_key(account)
        note = call_recording_envelope(transfer_id, party_uri, display_name, None, ISOTimestamp.now(),
                                       encrypt=(lambda text: self._seal(public_key, text)) if public_key is not None else None)
        if note is None:
            ActivityLog().warning(f'[transfer] {filename} ({transfer_id}): no own public key to seal where it belongs, our other devices will not know the conversation')
        else:
            from blink.contacts import URIUtils
            contact, contact_uri = URIUtils.find_contact(account.uri)
            self._send_message(OutgoingMessage(account, contact, note, METADATA_CONTENT_TYPE, sessionless=True))
            ActivityLog().info(f'[transfer] {filename} ({transfer_id}) placed in the conversation with {party_uri} for our other devices')
        self._upload_to_self(account, base, token, path, transfer_id, filename, public_key)
        return True

    @staticmethod
    def _own_public_key(account):
        if not account.sms.enable_pgp:
            return None
        filename = os.path.join(SIPSimpleSettings().chat.keys_directory.normalized, 'private', f'{account.id}.pubkey')
        try:
            key, _ = pgpy.PGPKey.from_file(filename)
        except Exception:
            return None
        return key

    @staticmethod
    def _seal(public_key, text):
        try:
            return str(public_key.encrypt(PGPMessage.new(text)))
        except Exception as e:
            ActivityLog().warning(f'[transfer] Cannot seal a note to our own key: {e}')
            return None

    @run_in_thread('file-transfer')
    def _upload_to_self(self, account, base, token, path, transfer_id, filename, public_key):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError as e:
            ActivityLog().error(f'[transfer] Cannot read {path}: {e}')
            return
        name = re.sub(r'[\s:]', '_', filename).lstrip('./') or f'file-{transfer_id}'
        content_type = 'application/octet-stream'
        if public_key is not None:
            try:
                data = str(public_key.encrypt(PGPMessage.new(data, file=True, filename=name))).encode()
                name += '.asc'
            except Exception as e:
                ActivityLog().warning(f'[transfer] Cannot encrypt {name} to our own key, uploading it as it is: {e}')
        else:
            import mimetypes
            content_type = mimetypes.guess_type(name)[0] or content_type
        url = upload_url(base, account.id, account.id, transfer_id, name)
        ActivityLog().info(f'[transfer] Uploading {name} ({len(data)} bytes) for our own devices to {url}')
        try:
            response = requests.post(url, data=data, headers={'Content-Type': content_type, 'Authorization': f'Apikey {token}'}, timeout=(15, 600))
        except requests.RequestException as e:
            ActivityLog().warning(f'[transfer] Uploading {name} for our own devices failed: {e}')
            return
        if 200 <= response.status_code < 300:
            ActivityLog().info(f'[transfer] {name} uploaded for our own devices: {response.status_code}')
        else:
            ActivityLog().warning(f'[transfer] Uploading {name} for our own devices failed: HTTP {response.status_code} {response.reason}')

    def note_file_transfer_url(self, account, body):
        """Learn the endpoint from a transfer that has just arrived (live or from the journal).
        Returns at once from the second transfer on: the first one is kept."""
        if account is BonjourAccount() or account.sms.file_transfer_url:
            return
        meta = file_transfer_envelope(body)
        base = base_url_from_transfer(meta.get('url')) if meta else None
        if not base:
            return
        account.sms.file_transfer_url = base
        account.save()
        ActivityLog().info(f'[transfer] File transfer URL of {account.id} learned from an incoming transfer: {base}')

    def _NH_SIPEngineGotMessage(self, notification):
        account_manager = AccountManager()
        account = account_manager.find_account(notification.data.request_uri)

        if account is None:
            return

        data = notification.data
        content_type = data.headers.get('Content-Type', Null).content_type
        from_header = data.headers.get('From', Null)
        x_replicated_message = data.headers.get('X-Replicated-Message', Null)
        to_header = data.headers.get('To', Null)
        instance_id = data.from_header.uri.parameters.get('instance_id', None)
        # the client that sent it: a SylkServer relaying for a web or mobile client names it in X-Sylk-User-Agent
        sip_user_agent = _header_text(data.headers, 'User-Agent')
        client_user_agent = _header_text(data.headers, 'X-Sylk-User-Agent')
        user_agent = client_user_agent or sip_user_agent
        relay = sip_user_agent if client_user_agent else None
        via = f' using {user_agent}' + (f' via {relay}' if relay else '') if user_agent else ''

        if instance_id and instance_id.startswith('urn:uuid:'):
            instance_id = instance_id[9:]

        cpim_message = None
        if content_type == "message/cpim":
            try:
                cpim_message = CPIMPayload.decode(data.body)
            except CPIMParserError:
                log.warning('SIP message from %s to %s rejected: CPIM parse error' % (from_header.uri, '%s@%s' % (to_header.uri.user, to_header.uri.host)))
                return
            body = cpim_message.content if isinstance(cpim_message.content, str) else cpim_message.content.decode()
            content_type = cpim_message.content_type
            sender = cpim_message.sender or from_header
            disposition = next(([item.strip() for item in header.value.split(',')] for header in cpim_message.additional_headers if header.name == 'Disposition-Notification'), None)
            message_id = next((header.value for header in cpim_message.additional_headers if header.name == 'Message-ID'), str(uuid.uuid4()))
            metadata = next((header.value for header in cpim_message.additional_headers if header.name == 'Metadata'), None)   # agp.Metadata
        else:
            payload = SimplePayload.decode(data.body, data.content_type)
            body = payload.content.decode()
            content_type = payload.content_type
            sender = from_header
            disposition = None
            message_id = str(uuid.uuid4())
            metadata = None

        if content_type.lower() == 'application/sylk-storage-changed':
            # an object of our account storage changed (a contact avatar, the key escrow): acted on, never stored, shown or counted
            if account is not BonjourAccount():
                from blink.account_storage import AccountStorage
                from blink.key_storage import KeyStorage
                AccountStorage().handle_changed(account, body)
                KeyStorage().handle_changed(account, body)
            return

        encryption = self.check_encryption(content_type, body)
        enc_text = f'{encryption} encrypted ' if encryption else ''

        log.info(f'Message {message_id} {enc_text}{content_type.lower()} received from {_bare(sender.uri)} for account {account.id}{via}')
        if content_type.lower() not in (IsComposingDocument.content_type, IMDNDocument.content_type):
            def aor(uri):
                # SIP headers carry bytes, CPIM headers str
                user, host = (part.decode(errors='replace') if isinstance(part, bytes) else part for part in (uri.user, uri.host))
                return f'{user}@{host}'
            if x_replicated_message is not Null and message_id in self._own_message_ids:
                peer, what = aor(to_header.uri), 'Own message replicated back by the server, ignored:'
            elif x_replicated_message is not Null and message_id in self._own_upload_ids:
                peer, what = aor(to_header.uri), 'Own upload confirmed by the server:'
            elif x_replicated_message is not Null:
                peer, what = aor(to_header.uri), 'Outgoing (sent from another device)'
            else:
                peer, what = instance_id or aor(sender.uri), 'Incoming'
            text = f'[Message with {peer}] {what} {enc_text}{content_type.lower()} message {message_id} for account {account.id}{via}'
            if content_type.lower() == 'text/pgp-public-key' and self._quiet_public_key(account, peer):
                log.debug(text)         # an answer to the lookups after the first sync, or one during it: counted, not listed
            else:
                ActivityLog().info(text)
        if account is BonjourAccount() and instance_id:
            log.debug(f'Bonjour neighbour instance id is {instance_id}')

        if x_replicated_message is Null and account is not BonjourAccount():
            from blink.contacts import is_blocked_address
            if is_blocked_address(sender.uri, account):
                ActivityLog().info(f'[message] Dropped {content_type.lower()} message {message_id} from {sender.uri}: blocked')
                return

        if x_replicated_message is not Null and message_id in self._own_message_ids:
            # the server replicates a message to all devices of the sender, this one included
            log.debug(f'Ignoring replicated copy of message {message_id} sent by this device')
            return

        if encryption == 'OpenPGP':
            if account.sms.enable_pgp and (account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized)):
                if not self.pgp_requests[account, GeneratePGPKeyRequest] and account is not BonjourAccount():
                    self._offer_key_generation(account, 0)
            elif not account.sms.enable_pgp:
                log.info(f"-- Skipping PGP encrypted message, PGP is disabled for {account.id}")
                return

        # only a CPIM message carries the sender's id; without CPIM the id above is made up here
        if cpim_message is not None and self.seen_message_ids.seen(message_id):
            ActivityLog().info(f'[Message] {content_type.lower()} message {message_id} for account {account.id} skipped, it was already handled (live or from the journal)')
            return

        if cpim_message is not None and content_type.lower() not in (IsComposingDocument.content_type, IMDNDocument.content_type):
            from blink.history import MessageHistory
            MessageHistory().record_agent(message_id, 'sent', user_agent, relay)

        if content_type.lower() == IsComposingDocument.content_type and x_replicated_message is not Null:
            # our own typing notice, replicated back from the server: never "typing" to ourselves
            log.debug(f'Ignoring the replicated copy of our is-composing message {message_id} for account {account.id}')
            return

        if content_type.lower() == ADDRESSBOOK_UPDATE_CONTENT_TYPE:
            # another device of ours changed the addressbook: refetch it (never stored)
            if account is not BonjourAccount():
                from blink.contacts import AddressbookNotifier
                AddressbookNotifier().handle_tick(account, body, sender.uri)
            return

        if content_type.lower() == MESSAGES_BULK_DELETE_CONTENT_TYPE:
            # a Data manager deletion on another device of ours (the journal brings it again: applying twice erases nothing more)
            if account is not BonjourAccount():
                self.apply_messages_bulk_delete(account, body, 'live')
            return

        if content_type.lower() == DATA_EXPORT_CONTENT_TYPE:
            # another device of ours is exporting its history (Data manager > Import from device): never stored
            if account is not BonjourAccount():
                self.handle_data_export_announcement(account, body, sender.uri)
            return

        if content_type.lower() in FILE_TRANSFER_CONTENT_TYPES:
            self.note_file_transfer_url(account, body)

        if content_type.lower() in self.__not_history_content_types__:
            # not history: acted on elsewhere or not at all. The other types the journal ignores
            # (API token, private key, typing) are handled live below
            ActivityLog().info(f'[Message] {content_type.lower()} message {message_id} for account {account.id} skipped, it is not history')
            return

        if is_file_transfer_notice(content_type, body):
            log.info(f'File transfer notice {message_id} for account {account.id} skipped, the transfer comes as its own message')
            return

        if content_type.lower() == 'application/sylk-api-token':
            try:
                data = json.loads(body)
            except json.decoder.JSONDecodeError:
                return

            try:
                token = data['token']
                url = data['url']
            except KeyError:
                return

            changed = token != account.sms.history_synchronization_token or url != account.sms.history_synchronization_url
            account.sms.history_synchronization_token = token
            account.sms.history_synchronization_url = url
            account.sms.history_synchronization_timestamp = None
            account.save()
            ActivityLog().info(f'[journal] Received {"a new" if changed else "the same"} API token for account {account.id}, journal at {url}')
            self._sync_messages(account, 'token received')
            return

        if content_type.lower() == 'text/pgp-private-key':
            log.info(f'Received private key of account {account.id} from another device')
            if not account.sms.enable_pgp:
                log.info(f"-- Skipping private key import, PGP is disabled for {account.id}")
                return
            regex = "(?P<public_key>-----BEGIN PGP PUBLIC KEY BLOCK-----.*-----END PGP PUBLIC KEY BLOCK-----)"
            matches = re.search(regex, body, re.DOTALL)
            public_key = matches.group('public_key')

            if self._compare_public_key(account, public_key):
                return

            for request in self.pgp_requests[account]:
                request.dialog.hide()
                self.pgp_requests.remove(request)

            import_dialog = ImportDialog()
            incoming_request = ImportPrivateKeyRequest(import_dialog, body, account)
            incoming_request.accepted.connect(self._SH_ImportPGPKeys)
            incoming_request.finished.connect(self._SH_PGPRequestFinished)
            bisect.insort_right(self.pgp_requests, incoming_request)
            incoming_request.dialog.show()
            return

        if content_type.lower() == 'text/pgp-public-key':
            if account is BonjourAccount():
                if instance_id:
                    self._save_pgp_key(body, instance_id)
            else:
                self._save_pgp_key(body, sender.uri)
            return

        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(sender.uri, display_name=sender.display_name, instance_id=instance_id)

        if x_replicated_message is not Null:
            contact, contact_uri = URIUtils.find_contact(to_header.uri)

        session_manager = SessionManager()
        notification_center = NotificationCenter()

        if content_type == 'application/sylk-message-remove':
            payload = json.loads(body)
            notification_center.post_notification('BlinkGotHistoryMessageDelete', sender=account,
                                                  data=NotificationData(message_id=payload['message_id'], timestamp=payload.get('timestamp'),
                                                                        remote_uri=contact_uri.uri if contact_uri is not None else None,
                                                                        source='removed on another device'))

            try:
                blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)
            except StopIteration:
                pass
            else:
                notification_center.post_notification('BlinkGotMessageDelete', sender=blink_session, data=payload['message_id'])
            return

        if content_type.lower() == 'application/sylk-conversation-read':
            contact, device = conversation_read_marker(body)
            if contact is None:
                ActivityLog().error(f'[Message] Cannot read the conversation read marker {message_id} for account {account.id}: {body[:200]!r}')
                return
            if self._own_conversation_reads.is_echo(canonical_uri(contact, account), device, this_device_id()):
                # this device's own marker fanned back by the server: already applied here
                log.debug(f'Ignoring the echo of our conversation read marker for {contact}')
                return
            ActivityLog().info(f"[Message with {contact}] Conversation read on another device{f' ({device})' if device else ''} for account {account.id}")
            NotificationCenter().post_notification('BlinkConfirmReadMessagesOnOtherDevice', data=NotificationData(remote_uri=contact, timestamp=None))
            return

        if content_type.lower() == 'application/sylk-conversation-remove':
            address, device = conversation_read_marker(body)
            if address is None:
                ActivityLog().error(f'[Message] Cannot read the conversation removal {message_id} for account {account.id}: {body[:200]!r}')
                return
            if self._own_conversation_removes.is_echo(canonical_uri(address, account), None, None):
                # our own removal fanned back by the server: already applied here
                log.debug(f'Ignoring the echo of our conversation removal for {address}')
                return
            payload = parse_payload(body) or {}
            contact, contact_uri = URIUtils.find_contact(address)
            try:
                if payload.get('timestamp'):
                    timestamp = ISOTimestamp(payload['timestamp'])    # when it was removed
                elif cpim_message is not None and cpim_message.timestamp is not None:
                    timestamp = ISOTimestamp(cpim_message.timestamp)  # a bare address: when it was sent
                else:
                    timestamp = ISOTimestamp.now()
            except (ValueError, OverflowError):
                timestamp = ISOTimestamp.now()
            ActivityLog().info(f'[Message with {contact_uri.uri}] Conversation removed on another device, messages up to {timestamp} for account {account.id}')
            try:
                blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)
            except StopIteration:
                notification_center.post_notification('BlinkGotHistoryConversationRemove', sender=account, data=NotificationData(contact=contact_uri.uri, timestamp=timestamp))
            else:
                # If the session switches account after we removed, we could be also deleting from another account
                if str(blink_session.account.uri) == str(to_header.uri):
                    NotificationCenter().post_notification('BlinkConversationWillRemove', sender=blink_session, data=NotificationData(contact=blink_session.contact_uri.uri, timestamp=timestamp))
                else:
                    log.info(f'Conversation remove is not for session account: {blink_session.account.id}')
            return

        timestamp = cpim_message.timestamp if cpim_message is not None and cpim_message.timestamp is not None else ISOTimestamp.now()
        if timestamp.tzinfo is tzutc():
            timestamp = timestamp.replace(tzinfo=timezone.utc).astimezone(tzlocal())
        timestamp = str(timestamp)
        message = BlinkMessage(body, content_type, sender, timestamp=timestamp, id=message_id, disposition=disposition, direction='incoming', metadata=metadata)

        if x_replicated_message is not Null:
            message.sender = account
            message.direction = "outgoing"

        if content_type.lower() == CALL_CONTENT_TYPE:
            # a call another device of this account took part in: published from the account to itself
            party = to_header.uri if x_replicated_message is not Null else sender.uri
            party = f'{party.user.decode() if isinstance(party.user, bytes) else party.user}@{party.host.decode() if isinstance(party.host, bytes) else party.host}'
            self._take_call_record(account, body, metadata, party, message_id, 'replicated' if x_replicated_message is not Null else 'live')
            return

        # a live delivery report (message/imdn+xml) is not the journal's message/imdn: it updates a state below
        if journal_action(content_type) == 'inert' and content_type.lower() not in (FTHTTPDocument.content_type, IMDNDocument.content_type):
            # stored as it is and never unread, without opening a conversation: locations, metadata
            # companions, call records and types this version does not know (not shown, for now)
            remote_uri = contact_instance_id(contact, contact_uri) or contact.uri.uri
            notification_center.post_notification('BlinkGotHistoryMessage', sender=account,
                                                  data=NotificationData(remote_uri=remote_uri, message=message, encryption=encryption, state='accepted'))
            known = content_type.lower() in KNOWN_INERT_CONTENT_TYPES
            link = metadata_link(body) if content_type.lower() == METADATA_CONTENT_TYPE else None
            tick = location_storage_fields(body, metadata) if content_type.lower() == LOCATION_CONTENT_TYPE else {}
            if x_replicated_message is Null and tick.get('related_action') == 'location_request' and 'positive-delivery' in (disposition or ()):
                self._confirm_location_request(account, contact, message)
            if link is not None:
                what = f': {link[1]} for message {link[0]}'
            elif tick:
                what = f': {tick["related_action"]} of share {tick.get("related_msg_id")}'
            elif content_type.lower() == LOCATION_CONTENT_TYPE:
                what = ': unreadable without decrypting'
            else:
                what = '' if known else ' (a type this version does not know)'
            ActivityLog().info(f'[Message with {remote_uri}] {content_type.lower()} message {message_id} stored, not shown{what}')
            return

        try:
            blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings or session.contact_uri.uri == contact_uri.uri or (instance_id and instance_id == session.remote_instance_id))
        except StopIteration:
            blink_session = None
            if any(content_type.lower().startswith(prefix) for prefix in self.__ignored_content_types__):
                log.debug(f"Not creating session for incoming message for content type {content_type.lower()}")
                if content_type.lower() != IMDNDocument.content_type:
                    return
            elif x_replicated_message is not Null:
                #log.debug("Not creating session for incoming message, message is replicated")
                #notification_center.post_notification('BlinkGotHistoryMessage',
                #                                      sender=account,
                #                                      data=NotificationData(remote_uri=contact.uri.uri,
                #                                                            message=message,
                #                                                           encryption=encryption,
                #                                                            state='accepted'))
                #return
                if instance_id:
                    log.info(f"Create incoming message {content_type.lower()} view for account {account.id} to instance_id {instance_id}")
                else:
                    log.info(f"Create incoming message {content_type.lower()} view for account {account.id} to {contact_uri.uri}")
                ActivityLog().info(f'[Message with {contact_uri.uri}] Conversation opened for account {account.id} by a message sent from another device')
                blink_session = session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False)
                blink_session.direction = 'outgoing'
            else:
                if instance_id:
                    log.info(f"Create incoming message {content_type.lower()} view for account {account.id} to instance_id {instance_id}")
                else:
                    log.info(f"Create incoming message {content_type.lower()} view for account {account.id} to {contact_uri.uri}")

                ActivityLog().info(f'[Message with {instance_id or contact_uri.uri}] Conversation opened for account {account.id} by an incoming message')
                blink_session = session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False, remote_instance_id=instance_id)
                # TODO session should have direction incoming, right now there is no way to create it without an event. We set the direction manually. -- Tijmen
                blink_session.direction = 'incoming'
        else:
            if blink_session.fake_streams.get('messages') is None:
                stream = StreamDescription('messages')
                blink_session.fake_streams.extend([stream.create_stream()])
                blink_session._delete_when_done = False
                if account.sms.enable_pgp and account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized):
                    blink_session.fake_streams.get('messages').enable_pgp()
                notification_center.post_notification('BlinkSessionWillAddStream', sender=blink_session, data=NotificationData(stream=stream))

        # no session for a delivery report of a conversation not open here (a Bonjour neighbour's, say): none is needed
        if blink_session is not None:
            if not blink_session.fake_streams.get('messages').can_decrypt_with_others:
                blink_session.fake_streams.get('messages').enable_pgp()

            if account.sms.enable_pgp and (account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized)) and account is BonjourAccount():
                stream = blink_session.fake_streams.get('messages')
                stream.generate_keys()

        if account.sms.use_cpim and account.sms.enable_imdn and content_type.lower() == IMDNDocument.content_type:
            # print("-- IMDN received")
            document = IMDNDocument.parse(body)
            imdn_message_id = document.message_id.value
            imdn_status = document.notification.status.__str__()
            imdn_datetime = document.datetime.__str__()
            log.info(f'Disposition {imdn_status} of message {imdn_message_id} received from {_bare(sender.uri)} for account {account.id} (IMDN {message_id}){via}')
            from blink.history import MessageHistory
            MessageHistory().record_agent(imdn_message_id, imdn_status, user_agent, relay)
            notification_center.post_notification('BlinkGotDispositionNotification', sender=blink_session if blink_session is not None else account, data=NotificationData(id=imdn_message_id, status=imdn_status))
            return
        elif content_type.lower() == IMDNDocument.content_type:
            # print("-- IMDN received, ignored")
            return

        if content_type.lower() == IsComposingDocument.content_type and x_replicated_message is Null:
            try:
                document = IsComposingMessage.parse(body)
            except ParserError as e:
                log.warning('Failed to parse Is-Composing payload: %s' % str(e))
            else:
                data = NotificationData(state=document.state.value,
                                        refresh=document.refresh.value if document.refresh is not None else 120,
                                        content_type=document.content_type.value if document.content_type is not None else None,
                                        last_active=document.last_active.value if document.last_active is not None else None,
                                        sender=sender)
                notification_center.post_notification('BlinkGotComposingIndication', sender=blink_session, data=data)
            return

        if x_replicated_message is not Null and message_id in self._own_upload_ids and content_type.lower() in FILE_TRANSFER_CONTENT_TYPES:
            # the server's message for a file this device uploaded: it completes the local copy
            # (its URL and expiry), it is not a new message
            notification_center.post_notification('BlinkGotHistoryMessage', sender=account,
                                                  data=NotificationData(remote_uri=contact.uri.uri, message=message, state='accepted',
                                                                        encryption='OpenPGP' if body and '.asc' in body else None))
            return

        if content_type.lower() == FTHTTPDocument.content_type:
            log.info("Messsge is a filetransfer message")
            try:
                document = FTHTTPDocument.parse(body)
            except ParserError as e:
                log.warning('Failed to parse FT HTTP payload: %s' % str(e))
            else:
                for info in document:
                    try:
                        until = document['until']
                    except KeyError:
                        until = ISOTimestamp(datetime.now() + timedelta(days=30))

                    try:
                        hash = info.hash.value
                    except AttributeError:
                        hash = None

                    file = File(info.file_name.value,
                                info.file_size.value,
                                contact,
                                hash,
                                message_id,
                                until,
                                info.data.url,
                                account=account,
                                protocol='sylk')

                    message.is_secure = info.file_name.value.endswith('.asc')

                    notification_center.post_notification('BlinkGotMessage',
                                                          sender=blink_session,
                                                          data=NotificationData(message=message, account=account))

                    history_message_data = NotificationData(remote_uri=contact.uri.uri,
                                                            message=message,
                                                            state='accepted',
                                                            encryption='OpenPGP' if file.encrypted else None)

                    notification_center.post_notification('BlinkGotHistoryMessage', sender=account, data=history_message_data)

                    notification.center.post_notification('BlinkSessionDidShareFile',
                                                          sender=blink_session,
                                                          data=NotificationData(file=file, direction=message.direction))
            return

        # text from here on: every other content type was handled, stored inert or dropped above

        if encryption is None and not x_replicated_message:
            otr = blink_session.fake_streams.get('messages').check_otr(message)
            if otr is not None:
                message = otr
            else:
                return

        if message.content.startswith("?OTR:") and x_replicated_message:
            log.warning('Incoming message skipped, OTR encrypted, it should be handled [BUG]')
            return

        if message.content.startswith("?OTRv3?") and x_replicated_message:
            return

        if x_replicated_message or account is not blink_session.account:
            history_message_data = NotificationData(remote_uri=contact.uri.uri,
                                                    message=message,
                                                    encryption=encryption,
                                                    state='accepted')
            notification_center.post_notification('BlinkGotHistoryMessage', sender=account, data=history_message_data)

        if encryption == 'OpenPGP':
            if account.sms.enable_pgp and (account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized)):
                self._incoming_encrypted_message_queue.append((message, account, contact))
                if account is blink_session.account:
                    notification_center.post_notification('BlinkMessageIsParsed', sender=blink_session, data=message)
                data = NotificationData(message=message,
                                        account=account,
                                        replicated_message=x_replicated_message)
                notification_center.post_notification('BlinkGotMessage',
                                                      sender=blink_session,
                                                      data=data)
            else:
                blink_session.fake_streams.get('messages').decrypt(message)
            return

        self._handle_incoming_message(message, blink_session, account)

    def _NH_BlinkJournalFirstSyncDidFinish(self, notification):
        account_id = notification.data.account_id
        try:
            account = AccountManager().get_account(account_id)
        except KeyError:
            return
        self._fetch_public_keys_once(account)
        self.announce_activation(account)

    def _NH_BlinkJournalProgress(self, notification):
        account = notification.sender
        if notification.data.phase == 'done' and getattr(account, 'id', None) in self._announce_after_sync:
            self._announce_after_sync.discard(account.id)
            call_in_gui_thread(self.announce_activation, account)

    def announce_activation(self, account):
        """Tell the account's other devices that this one has joined: a plain-text note to
        ourselves, which the journal replicates to every other device, as Sylk Mobile sends from
        afterFirstSync: "Account activated on <user agent>". Once per account, after the first
        sync, so it lands below the history this device has just read."""
        if account is BonjourAccount() or not account.enabled or account.sms.activation_announced:
            return
        # recorded before sending: a send that fails is not retried, but a success that was not
        # recorded would announce this device again at every start
        account.sms.activation_announced = True
        account.save()
        text = f'Account activated on {SIPSimpleSettings().user_agent}'
        ActivityLog().info(f'[Message with {account.id}] Announcing the activation of {account.id} to its other devices: {text}')
        # through the conversation with ourselves, so it is stored here like any message we send
        blink_session = self.create_message_session(str(account.uri), selected=False)
        if blink_session is None:
            return
        self.send_message(account, blink_session.contact, text, 'text/plain')

    # The public keys of all the contacts, once per account after its first sync

    public_keys_marker = 'public-keys.done'
    public_key_lookup_interval = 300        # ms between two lookups, not to flood the proxy

    @run_in_gui_thread
    def _fetch_public_keys_once(self, account):
        """Ask the server for the public key of every addressbook contact this device has
        none of (a sylk-api-pgp-key-lookup each; the server answers with the key as a
        text/pgp-public-key message, saved as any other). Once per account, after its first
        sync: journal/<account>/public-keys.done. Contacts added later get their key as
        before, with the first message."""
        if account is BonjourAccount() or not account.enabled or not account.sms.enable_pgp:
            return
        directory = self._journal_directory(account)
        done = os.path.join(directory, self.public_keys_marker)
        if os.path.exists(done) or os.path.exists(os.path.join(directory, FIRST_SYNC_MARKER)) or self._journal_snapshot_pending(account):
            return      # done, or asked for when the whole first sync is in (its older pages too)
        account_manager = AccountManager()
        addresses = set()
        for contact in AddressbookManager().get_contacts():
            try:
                uris = list(contact.uris)
            except (AttributeError, TypeError):
                continue
            for contact_uri in uris:
                address = str(getattr(contact_uri, 'uri', '') or '').strip()
                for scheme in ('sips:', 'sip:'):
                    if address.lower().startswith(scheme):
                        address = address[len(scheme):]
                address = address.split(';', 1)[0].split('?', 1)[0]
                user, at, domain = address.partition('@')
                if not at or not user or not domain or account_manager.has_account(address):
                    continue
                addresses.add(f'{user}@{domain.lower()}')
        keys_directory = SIPSimpleSettings().chat.keys_directory.normalized
        missing = sorted(address for address in addresses if not os.path.exists(os.path.join(keys_directory, address.replace('/', '_') + '.pubkey')))
        try:
            with open(done, 'w') as marker_file:
                marker_file.write(f'{ISOTimestamp.now()} {len(missing)} of {len(addresses)} asked for\n')
        except OSError as e:
            ActivityLog().warning(f'[pgp] Cannot write {done}: {e}')
            return
        ActivityLog().info(f'[pgp] Asking the server for the public keys of {len(missing)} contacts of {account.id} ({len(addresses) - len(missing)} of {len(addresses)} already known)')
        JournalLog()(account.id, 'public keys', asked=len(missing), known=len(addresses) - len(missing))
        from PyQt6.QtCore import QTimer
        self._bulk_key_lookups.update(address.lower() for address in missing)
        for number, address in enumerate(missing):
            QTimer.singleShot(number * self.public_key_lookup_interval, lambda address=address: self._lookup_public_key(account, address))
        # one line when the answers are in, instead of two per key
        call_later(len(missing) * self.public_key_lookup_interval / 1000 + 15, self._report_bulk_keys, account, len(missing))

    def _quiet_public_key(self, account, address):
        """Whether a received or saved public key is logged at debug level only: during the first
        journal sync of the account, or as an answer to the bulk lookup that follows it."""
        address = str(address or '').lower().removeprefix('sip:')
        if address in self._bulk_key_lookups:
            return True
        if account is not None and account is not BonjourAccount():
            from blink.history import first_sync_pending
            return first_sync_pending(account.id)
        return False

    def _report_bulk_keys(self, account, asked):
        saved, self._bulk_keys_saved = self._bulk_keys_saved, 0
        self._bulk_key_lookups.clear()
        ActivityLog().info(f'[pgp] Public keys of contacts of {account.id}: {saved} of {asked} asked for received and saved')

    def _lookup_public_key(self, account, address):
        if not account.enabled:
            return
        from types import SimpleNamespace
        target = SimpleNamespace(uri=SimpleNamespace(uri=address))     # no session: OutgoingMessage only needs the address
        log.info(f'Public key lookup for {address} from {account.id}')
        OutgoingMessage(account, target, 'Public key request', 'application/sylk-api-pgp-key-lookup').send()

    def _NH_BlinkServerHistoryWasFetched(self, notification):
        account = notification.sender
        messages = notification.data['messages']
        self._process_server_history_messages(account, messages)

    def _NH_BlinkSessionWasCreated(self, notification):
        session = notification.sender
        self.sessions.append(session)

    def _NH_BlinkSessionWasDeleted(self, notification):
        session = notification.sender
        self.sessions.remove(session)
        for request in self.pgp_requests[session.account, GeneratePGPKeyRequest]:
            request.dialog.hide()
            self.pgp_requests.remove(request)

    def _NH_BlinkSessionWasLoaded(self, notification):
        session = notification.sender
        stream = session.fake_streams.get('messages')

        if stream is None:
            return

        if session.account.sms.enable_pgp and (session.account.sms.private_key is None or not os.path.exists(session.account.sms.private_key.normalized)):
            for request in self.pgp_requests[session.account, GeneratePGPKeyRequest]:
                return

            if session.account is BonjourAccount():
                session = session
                stream = session.fake_streams.get('messages')
                stream.generate_keys()
                return

            self._offer_key_generation(session.account, 1, session)

        elif session.account.sms.enable_pgp and not stream.can_decrypt_with_others:
            stream.enable_pgp()

    def _NH_PGPKeysShouldReload(self, notification):
        session = notification.sender
        stream = session.fake_streams.get('messages')

        if stream is None:
            return

        if session.account.sms.enable_pgp and (session.account.sms.private_key is None or not os.path.exists(session.account.sms.private_key.normalized)):
            for request in self.pgp_requests[session.account, GeneratePGPKeyRequest]:
                return

            if session.account is BonjourAccount():
                session = session
                stream = session.fake_streams.get('messages')
                stream.generate_keys()
                return

            self._offer_key_generation(session.account, 1, session)


    def _NH_PGPKeysDidGenerate(self, notification):
        session = notification.sender
        try:
            key_id = notification.data.private_key.fingerprint.keyid
        except AttributeError:
            key_id = 'unknown'
        ActivityLog().info(f'[pgp] Generated a new PGP key {key_id} for account {session.account.id}')

        outgoing_message = OutgoingMessage(session.account, session.contact, str(notification.data.public_key), 'text/pgp-public-key', session=session)
        self._send_message(outgoing_message)

    def _NH_PGPMessageDidDecrypt(self, notification):
        if not isinstance(notification.data.message, BlinkMessage):
            return

        session = notification.sender
        message = notification.data.message
        message.is_secure = True

        notification_center = NotificationCenter()
        if message.id in self._journal_decrypts:
            # from a first sync: the plaintext is stored, the bubble redrawn; no receipt, no notification, no unread
            self._journal_decrypts.discard(message.id)
            from blink.history import MessageHistory
            MessageHistory().update_decrypted_message(message.id, message.content)
            notification_center.post_notification('BlinkMessageDidDecrypt', sender=session, data=NotificationData(message=message))
            return
        notification_center.post_notification('BlinkMessageDidDecrypt', sender=session, data=NotificationData(message=message))
        self._handle_incoming_message(message, session, notification.data.account)

    def _NH_PGPMessageDidNotDecrypt(self, notification):
        session = notification.sender
        message = notification.data.message

        try:
            msg_id = message.message_id
        except AttributeError:
            msg_id = message.id

        notification.data.message.is_secure = True
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidNotDecrypt', sender=session, data=NotificationData(message=message, error=notification.data.error))

        if message.direction == 'outgoing':
            return

        if msg_id in self._journal_decrypts:
            self._journal_decrypts.discard(msg_id)
            return      # from a first sync: its sender is not told now

        self.send_imdn_message(session, msg_id, message.timestamp, 'error')

    def _NH_BlinkMessageHistoryFailedLocalFound(self, notification):
        log.info('Resending unsent messages...')
        messages = notification.data.messages
        created_views = {}      # uri -> the session made for it in this loop, for its other messages
        sent = failed = 0
        for message in messages:
            try:
                if self._resend_unsent(message, created_views):
                    sent += 1
            except Exception as e:
                # one message that cannot be sent (a bad address, a gone account) must not keep the others back
                failed += 1
                ActivityLog().error(f'[Message with {message.remote_uri}] Unsent message {message.message_id} cannot be sent again: {e!r}')
        if sent or failed:
            ActivityLog().info(f'[Message] Sending again {sent} unsent messages' + (f', {failed} could not be' if failed else ''))

    def _resend_unsent(self, message, created_views):
        """Send one unsent (failed-local) message from history again. True if it was handed to be sent."""
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(message.remote_uri)

        if placeholder_instance_id(contact_uri.uri):
            return False  # a Bonjour neighbour who is away: retried when it is back

        session_manager = SessionManager()
        account = AccountManager().get_account(message.account_id)

        instance_id = contact_instance_id(contact, contact_uri)

        try:
            # a view made in this loop for an earlier message of this contact may not be in
            # self.sessions yet: use it rather than making a second one
            blink_session = created_views.get(contact_uri.uri) or next(session for session in self.sessions if session.contact_uri.uri == contact_uri.uri or (instance_id and instance_id == session.remote_instance_id))
        except StopIteration:
            log.info(f"Create message view from history for {contact_uri.uri} with instance_id {instance_id}")
            ActivityLog().info(f'[Message with {instance_id or contact_uri.uri}] Conversation opened from message history for account {account.id}')
            try:
                ab_contact = next(contact for contact in AddressbookManager().get_contacts() if contact_uri.uri in (addr.uri for addr in contact.uris))
            except StopIteration:
                pass
            else:
                contact.settings.name = ab_contact.name
            blink_session = session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False)
            created_views[contact_uri.uri] = blink_session
        else:
            if blink_session.fake_streams.get('messages') is None:
                stream = StreamDescription('messages')
                blink_session.fake_streams.extend([stream.create_stream()])
                blink_session._delete_when_done = False
                if account.sms.enable_pgp and account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized):
                    blink_session.fake_streams.get('messages').enable_pgp()
                NotificationCenter().post_notification('BlinkSessionWillAddStream', sender=blink_session, data=NotificationData(stream=stream))

            if not blink_session.fake_streams.get('messages').can_decrypt_with_others:
                blink_session.fake_streams.get('messages').enable_pgp()

        timestamp = message.timestamp.replace(tzinfo=timezone.utc).astimezone(tzlocal())
        outgoing_message = OutgoingMessage(account, contact, message.content, message.content_type, timestamp=timestamp, id=message.message_id, session=blink_session)
        self._send_message(outgoing_message)
        return True

    def generate_private_key(self, account):
        if account is None:
            return

        """The user chose Generate private key: the current key (if any) is kept aside as
        -<timestamp>-old, and a new one is made at once."""
        timestamp = datetime.now().strftime('%Y%m%d%H%M')
        settings = SIPSimpleSettings()
        directory = os.path.join(settings.chat.keys_directory.normalized, 'private')
        filename = os.path.join(directory, account.id)
        archived = False
        for extension in ('privkey', 'pubkey'):
            if os.path.exists(f'{filename}.{extension}'):
                os.rename(f'{filename}.{extension}', f'{filename}-{timestamp}-old.{extension}')
                archived = True
        account.sms.public_key = None
        account.sms.private_key = None
        account.save()
        ActivityLog().info(f'[pgp] Generating a new PGP key for account {account.id}, as the user chose from the menu'
                           + (f' (the current key kept as {account.id}-{timestamp}-old)' if archived else ' (none on this device)'))
        for request in list(self.pgp_requests[account, GeneratePGPKeyRequest]):
            request.dialog.hide()
            self.pgp_requests.remove(request)
        self._generate_keys(account)

    def export_private_key(self, account):
        if account is None:
            return

        for request in self.pgp_requests[account, ExportPrivateKeyRequest]:
            request.dialog.hide()
            self.pgp_requests.remove(request)

        export_dialog = ExportDialog()
        export_request = ExportPrivateKeyRequest(export_dialog, account)
        export_request.accepted.connect(self._SH_ExportPGPKeys)
        export_request.finished.connect(self._SH_PGPRequestFinished)
        bisect.insort_right(self.pgp_requests, export_request)
        export_request.dialog.show()

    def send_otr_message(self, session, data):
        outgoing_message = InternalOTROutgoingMessage(session.account, session.contact, data, 'text/plain', session=session)
        self._send_message(outgoing_message)

    def send_composing_indication(self, session, state, refresh=60, last_active=None):
        """Refresh 60 and last-active now, as Blink for macOS sends them: a receiver that
        hears nothing more drops "typing" after a minute rather than the RFC default 120 s."""
        if not session.account.sms.enable_iscomposing:
            return
        if last_active is None:
            last_active = ISOTimestamp.now()

        content = IsComposingDocument.create(state=State(state),
                                             refresh=Refresh(refresh) if refresh is not None else None,
                                             last_active=LastActive(last_active) if last_active is not None else None,
                                             content_type=ContentType('text'))

        outgoing_message = OutgoingMessage(session.account, session.contact, content, IsComposingDocument.content_type, session=session)
        self._send_message(outgoing_message)

    def send_remove_message(self, session, id, account=None):
        if (session.account if account is None else account) is BonjourAccount():
            return  # no server behind a link-local network
        outgoing_message = OutgoingMessage(session.account if account is None else account, session.contact, id, 'application/sylk-api-message-remove', session=session, use_cpim=False)
        self._send_message(outgoing_message)

    def send_message_remove(self, account, contact_uri, message_id):
        """Remove one message on the server and the account's other devices, as a delete in the conversation
        does, without opening a conversation for it. Returns whether it was sent."""
        if account is BonjourAccount() or not account.enabled or not message_id or not contact_uri:
            return False
        from blink.contacts import URIUtils
        contact, _ = URIUtils.find_contact(contact_uri)
        if contact is None:
            return False
        self._send_message(OutgoingMessage(account, contact, message_id, 'application/sylk-api-message-remove', use_cpim=False, sessionless=True))
        return True

    def send_conversation_read(self, session):
        """Tell this account's other devices the conversation was read here. Sent to the
        server API from the account to itself; the server replicates it to every device
        as application/sylk-conversation-read, this one included (the echo is ignored)."""
        account = session.account
        if account is BonjourAccount():
            return  # no server behind a link-local network
        contact = str(session.contact.uri.uri)
        content = conversation_read_envelope(contact, this_device_id())
        self._own_conversation_reads.note(canonical_uri(contact, account))
        ActivityLog().info(f'[Message with {contact}] Announcing that the conversation was read: {content}')
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(session.account.uri)
        outgoing_message = OutgoingMessage(session.account, contact, content, 'application/sylk-api-conversation-read', session=session, use_cpim=False)
        self._send_message(outgoing_message)

    def remove_conversation(self, blink_session):
        """Remove a conversation here, under every account it was filed under, and through the server
        on the other devices of each SIP account that had messages in it."""
        peer = blink_session.remote_instance_id or str(blink_session.contact_uri.uri)
        where = 'on this computer' if blink_session.remote_instance_id else 'on all devices'
        ActivityLog().info(f'[Message with {peer}] Removing the conversation {where}')
        self._removing_conversations[peer] = blink_session
        NotificationCenter().post_notification('BlinkConversationWillRemove', sender=blink_session, data=NotificationData(contact=blink_session.contact_uri.uri, timestamp=ISOTimestamp.now(), all_accounts=True))

    def _NH_BlinkMessageHistoryConversationDidRemove(self, notification):
        blink_session = self._removing_conversations.pop(notification.data.contact, None)
        if blink_session is None or blink_session.remote_instance_id:
            return  # not removed by the user here, or a Bonjour neighbour: no server to tell
        account_manager = AccountManager()
        accounts = [account_manager.get_account(account_id) for account_id in notification.data.accounts if account_manager.has_account(account_id)]
        if blink_session.account not in accounts:
            accounts.append(blink_session.account)  # the server may hold messages not synced here yet
        for account in accounts:
            if account is BonjourAccount() or not account.enabled:
                continue
            self.send_conversation_remove(blink_session, account=account)

    def send_messages_bulk_delete(self, account, payload):
        """Ask the other devices of the account to erase what a Data manager deletion here erased
        (docs/messaging/messages-bulk-delete.md). Sent to the server API from the account to itself:
        the server keeps it in the journal and sends it to every device, this one included (skipped
        by its device id)."""
        if account is BonjourAccount() or not account.enabled:
            return False
        from blink.contacts import URIUtils
        from blink.data_manage import criteria_label
        ActivityLog().info(f'[data] Announcing the bulk delete {payload["bulk_delete_id"]} ({criteria_label(payload["criteria"])}) to the other devices of {account.id}')
        contact, contact_uri = URIUtils.find_contact(account.uri)
        # no conversation (session) carries it: sent straight to the account's proxy, or OutgoingMessage.send drops it
        self._send_message(OutgoingMessage(account, contact, json.dumps(payload), API_MESSAGES_BULK_DELETE_CONTENT_TYPE, use_cpim=False, sessionless=True))
        return True

    # Data export and import between devices of the account (blink.data_import, blink.data_export)

    def handle_data_export_announcement(self, account, body, sender_uri):
        """application/sylk-data-export from another device of ours: where its export server is. Acted on
        only when fresh (a journal replay is not), and never for an export running here."""
        from blink import data_export, data_import
        # SIP headers carry bytes, CPIM headers str
        user, host = (part.decode(errors='replace') if isinstance(part, bytes) else part for part in (sender_uri.user, sender_uri.host))
        sender = f'{user}@{host}'
        if sender != str(account.id):
            ActivityLog().warning(f'[import] Ignoring a data export announcement for {account.id} sent by {sender}')
            return
        text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body or '')
        if '-----BEGIN PGP MESSAGE-----' in text:
            text = self._decrypt_to_own_key(account, text)
            if text is None:
                ActivityLog().info(f'[import] Cannot decrypt the data export announcement for {account.id}')
                return
        announcement = data_import.parse_announcement(text)
        if announcement is None:
            ActivityLog().info(f'[import] Unreadable data export announcement for {account.id}')
            return
        if announcement['key'] in data_export.ACTIVE_TOKENS:
            return      # our own export, relayed back to us
        if not data_import.is_fresh(announcement['timestamp']):
            ActivityLog().info(f'[import] Ignoring a stale data export announcement from {announcement["server"]}')
            return
        ActivityLog().info(f'[import] {account.id} is exporting its data at {announcement["server"]} ({"encrypted" if announcement["enc"] else "not encrypted"})')
        NotificationCenter().post_notification('BlinkDataExportWasAnnounced', sender=account, data=NotificationData(announcement=announcement))

    @staticmethod
    def _decrypt_to_own_key(account, text):
        if account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized):
            return None
        try:
            key, _ = pgpy.PGPKey.from_file(account.sms.private_key.normalized)
            plain = key.decrypt(PGPMessage.from_blob(text)).message
        except Exception as e:
            log.warning(f'Cannot decrypt a message to our own key of {account.id}: {e!r}')
            return None
        return plain.decode('utf-8', 'replace') if isinstance(plain, (bytes, bytearray)) else str(plain)

    def send_data_export_announcement(self, account, payload):
        """Tell the other devices of the account where this device's export is (Data manager > Export to device):
        the payload PGP-encrypted to the account's own public key, to the account itself, not journalled (a
        replay would be stale by the time anyone read it). Returns whether it was sent."""
        public_key = self._own_public_key(account)
        if public_key is None:
            ActivityLog().warning(f'[export] Cannot announce the export of {account.id}: no PGP public key of our own')
            return False
        content = self._seal(public_key, payload)
        if content is None:
            return False
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(account.uri)
        ActivityLog().info(f'[export] Announcing the export to the other devices of {account.id}')
        self._send_message(OutgoingMessage(account, contact, content, DATA_EXPORT_CONTENT_TYPE, use_cpim=False, skip_journal=True, sessionless=True))
        return True

    @run_in_thread('sync')
    def apply_imported_messages(self, account, messages, finished=None):
        """Store rows imported from another device the way journal entries are stored (same handlers, same
        duplicate check), in the sync thread so an import never interleaves with a journal run. finished(outcomes)."""
        outcomes = {}
        try:
            outcomes = self._apply_server_history_messages(account, messages, mark_synced=False)
        except JournalInterrupted:
            pass
        except Exception as e:
            ActivityLog().error(f'[import] Storing {len(messages)} imported messages of {account.id} failed: {e!r}')
        if finished is not None:
            finished(outcomes)

    def imported_messages_done(self, account):
        """After an import: unread counts, the contact list and background decryption follow, as after a journal run."""
        NotificationCenter().post_notification('BlinkJournalDidApply', sender=account,
                                               data=NotificationData(new_messages={}, stats_path=None, first_sync=False, first_sync_marker=None))

    def apply_messages_bulk_delete(self, account, body, source):
        """Erase what a bulk delete from another device of the account asks for. Returns the journal outcome."""
        from blink.data_manage import bulk_delete_criteria, criteria_label
        from blink.history import MessageHistory
        payload = parse_payload(body)
        criteria = bulk_delete_criteria(payload)
        if criteria is None:
            ActivityLog().warning(f'[data] Bulk delete for {account.id} ({source}) ignored, its criteria are missing or too broad: {str(body)[:300]!r}')
            return 'failed'
        if payload.get('device_id') == this_device_id():
            log.debug(f'Ignoring our own bulk delete {payload.get("bulk_delete_id")}')
            return 'own bulk deletes'
        device = payload.get('device_id') or 'another device'
        MessageHistory().bulk_delete(str(account.id), criteria, files=payload.get('files', True) is not False,
                                     source=f'on request of {device}, {source}', label=criteria_label(criteria), cascade=payload.get('cascade') is True)
        return 'bulk deletes'

    def send_conversation_remove(self, session, account=None):
        account = session.account if account is None else account
        if account is BonjourAccount():
            return  # no server behind a link-local network
        contact = str(session.contact.uri.uri)
        ActivityLog().info(f'[Message with {contact}] Asking the server to remove the conversation from the other devices of {account.id}')
        payload = {'contact': contact, 'timestamp': str(ISOTimestamp.now())}
        content = json.dumps(payload)
        self._own_conversation_removes.note(canonical_uri(contact, account))
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(account.uri)
        outgoing_message = OutgoingMessage(account, contact, content, 'application/sylk-api-conversation-remove', session=session, use_cpim=False)
        self._send_message(outgoing_message)

    def send_addressbook_update(self, account, content):
        """Tell the other devices of this account that the addressbook changed. To our own
        account, never journalled, no CPIM: it is a notice, not a message."""
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(account.uri)
        self._send_message(OutgoingMessage(account, contact, content, ADDRESSBOOK_UPDATE_CONTENT_TYPE, use_cpim=False, skip_journal=True))

    def announce_conversation_removal(self, keys):
        """Ask the server to remove these conversations from the other devices of every
        account that replicates (a contact deleted permanently here). The echo the
        server sends back is ignored (OwnMarkers)."""
        keys = [str(key) for key in keys if key]
        if not keys:
            return
        from blink.contacts import URIUtils
        for account in AccountManager().get_accounts():
            if account is BonjourAccount() or not account.enabled:
                continue
            for key in keys:
                if key == str(account.id).lower():
                    continue
                ActivityLog().info(f'[Message with {key}] Asking the server to remove the conversation from the other devices of {account.id}')
                content = json.dumps({'contact': key, 'timestamp': str(ISOTimestamp.now())})
                self._own_conversation_removes.note(canonical_uri(key, account))
                contact, contact_uri = URIUtils.find_contact(account.uri)
                self._send_message(OutgoingMessage(account, contact, content, 'application/sylk-api-conversation-remove', use_cpim=False, sessionless=True))

    def _confirm_location_request(self, account, contact, message, open_only=False):
        """Delivered, for a location request that asked for it: it is stored without opening a
        conversation, so through the conversation when one is open, else straight to the proxy.
        From the journal (open_only) only through an open conversation, as for a text."""
        session = self._journal_session(contact)
        if session is not None:
            self.send_imdn_message(session, message.id, message.timestamp, 'delivered', account)
            return
        if open_only or account is BonjourAccount() or not account.sms.use_cpim or not account.sms.enable_imdn or host.default_ip is None:
            return
        if self._before_first_sync(account, message.id, message.timestamp, 'delivered'):
            return
        content = IMDNDocument.create(message_id=message.id, datetime=message.timestamp, recipient_uri=contact.uri.uri,
                                      notification=DeliveryNotification('delivered'))
        self._send_message(OutgoingMessage(account, contact, content, IMDNDocument.content_type, sessionless=True))

    def _before_first_sync(self, account, message_id, timestamp, state):
        """A device never sends a receipt for a message older than the recent days of its first sync
        of the account (journal_recent_days before it started): the older history is archive, its
        senders were answered by the other devices. A recent message read or received here is
        confirmed as on any device."""
        if account is None or account is BonjourAccount():
            return False
        from blink.history import first_sync_date
        started = first_sync_date(account.id)
        if started is None:
            return False
        since = started - timedelta(days=self.journal_recent_days)
        try:
            when = timestamp if isinstance(timestamp, datetime) else ISOTimestamp(timestamp)
            older = when < since
        except Exception:
            return False
        if older:
            log.debug(f'Message {message_id} imdn {state} not sent: the message ({timestamp}) is older than the first sync of {account.id} ({since})')
        return older

    def send_imdn_message(self, session, id, timestamp, state, account=None):
        if host.default_ip is None:
            return

        if account is None and not session.account.sms.use_cpim or not session.account.sms.enable_imdn:
            return

        if account is not None:
            if not account.sms.use_cpim or not account.sms.enable_imdn:
                return

        if self._before_first_sync(session.account if account is None else account, id, timestamp, state):
            return

        log.debug(f"Message {id} imdn sending: {state}")
        if state == 'delivered':
            notification = DeliveryNotification(state)
        elif state == 'displayed':
            notification = DisplayNotification(state)
        elif state == 'error':
            notification = DisplayNotification(state)

        content = IMDNDocument.create(message_id=id,
                                      datetime=timestamp,
                                      recipient_uri=session.contact.uri.uri,
                                      notification=notification)

        outgoing_message = OutgoingMessage(session.account if account is None else account, session.contact, content, IMDNDocument.content_type, session=session)
        self._send_message(outgoing_message)

    def send_message(self, account, contact, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None):
        blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)
        if blink_session.remote_instance_id and account is not BonjourAccount():
            # a Bonjour neighbour is only ever reached link-local, never through a SIP account's proxy
            log.warning(f'Message to Bonjour neighbour {blink_session.remote_instance_id} was about to be sent from {account.id}, using the Bonjour account')
            account = blink_session.account = BonjourAccount()
        blink_session.last_failed_reason = None
        blink_session.updateTimestamp()
        outgoing_message = OutgoingMessage(account, contact, content, content_type, recipients, courtesy_recipients, subject, timestamp, required, additional_headers, id, blink_session)
        self._send_message(outgoing_message)

    def create_message_session(self, uri, display_name=None, selected=True):
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(uri)
        session_manager = SessionManager()
        instance_id = contact_instance_id(contact, contact_uri)
        # a Bonjour neighbour is talked to from the Bonjour account
        account = BonjourAccount() if instance_id else AccountManager().default_account

        try:
            blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings or session.contact_uri.uri == contact_uri.uri or (instance_id and instance_id == session.remote_instance_id) or (contact.type == 'dummy' and uri in session.contact.uris))
        except StopIteration:
            log.info(f"Create message view from session for {contact_uri.uri} with instance_id {instance_id}")
            ActivityLog().info(f'[Message with {instance_id or contact_uri.uri}] Conversation opened by the user for account {account.id}')
            try:
                ab_contact = next(contact for contact in AddressbookManager().get_contacts() if contact_uri.uri in (addr.uri for addr in contact.uris))
            except StopIteration:
                pass
            else:
                if ab_contact.name != contact_uri.uri:
                    contact.settings.name = ab_contact.name
                elif display_name and display_name != contact_uri.uri:
                    contact.settings.name = display_name

            blink_session = session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False, remote_instance_id=instance_id)
        else:
            if instance_id and blink_session.account is not BonjourAccount():
                blink_session.account = BonjourAccount()
                NotificationCenter().post_notification('BlinkSessionMessageAccountChanged', sender=blink_session)
            if blink_session.fake_streams.get('messages') is None:
                blink_session.add_stream(StreamDescription('messages'))
                if blink_session.account.sms.enable_pgp:
                    blink_session.fake_streams.get('messages').enable_pgp()

        if selected:
            NotificationCenter().post_notification('BlinkSessionIsSelected', sender=blink_session)
        return blink_session


@implementer(IObserver)
class KeyEscrowManager(object, metaclass=Singleton):
    """The PGP key escrow on our own XCAP contact, as Blink for macOS keeps it (blink.key_escrow).

    On every addressbook reload, per account:
    - restore: no local private key and an escrow on our own contact -> adopt it
      (decrypted with the account password). Latched on success, and a failure is
      not repeated for the same escrow and password.
    - report what our own contact carries, when it changed.
    - repair: a local key and no escrow anywhere -> write one, once per session.
      With no contact carrying our own address there is nowhere to write it, so
      that contact is created first (as Sylk Mobile does), once per session.
      With nothing escrowed nobody's key is displaced.
    - re-encrypt: an escrow of OUR key that does not open with the account password
      (changed here, on the web or on another device) is written again, encrypted
      with the current password -- but only once the server has accepted that
      password: a registration succeeded with it. A password merely typed into the
      preferences may be a typo, and an escrow locked with it opens nowhere.
    - after a key is imported from another device (key_imported): repair and the
      password check at once, not on some later reload; an escrow of ANOTHER key is
      replaced only when the user says so, since it is that key's only server copy.
    Every other difference between the local key and an escrow is the user's call.
    Nothing written here is announced to the other devices: it is this device
    catching up with the document it was handed.

    The escrow the SylkServer account storage keeps (blink.key_storage) is one more
    escrow record (key_escrow.STORAGE_ID): the pass waits for the storage to answer
    (or fail), and runs again whenever what it keeps changes -- a key escrowed by
    another device is restored here without waiting for an addressbook reload.

    Generating a key waits for the answer (when_answered): the addressbook has
    loaded (or the account has no XCAP, or 15 seconds have passed) AND the server
    has answered a public key lookup for the account itself (or it failed).
    A key is generated without asking only when the addressbook was really
    fetched and carries no escrow, and the server said it keeps no public key
    (may_generate_silently); otherwise the user is asked.

    The lookup is the usual sylk-api-pgp-key-lookup, to our own address. SylkServer
    answers it with 200 when it keeps a public key (the key follows as a
    text/pgp-public-key message) and 404 "No Public Key" when it keeps none.
    Any other answer (an error, no answer in time) is not a "no".
    """

    answer_timeout = 15.0
    lookup_timeout = 40.0               # longer than a SIP transaction (32 s)
    no_key_reason = 'no public key'

    def __init__(self):
        self._started = False
        self.checked = set()            # accounts whose addressbook has answered
        self.restore_done = set()
        self.restore_failed = {}        # account id -> the escrow and password it failed against
        self.repaired = set()
        self.self_contact_created = set()
        self._first_asked = {}
        self._waiting = {}              # account id -> callbacks held until the answer
        self.server_key = {}            # account id -> 'pending' | 'present' | 'absent' | 'failed' | 'unsupported' (no SylkServer)
        self._lookups = {}              # account id -> the lookup in flight
        self.server_public_keys = {}    # account id -> the public key the server sent for us (armored)
        self._status_logged = set()
        self._status_asked = set()
        self.accepted_passwords = {}    # account id -> the password a registration last succeeded with
        self.password_changed = set()   # accounts whose password changed and has not been accepted yet
        self.password_checked = {}      # account id -> the escrow and password last checked (escrow_password_check)
        self.imported = set()           # accounts whose imported key has not been compared with the escrow yet
        self.storage_waiting = set()    # accounts whose escrow pass waits for the account storage

    def start(self):
        if not self._started:
            self._started = True
            notification_center = NotificationCenter()
            notification_center.add_observer(self, name='XCAPManagerDidReloadData')
            notification_center.add_observer(self, name='BlinkKeyStorageDidChange')
            notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
            notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
            # started from MessageManager, made while SIPApplicationDidStart is being handled:
            # the accounts are loaded, but that notification will not reach us
            call_in_gui_thread(call_later, 2, self._log_key_status_of_all)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    # The private key of every account: where it is, logged once at start

    server_key_wait = 5.0       # seconds for the key that follows the server's 200

    def _log_key_status_of_all(self):
        try:
            for account in AccountManager().iter_accounts():
                self.check_key_status(account)
        except Exception as e:
            ActivityLog().exception(f'[pgp] Checking the private key of the accounts failed: {e!r}')

    def check_key_status(self, account):
        """Log the account's key status once, when it is known (log_key_status). Asked at
        start, on each addressbook reload and on registration, whichever comes first."""
        if account.id in self._status_asked or not account.enabled:
            return
        if account is not BonjourAccount():
            from blink.sylk_discovery import has_sylkserver
            if not has_sylkserver(account):
                return      # no SylkServer for the domain: no PGP messaging, nothing to report (asked again later: discovery may find one)
        self._status_asked.add(account.id)
        if not account.sms.enable_pgp:
            ActivityLog().info(f'[pgp] Private key of {account.id}: PGP is disabled for the account')
            return
        ActivityLog().info(f'[pgp] Checking the private key of {account.id}')
        try:
            self.when_answered(account, lambda: self.log_key_status(account))
        except Exception as e:
            ActivityLog().exception(f'[pgp] Checking the private key of {account.id} failed: {e!r}')
        if account is not BonjourAccount() and (account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized)):
            # no key here: decided at start, not when the first encrypted message arrives. Generated
            # silently when nobody has one; otherwise the user is told to export it from the device
            # that has it (or to generate one, having lost them all)
            MessageManager()._offer_key_generation(account, 1)

    def server_public_key_received(self, account, data):
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode('utf-8', 'replace')
        first = account.id not in self.server_public_keys
        self.server_public_keys[account.id] = data
        if first and self.server_key.get(account.id) != 'present':
            self.server_key[account.id] = 'present'     # the key itself may arrive before the 200

    def log_key_status(self, account, waited=False):
        """Three lines: the key on this device, the escrow on our own XCAP contact and the
        public key the server keeps, each compared with this device's."""
        if account.id in self._status_logged:
            return
        if not waited and self.server_key.get(account.id) == 'present' and account.id not in self.server_public_keys:
            call_later(self.server_key_wait, self.log_key_status, account, True)     # the key follows the 200 as a message
            return
        self._status_logged.add(account.id)
        from blink.pgp_keyids import key_fingerprint
        activity = ActivityLog()
        try:
            local_fingerprint = None
            public_path = account.sms.public_key.normalized if account.sms.public_key is not None else None
            if public_path and os.path.exists(public_path):
                with open(public_path, 'rb') as file:
                    local_fingerprint = key_fingerprint(file.read())
            has_private = account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized)

            def compared(fingerprint):
                if fingerprint is None:
                    return 'unreadable key'
                if local_fingerprint is None:
                    return f'key {fingerprint}'
                return f'key {fingerprint}, ' + ('the same as this device' if fingerprint == local_fingerprint else 'DIFFERENT from this device')

            if has_private:
                here = f'private key present, key {local_fingerprint or "unreadable"} ({account.sms.private_key.normalized})'
            else:
                here = 'no private key' + (f' (public key {local_fingerprint} only)' if local_fingerprint else '')

            if account is BonjourAccount():
                activity.info(f'[pgp] Private key of {account.id}: this device: {here}')
                return

            if not account.xcap.enabled:
                escrow = 'the account has no XCAP addressbook'
            elif account.id not in self.checked:
                escrow = f'unknown, the addressbook was not fetched within {self.answer_timeout:.0f}s'
            else:
                record = key_escrow.read_self_keys(account, storage=False)
                contacts = key_escrow.self_contact_elements(account)
                if record is not None:
                    escrow = (f"present on contact {record.get('contact_id')} ({record.get('namespace')}), written {record.get('timestamp') or '?'} "
                              f"by \"{record.get('device') or '?'}\", {compared(key_fingerprint(record.get('public_key')))}"
                              + ('' if record.get('private_key') else ', WITHOUT the private key'))
                elif contacts:
                    escrow = f"none, on {len(contacts)} contact(s) with our address ({', '.join(str(element.get('id')) for element in contacts)})"
                else:
                    escrow = 'none, no contact carries our address'

            state = self.server_key.get(account.id)
            if state == 'absent':
                server = 'no public key'
            elif state == 'present':
                kept = self.server_public_keys.get(account.id)
                server = compared(key_fingerprint(kept)) if kept else 'keeps a public key (not received)'
            elif state == 'unsupported':
                server = f'not asked, {account.id.domain} has no SylkServer'
            else:
                server = 'unknown, the public key lookup was not answered'

            activity.info(f'[pgp] Private key of {account.id}: this device: {here}')
            activity.info(f'[pgp] Private key of {account.id}: addressbook escrow: {escrow}')
            from blink.key_storage import KeyStorage
            stored = KeyStorage().entry(account)
            if not KeyStorage._wanted(account) or stored is None:
                storage_escrow = 'not asked'
            elif stored['state'] == 'present':
                kept = stored['record']
                storage_escrow = (f"present, written {kept.get('timestamp') or '?'} by \"{kept.get('device') or '?'}\", "
                                  f"{compared(key_fingerprint(kept.get('public_key')))}")
            elif stored['state'] == 'absent':
                storage_escrow = 'none'
            elif stored['state'] in ('invalid', 'failed', 'unsupported'):
                storage_escrow = f"{stored['state']}: {stored['reason']}"
            else:
                storage_escrow = 'unknown, the account storage has not answered'
            activity.info(f'[pgp] Private key of {account.id}: storage escrow: {storage_escrow}')
            activity.info(f'[pgp] Private key of {account.id}: server public key: {server}')
        except Exception as e:
            activity.exception(f'[pgp] Reporting the private key of {account.id} failed: {e!r}')

    def _NH_XCAPManagerDidReloadData(self, notification):
        account = getattr(notification.sender, 'account', None)
        if not isinstance(account, Account):
            return
        self.checked.add(account.id)
        self.check_key_status(account)
        if not self._storage_answered(account):
            from blink.key_storage import KeyStorage
            if account.id not in self.storage_waiting:
                self.storage_waiting.add(account.id)
                ActivityLog().info(f'[pgp] Key escrow: waiting for the account storage of {account.id} before the escrow pass')
            KeyStorage().fetch(account, 'addressbook loaded')
            return
        self._escrow_pass(account)

    def _NH_BlinkKeyStorageDidChange(self, notification):
        account = notification.data.account
        waited = account.id in self.storage_waiting
        self.storage_waiting.discard(account.id)
        if account.id in self.checked and (waited or notification.data.state in ('present', 'absent')):
            self._escrow_pass(account)
        else:
            self._release(account)

    def _storage_answered(self, account):
        """Whether the account storage has said what escrow it keeps: answered, failed, or not
        asked (no storage for the account, PGP off)."""
        from blink.key_storage import KeyStorage
        storage = KeyStorage()
        return not storage._wanted(account) or storage.state(account) in ('present', 'absent', 'invalid', 'unsupported', 'failed')

    def _escrow_pass(self, account):
        """Restore, report, repair and the password check, once the addressbook and the account
        storage have answered."""
        from blink.contacts import AddressbookNotifier
        with AddressbookNotifier().quiet():
            # restore before reporting, so one pass tells one story
            try:
                self.restore(account)
            except Exception as e:
                ActivityLog().exception(f'[pgp] Key escrow restore failed for {account.id}: {e!r}')
            try:
                key_escrow.log_self_contact(account)
            except Exception as e:
                ActivityLog().error(f'[pgp] Key escrow inspection failed for {account.id}: {e!r}')
            try:
                self.repair(account)
            except Exception as e:
                ActivityLog().exception(f'[pgp] Key escrow repair failed for {account.id}: {e!r}')
            try:
                self.check_password(account)
            except Exception as e:
                ActivityLog().exception(f'[pgp] Key escrow password check failed for {account.id}: {e!r}')
        try:
            self._escrow_imported_key(account)      # may ask: outside the quiet stretch
        except Exception as e:
            ActivityLog().exception(f'[pgp] Key escrow of the imported key of {account.id} failed: {e!r}')
        self._release(account)

    # A key imported from another device goes onto our own contact at once

    def key_imported(self, account):
        if not isinstance(account, Account) or not account.xcap.enabled or not MessageManager._pgp_messaging_possible(account):
            return      # no addressbook on the server to keep a backup in
        self.imported.add(account.id)
        self.repaired.discard(account.id)
        self.password_checked.pop(account.id, None)
        self._escrow_imported_key(account)

    def _escrow_imported_key(self, account):
        if account.id not in self.imported:
            return
        if key_escrow._resource_lists_element(account) is None:
            ActivityLog().info(f'[pgp] Key escrow: the addressbook of {account.id} has not been fetched yet, '
                               f'the imported key is saved on the server once it is')
            return
        self.imported.discard(account.id)
        from blink.contacts import AddressbookNotifier
        foreign = key_escrow.foreign_escrows(account)
        if not foreign:
            # none, or the same key: write what is missing, re-encrypt what does not open
            with AddressbookNotifier().quiet():
                self.repair(account)
                self.check_password(account)
            return

        from blink.pgp_keyids import key_fingerprint
        contact_id, record = foreign[0]
        old = key_fingerprint(record.get('public_key'))
        try:
            with open(account.sms.public_key.normalized, 'rb') as f:
                new = key_fingerprint(f.read())
        except (OSError, AttributeError):
            new = None
        old_id = old[-16:].upper() if old else translate('pgp_key_escrow', 'unknown')
        new_id = new[-16:].upper() if new else translate('pgp_key_escrow', 'unknown')
        ActivityLog().info(f'[pgp] Key escrow: {key_escrow.escrow_location(contact_id)} of {account.id} keeps a backup of key {old_id}, '
                           f'saved {record.get("timestamp", "?")} by "{record.get("device", "?")}", not of the imported key {new_id}: asking')

        main_window = getattr(QApplication.instance(), 'main_window', None)
        box = QMessageBox(QMessageBox.Icon.Question, translate('pgp_key_escrow', 'Replace PGP key backup'),
                          translate('pgp_key_escrow', 'The server keeps a backup of another PGP key for %s.\n\n'
                                                      'Key on the server: %s, saved %s by %s\n'
                                                      'Imported key: %s\n\n'
                                                      'Replace the backup with the imported key? The key on the server '
                                                      'can then no longer be restored on a new device, and messages '
                                                      'encrypted to it cannot be read there.')
                          % (account.id, old_id, record.get('timestamp', '?'), record.get('device', '?'), new_id),
                          parent=main_window)
        replace_button = box.addButton(translate('pgp_key_escrow', 'Replace'), QMessageBox.ButtonRole.AcceptRole)
        keep_button = box.addButton(translate('pgp_key_escrow', 'Keep'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_button)
        box.exec()
        if box.clickedButton() is not replace_button:
            ActivityLog().info(f'[pgp] Key escrow: the user kept the backup of key {old_id} for {account.id}')
            return
        ActivityLog().info(f'[pgp] Key escrow: the user chose to replace the backup of key {old_id} for {account.id} with the imported key {new_id}')
        # announced: the other devices learn of the new backup on their next reload
        written, reason = key_escrow.write_self_keys(account, force=True)
        if not written:
            QMessageBox.warning(main_window, translate('pgp_key_escrow', 'Replace PGP key backup'),
                                translate('pgp_key_escrow', 'The backup of %s was not replaced: %s') % (account.id, reason or ''))

    # The escrow is encrypted with the account password: when that changes, it is
    # encrypted again with the new one, once the server has accepted it

    def _NH_CFGSettingsObjectDidChange(self, notification):
        account = notification.sender
        if not isinstance(account, Account) or 'auth.password' not in notification.data.modified:
            return
        self.accepted_passwords.pop(account.id, None)
        if account.sms.enable_pgp and MessageManager._pgp_messaging_possible(account):
            self.password_changed.add(account.id)
            ActivityLog().info(f'[pgp] Key escrow: the password of {account.id} changed, the escrow is encrypted '
                               f'with it once a registration succeeds with it')

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        account = notification.sender
        if not isinstance(account, Account):
            return
        password = (account.auth.password or '').strip()
        if self.accepted_passwords.get(account.id) == password:
            return      # a refresh: nothing new is known about the password
        self.accepted_passwords[account.id] = password
        if account.id in self.password_changed:
            self.password_changed.discard(account.id)
            ActivityLog().info(f'[pgp] Key escrow: the server accepted the new password of {account.id}')
        from blink.contacts import AddressbookNotifier
        try:
            with AddressbookNotifier().quiet():
                self.check_password(account)
        except Exception as e:
            ActivityLog().exception(f'[pgp] Key escrow password check failed for {account.id}: {e!r}')

    def check_password(self, account):
        """Re-encrypt the escrow of our key when it does not open with the account password,
        and only with a password a registration has succeeded with. Each escrow and password
        is checked once (decrypting is not free and reloads are frequent)."""
        if not account.enabled or not account.sms.enable_pgp or not MessageManager._pgp_messaging_possible(account):
            return
        password = (account.auth.password or '').strip()
        if not password or self.accepted_passwords.get(account.id) != password:
            return      # not accepted by the server (yet): never encrypt the escrow with it
        if key_escrow._resource_lists_element(account) is None:
            return      # the addressbook has not been fetched yet; its reload checks again
        if account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized):
            return
        import hashlib
        records = key_escrow.self_escrow_records(account)
        if not records:
            return      # nothing escrowed: repair writes it, with this password
        signature = (hashlib.sha256(password.encode()).hexdigest(),
                     tuple(sorted((str(contact_id), str(record.get('timestamp')), len(record.get('private_key') or ''))
                                  for contact_id, _label, record in records)))
        if self.password_checked.get(account.id) == signature:
            return
        self.password_checked[account.id] = signature
        key_escrow.reencrypt_escrow(account)

    def restore(self, account):
        if account.id in self.restore_done:
            return
        if account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized):
            return
        record = key_escrow.read_self_keys(account)
        if record is None:
            return
        password = (account.auth.password or '').strip()
        signature = '%s#%s#%d#%d' % (account.id, record.get('timestamp', '?'), len(record.get('private_key') or ''), len(password))
        if self.restore_failed.get(account.id) == signature:
            return
        restored, reason = key_escrow.restore_from_own_contact(account)
        if restored:
            self.restore_done.add(account.id)
            self.restore_failed.pop(account.id, None)
            ActivityLog().info(f'[pgp] The private key of {account.id} was restored from the server')
            MessageManager()._keys_installed(account)
        elif reason:
            self.restore_failed[account.id] = signature
            ActivityLog().info(f'[pgp] Key escrow: not restoring for {account.id}: {reason}')

    def repair(self, account):
        if not key_escrow.escrow_is_missing(account):
            self.repaired.discard(account.id)
            return
        if account.id in self.repaired:
            return
        if not key_escrow.self_contact_elements(account):
            # the escrow lives on our own contact, as Sylk Mobile keeps it: create it, and
            # the escrow is written on the reload that brings it back from the server
            self.create_self_contact(account)
            return
        self.repaired.add(account.id)
        ActivityLog().info(f'[pgp] Key escrow: {account.id} holds a key but the server carries no escrow for it, saving it there')
        written, reason = key_escrow.write_self_keys(account)
        if not written:
            ActivityLog().info(f'[pgp] Key escrow: could not save the key of {account.id} on the server: {reason}')

    def create_self_contact(self, account):
        if account.id in self.self_contact_created:
            return
        if key_escrow._resource_lists_element(account) is None:
            return      # the document has not been fetched yet
        if not (account.auth.password or '').strip():
            return      # nothing to encrypt the escrow with, so no reason for the contact either
        account_id = str(account.id).lower()
        manager = AddressbookManager()
        if any(str(uri.uri).lower().removeprefix('sip:') == account_id for contact in manager.get_contacts() for uri in contact.uris):
            return      # saved here, not on the server yet
        self.self_contact_created.add(account.id)
        from sipsimple import addressbook
        from blink import addressbook_origin
        contact = addressbook.Contact()
        contact.name = account.display_name or str(account.id)
        contact.uris = [addressbook.ContactURI(uri=str(account.id), type='sip')]
        with addressbook_origin.reason('key-escrow'):
            contact.save()
        ActivityLog().info(f'[pgp] Key escrow: no contact carries {account.id}, created one ({contact.id}) to keep the key on')

    def _addressbook_answered(self, account):
        if account.id in self.checked or not account.xcap.enabled:
            return True
        first = self._first_asked.setdefault(account.id, time.time())
        return time.time() - first > self.answer_timeout

    def answered(self, account):
        """Whether we know enough to decide: the addressbook has loaded (or will not), the
        account storage has said what escrow it keeps (or failed, or the wait is over) and the
        server has answered the public key lookup (or it failed)."""
        storage = self._storage_answered(account) or not self._addressbook_answered_in_time(account)
        return (self._addressbook_answered(account) and storage
                and self.server_key.get(account.id) in ('present', 'absent', 'failed', 'unsupported'))

    def _addressbook_answered_in_time(self, account):
        """False once the answer_timeout has passed since the first question: past it, nothing
        is waited for any longer."""
        first = self._first_asked.setdefault(account.id, time.time())
        return time.time() - first <= self.answer_timeout

    def may_generate_silently(self, account):
        """(True, None) when a key may be generated without asking, else (False, why not)."""
        if account is BonjourAccount():
            return True, None
        if not account.xcap.enabled:
            return False, 'the account has no addressbook on the server to look for its key in'
        if account.id not in self.checked:
            return False, 'the addressbook has not been fetched from the server'
        try:
            escrowed = bool(key_escrow.self_escrow_records(account))
        except Exception as e:
            return False, f'the addressbook could not be searched for a key ({e!r})'
        if escrowed:
            return False, 'the addressbook or the account storage keeps a key for it that this device could not restore'
        state = self.server_key.get(account.id)
        if state == 'present':
            return False, 'the server keeps a public key for it'
        if state == 'unsupported':
            return False, 'its domain has no SylkServer to ask whether a key exists'
        if state != 'absent':
            return False, 'the server did not answer whether it keeps a public key for it'
        return True, None

    def lookup_server_key(self, account):
        """Ask the server whether it keeps a public key for the account itself (once per run,
        again after a failure). Only a domain with SylkServer (blink.sylk_discovery) is asked;
        for another one the state is 'unsupported', asked again next time (discovery may have
        found one since)."""
        if account is BonjourAccount() or self.server_key.get(account.id) in ('pending', 'present', 'absent'):
            return
        from blink.sylk_discovery import has_sylkserver
        if not has_sylkserver(account):
            # no SylkServer for the domain (no _sylkserver TXT configuration found): nobody to ask
            self.server_key[account.id] = 'unsupported'
            return
        from types import SimpleNamespace
        lookup = object()
        self._lookups[account.id] = lookup
        self.server_key[account.id] = 'pending'
        ActivityLog().info(f'[pgp] Asking the server whether it keeps a public key for {account.id}')
        target = SimpleNamespace(uri=SimpleNamespace(uri=str(account.id)))     # no session: OutgoingMessage only needs the address
        OutgoingMessage(account, target, 'Public key request', 'application/sylk-api-pgp-key-lookup',
                        on_result=lambda code, reason: self._lookup_answered(account, lookup, code, reason)).send()
        call_later(self.lookup_timeout, self._lookup_answered, account, lookup, None, 'no answer in time')

    def _lookup_answered(self, account, lookup, code, reason):
        if self._lookups.get(account.id) is not lookup:
            return      # answered already, or a later lookup replaced it
        del self._lookups[account.id]
        reason = (reason or '').strip()
        if code == 404 and reason.lower() == self.no_key_reason:
            state = 'absent'
            ActivityLog().info(f'[pgp] The server keeps no public key for {account.id}')
        elif code == 200:
            state = 'present'
            ActivityLog().info(f'[pgp] The server keeps a public key for {account.id}')
        else:
            state = 'failed'
            ActivityLog().info(f'[pgp] The server did not say whether it keeps a public key for {account.id}: {code or ""} {reason}'.rstrip())
        self.server_key[account.id] = state
        self._release(account)

    def when_answered(self, account, callback):
        if account is BonjourAccount():
            callback()
            return
        self.lookup_server_key(account)
        if self.answered(account):
            callback()
            return
        waiting = self._waiting.get(account.id)
        if waiting is None:
            waiting = self._waiting[account.id] = []
            if not self._addressbook_answered(account):
                elapsed = time.time() - self._first_asked[account.id]
                ActivityLog().info(f'[pgp] Waiting for the addressbook of {account.id} before generating a PGP key: it may carry one')
                call_later(max(0.5, self.answer_timeout - elapsed + 0.1), self._release, account)
        waiting.append(callback)

    def _release(self, account):
        if not self.answered(account):
            return      # still waiting for the addressbook or for the server's answer
        for callback in self._waiting.pop(account.id, []):
            try:
                callback()
            except Exception as e:
                ActivityLog().exception(f'[pgp] Offering to generate a PGP key for {account.id} failed: {e!r}')
