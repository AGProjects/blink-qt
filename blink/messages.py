import bisect
import dns.resolver
import json
import os
import re
import requests
import time
import urllib3
import random
import urllib
import uuid
import pgpy

from collections import Counter, OrderedDict, deque

from PyQt6 import uic
from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QStyle, QDialog

from pgpy import PGPMessage
from pgpy.errors import PGPEncryptionError, PGPDecryptionError

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import makedirs, host
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
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError, CPIMNamespace, CPIMHeader, ChatIdentity, Message as MSRPChatMessage, SimplePayload
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.configuration.datatypes import File
from blink.message_envelopes import CALL_CONTENT_TYPE, LOCATION_CONTENT_TYPE, METADATA_CONTENT_TYPE, conversation_read_envelope, conversation_read_marker, foreign_call_record, metadata_link, this_device_id
from blink.location import storage_fields as location_storage_fields
from blink.journal import KNOWN_INERT_CONTENT_TYPES, JournalCache, JournalStats, OwnMarkers, SeenMessageIds, journal_action, parse_payload
from blink.logging import ActivityLog, MessagingTrace as log
from blink.resources import ApplicationData, Resources
from blink.sessions import SessionManager, StreamDescription, IncomingDialogBase
from blink.uris import bare_instance_id, canonical_uri, placeholder_instance_id
from blink.util import call_in_gui_thread, call_later, run_in_gui_thread, translate

__all__ = ['MessageManager', 'BlinkMessage']

dns_error_map = {dns.resolver.NXDOMAIN: 'Domain not found in DNS',
                 dns.resolver.NoAnswer: 'DNS response contains no answer',
                 dns.resolver.NoNameservers: 'no DNS name servers could be reached',
                 dns.resolver.Timeout: 'no DNS response received, the query has timed out'}

ui_class, base_class = uic.loadUiType(Resources.get('generate_pgp_key_dialog.ui'))


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

        if scenario == 1:
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
ui_class, base_class = uic.loadUiType(Resources.get('import_private_key_dialog.ui'))


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
ui_class, base_class = uic.loadUiType(Resources.get('export_private_key_dialog.ui'))


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


class BlinkMessage(MSRPChatMessage):
    __slots__ = 'id', 'disposition', 'is_secure', 'direction', 'metadata'

    def __init__(self, content, content_type, sender=None, recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None, disposition=None, is_secure=False, direction=None, metadata=None):
        super(BlinkMessage, self).__init__(content, content_type, sender, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)
        self.id = id if id is not None else str(uuid.uuid4())
        self.disposition = disposition
        self.is_secure = is_secure
        self.direction = direction
        self.metadata = metadata    # cleartext side-band (CPIM agp.Metadata, journal metadata): location v2, call records


class OTRInternalMessage(BlinkMessage):
    def __init__(self, content):
        super(OTRInternalMessage, self).__init__(content, 'text/plain')


def can_use_cpim(content_type):
    """Requests to the server API and public keys go out bare, as Blink for macOS and
    Sylk Mobile send them: the server reads their body, not a CPIM wrapper."""
    content_type = str(content_type or '').lower()
    return not (content_type.startswith('application/sylk-api') or content_type == 'text/pgp-public-key')


def skip_journal_headers(content, otr=False):
    """[X-Sylk-Skip-Journal] for an OTR message, else []. An OTR ciphertext is bound to
    the session that made it: replayed from the journal on another device it can never
    be read. SylkServer checks for the header's presence, on both sides of the relay."""
    if otr or (isinstance(content, bytes) and content.startswith(b'?OTR')):
        return [Header('X-Sylk-Skip-Journal', 'yes')]
    return []


