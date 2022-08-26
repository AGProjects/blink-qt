import bisect
import os
import re
import random
import uuid

from collections import deque

from PyQt5 import uic
from PyQt5.QtCore import Qt, QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication, QDialogButtonBox, QStyle, QDialog

from pgpy import PGPMessage
from pgpy.errors import PGPEncryptionError, PGPDecryptionError

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import makedirs
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.account import Account, AccountManager
from sipsimple.addressbook import AddressbookManager, Group, Contact, ContactURI
from sipsimple.configuration import DuplicateIDError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, FromHeader, ToHeader, Message, RouteHeader
from sipsimple.lookup import DNSLookup
from sipsimple.payloads import ParserError
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.payloads.imdn import IMDNDocument, DeliveryNotification, DisplayNotification
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError, CPIMNamespace, CPIMHeader, ChatIdentity, Message as MSRPChatMessage, SimplePayload
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.resources import Resources
from blink.sessions import SessionManager, StreamDescription, IncomingDialogBase
from blink.util import run_in_gui_thread


__all__ = ['MessageManager', 'BlinkMessage']


ui_class, base_class = uic.loadUiType(Resources.get('generate_pgp_key_dialog.ui'))


class GeneratePGPKeyDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(GeneratePGPKeyDialog, self).__init__(parent)

        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)

        self.slot = None
        self.generate_button = self.dialog_button_box.addButton("Generate", QDialogButtonBox.AcceptRole)
        self.generate_button.setIcon(QApplication.style().standardIcon(QStyle.SP_DialogApplyButton))

    def show(self, activate=True):
        self.setAttribute(Qt.WA_ShowWithoutActivating, not activate)
        super(GeneratePGPKeyDialog, self).show()


class GeneratePGPKeyRequest(QObject):
    finished = pyqtSignal(object)
    accepted = pyqtSignal(object, str)
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
        if result == QDialog.Accepted:
            self.accepted.emit(self)
        elif result == QDialog.Rejected:
            self.rejected.emit(self)


del ui_class, base_class
ui_class, base_class = uic.loadUiType(Resources.get('import_private_key_dialog.ui'))


class ImportDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(ImportDialog, self).__init__(parent)

        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)

        self.slot = None
        self.import_button = self.dialog_button_box.addButton("Import", QDialogButtonBox.AcceptRole)
        self.import_button.setIcon(QApplication.style().standardIcon(QStyle.SP_DialogApplyButton))
        self.import_button.setEnabled(False)

    def show(self, activate=True):
        self.setAttribute(Qt.WA_ShowWithoutActivating, not activate)
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
        if result == QDialog.Accepted:
            self.accepted.emit(self, f'{self.before}{self.private_key}{self.after}')
        elif result == QDialog.Rejected:
            self.rejected.emit(self)


del ui_class, base_class
ui_class, base_class = uic.loadUiType(Resources.get('export_private_key_dialog.ui'))


class ExportDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(ExportDialog, self).__init__(parent)

        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)

        self.slot = None
        self.export_button = self.dialog_button_box.addButton("Export", QDialogButtonBox.AcceptRole)
        self.export_button.setIcon(QApplication.style().standardIcon(QStyle.SP_DialogApplyButton))
        self.export_button.setEnabled(False)

    def show(self, activate=True):
        self.setAttribute(Qt.WA_ShowWithoutActivating, not activate)
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

    def _SH_DialogFinished(self, result):
        self.finished.emit(self)
        if result == QDialog.Accepted:
            self.accepted.emit(self, f'{self.public_key}{str(self.enc_message)}')
        elif result == QDialog.Rejected:
            self.rejected.emit(self)


del ui_class, base_class


class BlinkMessage(MSRPChatMessage):
    __slots__ = 'id', 'disposition', 'is_secure'

    def __init__(self, content, content_type, sender=None, recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None, disposition=None, is_secure=False):
        super(BlinkMessage, self).__init__(content, content_type, sender, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)
        self.id = id if id is not None else str(uuid.uuid4())
        self.disposition = disposition
        self.is_secure = is_secure


