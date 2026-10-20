import base64
import os
import re
import uuid
import json
import platform

from PyQt6 import uic
from PyQt6.QtCore import Qt, QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSettings, QSize, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QFont, QAction, QBrush, QColor, QIcon, QLinearGradient, QPainter, QPalette, QPen, QPixmap, QPolygonF, QTextDocument
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QSplitter, QCheckBox, QLabel, QListView, QMenu, QStyle, QStyleOption, QStylePainter, QToolButton, QVBoxLayout, QWidget, QSizePolicy
from PyQt6.QtWidgets import QFileDialog
from PyQt6.QtWidgets import QMessageBox

from application.notification import IObserver, NotificationCenter, ObserverWeakrefProxy, NotificationData
from application.python import Null, limit
from application.python.types import MarkerType
from application.system import makedirs
from collections import deque
from datetime import datetime, timedelta, timezone
from dateutil.tz import tzlocal
from itertools import count

from zope.interface import implementer


from sipsimple.account import AccountManager
from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads import ParserError
from sipsimple.payloads.rcsfthttp import FTHTTPDocument
from sipsimple.streams.msrp.chat import OTRState
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp

from blink.accounts import AccountModel, ActiveAccountModel
from blink.avatars import avatar_color, avatar_initials
from blink.configuration.datatypes import File, GraphTimeScale
from blink.configuration.settings import BlinkSettings
from blink.contact_mangler import mangled_name, mangling_enabled
from blink.contacts import URIUtils
from blink.history import HistoryManager
from blink.logging import ActivityLog, MessagingTrace as log
from blink.hidpi import svg_pixmap, sharpen_ui_pixmaps
from blink.message_envelopes import FILE_TRANSFER_CONTENT_TYPE, file_transfer_category, file_transfer_envelope
from blink.messagepane.chat import ChatItem, ChatTranscriptModel, SenderAvatars, save_inline_image
from blink.messagepane.composer import Composer
from blink.messagepane.fetch import AutoFetcher
from blink.messagepane.files import local_file
from blink.messagepane.header import contact_photo, draw_avatar
from blink.messagepane.view import TranscriptView
from blink.messages import MessageManager, BlinkMessage, msrp_message_id, send_msrp_message
from blink import radio_ptt
from blink.resources import IconManager, Resources, ui_settings
from blink.screensharing.support import can_share_my_screen, can_view_screens, vnc_screen_sharing_offered
from blink.sessions import ChatSessionModel, ChatSessionListView, SessionManager, StreamDescription, BlinkFileTransfer, conference_has_files, session_can_expand
from blink.util import run_in_gui_thread, call_later, translate
from blink.widgets.color import ColorHelperMixin, follow_theme, is_dark_theme
from blink.widgets.graph import Graph
from blink.widgets.otr import OTRWidget
from blink.widgets.util import ContextMenuActions, QtDynamicProperty
from blink.widgets.zrtp import ZRTPWidget

if platform.system() == 'Darwin':
    try:
        from blink.macos_notification import mac_notify as desktop_notification
    except ImportError:
        desktop_notification = Null


__all__ = ['ChatWindow']


class Container(object): pass


# Chat content classes
#
# What the window adds to a session's transcript. The transcript is drawn by the
# message pane's TranscriptView and BubbleDelegate (blink.messagepane), so an MSRP
# session looks like a conversation in the message pane: each of these becomes a
# ChatItem (blink.messagepane.chat) when it is added.

class ChatContent(object):
    def __init__(self, message, history=False, focus=False):
        self.message = message
        self.history = history
        self.focus = focus
        self.timestamp = datetime.now()


class ChatNotification(ChatContent):
    """A centred line in the transcript."""


def conference_takes_files(blink_session):
    """Files go to a conference room over MSRP while we are in it: a room that is not running
    does not exist on the server (a file transfer to it gets 404). Not a conference: True."""
    return not blink_session.is_conference or str(blink_session.state or '').startswith('connected')


class ChatStatus(ChatNotification):
    pass


class ChatMessage(ChatContent):
    """A message: message is its body as it was sent (content_type says what it is)."""

    def __init__(self, message, sender, direction, history=False, focus=False, id=None, timestamp=None, account=None, content_type='text/plain', private=False, recipient=None):
        super(ChatMessage, self).__init__(message, history, focus)
        self.sender = sender
        self.direction = direction
        self.private = private          # a private message in a conference
        self.recipient = recipient      # ChatSender: whom a private message went to
        self.id = str(uuid.uuid4()) if id is None else id
        self.status = ''
        self.account = account
        self.content_type = content_type
        self.timestamp = timestamp if timestamp is not None else ISOTimestamp.now()


class ChatFile(ChatMessage):
    """A file: message is its file transfer envelope (application/sylk-file-transfer JSON or RCS XML)."""


class ChatSender(object):
    def __init__(self, name, uri, iconpath=None):
        self.name = name
        self.uri = uri
        self.iconpath = iconpath

    def __eq__(self, other):
        if not isinstance(other, ChatSender):
            return NotImplemented
        return self.name == other.name and self.uri == other.uri

    def __ne__(self, other):
        return not (self == other)


ui_class, base_class = uic.loadUiType(Resources.get('chat_input_lock.ui'))


class ChatInputLock(base_class, ui_class):
    def __init__(self, parent=None):
        super(ChatInputLock, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        if parent is not None:
            parent.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.contentsRect())
        return False

    def dragEnterEvent(self, event):
        event.ignore()  # let the parent process DND

    def paintEvent(self, event):
        option = QStyleOption()
        option.initFrom(self)
        painter = QStylePainter(self)
        painter.setRenderHint(QStylePainter.RenderHint.Antialiasing, True)
        painter.drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option)

del ui_class, base_class


class LockType(object, metaclass=MarkerType):
    note_text = None
    button_text = None


class EncryptionLock(LockType):
    note_text = 'Encryption has been terminated by the other party'
    button_text = 'Confirm'


class ChatComposer(Composer):
    """The message pane's composer, with the locks of an MSRP chat: while locked (the other party
    ended the encryption) a note over the text asks to confirm before anything more is typed."""

    lockEngaged = pyqtSignal(object)
    lockReleased = pyqtSignal(object)

    def __init__(self, parent=None):
        super(ChatComposer, self).__init__(parent)
        self.lock_widget = ChatInputLock(self.input_row)
        self.lock_widget.hide()
        self.lock_widget.confirm_button.clicked.connect(self._SH_LockWidgetConfirmButtonClicked)
        self.lock_queue = deque()
        self._placeholder = self.edit.placeholderText()     # the one shown while connected

    @property
    def empty(self):
        return not self.text().strip()

    @property
    def locked(self):
        return bool(self.lock_queue)

    def setPlaceholderText(self, text):
        self._placeholder = text
        if self.edit.isEnabled():
            self.edit.setPlaceholderText(text)

    @property
    def connected(self):
        return self.edit.isEnabled()

    def set_connected(self, connected, reason=None):
        """Typing only into a connected chat: otherwise the text field and the send button are
        disabled, the placeholder saying why (reason, else not connected). Attaching stays: a file
        goes in an MSRP file transfer of its own, without the chat (set_files_allowed hides it where
        it cannot go)."""
        self.edit.setEnabled(connected)
        self.send_button.setEnabled(connected and bool(self.text().strip()))
        self.edit.setPlaceholderText(self._placeholder if connected else (reason or translate('chat_window', 'Not connected, messages can be sent while connected')))

    def setFocus(self, *args):
        self.edit.setFocus(*args)

    def lock(self, lock_type):
        if lock_type in self.lock_queue:
            raise ValueError("already locked with {}".format(lock_type))
        if not self.locked:
            self._show_lock(lock_type)
            self.lock_widget.setGeometry(self.input_row.contentsRect())
            self.lock_widget.show()
            self.lock_widget.raise_()
            self.edit.setReadOnly(True)
            self.lockEngaged.emit(lock_type)
        self.lock_queue.append(lock_type)

    def reset_locks(self):
        self.edit.setReadOnly(False)
        self.lock_widget.hide()
        self.lock_queue.clear()

    def _show_lock(self, lock_type):
        self.lock_widget.note_label.setText(lock_type.note_text)
        self.lock_widget.confirm_button.setText(lock_type.button_text)

    def _SH_LockWidgetConfirmButtonClicked(self):
        self.lockReleased.emit(self.lock_queue.popleft())
        if self.locked:
            lock_type = self.lock_queue[0]
            self._show_lock(lock_type)
            self.lockEngaged.emit(lock_type)
        else:
            self.lock_widget.hide()
            self.edit.setReadOnly(False)

    def _SH_TextChanged(self):
        super(ChatComposer, self)._SH_TextChanged()
        if not self.connected:
            self.send_button.setEnabled(False)

    def _send(self):
        if not self.locked and self.connected:
            super(ChatComposer, self)._send()

    def set_files_allowed(self, allowed, voice_notes=True):
        """Attach only where files can go (not a conference we are out of); voice notes also
        only where they are wanted (not to a conference)."""
        from blink.messagepane.recorder import recording_available
        self.files_allowed = (allowed, voice_notes)
        self.attach_button.setVisible(allowed)
        self.mic_button.setVisible(allowed and voice_notes and recording_available())


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


