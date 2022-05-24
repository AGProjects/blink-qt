import uuid

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.account import Account, AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, FromHeader, ToHeader, Message, RouteHeader
from sipsimple.lookup import DNSLookup
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.payloads.imdn import IMDNDocument
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError, CPIMNamespace, CPIMHeader, ChatIdentity, Message as MSRPChatMessage, SimplePayload
from sipsimple.util import ISOTimestamp

from blink.sessions import SessionManager, StreamDescription
from blink.util import run_in_gui_thread


__all__ = ['MessageManager', 'BlinkMessage']


class BlinkMessage(MSRPChatMessage):
    __slots__ = 'id', 'disposition'

    def __init__(self, content, content_type, sender=None, recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None, disposition=None):
        super(BlinkMessage, self).__init__(content, content_type, sender, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)
        self.id = id if id is not None else str(uuid.uuid4())
        self.disposition = disposition


@implementer(IObserver)
class OutgoingMessage(object):
    __ignored_content_types__ = {IsComposingDocument.content_type, IMDNDocument.content_type} #Content types to ignore in notifications

    def __init__(self, account, contact, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None):
        self.lookup = None
        self.account = account
        self.uri = contact.uri.uri
        self.content_type = content_type
        self.content = content
        self.id = id if id is not None else str(uuid.uuid4())
        self.timestamp = timestamp if timestamp is not None else ISOTimestamp.now()
        self.sip_uri = SIPURI.parse('sip:%s' % self.uri)

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

    def send(self, session):
        self.session = session
        if self.content_type.lower() not in self.__ignored_content_types__:
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
        notification_center.post_notification('BlinkMessageDidSucceed', sender=self.session, data=NotificationData(data=notification.data, id=self.id))

    def _NH_SIPMessageDidFail(self, notification):
        if self.content_type.lower() in self.__ignored_content_types__:
            return
        notification_center = NotificationCenter()
        notification_center.post_notification('BlinkMessageDidFail', sender=self.session, data=NotificationData(data=notification.data, id=self.id))


@implementer(IObserver)
class MessageManager(object, metaclass=Singleton):
    def __init__(self):
        self.sessions = []
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPEngineGotMessage')
        notification_center.add_observer(self, name='BlinkSessionWasCreated')

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

            from blink.contacts import URIUtils
            contact, contact_uri = URIUtils.find_contact(sender.uri)
            session_manager = SessionManager()

            notification_center = NotificationCenter()
            try:
                blink_session = next(session for session in self.sessions if session.reusable and session.contact.settings is contact.settings)
            except StopIteration:
                if content_type.lower() in [IsComposingDocument.content_type, IMDNDocument.content_type]:
                    return
                else:
                    blink_session = session_manager.create_session(contact, contact_uri, [StreamDescription('message')], account=account, connect=False)

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
            message = BlinkMessage(body, content_type, sender, timestamp=timestamp, id=message_id)

            notification_center.post_notification('BlinkGotMessage', sender=blink_session, data=message)
        else:
            pass
            # TODO handle replicated messages

    def _NH_BlinkSessionWasCreated(self, notification):
        self.sessions.append(notification.sender)
        notification.center.add_observer(self, sender=notification.sender)

    def send_composing_indication(self, session, state, refresh=None, last_active=None):
        if not session.account.sms.enable_iscomposing:
            return

        content = IsComposingDocument.create(state=State(state),
                                             refresh=Refresh(refresh) if refresh is not None else None,
                                             last_active=LastActive(last_active) if last_active is not None else None, 
                                             content_type=ContentType('text'))

        self.send_message(session.account, session.contact, content, IsComposingDocument.content_type)

    def send_message(self, account, contact, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None):
        blink_session = next(session for session in self.sessions if session.reusable and session.contact.settings is contact.settings)

        outgoing_message = OutgoingMessage(account, contact, content, content_type, recipients, courtesy_recipients, subject, timestamp, required, additional_headers, id)
        outgoing_message.send(blink_session)