@implementer(IObserver)
class OutgoingMessage(object):
    __ignored_content_types__ = {IsComposingDocument.content_type, IMDNDocument.content_type}  # Content types to ignore in notifications
    __disabled_imdn_content_types__ = {'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-api'}.union(__ignored_content_types__)  # Content types to ignore in notifications

    def __init__(self, account, contact, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None, session=None, use_cpim=True):
        self.lookup = None
        self.account = account
        self.uri = contact.uri.uri
        self.content_type = content_type
        self.content = content
        self.id = id if id is not None else str(uuid.uuid4())
        self.timestamp = timestamp if timestamp is not None else ISOTimestamp.now()
        self.sip_uri = SIPURI.parse('sip:%s' % self.uri)
        self.session = session
        self.contact = contact
        self.is_secure = False
        self.dns_failed_reason = None
        self.use_cpim = use_cpim and can_use_cpim(content_type)

    @property
    def message(self):
        return BlinkMessage(self.content, self.content_type, self.account, timestamp=self.timestamp, id=self.id, is_secure=self.is_secure, direction='outgoing')

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
                            data = NotificationData(originator='remote', reason=reason, id=self.id)
                            notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=data, code=None)
                            log.info(f'Message {self.id} to {str(self.sip_uri)[4:]} failed: {reason}')
                            return
                        self.is_secure = True
            content = content if isinstance(content, bytes) else content.encode()
            additional_sip_headers = skip_journal_headers(content)
            if additional_sip_headers:
                ActivityLog().info(f'[Message with {self._peer}] Sending {self.content_type} message {self.id} without journalling it (OTR)')
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

    def send(self):
        if self.content_type.lower() in ('text/pgp-private-key', 'application/sylk-api-token'):
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
            reason = dns_error_map.get(notification.data.error.__class__, '')
            self.dns_failed_reason = reason

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if notification.sender is self.lookup:
            routes = notification.data.result
            if self.content_type.lower() in ['text/pgp-private-key', 'application/sylk-api-token']:
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
        if self.content_type.lower() == IsComposingDocument.content_type:
            return

        if self.session is None:
            return

        reason = self.dns_failed_reason or notification.data.error
        originator = 'local' if 'no DNS' in self.dns_failed_reason else 'remote'

        log.info(f'DNS lookup for message {self.id} failed {originator}ly: {reason}')

        data = NotificationData(reason=reason, originator=originator, id=self.id, code=None)
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=data)

    @property
    def _peer(self):
        # a Bonjour neighbour by instance id, not by today's address
        return getattr(self.session, 'remote_instance_id', None) or self.uri

    def _NH_SIPMessageDidSucceed(self, notification):
        notification_center = NotificationCenter()
        if self.content_type.lower() in self.__ignored_content_types__:
            if self.content_type.lower() == IMDNDocument.content_type:
                document = IMDNDocument.parse(self.content)
                imdn_message_id = document.message_id.value
                imdn_status = document.notification.status.__str__()
                notification_center.post_notification('BlinkDidSendDispositionNotification', sender=self.session, data=NotificationData(id=imdn_message_id, status=imdn_status))
            return

        if self.session is not None:
            notification_center.post_notification('BlinkMessageDidSucceed', sender=self.session, data=NotificationData(data=notification.data, id=self.id))
        if not self._disabled_imdn_content_type:
            ActivityLog().info(f'[Message with {self._peer}] Sent {self.content_type} message {self.id} from account {self.account.id}')

    def _NH_SIPMessageDidFail(self, notification):
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

        log.info(f'Message {self.id} to {self.session.contact_uri.uri} failed {originator}ly: {code} {reason}')
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


