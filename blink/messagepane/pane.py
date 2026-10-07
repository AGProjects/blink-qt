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
A file sent over HTTP is a bubble here while it uploads (blink.messagepane.uploads);
the File Transfers window is for MSRP transfers only.
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
        self.header.locationAction.connect(self._location_action)
        self.header.addressChosen.connect(self._SH_AddressChosen)
        self.header.accountChosen.connect(self._SH_AccountChosen)
        self.chosen_accounts = {}           # conversation key: account id the user chose to send from
        layout.addWidget(self.header)
        self.strip = TranscriptStrip(self)
        self.strip.hide()
        layout.addWidget(self.strip)
        from blink.messagepane.filters import FilterBar
        self.filters = FilterBar(self)
        layout.addWidget(self.filters)
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
        self.transcript.audioAction.connect(self._SH_AudioAction)
        from blink.messagepane.uploads import Uploads
        Uploads.instance()          # files sent over HTTP are shown here, from the first one
        from blink.messagepane.fetch import AutoFetcher
        self.fetcher = AutoFetcher(self)
        self.fetcher.changed.connect(self._SH_DownloadChanged)
        self.transcript.bubble_delegate.progress_of = self.fetcher.progress
        self.transcript.verticalScrollBar().valueChanged.connect(self.fetcher.schedule)
        # pictures, videos and locations as tiles (blink.messagepane.grid), when the filter shows one of them
        from blink.messagepane.grid import GridView
        self.grid = GridView(self.stack)
        self.stack.addWidget(self.grid)
        self.grid.progress_of = self.fetcher.progress
        self.grid.actionRequested.connect(self._SH_ActionRequested)
        self.grid.deleteRequested.connect(self._delete_messages)
        self.grid.forwardRequested.connect(self._forward)
        self._forward_after_download = {}       # message id: (item, conversation key), sent once its file is here
        self.grid.verticalScrollBar().valueChanged.connect(self.fetcher.schedule)
        self._make_grid_controls()
        self.composer = Composer(self)
        self.composer.hide()
        layout.addWidget(self.composer)
        self.composer.sendText.connect(self._send_text)
        self.composer.filesDropped.connect(self._send_files)
        self.composer.voiceNote.connect(self._send_voice_note)
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
        chosen = self._account(self.chosen_accounts[key]) if key in self.chosen_accounts else None
        self.header.set_conversation(contact, uri, key, chosen or self._default_account())
        self.header.show()
        cached = key in self.models
        self.transcript.bubble_delegate.peer_avatar = self.header.avatar.draw
        model = self._model(key)
        self._follow_model(model)
        self.transcript.setModel(model)
        self.strip.set_conversation(model, self.transcript)
        self.strip.show()
        self.filters.set_conversation(model)
        self.stack.setCurrentWidget(self.transcript)
        self._update_mode()
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
        self.filters.set_conversation(None)
        self.grid.set_model(None)
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

    def _SH_AddressChosen(self, uri):
        """Another address of the contact: its own conversation."""
        from blink.contacts import _conversation_key
        key = _conversation_key(str(uri.uri), AccountManager().default_account)
        ActivityLog().info(f'[Message with {key}] Switched from {self.key} to the address {uri.uri}')
        self.show_conversation(self.contact, uri, key)

    def _SH_AccountChosen(self, account):
        if self.key is None:
            return
        self.chosen_accounts[self.key] = account.id
        self.header.set_account(account)
        ActivityLog().info(f'[Message with {self.key}] Messages are sent from account {account.id} (chosen)')

    def _confirm_account(self):
        """Before the first message of a conversation: with several accounts and none of them in the
        other party's domain, ask which one to send from. False when the user cancelled."""
        key = self.key
        if key is None or key in self.chosen_accounts:
            return True
        account = self.header.account
        if account is None or account is BonjourAccount():
            return True
        model = self.models.get(key)
        if model is None or not model.loaded or model.items or model.has_more or model.category or model.search_text:
            return True             # not the first message (or not known yet)
        accounts = self.header.sending_accounts()
        if len(accounts) < 2:
            return True
        address = str(self.uri.uri).split(':', 1)[-1]
        domain = address.rpartition('@')[2].lower()
        same_domain = [candidate for candidate in accounts if str(candidate.id.domain).lower() == domain]
        if same_domain:
            chosen = same_domain[0]
        else:
            from PyQt6.QtWidgets import QInputDialog
            names = [str(candidate.id) for candidate in accounts]
            current = names.index(str(account.id)) if str(account.id) in names else 0
            name, accepted = QInputDialog.getItem(self, translate('message_pane', 'Send From'),
                                                  translate('message_pane', 'This is the first message to %s. Send it from which account?') % address,
                                                  names, current, False)
            if not accepted:
                ActivityLog().info(f'[Message with {key}] First message not sent: no account chosen')
                return False
            chosen = accounts[names.index(name)]
        self.chosen_accounts[key] = chosen.id
        self.header.set_account(chosen)
        ActivityLog().info(f'[Message with {key}] First message sent from account {chosen.id}' + (' (same domain)' if same_domain else ' (chosen)'))
        return True

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
        if key != self.key or key in self.chosen_accounts:
            return              # the user's choice stands
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
            self.filters.refresh_later()

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
            for signal in (self._model_connected.initialLoadFinished, self._model_connected.rowsInserted, self._model_connected.jumped):
                try:
                    signal.disconnect(self.fetcher.schedule)
                except TypeError:
                    pass
        self._model_connected = model
        if model is not None:
            for signal in (model.initialLoadFinished, model.rowsInserted, model.dataChanged):
                signal.connect(self.check_read)
            for signal in (model.initialLoadFinished, model.rowsInserted, model.jumped):
                signal.connect(self.fetcher.schedule)
            self.check_read()
            self.fetcher.schedule()

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
        if not self._confirm_account():
            self.composer.set_text(text)      # nothing lost
            return
        import uuid
        from blink.messages import MessageManager
        try:
            session = self._message_session()
            account = self.header.account or session.account
            message_id = str(uuid.uuid4())
            reply = self.composer.reply
            editing = self.composer.editing
            timestamp = None
            if editing is not None:
                # an edit is the old message removed (here and at the peer) and the new text sent in its place
                timestamp = ISOTimestamp(editing['timestamp'])
                account = self._account(editing['account_id']) or account
                self._remove_message(editing['id'], editing['account_id'], for_both=True, why='edited')
                ActivityLog().info(f'[Message with {self.key}] Message {editing["id"]} edited: sent again as {message_id} at its original time')
            if reply is not None:
                # the link first, so the peer has it in hand when the reply arrives (as mobile and Blink for macOS do)
                from blink.message_envelopes import METADATA_CONTENT_TYPE, reply_envelope
                metadata_id = str(uuid.uuid4())
                envelope = reply_envelope(message_id, reply['id'], metadata_id, str(self.uri.uri), ISOTimestamp.now())
                MessageManager().send_message(account, session.contact, envelope, METADATA_CONTENT_TYPE, id=metadata_id)
                ActivityLog().info(f'[Message with {self.key}] Replying to message {reply["id"]} with {message_id}')
            MessageManager().send_message(account, session.contact, text, 'text/plain', timestamp=timestamp, id=message_id)
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
        """Show the files first (blink.messagepane.attach): crop, caption, smaller pictures; Send sends them."""
        import os
        if self.key is None or not paths:
            return
        paths = [path for path in paths if os.path.isfile(path)]
        if not paths or not self._confirm_account():
            return
        from blink.messagepane.attach import AttachmentPreview
        key = self.key
        peer = getattr(self.contact, 'name', '') or key
        dialog = AttachmentPreview(paths, peer, self)
        if dialog.exec() != AttachmentPreview.DialogCode.Accepted or not dialog.plan():
            ActivityLog().info(f'[Message with {key}] Sending {len(paths)} file(s) cancelled in the preview')
            return
        if self.key != key:
            return          # the conversation changed while the preview was open
        import uuid
        from blink.message_envelopes import METADATA_CONTENT_TYPE, label_envelope
        from blink.messages import MessageManager
        from blink.sessions import SessionManager
        session = self._message_session()
        account = self.header.account or session.account
        for path, caption in dialog.plan():
            transfer_id = str(uuid.uuid4())
            try:
                SessionManager().send_file(session.contact, session.contact_uri, path, transfer_id=transfer_id, account=account)
            except Exception as e:
                ActivityLog().error(f'[Message with {key}] Sending {path} failed: {e!r}')
                continue
            ActivityLog().info(f'[Message with {key}] Sending {os.path.basename(path)} as {transfer_id} from the message pane' + (' with a caption' if caption else ''))
            if caption:
                try:
                    metadata_id = str(uuid.uuid4())
                    envelope = label_envelope(transfer_id, metadata_id, caption, str(self.uri.uri), ISOTimestamp.now())
                    MessageManager().send_message(account, session.contact, envelope, METADATA_CONTENT_TYPE, id=metadata_id)
                except Exception as e:
                    ActivityLog().error(f'[Message with {key}] Sending the caption of {transfer_id} failed: {e!r}')
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

    # The grid

    def _make_grid_controls(self):
        from PyQt6.QtWidgets import QSpinBox, QToolButton
        self.grid_button = QToolButton(self.filters)
        self.grid_button.setText(translate('message_pane', 'Grid'))
        self.grid_button.setToolTip(translate('message_pane', 'Show pictures, videos and locations as tiles'))
        self.grid_button.setCheckable(True)
        self.grid_button.setChecked(QSettings().value('message_pane/grid', True, type=bool))
        self.grid_button.toggled.connect(self._SH_GridToggled)
        self.columns_box = QSpinBox(self.filters)
        self.columns_box.setRange(self.grid.min_columns, self.grid.max_columns)
        self.columns_box.setValue(self.grid.columns)
        self.columns_box.setSuffix(translate('message_pane', ' columns'))
        self.columns_box.valueChanged.connect(self.grid.set_columns)
        self.download_button = QToolButton(self.filters)
        self.download_button.setText(translate('message_pane', 'Download All'))
        self.download_button.setToolTip(translate('message_pane', 'Download the videos in view'))
        self.download_button.clicked.connect(self._download_visible)
        self.select_button = QToolButton(self.filters)
        self.select_button.setText(translate('message_pane', 'Select'))
        self.select_button.setToolTip(translate('message_pane', 'Tick tiles to forward or delete them together'))
        self.select_button.setCheckable(True)
        self.select_button.toggled.connect(self.grid.set_selecting)
        self.grid.selectingChanged.connect(self.select_button.setChecked)
        for widget in (self.download_button, self.select_button, self.columns_box, self.grid_button):
            self.filters.add_extra(widget)
        self.filters.categoryChosen.connect(lambda category: self._update_mode())
        self.grid_button.hide()
        self.columns_box.hide()
        self.download_button.hide()
        self.select_button.hide()

    def _SH_GridToggled(self, checked):
        QSettings().setValue('message_pane/grid', checked)
        self._update_mode()

    def _update_mode(self):
        """Tiles for pictures, videos and locations (when the grid is on), else the transcript."""
        from blink.messagepane.grid import GRID_CATEGORIES
        model = self.models.get(self.key) if self.key is not None else None
        category = model.category if model is not None else None
        tiles = category in GRID_CATEGORIES
        grid = tiles and self.grid_button.isChecked()
        self.grid_button.setVisible(tiles)
        self.columns_box.setVisible(grid)
        self.select_button.setVisible(grid)
        self.download_button.setVisible(grid and category == 'video')
        if model is None:
            return
        if grid:
            if self.grid.model is not model:
                self.grid.set_model(model)
            self.stack.setCurrentWidget(self.grid)
        else:
            self.grid.set_model(None)
            self.stack.setCurrentWidget(self.transcript)
        self.fetcher.schedule()

    def _download_visible(self):
        from blink.messagepane.files import local_file
        items = [item for item in self.grid.visible_items() if not local_file(item)]
        ActivityLog().info(f'[Message with {self.key}] Downloading {len(items)} files in view')
        for item in items:
            if self.fetcher.progress(item.id) is None:
                self.fetcher.fetch(item, force=True)

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
        from blink.messagepane.uploads import Uploads
        if item.upload is not None and action == 'open' and item.upload['state'] == 'failed':
            action = 'retry_upload'
        if action == 'retry_upload':
            Uploads.instance().retry(item.id)
        elif action == 'discard_upload':
            Uploads.instance().discard(item.id)
        elif action == 'delete':
            self._delete_message(item)
        elif action == 'open':
            from blink.messagepane.files import local_file
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices
            path = local_file(item)
            if path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            elif self.fetcher.progress(item.id) is None:
                self.fetcher.fetch(item, force=True)        # asked for: whatever its size, and again after a failure
        elif action == 'edit':
            from blink.messagepane.format import plain_summary
            content = item.content if isinstance(item.content, str) else (item.content or b'').decode('utf-8', 'replace')
            if item.content_type == 'text/html':
                content = plain_summary(item)
            self.composer.set_editing({'id': item.id, 'text': content, 'timestamp': item.timestamp, 'account_id': item.account_id})
        elif action == 'forward':
            self._forward([item])
        elif action == 'location':
            from blink.messagepane.locations import LocationWindow
            LocationWindow.show_for(item, getattr(self.contact, 'name', '') or self.key, self.window())
        elif action == 'call_details':
            from blink.message_envelopes import call_record
            from blink.messagepane.info import show_call_details
            show_call_details(self, item, call_record(item.content, item.metadata))
        elif action == 'caption':
            self._edit_caption(item)
        elif action == 'info':
            from blink.messagepane.files import local_file
            from blink.messagepane.format import bubble_kind, delivery_mark
            from blink.messagepane.info import show_message_info
            progress = self.fetcher.progress(item.id)
            shown = {'Drawn as': 'picture' if item.category == 'image' else bubble_kind(item),
                     'Delivery mark': delivery_mark(item)[0] or '—',
                     'File here': local_file(item) if item.category in ('image', 'audio', 'video', 'other') else None,
                     'Downloading': f'{int(progress * 100)}%' if progress is not None else None,
                     'Unread here': 'yes' if item.direction == 'incoming' and not item.read else None}
            show_message_info(self, item, shown)
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
        ActivityLog().info(f'[Message with {key}] Deleted {what} {item.id} from the message pane' + (', also for the other party' if for_both else ''))
        self._remove_message(item.id, item.account_id, for_both, why='deleted here')

    # Location: send the current one, ask for theirs

    def _location_action(self, action):
        if self.key is None or not self._confirm_account():
            return
        key = self.key
        if action == 'request':
            from datetime import datetime
            from blink.location import LOCATION_CONTENT_TYPE, location_request_envelope
            self._send_location_body(key, location_request_envelope(self._new_id(), now=datetime.now().astimezone()), LOCATION_CONTENT_TYPE, 'Location request')
            self._note(translate('message_pane', 'Location requested'))
            return
        from blink.messagepane.position import CurrentPosition
        self.header.location_button.setEnabled(False)
        self._note(translate('message_pane', 'Finding your location…'))
        CurrentPosition.request(lambda coords, why, key=key: self._position_found(key, coords, why))

    @staticmethod
    def _new_id():
        import uuid
        return str(uuid.uuid4())

    def _position_found(self, key, coords, why):
        self.header.location_button.setEnabled(True)
        if coords is None:
            from PyQt6.QtWidgets import QMessageBox
            ActivityLog().warning(f'[Message with {key}] Location not sent: {why}')
            QMessageBox.warning(self, translate('message_pane', 'Location Not Sent'), translate('message_pane', 'Your current location could not be found.') + '\n\n' + why)
            return
        if key != self.key:
            ActivityLog().warning(f'[Message with {key}] The location was found after the conversation was left: not sent')
            return
        from datetime import datetime
        from blink.location import LOCATION_CONTENT_TYPE, one_shot_envelope
        message_id = self._new_id()
        body = one_shot_envelope(coords, message_id, now=datetime.now().astimezone())
        if body is None:
            self._note(translate('message_pane', 'Location not sent: the position is not known'), error=True)
            return
        self._send_location_body(key, body, LOCATION_CONTENT_TYPE, 'Current location', message_id)
        self.transcript.follow_bottom()

    def _send_location_body(self, key, body, content_type, what, message_id=None):
        import json
        from blink.messages import MessageManager
        message_id = message_id or json.loads(body).get('messageId') or self._new_id()
        try:
            session = self._message_session()
            account = self.header.account or session.account
            MessageManager().send_message(account, session.contact, body, content_type, id=message_id)
        except Exception as e:
            ActivityLog().error(f'[Message with {key}] Sending {what.lower()} failed: {e!r}')
            self._note(translate('message_pane', '%s not sent') % what, error=True)
            return
        ActivityLog().info(f'[Message with {key}] {what} sent as {message_id} from account {account.id}')

    def _note(self, text, error=False):
        """A short word near the location button."""
        from PyQt6.QtWidgets import QToolTip
        button = self.header.location_button
        QToolTip.showText(button.mapToGlobal(button.rect().bottomLeft()), text, button, button.rect(), 4000)
        if error:
            ActivityLog().warning(f'[Message with {self.key}] {text}')

    # Forward (blink.messagepane.forward)

    def _forward(self, items):
        from blink.messagepane.files import local_file
        from blink.messagepane.forward import ForwardDialog, forwardable
        items = [item for item in items if forwardable(item)]
        if not items:
            return
        dialog = ForwardDialog(len(items), exclude=self.key, parent=self)
        if dialog.exec() != ForwardDialog.DialogCode.Accepted or not dialog.key:
            return
        target = dialog.key
        ActivityLog().info(f'[Message with {self.key}] Forwarding {len(items)} messages to {target}')
        ready = []
        for item in items:
            if item.category != 'text' and not local_file(item):
                self._forward_after_download[item.id] = (item, target)
                self.fetcher.fetch(item, force=True)
                ActivityLog().info(f'[Message with {target}] Message {item.id} is forwarded once its file is downloaded')
            else:
                ready.append(item)
        self._forward_to(target, ready)
        self.grid.set_selecting(False)

    def _forward_to(self, target, items):
        """Send items again, as new messages, to the conversation target (a conversation key)."""
        import os
        import uuid
        from blink.message_envelopes import METADATA_CONTENT_TYPE, label_envelope
        from blink.messagepane.files import local_file
        from blink.messages import MessageManager
        from blink.sessions import SessionManager
        if not items:
            return
        try:
            session = MessageManager().create_message_session(target, selected=False)
        except Exception as e:
            ActivityLog().error(f'[Message with {target}] Cannot forward to it: {e!r}')
            return
        account = session.account
        for item in items:
            message_id = str(uuid.uuid4())
            try:
                if item.category == 'text':
                    content = item.content if isinstance(item.content, str) else (item.content or b'').decode('utf-8', 'replace')
                    content_type = item.content_type if item.content_type in ('text/plain', 'text/html') else 'text/plain'
                    MessageManager().send_message(account, session.contact, content, content_type, id=message_id)
                else:
                    path = local_file(item)
                    SessionManager().send_file(session.contact, session.contact_uri, path, transfer_id=message_id, account=account)
                    if item.caption:
                        metadata_id = str(uuid.uuid4())
                        envelope = label_envelope(message_id, metadata_id, item.caption, str(session.contact_uri.uri), ISOTimestamp.now())
                        MessageManager().send_message(account, session.contact, envelope, METADATA_CONTENT_TYPE, id=metadata_id)
            except Exception as e:
                ActivityLog().error(f'[Message with {target}] Forwarding message {item.id} failed: {e!r}')
                continue
            what = 'text' if item.category == 'text' else os.path.basename(local_file(item) or '')
            ActivityLog().info(f'[Message with {target}] Message {item.id} ({what}) forwarded as {message_id} from account {account.id}')

    def _delete_messages(self, items):
        """Delete several messages (the grid's ticked tiles) after one question; one's own
        also for the other party when asked to (and possible)."""
        from PyQt6.QtWidgets import QCheckBox, QMessageBox
        from blink.messagepane.uploads import Uploads
        from blink.uris import BONJOUR_ACCOUNT_ID
        items = [item for item in items if item is not None]
        if not items or self.key is None:
            return
        key = self.key
        stored = [item for item in items if item.upload is None]
        own = [item for item in stored if item.outgoing and item.account_id != BONJOUR_ACCOUNT_ID and item.state not in ('failed-local', 'pending')]
        count = len(items)
        box = QMessageBox(QMessageBox.Icon.Question, translate('message_pane', 'Delete %d messages') % count if count > 1 else translate('message_pane', 'Delete message'),
                          (translate('message_pane', 'Delete these %d messages from this conversation?') % count if count > 1 else translate('message_pane', 'Delete this message from this conversation?'))
                          + '\n' + translate('message_pane', 'Files downloaded here are deleted with them.'), parent=self)
        delete_button = box.addButton(translate('message_pane', 'Delete'), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        both = None
        if own:
            name = getattr(self.contact, 'name', '') or key
            both = QCheckBox((translate('message_pane', 'Delete the %d I sent for %s too') % (len(own), name)) if len(own) > 1 else translate('message_pane', 'Delete the one I sent for %s too') % name)
            both.setChecked(True)
            box.setCheckBox(both)
        box.exec()
        if box.clickedButton() is not delete_button:
            return
        for_both = both is not None and both.isChecked()
        ActivityLog().info(f'[Message with {key}] Deleting {count} messages from the message pane' + (f', {len(own)} also for the other party' if for_both else ''))
        for item in items:
            if item.upload is not None:
                Uploads.instance().discard(item.id)
            else:
                self._remove_message(item.id, item.account_id, for_both and item in own, why='deleted here')
        self.grid.set_selecting(False)

    def _remove_message(self, message_id, account_id, for_both, why):
        """Hide a message here (with its downloaded file) and, for_both, ask the other party's devices to remove it."""
        from blink.history import HistoryManager, MessageHistory
        key = self.key
        MessageHistory().tombstone_message(message_id, account_id=account_id, remote_uri=key, source=why)
        HistoryManager().download_history.remove(message_id)       # the downloaded file goes with it
        if for_both and account_id != 'bonjour@local':
            from blink.messages import MessageManager
            try:
                session = self._message_session()
                account = self._account(account_id) or session.account
                MessageManager().send_remove_message(session, message_id, account)
            except Exception as e:
                ActivityLog().warning(f'[Message with {key}] Cannot ask the other party to remove message {message_id}: {e!r}')
        model = self.models.get(key)
        if model is not None:
            model.remove_item(message_id)

    def _edit_caption(self, item):
        """Set or clear the caption of one's own picture or video: a label companion, as mobile sends it."""
        import uuid
        from PyQt6.QtWidgets import QInputDialog
        from blink.message_envelopes import METADATA_CONTENT_TYPE, label_envelope
        from blink.messages import MessageManager
        text, accepted = QInputDialog.getText(self, translate('message_pane', 'Edit Caption'), translate('message_pane', 'Caption (empty to remove it):'), text=item.caption or '')
        if not accepted or text.strip() == (item.caption or ''):
            return
        try:
            session = self._message_session()
            account = self._account(item.account_id) or session.account
            metadata_id = str(uuid.uuid4())
            envelope = label_envelope(item.id, metadata_id, text.strip(), str(self.uri.uri), ISOTimestamp.now())
            MessageManager().send_message(account, session.contact, envelope, METADATA_CONTENT_TYPE, id=metadata_id)
        except Exception as e:
            ActivityLog().error(f'[Message with {self.key}] Setting the caption of {item.id} failed: {e!r}')
            return
        ActivityLog().info(f'[Message with {self.key}] Caption of {item.id} ' + (f'set to {text.strip()!r}' if text.strip() else 'removed'))

    def _SH_DownloadChanged(self, message_id):
        self.transcript.bubble_delegate.forget(message_id)      # where its file is may have changed
        self.grid.forget(message_id)
        if message_id in self._forward_after_download:
            from blink.messagepane.files import local_file
            item, target = self._forward_after_download[message_id]
            if local_file(item):
                del self._forward_after_download[message_id]
                self._forward_to(target, [item])
            elif self.fetcher.progress(message_id) is None:
                del self._forward_after_download[message_id]
                ActivityLog().warning(f'[Message with {target}] Message {message_id} not forwarded: its file could not be downloaded')
        model = self.models.get(self.key)
        row = model.row_of(message_id) if model is not None else None
        if row is not None:
            index = model.index(row)
            model.dataChanged.emit(index, index)
            self.transcript.scheduleDelayedItemsLayout()

    def _SH_AudioAction(self, item, action, fraction):
        from blink.messagepane.audio import AudioPlayer
        path = self.transcript.bubble_delegate.file_path(item)
        if not path:
            return
        video = item.category == 'video'
        if action == 'play':
            AudioPlayer.instance().toggle(item.id, path, video)
        else:
            AudioPlayer.instance().seek(item.id, path, fraction, video)

    def _send_voice_note(self, note):
        """Compress a recorded voice note to AAC (blink.messagepane.transcode; the WAV when that cannot be
        done) and send it as a file transfer, with its waveform as a peaks companion (as mobile does)."""
        from blink.messagepane.transcode import convert
        if not self._confirm_account():
            return
        key = self.key

        def converted(path):
            import os
            if path is not None:
                try:
                    os.unlink(note['path'])
                except OSError:
                    pass
                note['path'] = path
            self._send_voice_note_file(note, key)
        convert(note['path'], converted)

    def _send_voice_note_file(self, note, key):
        import os
        import uuid
        from blink.message_envelopes import METADATA_CONTENT_TYPE, peaks_envelope
        from blink.messages import MessageManager
        from blink.messagepane.format import waveform_bars
        from blink.sessions import SessionManager
        if self.key is None or self.key != key:
            if self.key != key:
                ActivityLog().warning(f'[Message with {key}] The voice note {os.path.basename(note["path"])} was ready after the conversation was left: not sent')
            return
        try:
            session = self._message_session()
            account = self.header.account or session.account
            transfer_id = str(uuid.uuid4())
            SessionManager().send_file(session.contact, session.contact_uri, note['path'], transfer_id=transfer_id, account=account)
            peaks = [round(value, 3) for value in waveform_bars(note['peaks'], 100)]
            metadata_id = str(uuid.uuid4())
            envelope = peaks_envelope(transfer_id, metadata_id, {'l': peaks, 'r': []}, None, str(self.uri.uri), ISOTimestamp.now())
            MessageManager().send_message(account, session.contact, envelope, METADATA_CONTENT_TYPE, id=metadata_id)
        except Exception as e:
            ActivityLog().error(f'[Message with {self.key}] Sending the voice note {note["path"]} failed: {e!r}')
            return
        ActivityLog().info(f'[Message with {self.key}] Sending voice note {os.path.basename(note["path"])} ({note["duration"]:.1f} s) as {transfer_id}')
        self.transcript.follow_bottom()