@implementer(IObserver)
class OutgoingMessage(object):
    __ignored_content_types__ = {IsComposingDocument.content_type, IMDNDocument.content_type}  # Content types to ignore in notifications
    __disabled_imdn_content_types__ = {'text/pgp-public-key', 'text/pgp-private-key'}.union(__ignored_content_types__)  # Content types to ignore in notifications

    def __init__(self, account, contact, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None, session=None):
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

    @property
    def message(self):
        return BlinkMessage(self.content, self.content_type, self.account, timestamp=self.timestamp, id=self.id, is_secure=self.is_secure)

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
            if self.session is not None:
                stream = self.session.fake_streams.get('messages')
                if self.content_type.lower() not in self.__disabled_imdn_content_types__:
                    if self.account.sms.enable_pgp and stream.can_encrypt:
                        content = stream.encrypt(self.content)
                        self.is_secure = True
            content = content if isinstance(content, bytes) else content.encode()
            additional_sip_headers = []
            if self.account.sms.use_cpim:
                ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
                additional_headers = [CPIMHeader('Message-ID', ns, self.id)]
                if self.account.sms.enable_imdn and self.content_type not in self.__disabled_imdn_content_types__:
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
            message_request.send()
        else:
            pass
            # TODO

    def send(self):
        if self.content_type.lower() == 'text/pgp-private-key':
            self._lookup()
            return

        if self.session is None:
            return

        if self.content_type.lower() not in self.__disabled_imdn_content_types__:
            notification_center = NotificationCenter()
            notification_center.post_notification('BlinkMessageIsPending', sender=self.session, data=NotificationData(message=self.message, id=self.id))

        if self.session.routes:
            self._send()
        else:
            self._lookup()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if notification.sender is self.lookup:
            routes = notification.data.result
            if self.content_type.lower() == 'text/pgp-private-key':
                self._send(routes)
                return
            self.session.routes = routes

            # TODO: Figure out how now to send a public when required, not always on start of the first message in the session
            if self.content_type != 'text/pgp-public-key':
                stream = self.session.fake_streams.get('messages')
                if self.account.sms.enable_pgp and stream.can_encrypt:
                    directory = os.path.join(SIPSimpleSettings().chat.keys_directory.normalized, 'private')
                    filename = os.path.join(directory, f'{self.account.id}')

                    with open(f'{filename}.pubkey', 'rb') as f:
                        public_key = f.read().decode()
                    public_key_message = OutgoingMessage(self.account, self.contact, str(public_key), 'text/pgp-public-key', session=self.session)
                    public_key_message.send()
            self._send()

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self.content_type.lower() == IsComposingDocument.content_type:
            return

        if self.session is None:
            return
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=NotificationData(data=NotificationData(code=404, reason=notification.data.error), id=self.id))

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

    def _NH_SIPMessageDidFail(self, notification):
        if self.content_type.lower() in self.__ignored_content_types__:
            return

        if self.session is None:
            return
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=NotificationData(data=notification.data, id=self.id))


class RequestList(list):
    def __getitem__(self, key):
        if isinstance(key, int):
            return super(RequestList, self).__getitem__(key)
        elif isinstance(key, tuple):
            account, item_type = key
            return [item for item in self if item.account is account and isinstance(item, item_type)]
        else:
            return [item for item in self if item.account is key]