@implementer(IObserver)
class MessageManager(object, metaclass=Singleton):
    __ignored_content_types__ = {IsComposingDocument.content_type, IMDNDocument.content_type,
                                 'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-message-remove', 'application/sylk-api'}

    # what the journal ignores and nothing below handles live
    __not_history_content_types__ = {'application/sylk-addressbook-update', 'application/sylk-data-export', 'application/sylk-contact-update'}

    own_message_ids_size = 1000
    seen_message_ids_size = 10000

    def __init__(self):
        self.sessions = []
        self._own_message_ids = OrderedDict()  # ids of messages sent by this device, to recognise their replicated copies
        self.seen_message_ids = SeenMessageIds(self.seen_message_ids_size)  # handled live or from the journal, whichever came first
        self._own_conversation_reads = OwnMarkers(ttl=30)  # read markers this device sent, to recognise their echo
        self._own_conversation_removes = OwnMarkers(ttl=60)  # conversation removals this device asked the server for
        self._outgoing_message_queue = deque()
        self._incoming_encrypted_message_queue = deque()
        self._sync_queue = deque()
        self._token_requested = {}      # account id -> monotonic time of the last API token request
        self._token_retry_pending = set()
        self._syncing = set()           # account ids with a history download in progress
        self.pgp_requests = RequestList()

        self._removing_conversations = {}  # conversation key -> session, while the user removes it

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPEngineGotMessage')
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
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

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
            # don't process my own public key
            return

        directory = settings.chat.keys_directory.normalized
        filename = os.path.join(directory, id + '.pubkey')
        makedirs(directory)
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
    journal_since_years = 5     # how far back a first sync (no cursor) asks, as sylk mobile does
    journal_max_pages = 200     # pages per run; the next run continues from the cursor

    def _journal_directory(self, account):
        path = ApplicationData.get(f'journal/{account.id}')
        makedirs(path)
        return path

    def _journal_page_url(self, account):
        """The next journal page: after the cursor, or for a first sync `since` five years ago.

        Without `since` and without a cursor the server answers with the last
        three days only, and the older entries are never asked for.
        """
        base = str(account.sms.history_synchronization_url)
        cursor = account.sms.history_synchronization_id
        if cursor:
            url = urllib.parse.urljoin(f'{base}/', cursor)
            query = ''
        else:
            since = datetime.now(timezone.utc) - timedelta(days=365 * self.journal_since_years)
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
        if not account.sms.enable_history_synchronization:
            if account.sms.history_synchronization_timestamp:
                account.sms.history_synchronization_timestamp = None
                account.save()
            return

        if not account.sms.history_synchronization_token:
            self._request_history_synchronization_token(account, 'no token')
            return

        if not account.sms.history_synchronization_url:
            return

        directory = self._journal_directory(account)
        headers = {'Authorization': f'Apikey {account.sms.history_synchronization_token}'}
        settings = SIPSimpleSettings()
        activity = ActivityLog()
        first_sync = not account.sms.history_synchronization_id
        activity.info(f'[journal] Fetching the message journal of {account.id}' + (f' ({reason})' if reason else '')
                      + (f' since {self.journal_since_years} years ago' if first_sync else f' after {account.sms.history_synchronization_id}'))
        if not settings.tls.verify_server and not self._journal_unverified_logged:
            # the user's choice (tls.verify_server); said once here instead of a urllib3 warning per request
            self._journal_unverified_logged = True
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            activity.warning('[journal] The certificate of the journal server is not verified (TLS verify server is off)')
        started = time.monotonic()
        pages = entries = transferred = 0
        complete = False
        stats = JournalStats(account.id, first_sync=first_sync, cursor=account.sms.history_synchronization_id, reason=reason)

        while pages < self.journal_max_pages:
            cursor = account.sms.history_synchronization_id
            url = self._journal_page_url(account)
            page_started = time.monotonic()
            log.info(f'Fetching message history for {account.id} from server {url}')
            try:
                r = requests.get(url, headers=headers, timeout=20, verify=settings.tls.verify_server)
                r.raise_for_status()
                data = r.json()
            except requests.HTTPError as e:
                if e.response.status_code == 401:
                    activity.info(f'[journal] The API token of {account.id} was refused (401)')
                    self._request_history_synchronization_token(account, 'token refused')
                else:
                    activity.warning(f'[journal] The message journal of {account.id} answered {e.response.status_code}, stopped after {pages} pages')
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                activity.warning(f'[journal] Cannot reach the message journal of {account.id}, stopped after {pages} pages: {e}')
                break
            except (requests.RequestException, ValueError) as e:
                activity.warning(f'[journal] Bad answer from the message journal of {account.id}, stopped after {pages} pages: {e}')
                break

            messages = data.get('messages') if isinstance(data, dict) else None
            if not messages:
                complete = True
                break

            name = self._journal_file_name(messages)
            path = os.path.join(directory, name)
            try:
                with open(path + '.part', 'w', encoding='utf-8') as page_file:
                    # the cursor as it stood before this page: the apply stage tells a
                    # first-ever backfill from a catch-up by it
                    json.dump({'cursor': cursor or '', 'messages': messages}, page_file)
                os.replace(path + '.part', path)
            except OSError as e:
                activity.error(f'[journal] Cannot save journal page {path}: {e}')
                break

            pages += 1
            entries += len(messages)
            transferred += len(r.content)
            last_id = messages[-1].get('message_id')
            stats.page_downloaded(name, len(messages), len(r.content), time.monotonic() - page_started, last_id)
            log.info(f'Cached journal page {name} ({len(messages)} entries, {len(r.content)} bytes, cursor {last_id})')

            if not last_id:
                break
            account.sms.history_synchronization_id = last_id
            account.save()
        else:
            activity.warning(f'[journal] Stopped the download of {account.id} at {self.journal_max_pages} pages, the rest follows on the next sync')

        if pages:
            activity.info(f'[journal] Downloaded {entries} journal entries of {account.id} in {pages} pages ({transferred} bytes, {time.monotonic() - started:.1f}s)')
        elif complete:
            activity.info(f'[journal] The message journal of {account.id} has no new entries')
        self._apply_cached_journal(account, stats)

    journal_progress_every = 250    # entries between progress lines, and between short pauses
    journal_throttle = 0.05         # seconds to pause, so the GUI and the db thread keep up

    def _apply_cached_journal(self, account, stats=None):
        """Apply cached journal pages oldest first.

        A page is deleted only once applied. A page that cannot be applied stops
        the run (order matters) and is retried on the next sync; after
        MAX_PAGE_ATTEMPTS failed runs it is quarantined and the pages after it go on.
        """
        cache = JournalCache(self._journal_directory(account))
        names = cache.pages()
        if not names:
            return
        activity = ActivityLog()
        activity.info(f'[journal] Applying {len(names)} cached journal pages of {account.id}')
        if stats is None:
            stats = JournalStats(account.id, reason='cached pages')
        started = time.monotonic()
        totals = Counter()
        contacts = Counter()
        applied = 0
        for name in names:
            try:
                page = cache.load(name)
            except (OSError, ValueError) as e:
                page = None
                error = e
            if page is not None:
                try:
                    page_stats = self._apply_server_history_messages(account, page.get('messages') or [], first_sync=not page.get('cursor'), contacts=contacts, stats=stats)
                except Exception as e:
                    page_stats = None
                    error = e
            if page is None or page_stats is None:
                attempts, quarantined = cache.failed(name)
                if quarantined:
                    stats.quarantined.append(name)
                    activity.error(f'[journal] Journal page {name} of {account.id} failed {attempts} times and was moved to quarantine: {error}')
                    continue
                activity.exception(f'[journal] Applying journal page {name} of {account.id} failed (attempt {attempts}), kept for the next sync: {error}')
                break
            cache.applied(name)
            applied += 1
            totals.update(page_stats)
            log.info(f'Applied journal page {name}: ' + ', '.join(f'{count} {outcome}' for outcome, count in sorted(page_stats.items())))
        stats.apply_seconds += time.monotonic() - started
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
            activity.info(f'[journal] Import statistics written to {stats_path}')
        # one notification for the whole run: unread counts and the Messages group are refreshed from
        # history, then the database is counted against this run
        NotificationCenter().post_notification('BlinkJournalDidApply', sender=account, data=NotificationData(new_messages=dict(contacts), stats_path=stats_path, first_sync=stats.first_sync))

    @run_in_thread('sync')
    def _process_server_history_messages(self, account, messages):
        self._apply_server_history_messages(account, messages)

    def _apply_server_history_messages(self, account, messages, first_sync=False, contacts=None, stats=None):
        """Apply journal entries by content type (blink.journal.journal_action). Runs in the sync thread.

        Bulk mode: nothing here creates a conversation or decrypts; an entry for a
        conversation that is open is also shown there. Returns {outcome: count}.
        An entry that cannot be applied is counted as failed and logged; the page
        still counts as applied.
        """
        outcomes = Counter()
        contacts = contacts if contacts is not None else Counter()
        started = time.monotonic()
        log.debug(f'-- {len(messages)} messages fetched from server for {account.id}')
        for index, message in enumerate(messages, 1):
            content_type = str(message.get('content_type') or '').lower()
            action = journal_action(content_type)
            if self.seen_message_ids.seen(message.get('message_id')):
                outcome = 'duplicates'      # already handled live (or earlier in this run)
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
            if index % self.journal_progress_every == 0:
                elapsed = time.monotonic() - started
                log.info(f'Applied {index} of {len(messages)} journal entries of {account.id} ({index / elapsed if elapsed else 0:.0f}/s)')
                time.sleep(self.journal_throttle)
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
        if first_sync:
            return 'receipts skipped (first sync)'
        payload = parse_payload(message.get('content'))
        if not payload or not payload.get('message_id'):
            return 'failed'
        kwargs = {'data': NotificationData(id=payload['message_id'], status=message.get('state'))}
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
        ActivityLog().info(f'[Message with {contact_uri.uri}] Conversation removed on another device (from the journal), messages up to {timestamp} for account {account.id}')
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
        """Persist a journal entry (history decides read state and category) and show it if its conversation is open."""
        NotificationCenter().post_notification('BlinkGotHistoryMessage', sender=account,
                                               data=NotificationData(remote_uri=remote_uri, message=history_message, encryption=encryption, state=state))

    def _journal_sender(self, account, message, contact):
        if message['direction'] == 'incoming':
            return ChatIdentity(SIPURI.parse(f'sip:{contact.uri.uri}'), contact.name)
        return account

    def _journal_file_transfer(self, account, message, content_type, first_sync, contacts):
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
        self._journal_store(account, message, history_message, contact.uri.uri, encryption='OpenPGP' if is_secure else None, state='accepted')
        if message['direction'] == 'incoming':
            contacts[contact.uri.uri] += 1
        session = self._journal_session(contact)
        if session is not None:
            NotificationCenter().post_notification('BlinkGotMessage', sender=session, data=NotificationData(message=history_message, history=True, account=account))
            file = File(document['filename'], document.get('filesize'), contact, file_hash, message['message_id'], ISOTimestamp(until),
                        document.get('url'), account=account, protocol='sylk')
            NotificationCenter().post_notification('BlinkSessionDidShareFile', sender=session, data=NotificationData(file=file, direction=message['direction']))
        return 'files'

    def _journal_text(self, account, message, content_type, first_sync, contacts):
        if message.get('contact') is None:
            return 'failed'
        content = message.get('content') or ''
        if content.startswith('?OTR:') or content.startswith('?OTRv3?'):
            return 'OTR skipped'
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(message['contact'])
        history_message = BlinkMessage(content, message['content_type'], self._journal_sender(account, message, contact),
                                       timestamp=self._journal_timestamp(message), id=message['message_id'],
                                       disposition=message.get('disposition'), direction=message['direction'])
        encryption = self.check_encryption(history_message.content_type, history_message.content)
        self._journal_store(account, message, history_message, message['contact'], encryption=encryption, state=message.get('state'))
        if message['direction'] == 'incoming':
            contacts[contact.uri.uri] += 1
        session = self._journal_session(contact)
        if session is not None:
            if message['direction'] == 'incoming' and 'positive-delivery' in (history_message.disposition or ()):
                self.send_imdn_message(session, history_message.id, history_message.timestamp, 'delivered')
            NotificationCenter().post_notification('BlinkGotMessage', sender=session, data=NotificationData(message=history_message, history=True, account=account))
            if encryption == 'OpenPGP':
                if session.fake_streams.get('messages').can_decrypt:
                    session.fake_streams.get('messages').decrypt(history_message)
                else:
                    self._incoming_encrypted_message_queue.append((history_message, account, contact))
        return 'texts'

    def _take_call_record(self, account, body, metadata, party, message_id, origin):
        """Hand a call detail record from another device to history; returns the journal outcome."""
        record, refused = foreign_call_record(body, metadata, party, account.id, this_device_id())
        if record is None:
            ActivityLog().info(f'[Message] Call record message {message_id} for account {account.id} skipped ({origin}): {refused}')
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
            return f'location {tick["related_action"]}' if tick else 'location (unreadable)'
        if content_type == METADATA_CONTENT_TYPE:
            link = metadata_link(history_message.content)
            return f'metadata {link[1]}' if link is not None else 'metadata (unlinked)'
        return f'stored as {content_type}'

    @run_in_gui_thread
    def handle_notification(self, notification):
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

        for session in [session for session in self.sessions if session.account is request.account]:
            stream = session.fake_streams.get('messages')
            if stream and not stream.can_encrypt:
                stream.enable_pgp()

        while self._incoming_encrypted_message_queue:
            message, account, contact = self._incoming_encrypted_message_queue.popleft()
            try:
                blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)
            except StopIteration:
                pass
            else:
                stream = blink_session.fake_streams.get('messages')
                if not stream.can_encrypt:
                    stream.enable_pgp()

                stream.decrypt(message)

    def _SH_ExportPGPKeys(self, request, message):
        account = request.account
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(account.uri)
        outgoing_message = OutgoingMessage(account, contact, message, 'text/pgp-private-key')
        self._send_message(outgoing_message)

    def _SH_GeneratePGPKeys(self, request):
        session = request.session
        stream = session.fake_streams.get('messages')
        stream.generate_keys()

    def _SH_PGPRequestFinished(self, request):
        request.dialog.hide()
        self.pgp_requests.remove(request)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if not isinstance(notification.sender, Account):
            return
        modified = notification.data.modified
        if 'sms.enable_history_synchronization' in modified:
            self._sync_messages(notification.sender, 'history synchronization enabled')
        elif 'sms.enable_message_replication' in modified and notification.sender.sms.enable_message_replication:
            self._sync_messages(notification.sender, 'replication enabled')

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        account = notification.sender
        if account is not BonjourAccount():
            # give the registration a moment to settle (and the server to see the device)
            call_later(self.sync_registration_delay, self._sync_messages, account, 'registered')

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

        if instance_id and instance_id.startswith('urn:uuid:'):
            instance_id = instance_id[9:]

        if x_replicated_message is not Null:
            if not account.sms.enable_message_replication:
                log.debug(f'Skipping replicated message for account {account.id}')
                return

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

        encryption = self.check_encryption(content_type, body)
        enc_text = f'{encryption} encrypted ' if encryption else ''

        log.info(f'Message {message_id} {enc_text}{content_type.lower()} for account {account.id} from {sender.uri}')
        if content_type.lower() not in (IsComposingDocument.content_type, IMDNDocument.content_type):
            def aor(uri):
                # SIP headers carry bytes, CPIM headers str
                user, host = (part.decode(errors='replace') if isinstance(part, bytes) else part for part in (uri.user, uri.host))
                return f'{user}@{host}'
            if x_replicated_message is not Null:
                peer, what = aor(to_header.uri), 'Replicated outgoing'
            else:
                peer, what = instance_id or aor(sender.uri), 'Incoming'
            ActivityLog().info(f'[Message with {peer}] {what} {enc_text}{content_type.lower()} message {message_id} for account {account.id}')
        if account is BonjourAccount() and instance_id:
            log.debug(f'Bonjour neighbour instance id is {instance_id}')

        if x_replicated_message is not Null and message_id in self._own_message_ids:
            # the server replicates a message to all devices of the sender, this one included
            log.debug(f'Ignoring replicated copy of message {message_id} sent by this device')
            return

        if encryption == 'OpenPGP':
            if account.sms.enable_pgp and (account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized)):
                if not self.pgp_requests[account, GeneratePGPKeyRequest] and account is not BonjourAccount():
                    generate_dialog = GeneratePGPKeyDialog()
                    generate_request = GeneratePGPKeyRequest(generate_dialog, account, 0)
                    generate_request.accepted.connect(self._SH_GeneratePGPKeys)
                    generate_request.finished.connect(self._SH_PGPRequestFinished)
                    bisect.insort_right(self.pgp_requests, generate_request)
                    generate_request.dialog.show()
            elif not account.sms.enable_pgp:
                log.info(f"-- Skipping PGP encrypted message, PGP is disabled for {account.id}")
                return

        # only a CPIM message carries the sender's id; without CPIM the id above is made up here
        if cpim_message is not None and self.seen_message_ids.seen(message_id):
            ActivityLog().info(f'[Message] {content_type.lower()} message {message_id} for account {account.id} skipped, it was already handled (live or from the journal)')
            return

        if content_type.lower() == IsComposingDocument.content_type and x_replicated_message is not Null:
            # our own typing notice, replicated back from the server: never "typing" to ourselves
            log.debug(f'Ignoring the replicated copy of our is-composing message {message_id} for account {account.id}')
            return

        if content_type.lower() in self.__not_history_content_types__:
            # not history: acted on elsewhere or not at all. The other types the journal ignores
            # (API token, private key, typing) are handled live below
            ActivityLog().info(f'[Message] {content_type.lower()} message {message_id} for account {account.id} skipped, it is not history')
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

        if journal_action(content_type) == 'inert' and content_type.lower() != FTHTTPDocument.content_type:
            # stored as it is and never unread, without opening a conversation: locations, metadata
            # companions, call records and types this version does not know (not shown, for now)
            remote_uri = contact_instance_id(contact, contact_uri) or contact.uri.uri
            notification_center.post_notification('BlinkGotHistoryMessage', sender=account,
                                                  data=NotificationData(remote_uri=remote_uri, message=message, encryption=encryption, state='accepted'))
            known = content_type.lower() in KNOWN_INERT_CONTENT_TYPES
            link = metadata_link(body) if content_type.lower() == METADATA_CONTENT_TYPE else None
            tick = location_storage_fields(body, metadata) if content_type.lower() == LOCATION_CONTENT_TYPE else {}
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
            notification_center.post_notification('BlinkGotDispositionNotification', sender=blink_session, data=NotificationData(id=imdn_message_id, status=imdn_status))
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

            generate_dialog = GeneratePGPKeyDialog()
            generate_request = GeneratePGPKeyRequest(generate_dialog, session.account, 1, session)
            generate_request.accepted.connect(self._SH_GeneratePGPKeys)
            generate_request.finished.connect(self._SH_PGPRequestFinished)
            bisect.insort_right(self.pgp_requests, generate_request)
            generate_request.dialog.show()

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

            generate_dialog = GeneratePGPKeyDialog()
            generate_request = GeneratePGPKeyRequest(generate_dialog, session.account, 1, session)
            generate_request.accepted.connect(self._SH_GeneratePGPKeys)
            generate_request.finished.connect(self._SH_PGPRequestFinished)
            bisect.insort_right(self.pgp_requests, generate_request)
            generate_request.dialog.show()


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
        notification.data.message.is_secure = True

        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidDecrypt', sender=session, data=NotificationData(message=notification.data.message))
        self._handle_incoming_message(notification.data.message, session, notification.data.account)

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

        self.send_imdn_message(session, msg_id, message.timestamp, 'error')

    def _NH_BlinkMessageHistoryFailedLocalFound(self, notification):
        log.info('Resending unsent messages...')
        messages = notification.data.messages
        created_views = set()
        for message in messages:
            from blink.contacts import URIUtils
            contact, contact_uri = URIUtils.find_contact(message.remote_uri)

            if placeholder_instance_id(contact_uri.uri):
                continue  # a Bonjour neighbour who is away: retried when it is back

            if contact_uri.uri in created_views:
                # creation of message views take time, so we need to skip duplicates here
                continue

            session_manager = SessionManager()
            account = AccountManager().get_account(message.account_id)

            instance_id = contact_instance_id(contact, contact_uri)

            try:
                blink_session = next(session for session in self.sessions if session.contact_uri.uri == contact_uri.uri or (instance_id and instance_id == session.remote_instance_id))
            except StopIteration:
                log.info(f"Create message view from history for {contact_uri.uri} with instance_id {instance_id}")
                ActivityLog().info(f'[Message with {instance_id or contact_uri.uri}] Conversation opened from message history for account {account.id}')
                created_views.add(contact_uri.uri)
                try:
                    ab_contact = next(contact for contact in AddressbookManager().get_contacts() if contact_uri.uri in (addr.uri for addr in contact.uris))
                except StopIteration:
                    pass
                else:
                    contact.settings.name = ab_contact.name
                blink_session = session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False)
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

    def generate_private_key(self, account):
        if account is None:
            return

        log.info(f'Generate a new private key for {account.id}')
        timestamp = datetime.now().strftime('%Y%m%d%H%M')
        settings = SIPSimpleSettings()
        directory = os.path.join(settings.chat.keys_directory.normalized, 'private')
        filename = os.path.join(directory, account.id)
        os.rename(f'{filename}.privkey', f'{filename}-{timestamp}-old.privkey')
        os.rename(f'{filename}.pubkey', f'{filename}-{timestamp}-old.pubkey')

        account.sms.public_key = None
        account.sms.private_key = None
        account.save()
        log.debug(f'Current key deleted for {account.id}')

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

    def send_conversation_read(self, session):
        """Tell this account's other devices the conversation was read here. Sent to the
        server API from the account to itself; the server replicates it to every device
        as application/sylk-conversation-read, this one included (the echo is ignored)."""
        account = session.account
        if account is BonjourAccount():
            return  # no server behind a link-local network
        if not account.sms.enable_message_replication:
            return  # no other devices to tell
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

    def announce_conversation_removal(self, keys):
        """Ask the server to remove these conversations from the other devices of every
        account that replicates (a contact deleted permanently here). The echo the
        server sends back is ignored (OwnMarkers)."""
        keys = [str(key) for key in keys if key]
        if not keys:
            return
        from blink.contacts import URIUtils
        for account in AccountManager().get_accounts():
            if account is BonjourAccount() or not account.enabled or not account.sms.enable_message_replication:
                continue
            for key in keys:
                if key == str(account.id).lower():
                    continue
                ActivityLog().info(f'[Message with {key}] Asking the server to remove the conversation from the other devices of {account.id}')
                content = json.dumps({'contact': key, 'timestamp': str(ISOTimestamp.now())})
                self._own_conversation_removes.note(canonical_uri(key, account))
                contact, contact_uri = URIUtils.find_contact(account.uri)
                self._send_message(OutgoingMessage(account, contact, content, 'application/sylk-api-conversation-remove', use_cpim=False))

    def send_imdn_message(self, session, id, timestamp, state, account=None):
        if host.default_ip is None:
            return

        if account is None and not session.account.sms.use_cpim or not session.account.sms.enable_imdn:
            return

        if account is not None:
            if not account.sms.use_cpim or not account.sms.enable_imdn:
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
