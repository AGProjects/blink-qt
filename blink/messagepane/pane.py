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

At the bottom the composer (blink.messagepane.composer): Enter sends the text
on the conversation's account, typing tells the peer, unsent text is kept per
conversation, files dropped anywhere on the conversation (or pasted) are sent.
A−/A+ in the strip set the text size of the transcript and the composer,
kept across restarts.
"""

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implementer

from PyQt6.QtCore import Qt, QSettings, QTimer
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.logging import ActivityLog, MessagingTrace as log
from blink.messagepane.composer import Composer
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
        self.header.dayChosen.connect(self._jump_to)
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
        self.transcript.actionRequested.connect(self._SH_ActionRequested)
        self.transcript.quoteClicked.connect(self._SH_QuoteClicked)
        self.composer = Composer(self)
        self.composer.hide()
        layout.addWidget(self.composer)
        self.composer.sendText.connect(self._send_text)
        self.composer.filesDropped.connect(self._send_files)
        self.composer.composing.connect(self._send_composing)
        self.header.fontStep.connect(self._step_font)
        self.unsent = {}            # conversation key: text typed and not sent
        self.setAcceptDrops(True)
        self._apply_font()
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
        self._pending = None         # (contact, uri, key) selected while the pane was closed
        self._calendar_timer = QTimer(self)
        self._calendar_timer.setSingleShot(True)
        self._calendar_timer.setInterval(2000)
        self._calendar_timer.timeout.connect(lambda: self.key and self._load_day_counts(self.key))

        self.apply_theme()
        follow_theme(self)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkConversationPreviewsDidChange')
        notification_center.add_observer(self, name='PGPKeysShouldReload')
        notification_center.add_observer(self, name='BlinkMessageHistoryMessageDidStore')
        notification_center.add_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')

    def apply_theme(self):
        palette = self.empty_label.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, QPalette.ColorRole.WindowText, secondary_text_color(self.palette(), group))
        self.empty_label.setPalette(palette)

    def show_conversation(self, contact, uri, key):
        """Switch to the conversation with a contact, on one of its addresses (key: its conversation key).
        With the pane closed nothing is loaded: the conversation is shown when the pane opens."""
        if not self.isVisible():
            self._pending = (contact, uri, key)
            return
        self._pending = None
        if contact is self.contact and key == self.key:
            return
        self._keep_unsent()
        self.contact, self.uri, self.key = contact, uri, key
        self.composer.set_text(self.unsent.get(key, ''))
        self.composer.show()
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
        self._load_day_counts(key)
        model = self.models[key]
        how = f'{len(model.items)} messages already loaded' if cached and model.loaded else 'loading'
        ActivityLog().info(f'[Message with {key}] Conversation selected in the message pane ({uri.uri}, {how})')

    def clear(self):
        """No conversation: the empty state."""
        self._pending = None
        if self.contact is None:
            return
        self._keep_unsent()
        self.composer.hide()
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

    def _NH_BlinkMessageHistoryMessageDidStore(self, notification):
        if self.key is not None and str(notification.data.remote_uri) == self.key:
            self._calendar_timer.start()

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
        if self._pending is not None:
            self.show_conversation(*self._pending)
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

    # Calendar

    @run_in_thread('db')
    def _load_day_counts(self, key):
        from blink.history import MessageHistory
        try:
            counts = MessageHistory().day_counts(key)
        except Exception as e:
            log.warning(f'Cannot count the days of the conversation with {key}: {e!r}')
            return
        call_in_gui_thread(self.header.set_day_counts, key, counts)

    def _jump_to(self, day):
        model = self.models.get(self.key)
        if model is None:
            return
        model.jump_to(day)
        self.strip.set_conversation(model, self.transcript)     # a search gives way to the jump

    # Composer

    def _keep_unsent(self):
        if self.key is None:
            return
        text = self.composer.text()
        if text.strip():
            self.unsent[self.key] = text
        else:
            self.unsent.pop(self.key, None)

    def _message_session(self):
        from blink.messages import MessageManager
        return MessageManager().create_message_session(str(self.uri.uri), selected=False)

    def _send_text(self, text):
        if self.key is None:
            return
        import uuid
        from blink.messages import MessageManager
        try:
            session = self._message_session()
            account = self.header.account or session.account
            message_id = str(uuid.uuid4())
            reply = self.composer.reply
            if reply is not None:
                # the link first, so the peer has it in hand when the reply arrives (as mobile and Blink for macOS do)
                from blink.message_envelopes import METADATA_CONTENT_TYPE, reply_envelope
                metadata_id = str(uuid.uuid4())
                envelope = reply_envelope(message_id, reply['id'], metadata_id, str(self.uri.uri), ISOTimestamp.now())
                MessageManager().send_message(account, session.contact, envelope, METADATA_CONTENT_TYPE, id=metadata_id)
                ActivityLog().info(f'[Message with {self.key}] Replying to message {reply["id"]} with {message_id}')
            MessageManager().send_message(account, session.contact, text, 'text/plain', id=message_id)
        except Exception as e:
            ActivityLog().error(f'[Message with {self.key}] Sending a message from the message pane failed: {e!r}')
            self.composer.set_text(text)      # nothing lost
            return
        self.unsent.pop(self.key, None)
        model = self.models.get(self.key)
        if model is not None and (model.has_newer or model.search_text):
            model.search_text = ''
            model.load()                      # back to the newest, where the message goes
            self.strip.set_conversation(model, self.transcript)
        self.transcript.follow_bottom()

    def _send_composing(self, state):
        if self.key is None:
            return
        from blink.messages import MessageManager
        try:
            MessageManager().send_composing_indication(self._message_session(), state)
        except Exception as e:
            log.warning(f'Cannot tell {self.key} about typing: {e!r}')

    def _send_files(self, paths):
        if self.key is None or not paths:
            return
        import os
        from blink.sessions import SessionManager
        session = self._message_session()
        account = self.header.account or session.account
        for path in paths:
            if os.path.isfile(path):
                SessionManager().send_file(session.contact, session.contact_uri, path, account=account)
                ActivityLog().info(f'[Message with {self.key}] Sending {os.path.basename(path)} from the message pane')
        self.transcript.follow_bottom()

    def dragEnterEvent(self, event):
        if self.key is not None and event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        event.acceptProposedAction()
        self._send_files(paths)

    # Text size

    font_steps = (-3, 8)        # points smaller / larger than the system font

    def _font_delta(self):
        try:
            return int(QSettings().value('message_pane/font_delta', 0))
        except (TypeError, ValueError):
            return 0

    def _step_font(self, step):
        low, high = self.font_steps
        delta = max(low, min(high, self._font_delta() + step))
        QSettings().setValue('message_pane/font_delta', delta)
        self._apply_font()

    def _apply_font(self):
        from PyQt6.QtWidgets import QApplication
        font = QApplication.font()
        if font.pointSizeF() > 0:
            font.setPointSizeF(max(font.pointSizeF() + self._font_delta(), 6))
        self.transcript.setFont(font)
        self.composer.edit.setFont(font)

    # Message actions

    def _SH_ActionRequested(self, action, item):
        if action == 'delete':
            self._delete_message(item)
        elif action == 'reply':
            from blink.messagepane.format import plain_summary
            name = translate('message_pane', 'yourself') if item.outgoing else (getattr(self.contact, 'name', '') or item.display_name or self.key)
            self.composer.set_reply({'id': item.id, 'name': name, 'text': plain_summary(item)})

    def _SH_QuoteClicked(self, reply):
        """Go to the message a reply answers: in place when loaded, else load the day it is from."""
        if self.transcript.show_message(reply['id']):
            return
        model = self.models.get(self.key)
        if model is None or reply.get('timestamp') is None:
            return
        def landed(row, message_id=reply['id']):
            model.jumped.disconnect(landed)
            QTimer.singleShot(50, lambda: self.transcript.show_message(message_id))
        model.jumped.connect(landed)
        model.jump_to(reply['timestamp'].astimezone().date())
        self.strip.set_conversation(model, self.transcript)

    def _delete_message(self, item):
        """Delete a message here, after asking; one's own message also for the other party
        when asked to (and possible: not a Bonjour neighbour, not a message never sent).
        A file transfer takes its downloaded file with it."""
        from PyQt6.QtWidgets import QCheckBox, QMessageBox
        from blink.history import HistoryManager, MessageHistory
        from blink.messagepane.files import local_file
        from blink.uris import BONJOUR_ACCOUNT_ID
        key = self.key
        is_file = local_file(item) is not None or item.category in ('image', 'audio', 'video', 'other')
        what = translate('message_pane', 'file') if is_file else translate('message_pane', 'message')
        box = QMessageBox(QMessageBox.Icon.Question, translate('message_pane', 'Delete %s') % what,
                          translate('message_pane', 'Delete this %s from this conversation?') % what, parent=self)
        delete_button = box.addButton(translate('message_pane', 'Delete'), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        both = None
        if item.outgoing and item.account_id != BONJOUR_ACCOUNT_ID and item.state not in ('failed-local', 'pending'):
            name = getattr(self.contact, 'name', '') or key
            both = QCheckBox(translate('message_pane', 'Delete it for %s too') % name)
            both.setChecked(True)
            box.setCheckBox(both)
        box.exec()
        if box.clickedButton() is not delete_button:
            return
        for_both = both is not None and both.isChecked()
        MessageHistory().tombstone_message(item.id, account_id=item.account_id, remote_uri=key, source='deleted here')
        HistoryManager().download_history.remove(item.id)          # the downloaded file goes with it
        ActivityLog().info(f'[Message with {key}] Deleted {what} {item.id} from the message pane' + (', also for the other party' if for_both else ''))
        if for_both:
            from blink.messages import MessageManager
            try:
                session = self._message_session()
                account = self._account(item.account_id) or session.account
                MessageManager().send_remove_message(session, item.id, account)
            except Exception as e:
                ActivityLog().warning(f'[Message with {key}] Cannot ask the other party to delete message {item.id}: {e!r}')
        model = self.models.get(key)
        if model is not None:
            model.remove_item(item.id)