@implementer(IObserver)
class MessageManager(object, metaclass=Singleton):
    __ignored_content_types__ = {IsComposingDocument.content_type, IMDNDocument.content_type, 'text/pgp-public-key', 'text/pgp-private-key'}

    def __init__(self):
        self.sessions = []
        self._outgoing_message_queue = deque()
        self._incoming_encrypted_message_queue = deque()
        self.pgp_requests = RequestList()

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPEngineGotMessage')
        notification_center.add_observer(self, name='BlinkSessionWasCreated')
        notification_center.add_observer(self, name='BlinkSessionNewOutgoing')
        notification_center.add_observer(self, name='BlinkSessionWasDeleted')
        notification_center.add_observer(self, name='PGPKeysDidGenerate')
        notification_center.add_observer(self, name='PGPMessageDidNotDecrypt')
        notification_center.add_observer(self, name='PGPMessageDidDecrypt')

    def _add_contact_to_messages_group(self, account, contact):  # Maybe this needs to be placed in Contacts? -- Tijmen
        if not account.sms.add_unknown_contacts:
            return
        if contact.type not in ['dummy', 'unknown']:
            return

        print('Adding contact')
        group_id = '_messages'
        try:
            group = next((group for group in AddressbookManager().get_groups() if group.id == group_id))
        except StopIteration:
            try:
                group = Group(id=group_id)
            except DuplicateIDError as e:
                return
            else:
                group.name = 'Messages'
                group.position = 0
                group.expanded = True

        new_contact = Contact()
        new_contact.name = contact.name
        new_contact.preferred_media = contact.preferred_media
        new_contact.uris = [ContactURI(uri=uri.uri, type=uri.type) for uri in contact.uris]
        new_contact.save()

        group.contacts.add(contact)
        group.save()

    @run_in_thread('file-io')
    def _save_pgp_key(self, data, uri):
        print(f'-- Saving public key for {uri}')
        settings = SIPSimpleSettings()

        id = str(uri).replace('/', '_')
        directory = settings.chat.keys_directory.normalized
        filename = os.path.join(directory, id + '.pubkey')
        makedirs(directory)

        with open(filename, 'wb') as f:
            data = data if isinstance(data, bytes) else data.encode()
            f.write(data)

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
                with open(filename) as f:
                    content = f.read()
            except Exception as e:
                pass
            else:
                if content == public_key:
                    print('Import skipped, public keys are the same')
                    return True
        return False

    def _handle_incoming_message(self, message, session):
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageIsParsed', sender=session, data=message)

        if message is not None and 'positive-delivery' in message.disposition:
            print("-- Should send delivered imdn")
            self.send_imdn_message(session, message.id, message.timestamp, 'delivered')

        self._add_contact_to_messages_group(session)
        notification_center.post_notification('BlinkGotMessage', sender=session, data=message)

    def _send_message(self, outgoing_message):
        self._outgoing_message_queue.append(outgoing_message)
        self._send_outgoing_messages()

    def _send_outgoing_messages(self):
        while self._outgoing_message_queue:
            message = self._outgoing_message_queue.popleft()
            message.send()

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
        stream.generate_keys(session)

    def _SH_PGPRequestFinished(self, request):
        request.dialog.hide()
        self.pgp_requests.remove(request)

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

        if x_replicated_message is Null:
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
            else:
                payload = SimplePayload.decode(data.body, data.content_type)
                body = payload.content.decode()
                content_type = payload.content_type
                sender = from_header
                disposition = None
                message_id = str(uuid.uuid4())

            encryption = self.check_encryption(content_type, body)
            if encryption == 'OpenPGP':
                if account.sms.enable_pgp and (account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized)):
                    if not self.pgp_requests[account, GeneratePGPKeyRequest]:
                        generate_dialog = GeneratePGPKeyDialog()
                        generate_request = GeneratePGPKeyRequest(generate_dialog, account, 0)
                        generate_request.accepted.connect(self._SH_GeneratePGPKeys)
                        generate_request.finished.connect(self._SH_PGPRequestFinished)
                        bisect.insort_right(self.pgp_requests, generate_request)
                        generate_request.dialog.show()
                elif not account.sms.enable_pgp:
                    return

            if content_type.lower() == 'text/pgp-private-key':
                if not account.sms.enable_pgp:
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

            if content_type.lower() == 'text/pgp-public-key':
                # print('-- Received public key')
                self._save_pgp_key(body, sender.uri)

            from blink.contacts import URIUtils
            contact, contact_uri = URIUtils.find_contact(sender.uri)
            session_manager = SessionManager()

            notification_center = NotificationCenter()

            timestamp = str(cpim_message.timestamp) if cpim_message is not None and cpim_message.timestamp is not None else str(ISOTimestamp.now())
            message = BlinkMessage(body, content_type, sender, timestamp=timestamp, id=message_id, disposition=disposition, direction='incoming')
            try:
                blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)
            except StopIteration:
                blink_session = None
                if content_type.lower() in self.__ignored_content_types__:
                    print("Skipping session")
                    if content_type.lower() != IMDNDocument.content_type:
                        return
                else:
                    blink_session = session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False)
            else:
                if blink_session.fake_streams.get('messages') is None:
                    stream = StreamDescription('messages')
                    blink_session.fake_streams.extend([stream.create_stream()])
                    blink_session._delete_when_done = False
                    if account.sms.enable_pgp and account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized):
                        blink_session.fake_streams.get('messages').enable_pgp()
                    notification_center.post_notification('BlinkSessionWillAddStream', sender=blink_session, data=NotificationData(stream=stream))

            if account.sms.use_cpim and account.sms.enable_imdn and content_type.lower() == IMDNDocument.content_type:
                document = IMDNDocument.parse(body)
                imdn_message_id = document.message_id.value
                imdn_status = document.notification.status.__str__()
                imdn_datetime = document.datetime.__str__()
                notification_center.post_notification('BlinkGotDispositionNotification', sender=blink_session, data=NotificationData(id=imdn_message_id, status=imdn_status))
                return
            elif content_type.lower() == IMDNDocument.content_type:
                return
            if content_type.lower() in ['text/pgp-public-key', 'text/pgp-private-key']:
                notification_center.post_notification('PGPKeysShouldReload', sender=blink_session)
                return

            if content_type.lower() == IsComposingDocument.content_type:
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


            if encryption == 'OpenPGP':
                if blink_session.fake_streams.get('messages').can_decrypt:
                    blink_session.fake_streams.get('messages').decrypt(message)
                else:
                    self._incoming_encrypted_message_queue.append((message, account, contact))
                    notification_center.post_notification('BlinkMessageIsParsed', sender=blink_session, data=message)
                    self._add_contact_to_messages_group(blink_session.account, blink_session.contact)
                    notification_center.post_notification('BlinkGotMessage', sender=blink_session, data=message)
                return

            self._handle_incoming_message(message, blink_session)
        else:
            # TODO handle replicated messages
            pass

    def _NH_BlinkSessionWasCreated(self, notification):
        session = notification.sender
        self.sessions.append(session)

    def _NH_BlinkSessionWasDeleted(self, notification):
        session = notification.sender
        self.sessions.remove(session)
        for request in self.pgp_requests[session.account, GeneratePGPKeyRequest]:
            request.dialog.hide()
            self.pgp_requests.remove(request)

    def _NH_BlinkSessionNewOutgoing(self, notification):
        session = notification.sender
        stream = session.fake_streams.get('messages')

        if stream is None:
            return

        if session.account.sms.enable_pgp and (session.account.sms.private_key is None or not os.path.exists(session.account.sms.private_key.normalized)):
            for request in self.pgp_requests[session.account, GeneratePGPKeyRequest]:
                return

            generate_dialog = GeneratePGPKeyDialog()
            generate_request = GeneratePGPKeyRequest(generate_dialog, session.account, 1, session)
            generate_request.accepted.connect(self._SH_GeneratePGPKeys)
            generate_request.finished.connect(self._SH_PGPRequestFinished)
            bisect.insort_right(self.pgp_requests, generate_request)
            generate_request.dialog.show()

        elif session.account.sms.enable_pgp:
            stream.enable_pgp()

    def _NH_PGPKeysDidGenerate(self, notification):
        session = notification.sender

        outgoing_message = OutgoingMessage(session.account, session.contact, str(notification.data.public_key), 'text/pgp-public-key', session=session)
        self._send_message(outgoing_message)

    def _NH_PGPMessageDidDecrypt(self, notification):
        if not isinstance(notification.data.message, BlinkMessage):
            return

        session = notification.sender
        notification.data.message.is_secure = True

        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidDecrypt', sender=session, data=NotificationData(message=notification.data.message))
        self._handle_incoming_message(notification.data.message, session)

    def _NH_PGPMessageDidNotDecrypt(self, notification):
        session = notification.sender
        data = notification.data.message

        self.send_imdn_message(session, data.message_id, data.timestamp, 'error')

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

    def send_composing_indication(self, session, state, refresh=None, last_active=None):
        if not session.account.sms.enable_iscomposing:
            return

        content = IsComposingDocument.create(state=State(state),
                                             refresh=Refresh(refresh) if refresh is not None else None,
                                             last_active=LastActive(last_active) if last_active is not None else None,
                                             content_type=ContentType('text'))

        outgoing_message = OutgoingMessage(session.account, session.contact, content, IsComposingDocument.content_type, session=session)
        self._send_message(outgoing_message)

    def send_imdn_message(self, session, id, timestamp, state):
        if not session.account.sms.use_cpim and not session.account.sms.enable_imdn:
            return

        # print(f"-- Will send imdn for {id} -> {state}")
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

        outgoing_message = OutgoingMessage(session.account, session.contact, content, IMDNDocument.content_type, session=session)
        self._send_message(outgoing_message)

    def send_message(self, account, contact, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None):
        blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)

        outgoing_message = OutgoingMessage(account, contact, content, content_type, recipients, courtesy_recipients, subject, timestamp, required, additional_headers, id, blink_session)
        self._send_message(outgoing_message)
        self._add_contact_to_messages_group(blink_session.account, blink_session.contact)

    def create_message_session(self, uri):
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(uri)
        session_manager = SessionManager()
        account = AccountManager().default_account

        try:
            next(session for session in self.sessions if session.contact.settings is contact.settings)
        except StopIteration:
            session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False)
