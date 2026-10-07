"""MessagePane: the right side of the main window, next to the contact list.

The contact list is the conversation switcher; this shows the conversation.
It follows the selection in the contact list (it never opens because of it):
one contact selected shows that contact's conversation, anything else the
empty state. A conversation has its header (blink.messagepane.header) and its
transcript (ConversationModel in TranscriptView); the composer comes later
(docs/messaging/ui-plan.md, B6). The models of the last few conversations are
kept with the pages they loaded, so going back to one does not query history again.

A conversation is being read while it is selected here, the pane is shown and
the window is active (and not minimised). Becoming read marks its incoming
messages read in history, clears its badge, sends the displayed notifications
the sender asked for and tells this account's other devices; while it is not
being read nothing is marked, and messages arriving stay unread.
"""

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implementer

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.logging import ActivityLog, MessagingTrace as log
from blink.messagepane.header import ConversationHeader
from blink.messagepane.model import ConversationModel
from blink.messagepane.strip import TranscriptStrip
from blink.messagepane.view import TranscriptView
from blink.util import call_in_gui_thread, run_in_gui_thread, translate
from blink.widgets.color import follow_theme, secondary_text_color


__all__ = ['MessagePane']


@implementer(IObserver)
class MessagePane(QWidget):
    kept_conversations = 8

    minimum_width = 320
    default_width = 480

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('message_pane')
        self.setMinimumWidth(self.minimum_width)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = ConversationHeader(self)
        self.header.hide()
        layout.addWidget(self.header)
        self.strip = TranscriptStrip(self)
        self.strip.hide()
        layout.addWidget(self.strip)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)

        self.empty_label = QLabel(translate('message_pane', 'Select a contact to see messages'), self.stack)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setMargin(24)
        self.stack.addWidget(self.empty_label)
        self.transcript = TranscriptView(self.stack)
        self.stack.addWidget(self.transcript)
        self.transcript.verticalScrollBar().valueChanged.connect(self.strip.update_text)
        self.models = {}            # conversation key: ConversationModel, most recent last
        self.stack.setCurrentWidget(self.empty_label)

        self.contact = None
        self.uri = None
        self.key = None
        self._displayed_sent = set()     # message ids a displayed notification went out for
        self._read_timer = QTimer(self)
        self._read_timer.setSingleShot(True)
        self._read_timer.setInterval(200)
        self._read_timer.timeout.connect(self._read)
        self._model_connected = None

        self.apply_theme()
        follow_theme(self)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkConversationPreviewsDidChange')
        notification_center.add_observer(self, name='PGPKeysShouldReload')
        notification_center.add_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')

    def apply_theme(self):
        palette = self.empty_label.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, QPalette.ColorRole.WindowText, secondary_text_color(self.palette(), group))
        self.empty_label.setPalette(palette)

    def show_conversation(self, contact, uri, key):
        """Switch to the conversation with a contact, on one of its addresses (key: its conversation key)."""
        if contact is self.contact and key == self.key:
            return
        self.contact, self.uri, self.key = contact, uri, key
        self.header.set_conversation(contact, uri, key, self._default_account())
        self.header.show()
        cached = key in self.models
        self.transcript.bubble_delegate.peer_avatar = self.header.avatar.draw
        model = self._model(key)
        self._follow_model(model)
        self.transcript.setModel(model)
        self.strip.set_conversation(model, self.transcript)
        self.strip.show()
        self.stack.setCurrentWidget(self.transcript)
        self._find_account(key)
        model = self.models[key]
        how = f'{len(model.items)} messages already loaded' if cached and model.loaded else 'loading'
        ActivityLog().info(f'[Message with {key}] Conversation selected in the message pane ({uri.uri}, {how})')

    def clear(self):
        """No conversation: the empty state."""
        if self.contact is None:
            return
        self.contact = self.uri = self.key = None
        self._follow_model(None)
        self.header.hide()
        self.strip.hide()
        self.transcript.setModel(None)
        self.stack.setCurrentWidget(self.empty_label)

    def _model(self, key):
        model = self.models.pop(key, None)
        if model is None:
            model = ConversationModel(key, self)
            model.load()
        self.models[key] = model
        while len(self.models) > self.kept_conversations:
            oldest = next(iter(self.models))
            dropped = self.models.pop(oldest)
            dropped.close()
            dropped.deleteLater()
        return model

    # The conversation's account: the one its newest message was on

    def _default_account(self):
        if self.key and self.contact is not None and getattr(self.contact, 'type', None) == 'bonjour':
            return BonjourAccount()
        from blink.uris import is_instance_id
        if is_instance_id(self.key or ''):
            return BonjourAccount()
        return AccountManager().default_account

    @run_in_thread('db')
    def _find_account(self, key):
        from blink.history import MessageHistory
        try:
            account_id = MessageHistory().last_message_accounts(remote_uri=key).get(key)
        except Exception as e:
            log.warning(f'Cannot find the account of the conversation with {key}: {e}')
            return
        if account_id:
            call_in_gui_thread(self._set_account, key, account_id)

    def _set_account(self, key, account_id):
        if key != self.key:
            return
        from blink.uris import BONJOUR_ACCOUNT_ID
        if account_id == BONJOUR_ACCOUNT_ID:
            account = BonjourAccount()
        else:
            try:
                account = AccountManager().get_account(account_id)
            except KeyError:
                return
            if not account.enabled:
                return
        self.header.set_account(account)

    # Notifications

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkConversationPreviewsDidChange(self, notification):
        keys = notification.data.keys
        if self.key is not None and (keys is None or self.key in keys):
            self.header.update_info()

    def _NH_PGPKeysShouldReload(self, notification):
        self.header.update_lock()

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        if self.contact is not None:
            self.header.set_account(self._default_account())
            self._find_account(self.key)

    # Read state

    def _follow_model(self, model):
        if self._model_connected is not None:
            for signal in (self._model_connected.initialLoadFinished, self._model_connected.rowsInserted, self._model_connected.dataChanged):
                try:
                    signal.disconnect(self.check_read)
                except TypeError:
                    pass
        self._model_connected = model
        if model is not None:
            for signal in (model.initialLoadFinished, model.rowsInserted, model.dataChanged):
                signal.connect(self.check_read)
            self.check_read()

    def is_reading(self):
        """Whether the user has the conversation in front of them."""
        window = self.window()
        return (self.key is not None and self.isVisible() and window.isActiveWindow()
                and not window.isMinimized() and self.stack.currentWidget() is self.transcript)

    def check_read(self, *args):
        """Mark what is shown read, soon, if it is being read (called on every change that may make it so)."""
        if self.is_reading():
            self._read_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.check_read()

    def _read(self):
        if not self.is_reading():
            return
        model = self.models.get(self.key)
        if model is None or not model.loaded or model.search_text:
            return
        unread = [item for item in model.items if item.direction == 'incoming' and not item.read]
        main_window = self.window()
        badge = getattr(main_window, 'unread_messages', {}).get(self.key, 0)
        if not unread and not badge:
            return
        key = self.key
        from blink.history import MessageHistory
        MessageHistory().mark_conversation_read(key)
        NotificationCenter().post_notification('BlinkMessagePaneDidReadConversation', sender=self, data=NotificationData(remote_uri=key))
        ActivityLog().info(f'[Message with {key}] Read in the message pane: {len(unread)} unread messages shown' + (f', badge was {badge}' if badge else ''))

        displayed = [item for item in unread if 'display' in item.disposition and item.id not in self._displayed_sent
                     and '-----BEGIN PGP MESSAGE-----' not in str(item.content or '')]
        if not unread and not displayed:
            return
        from blink.messages import MessageManager
        manager = MessageManager()
        try:
            session = manager.create_message_session(str(self.uri.uri), selected=False)
        except Exception as e:
            log.warning(f'Cannot reach the conversation with {key} to confirm reading it: {e!r}')
            return
        for item in displayed:
            account = self._account(item.account_id) or session.account
            self._displayed_sent.add(item.id)
            manager.send_imdn_message(session, item.id, ISOTimestamp(item.timestamp), 'displayed', account)
        if unread:
            manager.send_conversation_read(session)

    @staticmethod
    def _account(account_id):
        from blink.uris import BONJOUR_ACCOUNT_ID
        if account_id == BONJOUR_ACCOUNT_ID:
            return BonjourAccount()
        try:
            account = AccountManager().get_account(account_id)
        except KeyError:
            return None
        return account if account.enabled else None