@implementer(IObserver)
class ChatWidget(QWidget):
    """The transcript and the composer of one session of the MSRP sessions window.

    The transcript is the message pane's (TranscriptView, BubbleDelegate) over the rows
    this window adds (ChatTranscriptModel); files are fetched as the message pane fetches
    them (AutoFetcher: what is in view, within its limits, from contacts allowed to send
    files), and the text size follows the message pane's (A−/A+ there).
    """

    default_user_icon = IconDescriptor(Resources.get('icons/default-avatar.png'))

    # what a message's menu offers here: no replies, edits, reactions or forwarding over MSRP
    menu_actions = frozenset({'download', 'delete'})

    def __init__(self, session, parent=None):
        super(ChatWidget, self).__init__(parent)
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.session = session
        self._build_header()
        layout.addWidget(self.header)
        from blink.messagepane.strip import TranscriptStrip
        self.strip = TranscriptStrip(self)      # what is shown, and the search field: typing filters the transcript
        layout.addWidget(self.strip)
        self.model = ChatTranscriptModel(self)
        self.chat_view = TranscriptView(self)
        self.chat_view.menu_actions = self.menu_actions
        self.chat_view.extra_menu_actions = self._extra_menu_actions
        self.chat_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chat_view.setMinimumSize(300, 100)
        self.chat_view.setModel(self.model)
        self.avatars = SenderAvatars()
        self.chat_view.bubble_delegate.sender_avatar = self.avatars.draw
        self.chat_view.bubble_delegate.sender_name = self.avatars.name
        self.chat_view.bubble_delegate.group_by_sender = True     # a conference: each participant's turns of their own
        # audio bubbles (call recordings, voice notes) more compact than in the message pane
        self.chat_view.bubble_delegate.audio_button = 24
        self.chat_view.bubble_delegate.audio_wave_height = 22
        self.chat_view.bubble_delegate.audio_width = 260
        layout.addWidget(self.chat_view, 1)
        self.chat_input = ChatComposer(self)
        layout.addWidget(self.chat_input)
        self.transcript = self.chat_view       # what AutoFetcher looks at
        self.fetcher = AutoFetcher(self)
        self.chat_view.bubble_delegate.progress_of = self.fetcher.progress
        self.chat_view.verticalScrollBar().valueChanged.connect(self.fetcher.schedule)
        self.model.rowsInserted.connect(self.fetcher.schedule)
        self.model.dataChanged.connect(self._SH_ModelDataChanged)
        self.strip.set_conversation(self.model, self.chat_view)
        self.chat_view.verticalScrollBar().valueChanged.connect(self.strip.update_text)
        # the calendar, as in the message pane: jump to a day of the conversation (Back to Now after one)
        from blink.resources import themed_icon
        self.calendar_button = QToolButton(self)
        self.calendar_button.setAutoRaise(True)
        self.calendar_button.setToolTip(translate('message_pane', 'Jump to a date'))
        self.calendar_button.setIconSize(QSize(16, 16))
        self.calendar_button.setFixedSize(26, 26)
        self.calendar_button.setIcon(themed_icon(Resources.get('icons/clock.svg'), '#d0d0d0', outline=True))
        self.calendar_menu = QMenu(self.calendar_button)
        self.calendar_menu.aboutToShow.connect(self._fill_calendar_menu)
        self.calendar_button.setMenu(self.calendar_menu)
        self.calendar_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.calendar_button.setStyleSheet('QToolButton::menu-indicator { image: none; }')
        self.calendar_button.setEnabled(False)
        self.strip.add_before_search(self.calendar_button)
        self.day_counts = {}
        self._history_range = None      # (oldest, newest) time of the rows loaded from history: where the next pages start
        self.model.pager = self.load_page
        self.model.modelReset.connect(self._SH_SearchChanged)
        self._apply_font()

        self.otr_timer = QTimer()
        self.otr_timer.setSingleShot(True)
        self.otr_timer.setInterval(15000)
        self.session = session
        self.history_loaded = False
        self.pending_decryption = []
        self._message_statuses = {}
        if session is not None:
            notification_center = NotificationCenter()
            notification_center.add_observer(ObserverWeakrefProxy(self), sender=session.blink_session)

        self.chat_input.sendText.connect(self._SH_ChatInputTextEntered)
        self.chat_input.composing.connect(self._SH_ChatInputComposing)
        self.chat_input.filesDropped.connect(self._SH_ChatInputFilesDropped)
        self.chat_input.voiceNote.connect(self._SH_ChatInputVoiceNote)
        self.chat_input.lockReleased.connect(self._SH_ChatInputLockReleased)
        self.update_input_state()       # disabled until the session is connected with chat
        self.chat_view.actionRequested.connect(self._SH_ActionRequested)
        self.chat_view.audioAction.connect(self._SH_AudioAction)
        self.fetcher.changed.connect(self._SH_DownloadChanged)

        self.otr_timer.timeout.connect(self._SH_OTRTimerTimeout)
        self.update_file_options()
        self.update_header()

    # The bar on top: the message pane's header (name, address, A− A+, the call buttons), without
    # location and video; its chat and audio buttons connect and hang up this session

    def _build_header(self):
        from PyQt6.QtWidgets import QHBoxLayout
        from blink.messagepane.header import ConversationHeader
        header = self.header = ConversationHeader(self)
        # what does not belong to an MSRP session: location, video, the calendar (the message
        # pane's strip), another address or account (this session has its own), the PGP lock
        header._update_location_button = lambda: header.location_button.hide()
        header._update_account_button = lambda: header.account_button.hide()
        header._update_address_button = lambda: header.address_button.hide()
        for button in (header.location_button, header.video_button, header.calendar_button, header.account_button, header.address_button, header.lock_button):
            button.hide()
        header.audio_button.clicked.disconnect()
        header.audio_button.clicked.connect(self._SH_HeaderAudioClicked)
        self.header_chat_button = header._tool_button(translate('chat_window', 'Chat'))
        from blink.resources import themed_icon
        self.header_chat_button.setIcon(themed_icon(Resources.get('icons/chat.svg'), '#d0d0d0'))     # as the handset beside it
        self.header_chat_button.setProperty('segmented', 'call')
        self.header_chat_button.clicked.connect(self._SH_HeaderChatClicked)
        for button in (self.header_chat_button, header.audio_button):
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for row in header.findChildren(QHBoxLayout):
            position = row.indexOf(header.audio_button)
            if position >= 0:
                row.insertWidget(position, self.header_chat_button)     # chat, then audio: one segmented control
                break
        # push to talk, right after the room's name; shown and driven by the window (ChatWindow._update_radio_ptt)
        self.radio_ptt_button = QToolButton(header)
        self.radio_ptt_button.setObjectName('radio_ptt_button')
        self.radio_ptt_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.radio_ptt_button.setAutoRaise(True)
        self.radio_ptt_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.radio_ptt_button.setMinimumHeight(30)
        self.radio_ptt_button.setIconSize(QSize(22, 22))
        self.radio_ptt_button.hide()
        for row in header.findChildren(QHBoxLayout):
            position = row.indexOf(header.account_button)
            if position >= 0:
                row.insertWidget(position, self.radio_ptt_button)
                break
        header.fontStep.connect(self._SH_FontStep)
        self._header_conversation = None

    def update_header(self):
        if self.session is None:
            self.header.hide()
            return
        blink_session = self.session.blink_session
        if blink_session is None or blink_session.contact is None or blink_session.contact_uri is None:
            return
        from blink.history import session_conversation_key
        conversation = (blink_session.contact, str(blink_session.contact_uri.uri), blink_session.account)
        if conversation != self._header_conversation:
            self._header_conversation = conversation
            self.header.set_conversation(blink_session.contact, blink_session.contact_uri, session_conversation_key(blink_session), blink_session.account)
            room_title = blink_session.conference_title
            if room_title is not None and not mangling_enabled():
                self.header.name_label.setText(room_title)      # a room: its own name, else Room <room>
                self.header.name_label.setToolTip(room_title)
            self.header.lock_button.hide()
        state = str(blink_session.state or '')
        idle = state in ('', 'initialized', 'ended', 'deleted')
        busy = state in ('ending', 'connected/sent_proposal', 'connected/received_proposal')
        streams = blink_session.streams.types if not idle else set()
        has_chat = 'chat' in streams or blink_session.chat_type is None and not idle
        has_audio = 'audio' in streams
        chat_button, audio_button = self.header_chat_button, self.header.audio_button
        chat_button.setChecked(not idle and has_chat)
        audio_button.setChecked(not idle and has_audio)
        chat_button.setEnabled(not busy)
        audio_button.setEnabled(not busy and blink_session.account is not None)
        if idle:
            chat_button.setToolTip(translate('chat_window', 'Connect with chat'))
            audio_button.setToolTip(translate('chat_window', 'Connect with audio and chat'))
        elif state.startswith('connecting'):
            chat_button.setToolTip(translate('chat_window', 'Cancel'))
            audio_button.setToolTip(translate('chat_window', 'Cancel'))
        else:
            chat_button.setToolTip(translate('chat_window', 'Hang up'))
            if has_audio:
                audio_button.setToolTip(translate('chat_window', 'Hang up') if not streams - {'audio'} else translate('chat_window', 'Remove audio'))
            else:
                audio_button.setToolTip(translate('chat_window', 'Add audio'))

    def _log_header_action(self, action):
        blink_session = self.session.blink_session
        uri = blink_session.contact_uri.uri if blink_session.contact_uri is not None else '?'
        ActivityLog().info(f'[chat window] {action} {uri} (account {getattr(blink_session.account, "id", "?")}, state {blink_session.state or "new"})')

    def _SH_HeaderChatClicked(self):
        if self.session is None:
            return
        blink_session = self.session.blink_session
        state = str(blink_session.state or '')
        if state in ('', 'initialized', 'ended', 'deleted'):
            self._log_header_action('Connect with chat:')
            blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=[StreamDescription('chat')], reinitialize=True)
            blink_session.connect()
        elif state.startswith(('connecting', 'connected')):
            self._log_header_action('Hang up')
            self.session.end()
        self.update_header()

    def _SH_HeaderAudioClicked(self):
        if self.session is None:
            return
        blink_session = self.session.blink_session
        state = str(blink_session.state or '')
        if state in ('', 'initialized', 'ended', 'deleted'):
            self._log_header_action('Connect with audio and chat:')
            blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri,
                                        stream_descriptions=[StreamDescription('audio'), StreamDescription('chat')], reinitialize=True)
            blink_session.connect()
        elif state.startswith('connecting'):
            self._log_header_action('Cancel')
            self.session.end()
        elif state == 'connected':
            streams = blink_session.streams.types
            if 'audio' in streams:
                if streams - {'audio'}:
                    self._log_header_action('Remove audio from')
                    blink_session.remove_stream(blink_session.streams.get('audio'))
                else:
                    self._log_header_action('Hang up')
                    self.session.end()
            else:
                self._log_header_action('Add audio to')
                if 'chat' in streams:
                    blink_session.add_stream(StreamDescription('audio'))
                else:
                    blink_session.add_streams([StreamDescription('audio'), StreamDescription('chat')])     # audio here always comes with MSRP chat
        self.update_header()

    def _SH_FontStep(self, step):
        from blink.messagepane.pane import MessagePane
        low, high = MessagePane.font_steps
        try:
            delta = int(ui_settings().value('message_pane/font_delta', 0))
        except (TypeError, ValueError):
            delta = 0
        ui_settings().setValue('message_pane/font_delta', max(low, min(high, delta + step)))
        self._apply_font()

    _media_names = {'audio': 'Audio', 'video': 'Video', 'screen-sharing': 'Screen sharing'}

    def _media_note(self, stream, text):
        """A line in the transcript for audio, video or screen sharing added to or gone from the session."""
        name = self._media_names.get(getattr(stream, 'type', None))
        if name is not None:
            self.add_message(ChatStatus(translate('chat_window', text) % translate('chat_window', name)))

    def _NH_BlinkSessionDidAddStream(self, notification):
        self.update_input_state()
        self.update_header()
        self._media_note(notification.data.stream, '%s added')

    def _NH_BlinkSessionContactDidChange(self, notification):
        self._header_conversation = None
        self.update_header()

    def update_file_options(self):
        """A conference (the remote party is or was a focus) takes files only while we are in it;
        a one to one conversation always does."""
        allowed = self.session is None or conference_takes_files(self.session.blink_session)
        voice_notes = self.session is None or not self.session.blink_session.is_conference     # no voice notes to a conference (focus, or was one)
        if getattr(self.chat_input, 'files_allowed', None) != (allowed, voice_notes):
            self.chat_input.set_files_allowed(allowed, voice_notes)

    def update_input_state(self):
        """The composer takes text only once the session is connected with a chat stream: before
        (and after it ends, or the chat stream is removed) it is disabled."""
        session = self.session
        blink_session = session.blink_session if session is not None else None
        connected = blink_session is not None and str(blink_session.state or '').startswith('connected')
        ready = connected and session.chat_stream is not None
        was_ready = self.chat_input.connected
        reason = translate('chat_window', 'Connected without chat, add MSRP chat from the wheel to send messages') if connected and not ready else None
        self.chat_input.set_connected(ready, reason)
        if ready and not was_ready and self.isVisible():
            self.chat_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _NH_BlinkSessionDidChangeState(self, notification):
        self.update_file_options()
        self.update_input_state()
        self.update_header()

    @property
    def user_icon(self):
        return IconManager().get('avatar') or self.default_user_icon

    # What AutoFetcher asks of the widget it fetches for

    @property
    def contact(self):
        return self.session.blink_session.contact if self.session is not None else None

    @staticmethod
    def _account(account_id):
        account_manager = AccountManager()
        if account_id and account_manager.has_account(account_id):
            return account_manager.get_account(account_id)
        return None

    def _message_session(self):
        return self.session.blink_session

    # Text size: the message pane's

    def _apply_font(self):
        try:
            delta = int(ui_settings().value('message_pane/font_delta', 0))
        except (TypeError, ValueError):
            delta = 0
        font = QApplication.font()
        if font.pointSizeF() > 0:
            font.setPointSizeF(max(font.pointSizeF() + delta, 6))
        from blink.messagepane.reactions import EMOJI_FAMILIES
        font.setFamilies([font.family()] + [family for family in EMOJI_FAMILIES if family != font.family()])
        if font != self.chat_view.font():
            self.chat_view.setFont(font)
            self.chat_input.edit.setFont(font)

    def showEvent(self, event):
        self._apply_font()      # the message pane's text size may have changed meanwhile
        super(ChatWidget, self).showEvent(event)

    # The transcript

    def _item_for(self, message):
        """The transcript row of what the window adds."""
        if isinstance(message, ChatNotification):
            return ChatItem(getattr(message, 'id', None) or uuid.uuid4(), direction=getattr(message, 'direction', 'incoming'),
                            content=message.message, category=None, timestamp=message.timestamp, note=True)
        sender = message.sender
        if message.direction == 'outgoing':
            account_id = sender.uri if sender is not None else ''
        elif getattr(message, 'account', None) is not None:
            account_id = message.account.id
        else:
            account_id = self.session.blink_session.account.id if self.session is not None else ''     # received on the session's account
        if isinstance(message, ChatFile):
            category = file_transfer_category(message.message) or 'other'
        else:
            category = 'text'
        return ChatItem(message.id, direction=message.direction, content=message.message, content_type=message.content_type, category=category,
                        timestamp=message.timestamp, display_name=sender.name if sender is not None else '', uri=sender.uri if sender is not None else '',
                        account_id=account_id, remote_uri=self._remote_uri(),
                        state=self._message_statuses.get(message.id), sender=sender.uri if sender is not None else None,
                        private=bool(getattr(message, 'private', False)), recipient_name=self._recipient_name(getattr(message, 'recipient', None)))

    @staticmethod
    def _recipient_name(recipient):
        if recipient is None:
            return ''
        return recipient.name or str(recipient.uri or '').partition('@')[0]

    def _remote_uri(self):
        if self.session is None:
            return ''
        return str(getattr(self.session.blink_session.contact_uri, 'uri', '') or '')

    def add_message(self, message):
        item = self._item_for(message)
        if not item.note and item.id in self.model.ids:
            return
        if item.note:
            last = self.model.all_items[-1] if self.model.all_items else None
            if last is not None and last.note and last.content == item.content:
                return      # the same line twice in a row (the stream and the session both say it)
        at_bottom = self._at_bottom()
        self.model.add(item)
        if item.outgoing or at_bottom:
            self.chat_view.follow_bottom()
        if not item.note:
            self._tile_changed()

    def replace_message(self, id, message):
        item = self._item_for(message)
        self.model.replace(id, item)
        self.chat_view.bubble_delegate.forget(id)
        self._tile_changed()

    def _tile_changed(self):
        """The session's tile shows the last message and its time: drawn again, once for many changes (history loading)."""
        if self.session is None or getattr(self, '_tile_pending', False):
            return
        self._tile_pending = True
        def refresh():
            self._tile_pending = False
            session = self.session
            if session is None or getattr(session, 'widget', None) is None or session.blink_session is None:
                return
            session.widget.update_content(session)
            NotificationCenter().post_notification('ChatSessionItemDidChange', sender=session)
        QTimer.singleShot(0, refresh)

    def update_message_text(self, id, text, content_type='text/plain'):
        """New content for a message (its text once decrypted)."""
        if self.model.update(id, content=text, content_type=content_type):
            self.chat_view.bubble_delegate.clear_cache()
            self._tile_changed()

    # a message state never goes back: a 'delivered' or 'received' report
    # arriving after 'displayed' (another device of the peer, the server
    # journal) must not take the double tick away
    message_status_rank = {'pending': 0, 'failed-local': 1, 'failed': 1, 'accepted': 2, 'sent': 2, 'received': 2, 'delivered': 3, 'displayed': 4}

    def update_message_status(self, id, status):
        rank = self.message_status_rank.get(status, 2)
        if rank < self.message_status_rank.get(self._message_statuses.get(id), -1):
            return
        self._message_statuses[id] = status
        self.model.update(id, state=status)

    def update_message_encryption(self, id, is_secure=False):
        if is_secure is True:
            self.model.update(id, encryption_type='OpenPGP')

    def remove_message(self, id):
        self.model.remove(id)
        self._tile_changed()

    def clear(self):
        """Forget what is shown (the conversation is about to be loaded again)."""
        self.model.clear()
        self.model.has_more = self.model.has_newer = False
        self._history_range = None
        self.avatars.clear()
        self._message_statuses.clear()

    # Pages of history, read and paged as the message pane does (MessageHistory.load): older ones as
    # the transcript is scrolled up, a past day from the calendar and the newer ones after it

    def load_page(self, kind, day=None):
        """kind 'older', 'newer', 'jump' (to day) or 'now' (the newest page again, after a jump)."""
        if self.session is None or self.session.blink_session is None:
            return
        tick = timedelta(microseconds=1)
        before = after = None
        if kind == 'older':
            if self._history_range is None:
                return
            before = self._history_range[0] + tick       # rows sharing its time that did not fit come too (known ones are dropped)
        elif kind == 'newer':
            if self._history_range is None:
                return
            after = self._history_range[1] - tick
        elif kind == 'jump':
            before = datetime.combine(day + timedelta(days=1), datetime.min.time()).astimezone()     # local midnight after the day
        if kind in ('jump', 'now'):
            self.strip.close_search()
        self.model.set_loading(True)
        blink_session = self.session.blink_session
        HistoryManager().load(blink_session.contact.uri.uri, blink_session, kind=kind, before=before, after=after, day=day)

    def history_page_loaded(self, kind, messages, data):
        """A page is in the transcript: where the next ones start, and what is left to load."""
        times = [message.timestamp.replace(tzinfo=timezone.utc) if message.timestamp.tzinfo is None else message.timestamp.astimezone(timezone.utc) for message in messages]
        previous = self._history_range
        if times:
            if previous is None or kind in ('initial', 'now', 'jump'):
                self._history_range = (min(times), max(times))
            else:
                self._history_range = (min(previous[0], min(times)), max(previous[1], max(times)))
        model = self.model
        more = bool(getattr(data, 'more', False))
        if kind == 'older':
            # no further back than before (only rows already shown): the beginning
            model.has_more = more and bool(times) and (previous is None or min(times) < previous[0])
        elif kind == 'newer':
            model.has_newer = more and bool(times) and (previous is None or max(times) > previous[1])
        elif kind in ('initial', 'now', 'jump'):
            model.has_more = more
            model.has_newer = bool(getattr(data, 'newer', False)) if kind == 'jump' else False
        day_counts = getattr(data, 'day_counts', None)
        if day_counts is not None:
            self.day_counts = day_counts
            self.calendar_button.setEnabled(bool(day_counts))
        model.loaded = True
        model.set_loading(False)
        if kind == 'jump':
            day = data.day
            row = next((position for position, item in enumerate(model.items) if item.timestamp.astimezone().date() >= day), max(len(model.items) - 1, 0))
            model.jumped.emit(row)
        self.strip.update_text()

    def _fill_calendar_menu(self):
        from blink.messagepane.header import fill_calendar_menu
        fill_calendar_menu(self.calendar_menu, self.day_counts, lambda day: self.load_page('jump', day),
                           (lambda: self.load_page('now')) if self.model.has_newer else None)

    def has_file(self, file_hash):
        """Whether a file with this MSRP hash is in the transcript already (one's own upload, told of again by the room)."""
        return self.file_item(file_hash) is not None

    def file_item(self, file_hash):
        """The transcript row of the file with this MSRP hash, or None."""
        if not file_hash:
            return None
        from blink.history import msrp_hash
        wanted = msrp_hash(file_hash)
        for item in self.model.all_items:
            if item.category in ('image', 'audio', 'video', 'other'):
                meta = file_transfer_envelope(item.content) or {}
                if meta.get('hash') and msrp_hash(meta['hash']) == wanted:
                    return item
        return None

    def file_ready(self, id):
        """The file of a message is in place: draw it."""
        self.chat_view.bubble_delegate.forget(id)
        self.model.refresh(id)

    def show_loading_screen(self, visible):
        self.chat_view.setEnabled(not visible)

    def scroll_to_bottom(self):
        self.chat_view.follow_bottom()

    def _at_bottom(self):
        scrollbar = self.chat_view.verticalScrollBar()
        return scrollbar.value() >= scrollbar.maximum() - 4

    def _SH_SearchChanged(self):
        self.chat_view.bubble_delegate.clear_cache()      # the hits are highlighted
        if self.model.search_text:
            self.chat_view.scrollToTop()
        else:
            self.chat_view.follow_bottom()

    def _SH_ModelDataChanged(self, *args):
        self.chat_view.scheduleDelayedItemsLayout()

    def _SH_DownloadChanged(self, message_id):
        self.file_ready(message_id)

    # Sending

    def send_sip_message(self, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None):
        account = self.session.blink_session.account
        contact = self.session.blink_session.contact
        manager = MessageManager()
        manager.send_message(account, contact, content, content_type, recipients, courtesy_recipients, subject, timestamp, required, additional_headers, id)

    def send_message(self, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None, id=None):
        blink_session = self.session.blink_session

        if blink_session.chat_type is None:
            self.send_sip_message(content, content_type, recipients, courtesy_recipients, subject, timestamp, required, additional_headers, id)
            return

        if blink_session.state in ('initialized', 'ended'):
            blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, [StreamDescription('chat')], reinitialize=True)
            blink_session.connect()
        elif blink_session.state == 'connected/*':
            if self.session.chat_stream is None:
                self.session.blink_session.add_stream(StreamDescription('chat'))
        elif blink_session.state == 'connecting/*' and self.session.chat_stream is not None:
            pass
        else:
            raise RuntimeError("Cannot send messages in the '%s' state" % blink_session.state)

        message_id = send_msrp_message(self.session.chat_stream, content, content_type, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)
        notification_center = NotificationCenter()

        timestamp = timestamp if timestamp is not None else ISOTimestamp.now()
        notification_center.post_notification('ChatStreamWillSendMessage', blink_session, data=BlinkMessage(content, content_type, blink_session.account, recipients, timestamp=timestamp, id=message_id))
        return message_id

    def start_otr_timer(self):
        self.otr_timer.start()

    def stop_otr_timer(self):
        self.otr_timer.stop()

    def _own_sender(self):
        account = self.session.blink_session.account
        return ChatSender(account.display_name, account.id, self.user_icon.filename)

    # Drag and drop

    def dragEnterEvent(self, event):
        mime_data = event.mimeData()
        if mime_data.hasUrls() and self.session is not None and not conference_takes_files(self.session.blink_session) \
                and all(url.isLocalFile() for url in mime_data.urls()):
            event.ignore()      # files cannot go to a conference we are not in
            return
        if mime_data.hasUrls() or mime_data.hasHtml() or mime_data.hasText():
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        event.accept()

    def dragMoveEvent(self, event):
        if event.possibleActions() & (Qt.DropAction.CopyAction | Qt.DropAction.LinkAction):
            event.accept(self.rect())
        else:
            event.ignore(self.rect())

    def dropEvent(self, event):
        event.acceptProposedAction()
        mime_data = event.mimeData()
        if mime_data.hasUrls():
            urls = mime_data.urls()
            schemes = {url.scheme() for url in urls}
            if schemes == {'file'}:
                self._DH_Files([url.toLocalFile() for url in urls])
            else:
                self._DH_Text('\n'.join(url.toString() for url in urls))
        else:
            mime_types = set(mime_data.formats())
            if mime_types.issuperset({'text/html', 'text/_moz_htmlcontext'}):
                text = str(mime_data.data('text/html'), encoding='utf16')
            else:
                text = mime_data.html() or mime_data.text()
            self._DH_Text(text)

    def _DH_Files(self, paths):
        session_manager = SessionManager()
        blink_session = self.session.blink_session
        if not conference_takes_files(blink_session):
            self.add_message(ChatStatus(translate('chat_window', 'Join the conference to share files: the room exists only while someone is in it')))
            return
        for path in paths:
            session_manager.send_file(blink_session.contact, blink_session.contact_uri, path, account=blink_session.account, route='msrp')  # the MSRP chat window: MSRP only

    image_data_re = re.compile(r"data:(?P<type>image/.+?);base64,(?P<data>.*)", re.I | re.U | re.S)

    def _DH_Text(self, text):
        match = self.image_data_re.match(text)
        if match is not None:
            content_type = match.group('type')
            try:
                data = base64.b64decode(match.group('data'))
                message_id = self.send_message(match.group('data'), content_type=content_type)
            except Exception as e:
                self.add_message(ChatStatus(translate('chat_window', 'Error sending image: %s') % str(e)))
            else:
                message_id = message_id or str(uuid.uuid4())
                envelope = save_inline_image(message_id, content_type, data)
                if envelope is not None:
                    self.add_message(ChatFile(envelope, self._own_sender(), 'outgoing', id=message_id, content_type=FILE_TRANSFER_CONTENT_TYPE))
        else:
            document = QTextDocument()
            document.setHtml(text)
            plain_text = document.toPlainText().strip()
            if plain_text:
                self._SH_ChatInputTextEntered(plain_text)

    # The composer

    def _SH_ChatInputFilesDropped(self, paths):
        self._DH_Files(paths)

    def _SH_ChatInputVoiceNote(self, note):
        """A voice note goes as a file, compressed to AAC as in the message pane (the WAV when that cannot be done)."""
        from blink.messagepane.transcode import convert

        def converted(path):
            if path is not None:
                try:
                    os.unlink(note['path'])
                except OSError:
                    pass
                note['path'] = path
            self._DH_Files([note['path']])
        convert(note['path'], converted)

    def _SH_ChatInputComposing(self, state):
        if self.session is None:
            return
        if self.session.blink_session.chat_type is None:
            MessageManager().send_composing_indication(self.session.blink_session, state)
            return
        chat_stream = self.session.chat_stream
        if chat_stream is None:
            return
        try:
            chat_stream.send_composing_indication(state)
        except Exception:
            pass

    def _SH_ChatInputTextEntered(self, text):
        if text == '/otr+':
            try:
                self.session.chat_stream.encryption.start()
            except AttributeError:
                pass
            return
        elif text == '/otr-':
            try:
                self.session.chat_stream.encryption.stop()
            except AttributeError:
                pass
            return
        id = str(uuid.uuid4())
        try:
            msg_id = self.send_message(text, content_type='text/plain', id=id)
        except Exception as e:
            self.add_message(ChatStatus(translate('chat_window', 'Error sending message: %s') % e))
            log.error('Error sending message:  %s' % str(e))
        else:
            if msg_id is not None:
                id = msg_id
            self.add_message(ChatMessage(text, self._own_sender(), 'outgoing', id=id))
            self.chat_view.follow_bottom()

    def _SH_ChatInputLockReleased(self, lock_type):
        blink_session = self.session.blink_session
        if lock_type is EncryptionLock:
            if blink_session.chat_type is not None:
                self.session.chat_stream.encryption.stop()
            else:
                blink_session.fake_streams.get('messages').encryption.stop()

    def _SH_OTRTimerTimeout(self):
        self.otr_timer.stop()
        self.add_message(ChatStatus(translate('chat_window', 'Timeout in enabling OTR, recipient did not answer to OTR encryption request')))  # decide what type to use here. -Dan
        NotificationCenter().post_notification('ChatStreamOTRTimeout', self.session.blink_session)

    # Message actions

    def _SH_ActionRequested(self, action, item):
        if action == 'delete':
            self._delete_message(item)
        elif action in ('download', 'open'):
            path = local_file(item)
            if path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            elif self.fetcher.progress(item.id) is None:
                self.fetcher.fetch(item, force=True)        # asked for: whatever its size, and again after a failure

    # Private messages in a conference, as Blink for macOS sends them: CPIM To one participant,
    # offered when the focus allows them (a=chatroom:private-messages, the stream's
    # private_messages_allowed) and the participant takes messages

    def _private_recipient(self, item):
        """The conference participant a message came from, when a private reply can go to them; else None."""
        if self.session is None or item.note or item.outgoing:
            return None
        blink_session = self.session.blink_session
        if not blink_session.remote_focus:
            return None
        chat_stream = self.session.chat_stream
        if chat_stream is None or not getattr(chat_stream, 'private_messages_allowed', False):
            return None
        from blink.messagepane.chat import sender_key
        uri = sender_key(item)
        if not uri or uri == str(blink_session.account.id) or uri == self._remote_uri().replace('sip:', ''):
            return None
        participant = blink_session.server_conference.participants.get(uri)
        if participant is None or 'chat' not in participant.active_media:
            return None
        return participant

    def _extra_menu_actions(self, item):
        participant = self._private_recipient(item)
        if participant is None:
            return []
        return [(translate('chat_window', 'Reply in Private…'), lambda: self._reply_in_private(item, participant))]

    def _reply_in_private(self, item, participant):
        from PyQt6.QtWidgets import QInputDialog
        from sipsimple.core import SIPURI, SIPCoreError
        from sipsimple.streams.msrp.chat import ChatIdentity
        name = self.chat_view.bubble_delegate.sender_name(item) if self.chat_view.bubble_delegate.sender_name is not None else (item.display_name or participant.uri)
        text, ok = QInputDialog.getMultiLineText(self, translate('chat_window', 'Private message'), translate('chat_window', 'Private message to %s:') % name)
        text = text.strip() if ok else ''
        if not text:
            return
        try:
            recipient = ChatIdentity(SIPURI.parse('sip:%s' % participant.uri), display_name=name)
        except SIPCoreError:
            return
        chat_stream = self.session.chat_stream
        if chat_stream is None or not getattr(chat_stream, 'private_messages_allowed', False):
            self.add_message(ChatStatus(translate('chat_window', 'The conference does not take private messages')))
            return
        try:
            message_id = self.send_message(text, content_type='text/plain', recipients=[recipient])
        except Exception as e:
            self.add_message(ChatStatus(translate('chat_window', 'Error sending private message: %s') % e))
            log.error('Error sending private message: %s' % str(e))
            return
        message_id = message_id or str(uuid.uuid4())
        ActivityLog().info(f'[chat] Private message {message_id} to {participant.uri} in {self._session_address()}')
        self.add_message(ChatMessage(text, self._own_sender(), 'outgoing', id=message_id, private=True, recipient=ChatSender(name, participant.uri)))
        self.chat_view.follow_bottom()

    def _SH_AudioAction(self, item, action, fraction):
        from blink.messagepane.audio import AudioPlayer
        path = self.chat_view.bubble_delegate.file_path(item)
        if not path:
            return
        video = item.category == 'video'
        if action == 'play':
            AudioPlayer.instance().toggle(item.id, path, video)
        else:
            AudioPlayer.instance().seek(item.id, path, fraction, video)

    def _delete_message(self, item):
        """Delete a message here, after asking (as the message pane asks). Never for the other
        party too: a message can not be removed at the other end over MSRP."""
        if item.note or self.session is None:
            return
        blink_session = self.session.blink_session
        is_file = item.category in ('image', 'audio', 'video', 'other')
        what = translate('chat_window', 'file') if is_file else translate('chat_window', 'message')
        box = QMessageBox(QMessageBox.Icon.Question, translate('chat_window', 'Delete %s') % what,
                          translate('chat_window', 'Delete this %s from this conversation?') % what, parent=self)
        delete_button = box.addButton(translate('chat_window', 'Delete'), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not delete_button:
            return
        self.remove_message(item.id)
        NotificationCenter().post_notification('BlinkMessageWillDelete', sender=blink_session, data=NotificationData(id=item.id))
        ActivityLog().info(f'[chat] Deleted {what} {item.id} from the MSRP sessions window')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    # The session connecting and disconnecting, as lines in the transcript

    def _session_address(self):
        blink_session = self.session.blink_session
        return str(getattr(blink_session, 'uri', None) or getattr(blink_session.contact_uri, 'uri', '') or '').replace('sip:', '')

    def _NH_BlinkSessionWillConnect(self, notification):
        self._connected = False
        self.add_message(ChatStatus(translate('chat_window', 'Connecting to %s…') % self._session_address()))

    def _NH_BlinkSessionDidConnect(self, notification):
        self._connected = True
        self.update_input_state()
        self.add_message(ChatStatus(translate('chat_window', 'Connected')))

    def _NH_BlinkSessionDidEnd(self, notification):
        self.chat_input.reset_locks()
        self.update_input_state()
        # the chat session: never "Call ended", calls are the main window's and the message pane's
        reason = getattr(notification.data, 'reason', None)
        error = getattr(notification.data, 'error', False)
        if not getattr(self, '_connected', False):
            text = translate('chat_window', 'Could not connect: %s') % reason if reason else translate('chat_window', 'Could not connect')
        elif error and reason:
            text = translate('chat_window', 'Disconnected: %s') % reason
        else:
            text = translate('chat_window', 'Disconnected')
        self._connected = False
        self.add_message(ChatStatus(text))

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.fetcher.close()
        self.setParent(None)

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        self.update_input_state()
        self.update_header()
        if notification.data.stream.type == 'chat':
            self.chat_input.reset_locks()
            self.add_message(ChatStatus(translate('chat_window', 'Chat stream ended')))
        else:
            self._media_note(notification.data.stream, '%s ended')

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'chat':
            self.add_message(ChatStatus(translate('chat_window', 'Cannot add chat stream: %s (%s)') % (notification.data.reason, notification.data.code)))
        else:
            reason, code = getattr(notification.data, 'reason', None), getattr(notification.data, 'code', None)
            self._media_note(notification.data.stream, '%s could not be added' + (f': {reason}' if reason else '') + (f' ({code})' if code else ''))

    def _NH_BlinkSessionShouldDeleteFile(self, notification):
        item = self.model.get(notification.data.item.id)
        if item is not None:
            self._delete_message(item)


class NoSessionsLabel(QLabel):
    def apply_theme(self):
        background, color = ('palette(base)', '#9a9a9a') if is_dark_theme() else ('white', '#545454')
        self.setStyleSheet(f"""QLabel {{ border: 1px inset palette(dark); border-radius: 3px; background-color: {background}; color: {color}; }}""")

    def __init__(self, chat_window):
        super(NoSessionsLabel, self).__init__(chat_window.session_panel)
        self.chat_window = chat_window
        font = self.font()
        font.setPointSizeF(font.pointSizeF() * 1.1)
        self.setFont(font)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setContentsMargins(0, 12, 0, 0)
        self.apply_theme()
        follow_theme(self)
        self.setText(translate('chat_window', "No Sessions"))
        chat_window.session_panel.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.resize(event.size())
        return False


ui_class, base_class = uic.loadUiType(Resources.get('chat_window.ui'))


@implementer(IObserver)
class ChatWindow(base_class, ui_class, ColorHelperMixin):

    sliding_panels = True

    __streamtypes__ = {'chat', 'screen-sharing'}  # the stream types for which we show the chat window (video has a window of its own, see videowindow)

    def apply_theme(self):
        self.session_widget.setPalette(QPalette() if is_dark_theme() else self._session_widget_palette)
        self._tint_panel_buttons()

    def _tint_panel_buttons(self):
        """The info/participants/files buttons of the left panel. Their SVGs are thin white glyphs made
        for the old dark panel (the 'i' an SVG text element), invisible on a light theme: draw bold
        glyphs in a colour that contrasts with the theme, at the 12px the button's frame leaves."""
        color = QColor('#d8d8d8') if is_dark_theme() else QColor('#303030')
        size = 48   # drawn large, scaled down to the icon size
        icon_size = QSize(12, 12)

        def canvas():
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            return pixmap, painter

        def info():
            pixmap, painter = canvas()
            painter.drawEllipse(QRectF(size * 0.37, size * 0.02, size * 0.26, size * 0.26))     # the dot
            painter.drawRoundedRect(QRectF(size * 0.38, size * 0.36, size * 0.24, size * 0.62), size * 0.05, size * 0.05)   # the stem
            painter.end()
            return QIcon(pixmap)

        def files():
            pixmap, painter = canvas()
            # an arrow down into a tray: transferred files
            painter.drawRect(QRectF(size * 0.40, size * 0.02, size * 0.20, size * 0.38))
            painter.drawPolygon(QPolygonF([QPointF(size * 0.18, size * 0.36), QPointF(size * 0.82, size * 0.36), QPointF(size * 0.50, size * 0.68)]))
            pen = QPen(color, size * 0.12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QPolygonF([QPointF(size * 0.08, size * 0.62), QPointF(size * 0.08, size * 0.92), QPointF(size * 0.92, size * 0.92), QPointF(size * 0.92, size * 0.62)]))
            painter.end()
            return QIcon(pixmap)

        def participants():
            pixmap, painter = canvas()
            # two people: heads and shoulders
            for x, scale in ((0.32, 0.9), (0.66, 1.0)):
                head = size * 0.20 * scale
                painter.drawEllipse(QPointF(size * x, size * 0.30), head, head)
                painter.drawChord(QRectF(size * x - size * 0.28 * scale, size * 0.56, size * 0.56 * scale, size * 0.76), 0, 180 * 16)
            painter.end()
            return QIcon(pixmap)

        info_icon = info()
        files_icon = files()
        participants_icon = participants()
        for button in (self.participants_panel_info_button, self.files_panel_info_button,
                       self.participants_panel_files_button, self.info_panel_files_button,
                       self.files_panel_participants_button, self.info_panel_participants_button):
            button.setIconSize(icon_size)
        for button in (self.participants_panel_info_button, self.files_panel_info_button):
            button.setIcon(info_icon)
        for button in (self.participants_panel_files_button, self.info_panel_files_button):
            button.setIcon(files_icon)
        for button in (self.files_panel_participants_button, self.info_panel_participants_button):
            button.setIcon(participants_icon)

    def __init__(self, parent=None):
        super(ChatWindow, self).__init__(parent)
        with Resources.directory:
            self.setupUi()

        # chat_window.ui paints the session area white; under a dark theme it takes the theme's colours
        self._session_widget_palette = QPalette(self.session_widget.palette())
        self.apply_theme()
        follow_theme(self)

        self.selected_item = None
        self.session_model = ChatSessionModel(self)
        self.session_list.setModel(self.session_model)
        self.session_widget.installEventFilter(self)
        self.state_label.installEventFilter(self)

        self.info_panel.installEventFilter(self)
        self.audio_encryption_label.installEventFilter(self)
        self.video_encryption_label.installEventFilter(self)
        self.chat_encryption_label.installEventFilter(self)

        self.latency_graph.installEventFilter(self)
        self.packet_loss_graph.installEventFilter(self)
        self.traffic_graph.installEventFilter(self)

        self.mute_button.clicked.connect(self._SH_MuteButtonClicked)
        self.hold_button.clicked.connect(self._SH_HoldButtonClicked)
        self.record_button.clicked.connect(self._SH_RecordButtonClicked)
        self.control_button.clicked.connect(self._SH_ControlButtonClicked)
        self.connect_audio_button.clicked.connect(self._SH_ConnectAudioButtonClicked)
        self.connect_chat_button.clicked.connect(lambda: self._AH_Connect())
        self.hangup_button.clicked.connect(lambda: self._AH_Disconnect())
        self.participants_panel_info_button.clicked.connect(self._SH_InfoButtonClicked)
        self.participants_panel_files_button.clicked.connect(self._SH_FilesButtonClicked)
        self.files_panel_info_button.clicked.connect(self._SH_InfoButtonClicked)
        self.files_panel_participants_button.clicked.connect(self._SH_ParticipantsButtonClicked)
        self.info_panel_files_button.clicked.connect(self._SH_FilesButtonClicked)
        self.info_panel_participants_button.clicked.connect(self._SH_ParticipantsButtonClicked)
        self.latency_graph.updated.connect(self._SH_LatencyGraphUpdated)
        self.packet_loss_graph.updated.connect(self._SH_PacketLossGraphUpdated)
        self.traffic_graph.updated.connect(self._SH_TrafficGraphUpdated)
        self.session_model.sessionAdded.connect(self._SH_SessionModelSessionAdded)
        self.session_model.sessionAdded.connect(lambda session: self._save_conference_tiles())
        self.session_model.sessionAdded.connect(lambda session: self._update_back_to_sessions())
        self.session_model.sessionRemoved.connect(lambda session: self._update_back_to_sessions())
        self.session_model.dataChanged.connect(lambda *args: self._update_back_to_sessions())     # a tile connected or ended
        self.session_model.sessionRemoved.connect(lambda session: self._save_conference_tiles())
        self.session_model.rowsMoved.connect(lambda *args: self._save_conference_tiles())
        self.session_model.sessionRemoved.connect(self._SH_SessionModelSessionRemoved)
        self.session_model.sessionAboutToBeRemoved.connect(self._SH_SessionModelSessionAboutToBeRemoved)
        self.session_list.selectionModel().selectionChanged.connect(self._SH_SessionListSelectionChanged)
        self.otr_widget.nameChanged.connect(self._SH_OTRWidgetNameChanged)
        self.otr_widget.statusChanged.connect(self._SH_OTRWidgetStatusChanged)
        self.zrtp_widget.nameChanged.connect(self._SH_ZRTPWidgetNameChanged)
        self.zrtp_widget.statusChanged.connect(self._SH_ZRTPWidgetStatusChanged)


        geometry = ui_settings().value("chat_window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        splitter_state = ui_settings().value("chat_window/splitter")
        if splitter_state:
            self.splitter.restoreState(splitter_state)

        self.pending_displayed_notifications = {}
        self.render_after_load = deque()
        self.last_desktop_notify = None
        self.last_incoming_message_alert = None

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        notification_center.add_observer(self, name='BlinkSessionNewIncoming')
        notification_center.add_observer(self, name='BlinkSessionNewOutgoing')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForIncoming')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForOutgoing')
        notification_center.add_observer(self, name='BlinkSessionIsSelected')
        notification_center.add_observer(self, name='ChatStreamGotMessage')
        notification_center.add_observer(self, name='ChatStreamGotComposingIndication')
        notification_center.add_observer(self, name='ChatStreamDidSendMessage')
        notification_center.add_observer(self, name='ChatUnreadMessagesCountChanged')
        notification_center.add_observer(self, name='ChatStreamDidDeliverMessage')
        notification_center.add_observer(self, name='ChatStreamDidNotDeliverMessage')
        notification_center.add_observer(self, name='ChatStreamOTREncryptionStateChanged')
        notification_center.add_observer(self, name='ChatStreamOTRError')
        notification_center.add_observer(self, name='ChatStreamOTRTimeout')
        notification_center.add_observer(self, name='MediaStreamDidInitialize')
        notification_center.add_observer(self, name='MediaStreamDidNotInitialize')
        notification_center.add_observer(self, name='MediaStreamDidStart')
        notification_center.add_observer(self, name='MediaStreamDidFail')
        notification_center.add_observer(self, name='MediaStreamDidEnd')
        notification_center.add_observer(self, name='MediaStreamWillEnd')
        notification_center.add_observer(self, name='BlinkGotMessage')
        notification_center.add_observer(self, name='BlinkGotComposingIndication')
        notification_center.add_observer(self, name='BlinkGotDispositionNotification')
        notification_center.add_observer(self, name='BlinkGotMessageDelete')
        notification_center.add_observer(self, name='BlinkMessageDidSucceed')
        notification_center.add_observer(self, name='BlinkMessageDidFail')
        notification_center.add_observer(self, name='BlinkMessageHistoryLoadDidSucceed')
        notification_center.add_observer(self, name='BlinkMessageHistoryLoadDidFail')
        notification_center.add_observer(self, name='BlinkMessageHistoryLastContactsDidSucceed')
        notification_center.add_observer(self, name='BlinkMessageHistoryCallHistoryDidStore')
        notification_center.add_observer(self, name='BlinkConversationWillRemove')
        notification_center.add_observer(self, name='MessageStreamPGPKeysDidLoad')
        notification_center.add_observer(self, name='PGPMessageDidDecrypt')
        notification_center.add_observer(self, name='PGPFileDidNotDecrypt')
        notification_center.add_observer(self, name='BlinkFileTransferDidEnd')
        notification_center.add_observer(self, name='BlinkSessionDidSaveCallRecording')
        notification_center.add_observer(self, name='BlinkMessageHistoryMustReload')
        notification_center.add_observer(self, name='ConferenceParticipantDidChange')


        # self.splitter.splitterMoved.connect(self._SH_SplitterMoved) # check this and decide on what size to have in the window (see Notes) -Dan

    def _SH_SplitterMoved(self, pos, index):
        print("-- splitter:", pos, index, self.splitter.sizes())

    def setupUi(self):
        super(ChatWindow, self).setupUi(self)

        self.session_list = ChatSessionListView(self)
        self.session_list.setObjectName('session_list')

        self.no_sessions_label = NoSessionsLabel(self)
        self.no_sessions_label.setObjectName('no_sessions_label')

        self.otr_widget = OTRWidget(self.info_panel)
        self.zrtp_widget = ZRTPWidget(self.info_panel)
        self.zrtp_widget.stream_type = None

        self.control_icon = QIcon(Resources.get('icons/cog.svg'))
        self.cancel_icon = QIcon(Resources.get('icons/cancel.png'))

        self.pixmaps = Container()

        self.pixmaps.direct_connection = svg_pixmap(Resources.get('icons/connection-direct.svg'))
        self.pixmaps.relay_connection = svg_pixmap(Resources.get('icons/connection-relay.svg'))
        self.pixmaps.unknown_connection = svg_pixmap(Resources.get('icons/connection-unknown.svg'))

        self.pixmaps.blue_lock = svg_pixmap(Resources.get('icons/lock-blue-12.svg'))
        self.pixmaps.grey_lock = svg_pixmap(Resources.get('icons/lock-grey-12.svg'))
        self.pixmaps.green_lock = svg_pixmap(Resources.get('icons/lock-green-12.svg'))
        self.pixmaps.orange_lock = svg_pixmap(Resources.get('icons/lock-orange-12.svg'))

        def blended_pixmap(pixmap, color):
            blended_pixmap = QPixmap(pixmap)
            if blended_pixmap.isNull():
                return blended_pixmap       # the SVG did not load (no Qt SVG plugin): painting on it crashes Qt
            painter = QPainter(blended_pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
            painter.fillRect(blended_pixmap.rect(), color)
            painter.end()
            return blended_pixmap

        color = QColor(255, 255, 255, 64)
        self.pixmaps.light_blue_lock = blended_pixmap(self.pixmaps.blue_lock, color)
        self.pixmaps.light_grey_lock = blended_pixmap(self.pixmaps.grey_lock, color)
        self.pixmaps.light_green_lock = blended_pixmap(self.pixmaps.green_lock, color)
        self.pixmaps.light_orange_lock = blended_pixmap(self.pixmaps.orange_lock, color)

        # fix the SVG icons as the generated code loads them as pixmaps, losing their ability to scale -Dan
        def svg_icon(filename_off, filename_on):
            icon = QIcon()
            icon.addFile(filename_off, mode=QIcon.Mode.Normal, state=QIcon.State.Off)
            icon.addFile(filename_on,  mode=QIcon.Mode.Normal, state=QIcon.State.On)
            icon.addFile(filename_on,  mode=QIcon.Mode.Active, state=QIcon.State.On)
            return icon

        self.mute_button.setIcon(svg_icon(Resources.get('icons/mic-on.svg'), Resources.get('icons/mic-off.svg')))
        self.hold_button.setIcon(svg_icon(Resources.get('icons/pause.svg'), Resources.get('icons/paused.svg')))
        self.record_button.setIcon(svg_icon(Resources.get('icons/record.svg'), Resources.get('icons/recording.svg')))

        self.control_button.setIcon(self.control_icon)
        sharpen_ui_pixmaps(self, 'chat_window.ui')

        # an expanded session (the list collapsed onto it): a bar back to all the sessions, as the audio drawer has
        self.back_to_sessions_button = QToolButton(self.session_panel)
        self.back_to_sessions_button.setObjectName('back_to_sessions_button')
        self.back_to_sessions_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.back_to_sessions_button.setAutoRaise(True)
        self.back_to_sessions_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.back_to_sessions_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.back_to_sessions_button.setToolTip(translate('chat_window', 'Back to the list of chats'))
        self.back_to_sessions_button.setStyleSheet('QToolButton { text-align: left; padding: 3px 6px; border: none; border-bottom: 1px solid palette(mid); }'
                                                   'QToolButton:hover { background: palette(midlight); }')
        self.back_to_sessions_button.clicked.connect(lambda: self._EH_ShowSessions())
        self.session_panel_layout.insertWidget(0, self.back_to_sessions_button)
        self.session_list.raise_()      # the list covers the panel, the bar included

        # what this window is for, above the sessions (the window title names the selected session)
        self.window_kind_label = QLabel(translate('chat_window', 'MSRP sessions'), self.layoutWidget)
        self.window_kind_label.setObjectName('window_kind_label')
        self.window_kind_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.window_kind_label.setContentsMargins(6, 4, 6, 0)
        font = self.window_kind_label.font()
        font.setBold(True)
        self.window_kind_label.setFont(font)
        self.window_kind_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.left_panel_layout.insertWidget(0, self.window_kind_label)
        self.no_sessions_label.raise_()

        # connect buttons on the bottom bar, shown while the session is not connected (the wheel's
        # menu is for a connected session)
        def connect_button(name, icon, text, tooltip):
            button = QToolButton(self.toolbar_widget)
            button.setObjectName(name)
            button.setIcon(QIcon(Resources.get(icon)))
            button.setText(text)
            button.setToolTip(tooltip)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setMinimumHeight(self.control_button.minimumHeight() or 24)
            button.setIconSize(QSize(16, 16))
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.hide()
            return button
        self.connect_audio_button = connect_button('connect_audio_button', 'icons/handset.svg', translate('chat_window', 'Audio'), translate('chat_window', 'Connect with audio and chat'))
        self.connect_chat_button = connect_button('connect_chat_button', 'icons/chat.svg', translate('chat_window', 'Chat'), translate('chat_window', 'Connect with chat'))
        # in their place while connecting, connected or disconnecting: where the session is
        self.connection_status_label = QLabel(self.toolbar_widget)
        self.connection_status_label.setObjectName('connection_status_label')
        self.connection_status_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.connection_status_label.hide()
        position = self.toolbar_layout.indexOf(self.record_button) + 1
        self.toolbar_layout.insertWidget(position, self.connect_audio_button)
        self.toolbar_layout.insertWidget(position + 1, self.connect_chat_button)
        self.toolbar_layout.insertWidget(position + 2, self.connection_status_label)
        # a connected session (any media): hang up, at the left end of the bar, before the microphone
        self.hangup_button = connect_button('hangup_button', 'icons/handset12-red.svg', translate('chat_window', 'Hang up'), translate('chat_window', 'End the session'))
        self.toolbar_layout.insertWidget(0, self.hangup_button)
        # a room with participants that can key a two-way radio: push to talk, after the microphone
        self._no_radio_ptt_button = QToolButton(self)      # stands in for the top bar's button when no session is selected
        self._no_radio_ptt_button.hide()
        # the bar, left to right: the wheel (always there), the microphone, push to talk, the connect
        # buttons or the connection status; hang up alone at the right end, away from the others
        while self.toolbar_layout.count():
            self.toolbar_layout.takeAt(0)
        for widget in (self.control_button, self.mute_button, self.hold_button, self.record_button,
                       self.connect_audio_button, self.connect_chat_button, self.connection_status_label):
            self.toolbar_layout.addWidget(widget)
        self.toolbar_layout.addStretch(1)
        self.toolbar_layout.addWidget(self.hangup_button)
        self.toolbar_layout.setSpacing(6)     # the .ui packs them edge to edge
        self.radio_pulse_timer = QTimer(self)
        self.radio_pulse_timer.setInterval(radio_ptt.pulse.interval)
        self.radio_pulse_timer.timeout.connect(self._SH_RadioPulseTimerTimeout)
        self.radio_ptt_commands = {}     # id of a command sent -> (action, endpoint), until the room answers it

        self.control_menu = QMenu(self.control_button)
        self.control_button.setMenu(self.control_menu)
        self.control_button.actions = ContextMenuActions()
        self.control_button.actions.connect_with_msrp = QAction(translate('chat_window', "Start MSRP chat"), self, triggered=self._AH_Connect)
        self.control_button.actions.connect_with_audio = QAction(translate('chat_window', "Start audio and MSRP chat"), self, triggered=self._AH_ConnectWithAudio)
        self.control_button.actions.mark_messages_read = QAction(translate('chat_window', "Mark as read"), self, triggered=self._AH_MarkMessagesRead)
        self.control_button.actions.connect_with_video = QAction(translate('chat_window', "Start video call"), self, triggered=self._AH_ConnectWithVideo)
        self.control_button.actions.disconnect = QAction(translate('chat_window', "Disconnect"), self, triggered=self._AH_Disconnect)
        self.control_button.actions.add_audio = QAction(translate('chat_window', "Add audio"), self, triggered=self._AH_AddAudio)
        self.control_button.actions.remove_audio = QAction(translate('chat_window', "Remove audio"), self, triggered=self._AH_RemoveAudio)
        self.control_button.actions.add_video = QAction(translate('chat_window', "Add video"), self, triggered=self._AH_AddVideo)
        self.control_button.actions.remove_video = QAction(translate('chat_window', "Remove video"), self, triggered=self._AH_RemoveVideo)
        self.control_button.actions.add_chat = QAction(translate('chat_window', "Add MSRP chat"), self, triggered=self._AH_AddChat)
        self.control_button.actions.remove_chat = QAction(translate('chat_window', "Remove MSRP chat"), self, triggered=self._AH_RemoveChat)
        self.control_button.actions.send_files = QAction(translate("chat_window", "Send File(s)..."), self, triggered=self._AH_SendFiles)
        self.control_button.actions.share_my_screen = QAction(translate('chat_window', "Share my screen"), self, triggered=self._AH_ShareMyScreen)
        self.control_button.actions.request_screen = QAction(translate('chat_window', "Request screen"), self, triggered=self._AH_RequestScreen)
        self.control_button.actions.end_screen_sharing = QAction(translate('chat_window', "End screen sharing"), self, triggered=self._AH_EndScreenSharing)
        self.control_button.actions.enable_otr = QAction(translate('chat_window', "Enable OTR for messaging"), self, triggered=self._AH_EnableOTR)
        self.control_button.actions.enable_otr_progress = QAction(translate('chat_window', "Enabling OTR for messaging"), self, enabled=False)
        self.control_button.actions.disable_otr = QAction(translate('chat_window', "Disable OTR for messaging"), self, triggered=self._AH_DisableOTR)
        self.control_button.actions.main_window = QAction(translate('chat_window', "Main Window"), self, triggered=self._AH_MainWindow, shortcut='Ctrl+B', shortcutContext=Qt.ShortcutContext.ApplicationShortcut)
        self.control_button.actions.show_transferred_files = QAction(translate('chat_window', "Show transferred files"), self, triggered=self._AH_ShowTransferredFiles)
        self.control_button.actions.remove_conversation = QAction(translate('chat_window', "Remove conversation"), self, triggered=self._AH_RemoveConversation)
        self.addAction(self.control_button.actions.main_window)  # make this active even when it's not in the control_button's menu

        self.slide_direction = self.session_details.RightToLeft  # decide if we slide from one direction only -Dan
        self.slide_direction = self.session_details.Automatic
        self.session_details.animationDuration = 300
        self.session_details.animationEasingCurve = QEasingCurve.Type.OutCirc

        # As the drawer of Blink for macOS: the participants of a conference and the files transferred
        # in one column, one above the other; no switching between them, the column as a whole is
        # expanded or collapsed. A session that is not a conference shows only the files.
        self.session_details.removeWidget(self.participants_panel)
        self.session_details.removeWidget(self.files_panel)
        self.details_splitter = QSplitter(Qt.Orientation.Vertical, self.session_details)
        self.details_splitter.setObjectName('details_splitter')
        self.details_splitter.setChildrenCollapsible(False)
        self.details_splitter.setHandleWidth(5)
        self.details_splitter.addWidget(self.participants_panel)
        self.details_splitter.addWidget(self.files_panel)
        self.files_panel.show()     # the stack hid whichever page was not current
        self.details_splitter.setStretchFactor(0, 3)
        self.details_splitter.setStretchFactor(1, 2)
        self.session_details.addWidget(self.details_splitter)
        # what a conference shows before it is connected: nothing (no info panel, no lists yet)
        self.details_placeholder = QWidget(self.session_details)
        self.details_placeholder.setObjectName('details_placeholder')
        self.session_details.addWidget(self.details_placeholder)
        self.participants_panel_files_button.hide()     # both are in view, nothing to switch to
        self.files_panel_participants_button.hide()

        self.audio_latency_graph = Graph([], color=QColor(0, 100, 215), over_boundary_color=QColor(255, 0, 100))
        self.video_latency_graph = Graph([], color=QColor(0, 215, 100), over_boundary_color=QColor(255, 100, 0), enabled=False)     # disable for now
        self.audio_packet_loss_graph = Graph([], color=QColor(0, 100, 215), over_boundary_color=QColor(255, 0, 100))
        self.video_packet_loss_graph = Graph([], color=QColor(0, 215, 100), over_boundary_color=QColor(255, 100, 0), enabled=False) # disable for now

        self.incoming_traffic_graph = Graph([], color=QColor(255, 50, 50))
        self.outgoing_traffic_graph = Graph([], color=QColor(0, 100, 215))

        self.latency_graph.add_graph(self.audio_latency_graph)
        self.latency_graph.add_graph(self.video_latency_graph)
        self.packet_loss_graph.add_graph(self.audio_packet_loss_graph)
        self.packet_loss_graph.add_graph(self.video_packet_loss_graph)

        # the graph added 2nd will be displayed on top
        self.traffic_graph.add_graph(self.incoming_traffic_graph)
        self.traffic_graph.add_graph(self.outgoing_traffic_graph)

        self.dummy_tab = None    # will be replaced by a dummy ChatWidget during SIPApplicationDidStart (creating a ChatWidget needs access to settings)
        self.tab_widget.clear()  # remove the tab(s) added in designer
        self.tab_widget.tabBar().hide()

        self.session_list.hide()

        self.otr_widget.hide()
        self.zrtp_widget.hide()
        self.info_panel_files_button.hide()
        self.info_panel_participants_button.hide()
        self.participants_panel_files_button.hide()

        self.new_messages_button.hide()
        self.hold_button.hide()
        self.record_button.hide()
        self.control_button.setEnabled(False)

        self.info_label.setForegroundRole(QPalette.ColorRole.Dark)

        # prepare the RTP stream encryption labels so we can take over their behaviour
        self.audio_encryption_label.hovered = False
        self.video_encryption_label.hovered = False
        self.audio_encryption_label.stream_type = 'audio'
        self.video_encryption_label.stream_type = 'video'

        self.chat_encryption_label.hovered = False

        # prepare self.session_widget so we can take over some of its painting and behaviour
        self.session_widget.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.session_widget.hovered = False

    def _get_selected_session(self):
        try:
            return self.__dict__['selected_session']
        except KeyError:
            return None

    def _set_selected_session(self, session):
        old_session = self.__dict__.get('selected_session', None)
        new_session = self.__dict__['selected_session'] = session
        if new_session != old_session:
            self.otr_widget.hide()
            self.zrtp_widget.hide()
            self.zrtp_widget.stream_type = None
            notification_center = NotificationCenter()
            if old_session is not None:
                notification_center.remove_observer(self, sender=old_session)
                notification_center.remove_observer(self, sender=old_session.blink_session)
            if new_session is not None:
                notification_center.add_observer(self, sender=new_session)
                notification_center.add_observer(self, sender=new_session.blink_session)
                self._update_widgets_for_session()  # clean this up -Dan (too many functions called in 3 different places: on selection changed, here and on notifications handlers)
                self._update_control_menu()
                self._update_panel_buttons()
                self._update_radio_ptt()
                if ('audio' not in new_session.blink_session.streams and 'video' not in new_session.blink_session.streams):
                    self._SH_FilesButtonClicked(True)
                self._show_conference_participants(new_session)
                self._update_session_info_panel(elements={'session', 'media', 'statistics', 'status'}, update_visibility=True)
                # TODO: somehow the placeholder does not get changed inside ChatInputText class -adi
                if new_session.blink_session.info.streams.messages and new_session.blink_session.info.streams.messages.encryption:
                    new_session.chat_widget.chat_input.setPlaceholderText(translate('chat_window', 'Write a message. Messages will be encrypted using %s' % new_session.blink_session.info.streams.messages.encryption))
                else:
                    new_session.chat_widget.chat_input.setPlaceholderText(translate('chat_window', 'Write a message'))


    selected_session = property(_get_selected_session, _set_selected_session)
    del _get_selected_session, _set_selected_session

    def _update_back_to_sessions(self):
        tiles = [item for item in self.session_model.sessions if self._shows_session(item.blink_session)]
        connected = sum(1 for item in tiles if str(item.blink_session.state or '').startswith('connected'))
        self.back_to_sessions_button.setText('\u2039  ' + (translate('chat_window', 'All chats (%d \u00b7 %d connected)') % (len(tiles), connected)))

    def _update_widgets_for_session(self):
        self._update_back_to_sessions()
        session = self.selected_session
        widget = session.widget
        # session widget
        self.name_label.setText(widget.name_label.text())
        self.info_label.setText(widget.info_label.text())
        self.icon_label.setPixmap(widget.icon_label.pixmap())
        self.state_label.state = widget.state_label.state or 'offline'
        self.hold_icon.setVisible(widget.hold_icon.isVisibleTo(widget))
        self.composing_icon.setVisible(widget.composing_icon.isVisibleTo(widget))
        self.audio_icon.setVisible(widget.audio_icon.isVisibleTo(widget))
        self.audio_icon.setEnabled(widget.audio_icon.isEnabledTo(widget))
        self.chat_icon.setVisible(widget.chat_icon.isVisibleTo(widget))
        self.chat_icon.setEnabled(widget.chat_icon.isEnabledTo(widget))
        self.video_icon.setVisible(widget.video_icon.isVisibleTo(widget))
        self.video_icon.setEnabled(widget.video_icon.isEnabledTo(widget))
        self.screen_sharing_icon.setVisible(widget.screen_sharing_icon.isVisibleTo(widget))
        self.screen_sharing_icon.setEnabled(widget.screen_sharing_icon.isEnabledTo(widget))
        # toolbar buttons: no hold nor record here, both are in the audio drawer of the main window
        self.hold_button.hide()
        self.record_button.hide()
        self._update_window_title()

    default_window_title = 'Chat sessions (MSRP)'

    def _update_window_title(self):
        """The selected session in the title: its URI, preceded by the name of its addressbook
        contact when there is one with a display name of its own (not the user part or the URI)."""
        session = self.selected_session
        if session is None or session.blink_session.contact_uri is None:
            self.setWindowTitle(translate('chat_window', self.default_window_title))
            return
        blink_session = session.blink_session
        uri = str(blink_session.contact_uri.uri)
        if uri.startswith(('sip:', 'sips:')):
            uri = uri.partition(':')[2]
        contact = blink_session.contact
        name = (getattr(contact, 'name', None) or '').strip()
        has_name = getattr(contact, 'type', None) not in (None, 'dummy') and name and name not in (uri, uri.partition('@')[0])
        room_title = blink_session.conference_title
        if room_title is not None:
            title = room_title      # the room's own name, else Room <room>
        else:
            title = f'{name} <{uri}>' if has_name else uri
        self.setWindowTitle(title)

    @staticmethod
    def _connection_status(blink_session):
        state = str(blink_session.state or '')
        if state == 'connecting/dns_lookup' or state == 'connecting':
            return translate('chat_window', 'Connecting...')
        if state == 'connecting/ringing':
            return translate('chat_window', 'Ringing...')
        if state == 'connecting/early_media':
            return translate('chat_window', 'Early media...')
        if state.startswith('connecting'):
            return translate('chat_window', 'Starting...')
        if state == 'connected/sent_proposal':
            return translate('chat_window', 'Adding media...')
        if state == 'connected/received_proposal':
            return translate('chat_window', 'Media proposal...')
        if state.startswith('connected'):
            media = ', '.join(sorted(blink_session.streams.types)) or translate('chat_window', 'no media')
            return translate('chat_window', 'Connected: %s') % media
        if state == 'ending':
            return translate('chat_window', 'Disconnecting...')
        return state

    def _update_control_menu(self):
        menu = self.control_menu
        menu.hide()
        if not self.selected_session:
            self.connect_audio_button.hide()
            self.connect_chat_button.hide()
            self.connection_status_label.hide()
            self.hangup_button.hide()
            self.control_button.show()
            self.control_button.setEnabled(False)
            return

        blink_session = self.selected_session.blink_session
        state = blink_session.state
        messages_info = blink_session.info.streams.messages
        idle = str(state or '') in ('', 'initialized', 'ended', 'deleted')
        # connecting and hanging up are the chat and audio buttons of the bar on top of the session (ChatWidget.header)
        self.connect_audio_button.hide()
        self.connect_chat_button.hide()
        connected = str(state or '').startswith('connected')
        self.connection_status_label.setVisible(not idle and not connected)     # while connecting or disconnecting only
        self.hangup_button.setVisible(connected)
        if not idle and not connected:
            self.connection_status_label.setText(self._connection_status(blink_session))
        self.control_button.show()      # the wheel: always there; a connected session's menu (or cancel, while connecting)
        if idle:
            # not connected: the wheel connects
            self.control_button.setEnabled(True)
            self.control_button.setIcon(self.control_icon)
            menu.clear()
            menu.addAction(self.control_button.actions.connect_with_msrp)
            menu.addAction(self.control_button.actions.connect_with_audio)
            if conference_has_files(self.selected_session):     # a conference where files were exchanged
                menu.addSeparator()
                menu.addAction(self.control_button.actions.show_transferred_files)
            menu.addSeparator()
            menu.addAction(self.control_button.actions.remove_conversation)
            self.control_button.setMenu(menu)
            return
        if state == 'connecting/*' and blink_session.direction == 'outgoing' or state == 'connected/sent_proposal':
            self.control_button.setMenu(None)
            self.control_button.setIcon(self.cancel_icon)
            self.control_button.setEnabled(True)
        elif state == 'connected/received_proposal':
            self.control_button.setEnabled(False)
        else:
            self.control_button.setEnabled(True)
            self.control_button.setIcon(self.control_icon)
            menu.clear()
            menu.addAction(self.control_button.actions.mark_messages_read)
            menu.addSeparator()
            if conference_takes_files(blink_session):
                menu.addAction(self.control_button.actions.send_files)
            if conference_has_files(self.selected_session):     # a conference where files were exchanged
                menu.addAction(self.control_button.actions.show_transferred_files)
            if True:    # only a connected session gets here (idle ones have the connect buttons)
                if state == 'connected':
                    stream_types = blink_session.streams.types
                    if 'chat' not in stream_types:
                        if messages_info.encryption != 'OTR':
                            if self.selected_session.chat_widget.otr_timer.isActive():
                                menu.addAction(self.control_button.actions.enable_otr_progress)
                            else:
                                if messages_info.encryption != 'OpenPGP':
                                    menu.addAction(self.control_button.actions.enable_otr)
                        else:
                            if messages_info.encryption != 'OpenPGP':
                                menu.addAction(self.control_button.actions.disable_otr)
                    if 'audio' not in stream_types:
                        menu.addAction(self.control_button.actions.add_audio)
                    elif stream_types != {'audio'} and not stream_types.intersection({'screen-sharing', 'video'}):
                        menu.addAction(self.control_button.actions.remove_audio)
                    if 'video' not in stream_types:
                        if not blink_session.is_conference:     # no SIP video in conferences
                            menu.addAction(self.control_button.actions.add_video)
                    elif stream_types != {'video'}:
                        menu.addAction(self.control_button.actions.remove_video)
                    if 'screen-sharing' not in stream_types:
                        if not blink_session.is_conference and vnc_screen_sharing_offered():  # no screen sharing with a conference
                            menu.addAction(self.control_button.actions.request_screen)
                            menu.addAction(self.control_button.actions.share_my_screen)
                            self.control_button.actions.request_screen.setEnabled(can_view_screens())
                            self.control_button.actions.share_my_screen.setEnabled(can_share_my_screen())
                    elif stream_types != {'screen-sharing'}:
                        menu.addAction(self.control_button.actions.end_screen_sharing)
                    if 'chat' not in stream_types:
                        menu.addAction(self.control_button.actions.add_chat)
                    elif stream_types != {'chat'}:
                        menu.addAction(self.control_button.actions.remove_chat)
                    # Disconnect is the Hang up button on the bar, next to the connect buttons

            if state != 'connected':
                menu.addSeparator()
                menu.addAction(self.control_button.actions.remove_conversation)
            self.control_button.setMenu(menu)

    def _update_panel_buttons(self):
        self.info_panel_participants_button.setVisible(self.selected_session.blink_session.is_conference)
        self.info_panel_files_button.setVisible(self._shows_files(self.selected_session))
        self.files_panel_participants_button.hide()
        self.participants_panel_files_button.hide()

    def _update_session_info_panel(self, elements=set(), update_visibility=False):
        blink_session = self.selected_session.blink_session
        have_session = blink_session.state in ('connecting/*', 'connected/*', 'ending')

        if update_visibility:
            self.status_value_label.setEnabled(have_session)
            self.duration_value_label.setEnabled(have_session)
            self.account_value_label.setEnabled(have_session)
            self.remote_agent_value_label.setEnabled(have_session)
            self.audio_value_widget.setEnabled('audio' in blink_session.streams)
            self.video_value_widget.setEnabled('video' in blink_session.streams)
            self.chat_value_widget.setEnabled('chat' in blink_session.streams)
            self.screen_value_widget.setEnabled('screen-sharing' in blink_session.streams)

        session_info = blink_session.info
        audio_info = blink_session.info.streams.audio
        video_info = blink_session.info.streams.video
        chat_info = blink_session.info.streams.chat
        messages_info = blink_session.info.streams.messages
        screen_info = blink_session.info.streams.screen_sharing
        state = "%s" % blink_session.state

        if 'status' in elements and blink_session.state in ('initialized', 'connecting/*', 'connected/*', 'ended'):
            state_map = {'initialized': translate('chat_window', 'Disconnected'),
                         'connecting/dns_lookup': translate('chat_window', "Finding destination..."),
                         'connecting': translate('chat_window', "Connecting..."),
                         'connecting/ringing': translate('chat_window', "Ringing"),
                         'connecting/starting': translate('chat_window', "Starting media..."),
                         'connected': translate('chat_window', "Connected")}

            if blink_session.state == 'ended':
                self.status_value_label.setForegroundRole(QPalette.ColorRole.AlternateBase if blink_session.state.error else QPalette.ColorRole.WindowText)
                self.status_value_label.setText(blink_session.state.reason)

                self.chat_value_widget.setEnabled(True)
                self.chat_value_label.setText(translate('chat_window', "Using SIP Message"))
                if blink_session.chat_type is not None:
                    self.chat_encryption_label.setVisible(False)
                    self.chat_connection_label.setVisible(False)
            elif state in state_map:
                self.status_value_label.setForegroundRole(QPalette.ColorRole.WindowText)
                self.status_value_label.setText(state_map[state])

            want_duration = blink_session.state == 'connected/*' or blink_session.state == 'ended' and not blink_session.state.error
            self.status_title_label.setVisible(not want_duration)
            self.status_value_label.setVisible(not want_duration)
            self.duration_title_label.setVisible(want_duration)
            self.duration_value_label.setVisible(want_duration)

        if 'session' in elements:
            self.account_value_label.setText(blink_session.account.id)
            self.remote_agent_value_label.setText(session_info.remote_user_agent or translate('chat_window', 'N/A'))

        if 'media' in elements:
            self.audio_value_label.setText(audio_info.codec or translate('chat_window', 'N/A'))
            if audio_info.ice_status == 'succeeded':
                if 'relay' in {candidate.type.lower() for candidate in (audio_info.local_rtp_candidate, audio_info.remote_rtp_candidate)}:
                    self.audio_connection_label.setPixmap(self.pixmaps.relay_connection)
                    self.audio_connection_label.setToolTip(translate('chat_window', "Using relay"))
                else:
                    self.audio_connection_label.setPixmap(self.pixmaps.direct_connection)
                    self.audio_connection_label.setToolTip(translate('chat_window', "Peer to peer"))
            elif audio_info.ice_status == 'failed':
                self.audio_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.audio_connection_label.setToolTip(translate('chat_window', "Couldn't negotiate ICE"))
            elif audio_info.ice_status == 'disabled':
                if blink_session.contact.type == 'bonjour':
                    self.audio_connection_label.setPixmap(self.pixmaps.direct_connection)
                    self.audio_connection_label.setToolTip(translate('chat_window', "Peer to peer"))
                else:
                    self.audio_connection_label.setPixmap(self.pixmaps.unknown_connection)
                    self.audio_connection_label.setToolTip(translate('chat_window', "ICE is disabled"))
            elif audio_info.ice_status is None:
                self.audio_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.audio_connection_label.setToolTip(translate('chat_window', "ICE is unavailable"))
            else:
                self.audio_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.audio_connection_label.setToolTip(translate('chat_window', "Negotiating ICE"))

            if audio_info.encryption is not None:
                self.audio_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using %s (%s)") % (audio_info.encryption, audio_info.encryption_cipher))
            else:
                self.audio_encryption_label.setToolTip(translate('chat_window', "Media is not encrypted"))
            self._update_rtp_encryption_icon(self.audio_encryption_label)

            self.audio_connection_label.setVisible(audio_info.remote_address is not None)
            self.audio_encryption_label.setVisible(audio_info.encryption is not None)

            self.video_value_label.setText(video_info.codec or translate('chat_window', 'N/A'))
            if video_info.ice_status == 'succeeded':
                if 'relay' in {candidate.type.lower() for candidate in (video_info.local_rtp_candidate, video_info.remote_rtp_candidate)}:
                    self.video_connection_label.setPixmap(self.pixmaps.relay_connection)
                    self.video_connection_label.setToolTip(translate('chat_window', "Using relay"))
                else:
                    self.video_connection_label.setPixmap(self.pixmaps.direct_connection)
                    self.video_connection_label.setToolTip(translate('chat_window', "Peer to peer"))
            elif video_info.ice_status == 'failed':
                self.video_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.video_connection_label.setToolTip("Couldn't negotiate ICE")
            elif video_info.ice_status == 'disabled':
                if blink_session.contact.type == 'bonjour':
                    self.video_connection_label.setPixmap(self.pixmaps.direct_connection)
                    self.video_connection_label.setToolTip(translate('chat_window', "Peer to peer"))
                else:
                    self.video_connection_label.setPixmap(self.pixmaps.unknown_connection)
                    self.video_connection_label.setToolTip(translate('chat_window', "ICE is disabled"))
            elif video_info.ice_status is None:
                self.video_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.video_connection_label.setToolTip(translate('chat_window', "ICE is unavailable"))
            else:
                self.video_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.video_connection_label.setToolTip(translate('chat_window', "Negotiating ICE"))

            if video_info.encryption is not None:
                self.video_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using %s (%s)") % (video_info.encryption, video_info.encryption_cipher))
            else:
                self.video_encryption_label.setToolTip(translate('chat_window', "Media is not encrypted"))
            self._update_rtp_encryption_icon(self.video_encryption_label)

            self.video_connection_label.setVisible(video_info.remote_address is not None)
            self.video_encryption_label.setVisible(video_info.encryption is not None)

            if self.zrtp_widget.isVisibleTo(self.info_panel):
                # refresh the ZRTP widget (we need to hide/change/show because in certain configurations it flickers when changed while visible)
                stream_info = blink_session.info.streams[self.zrtp_widget.stream_type]
                self.zrtp_widget.hide()
                self.zrtp_widget.peer_name = stream_info.zrtp_peer_name
                self.zrtp_widget.peer_verified = stream_info.zrtp_verified
                self.zrtp_widget.sas = stream_info.zrtp_sas
                self.zrtp_widget.show()

            if any(len(path) > 1 for path in (chat_info.full_local_path, chat_info.full_remote_path)):
                self.chat_value_label.setText(translate('chat_window', "Using relay"))
                self.chat_connection_label.setPixmap(self.pixmaps.relay_connection)
                self.chat_connection_label.setToolTip(translate('chat_window', "Using relay"))
            elif chat_info.full_local_path and chat_info.full_remote_path:
                self.chat_value_label.setText(translate('chat_window', "Peer to peer"))
                self.chat_connection_label.setPixmap(self.pixmaps.direct_connection)
                self.chat_connection_label.setToolTip(translate('chat_window', "Peer to peer"))
            elif blink_session.chat_type is None:
                self.chat_value_widget.setEnabled(True)
                self.chat_value_label.setText(translate('chat_window', "Using SIP Message"))
                self.chat_connection_label.setToolTip(translate('chat_window', "Using SIP Message"))
            else:
                self.chat_value_label.setText(translate('chat_window', "N/A"))

            if chat_info.encryption is not None and chat_info.transport == 'tls':
                self.chat_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using TLS and {0.encryption} ({0.encryption_cipher})").format(chat_info))
            elif chat_info.encryption is not None:
                self.chat_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using {0.encryption} ({0.encryption_cipher})").format(chat_info))
            elif chat_info.transport == 'tls':
                self.chat_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using TLS"))
            else:
                self.chat_encryption_label.setToolTip(translate('chat_window', "Media is not encrypted"))
            if messages_info.encryption is not None:
                if messages_info.encryption == 'OpenPGP':
                    self.chat_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using {0.encryption}").format(messages_info))
                else:
                    self.chat_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using {0.encryption} ({0.encryption_cipher})").format(messages_info))

            self._update_chat_encryption_icon()

            self.chat_connection_label.setVisible(chat_info.remote_address is not None)
            self.chat_encryption_label.setVisible((chat_info.remote_address is not None and (chat_info.encryption is not None or chat_info.transport == 'tls')) or messages_info.encryption is not None)

            if self.otr_widget.isVisibleTo(self.info_panel):
                # refresh the OTR widget (we need to hide/change/show because in certain configurations it flickers when changed while visible)
                stream_info = blink_session.info.streams.chat or blink_session.info.streams.messages
                self.otr_widget.hide()
                self.otr_widget.peer_name = stream_info.otr_peer_name
                self.otr_widget.peer_verified = stream_info.otr_verified
                self.otr_widget.peer_fingerprint = stream_info.otr_peer_fingerprint
                self.otr_widget.my_fingerprint = stream_info.otr_key_fingerprint
                self.otr_widget.smp_status = stream_info.smp_status
                self.otr_widget.show()

            if screen_info.remote_address is not None and screen_info.mode == 'active':
                self.screen_value_label.setText(translate('chat_window', "Viewing remote"))
            elif screen_info.remote_address is not None and screen_info.mode == 'passive':
                self.screen_value_label.setText(translate('chat_window', "Sharing local"))
            else:
                self.screen_value_label.setText(translate('chat_window', "N/A"))

            if any(len(path) > 1 for path in (screen_info.full_local_path, screen_info.full_remote_path)):
                self.screen_connection_label.setPixmap(self.pixmaps.relay_connection)
                self.screen_connection_label.setToolTip(translate('chat_window', "Using relay"))
            elif screen_info.full_local_path and screen_info.full_remote_path:
                self.screen_connection_label.setPixmap(self.pixmaps.direct_connection)
                self.screen_connection_label.setToolTip(translate('chat_window', "Peer to peer"))

            self.screen_encryption_label.setToolTip(translate('chat_window', "Media is encrypted using TLS"))

            self.screen_connection_label.setVisible(screen_info.remote_address is not None)
            self.screen_encryption_label.setVisible(screen_info.remote_address is not None and screen_info.transport == 'tls')

        if 'statistics' in elements:
            self.duration_value_label.value = session_info.duration
            self.audio_latency_graph.data = audio_info.latency
            self.video_latency_graph.data = video_info.latency
            self.audio_packet_loss_graph.data = audio_info.packet_loss
            self.video_packet_loss_graph.data = video_info.packet_loss
            self.incoming_traffic_graph.data = audio_info.incoming_traffic
            self.outgoing_traffic_graph.data = audio_info.outgoing_traffic
            self.latency_graph.update()
            self.packet_loss_graph.update()
            self.traffic_graph.update()

    def _update_rtp_encryption_icon(self, encryption_label):
        stream = self.selected_session.blink_session.streams.get(encryption_label.stream_type)
        stream_info = self.selected_session.blink_session.info.streams[encryption_label.stream_type]
        if encryption_label.isEnabled() and stream_info.encryption == 'ZRTP':
            if encryption_label.hovered and stream is not None and not stream._done:
                encryption_label.setPixmap(self.pixmaps.light_green_lock if stream_info.zrtp_verified else self.pixmaps.light_orange_lock)
            else:
                encryption_label.setPixmap(self.pixmaps.green_lock if stream_info.zrtp_verified else self.pixmaps.orange_lock)
        else:
            encryption_label.setPixmap(self.pixmaps.grey_lock)

    def _update_chat_encryption_icon(self):
        stream = self.selected_session.chat_stream
        stream_info = self.selected_session.blink_session.info.streams.chat
        messages_info = self.selected_session.blink_session.info.streams.messages
        if self.chat_encryption_label.isEnabled() and stream_info.encryption == 'OTR':
            if self.chat_encryption_label.hovered and stream is not None and not stream._done:
                self.chat_encryption_label.setPixmap(self.pixmaps.light_green_lock if stream_info.otr_verified else self.pixmaps.light_orange_lock)
            else:
                self.chat_encryption_label.setPixmap(self.pixmaps.green_lock if stream_info.otr_verified else self.pixmaps.orange_lock)
        elif self.chat_encryption_label.isEnabled() and messages_info.encryption == 'OpenPGP':
            self.chat_encryption_label.setPixmap(self.pixmaps.green_lock)
        else:
            self.chat_encryption_label.setPixmap(self.pixmaps.grey_lock)

    def show(self):
        self._hide_message_only_sessions()
        if self.session_model.rowCount() and not self.has_visible_sessions():
            # only message conversations (the message pane's): nothing to show in this window
            self.hide()
            return
        super(ChatWindow, self).show()
        self.raise_()
        self.activateWindow()
        self.showNormal()

    def _hide_message_only_sessions(self):
        """SIP message conversations are the message pane's: their rows are hidden here (the
        window still routes their messages: unread counts, receipts), and one selected is left."""
        model = self.session_model
        selection_model = self.session_list.selectionModel()
        for position, session in enumerate(model.sessions):
            self.session_list.setRowHidden(position, not self._shows_session(session.blink_session))
        if self.selected_session is not None and not self._shows_session(self.selected_session.blink_session):
            visible = next((position for position, session in enumerate(model.sessions) if self._shows_session(session.blink_session)), None)
            if visible is None:
                selection_model.clearSelection()
            else:
                selection_model.select(model.index(visible), selection_model.SelectionFlag.ClearAndSelect)

    def has_visible_sessions(self):
        return any(self._shows_session(session.blink_session) for session in self.session_model.sessions)
        # if not self.session_model.rowCount():
        #     history = HistoryManager()
        #     history.get_last_contacts()

    def show_session_info(self):
        self.show()
        self.session_list.hide()
        if not self.selected_session:
            return

        if self.selected_session.active_panel != self.info_panel:
            if self.sliding_panels:
                self._slide_details(self.info_panel, direction=self.slide_direction)
            else:
                self._set_details(self.info_panel)
            self.selected_session.active_panel = self.info_panel

    def show_with_messages(self):
        self._hide_message_only_sessions()
        super(ChatWindow, self).show()
        self.raise_()
        self.activateWindow()
        self.showNormal()
        history = HistoryManager()
        history.get_last_contacts()

    def show_unread_messages(self):
        super(ChatWindow, self).show()
        self.raise_()
        self.activateWindow()
        self.showNormal()
        history = HistoryManager()
        history.get_last_contacts(unread=True)

    def closeEvent(self, event):
        self._stop_radio_pulse()
        ui_settings().setValue("chat_window/geometry", self.saveGeometry())
        ui_settings().setValue("chat_window/splitter", self.splitter.saveState())
        super(ChatWindow, self).closeEvent(event)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if watched is self.session_widget:
            if event_type == QEvent.Type.HoverEnter:
                watched.hovered = True
            elif event_type == QEvent.Type.HoverLeave:
                watched.hovered = False
            elif event_type == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                self._EH_ShowSessions()
        elif watched is self.state_label:
            if event_type == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                upper_half = QRect(0, 0, int(self.state_label.width()), int(self.state_label.height() / 2))
                if upper_half.contains(event.pos()):
                    self._EH_CloseSession()
                else:
                    self._EH_ShowSessions()
            elif event_type == QEvent.Type.Paint:  # and self.session_widget.hovered:
                watched.event(event)
                self.drawSessionWidgetIndicators()
                return True
        elif watched in (self.latency_graph, self.packet_loss_graph, self.traffic_graph):
            if event_type == QEvent.Type.Wheel and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                settings = BlinkSettings()
                wheel_delta = event.angleDelta().y()
                if wheel_delta > 0 and settings.chat_window.session_info.graph_time_scale > GraphTimeScale.min_value:
                    settings.chat_window.session_info.graph_time_scale -= 1
                    settings.save()
                elif wheel_delta < 0 and settings.chat_window.session_info.graph_time_scale < GraphTimeScale.max_value:
                    settings.chat_window.session_info.graph_time_scale += 1
                    settings.save()
        elif watched in (self.audio_encryption_label, self.video_encryption_label):
            if event_type == QEvent.Type.Enter:
                watched.hovered = True
                self._update_rtp_encryption_icon(watched)
            elif event_type == QEvent.Type.Leave:
                watched.hovered = False
                self._update_rtp_encryption_icon(watched)
            elif event_type == QEvent.Type.EnabledChange and not watched.isEnabled():
                watched.setPixmap(self.pixmaps.grey_lock)
            elif event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick) and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier and watched.isEnabled():
                self._EH_RTPEncryptionLabelClicked(watched)
        elif watched is self.chat_encryption_label:
            if event_type == QEvent.Type.Enter:
                watched.hovered = True
                self._update_chat_encryption_icon()
            elif event_type == QEvent.Type.Leave:
                watched.hovered = False
                self._update_chat_encryption_icon()
            elif event_type == QEvent.Type.EnabledChange and not watched.isEnabled():
                watched.setPixmap(self.pixmaps.grey_lock)
            elif event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick) and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier and watched.isEnabled():
                self._EH_ChatEncryptionLabelClicked()
        elif watched is self.info_panel:
            if event_type == QEvent.Type.Resize:
                if self.zrtp_widget.isVisibleTo(self.info_panel):
                    rect = self.zrtp_widget.geometry()
                    rect.setWidth(self.info_panel.width())
                    self.zrtp_widget.setGeometry(rect)
                if self.otr_widget.isVisibleTo(self.info_panel):
                    rect = self.otr_widget.geometry()
                    rect.setWidth(self.info_panel.width())
                    self.otr_widget.setGeometry(rect)
        return False

    def drawSessionWidgetIndicators(self):
        painter = QPainter(self.state_label)
        palette = self.state_label.palette()
        rect = self.state_label.rect()

        pen_thickness = 1.6

        if self.state_label.state is not None:
            background_color = self.state_label.state_colors[self.state_label.state]
            base_contrast_color = self.calc_light_color(background_color)
            gradient = QLinearGradient(0, 0, 1, 0)
            gradient.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectBoundingMode)
            gradient.setColorAt(0.0, self.color_with_alpha(base_contrast_color, 0.3 * 255))
            gradient.setColorAt(1.0, self.color_with_alpha(base_contrast_color, 0.8 * 255))
            contrast_color = QBrush(gradient)
        else:
            background_color = palette.color(QPalette.ColorRole.Window)
            contrast_color = self.calc_light_color(background_color)
        foreground_color = palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
        line_color = self.deco_color(background_color, foreground_color)

        pen = QPen(line_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        contrast_pen = QPen(contrast_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

        # draw the expansion indicator at the bottom (works best with a state_label of width 14)
        arrow_rect = QRect(0, 0, 14, 14)
        arrow_rect.moveBottomRight(rect.bottomRight())

        arrow = QPolygonF([QPointF(3, 1.5), QPointF(-0.5, -2.5), QPointF(-4, 1.5)])
        arrow.translate(2, 1)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(arrow_rect.center())
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawPolyline(arrow)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawPolyline(arrow)
        painter.restore()

        # draw the close indicator at the top (works best with a state_label of width 14)
        cross_rect = QRect(0, 0, 14, 14)
        cross_rect.moveTopRight(rect.topRight())

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(cross_rect.center())
        painter.translate(+1.5, +1)
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawLine(-3, -3, 3, 3)
        painter.drawLine(-3, 3, 3, -3)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawLine(-3, -3, 3, 3)
        painter.drawLine(-3, 3, 3, -3)
        painter.restore()

    def _open_from_notification(self, session):
        """Clicked desktop notification of a chat message: this window in front, on that session."""
        if session not in self.session_model.sessions:
            return
        model = self.session_model
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(model.sessions.index(session)), selection_model.SelectionFlag.ClearAndSelect)
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event):
        super(ChatWindow, self).changeEvent(event)
        if event.type() in (QEvent.Type.ActivationChange, QEvent.Type.WindowStateChange) and self.isActiveWindow() and not self.isMinimized():
            self._withdraw_notification(self.selected_session)     # looking at it now: its notification goes

    def _withdraw_notification(self, session):
        notifier = getattr(QApplication.instance().main_window, 'message_notifier', None)
        if notifier is not None and session is not None and session.blink_session is not None:
            notifier.withdraw_chat(session.blink_session)

    def confirm_read_messages(self, session):
        if self.selected_session:
            NotificationCenter().post_notification('BlinkSessionConfirmReadMessages', sender=session.blink_session)

        if session and session.blink_session in self.pending_displayed_notifications:
            MessageManager().send_conversation_read(session.blink_session)
            item = self.pending_displayed_notifications.pop(self.selected_session.blink_session)
            for (id, timestamp, account) in item:
                MessageManager().send_imdn_message(session.blink_session, id, timestamp, 'displayed', account)
        else:
            self.mark_read()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange')

        blink_settings = BlinkSettings()
        self.latency_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
        self.packet_loss_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
        self.traffic_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
        self.latency_graph.update()
        self.packet_loss_graph.update()
        self.traffic_graph.update()

        self.dummy_tab = ChatWidget(None, self.tab_widget)
        self.dummy_tab.setDisabled(True)
        self.tab_widget.addTab(self.dummy_tab, "Dummy")
        self.tab_widget.setCurrentWidget(self.dummy_tab)

        QTimer.singleShot(3000, self._restore_conference_tiles)     # once the accounts and the addressbook are in

    # The tiles are remembered, conferences and one to one MSRP chats alike: the window opens with
    # those it had, newest on top; a tile goes only when closed (its x).
    settings_key = 'chat_window/conference_tiles'

    def _NH_SIPApplicationWillEnd(self, notification):
        self._quitting = True       # sessions ending and going away on quit are not closed tiles

    def _save_conference_tiles(self, keep=()):
        """Remember the tiles shown; keep: remembered tiles that did not come up this time (they are
        not forgotten because of a failure: only closing a tile forgets it)."""
        if getattr(self, '_restoring_tiles', False) or getattr(self, '_quitting', False):
            return
        tiles = []
        for item in self.session_model.sessions:
            blink_session = item.blink_session
            if blink_session.contact_uri is None or not self._shows_session(blink_session):
                continue    # a SIP message conversation (the message pane's) is not a tile here
            account = getattr(blink_session.account, 'id', None)
            tiles.append({'uri': str(blink_session.contact_uri.uri), 'account': str(account) if account else None})
        shown = {tile['uri'].lower() for tile in tiles}
        tiles.extend(tile for tile in keep if tile.get('uri') and tile['uri'].lower() not in shown)
        ui_settings().setValue(self.settings_key, json.dumps(tiles))

    # one tile per pass of the event loop: building a tile (its ChatWidget) is mostly C++ widget
    # construction, done with the GIL held; all of them in one go kept the SIP reactor thread
    # (DNS lookups, timers) from running for seconds at the start
    tile_restore_interval = 0      # ms between two tiles

    def _restore_conference_tiles(self):
        try:
            tiles = json.loads(ui_settings().value(self.settings_key) or '[]')
        except (TypeError, ValueError):
            tiles = []
        if not tiles:
            return
        present = {str(item.blink_session.contact_uri.uri).lower() for item in self.session_model.sessions
                   if item.blink_session.contact_uri is not None and self._shows_session(item.blink_session)}
        hidden = {str(item.blink_session.contact_uri.uri).lower(): item for item in self.session_model.sessions
                  if item.blink_session.contact_uri is not None and not self._shows_session(item.blink_session)}
        self._restoring_tiles = True
        pending = list(reversed(tiles))       # the bottom one first: each newer one goes on top
        self._restore_next_tile(tiles, pending, present, hidden, 0)

    def _restore_next_tile(self, tiles, pending, present, hidden, restored):
        from blink.contacts import URIUtils
        account_manager = AccountManager()
        try:
            while pending:
                tile = pending.pop(0)
                uri = tile.get('uri')
                if not uri or uri.lower() in present:
                    continue
                if uri.lower() in hidden:
                    # its session is here already, as a message conversation: it is the tile again
                    hidden[uri.lower()].msrp_tile = True
                    present.add(uri.lower())
                    restored += 1
                    continue        # nothing built: the next one in this pass
                try:
                    account = account_manager.get_account(tile['account']) if tile.get('account') else account_manager.default_account
                except KeyError:
                    account = account_manager.default_account
                if account is None or not account.enabled:
                    continue
                contact, contact_uri = URIUtils.find_contact(uri)
                SessionManager().create_session(contact, contact_uri, [StreamDescription('chat')], account=account, connect=False)
                present.add(uri.lower())
                restored += 1
                break               # one tile built: the rest after the event loop has had a turn
        except Exception as e:
            ActivityLog().error(f'[ui] Chat window: restoring a tile failed: {e!r}')
        if pending:
            QTimer.singleShot(self.tile_restore_interval, lambda: self._restore_next_tile(tiles, pending, present, hidden, restored))
            return
        self._restoring_tiles = False
        self._hide_message_only_sessions()      # the rows of tiles made visible again
        shown = {str(item.blink_session.contact_uri.uri).lower() for item in self.session_model.sessions
                 if item.blink_session.contact_uri is not None and self._shows_session(item.blink_session)}
        missing = [tile for tile in tiles if tile.get('uri') and tile['uri'].lower() not in shown]
        self._save_conference_tiles(keep=missing)
        if restored:
            ActivityLog().info(f'[ui] Chat window: {restored - len(missing)} of {len(tiles)} tiles restored')
        if missing:
            ActivityLog().warning(f'[ui] Chat window: {len(missing)} tiles did not come up, kept for the next start: {", ".join(tile["uri"] for tile in missing)}')

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        blink_settings = BlinkSettings()
        if notification.sender is settings:
            if 'audio.muted' in notification.data.modified:
                self.mute_button.setChecked(settings.audio.muted)
        elif notification.sender is blink_settings:
            if 'chat_window.session_info.bytes_per_second' in notification.data.modified:
                self.traffic_graph.update()
            if 'chat_window.session_info.graph_time_scale' in notification.data.modified:
                self.latency_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
                self.packet_loss_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
                self.traffic_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
                self.latency_graph.update()
                self.packet_loss_graph.update()
                self.traffic_graph.update()

    def _NH_BlinkSessionNewIncoming(self, notification):
        if notification.sender.streams.types.intersection(self.__streamtypes__):
            if self.session_model.rowCount():
                call_later(.5, self._NH_BlinkSessionIsSelected, notification)
            else:
                self.show()

    def _NH_BlinkSessionNewOutgoing(self, notification):
        if getattr(self, '_restoring_tiles', False):
            return      # the remembered tiles come back without opening the window
        if notification.sender.stream_descriptions.types.intersection(self.__streamtypes__):
            if self.session_model.rowCount():
                call_later(.5, self._NH_BlinkSessionIsSelected, notification)
            else:
                self.show()

    def _NH_BlinkSessionDidReinitializeForIncoming(self, notification):
        model = self.session_model
        position = model.sessions.index(notification.sender.items.chat)
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.SelectionFlag.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.ScrollHint.EnsureVisible)  # or PositionAtCenter
        if notification.sender.streams.types.intersection(self.__streamtypes__):
            self.show()

    def _NH_BlinkSessionDidReinitializeForOutgoing(self, notification):
        model = self.session_model
        position = model.sessions.index(notification.sender.items.chat)
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.SelectionFlag.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.ScrollHint.EnsureVisible)  # or PositionAtCenter
        if notification.sender.stream_descriptions.types.intersection(self.__streamtypes__):
            self.show()

    # use BlinkSessionNewIncoming/Outgoing to show the chat window if there is a chat stream available (like with reinitialize) instead of using the sessionAdded signal from the model -Dan
    # or maybe not. sessionAdded means it was added to the model, while during NewIncoming/Outgoing we do not know that yet. but then we have a problem with the DidReinitialize since
    # they do not check if the session is in the model. maybe the right approach is to always have BlinkSessions in the model and if we need any other kind of sessions we create a
    # different class for them that posts different notifications. in that case we can do in in NewIncoming/Outgoing -Dan

    def _NH_BlinkSessionWillAddStream(self, notification):
        if notification.data.stream.type in self.__streamtypes__:
            self.show()

    def _shows_session(self, session):
        """Whether this window is where a session is shown: a conference (connected or not, so its
        tile stays to switch to and reconnect), MSRP chat and screen sharing. A conversation of SIP
        messages only is shown in the main window's message pane."""
        if getattr(session, 'is_conference', False):
            return True
        item = getattr(getattr(session, 'items', None), 'chat', None)
        if getattr(item, 'msrp_tile', False):
            return True         # had MSRP chat here once: its tile stays (ended or not) until closed
        types = set()
        for attribute in ('streams', 'stream_descriptions'):
            try:
                types.update(getattr(session, attribute).types)
            except AttributeError:
                pass
        shown = bool(types & self.__streamtypes__)
        if shown and item is not None:
            item.msrp_tile = True
        return shown

    def _NH_BlinkSessionIsSelected(self, notification):
        session = notification.sender
        if not self._shows_session(session):
            main_window = QApplication.instance().main_window
            main_window.show_conversation_in_pane(session.contact, session.contact_uri)
            return
        model = self.session_model
        position = model.sessions.index(notification.sender.items.chat)
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.SelectionFlag.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.ScrollHint.EnsureVisible)  # or PositionAtCenter
        self.show()

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        self._update_control_menu()
        self._update_session_info_panel(update_visibility=True)
        self._update_radio_ptt()

    def _NH_BlinkSessionDidAddStream(self, notification):
        self._update_radio_ptt()

    def _NH_BlinkSessionDidChangeState(self, notification):
        # even if we use this, we also need to listen for BlinkSessionDidRemoveStream as that transition doesn't change the state at all -Dan
        self._update_control_menu()
        self._update_panel_buttons()
        self._update_window_title()  # remote_focus is only known once connected
        self._update_radio_ptt()
        if self.selected_session is not None and self.selected_session.blink_session is notification.sender:
            self._show_conference_participants(self.selected_session)
            old_state = str(notification.data.old_state or '')
            new_state = str(notification.data.new_state or '')
            if notification.sender.is_conference and new_state.startswith('connected') and not old_state.startswith('connected'):
                self._expand_selected_session()     # once per connect: the user may go back to the list afterwards
        self._update_session_info_panel(elements={'status'}, update_visibility=True)

    def _expand_selected_session(self):
        """Collapse the session list onto the selected tile, as its expand arrow does."""
        if not self.session_list.isVisibleTo(self) or not session_can_expand(self.selected_session):
            return
        ActivityLog().info(f'[chat window] Expand the tile of {self.selected_session.blink_session.uri} on connect')
        if self.isVisible():
            self.session_list.animation.setDirection(QPropertyAnimation.Direction.Backward)
            self.session_list.animation.start()
        else:
            self.session_list.hide()

    def _NH_BlinkSessionDidEnd(self, notification):
        self._update_radio_ptt()
        if not self.session_list.isVisibleTo(self) and self.isVisible() and not session_can_expand(self.selected_session):
            self._EH_ShowSessions()     # an ended session is not expanded (unless a conference with files): back to the list
        if self.selected_session.active_panel is not self.info_panel:
            if self.sliding_panels:
                self._slide_details(self.info_panel, direction=self.slide_direction)
            else:
                self._set_details(self.info_panel)
            self.selected_session.active_panel = self.info_panel

    def _NH_BlinkSessionInfoUpdated(self, notification):
        self._update_session_info_panel(elements=notification.data.elements)

    def _NH_BlinkSessionWillAddParticipant(self, notification):
        if self.selected_session is not None and self.selected_session.blink_session is notification.sender:
            self._update_panel_buttons()
            self._show_conference_participants(self.selected_session)
            self._update_radio_ptt()

    def _NH_BlinkSessionDidRemoveParticipant(self, notification):
        if self.selected_session is not None and self.selected_session.blink_session is notification.sender:
            self._update_radio_ptt()

    def _NH_ConferenceParticipantDidChange(self, notification):
        session = self.selected_session
        if session is None or session.blink_session is None:
            return
        server_conference = getattr(session.blink_session, 'server_conference', None)
        if server_conference is not None and notification.sender in server_conference.participants.values():
            self._update_radio_ptt()

    @staticmethod
    def _shows_files(session):
        """The files pane is a connected conference's, once files were shared in it: a one to one
        chat, or a conference not connected, has none."""
        if session is None or session.blink_session is None:
            return False
        return str(session.blink_session.state or '').startswith('connected') and conference_has_files(session)

    def _details_page(self, panel):
        """The participants and the files share one page, a connected conference's. A conference
        shows the info panel here only when asked for with the i button (info_requested), otherwise
        an empty page until it is connected, then its participants (and its files, once some were
        shared). A one to one chat has the info panel only."""
        session = self.selected_session
        conference = session is not None and session.blink_session.is_conference
        if conference and panel is self.info_panel and getattr(session, 'info_requested', False):
            return self.info_panel
        connected = session is not None and str(session.blink_session.state or '').startswith('connected')
        if conference:
            if not connected:
                return self.details_placeholder
            panel = self.participants_panel
        elif panel in (self.participants_panel, self.files_panel):
            return self.info_panel
        if panel in (self.participants_panel, self.files_panel):
            self.participants_panel.setVisible(True)
            self.files_panel.setVisible(self._shows_files(session))     # no files shared yet: no files pane
            self.files_panel_info_button.hide()     # one info button per column
            return self.details_splitter
        return panel

    def _slide_details(self, panel, direction=None):
        page = self._details_page(panel)
        if direction is None:
            self.session_details.slideInWidget(page)
        else:
            self.session_details.slideInWidget(page, direction=direction)

    def _set_details(self, panel):
        self.session_details.setCurrentWidget(self._details_page(panel))

    def _show_conference_participants(self, session):
        """A conference shows its participants in the left panel, like the drawer of Blink for macOS
        (the session info lives in the audio drawer now): switch to them from the info panel as soon
        as the session is known to be a conference; the list fills as conference-info arrives."""
        blink_session = session.blink_session
        if not blink_session.is_conference:
            return
        if not str(blink_session.state or '').startswith('connected'):
            return  # the column appears once the conference is connected (from BlinkSessionDidChangeState)
        if session is self.selected_session:
            self.participants_panel.setVisible(True)
            self.files_panel.setVisible(self._shows_files(session))  # shown once the conference shares a file
            self.files_panel_info_button.hide()
            if self.session_list.isVisibleTo(self) and sum(1 for item in self.session_model.sessions if self._shows_session(item.blink_session)) < 2:
                self.session_list.hide()    # the session list covers the panel; kept when there are sessions to switch between
        if session.active_panel is not self.info_panel and (session is not self.selected_session or self.session_details.currentWidget() is self.details_splitter):
            return  # already in view
        if getattr(session, 'info_requested', False):
            return  # the user is looking at the info panel (i button): the participants wait for the participants button
        if self.sliding_panels:
            self._slide_details(self.participants_panel, direction=self.slide_direction)
        else:
            self._set_details(self.participants_panel)
        session.active_panel = self.participants_panel
        if not getattr(session, 'conference_participants_shown', False):
            session.conference_participants_shown = True
            ActivityLog().info(f'[conference] Showing the {len(blink_session.server_conference.participants)} participants of {blink_session.uri} in the chat window')

    def _NH_ChatSessionItemDidChange(self, notification):
        self._update_widgets_for_session()
        session = self.selected_session
        if session is not None and notification.sender is session and session.blink_session is not None and session.blink_session.is_conference:
            self.files_panel.setVisible(self._shows_files(session))  # the first file shared in the conference brings the files pane
            self.info_panel_files_button.setVisible(self._shows_files(session))

    def _parse_fthttp(self, blink_session, message, from_history=False, account=None):
        """The envelope of a file transfer message, drawn as a file bubble; None for a transfer
        that expired before its file was fetched. The file is fetched as the message pane
        fetches it (when in view, within limits, from contacts allowed to send files)."""
        if account is None:
            try:
                account = AccountManager().get_account(message.account_id)
            except (AttributeError, KeyError):
                account = blink_session.account

        document = FTHTTPDocument.parse(message.content)     # ParserError for the caller

        for info in document:
            try:
                hash = info.hash.value
            except AttributeError:
                hash = None

            file = File(info.file_name.value,
                        info.file_size.value,
                        blink_session.contact,
                        hash,
                        message.message_id if from_history else message.id,
                        info.data.until if info.data.until else None,
                        url=info.data.url,
                        type=info.content_type.value,
                        account=account,
                        protocol='sylk')
            file.name = HistoryManager().get_decrypted_filename(file)

            try:
                if info.data.until < datetime.now(timezone.utc) and not file.already_exists:
                    return None
            except (AttributeError, TypeError):
                pass

            if from_history:
                NotificationCenter().post_notification('BlinkSessionDidShareFile',
                                                       sender=blink_session,
                                                       data=NotificationData(file=file, direction=message.direction, history=True))
            return message.content
        return None

    @staticmethod
    def _image_content(message_id, content_type, content):
        """An image sent inside a chat message as a file of its own, and its envelope (or None)."""
        try:
            data = content if isinstance(content, bytes) else base64.b64decode(content.strip())
        except (ValueError, TypeError):
            return None
        return save_inline_image(message_id, content_type, data)

    def _NH_BlinkGotMessage(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat
        if session is None:
            return

        if not self.isVisible():
            #self.show_with_messages()
            pass #show now or allow the user to bring up the window?

        try:
             replicated_message = notification.data.replicated_message
        except AttributeError:
             replicated_message = False

        message = notification.data.message
        direction = notification.data.message.direction

        try:
            history = notification.data.history
        except AttributeError:
            history = False

        try:
            received_account = notification.data.account
        except AttributeError:
            received_account = None

        encrypted = False
        content_type = message.content_type
        is_file = False
        if message.content_type.startswith('image/'):
            content = self._image_content(message.id, message.content_type, message.content)
            if content is None:
                return
            content_type, is_file = FILE_TRANSFER_CONTENT_TYPE, True
        elif message.content_type.startswith('text/'):
            content = message.content
            if MessageManager().check_encryption(message.content_type, message.content) == 'OpenPGP':
                encrypted = True        # drawn as an encrypted message until it is decrypted
                session.chat_widget.pending_decryption.append((message))
        elif message.content_type.lower() == FTHTTPDocument.content_type:
            try:
                content = self._parse_fthttp(blink_session, message, account=received_account)
            except ParserError:
                return
            if content is None:
                return
            is_file = True
        else:
            return

        try:
            uri = '%s@%s' % (message.sender.uri.user.decode(), message.sender.uri.host.decode())
        except AttributeError:
            uri = '%s@%s' % (message.sender.uri.user, message.sender.uri.host)

        account_manager = AccountManager()
        if account_manager.has_account(uri):
            account = account_manager.get_account(uri)
            sender = ChatSender(message.sender.display_name or account.display_name, uri)
        else:
            account = None
            sender = ChatSender(message.sender.display_name or session.name, uri)

        if is_file:
            chat_message = ChatFile(content, sender, direction, id=message.id, timestamp=message.timestamp, history=history, account=received_account, content_type=content_type)
        else:
            chat_message = ChatMessage(content, sender, direction, id=message.id, timestamp=message.timestamp, history=history, account=received_account, content_type=content_type)

        if session.chat_widget.history_loaded:
            if message in session.chat_widget.pending_decryption and not encrypted:
                session.chat_widget.pending_decryption.remove(message)
                session.chat_widget.update_message_text(message.id, content, content_type)
            else:
                session.chat_widget.add_message(chat_message)
            session.chat_widget.update_message_encryption(message.id, message.is_secure)
            if received_account is not None and received_account.enabled and received_account != blink_session.account and not blink_session.remote_instance_id:
                blink_session.account = received_account
                NotificationCenter().post_notification('BlinkSessionMessageAccountChanged', sender=blink_session)
                NotificationCenter().post_notification('PGPKeysShouldReload', sender=blink_session)
            NotificationCenter().post_notification('BlinkSessionWasLoaded', sender=blink_session)
        else:
            self.render_after_load.append((session, received_account, chat_message))

        if getattr(notification.data, 'first_sync', False):
            # replayed by a first sync (thousands of them): drawn only; unread counts come from history
            # when the sync is settled, and no receipt, notification or sound goes out for it
            session.remote_composing = False
            return

        if direction != 'outgoing' and message.disposition is not None and 'display' in message.disposition and not encrypted:
            if self.selected_session is session and not self.isMinimized() and self.isActiveWindow():
                MessageManager().send_imdn_message(blink_session, message.id, message.timestamp, 'displayed', received_account)
            else:
                self.pending_displayed_notifications.setdefault(blink_session, []).append((message.id, message.timestamp, received_account))

        if direction == 'incoming':
            if self.selected_session is session and not self.isMinimized() and self.isActiveWindow():
                # seen as it arrives: read in history too
                NotificationCenter().post_notification('BlinkSessionConfirmReadMessages', sender=blink_session)
            else:
                # unread counts are keyed like history: a Bonjour neighbour by its instance id
                NotificationCenter().post_notification('BlinkMessageNewUnread', sender=blink_session.remote_instance_id or uri)

        if direction != 'outgoing':
            if self.selected_session is session and not self.isMinimized() and self.isActiveWindow():
                pass
            else:
                self.desktop_notify(uri)

        session.remote_composing = False
        settings = SIPSimpleSettings()
        must_play = not settings.audio.silent and not replicated_message

        if self.last_incoming_message_alert:
            d = datetime.now() - self.last_incoming_message_alert
            if d.total_seconds() < 30:
                must_play = False

        if settings.sounds.play_message_alerts and self.selected_session is session and must_play:
            player = WavePlayer(SIPApplication.alert_audio_bridge.mixer, Resources.get('sounds/message_received.wav'), volume=20)
            SIPApplication.alert_audio_bridge.add(player)
            player.start()
            self.last_incoming_message_alert = datetime.now()

    def desktop_notify(self, from_uri):
        notification_title = 'Blink Qt'
        notification_message = translate('chat_window', f"New message from {from_uri}")
        icon = QIcon(Resources.get('icons/blink.png'))
        if self.last_desktop_notify:
            d = datetime.now() - self.last_desktop_notify
            if d.total_seconds() < 60:
                return

        self.last_desktop_notify = datetime.now()
        if platform.system() == 'Darwin':
            desktop_notification(notification_title, notification_message, '', sound=True)

    def _NH_BlinkGotComposingIndication(self, notification):
        session = notification.sender.items.chat
        if session is None:
            return
        session.update_composing_indication(notification.data)

    def _NH_BlinkGotDispositionNotification(self, notification):
        blink_session = notification.sender
        try:
            session = blink_session.items.chat
        except AttributeError:
            return

        if session is None:
            return
        data = notification.data
        session.chat_widget.update_message_status(id=data.id, status=data.status)

    def _NH_BlinkGotMessageDelete(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat

        if session is None:
            return

        session.chat_widget.remove_message(notification.data)

    def _NH_BlinkMessageDidSucceed(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat
        if session is None:
            return
        session.chat_widget.update_message_status(id=notification.data.id, status='accepted')
        if blink_session.fake_streams.get('messages').can_encrypt:
            session.chat_widget.update_message_encryption(notification.data.id, True)

    def _NH_BlinkMessageDidFail(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat
        if session is None:
            return

        reason = notification.data.reason
        status = 'failed-local' if notification.data.originator == 'local' else 'failed'
        code = None
        if status == 'failed':
            code = notification.data.code

        if code is None:
            code = ''

        log.debug(f'Last failure code: {blink_session.last_failed_reason} new: {(reason, code)}')

        if blink_session.last_failed_reason != (reason, code):
            blink_session.last_failed_reason = (reason, code)
            session.chat_widget.add_message(ChatStatus(translate('chat_window', f'Sending failed: {reason} {code}')))

        call_later(.5, session.chat_widget.update_message_status, id=notification.data.id, status=status)

    def _NH_PGPMessageDidDecrypt(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat
        message = notification.data.message

        if session is None:
            return

        if isinstance(message, BlinkMessage):
            return

        if message in session.chat_widget.pending_decryption:
            session.chat_widget.pending_decryption.remove(message)
            content = message.content
            session.chat_widget.update_message_text(message.message_id, content, message.content_type)
            notification_center = NotificationCenter()
            blink_message = BlinkMessage(content, message.content_type, id=message.message_id, is_secure=True)
            notification_center.post_notification('BlinkMessageDidDecrypt', sender=blink_session, data=NotificationData(message=blink_message))

            account_manager = AccountManager()
            account = account_manager.get_account(message.account_id) if account_manager.has_account(message.account_id) else None
            if message.direction != 'outgoing' and message.state != 'displayed' and 'display' in message.disposition:
                if message.state != 'delivered' and 'positive-delivery' in message.disposition:
                    MessageManager().send_imdn_message(blink_session, message.message_id, message.timestamp, 'delivered', account)

                if self.selected_session is session and not self.isMinimized() and self.isActiveWindow():
                    MessageManager().send_imdn_message(blink_session, message.message_id, message.timestamp, 'displayed', account)
            session.chat_widget.update_message_encryption(message.message_id, True)

    def _NH_BlinkSessionDidShareFile(self, notification):
        if self.selected_session and self.selected_session.blink_session == notification.sender:
             self._AH_ShowTransferredFiles()
        if getattr(notification.data, 'conference', False):
            # a file the room tells of: a bubble in its transcript, as history keeps it (HistoryManager)
            from blink.history import msrp_file_envelope, own_room_upload_id, room_file_direction, session_conversation_key
            blink_session = notification.sender
            chat = getattr(blink_session.items, 'chat', None)
            file = notification.data.file
            if chat is None or blink_session.account is None or chat.chat_widget.has_file(file.hash):
                return
            sender_uri = getattr(file, 'sender_uri', '') or ''
            direction = room_file_direction(blink_session, sender_uri, file.hash)
            file_id = (own_room_upload_id(blink_session, file.hash) if direction == 'outgoing' else None) or file.id
            source = None
            if direction == 'outgoing':
                # our own upload: the file we sent it from, when its transfer is still known
                from blink.sessions import SessionManager
                transfer = next((transfer for transfer in SessionManager().file_transfers if transfer.id == file_id), None)
                selector = getattr(transfer, 'file_selector', None)
                if selector is not None and os.path.isabs(str(selector.name)):
                    source = selector.name
            envelope = msrp_file_envelope(file.name, file.size, None, file_id, file.hash, room=session_conversation_key(blink_session), path=source)
            sender = ChatSender(getattr(file.contact, 'name', '') or sender_uri, sender_uri)
            chat.chat_widget.add_message(ChatFile(envelope, sender, direction, id=file_id, content_type=FILE_TRANSFER_CONTENT_TYPE, account=blink_session.account))

    def _NH_BlinkFileTransferDidEnd(self, notification):
        if type(notification.sender) is BlinkFileTransfer:
            try:
                blink_session = next(session.blink_session for session in self.session_model.sessions if session.blink_session.contact.settings is notification.sender.contact.settings)
            except StopIteration:
                return

            if notification.data.error:
                return

            id = notification.sender.id
            filename = notification.sender.file_selector.name
            self._show_msrp_transfer(blink_session, notification.sender)

        else:
            blink_session = notification.sender
            filename = notification.data.filename
            id = notification.data.id

        if notification.data.must_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(filename))

        chat = getattr(getattr(blink_session, 'items', None), 'chat', None)
        if chat is not None:
            chat.chat_widget.file_ready(id)      # its bubble shows the file now

    def _NH_BlinkSessionDidSaveCallRecording(self, notification):
        """A call (a conference's audio) recorded on this device: an audio bubble of ours in the
        transcript of its conversation, to listen to later. It was filed in history as an outgoing
        file that was never sent (MessageHistory.add_call_recording), where reloading finds it too."""
        from blink.history import MessageHistory, session_conversation_key
        data = notification.data
        account = data.account
        try:
            chat = next(item for item in self.session_model.sessions
                        if item.blink_session is not None and item.blink_session.account is account
                        and (item.blink_session is notification.sender or session_conversation_key(item.blink_session) == data.key))
        except StopIteration:
            return
        try:
            envelope = MessageHistory.own_file_envelope(data.path, data.transfer_id, account, 'audio/wav', {'call_recording': True})
        except OSError:
            return
        timestamp = data.timestamp.replace(tzinfo=timezone.utc) if data.timestamp.tzinfo is None else data.timestamp
        sender = ChatSender(account.display_name, str(account.id))
        chat.chat_widget.add_message(ChatFile(envelope, sender, 'outgoing', id=data.transfer_id, timestamp=timestamp, account=account, content_type=FILE_TRANSFER_CONTENT_TYPE))
        ActivityLog().info(f'[chat window] Call recording {data.transfer_id} shown in the conversation with {data.key}')

    def _show_msrp_transfer(self, blink_session, transfer):
        """A file sent or received over MSRP: a bubble in the session's transcript, as history keeps it
        (HistoryManager._store_msrp_transfer). A pull fetched one the transcript already has."""
        chat = getattr(blink_session.items, 'chat', None)
        if chat is None or transfer.route != 'msrp' or transfer.transfer_type != 'push' or transfer.file_selector is None:
            return
        if chat.chat_widget.model.get(transfer.id) is not None:
            return
        existing = chat.chat_widget.file_item(transfer.file_selector.hash)
        if existing is not None and not (transfer.direction == 'outgoing' and not existing.outgoing):
            return      # already shown, on the right side
        from blink.history import msrp_file_envelope, session_conversation_key
        from blink.sessions import is_conference_uri
        key = session_conversation_key(transfer)
        conference = is_conference_uri(str(transfer.contact_uri.uri), transfer.account)
        selector = transfer.file_selector
        try:
            size = os.path.getsize(selector.name)
        except OSError:
            size = selector.size
        source = selector.name if transfer.direction == 'outgoing' and os.path.isabs(str(selector.name)) else None
        envelope = msrp_file_envelope(selector.name, size, getattr(selector, 'type', None), transfer.id, selector.hash, room=key if conference else None, path=source)
        if transfer.direction == 'outgoing':
            sender = ChatSender(transfer.account.display_name, str(transfer.account.id))
        else:
            sender = ChatSender(getattr(transfer.contact, 'name', '') or str(transfer.contact_uri.uri), str(transfer.contact_uri.uri).replace('sip:', ''))
        message = ChatFile(envelope, sender, transfer.direction, id=transfer.id, content_type=FILE_TRANSFER_CONTENT_TYPE, account=transfer.account)
        if existing is not None:
            chat.chat_widget.replace_message(existing.id, message)     # the room's bubble of our own upload, shown as another's
        else:
            chat.chat_widget.add_message(message)

    def _NH_PGPFileDidNotDecrypt(self, notification):
        transfer_session = notification.sender

        blink_session = next(session.blink_session for session in self.session_model.sessions if session.blink_session.contact.settings is transfer_session.contact.settings)

        if blink_session is None:
            return

        session = blink_session.items.chat
        if session is None:
            return
        session.chat_widget.replace_message(transfer_session.id, ChatStatus(translate('chat_window', f'File decryption failed: {notification.data.error}')))

    def _NH_MessageStreamPGPKeysDidLoad(self, notification):
        stream = notification.sender
        blink_session = stream.blink_session
        try:
            session = blink_session.items.chat
        except AttributeError:
            return

        if session is None:
            return

        stream = blink_session.fake_streams.get('messages')
        for message in session.chat_widget.pending_decryption:
            if isinstance(message, BlinkMessage):
                continue
            if stream and (stream.can_decrypt or stream.can_decrypt_with_others):
                stream.decrypt(message)

    def _NH_BlinkMessageHistoryLoadDidSucceed(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat

        messages = notification.data.messages
        account_manager = AccountManager()

        if session is None:
            return

        kind = getattr(notification.data, 'kind', None) or 'initial'
        if kind in ('jump', 'now'):
            session.chat_widget.clear()       # a page of its own: what was shown goes

        last_account = None
        last_timestamp = None
        newest_timestamp = None
        # an older page goes in newest first, each row on top: the view keeps what is on screen in place
        for message in (reversed(messages) if kind == 'older' else messages):
            encrypted = False
            state = message.state
            account = account_manager.get_account(message.account_id) if account_manager.has_account(message.account_id) else None

            # We don't load messages if the account is not present or not enabled.
            if account is None or not account.enabled:
               continue

            last_account = account

            content_type = message.content_type
            is_file = False
            if message.content_type.startswith('image/'):
                content = self._image_content(message.message_id, message.content_type, message.content)
                if content is None:
                    continue
                content_type, is_file = FILE_TRANSFER_CONTENT_TYPE, True
            elif message.content_type.startswith('text/'):
                content = message.content
                if MessageManager().check_encryption(message.content_type, message.content) == 'OpenPGP':
                    encrypted = True        # drawn as an encrypted message until it is decrypted
                    if message.decrypted != '2':
                        session.chat_widget.pending_decryption.append((message))
                        stream = blink_session.fake_streams.get('messages')
                        if stream and (stream.can_decrypt or stream.can_decrypt_with_others):
                            stream.decrypt(message)
            elif message.content_type.lower() == FTHTTPDocument.content_type:
                try:
                    content = self._parse_fthttp(blink_session, message, from_history=True)
                except ParserError as e:
                    continue
                if not content:
                    continue
                is_file = True
            elif message.content_type.lower() == FILE_TRANSFER_CONTENT_TYPE and file_transfer_envelope(message.content) is not None:
                content = message.content
                is_file = True
            else:
                continue        # calls among them: the MSRP window shows chat only, calls are in the message pane

            # message.sender = SIPURI.parse(f'sip:{message.remote_uri}')
            if message.direction == 'outgoing':
                uri = message.account_id
            elif blink_session.is_conference and message.uri:
                uri = re.sub('^sips?:', '', str(message.uri))       # the participant who wrote it (CPIM From), not the room
            else:
                uri = message.remote_uri

            timestamp = message.timestamp.replace(tzinfo=timezone.utc).astimezone(tzlocal())
            if last_timestamp and timestamp > last_timestamp:
                newest_timestamp = timestamp
            last_timestamp = timestamp

            direction = message.direction
            if direction == 'incoming' and blink_session.is_conference:
                from blink.history import bare_address, is_own_address
                if is_own_address(uri, blink_session.account):
                    direction = 'outgoing'      # ours, played back by the room (or a file of ours it told of, stored as another's)
                    uri = bare_address(uri)
            if account_manager.has_account(uri):
                found_account = account_manager.get_account(uri)
                sender = ChatSender(message.display_name or found_account.display_name, uri)
            else:
                sender = ChatSender(message.display_name or session.name, uri)
            if is_file:
                chat_message = ChatFile(content, sender, direction, id=message.message_id, timestamp=timestamp, history=True, account=account, content_type=content_type)
            else:
                chat_message = ChatMessage(content, sender, direction, id=message.message_id, timestamp=timestamp, history=True, account=account, content_type=content_type)

            session.chat_widget.add_message(chat_message)

            if message.direction == "outgoing":
                session.chat_widget.update_message_status(id=message.message_id, status=message.state)
            elif message.state != 'displayed':
                if 'display' in message.disposition:
                    if not encrypted:
                        if account_manager.has_account(message.account_id):
                            account = account_manager.get_account(message.account_id)

                        if message.state != 'delivered' and 'positive-delivery' in message.disposition:
                            MessageManager().send_imdn_message(blink_session, message.message_id, message.timestamp, 'delivered', account)

                        if self.selected_session is session and not self.isMinimized() and self.isActiveWindow():
                            MessageManager().send_imdn_message(blink_session, message.message_id, message.timestamp, 'displayed', account)
                        else:
                            self.pending_displayed_notifications.setdefault(blink_session, []).append((message.message_id, message.timestamp, account))
                else:
                    HistoryManager().message_history.update(message.message_id, 'displayed')

            if 'OpenPGP' in message.encryption_type:
                session.chat_widget.update_message_encryption(message.message_id, True)
            elif 'OTR' in message.encryption_type:
                session.chat_widget.update_message_encryption(message.message_id, True)
        session.chat_widget.history_loaded = True
        if kind != 'reload':
            session.chat_widget.history_page_loaded(kind, messages, notification.data)
        if kind in ('older', 'newer', 'jump'):
            return
        if kind == 'now':
            session.chat_widget.scroll_to_bottom()
            return

        # the messages that arrived for this session while its history loaded; the others stay queued
        # (going once over what is queued: putting them back while emptying the queue never ends)
        for _ in range(len(self.render_after_load)):
            (found_session, received_account, message) = self.render_after_load.popleft()
            if found_session is not session:
                self.render_after_load.append((found_session, received_account, message))
                continue
            if received_account is not None and received_account != last_account and received_account != session.blink_session.account:
                last_account = received_account
            session.chat_widget.add_message(message)

        if blink_session.direction == 'outgoing' and last_account is not None and last_account.enabled and last_account != blink_session.account and not blink_session.remote_instance_id:
            blink_session.account = last_account
            NotificationCenter().post_notification('BlinkSessionMessageAccountChanged', sender=blink_session)
            NotificationCenter().post_notification('PGPKeysShouldReload', sender=blink_session)

        blink_session.updateTimestamp(newest_timestamp)

        NotificationCenter().post_notification('BlinkSessionWasLoaded', sender=blink_session)
        session.chat_widget.scroll_to_bottom()
        session.chat_widget.show_loading_screen(False)

    def _NH_BlinkMessageHistoryLoadDidFail(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat
        # TODO Should we attempt to reload history if it fails? -- Tijmen
        session.chat_widget.history_loaded = True
        session.chat_widget.model.set_loading(False)
        session.chat_widget.show_loading_screen(False)

    def _NH_BlinkMessageHistoryLastContactsDidSucceed(self, notification):
        contacts = notification.data.contacts
        message_manager = MessageManager()
        for display_name, contact, timestamp in contacts[::-1]:
            # allow the user to select the 1st session
            message_manager.create_message_session(contact, display_name, selected=False)

    def _NH_BlinkMessageHistoryCallHistoryDidStore(self, notification):
        """A call record is not shown here: the MSRP window shows chat only, calls are in the message pane."""

    def _NH_BlinkConversationWillRemove(self, notification):
        session = notification.sender.items.chat
        if session.chat_widget.history_loaded:
            session.chat_widget.history_loaded = False
            session.chat_widget.show_loading_screen(True)
            session.chat_widget.clear()

    def _NH_ChatStreamGotMessage(self, notification):
        blink_session = notification.sender.blink_session
        session = blink_session.items.chat

        if session is None:
            return

        message = notification.data.message

        message_id = msrp_message_id(message)       # the id history keeps it by
        content_type = message.content_type
        is_file = False
        if message.content_type.startswith('image/'):
            # an MSRP image arrives as raw bytes; text was already base64 encoded by the sender
            content = self._image_content(message_id, message.content_type, message.content)
            if content is None:
                return
            content_type, is_file = FILE_TRANSFER_CONTENT_TYPE, True
        elif message.content_type.startswith('text/'):
            content = message.content
        else:
            return

        try:
            uri = '%s@%s' % (message.sender.uri.user.decode(), message.sender.uri.host.decode())
        except AttributeError:
            uri = '%s@%s' % (message.sender.uri.user, message.sender.uri.host)

        account_manager = AccountManager()
        account = None
        if account_manager.has_account(uri):
            account = account_manager.get_account(uri)
            sender = ChatSender(message.sender.display_name or account.display_name, uri)
        elif blink_session.is_conference:
            contact, contact_uri = URIUtils.find_contact(uri)
            sender = ChatSender(message.sender.display_name or contact.name, uri)
        else:
            sender = ChatSender(message.sender.display_name or session.name, uri)

        is_status_message = any(h.name == 'Message-Type' and h.value == 'status' and h.namespace == 'urn:ag-projects:xml:ns:cpim' for h in message.additional_headers)
        private = bool(getattr(notification.data, 'private', False))
        if is_status_message:
            if content_type == 'text/html':
                document = QTextDocument()
                document.setHtml(content)
                content = document.toPlainText()
            session.chat_widget.add_message(ChatStatus(content))
        else:
            # one of our own the room plays back (another device of ours, the backlog on joining) is ours
            direction = 'outgoing' if account is not None and account is blink_session.account else 'incoming'
            message_class = ChatFile if is_file else ChatMessage
            recipient = None
            if private and direction == 'outgoing' and message.recipients:
                # one of our private messages played back by the room: whom it went to
                to = message.recipients[0]
                try:
                    to_uri = '%s@%s' % (to.uri.user.decode(), to.uri.host.decode())
                except AttributeError:
                    to_uri = '%s@%s' % (to.uri.user, to.uri.host)
                recipient = ChatSender(to.display_name or URIUtils.find_contact(to_uri)[0].name, to_uri)
            session.chat_widget.add_message(message_class(content, sender, direction, id=message_id, timestamp=message.timestamp, content_type=content_type, private=private, recipient=recipient))
            if direction == 'incoming' and self.selected_session is session and not self.isMinimized() and self.isActiveWindow():
                # seen as it arrives: read in history too (unread counts come from history otherwise)
                NotificationCenter().post_notification('BlinkSessionConfirmReadMessages', sender=blink_session)
            elif direction == 'incoming':
                # not looked at: a desktop notification with who wrote what
                text = content if not is_file else translate('chat_window', 'sent a file')
                if not is_file and content_type == 'text/html':
                    document = QTextDocument()
                    document.setHtml(content)
                    text = document.toPlainText()
                if private:
                    text = translate('chat_window', '(private) %s') % text
                notifier = getattr(QApplication.instance().main_window, 'message_notifier', None)
                if notifier is not None:
                    notifier.notify_chat(blink_session, sender.name or sender.uri, text, lambda session=session: self._open_from_notification(session))

        session.remote_composing = False
        settings = SIPSimpleSettings()
        if settings.sounds.play_message_alerts and self.selected_session is session and not settings.audio.silent:
            player = WavePlayer(SIPApplication.alert_audio_bridge.mixer, Resources.get('sounds/message_received.wav'), volume=20)
            SIPApplication.alert_audio_bridge.add(player)
            player.start()

    def _NH_ChatStreamGotComposingIndication(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.update_composing_indication(notification.data)

    def _NH_ChatStreamDidSendMessage(self, notification):
        if notification.data.message.message_id in self.radio_ptt_commands:
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.update_message_status(id=notification.data.message.message_id, status='accepted')
        # TODO: do we want to use this? Play the message sent tone? -Saul

    def _NH_ChatUnreadMessagesCountChanged(self, notification):
        total_count = 0
        for session in self.session_model.sessions:
            total_count = total_count + session.blink_session.unread_messages

        if total_count == 0:
            label = translate('chat_window', 'No new messages')
        elif total_count == 1:
            label = translate('chat_window', 'There is 1 new message')
        else:
            label = translate('chat_window', 'There are %d new messages') % total_count

        self.new_messages_button.setText(label)

        if total_count:
            self.new_messages_button.show()
        else:
            self.new_messages_button.hide()

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        if self._radio_ptt_report(notification.data.message.message_id, 200, 'OK'):
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.update_message_status(id=notification.data.message.message_id, status='delivered')
        # TODO: implement -Saul

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        if self._radio_ptt_report(notification.data.message_id, getattr(notification.data, 'code', None), getattr(notification.data, 'reason', None)):
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.update_message_status(id=notification.data.message_id, status='failed')
        # TODO: implement -Saul

    def _NH_ChatStreamOTREncryptionStateChanged(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        if notification.data.new_state is OTRState.Encrypted:
            session.chat_widget.stop_otr_timer()
            session.chat_widget.add_message(ChatStatus(translate('chat_window','Encryption enabled')))
        elif notification.data.old_state is OTRState.Encrypted:
            session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Encryption disabled')))
            self.otr_widget.hide()
        if notification.data.new_state is OTRState.Finished:
            session.chat_widget.chat_input.lock(EncryptionLock)
            # todo: play sound here?
        call_later(.1, self._update_control_menu)

    def _NH_ChatStreamOTRError(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is not None:
            message = "OTR Error: {.error}".format(notification.data)
            session.chat_widget.add_message(ChatStatus(message))

    def _NH_ChatStreamOTRTimeout(self, notification):
        self._update_control_menu()

    def _NH_MediaStreamDidInitialize(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        # session.chat_widget.add_message(ChatStatus('Connecting...'))  # disable it until we can replace it in the DOM -Dan

    def _NH_MediaStreamDidNotInitialize(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Failed to initialize chat: %s') % notification.data.reason))
        log.info('Failed to initialize chat session: %s' % notification.data.reason)

    def _NH_MediaStreamDidFail(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Media failed: %s') % notification.data.reason))
        log.info('Chat media failed: %s' % notification.data.reason)

    def _NH_MediaStreamDidStart(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        if str(notification.sender.blink_session.state or '').startswith('connected'):
            session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Chat started')))     # added to a session already connected; else "Connected" says it
        if getattr(session, 'chat_widget', None) is not None:
            session.chat_widget.update_input_state()    # the stream may start after the session said it connected
        if session is self.selected_session:
            self._update_radio_ptt()

    def _NH_MediaStreamDidEnd(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        if session is self.selected_session:
            self._update_radio_ptt()
        if getattr(session, 'chat_widget', None) is not None:
            session.chat_widget.update_input_state()

        if notification.data.error is not None:
            session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Disconnected: %s') % notification.data.error))
        else:
            #session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Disconnected')))
            # Set type back for message as the stream ended cleanly -- Tijmen
            notification.sender.blink_session.chat_type = None

    def _NH_MediaStreamWillEnd(self, notification):
        stream = notification.sender
        if stream.type == 'chat' and stream.blink_session.items.chat is self.selected_session:
            self.otr_widget.hide()
        if stream.type == self.zrtp_widget.stream_type and stream.blink_session.items.chat is self.selected_session:
            self.zrtp_widget.hide()
            self.zrtp_widget.stream_type = None

    # Radio push to talk (docs/radio-ptt.md of SylkServer)
    #
    @property
    def radio_ptt_button(self):
        """Push to talk of the selected session, in the top bar of its chat."""
        widget = getattr(self.selected_session, 'chat_widget', None)
        return widget.radio_ptt_button if widget is not None else self._no_radio_ptt_button

    def _session_radios(self, session):
        """The radios of the conference of a session: [(participant, radio)], radios of participants
        with audio only; empty when the session is not to a focus."""
        blink_session = getattr(session, 'blink_session', None)
        if blink_session is None or not blink_session.remote_focus:
            return []
        server_conference = getattr(blink_session, 'server_conference', None)
        if server_conference is None:
            return []
        return [(participant, radio) for participant in server_conference.participants.values() for radio in participant.radios]

    @staticmethod
    def _chat_connected(session):
        blink_session = session.blink_session
        stream = session.chat_stream
        return stream is not None and str(blink_session.state or '').startswith('connected')

    def _update_radio_ptt(self):
        """The push to talk button of the selected session, and the pulse, from the last conference-info:
        the button is there only for a room where someone can key a radio, says what a click will do and
        flashes while a radio is on air. Nothing changes on a click until the room reports it."""
        session = self.selected_session
        radios = [radio for participant, radio in self._session_radios(session)] if session is not None else []
        if not radios:
            self.radio_ptt_button.hide()
            self._stop_radio_pulse()
            return
        keyed = any(radio.keyed for radio in radios)
        transmitting = any(radio.state == 'transmitting' for radio in radios)
        if keyed:
            self.radio_ptt_button.setText(translate('chat_window', 'Push to Stop'))
            self.radio_ptt_button.setToolTip(translate('chat_window', 'Release the radio'))
        else:
            self.radio_ptt_button.setText(translate('chat_window', 'Push to Talk'))
            self.radio_ptt_button.setToolTip(translate('chat_window', 'Key the radio of conference participants'))
        chat = self._chat_connected(session)
        self.radio_ptt_button.setEnabled(chat)
        if not chat:
            self.radio_ptt_button.setToolTip(translate('chat_window', 'The radio is keyed through the MSRP chat with the room: add chat from the wheel'))
        self.radio_ptt_button.show()
        if transmitting:
            if not self.radio_pulse_timer.isActive():
                radio_ptt.pulse.on = True
                self.radio_pulse_timer.start()
        else:
            self._stop_radio_pulse()
        self.radio_ptt_button.setIcon(radio_ptt.radio_icon(22, transmitting and radio_ptt.pulse.on, self.devicePixelRatioF()))

    def _stop_radio_pulse(self):
        was_active = self.radio_pulse_timer.isActive()
        self.radio_pulse_timer.stop()
        if was_active or radio_ptt.pulse.on:
            radio_ptt.pulse.on = False      # the plain picture: 'on' is the glowing phase
            self._repaint_radio_participants()
            if self.radio_ptt_button.isVisible():
                self.radio_ptt_button.setIcon(radio_ptt.radio_icon(22, False, self.devicePixelRatioF()))

    def _repaint_radio_participants(self):
        model = self.participants_list.model()
        for item in getattr(model, 'participants', ()):
            if item.radio_state is not None:
                item.widget.update_content(item)
        self.participants_list.viewport().update()

    def _SH_RadioPulseTimerTimeout(self):
        radio_ptt.pulse.on = not radio_ptt.pulse.on
        session = self.selected_session
        if session is None or not any(radio.state == 'transmitting' for participant, radio in self._session_radios(session)):
            self._stop_radio_pulse()
            return
        self.radio_ptt_button.setIcon(radio_ptt.radio_icon(22, radio_ptt.pulse.on, self.devicePixelRatioF()))
        self._repaint_radio_participants()

    def _SH_RadioPTTButtonClicked(self):
        """A toggle: with no radio keyed, press every radio of the room; with any keyed, release those."""
        session = self.selected_session
        if session is None or not self._chat_connected(session):
            return
        radios = self._session_radios(session)
        keyed = [(participant, radio) for participant, radio in radios if radio.keyed]
        action, targets = ('release', keyed) if keyed else ('press', radios)
        stream = session.chat_stream
        for participant, radio in targets:
            command_id, body = radio_ptt.command(action, radio.entity)
            try:
                message_id = send_msrp_message(stream, body, radio_ptt.CONTENT_TYPE)     # to the room itself, not saved in history
            except Exception as e:
                ActivityLog().error(f'[conference] Radio PTT {action} id={command_id} of {participant.uri} ({radio.entity}) in {session.blink_session.uri} not sent: {e}')
                continue
            self.radio_ptt_commands[message_id] = (action, command_id, participant.uri, radio.entity)
            ActivityLog().info(f'[conference] Radio PTT {action} id={command_id} of {participant.uri} ({radio.entity}) in {session.blink_session.uri}')

    def _radio_ptt_report(self, message_id, code, reason):
        """The room's MSRP report on a command of ours: logged (the state comes with conference-info).
        True when the message was one."""
        try:
            action, command_id, uri, entity = self.radio_ptt_commands.pop(message_id)
        except KeyError:
            return False
        if code == 200:
            ActivityLog().info(f'[conference] Radio PTT {action} id={command_id} of {uri} accepted by the room')
        else:
            ActivityLog().warning(f'[conference] Radio PTT {action} id={command_id} of {uri} ({entity}) refused by the room: {code} {reason or ""}'.rstrip())
        return True

    # signal handlers
    #
    def _SH_InfoButtonClicked(self, checked):
        if not self.selected_session:
            return

        # the info panel here, also for a conference and with audio (the participants or files button goes back)
        self.selected_session.info_requested = True
        self._update_panel_buttons()
        self._update_session_info_panel(elements={'session', 'media', 'statistics', 'status'}, update_visibility=True)
        if self.sliding_panels:
            self._slide_details(self.info_panel, direction=self.slide_direction)
        else:
            self._set_details(self.info_panel)
        self.selected_session.active_panel = self.info_panel


    def _SH_FilesButtonClicked(self, checked):
        if self.selected_session is None:
            return
        self.selected_session.info_requested = False
        if self.sliding_panels:
            self._slide_details(self.files_panel, direction=self.slide_direction)
        else:
            self._set_details(self.files_panel)
        self.selected_session.active_panel = self.files_panel

    def _SH_ParticipantsButtonClicked(self, checked):
        if self.selected_session is None:
            return
        self.selected_session.info_requested = False
        if self.sliding_panels:
            self._slide_details(self.participants_panel, direction=self.slide_direction)
        else:
            self._set_details(self.participants_panel)
        self.selected_session.active_panel = self.participants_panel

    def _SH_LatencyGraphUpdated(self):
        self.latency_label.setText(translate('chat_window', 'Network Latency: %dms, max=%dms') % (max(self.audio_latency_graph.last_value, self.video_latency_graph.last_value), self.latency_graph.max_value))

    def _SH_PacketLossGraphUpdated(self):
        self.packet_loss_label.setText(translate('chat_window', 'Packet Loss: %.1f%%, max=%.1f%%') % (max(self.audio_packet_loss_graph.last_value, self.video_packet_loss_graph.last_value), self.packet_loss_graph.max_value))

    def _SH_TrafficGraphUpdated(self):
        blink_settings = BlinkSettings()
        if blink_settings.chat_window.session_info.bytes_per_second:
            incoming_traffic = TrafficNormalizer.normalize(self.incoming_traffic_graph.last_value)
            outgoing_traffic = TrafficNormalizer.normalize(self.outgoing_traffic_graph.last_value)
        else:
            incoming_traffic = TrafficNormalizer.normalize(self.incoming_traffic_graph.last_value * 8, bits_per_second=True)
            outgoing_traffic = TrafficNormalizer.normalize(self.outgoing_traffic_graph.last_value * 8, bits_per_second=True)
        self.traffic_label.setText(translate('chat_window', """<p>Traffic: <span style="font-family: sans-serif; color: #d70000;">%s</span> %s <span style="font-family: sans-serif; color: #0064d7;">%s</span> %s</p>""") % ("\u2193", incoming_traffic, "\u2191", outgoing_traffic))

    def _SH_MuteButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.audio.muted = checked
        settings.save()

    def _SH_HoldButtonClicked(self, checked):
        if checked:
            self.selected_session.blink_session.hold()
        else:
            self.selected_session.blink_session.unhold()

    def _SH_RecordButtonClicked(self, checked):
        if checked:
            self.selected_session.blink_session.start_recording()
        else:
            self.selected_session.blink_session.stop_recording()

    def _SH_ControlButtonClicked(self):
        # this is only called if the control button doesn't have a menu attached
        if self.selected_session.blink_session.state == 'connected/sent_proposal':
            self.selected_session.blink_session.sip_session.cancel_proposal()
        else:
            self.selected_session.end()

    def _SH_SessionModelSessionAdded(self, session):
        model = self.session_model
        position = model.sessions.index(session)
        session.chat_widget = ChatWidget(session, self.tab_widget)
        session.chat_widget.radio_ptt_button.clicked.connect(self._SH_RadioPTTButtonClicked)
        session.active_panel = self.info_panel
        self.tab_widget.insertTab(position, session.chat_widget, session.name)
        self.no_sessions_label.hide()
        selection_model = self.session_list.selectionModel()
        # allow user select the contact
        if 'chat' in session.blink_session.streams or session.blink_session.is_conference:
            # a room opened from the contact list is selected, connected or not
            selection_model.select(model.index(position), selection_model.SelectionFlag.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.ScrollHint.EnsureVisible)  # or PositionAtCenter
        self.session_list.animation.setStartValue(self.session_widget.geometry())
        self.session_list.show()
        session.chat_widget.chat_input.setFocus(Qt.FocusReason.OtherFocusReason)
        history = HistoryManager()
        history.load(session.blink_session.contact.uri.uri, session.blink_session)
        self._hide_message_only_sessions()

    def _NH_BlinkMessageHistoryMustReload(self, notification):
        history = HistoryManager()
        for session in self.session_model.sessions:
            # print(f'Check if we must reload {session.blink_session}')
            if session.blink_session.account.id == notification.data.account:
                history.reload_pending_encrypted(session.blink_session.contact.uri.uri, session.blink_session)

    def _SH_SessionModelSessionRemoved(self, session):
        self.tab_widget.removeTab(self.tab_widget.indexOf(session.chat_widget))
        session.chat_widget = None
        session.active_panel = None
        if not self.session_model.sessions:
            self.close()
            self.no_sessions_label.show()
        elif not self.has_visible_sessions():
            self.hide()     # what is left are message conversations, shown in the message pane
            self.no_sessions_label.show()
        elif not self.session_list.isVisibleTo(self):
            if self.session_list.animation:
                self.session_list.animation.setDirection(QPropertyAnimation.Direction.Forward)
                self.session_list.animation.setStartValue(self.session_widget.geometry())
                self.session_list.animation.setEndValue(self.session_panel.rect())
            self.session_list.show()
            self.session_list.animation.start()

    def _SH_SessionModelSessionAboutToBeRemoved(self, session):
        # choose another one to select (a chat only or ended session if available, else one with audio but keep audio on hold? or select nothing and display the dummy tab?)
        # selection_model = self.session_list.selectionModel()
        # selection_model.clearSelection()
        pass

    def _SH_SessionListSelectionChanged(self, selected, deselected):
        #print("-- chat selection changed %s -> %s" % ([x.row() for x in deselected.indexes()], [x.row() for x in selected.indexes()]))
        previous = self.selected_session
        self.selected_session = selected[0].topLeft().data(Qt.ItemDataRole.UserRole) if selected else None
        if self.selected_session is not None:
            self.tab_widget.setCurrentWidget(self.selected_session.chat_widget)  # why do we switch the tab here, but do everything else in the selected_session property setter? -Dan
            self._set_details(self.selected_session.active_panel)
            self.participants_list.setModel(self.selected_session.participants_model)
            self.files_list.setModel(self.selected_session.files_model)
            self.control_button.setEnabled(True)
            if not self.isMinimized():
                self.confirm_read_messages(self.selected_session)
            if self.isActiveWindow() and not self.isMinimized():
                self._withdraw_notification(self.selected_session)
        else:
            if previous is not None and self.session_model.sessions:
                # only when a selection was lost: at start sessions are added one by one with none
                # selected yet, and each addition comes here with nothing to report
                ActivityLog().info(f'[ui] Chat window: no session selected ({len(self.session_model.sessions)} sessions, {sum(1 for item in self.session_model.sessions if self._shows_session(item.blink_session))} shown here)')
            if previous is not None and previous in self.session_model.sessions and self._shows_session(previous.blink_session):
                # the selection was lost while the session is still here (a message only conversation
                # being added, a row hidden): select it again rather than leave the window blank
                QTimer.singleShot(0, lambda: self._reselect_session(previous))
            self.tab_widget.setCurrentWidget(self.dummy_tab)
            self._set_details(self.info_panel)
            self.participants_list.setModel(None)
            self.control_button.setEnabled(False)
            self._update_window_title()
            self._update_radio_ptt()

    def _reselect_session(self, session):
        if self.selected_session is not None or session not in self.session_model.sessions:
            return
        model = self.session_model
        index = model.index(model.sessions.index(session))
        self.session_list.selectionModel().select(index, self.session_list.selectionModel().SelectionFlag.ClearAndSelect)
        ActivityLog().info(f'[ui] Chat window: selected {session.blink_session.uri} again')

    def _SH_OTRWidgetNameChanged(self):
        stream = self.selected_session.chat_stream or self.selected_session.messages_stream or Null
        stream.encryption.peer_name = self.otr_widget.peer_name

    def _SH_OTRWidgetStatusChanged(self):
        stream = self.selected_session.chat_stream or self.selected_session.messages_stream or Null
        stream.encryption.verified = self.otr_widget.peer_verified

    def _SH_ZRTPWidgetNameChanged(self):
        stream = self.selected_session.blink_session.streams.get(self.zrtp_widget.stream_type, Null)
        stream.encryption.zrtp.peer_name = self.zrtp_widget.peer_name

    def _SH_ZRTPWidgetStatusChanged(self):
        stream = self.selected_session.blink_session.streams.get(self.zrtp_widget.stream_type, Null)
        stream.encryption.zrtp.verified = self.zrtp_widget.peer_verified

    @staticmethod
    def _log_action(action, blink_session):
        uri = blink_session.contact_uri.uri if blink_session.contact_uri is not None else '?'
        account = getattr(blink_session.account, 'id', '?')
        ActivityLog().info(f'[chat window] {action} {uri} (account {account}, state {blink_session.state or "new"})')

    def _AH_Connect(self):
        blink_session = self.selected_session.blink_session
        self._log_action('Connect with chat:', blink_session)
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=[StreamDescription('chat')], reinitialize=True)
        blink_session.connect()

    def _AH_ShowTransferredFiles(self):
        if not self.selected_session:
            return
        blink_session = self.selected_session.blink_session
        self.session_list.hide()
        self._SH_FilesButtonClicked(True)

    def _AH_RemoveConversation(self):
        if not self.selected_session:
            return
        blink_session = self.selected_session.blink_session
        title = translate('chat_window', "Remove conversation")
        message = translate('chat_window', "Do you want to remove all messages exchanged with %s on all devices?. This cannot be undone.") % blink_session.contact.name
        if QMessageBox.warning(self, title, message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            MessageManager().remove_conversation(blink_session)
            log.info('Remove conversation from account %s with %s' % (blink_session.account.id, blink_session.contact_uri))

    def _SH_ConnectAudioButtonClicked(self):
        # audio with the chat: this window shows sessions that have chat (audio alone lives in the audio drawer)
        if self.selected_session is None:
            return
        blink_session = self.selected_session.blink_session
        self._log_action('Connect with audio and chat:', blink_session)
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri,
                                    stream_descriptions=[StreamDescription('audio'), StreamDescription('chat')], reinitialize=True)
        blink_session.connect()

    def _AH_ConnectWithAudio(self):
        stream_descriptions = [StreamDescription('audio'), StreamDescription('chat')]     # audio here always comes with MSRP chat
        blink_session = self.selected_session.blink_session
        self._log_action('Connect with audio and chat:', blink_session)
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=stream_descriptions, reinitialize=True)
        blink_session.connect()

    def _AH_MarkMessagesRead(self):
        self.mark_read()

    def mark_read(self):
        if not self.selected_session:
            return

        blink_session = self.selected_session.blink_session
        uri = str(blink_session.uri).partition(':')[2]
        HistoryManager().message_history.update_displayed_for_uri(uri)

    def _AH_ConnectWithVideo(self):
        stream_descriptions = [StreamDescription('audio'), StreamDescription('video'), StreamDescription('chat')]
        blink_session = self.selected_session.blink_session
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=stream_descriptions, reinitialize=True)
        blink_session.connect()

    def _AH_Disconnect(self):
        self._log_action('Hang up', self.selected_session.blink_session)
        self.selected_session.end()

    def _AH_AddAudio(self):
        blink_session = self.selected_session.blink_session
        if 'chat' in blink_session.streams:
            blink_session.add_stream(StreamDescription('audio'))
        else:
            blink_session.add_streams([StreamDescription('audio'), StreamDescription('chat')])     # audio here always comes with MSRP chat

    def _AH_RemoveAudio(self):
        self.selected_session.blink_session.remove_stream(self.selected_session.blink_session.streams.get('audio'))

    def _AH_AddVideo(self):
        if 'audio' in self.selected_session.blink_session.streams:
            self.selected_session.blink_session.add_stream(StreamDescription('video'))
        else:
            self.selected_session.blink_session.add_streams([StreamDescription('video'), StreamDescription('audio')])

    def _AH_RemoveVideo(self):
        self.selected_session.blink_session.remove_stream(self.selected_session.blink_session.streams.get('video'))

    def _AH_AddChat(self):
        self.selected_session.blink_session.add_stream(StreamDescription('chat'))

    def _AH_RemoveChat(self):
        self.selected_session.blink_session.remove_stream(self.selected_session.blink_session.streams.get('chat'))

    def _AH_RequestScreen(self):
        if 'audio' in self.selected_session.blink_session.streams:
            self.selected_session.blink_session.add_stream(StreamDescription('screen-sharing', mode='viewer'))
        else:
            self.selected_session.blink_session.add_streams([StreamDescription('screen-sharing', mode='viewer'), StreamDescription('audio')])

    def _AH_ShareMyScreen(self):
        if 'audio' in self.selected_session.blink_session.streams:
            self.selected_session.blink_session.add_stream(StreamDescription('screen-sharing', mode='server'))
        else:
            self.selected_session.blink_session.add_streams([StreamDescription('screen-sharing', mode='server'), StreamDescription('audio')])

    def _AH_EndScreenSharing(self):
        self.selected_session.blink_session.remove_stream(self.selected_session.blink_session.streams.get('screen-sharing'))

    def _AH_SendFiles(self, uri=None):
        session_manager = SessionManager()
        if not conference_takes_files(self.selected_session.blink_session):
            self.selected_session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Join the conference to share files: the room exists only while someone is in it')))
            return
        contact = self.selected_session.blink_session.contact
        selected_uri = uri or contact.uri
        for filename in QFileDialog.getOpenFileNames(self, translate('chat_window', 'Select File(s)'), session_manager.send_file_directory, 'Any file (*.*)')[0]:
            session_manager.send_file(contact, selected_uri, filename, account=self.selected_session.blink_session.account, route='msrp')  # MSRP only here

    def _AH_EnableOTR(self, action):
        self.selected_session.chat_widget.start_otr_timer()
        self.selected_session.messages_stream.enable_otr()
        self._update_control_menu()

    def _AH_DisableOTR(self):
        self.selected_session.chat_widget.stop_otr_timer()
        self.selected_session.messages_stream.disable_otr()

    def _AH_MainWindow(self):
        blink = QApplication.instance()
        blink.main_window.show()

    def _EH_CloseSession(self):
        if self.selected_session is not None:
            self._log_action('Close the tile of', self.selected_session.blink_session)
            self.selected_session.end(delete=True)

    def _EH_ShowSessions(self):
        self.session_list.animation.setDirection(QPropertyAnimation.Direction.Forward)
        self.session_list.animation.setStartValue(self.session_widget.geometry())
        self.session_list.animation.setEndValue(self.session_panel.rect())
        self.session_list.scrollToTop()
        self.session_list.show()
        self.session_list.raise_()
        self.session_list.animation.start()

    def _EH_ChatEncryptionLabelClicked(self):
        stream = self.selected_session.chat_stream
        stream_info = self.selected_session.blink_session.info.streams.chat
        if self.selected_session.blink_session.chat_type is None:
            stream = self.selected_session.messages_stream
            stream_info = self.selected_session.blink_session.info.streams.messages

        if stream is not None and not stream._done and stream_info.encryption == 'OTR':
            if self.otr_widget.isVisible():
                self.otr_widget.hide()
            else:
                encryption_label = self.chat_encryption_label
                self.zrtp_widget.hide()
                self.otr_widget.peer_name = stream_info.otr_peer_name
                self.otr_widget.peer_verified = stream_info.otr_verified
                self.otr_widget.peer_fingerprint = stream_info.otr_peer_fingerprint
                self.otr_widget.my_fingerprint = stream_info.otr_key_fingerprint
                self.otr_widget.smp_status = stream_info.smp_status
                self.otr_widget.setGeometry(QRect(0, encryption_label.rect().translated(encryption_label.mapTo(self.info_panel, QPoint(0, 0))).bottom() + 3, self.info_panel.width(), 320))
                self.otr_widget.verification_stack.setCurrentWidget(self.otr_widget.smp_panel)
                self.otr_widget.show()
                self.otr_widget.peer_name_value.setFocus(Qt.FocusReason.OtherFocusReason)

    def _EH_RTPEncryptionLabelClicked(self, encryption_label):
        stream = self.selected_session.blink_session.streams.get(encryption_label.stream_type)
        stream_info = self.selected_session.blink_session.info.streams[encryption_label.stream_type]
        if stream is not None and not stream._done and stream_info.encryption == 'ZRTP':
            if self.zrtp_widget.isVisible() and self.zrtp_widget.stream_type == encryption_label.stream_type:
                self.zrtp_widget.hide()
                self.zrtp_widget.stream_type = None
            else:
                self.zrtp_widget.hide()
                self.zrtp_widget.peer_name = stream_info.zrtp_peer_name
                self.zrtp_widget.peer_verified = stream_info.zrtp_verified
                self.zrtp_widget.sas = stream_info.zrtp_sas
                self.zrtp_widget.stream_type = encryption_label.stream_type
                self.zrtp_widget.setGeometry(QRect(0, encryption_label.rect().translated(encryption_label.mapTo(self.info_panel, QPoint(0, 0))).bottom() + 3, self.info_panel.width(), 320))
                self.zrtp_widget.show()
                self.zrtp_widget.peer_name_value.setFocus(Qt.FocusReason.OtherFocusReason)


del ui_class, base_class


# Helpers
#

class TrafficNormalizer(object):
    boundaries = [(             1024, '%d%ss',                   1),
                  (          10*1024, '%.2fk%ss',           1024.0),  (        1024*1024, '%.1fk%ss',           1024.0),
                  (     10*1024*1024, '%.2fM%ss',      1024*1024.0),  (   1024*1024*1024, '%.1fM%ss',      1024*1024.0),
                  (10*1024*1024*1024, '%.2fG%ss', 1024*1024*1024.0),  (float('infinity'), '%.1fG%ss', 1024*1024*1024.0)]

    @classmethod
    def normalize(cls, value, bits_per_second=False):
        for boundary, format, divisor in cls.boundaries:
            if value < boundary:
                return format % (value/divisor, 'bp' if bits_per_second else 'B/')


