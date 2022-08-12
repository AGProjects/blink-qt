import os
import re
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
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.payloads.imdn import IMDNDocument, DeliveryNotification, DisplayNotification
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError, CPIMNamespace, CPIMHeader, ChatIdentity, Message as MSRPChatMessage, SimplePayload
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.resources import Resources
from blink.sessions import SessionManager, StreamDescription, IncomingDialogBase
from blink.util import run_in_gui_thread


__all__ = ['MessageManager', 'BlinkMessage']


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


class BlinkMessage(MSRPChatMessage):
    __slots__ = 'id', 'disposition'

    def __init__(self, content, content_type, sender=None, recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None, disposition=None):
        super(BlinkMessage, self).__init__(content, content_type, sender, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)
        self.id = id if id is not None else str(uuid.uuid4())
        self.disposition = disposition


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

    @property
    def message(self):
        return BlinkMessage(self.content, self.content_type, self.account, timestamp=self.timestamp, id=self.id)

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

    def _send(self):
        if self.session.routes:
            from_uri = self.account.uri
            content = self.content if isinstance(self.content, bytes) else self.content.encode()
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

            route = self.session.routes[0]
            message_request = Message(FromHeader(from_uri, self.account.display_name),
                                      ToHeader(self.sip_uri),
                                      RouteHeader(route.uri),
                                      content_type,
                                      payload,
                                      credentials=self.account.credentials,
                                      extra_headers=additional_sip_headers)
            notification_center = NotificationCenter()
            notification_center.add_observer(self, sender=message_request)
            message_request.send()
        else:
            pass
            # TODO

    def send(self):
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
            self.session.routes = routes
            self._send()

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self.content_type.lower() == IsComposingDocument.content_type:
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

        notification_center.post_notification('BlinkMessageDidSucceed', sender=self.session, data=NotificationData(data=notification.data, id=self.id))

    def _NH_SIPMessageDidFail(self, notification):
        if self.content_type.lower() in self.__ignored_content_types__:
            return

        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=NotificationData(data=notification.data, id=self.id))


@implementer(IObserver)
class MessageManager(object, metaclass=Singleton):
    __ignored_content_types__ = {IsComposingDocument.content_type, IMDNDocument.content_type, 'text/pgp-public-key', 'text/pgp-private-key'}

    def __init__(self):
        self.sessions = []
        self._outgoing_message_queue = deque()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPEngineGotMessage')
        notification_center.add_observer(self, name='BlinkSessionWasCreated')
        notification_center.add_observer(self, name='BlinkSessionWasDeleted')

    def _add_contact_to_messages_group(self, session):  # Maybe this needs to be placed in Contacts? -- Tijmen
        if not session.account.sms.add_unknown_contacts:
            return

        if session.contact.type not in ['dummy', 'unknown']:
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

        contact = Contact()
        contact.name = session.contact.name
        contact.preferred_media = session.contact.preferred_media
        contact.uris = [ContactURI(uri=uri.uri, type=uri.type) for uri in session.contact.uris]
        contact.save()

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

            if (content_type.lower().startswith('text/') and
                '-----BEGIN PGP MESSAGE-----' in body and
                body.strip().endswith('-----END PGP MESSAGE-----') and
                content_type != 'text/pgp-private-key'):
                return

            if content_type.lower() == 'text/pgp-public-key':
                # print('-- Received public key')
                self.save_key(body, sender.uri)

            from blink.contacts import URIUtils
            contact, contact_uri = URIUtils.find_contact(sender.uri)
            session_manager = SessionManager()

            notification_center = NotificationCenter()
            try:
                blink_session = next(session for session in self.sessions if session.contact.settings is contact.settings)
            except StopIteration:
                if content_type.lower() in self.__ignored_content_types__:
                    return
                else:
                    blink_session = session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False)

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

            timestamp = str(cpim_message.timestamp) if cpim_message is not None and cpim_message.timestamp is not None else str(ISOTimestamp.now())

            if account.sms.use_cpim and account.sms.enable_imdn and content_type.lower() == IMDNDocument.content_type:
                # print("-- IMDN received")
                document = IMDNDocument.parse(body)
                imdn_message_id = document.message_id.value
                imdn_status = document.notification.status.__str__()
                imdn_datetime = document.datetime.__str__()
                notification_center.post_notification('BlinkGotDispositionNotification', sender=blink_session, data=NotificationData(id=imdn_message_id, status=imdn_status))
                return

            message = BlinkMessage(body, content_type, sender, timestamp=timestamp, id=message_id, disposition=disposition)
            notification_center.post_notification('BlinkMessageIsParsed', sender=blink_session, data=message)

            if disposition is not None and 'positive-delivery' in disposition:
                # print("-- Should send delivered imdn")
                self.send_imdn_message(blink_session, message_id, timestamp, 'delivered')

            self._add_contact_to_messages_group(blink_session)
            notification_center.post_notification('BlinkGotMessage', sender=blink_session, data=message)
        else:
            # TODO handle replicated messages
            pass

    def _NH_BlinkSessionWasCreated(self, notification):
        session = notification.sender
        self.sessions.append(session)

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.sessions.remove(notification.sender)

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
        self._add_contact_to_messages_group(blink_session)

    def create_message_session(self, uri):
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(uri)
        session_manager = SessionManager()
        account = AccountManager().default_account

        try:
            next(session for session in self.sessions if session.contact.settings is contact.settings)
        except StopIteration:
            session_manager.create_session(contact, contact_uri, [StreamDescription('messages')], account=account, connect=False)
