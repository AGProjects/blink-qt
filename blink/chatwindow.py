
import bisect
import base64
import locale
import os
import re
import uuid
import sys
import json
import platform

from PyQt6 import uic
from PyQt6.QtCore import Qt, QBuffer, QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSettings, QSize, QSizeF, QTimer, QUrl, pyqtSignal, QObject, QFileInfo, pyqtSlot
from PyQt6.QtGui import QAction, QBrush, QColor, QIcon, QImageReader, QKeyEvent, QLinearGradient, QPainter, QPalette, QPen, QPixmap, QPolygonF, QTextCharFormat, QTextCursor, QTextDocument
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings, QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QListView, QMenu, QStyle, QStyleOption, QStylePainter, QTextEdit, QToolButton, QPlainTextEdit
from PyQt6.QtWidgets import QFileDialog, QFileIconProvider

from abc import ABCMeta, abstractmethod
from application.notification import IObserver, NotificationCenter, ObserverWeakrefProxy, NotificationData
from application.python import Null, limit
from application.python.descriptor import WriteOnceAttribute
from application.python.types import MarkerType
from application.system import makedirs
from collections.abc import MutableSet
from collections import deque
from datetime import datetime, timedelta, timezone
from dateutil.tz import tzlocal
from functools import partial
from itertools import count
from lxml import etree, html
from lxml.html.clean import autolink

from weakref import proxy
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
from blink.configuration.datatypes import File, FileURL, GraphTimeScale
from blink.configuration.settings import BlinkSettings
from blink.contacts import URIUtils
from blink.history import HistoryManager
from blink.logging import MessagingTrace as log
from blink.messages import MessageManager, BlinkMessage
from blink.resources import ApplicationData, IconManager, Resources
from blink.sessions import ChatSessionModel, ChatSessionListView, SessionManager, StreamDescription, FileSizeFormatter, IncomingDialogBase, RequestList, BlinkFileTransfer
from blink.util import run_in_gui_thread, call_later, translate, copy_transfer_file
from blink.widgets.color import ColorHelperMixin
from blink.widgets.graph import Graph
from blink.widgets.otr import OTRWidget
from blink.widgets.util import ContextMenuActions, QtDynamicProperty
from blink.widgets.video import VideoSurface
from blink.widgets.zrtp import ZRTPWidget

if platform.system() == 'Darwin':
    try:
        from blink.macos_notification import mac_notify as desktop_notification
    except ImportError:
        desktop_notification = Null


__all__ = ['ChatWindow']


class Container(object): pass


# Chat style classes
#

class ChatStyleError(Exception): pass


class ChatHtmlTemplates(object):
    def __init__(self, style_path):
        try:
            self.message = open(os.path.join(style_path, 'html/message.html')).read()
            self.message_continuation = open(os.path.join(style_path, 'html/message_continuation.html')).read()
            self.notification = open(os.path.join(style_path, 'html/notification.html')).read()
            self.status = open(os.path.join(style_path, 'html/message_status.html')).read()
        except (OSError, IOError):
            raise ChatStyleError("missing or unreadable chat message html template files in %s" % os.path.join(style_path, 'html'))


class ChatMessageStyle(object):
    def __init__(self, name):
        self.name = name
        self.path = Resources.get('chat/styles/%s' % name)
        try:
            xml_tree = etree.parse(os.path.join(self.path, 'style.xml'), parser=etree.XMLParser(resolve_entities=False))
        except (etree.ParseError, OSError, IOError):
            self.info = {}
        else:
            self.info = dict((element.tag, element.text) for element in xml_tree.getroot())
        try:
            self.variants = tuple(sorted(name[:-len('.css')] for name in os.listdir(self.path) if name.endswith('.css')))
        except (OSError, IOError):
            self.variants = ()
        if not self.variants:
            raise ChatStyleError("chat style %s contains no variants" % name)
        self.html = ChatHtmlTemplates(self.path)

    @property
    def default_variant(self):
        default_variant = self.info.get('default_variant')
        return default_variant if default_variant in self.variants else self.variants[0]

    @property
    def font_family(self):
        return self.info.get('font_family', 'sans-serif')

    @property
    def font_size(self):
        try:
            return int(self.info['font_size'])
        except (KeyError, ValueError):
            return 11


# Chat content classes
#

class Link(object):
    __slots__ = 'prev', 'next', 'key', '__weakref__'


class OrderedSet(MutableSet):
    def __init__(self, iterable=None):
        self.__hardroot = Link()  # sentinel node for doubly linked list
        self.__root = root = proxy(self.__hardroot)
        root.prev = root.next = root
        self.__map = {}
        if iterable is not None:
            self |= iterable

    def __len__(self):
        return len(self.__map)

    def __contains__(self, key):
        return key in self.__map

    def __iter__(self):
        root = self.__root
        curr = root.next
        while curr is not root:
            yield curr.key
            curr = curr.next

    def __reversed__(self):
        root = self.__root
        curr = root.prev
        while curr is not root:
            yield curr.key
            curr = curr.prev

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, list(self))

    def add(self, key):
        if key not in self.__map:
            self.__map[key] = link = Link()
            root = self.__root
            last = root.prev
            link.prev, link.next, link.key = last, root, key
            last.next = link
            root.prev = proxy(link)

    def discard(self, key):
        if key in self.__map:
            link = self.__map.pop(key)
            link_prev = link.prev
            link_next = link.next
            link_prev.next = link_next
            link_next.prev = link_prev

    def clear(self):
        root = self.__root
        root.prev = root.next = root
        self.__map.clear()


class ChatContentBooleanOption(object):
    """Adds/removes name from css classes based on option being True/False"""

    def __init__(self, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return self.name in instance.__cssclasses__

    def __set__(self, obj, value):
        if value:
            obj.__cssclasses__.add(self.name)
        else:
            obj.__cssclasses__.discard(self.name)

    def __delete__(self, obj):
        raise AttributeError('attribute cannot be deleted')


class AnyValue(metaclass=MarkerType): pass


class ChatContentStringAttribute(object):
    """A string attribute that is also added as a css class"""

    def __init__(self, name, allowed_values=AnyValue):
        self.name = name
        self.allowed_values = allowed_values

    def __get__(self, instance, owner):
        if instance is None:
            return self
        try:
            return instance.__dict__[self.name]
        except KeyError:
            raise AttributeError("'{}' attribute is not set".format(self.name))

    def __set__(self, obj, value):
        if self.allowed_values is not AnyValue and value not in self.allowed_values:
            raise ValueError("invalid value for '{}': '{}'".format(self.name, value))
        old_value = obj.__dict__.get(self.name, None)
        obj.__cssclasses__.discard(old_value)
        if value is not None:
            obj.__cssclasses__.add(value)
        obj.__dict__[self.name] = value

    def __delete__(self, obj):
        raise AttributeError('attribute cannot be deleted')


class ChatContent(object, metaclass=ABCMeta):
    __cssclasses__ = ()

    continuation_interval = timedelta(0, 5 * 60)  # 5 minutes

    history = ChatContentBooleanOption('history')
    focus = ChatContentBooleanOption('focus')
    consecutive = ChatContentBooleanOption('consecutive')
    mention = ChatContentBooleanOption('mention')  # keep it here? or keep it at all? -Dan

    def __init__(self, message, history=False, focus=False):
        self.__cssclasses__ = OrderedSet(self.__class__.__cssclasses__)
        self.message = message
        self.history = history
        self.focus = focus
        self.timestamp = datetime.now()

    @property
    def css_classes(self):
        return ' '.join(self.__cssclasses__)

    @property
    def date(self):
        language, encoding = locale.getlocale(locale.LC_TIME)
        return self.timestamp.strftime('%d %b %Y')

    @property
    def time(self):
        language, encoding = locale.getlocale(locale.LC_TIME)
        return self.timestamp.strftime('%H:%M')

    @property
    def text_direction(self):
        try:
            return self.__dict__['text_direction']
        except KeyError:
            document = QTextDocument()
            document.setHtml(self.message)
            return self.__dict__.setdefault('text_direction', 'rtl' if document.firstBlock().textDirection() == Qt.LayoutDirection.RightToLeft else 'ltr')

    def add_css_class(self, name):
        self.__cssclasses__.add(name)

    def is_related_to(self, other):
        return type(self) is type(other) and self.history == other.history and timedelta(0) <= self.timestamp - other.timestamp <= self.continuation_interval

    @abstractmethod
    def to_html(self, style, **kw):
        raise NotImplementedError


class ChatMessageStatus(ChatContent):
    __cssclasses__ = ('status-icon',)

    def __init__(self, status, iconpath, id):
        super(ChatMessageStatus, self).__init__(status)
        self.status = status
        self.iconpath = QUrl.fromLocalFile(iconpath).toString()
        self.id = id

    def to_html(self, style, **kw):
        return style.html.status.format(message=self, **kw)


class ChatNotification(ChatContent):
    __cssclasses__ = ('event',)

    def to_html(self, style, **kw):
        return style.html.notification.format(message=self, **kw)


class ChatEvent(ChatNotification):
    __cssclasses__ = ('event',)

    direction = ChatContentStringAttribute('direction', allowed_values=('incoming', 'outgoing'))

    def __init__(self, message, direction='incoming', history=False, focus=False, id=None, timestamp=None):
        super(ChatEvent, self).__init__(message, history, focus)
        self.direction = direction
        self.id = str(uuid.uuid4()) if id is None else id
        self.timestamp = timestamp if timestamp is not None else ISOTimestamp.now()


class ChatStatus(ChatNotification):
    __cssclasses__ = ('status',)


class ChatMessage(ChatContent):
    __cssclasses__ = ('message',)

    direction = ChatContentStringAttribute('direction', allowed_values=('incoming', 'outgoing'))
    autoreply = ChatContentBooleanOption('autoreply')

    def __init__(self, message, sender, direction, history=False, focus=False, id=None, timestamp=None, account=None):
        super(ChatMessage, self).__init__(message, history, focus)
        self.sender = sender
        self.direction = direction
        self.id = str(uuid.uuid4()) if id is None else id
        self.status = ''
        self.account = account
        self.timestamp = timestamp if timestamp is not None else ISOTimestamp.now()

    def is_related_to(self, other):
        return super(ChatMessage, self).is_related_to(other) and self.sender == other.sender and self.direction == other.direction

    def to_html(self, style, **kw):
        if self.consecutive:
            return style.html.message_continuation.format(message=self, **kw)
        else:
            return style.html.message.format(message=self, **kw)


class ChatFile(ChatMessage): pass


class ChatSender(object):
    __colors__ = ["aqua", "aquamarine", "blue", "blueviolet", "brown", "burlywood", "cadetblue", "chartreuse", "chocolate", "coral", "cornflowerblue", "crimson", "cyan", "darkblue", "darkcyan",
                  "darkgoldenrod", "darkgreen", "darkgrey", "darkkhaki", "darkmagenta", "darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon", "darkseagreen", "darkslateblue",
                  "darkslategrey", "darkturquoise", "darkviolet", "deeppink", "deepskyblue", "dimgrey", "dodgerblue", "firebrick", "forestgreen", "fuchsia", "gold", "goldenrod", "green",
                  "greenyellow", "grey", "hotpink", "indianred", "indigo", "lawngreen", "lightblue", "lightcoral", "lightgreen", "lightgrey", "lightpink", "lightsalmon", "lightseagreen",
                  "lightskyblue", "lightslategrey", "lightsteelblue", "lime", "limegreen", "magenta", "maroon", "mediumaquamarine", "mediumblue", "mediumorchid", "mediumpurple", "mediumseagreen",
                  "mediumslateblue", "mediumspringgreen", "mediumturquoise", "mediumvioletred", "midnightblue", "navy", "olive", "olivedrab", "orange", "orangered", "orchid", "palegreen",
                  "paleturquoise", "palevioletred", "peru", "pink", "plum", "powderblue", "purple", "red", "rosybrown", "royalblue", "saddlebrown", "salmon", "sandybrown", "seagreen", "sienna",
                  "silver", "skyblue", "slateblue", "slategrey", "springgreen", "steelblue", "tan", "teal", "thistle", "tomato", "turquoise", "violet", "yellowgreen"]

    def __init__(self, name, uri, iconpath):
        self.name = name
        self.uri = uri
        self.iconpath = QUrl.fromLocalFile(iconpath).toString()

    def __eq__(self, other):
        if not isinstance(other, ChatSender):
            return NotImplemented
        return self.name == other.name and self.uri == other.uri

    def __ne__(self, other):
        return not (self == other)

    @property
    def color(self):
        return self.__colors__[hash(self.uri) % len(self.__colors__)]


class ChatWebPage(QWebEnginePage):
    linkClicked = pyqtSignal(QUrl)

    def __init__(self, parent=None):
        super(ChatWebPage, self).__init__(parent)
        disable_actions = {QWebEnginePage.WebAction.OpenLinkInNewBackgroundTab, QWebEnginePage.WebAction.OpenLinkInNewTab, QWebEnginePage.WebAction.OpenLinkInNewWindow,
                           QWebEnginePage.WebAction.OpenLinkInThisWindow,
                           QWebEnginePage.WebAction.DownloadLinkToDisk, QWebEnginePage.WebAction.DownloadImageToDisk, QWebEnginePage.WebAction.DownloadMediaToDisk,
                           QWebEnginePage.WebAction.Back, QWebEnginePage.WebAction.Forward, QWebEnginePage.WebAction.Stop, QWebEnginePage.WebAction.Reload,
                           QWebEnginePage.WebAction.SavePage, QWebEnginePage.WebAction.ViewSource}
        for action in (self.action(action) for action in disable_actions):
            action.setVisible(False)

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):  # not sure if needed since we already disabled the corresponding actions. (can they be triggered otherwise?)
        if navigation_type in (QWebEnginePage.NavigationType.NavigationTypeBackForward, QWebEnginePage.NavigationType.NavigationTypeReload):
            return False
        elif navigation_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            self.linkClicked.emit(url)
            return False
        return super(ChatWebPage, self).acceptNavigationRequest(url, navigation_type, is_main_frame)


class ChatWebView(QWebEngineView):
    sizeChanged = pyqtSignal()
    messageDelete = pyqtSignal(str)

    def __init__(self, parent=None):
        super(ChatWebView, self).__init__(parent)
        palette = self.palette()
        palette.setBrush(QPalette.ColorRole.Base, Qt.GlobalColor.transparent)
        self.setPalette(palette)
        self.setPage(ChatWebPage(self))
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.last_message_id = None

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        self.id = None
        if self.last_message_id is not None:
            if self.last_message_id.startswith('text-'):
                self.id = self.last_message_id[5:]
                action = menu.addAction('Delete Message')
                action.triggered.connect(self._SH_DeleteClicked)

            if self.last_message_id.startswith('message-'):
                self.id = self.last_message_id[8:]
                action = menu.addAction('Delete Message')
                action.triggered.connect(self._SH_DeleteClicked)

        if any(action.isVisible() and not action.isSeparator() for action in menu.actions()):
            menu.aboutToHide.connect(self._SH_AboutToHide)
            menu.exec(event.globalPos())

    def createWindow(self, window_type):
        print("create window of type", window_type)
        return None

    def dragEnterEvent(self, event):
        event.ignore()  # let the parent process DND

    def resizeEvent(self, event):
        super(ChatWebView, self).resizeEvent(event)
        self.sizeChanged.emit()

    def _SH_DeleteClicked(self):
        self.messageDelete.emit(self.id)

    def _SH_AboutToHide(self):
        self.last_message_id = None


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


class LockType(object, metaclass=MarkerType):
    note_text = None
    button_text = None


class EncryptionLock(LockType):
    note_text = 'Encryption has been terminated by the other party'
    button_text = 'Confirm'


class QTextEdit(QTextEdit):
    """QPlainTextEdit with placeholder text option.
    Reimplemented from the C++ code used in Qt5.
    """
    def __init__(self, *args, **kwargs):
        super(QTextEdit, self).__init__(*args, **kwargs)

        self._placeholderText = ''
        self._placeholderVisible = False
        self.textChanged.connect(self.placeholderVisible)

    def placeholderVisible(self):
        """Return if the placeholder text is visible, and force update if required."""
        placeholderCurrentlyVisible = self._placeholderVisible
        self._placeholderVisible = self._placeholderText and self.document().isEmpty() and not self.hasFocus()
        if self._placeholderVisible != placeholderCurrentlyVisible:
            self.viewport().update()
        return self._placeholderVisible

    def placeholderText(self):
        """Return text used as a placeholder."""
        return self._placeholderText

    def setPlaceholderText(self, text):
        """Set text to use as a placeholder."""
        self._placeholderText = text
        if self.document().isEmpty():
            self.viewport().update()

    def paintEvent(self, event):
        """Override the paint event to add the placeholder text."""
        if self.placeholderVisible():
            painter = QPainter(self.viewport())
            colour = self.palette().text().color()
            colour.setAlpha(128)
            painter.setPen(colour)
            painter.setClipRect(self.rect())
            margin = self.document().documentMargin()
            textRect = self.viewport().rect().adjusted(int(margin), int(margin), 0, 0)
            painter.drawText(textRect, Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.placeholderText())

        super(QTextEdit, self).paintEvent(event)

class ChatTextInput(QTextEdit):
    textEntered = pyqtSignal(str)
    lockEngaged = pyqtSignal(object)
    lockReleased = pyqtSignal(object)

    def __init__(self, parent=None):
        super(ChatTextInput, self).__init__(parent)
        self.setTabStopDistance(22)
        self.lock_widget = ChatInputLock(self)
        self.lock_widget.hide()
        self.lock_widget.confirm_button.clicked.connect(self._SH_LockWidgetConfirmButtonClicked)
        self.document().documentLayout().documentSizeChanged.connect(self._SH_DocumentLayoutSizeChanged)
        self.lock_queue = deque()
        self.history = []
        self.history_index = 0  # negative indexes with 0 indicating the text being typed.
        self.stashed_content = None
        self.setPlaceholderText(translate('chat_window', 'Write a message'))
        

    @property
    def empty(self):
        document = self.document()
        last_block = document.lastBlock()
        return document.characterCount() <= 1 and not last_block.textList()

    @property
    def locked(self):
        return bool(self.lock_queue)

    def dragEnterEvent(self, event):
        event.ignore()  # let the parent process DND

    def keyPressEvent(self, event):
        key, modifiers = event.key(), event.modifiers()
        if self.isReadOnly():
            event.ignore()
        elif key in (Qt.Key.Key_Enter, Qt.Key.Key_Return) and modifiers == Qt.KeyboardModifier.NoModifier:
            document = self.document()
            last_block = document.lastBlock()
            if document.characterCount() > 1 or last_block.textList():
                text = self.toHtml()
                if not self.history or self.history[-1] != text:
                    self.history.append(text)
                self.history_index = 0
                self.stashed_content = None
                if document.blockCount() > 1 and not last_block.text() and not last_block.textList():
                    # prevent an extra empty line being added at the end of the text
                    cursor = self.textCursor()
                    cursor.movePosition(cursor.MoveOperation.End)
                    cursor.deletePreviousChar()
                text = self.toHtml()
                self.clear()
                self.textEntered.emit(text)
            event.accept()
        elif key == Qt.Key.Key_Up and modifiers == Qt.KeyboardModifier.ControlModifier:
            try:
                history_entry = self.history[self.history_index - 1]
            except IndexError:
                pass
            else:
                if self.history_index == 0:
                    self.stashed_content = self.toHtml()
                self.history_index -= 1
                self.setHtml(history_entry)
            event.accept()
        elif key == Qt.Key.Key_Down and modifiers == Qt.KeyboardModifier.ControlModifier:
            if self.history_index == 0:
                pass
            elif self.history_index == -1:
                self.history_index = 0
                self.setHtml(self.stashed_content)
                self.stashed_content = None
            else:
                self.history_index += 1
                self.setHtml(self.history[self.history_index])
            event.accept()
        else:
            QTextEdit.keyPressEvent(self, event)

    def _SH_DocumentLayoutSizeChanged(self, new_size):
        self.setFixedHeight(int(min(new_size.height() + self.contentsMargins().top() + self.contentsMargins().bottom(), self.parent().height() / 2)))

    def _SH_LockWidgetConfirmButtonClicked(self):
        self.lockReleased.emit(self.lock_queue.popleft())
        if self.locked:
            lock_type = self.lock_queue[0]
            self.lock_widget.note_label.setText(lock_type.note_text)
            self.lock_widget.confirm_button.setText(lock_type.button_text)
            self.lockEngaged.emit(lock_type)
        else:
            self.lock_widget.hide()
            self.setReadOnly(False)

    def lock(self, lock_type):
        if lock_type in self.lock_queue:
            raise ValueError("already locked with {}".format(lock_type))
        if not self.locked:
            self.lock_widget.note_label.setText(lock_type.note_text)
            self.lock_widget.confirm_button.setText(lock_type.button_text)
            self.lock_widget.show()
            self.setReadOnly(True)
            self.lockEngaged.emit(lock_type)
        self.lock_queue.append(lock_type)

    def reset_locks(self):
        self.setReadOnly(False)
        self.lock_widget.hide()
        self.lock_queue.clear()

    def clear(self):
        super(ChatTextInput, self).clear()
        self.setCurrentCharFormat(QTextCharFormat())  # clear() doesn't clear the text formatting, only the content

    def setHtml(self, text):
        super(ChatTextInput, self).setHtml(text)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)


class FileIcon(object):
    def __new__(cls, filename):
        icon = QFileIconProvider().icon(QFileInfo(filename))
        image = icon.pixmap(32)
        image_buffer = QBuffer()
        image_format = 'png' if image.hasAlphaChannel() else 'jpeg'
        image.save(image_buffer, image_format)
        image_data = image_buffer.data()
        instance = super(FileIcon, cls).__new__(cls)
        instance.__dict__['data'] = image_data
        instance.__dict__['type'] = 'image/{}'.format(image_format)
        return instance

    @property
    def data(self):
        return self.__dict__['data']

    @property
    def type(self):
        return self.__dict__['type']


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


class AudioDescriptor(object):
    def __new__(cls, filename):
        basename = os.path.basename(filename)
        if basename.startswith('sylk-audio-recording'):
            instance = super(AudioDescriptor, cls).__new__(cls)
        else:
            instance = None
        return instance


class Thumbnail(object):
    def __new__(cls, filename):
        image_reader = QImageReader(filename)
        if image_reader.canRead() and image_reader.size().isValid():
            if image_reader.supportsAnimation() and image_reader.imageCount() > 1:
                image_format = str(image_reader.format())
                image_data = image_reader.device().read()
            else:
                file_format = str(image_reader.format())
                file_size = image_reader.device().size()
                image_size = image_reader.size()
                if image_size.height() > 720:
                    image_reader.setScaledSize(image_size * 720 / image_size.height())
                image = QPixmap.fromImageReader(image_reader)
                image_buffer = QBuffer()
                image_format = 'png' if image.hasAlphaChannel() or (file_format in {'png', 'tiff', 'ico'} and file_size <= 100 * 1024) else 'jpeg'
                image.save(image_buffer, image_format)
                image_data = image_buffer.data()
            instance = super(Thumbnail, cls).__new__(cls)
            instance.__dict__['data'] = image_data
            instance.__dict__['type'] = 'image/{}'.format(image_format)
        else:
            instance = None
        return instance

    @property
    def data(self):
        return self.__dict__['data']

    @property
    def type(self):
        return self.__dict__['type']


class FileDescriptor(object):
    filename  = WriteOnceAttribute()
    thumbnail = WriteOnceAttribute()

    def __init__(self, filename):
        self.filename = filename
        self.thumbnail = Thumbnail(filename)

    def __hash__(self):
        return hash(self.filename)

    def __eq__(self, other):
        if isinstance(other, FileDescriptor):
            return self.filename == other.filename
        return NotImplemented

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        return 'FileDescriptor({})'.format(self.filename)

    @property
    def fileurl(self):
        return QUrl.fromLocalFile(self.filename).toString()


class ChatJSInterface(QObject):
    contextMenuEvent = pyqtSignal(str)
    js_file = open(Resources.get('chat/js_helper_functions.js')).read()

    def __init__(self, page, parent=None):
        super(ChatJSInterface, self).__init__(parent)
        self.page = page
        self.loaded = False
        self._js_operations_queue = deque()
        self.channel = QWebChannel()
        self.channel.registerObject('chat', self)
        self.page.setWebChannel(self.channel)
        # self.page.profile().scripts().insert(self._get_script())

    # Somehow the script gets inserted in every page, for now we load it from html
    def _get_script(self):
        script = QWebEngineScript()
        script.setSourceCode(self.js_file)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setRunsOnSubFrames(True)
        return script

    @pyqtSlot(bool)
    def _JH_LoadFinished(self, ok):
        if ok:
            self.loaded = True
            self._run_js()

    def _js_operation(self, operation):
        self._js_operations_queue.append(operation)
        self._run_js()

    def _run_js(self):
        if not self.loaded:
            return
        while self._js_operations_queue:
            operation = self._js_operations_queue.popleft()
            if isinstance(operation, tuple):
                self.page.runJavaScript(*operation)
            else:
                self.page.runJavaScript(operation)

    def append_element(self, query, content):
        content = json.dumps(content)
        self._js_operation(f"appendElement('{query}', {content})")

    def update_element(self, query, content):
        content = json.dumps(content)
        self._js_operation(f"updateElement('{query}', {content})")

    def replace_element(self, query, content):
        content = json.dumps(content)
        self._js_operation(f"replaceElement('{query}', {content})")

    def remove_element(self, query):
        self._js_operation(f"removeElement('{query}')")

    def empty_element(self, query):
        self._js_operation(f"emptyElement('{query}')")

    def previous_sibling(self, query, content):
        content = json.dumps(content)
        self._js_operation(f"previousSibling('{query}', {content})")

    def insert_as_parent(self, query, content, new_consecutive):
        content = json.dumps(content)
        new_consecutive = json.dumps(new_consecutive)
        self._js_operation(f"insertAsParent('{query}', {content}, {new_consecutive})")

    def prepend_outside_element(self, query, content):
        content = json.dumps(content)
        self._js_operation(f"prependOutside('{query}', {content})")

    def append_outside_element(self, query, content):
        content = json.dumps(content)
        self._js_operation(f"appendOutside('{query}', {content})")

    def set_style_property_element(self, query, property, value):
        self._js_operation(f"styleElement('{query}', '{property}', '{value}')")

    def get_height_element(self, query, callback):
        self._js_operation((f"getHeightElement('{query}')", callback))

    def scroll_to_bottom(self):
        self._js_operation('scrollToBottom()')

    def append_message_to_chat(self, content, id=None):
        content = json.dumps(content)
        self._js_operation(f"appendMessageToChat({content})")
        if id:
            self.add_context_menu(id)

    def add_context_menu(self, id):
        if id is not None:
            self._js_operation(f"addContextMenuToElement('#message-{id}')")

    @pyqtSlot(str)
    def handleContextMenuEvent(self, id):
        if id != '':
            self.contextMenuEvent.emit(id)


ui_class, base_class = uic.loadUiType(Resources.get('delete_message_dialog.ui'))


class DeleteMessageDialog(IncomingDialogBase, ui_class):
    def __init__(self, parent=None):
        super(DeleteMessageDialog, self).__init__(parent)

        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)

        self.delete_button = self.dialog_button_box.addButton(translate("delete_message_dialog", "Delete"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.delete_button.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.delete_button.setStyleSheet("""
                                         border-color: #800000;
                                         color: #800000;
                                         """)
        self.slot = None

    def show(self, activate=True):
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        super(DeleteMessageDialog, self).show()


class DeleteMessageRequest(QObject):
    finished = pyqtSignal(object)
    accepted = pyqtSignal(object)
    rejected = pyqtSignal(object)
    priority = 5

    template = open(Resources.get('chat/template.html')).read()

    def __init__(self, dialog, session, account, id, messages):
        super(DeleteMessageRequest, self).__init__()
        self.session = session
        self.account = account
        self.dialog = dialog
        self.id = id
        self.dialog.finished.connect(self._SH_DialogFinished)

        blink_settings = BlinkSettings()
        self.style = ChatMessageStyle(blink_settings.chat_window.style)
        self.style_variant = blink_settings.chat_window.style_variant or self.style.default_variant
        self.font_family = blink_settings.chat_window.font or self.style.font_family
        self.font_size = blink_settings.chat_window.font_size or self.style.font_size
        self.user_icons = 'show-icons' if blink_settings.chat_window.show_user_icons else 'hide-icons'
        self.dialog.message_view.setHtml(self.template.format(base_url=FileURL(self.style.path) + '/', style_url=self.style_variant + '.css', font_family=self.font_family, font_size=self.font_size), baseUrl=QUrl.fromLocalFile(os.path.abspath(sys.argv[0])))
        self.chat_js = ChatJSInterface(self.dialog.message_view.page())

        self.dialog.message_view.last_message = None

        def add_message(message):
            if message.direction == 'outgoing':
                replace = self.dialog.delete_both_checkbox.text().replace('__REMOTE_PARTY__', self.session.contact.name)
                self.dialog.delete_both_checkbox.setText(replace)
            else:
                self.dialog.delete_both_checkbox.hide()
                self.dialog.delete_both_checkbox.setEnabled(False)

            if isinstance(message, ChatFile):
                replace1 = self.dialog.delete_message_dialog_text.text().replace('message', translate('delete_message_dialog', 'file'))
                replace2 = self.dialog.delete_message_dialog_title.text().replace('message', translate('delete_message_dialog', 'file'))
                self.dialog.delete_message_dialog_text.setText(replace1)
                self.dialog.delete_message_dialog_title.setText(replace2)

            message.consecutive = False
            message.history = False
            if message.is_related_to(self.dialog.message_view.last_message):
                message.consecutive = True
                html_message = message.to_html(self.style, user_icons=self.user_icons)
                self.chat_js.replace_element('#insert', html_message)
            else:
                html_message = message.to_html(self.style, user_icons=self.user_icons)
                self.chat_js.append_message_to_chat(html_message)
            self.dialog.message_view.last_message = message

        for message in messages:
            add_message(message)

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
ui_class, base_class = uic.loadUiType(Resources.get('chat_widget.ui'))


@implementer(IObserver)
class ChatWidget(base_class, ui_class):
    default_user_icon = IconDescriptor(Resources.get('icons/default-avatar.png'))
    checkmark_icon = IconDescriptor(Resources.get('icons/checkmark.svg'))
    warning_icon = IconDescriptor(Resources.get('icons/warning.svg'))
    done_all_icon = IconDescriptor(Resources.get('icons/done-all.svg'))
    encrypted_icon = IconDescriptor(Resources.get('icons/lock-grey-12.svg'))
    clock_icon = IconDescriptor(Resources.get('icons/clock.svg'))

    chat_template = open(Resources.get('chat/template.html')).read()
    loading_template = open(Resources.get('chat/loading.html')).read()

    image_data_re = re.compile(r"data:(?P<type>image/.+?);base64,(?P<data>.*)", re.I|re.U)

    def __init__(self, session, parent=None):
        super(ChatWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        blink_settings = BlinkSettings()
        self.style = ChatMessageStyle(blink_settings.chat_window.style)
        self.style_variant = blink_settings.chat_window.style_variant or self.style.default_variant
        self.font_family = blink_settings.chat_window.font or self.style.font_family
        self.font_size = blink_settings.chat_window.font_size or self.style.font_size
        self.user_icons_css_class = 'show-icons' if blink_settings.chat_window.show_user_icons else 'hide-icons'
        self.chat_view.setHtml(self.chat_template.format(base_url=FileURL(self.style.path) + '/', style_url=self.style_variant + '.css', font_family=self.font_family, font_size=self.font_size), baseUrl=QUrl.fromLocalFile(os.path.abspath(sys.argv[0])))
        self.chat_js = ChatJSInterface(self.chat_view.page())
        self.composing_timer = QTimer()
        self.otr_timer = QTimer()
        self.otr_timer.setSingleShot(True)
        self.otr_timer.setInterval(15000)
        self.last_message = None
        self.session = session
        self.history_loaded = False
        self.timestamp_rendered_messages = []
        self.pending_decryption = []
        self.remove_requests = RequestList()
        self.size = QSizeF()
        if session is not None:
            notification_center = NotificationCenter()
            notification_center.add_observer(ObserverWeakrefProxy(self), sender=session.blink_session)
            self.show_loading_screen(True)

        # connect to signals
        self.chat_input.textChanged.connect(self._SH_ChatInputTextChanged)
        self.chat_input.textEntered.connect(self._SH_ChatInputTextEntered)
        self.chat_input.lockReleased.connect(self._SH_ChatInputLockReleased)
        self.chat_view.sizeChanged.connect(self._SH_ChatViewSizeChanged)

        self.chat_view.page().contentsSizeChanged.connect(self._SH_ChatViewFrameContentsSizeChanged)
        self.chat_view.page().linkClicked.connect(self._SH_LinkClicked)
        self.chat_js.contextMenuEvent.connect(self._SH_ContextMenuEvent)

        self.chat_view.messageDelete.connect(self._SH_MessageDelete)

        self.composing_timer.timeout.connect(self._SH_ComposingTimerTimeout)
        self.otr_timer.timeout.connect(self._SH_OTRTimerTimeout)

    @property
    def user_icon(self):
        return IconManager().get('avatar') or self.default_user_icon

    def add_message(self, message):
        message_id = None
        if hasattr(message, 'id'):
            message_id = message.id
            if self.last_message is not None and not self.last_message.history and message.history:
                message.history = False

            for i, (timestamp, id, rendered_message) in enumerate(self.timestamp_rendered_messages):
                if id == message.id:
                    return

                (prev_timestamp, prev_id, previous_rendered_message) = self.timestamp_rendered_messages[i-1]
                if timestamp >= message.timestamp:
                    if message.is_related_to(previous_rendered_message):
                        print(f'consecutive {message_id} to previous {previous_rendered_message.id} {timestamp} > {message.timestamp}')
                        message.consecutive = True
                        html_message = message.to_html(self.style, user_icons=self.user_icons_css_class).replace("<div id=\"insert\"></div>", '').replace("<span id=\"insert\"></span>", '')
                        if previous_rendered_message.consecutive:
                            self.chat_js.append_outside_element(f'message-{previous_rendered_message.id}', html_message)
                        else:
                            if message.is_related_to(rendered_message):
                                self.chat_js.prepend_outside_element(f'message-{id}', html_message)
                            else:
                                self.chat_js.append_element(f'#message-{prev_id}', html_message)
                        self.chat_js.add_context_menu(message.id)
                    elif message.is_related_to(rendered_message):
                        if rendered_message.consecutive:
                            message.consecutive = True
                            html_message = message.to_html(self.style, user_icons=self.user_icons_css_class).replace("<div id=\"insert\"></div>", '').replace("<span id=\"insert\"></span>", '')
                            self.chat_js.previous_sibling(f'message-{id}', html_message)
                            self.chat_js.add_context_menu(message.id)
                        else:
                            html_message = message.to_html(self.style, user_icons=self.user_icons_css_class)
                            rendered_message.consecutive = True
                            html_rendered_message = rendered_message.to_html(self.style, user_icons=self.user_icons_css_class).replace("<div id=\"insert\"></div>", '').replace("<span id=\"insert\"></span>", '')
                            self.timestamp_rendered_messages[i] = (rendered_message.timestamp, rendered_message.id, rendered_message)
                            self.chat_js.insert_as_parent(f'message-{id}', html_message, html_rendered_message)
                            self.chat_js.add_context_menu(message.id)
                    else:
                        html_message = message.to_html(self.style, user_icons=self.user_icons_css_class).replace("<div id=\"insert\"></div>", '').replace("<span id=\"insert\"></span>", '')
                        self.chat_js.prepend_outside_element(f'message-{id}', html_message)
                        self.chat_js.add_context_menu(message.id)
                    self.timestamp_rendered_messages.insert(i, (message.timestamp, message.id, message))

                    try:
                        if self.last_message.timestamp < message.timestamp:
                            self.last_message = message
                    except (TypeError, AttributeError):
                        self.last_message.timestamp = self.last_message.timestamp.replace(tzinfo=tzlocal())
                        if self.last_message.timestamp < message.timestamp:
                            self.last_message = message

                    return

        if message.is_related_to(self.last_message):
            message.consecutive = True
            html_message = message.to_html(self.style, user_icons=self.user_icons_css_class)
            self.chat_js.replace_element('#insert', html_message)
            self.chat_js.add_context_menu(message_id)
        else:
            html_message = message.to_html(self.style, user_icons=self.user_icons_css_class)
            self.chat_js.append_message_to_chat(html_message, message_id)

        if hasattr(message, 'id'):
            self.timestamp_rendered_messages.append((message.timestamp, message.id, message))
        self.last_message = message

    def replace_message(self, id, message):
        html = message.to_html(self.style, user_icons=self.user_icons_css_class).replace("<div id=\"insert\"></div>", '')
        self.chat_js.replace_element(f'#message-{id}', html)
        self.chat_js.add_context_menu(id)

    def update_message_text(self, id, text):
        self.chat_js.update_element(f'#text-{id}', text)
        for i, (timestamp, rendered_id, rendered_message) in enumerate(self.timestamp_rendered_messages):
            if rendered_id == id:
                rendered_message.message = text
                self.timestamp_rendered_messages[i] = (rendered_message.timestamp, rendered_message.id, rendered_message)

    def update_message_status(self, id, status):
        if status == 'pending':
            icon = self.clock_icon
        if status == 'failed':
            icon = self.warning_icon
        elif status == 'failed-local':
            icon = self.warning_icon
        elif status == 'displayed':
            icon = self.done_all_icon
        else:
            icon = self.checkmark_icon
        html = ChatMessageStatus(status, icon.filename, id).to_html(self.style)
        self.chat_js.replace_element(f'span#status-{id}', html)

    def update_message_encryption(self, id, is_secure=False):
        if is_secure is True:
            path = QUrl.fromLocalFile(self.encrypted_icon.filename).toString()
            html = f'<img src="{path}" class="status-icon is-secure">'
            self.chat_js.update_element(f'span#encryption-{id}', html)

    def remove_message(self, id):
        try:
            if self.last_message is not None and self.last_message.id == id:
                self.last_message = None
        except AttributeError:
            pass
        self.chat_js.remove_element(f'#message-{id}')

    def show_loading_screen(self, visible):
        if visible:
            self.chat_js.append_element('#loading', self.loading_template)
            self.chat_js.set_style_property_element('body', 'overflow', 'hidden')
        else:
            self.chat_js.empty_element('#loading')
            self.chat_js.set_style_property_element('body', 'overflow', 'auto')

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

        message_id = self.session.chat_stream.send_message(content, content_type, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)
        notification_center = NotificationCenter()

        timestamp = timestamp if timestamp is not None else ISOTimestamp.now()
        notification_center.post_notification('ChatStreamWillSendMessage', blink_session, data=BlinkMessage(content, content_type, blink_session.account, recipients, timestamp=timestamp, id=message_id))
        return message_id

    def start_otr_timer(self):
        self.otr_timer.start()

    def stop_otr_timer(self):
        self.otr_timer.stop()

    def _process_height(self, height, scroll=False):
        widget_height = self.chat_view.size().height()
        content_height = height
        if widget_height > content_height:
            self.chat_js.set_style_property_element('#chat', 'position', 'relative')
            self.chat_js.set_style_property_element('#chat', 'top', '%dpx' % (widget_height - content_height))
        else:
            self.chat_js.set_style_property_element('#chat', 'position', 'static')
            self.chat_js.set_style_property_element('#chat', 'top', '0')
        if scroll:
            self._scroll_to_bottom()

    def _align_chat(self, scroll=False):
        self.chat_js.get_height_element('#chat', partial(self._process_height, scroll=scroll))

    def _scroll_to_bottom(self):
        self.chat_js.scroll_to_bottom()


    def dragEnterEvent(self, event):
        mime_data = event.mimeData()
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
                self._DH_Files(urls)
            else:
                self._DH_Text('\n'.join(url.toString() for url in urls))
        else:
            mime_types = set(mime_data.formats())
            if mime_types.issuperset({'text/html', 'text/_moz_htmlcontext'}):
                text = str(mime_data.data('text/html'), encoding='utf16')
            else:
                text = mime_data.html() or mime_data.text()
            self._DH_Text(text)

    def _DH_Files(self, urls):
        session_manager = SessionManager()
        blink_session = self.session.blink_session

        file_descriptors  = [FileDescriptor(url.toLocalFile()) for url in urls]
        image_descriptors = [descriptor for descriptor in file_descriptors if descriptor.thumbnail is not None]
        other_descriptors = [descriptor for descriptor in file_descriptors if descriptor.thumbnail is None]

        # for image in image_descriptors:
        #     try:
        #         image_data = base64.b64encode(image.thumbnail.data).decode()
        #         self.send_message(image_data, content_type=image.thumbnail.type)
        #     except Exception as e:
        #         self.add_message(ChatStatus("Error sending image '%s': %s" % (os.path.basename(image.filename), str(e))))  # decide what type to use here. -Dan
        #     else:
        #         content = '''<a href="{}"><img src="data:{};base64,{}" class="scaled-to-fit" /></a>'''.format(image.fileurl, image.thumbnail.type, image_data)
        #         sender  = ChatSender(blink_session.account.display_name, blink_session.account.id, self.user_icon.filename)
        #         self.add_message(ChatFile(content, sender, 'outgoing'))

        for image in image_descriptors:
            session_manager.send_file(blink_session.contact, blink_session.contact_uri, image.filename, account=blink_session.account)

        for descriptor in other_descriptors:
            session_manager.send_file(blink_session.contact, blink_session.contact_uri, descriptor.filename, account=blink_session.account)

    def _DH_Text(self, text):
        match = self.image_data_re.match(text)
        if match is not None:
            try:
                data = match.group('data') if isinstance(match.group('data'), bytes) else match.group('data').encode()
                image_data = base64.b64encode(data).decode()
                self.send_message(image_data, content_type=match.group('type'))
            except Exception as e:
                self.add_message(ChatStatus(translate('chat_window', 'Error sending image: %s') % str(e)))
            else:
                account = self.session.blink_session.account
                content = '''<img src="{}" class="scaled-to-fit" />'''.format(text)
                sender  = ChatSender(account.display_name, account.id, self.user_icon.filename)
                self.add_message(ChatFile(content, sender, 'outgoing'))
        else:
            user_text = self.chat_input.toHtml()
            self.chat_input.setHtml(text)
            self.chat_input.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, text='\r'))
            self.chat_input.setHtml(user_text)

    def _SH_ContextMenuEvent(self, id):
        self.chat_view.last_message_id = id

    def _SH_LinkClicked(self, link):
        directory = ApplicationData.get(f'downloads')
        try:
            link = copy_transfer_file(link, directory)
            QDesktopServices.openUrl(link)
        except FileNotFoundError:
            blink_session = self.session.blink_session
            id = None
            if link.hasFragment():
                id = link.fragment()
            NotificationCenter().post_notification('BlinkSessionShouldDownloadFile',
                                                   sender=blink_session,
                                                   data=NotificationData(filename=link.fileName(), id=id))

    def delete_message(self, session, account, id, messages=[]):
        for request in self.remove_requests[account, DeleteMessageRequest]:
            request.dialog.hide()
            self.remove_requests.remove(request)

        delete_dialog = DeleteMessageDialog()
        delete_request = DeleteMessageRequest(delete_dialog, session, account, id, messages)
        delete_request.accepted.connect(self._SH_MessageDeleteAccepted)
        delete_request.finished.connect(self._SH_MessageDeleteRequestFinished)
        bisect.insort_right(self.remove_requests, delete_request)
        delete_request.dialog.show()

    def _SH_MessageDelete(self, id):
        blink_session = self.session.blink_session

        messages = [message for (timestamp, msg_id, message) in self.timestamp_rendered_messages if msg_id == id]
        if messages[0].direction == 'outgoing':
            account_manager = AccountManager()
            account = account_manager.get_account(messages[0].sender.uri)
        else:
            account = messages[0].account
        self.delete_message(blink_session, account, id, messages)

    def _SH_MessageDeleteAccepted(self, request):
        self.remove_message(request.id)
        NotificationCenter().post_notification('BlinkMessageWillDelete', sender=request.session, data=NotificationData(id=request.id))
        if request.dialog.delete_both_checkbox.isChecked():
            MessageManager().send_remove_message(request.session, request.id, request.account)

    def _SH_MessageDeleteRequestFinished(self, request):
        request.dialog.hide()
        self.remove_requests.remove(request)

    def _SH_ChatViewSizeChanged(self):
        # print("chat view size changed")
        self._align_chat(scroll=True)

    def _SH_ChatViewFrameContentsSizeChanged(self, size):
        # print("frame contents size changed to %r (current=%r)" % (self.size, self.chat_view.page().contentsSize()))
        self._align_chat(scroll=size.height() > self.size.height())
        self.size = size

    def _SH_ChatInputTextChanged(self):
        if self.session.blink_session.chat_type is None:
            manager = MessageManager()
            if self.chat_input.empty:
                if self.composing_timer.isActive():
                    self.composing_timer.stop()
                    manager.send_composing_indication(self.session.blink_session, 'idle')
            elif not self.composing_timer.isActive():
                manager.send_composing_indication(self.session.blink_session, 'active')
                self.composing_timer.start(10000)
        else:
            chat_stream = self.session.chat_stream
            if chat_stream is None:
                return
        if self.chat_input.empty:
            if self.composing_timer.isActive():
                self.composing_timer.stop()
                try:
                    chat_stream.send_composing_indication('idle')
                except Exception:
                    pass
        elif not self.composing_timer.isActive():
            try:
                chat_stream.send_composing_indication('active')
            except Exception:
                pass
            else:
                self.composing_timer.start(10000)

    def _SH_ChatInputTextEntered(self, text):
        self.composing_timer.stop()
        doc = QTextDocument()
        doc.setHtml(text)
        plain_text = doc.toPlainText()
        if plain_text == '/otr+':
            try:
                self.session.chat_stream.encryption.start()
            except AttributeError:
                pass
            return
        elif plain_text == '/otr-':
            try:
                self.session.chat_stream.encryption.stop()
            except AttributeError:
                pass
            return
        id = str(uuid.uuid4())
        try:
            msg_id = self.send_message(text, content_type='text/html', id=id)
        except Exception as e:
            self.add_message(ChatStatus(translate('chat_window', 'Error sending message: %s') % e))  # decide what type to use here. -Dan
        else:
            if msg_id is not None:
                id = msg_id
            account = self.session.blink_session.account
            content = HtmlProcessor.autolink(text)
            sender  = ChatSender(account.display_name, account.id, self.user_icon.filename)
            self.add_message(ChatMessage(content, sender, 'outgoing', id=id))

    def _SH_ChatInputLockReleased(self, lock_type):
        blink_session = self.session.blink_session
        if lock_type is EncryptionLock:
            if blink_session.chat_type is not None:
                self.session.chat_stream.encryption.stop()
            else:
                blink_session.fake_streams.get('messages').encryption.stop()

    def _SH_ComposingTimerTimeout(self):
        self.composing_timer.stop()
        if self.session.blink_session.chat_type is None:
            manager = MessageManager()
            manager.send_composing_indication(self.session.blink_session, 'idle')
            return

        chat_stream = self.session.chat_stream or Null
        try:
            chat_stream.send_composing_indication('idle')
        except Exception:
            pass

    def _SH_OTRTimerTimeout(self):
        self.otr_timer.stop()
        self.add_message(ChatStatus(translate('chat_window', 'Timeout in enabling OTR, recipient did not answer to OTR encryption request')))  # decide what type to use here. -Dan
        NotificationCenter().post_notification('ChatStreamOTRTimeout', self.session.blink_session)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidEnd(self, notification):
        self.composing_timer.stop()
        self.chat_input.reset_locks()

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.setParent(None)

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'chat':
            self.composing_timer.stop()
            self.chat_input.reset_locks()

    def _NH_BlinkSessionShouldDeleteFile(self, notification):
        item = notification.data.item
        blink_session = self.session.blink_session

        messages = [message for (timestamp, msg_id, message) in self.timestamp_rendered_messages if msg_id == item.id]
        account_manager = AccountManager()

        try:
            msg = messages[0]
        except IndexError:
            pass
        else:
            account = account_manager.get_account(msg.account.id)
            self.delete_message(blink_session, account, item.id, messages)

del ui_class, base_class


class VideoToolButton(QToolButton):
    active = QtDynamicProperty('active', bool)

    def event(self, event):
        if event.type() == QEvent.Type.DynamicPropertyChange and event.propertyName() == 'active':
            self.setVisible(self.active)
        return super(VideoToolButton, self).event(event)


ui_class, base_class = uic.loadUiType(Resources.get('video_widget.ui'))


@implementer(IObserver)
class VideoWidget(VideoSurface, ui_class):

    def __init__(self, session_item, parent=None):
        super(VideoWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi()
        self.session_item = session_item
        self.blink_session = session_item.blink_session
        self.parent_widget = parent
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.fullscreen_button.clicked.connect(self._SH_FullscreenButtonClicked)
        self.screenshot_button.clicked.connect(self._SH_ScreenshotButtonClicked)
        self.detach_button.clicked.connect(self._SH_DetachButtonClicked)
        self.mute_button.clicked.connect(self._SH_MuteButtonClicked)
        self.hold_button.clicked.connect(self._SH_HoldButtonClicked)
        self.close_button.clicked.connect(self._SH_CloseButtonClicked)
        self.screenshot_button.customContextMenuRequested.connect(self._SH_ScreenshotButtonContextMenuRequested)
        self.camera_preview.adjusted.connect(self._SH_CameraPreviewAdjusted)
        self.detach_animation.finished.connect(self._SH_DetachAnimationFinished)
        self.preview_animation.finished.connect(self._SH_PreviewAnimationFinished)
        self.idle_timer.timeout.connect(self._SH_IdleTimerTimeout)
        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(self.geometryHint())
            self.setVisible('video' in session_item.blink_session.streams)
        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=session_item.blink_session)
        notification_center.add_observer(ObserverWeakrefProxy(self), name='CFGSettingsObjectDidChange', sender=settings)
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoStreamRemoteFormatDidChange')
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoStreamReceivedKeyFrame')
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoDeviceDidChangeCamera')

    def setupUi(self):
        super(VideoWidget, self).setupUi(self)

        self.no_flicker_widget = QLabel()
        self.no_flicker_widget.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        # self.no_flicker_widget.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)

        self.camera_preview = VideoSurface(self, framerate=10)
        self.camera_preview.interactive = True
        self.camera_preview.mirror = True
        self.camera_preview.setMinimumHeight(45)
        self.camera_preview.setMaximumHeight(135)
        self.camera_preview.setGeometry(QRect(0, 0, self.camera_preview.width_for_height(81), 81))
        self.camera_preview.lower()
        self.camera_preview.scale_factor = 1.0

        self.detach_animation = None
        self.detach_animation = QPropertyAnimation(self, b'geometry')
        self.detach_animation.setDuration(200)
        self.detach_animation.setEasingCurve(QEasingCurve.Type.Linear)

        self.preview_animation = None
        self.preview_animation = QPropertyAnimation(self.camera_preview, b'geometry')
        self.preview_animation.setDuration(500)
        self.preview_animation.setDirection(QPropertyAnimation.Direction.Forward)
        self.preview_animation.setEasingCurve(QEasingCurve.Type.OutQuad)

        self.idle_timer = QTimer()
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(3000)

        for button in self.tool_buttons:
            button.setCursor(Qt.CursorShape.ArrowCursor)
            button.installEventFilter(self)
            button.active = False

        # fix the SVG icons, as the generated code loads them as pixmaps, losing their ability to scale -Dan
        fullscreen_icon = QIcon()
        fullscreen_icon.addFile(Resources.get('icons/fullscreen.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.Off)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Mode.Active, state=QIcon.State.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Mode.Disabled, state=QIcon.State.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Mode.Selected, state=QIcon.State.On)

        detach_icon = QIcon()
        detach_icon.addFile(Resources.get('icons/detach.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.Off)
        detach_icon.addFile(Resources.get('icons/attach.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.On)
        detach_icon.addFile(Resources.get('icons/attach.svg'), mode=QIcon.Mode.Active, state=QIcon.State.On)
        detach_icon.addFile(Resources.get('icons/attach.svg'), mode=QIcon.Mode.Disabled, state=QIcon.State.On)
        detach_icon.addFile(Resources.get('icons/attach.svg'), mode=QIcon.Mode.Selected, state=QIcon.State.On)

        mute_icon = QIcon()
        mute_icon.addFile(Resources.get('icons/mic-on.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.Off)
        mute_icon.addFile(Resources.get('icons/mic-off.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.On)
        mute_icon.addFile(Resources.get('icons/mic-off.svg'), mode=QIcon.Mode.Active, state=QIcon.State.On)
        mute_icon.addFile(Resources.get('icons/mic-off.svg'), mode=QIcon.Mode.Disabled, state=QIcon.State.On)
        mute_icon.addFile(Resources.get('icons/mic-off.svg'), mode=QIcon.Mode.Selected, state=QIcon.State.On)

        hold_icon = QIcon()
        hold_icon.addFile(Resources.get('icons/pause.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.Off)
        hold_icon.addFile(Resources.get('icons/paused.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.On)
        hold_icon.addFile(Resources.get('icons/paused.svg'), mode=QIcon.Mode.Active, state=QIcon.State.On)
        hold_icon.addFile(Resources.get('icons/paused.svg'), mode=QIcon.Mode.Disabled, state=QIcon.State.On)
        hold_icon.addFile(Resources.get('icons/paused.svg'), mode=QIcon.Mode.Selected, state=QIcon.State.On)

        screenshot_icon = QIcon()
        screenshot_icon.addFile(Resources.get('icons/screenshot.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.Off)

        close_icon = QIcon()
        close_icon.addFile(Resources.get('icons/close.svg'), mode=QIcon.Mode.Normal, state=QIcon.State.Off)
        close_icon.addFile(Resources.get('icons/close-active.svg'), mode=QIcon.Mode.Active, state=QIcon.State.Off)

        self.fullscreen_button.setIcon(fullscreen_icon)
        self.screenshot_button.setIcon(screenshot_icon)
        self.detach_button.setIcon(detach_icon)
        self.mute_button.setIcon(mute_icon)
        self.hold_button.setIcon(hold_icon)
        self.close_button.setIcon(close_icon)

        self.screenshot_button_menu = QMenu(self)
        self.screenshot_button_menu.addAction(translate('chat_window', 'Open screenshots folder'), self._SH_ScreenshotsFolderActionTriggered)

    @property
    def interactive(self):
        return self.parent() is None and not self.isFullScreen()

    @property
    def tool_buttons(self):
        return tuple(attr for attr in vars(self).values() if isinstance(attr, VideoToolButton))

    @property
    def active_tool_buttons(self):
        return tuple(button for button in self.tool_buttons if button.active)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if watched is self.parent():
            if event_type == QEvent.Type.Resize:
                self.setGeometry(self.geometryHint())
        elif event_type == QEvent.Type.Enter:
            self.idle_timer.stop()
            cursor = self.cursor()
            cursor_pos = cursor.pos()
            if not watched.rect().translated(watched.mapToGlobal(QPoint(0, 0))).contains(cursor_pos):
                # sometimes we get invalid enter events for the fullscreen_button after we switch to fullscreen.
                # simulate a mouse move in and out of the button to force qt to update the button state.
                cursor.setPos(self.mapToGlobal(watched.geometry().center()))
                cursor.setPos(cursor_pos)
        elif event_type == QEvent.Type.Leave:
            self.idle_timer.start()
        return False

    def mousePressEvent(self, event):
        super(VideoWidget, self).mousePressEvent(event)
        if self._interaction.active:
            for button in self.active_tool_buttons:
                button.show()  # show or hide the tool buttons while we move/resize? -Dan
            self.idle_timer.stop()

    def mouseReleaseEvent(self, event):
        if self._interaction.active:
            for button in self.active_tool_buttons:
                button.show()
            self.idle_timer.start()
        super(VideoWidget, self).mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        super(VideoWidget, self).mouseMoveEvent(event)
        if self._interaction.active:
            return
        if not self.idle_timer.isActive():
            for button in self.active_tool_buttons:
                button.show()
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.idle_timer.start()

    def resizeEvent(self, event):
        if self.preview_animation and self.preview_animation.state() == QPropertyAnimation.State.Running:
            return

        if not event.oldSize().isValid():
            return

        if self.camera_preview.size() == event.oldSize():
            self.camera_preview.resize(event.size())
            return

        old_size = QSizeF(event.oldSize())
        new_size = QSizeF(event.size())

        ratio = new_size.height() / old_size.height()

        if ratio == 1:
            return

        scaled_preview_geometry = QRectF(QPointF(self.camera_preview.geometry().topLeft()) * ratio, QSizeF(self.camera_preview.size()) * ratio)
        preview_center = scaled_preview_geometry.center()
        ideal_geometry = scaled_preview_geometry.toAlignedRect()

        if ideal_geometry.right() > self.rect().right():
            ideal_geometry.moveRight(self.rect().right())
        if ideal_geometry.bottom() > self.rect().bottom():
            ideal_geometry.moveBottom(self.rect().bottom())

        new_height = int(limit((new_size.height() + 117) / 6 * self.camera_preview.scale_factor, min=self.camera_preview.minimumHeight(), max=self.camera_preview.maximumHeight()))
        preview_geometry = QRect(0, 0, self.width_for_height(new_height), new_height)

        quadrant = QRectF(QPointF(0, 0), new_size / 3)

        if quadrant.translated(0, 0).contains(preview_center):                                      # top left gravity
            preview_geometry.moveTopLeft(ideal_geometry.topLeft())
        elif quadrant.translated(quadrant.width(), 0).contains(preview_center):                     # top gravity
            preview_geometry.moveCenter(ideal_geometry.center())
            preview_geometry.moveTop(ideal_geometry.top())
        elif quadrant.translated(2 * quadrant.width(), 0).contains(preview_center):                   # top right gravity
            preview_geometry.moveTopRight(ideal_geometry.topRight())

        elif quadrant.translated(0, quadrant.height()).contains(preview_center):                    # left gravity
            preview_geometry.moveCenter(ideal_geometry.center())
            preview_geometry.moveLeft(ideal_geometry.left())
        elif quadrant.translated(quadrant.width(), quadrant.height()).contains(preview_center):     # center gravity
            preview_geometry.moveCenter(ideal_geometry.center())
        elif quadrant.translated(2 * quadrant.width(), quadrant.height()).contains(preview_center):   # right gravity
            preview_geometry.moveCenter(ideal_geometry.center())
            preview_geometry.moveRight(ideal_geometry.right())

        elif quadrant.translated(0, 2 * quadrant.height()).contains(preview_center):                  # bottom left gravity
            preview_geometry.moveBottomLeft(ideal_geometry.bottomLeft())
        elif quadrant.translated(quadrant.width(), 2 * quadrant.height()).contains(preview_center):   # bottom gravity
            preview_geometry.moveCenter(ideal_geometry.center())
            preview_geometry.moveBottom(ideal_geometry.bottom())
        elif quadrant.translated(2 * quadrant.width(), 2 * quadrant.height()).contains(preview_center):  # bottom right gravity
            preview_geometry.moveBottomRight(ideal_geometry.bottomRight())

        self.camera_preview.setGeometry(preview_geometry)

    def setParent(self, parent):
        old_parent = self.parent()
        if old_parent is not None:
            old_parent.removeEventFilter(self)
        super(VideoWidget, self).setParent(parent)
        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(self.geometryHint())

    def setVisible(self, visible):
        if visible is False and self.isFullScreen():
            self.showNormal()
            if not self.detach_button.isChecked():
                self.setParent(self.parent_widget)
                self.setGeometry(self.parent().rect())
            self.fullscreen_button.setChecked(False)
        super(VideoWidget, self).setVisible(visible)

    def geometryHint(self, parent=None):
        parent = parent or self.parent()
        if parent is not None:
            origin = QPoint(0, 0)
            size   = QSize(parent.width(), min(self.height_for_width(parent.width()), parent.height() - 175))
        else:
            origin = self.geometry().topLeft()
            size   = QSize(self.width_for_height(self.height()), self.height())
        return QRect(origin, size)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionWillConnect(self, notification):
        if 'video' in notification.sender.streams:
            self.setParent(self.parent_widget)
            self.setGeometry(self.geometryHint())
            self.detach_button.setChecked(False)
            for button in self.tool_buttons:
                button.active = False
            self.camera_preview.setMaximumHeight(16777215)
            self.camera_preview.setGeometry(self.rect())
            self.camera_preview.setCursor(Qt.CursorShape.ArrowCursor)
            self.camera_preview.interactive = False
            self.camera_preview.scale_factor = 1.0
            self.camera_preview.producer = SIPApplication.video_device.producer
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.show()

    def _NH_BlinkSessionDidConnect(self, notification):
        video_stream = notification.sender.streams.get('video')
        if video_stream is not None:
            if self.parent() is None:
                self.setParent(self.parent_widget)
                self.setGeometry(self.geometryHint())
                self.detach_button.setChecked(False)
            for button in self.tool_buttons:
                button.active = False
            self.camera_preview.setMaximumHeight(16777215)
            self.camera_preview.setGeometry(self.rect())
            self.camera_preview.setCursor(Qt.CursorShape.ArrowCursor)
            self.camera_preview.interactive = False
            self.camera_preview.scale_factor = 1.0
            self.camera_preview.producer = SIPApplication.video_device.producer
            self.producer = video_stream.producer
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.show()
        else:
            self.hide()
            self.producer = None
            self._image = None
            self.camera_preview.producer = None
            self.camera_preview._image = None

    def _NH_BlinkSessionWillAddStream(self, notification):
        if notification.data.stream.type == 'video':
            self.setParent(self.parent_widget)
            self.setGeometry(self.geometryHint())
            self.detach_button.setChecked(False)
            for button in self.tool_buttons:
                button.active = False
            self.camera_preview.setMaximumHeight(16777215)
            self.camera_preview.setGeometry(self.rect())
            self.camera_preview.setCursor(Qt.CursorShape.ArrowCursor)
            self.camera_preview.interactive = False
            self.camera_preview.scale_factor = 1.0
            self.camera_preview.producer = SIPApplication.video_device.producer
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.show()

    def _NH_BlinkSessionDidAddStream(self, notification):
        if notification.data.stream.type == 'video':
            self.producer = notification.data.stream.producer

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'video':
            self.hide()
            self.producer = None
            self._image = None
            self.camera_preview.producer = None
            self.camera_preview._image = None

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'video':
            self.hide()
            self.producer = None
            self._image = None
            self.camera_preview.producer = None
            self.camera_preview._image = None

    def _NH_BlinkSessionDidEnd(self, notification):
        self.hide()
        self.producer = None
        self._image = None
        self.camera_preview.producer = None
        self.camera_preview._image = None

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.stop()
        self.setParent(None)
        self.session_item = None
        self.blink_session = None
        self.parent_widget = None
        self.detach_animation = None
        self.preview_animation = None

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        self.hold_button.setChecked(notification.data.local_hold)

    def _NH_VideoStreamRemoteFormatDidChange(self, notification):
        if notification.sender.blink_session is self.blink_session and not self.isFullScreen():
            self.setGeometry(self.geometryHint())

    def _NH_VideoStreamReceivedKeyFrame(self, notification):
        if notification.sender.blink_session is self.blink_session and self.preview_animation and self.preview_animation.state() != QPropertyAnimation.State.Running and self.camera_preview.size() == self.size():
            if self.preview_animation:
                self.preview_animation.setStartValue(self.rect())
                self.preview_animation.setEndValue(QRect(0, 0, self.camera_preview.width_for_height(81), 81))
                self.preview_animation.start()

    def _NH_VideoDeviceDidChangeCamera(self, notification):
        # self.camera_preview.producer = SIPApplication.video_device.producer
        self.camera_preview.producer = notification.data.new_camera

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if 'audio.muted' in notification.data.modified:
            self.mute_button.setChecked(settings.audio.muted)

    def _SH_CameraPreviewAdjusted(self, old_geometry, new_geometry):
        if new_geometry.size() != old_geometry.size():
            default_height_for_size = (self.height() + 117) / 6
            self.camera_preview.scale_factor = new_geometry.height() / default_height_for_size

    def _SH_IdleTimerTimeout(self):
        for button in self.active_tool_buttons:
            button.hide()
        self.setCursor(Qt.CursorShape.BlankCursor)

    def _SH_FullscreenButtonClicked(self, checked):
        if checked:
            if not self.detach_button.isChecked():
                geometry = self.rect().translated(self.mapToGlobal(QPoint(0, 0)))
                self.setParent(None)
                self.setGeometry(geometry)
                self.show()  # without this, showFullScreen below doesn't work properly
            self.detach_button.active = False
            self.mute_button.active = True
            self.hold_button.active = True
            self.close_button.active = True
            self.showFullScreen()
            self.fullscreen_button.hide()  # it seems the leave event after the button is pressed doesn't register and starting the idle timer here doesn't work well either -Dan
            self.fullscreen_button.show()
        else:
            if not self.detach_button.isChecked():
                self.setGeometry(self.geometryHint(self.parent_widget))  # force a geometry change before re-parenting, else we will get a change from (-1, -1) to the parent geometry hint
                self.setParent(self.parent_widget)                       # this is probably because since it unmaps when it's re-parented, the geometry change won't appear from fullscreen
                self.setGeometry(self.geometryHint())                    # to the new size, since we changed the geometry after returning from fullscreen, while invisible
                self.mute_button.active = False
                self.hold_button.active = False
                self.close_button.active = False
            self.detach_button.active = True
            self.showNormal()
            self.window().show()
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _SH_DetachButtonClicked(self, checked):
        if checked:
            if self.isFullScreen():
                self.showNormal()

            blink = QApplication.instance()
            main_screen = blink.screenAt(blink.main_window.pos())

            screen_area = main_screen.availableGeometry()

            start_rect = self.rect()
            final_rect = QRect(0, 0, self.width_for_height(261), 261)
            start_geometry = start_rect.translated(self.mapToGlobal(QPoint(0, 0)))
            final_geometry = final_rect.translated(screen_area.topRight() - final_rect.topRight() + QPoint(-10, 10))

            pixmap = self.grab()
            self.no_flicker_widget.resize(pixmap.size())
            self.no_flicker_widget.setPixmap(pixmap)
            self.no_flicker_widget.setGeometry(self.rect().translated(self.mapToGlobal(QPoint(0, 0))))
            self.no_flicker_widget.show()
            self.no_flicker_widget.raise_()

            self.setParent(None)
            self.setGeometry(start_geometry)
            self.show()
            self.no_flicker_widget.hide()

            self.detach_animation.setDirection(QPropertyAnimation.Direction.Forward)
            self.detach_animation.setEasingCurve(QEasingCurve.Type.OutQuad)
            self.detach_animation.setStartValue(start_geometry)
            self.detach_animation.setEndValue(final_geometry)
            self.detach_animation.start()
        else:
            start_geometry = self.geometry()
            final_geometry = self.geometryHint(self.parent_widget).translated(self.parent_widget.mapToGlobal(QPoint(0, 0)))

            # do this early or late? -Dan
            self.parent_widget.window().show()

            self.detach_animation.setDirection(QPropertyAnimation.Direction.Backward)
            self.detach_animation.setEasingCurve(QEasingCurve.Type.InQuad)
            self.detach_animation.setStartValue(final_geometry)  # start and end are reversed because we go backwards
            self.detach_animation.setEndValue(start_geometry)
            self.detach_animation.start()
        self.fullscreen_button.setChecked(False)

    def _SH_ScreenshotButtonClicked(self):
        screenshot = VideoScreenshot(self)
        screenshot.capture()
        screenshot.save()

    def _SH_MuteButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.audio.muted = checked
        settings.save()

    def _SH_HoldButtonClicked(self, checked):
        if checked:
            self.blink_session.hold()
        else:
            self.blink_session.unhold()

    def _SH_CloseButtonClicked(self):
        if 'screen-sharing' in self.blink_session.streams:
            self.blink_session.remove_stream(self.session_item.video_stream)
        else:
            self.session_item.end()

    def _SH_ScreenshotButtonContextMenuRequested(self, pos):
        if not self.isFullScreen():
            self.screenshot_button_menu.exec_(self.screenshot_button.mapToGlobal(pos))

    def _SH_ScreenshotsFolderActionTriggered(self):
        settings = BlinkSettings()
        QDesktopServices.openUrl(QUrl.fromLocalFile(settings.screenshots_directory.normalized))

    def _SH_DetachAnimationFinished(self):
        if self.detach_animation.direction() == QPropertyAnimation.Direction.Backward:
            pixmap = self.grab()
            self.no_flicker_widget.resize(pixmap.size())
            self.no_flicker_widget.setPixmap(pixmap)
            self.no_flicker_widget.setGeometry(self.geometry())
            self.no_flicker_widget.show()
            self.no_flicker_widget.raise_()
            # self.no_flicker_widget.repaint()
            # self.repaint()
            self.setParent(self.parent_widget)
            self.setGeometry(self.geometryHint())
            self.show()  # solve the flicker -Dan
            # self.repaint()
            # self.no_flicker_widget.lower()
            self.no_flicker_widget.hide()
            # self.window().show()
            self.mute_button.active = False
            self.hold_button.active = False
            self.close_button.active = False
        else:
            self.detach_button.hide()  # it seems the leave event after the button is pressed doesn't register and starting the idle timer here doesn't work well either -Dan
            self.detach_button.show()
            self.mute_button.active = True
            self.hold_button.active = True
            self.close_button.active = True
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _SH_PreviewAnimationFinished(self):
        self.camera_preview.setMaximumHeight(135)
        self.camera_preview.interactive = True
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.detach_button.active = True
        self.fullscreen_button.active = True
        self.screenshot_button.active = True
        self.idle_timer.start()


del ui_class, base_class


class NoSessionsLabel(QLabel):
    def __init__(self, chat_window):
        super(NoSessionsLabel, self).__init__(chat_window.session_panel)
        self.chat_window = chat_window
        font = self.font()
        font.setPointSize(20)
        self.setFont(font)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.setStyleSheet("""QLabel { border: 1px inset palette(dark); border-radius: 3px; background-color: white; color: #545454; }""")
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

    __streamtypes__ = {'chat', 'screen-sharing', 'video'}  # the stream types for which we show the chat window

    def __init__(self, parent=None):
        super(ChatWindow, self).__init__(parent)
        with Resources.directory:
            self.setupUi()

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
        self.session_model.sessionRemoved.connect(self._SH_SessionModelSessionRemoved)
        self.session_model.sessionAboutToBeRemoved.connect(self._SH_SessionModelSessionAboutToBeRemoved)
        self.session_list.selectionModel().selectionChanged.connect(self._SH_SessionListSelectionChanged)
        self.otr_widget.nameChanged.connect(self._SH_OTRWidgetNameChanged)
        self.otr_widget.statusChanged.connect(self._SH_OTRWidgetStatusChanged)
        self.zrtp_widget.nameChanged.connect(self._SH_ZRTPWidgetNameChanged)
        self.zrtp_widget.statusChanged.connect(self._SH_ZRTPWidgetStatusChanged)

        self.identity.activated[int].connect(self._SH_IdentityChanged)
        self.identity.currentIndexChanged[int].connect(self._SH_IdentityCurrentIndexChanged)

        geometry = QSettings().value("chat_window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        splitter_state = QSettings().value("chat_window/splitter")
        if splitter_state:
            self.splitter.restoreState(splitter_state)

        self.pending_displayed_notifications = {}
        self.render_after_load = deque()
        self.fetch_after_load = deque()
        self.last_desktop_notify = None
        self.last_incoming_message_alert = None

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
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
        notification_center.add_observer(self, name='MessageStreamPGPKeysDidLoad')
        notification_center.add_observer(self, name='PGPMessageDidDecrypt')
        notification_center.add_observer(self, name='PGPFileDidNotDecrypt')
        notification_center.add_observer(self, name='BlinkFileTransferDidEnd')
        notification_center.add_observer(self, name='BlinkMessageHistoryMustReload')

        self.account_model = AccountModel(self)
        self.enabled_account_model = ActiveAccountModel(self.account_model, self)
        self.identity.setModel(self.enabled_account_model)

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

        self.pixmaps.direct_connection = QPixmap(Resources.get('icons/connection-direct.svg'))
        self.pixmaps.relay_connection = QPixmap(Resources.get('icons/connection-relay.svg'))
        self.pixmaps.unknown_connection = QPixmap(Resources.get('icons/connection-unknown.svg'))

        self.pixmaps.blue_lock = QPixmap(Resources.get('icons/lock-blue-12.svg'))
        self.pixmaps.grey_lock = QPixmap(Resources.get('icons/lock-grey-12.svg'))
        self.pixmaps.green_lock = QPixmap(Resources.get('icons/lock-green-12.svg'))
        self.pixmaps.orange_lock = QPixmap(Resources.get('icons/lock-orange-12.svg'))

        def blended_pixmap(pixmap, color):
            blended_pixmap = QPixmap(pixmap)
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

        self.control_menu = QMenu(self.control_button)
        self.control_button.setMenu(self.control_menu)
        self.control_button.actions = ContextMenuActions()
        self.control_button.actions.connect_with_msrp = QAction(translate('chat_window', "Start MSRP chat"), self, triggered=self._AH_Connect)
        self.control_button.actions.connect_with_audio = QAction(translate('chat_window', "Start audio call"), self, triggered=self._AH_ConnectWithAudio)
        self.control_button.actions.mark_messages_read = QAction(translate('chat_window', "Mark as read"), self, triggered=self._AH_MarkMessagesRead)
        self.control_button.actions.connect_with_video = QAction(translate('chat_window', "Start video call"), self, triggered=self._AH_ConnectWithVideo)
        self.control_button.actions.disconnect = QAction(translate('chat_window', "Disconnect"), self, triggered=self._AH_Disconnect)
        self.control_button.actions.add_audio = QAction(translate('chat_window', "Add audio"), self, triggered=self._AH_AddAudio)
        self.control_button.actions.remove_audio = QAction(translate('chat_window', "Remove audio"), self, triggered=self._AH_RemoveAudio)
        self.control_button.actions.add_video = QAction(translate('chat_window', "Add video"), self, triggered=self._AH_AddVideo)
        self.control_button.actions.remove_video = QAction(translate('chat_window', "Remove video"), self, triggered=self._AH_RemoveVideo)
        self.control_button.actions.add_chat = QAction(translate('chat_window', "Add real time chat"), self, triggered=self._AH_AddChat)
        self.control_button.actions.remove_chat = QAction(translate('chat_window', "Remove real time chat"), self, triggered=self._AH_RemoveChat)
        self.control_button.actions.send_files = QAction(translate("chat_window", "Send File(s)..."), self, triggered=self._AH_SendFiles)
        self.control_button.actions.share_my_screen = QAction(translate('chat_window', "Share my screen"), self, triggered=self._AH_ShareMyScreen)
        self.control_button.actions.request_screen = QAction(translate('chat_window', "Request screen"), self, triggered=self._AH_RequestScreen)
        self.control_button.actions.end_screen_sharing = QAction(translate('chat_window', "End screen sharing"), self, triggered=self._AH_EndScreenSharing)
        self.control_button.actions.enable_otr = QAction(translate('chat_window', "Enable OTR for messaging"), self, triggered=self._AH_EnableOTR)
        self.control_button.actions.enable_otr_progress = QAction(translate('chat_window', "Enabling OTR for messaging"), self, enabled=False)
        self.control_button.actions.disable_otr = QAction(translate('chat_window', "Disable OTR for messaging"), self, triggered=self._AH_DisableOTR)
        self.control_button.actions.main_window = QAction(translate('chat_window', "Main Window"), self, triggered=self._AH_MainWindow, shortcut='Ctrl+B', shortcutContext=Qt.ShortcutContext.ApplicationShortcut)
        self.control_button.actions.show_transferred_files = QAction(translate('chat_window', "Show transferred files"), self, triggered=self._AH_ShowTransferredFiles)

        self.addAction(self.control_button.actions.main_window)  # make this active even when it's not in the control_button's menu

        self.slide_direction = self.session_details.RightToLeft  # decide if we slide from one direction only -Dan
        self.slide_direction = self.session_details.Automatic
        self.session_details.animationDuration = 300
        self.session_details.animationEasingCurve = QEasingCurve.Type.OutCirc

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
                if ('audio' not in new_session.blink_session.streams and 'video' not in new_session.blink_session.streams):
                    self._SH_FilesButtonClicked(True)
                self._update_session_info_panel(elements={'session', 'media', 'statistics', 'status'}, update_visibility=True)
                # TODO: somehow the placeholder does not get changed inside ChatInputText class -adi
                if new_session.blink_session.info.streams.messages and new_session.blink_session.info.streams.messages.encryption:
                    self.chat_input.setPlaceholderText(translate('chat_window', 'Write a message. Messages will be encrypted using %s' % new_session.blink_session.info.streams.messages.encryption))
                else:
                    self.chat_input.setPlaceholderText(translate('chat_window', 'Write a message'))


    selected_session = property(_get_selected_session, _set_selected_session)
    del _get_selected_session, _set_selected_session

    def _update_widgets_for_session(self):
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
        # toolbar buttons
        self.hold_button.setVisible('audio' in session.blink_session.streams)
        self.hold_button.setChecked(session.blink_session.local_hold)
        self.record_button.setVisible('audio' in session.blink_session.streams)
        self.record_button.setChecked(session.blink_session.recording)

    def _update_control_menu(self):
        menu = self.control_menu
        menu.hide()
        if not self.selected_session:
            return

        blink_session = self.selected_session.blink_session
        state = blink_session.state
        messages_info = blink_session.info.streams.messages
        if state == 'connecting/*' and blink_session.direction == 'outgoing' or state == 'connected/sent_proposal':
            self.control_button.setMenu(None)
            self.control_button.setIcon(self.cancel_icon)
        elif state == 'connected/received_proposal':
            self.control_button.setEnabled(False)
        else:
            self.control_button.setEnabled(True)
            self.control_button.setIcon(self.control_icon)
            menu.clear()
            menu.addAction(self.control_button.actions.mark_messages_read)
            menu.addSeparator()
            menu.addAction(self.control_button.actions.send_files)
            menu.addAction(self.control_button.actions.show_transferred_files)
            if state not in ('connecting/*', 'connected/*'):
                if messages_info.encryption != 'OTR':
                    if self.selected_session.chat_widget.otr_timer.isActive():
                        menu.addAction(self.control_button.actions.enable_otr_progress)
                    else:
                        if messages_info.encryption != 'OpenPGP':
                            menu.addAction(self.control_button.actions.enable_otr)
                else:
                    if messages_info.encryption != 'OpenPGP':
                        menu.addAction(self.control_button.actions.disable_otr)
                menu.addAction(self.control_button.actions.connect_with_audio)
                menu.addAction(self.control_button.actions.connect_with_video)
                menu.addSeparator()
                menu.addAction(self.control_button.actions.connect_with_msrp)
            else:
                menu.addAction(self.control_button.actions.disconnect)
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
                        menu.addAction(self.control_button.actions.add_video)
                    elif stream_types != {'video'}:
                        menu.addAction(self.control_button.actions.remove_video)
                    if 'screen-sharing' not in stream_types:
                        menu.addAction(self.control_button.actions.request_screen)
                        menu.addAction(self.control_button.actions.share_my_screen)
                    elif stream_types != {'screen-sharing'}:
                        menu.addAction(self.control_button.actions.end_screen_sharing)
                    menu.addSeparator()
                    if 'chat' not in stream_types:
                        menu.addAction(self.control_button.actions.add_chat)
                    elif stream_types != {'chat'}:
                        menu.addAction(self.control_button.actions.remove_chat)
            self.control_button.setMenu(menu)

    def _update_panel_buttons(self):
        self.info_panel_participants_button.setVisible(self.selected_session.blink_session.remote_focus)
        if self.selected_session.blink_session.remote_focus:
            self.info_panel_files_button.setVisible(len(self.selected_session.blink_session.server_conference.shared_files) == 0)
            self.participants_panel_files_button.setVisible(len(self.selected_session.blink_session.server_conference.shared_files) == 0)
        else:
            self.info_panel_files_button.setVisible(True)
        self.files_panel_participants_button.setVisible(self.selected_session.blink_session.remote_focus)

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
        super(ChatWindow, self).show()
        self.raise_()
        self.activateWindow()
        self.showNormal()
        if not self.session_model.rowCount():
            history = HistoryManager()
            history.get_last_contacts()

    def show_session_info(self):
        self.show()
        self.session_list.hide()
        if self.selected_session.active_panel != self.info_panel:
            if self.sliding_panels:
                self.session_details.slideInWidget(self.info_panel, direction=self.slide_direction)
            else:
                self.session_details.setCurrentWidget(self.info_panel)
            self.selected_session.active_panel = self.info_panel

    def show_with_messages(self):
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
        QSettings().setValue("chat_window/geometry", self.saveGeometry())
        QSettings().setValue("chat_window/splitter", self.splitter.saveState())
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
        if blink_settings.chat_window.session_info.alternate_style:
            title_role = 'alt-title'
            value_role = 'alt-value'
        else:
            title_role = 'title'
            value_role = 'value'
        for label in (attr for name, attr in vars(self).items() if name.endswith('_title_label') and attr.property('role') is not None):
            label.setProperty('role', title_role)
        for label in (attr for name, attr in vars(self).items() if name.endswith('_value_label') or name.endswith('_value_widget') and attr.property('role') is not None):
            label.setProperty('role', value_role)
        self.info_panel_container_widget.setStyleSheet(self.info_panel_container_widget.styleSheet())
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

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        blink_settings = BlinkSettings()
        if notification.sender is settings:
            if 'audio.muted' in notification.data.modified:
                self.mute_button.setChecked(settings.audio.muted)
        elif notification.sender is blink_settings:
            if 'chat_window.session_info.alternate_style' in notification.data.modified:
                if blink_settings.chat_window.session_info.alternate_style:
                    title_role = 'alt-title'
                    value_role = 'alt-value'
                else:
                    title_role = 'title'
                    value_role = 'value'
                for label in (attr for name, attr in vars(self).items() if name.endswith('_title_label') and attr.property('role') is not None):
                    label.setProperty('role', title_role)
                for label in (attr for name, attr in vars(self).items() if name.endswith('_value_label') or name.endswith('_value_widget') and attr.property('role') is not None):
                    label.setProperty('role', value_role)
                self.info_panel_container_widget.setStyleSheet(self.info_panel_container_widget.styleSheet())
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

    def _NH_BlinkSessionIsSelected(self, notification):
        model = self.session_model
        position = model.sessions.index(notification.sender.items.chat)
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.SelectionFlag.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.ScrollHint.EnsureVisible)  # or PositionAtCenter
        self.show()

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        self._update_control_menu()
        self._update_session_info_panel(update_visibility=True)

    def _NH_BlinkSessionDidChangeState(self, notification):
        # even if we use this, we also need to listen for BlinkSessionDidRemoveStream as that transition doesn't change the state at all -Dan
        self._update_control_menu()
        self._update_panel_buttons()
        self._update_session_info_panel(elements={'status'}, update_visibility=True)

    def _NH_BlinkSessionDidEnd(self, notification):
        if self.selected_session.active_panel is not self.info_panel:
            if self.sliding_panels:
                self.session_details.slideInWidget(self.info_panel, direction=self.slide_direction)
            else:
                self.session_details.setCurrentWidget(self.info_panel)
            self.selected_session.active_panel = self.info_panel

    def _NH_BlinkSessionInfoUpdated(self, notification):
        self._update_session_info_panel(elements=notification.data.elements)

    def _NH_BlinkSessionWillAddParticipant(self, notification):
        if len(notification.sender.server_conference.participants) == 1 and self.selected_session.active_panel is not self.participants_panel:
            if self.sliding_panels:
                self.session_details.slideInWidget(self.participants_panel, direction=self.slide_direction)
            else:
                self.session_details.setCurrentWidget(self.participants_panel)
            self.selected_session.active_panel = self.participants_panel

    def _NH_ChatSessionItemDidChange(self, notification):
        self._update_widgets_for_session()

    def _parse_fthttp(self, blink_session, message, from_history=False, account=None):
        session = blink_session.items.chat

        if account is None:
            try:
                account = AccountManager().get_account(message.account_id)
            except AttributeError:
                account = blink_session.account

        try:
            document = FTHTTPDocument.parse(message.content)
        except ParserError as e:
            raise ParserError
            #log.warning('Failed to parse FT HTTP payload: %s' % str(e))

        for info in document:
            try:
                hash = info.hash.value
            except AttributeError:
                hash = None

            if from_history:
                id = message.message_id
            else:
                id = message.id

            file = File(info.file_name.value,
                        info.file_size.value,
                        blink_session.contact,
                        hash,
                        id,
                        info.data.until if info.data.until else None,
                        url=info.data.url,
                        type=info.content_type.value,
                        account=account,
                        protocol='sylk')

            file.name = HistoryManager().get_decrypted_filename(file)
            #log.info(f"History File {file.id} url {file.url} name {file.name} ")
            is_audio_message = AudioDescriptor(info.file_name.value)

            try:
                if info.data.until < datetime.now(timezone.utc):
                    # session.chat_widget.add_message(ChatStatus(translate('chat_window', 'File transfer is expired: %s') % os.path.basename(info.file_name.value)))
                    if not file.already_exists:
                        return None
            except AttributeError:
                pass

            if not info.content_type.value.startswith('image/') and not is_audio_message:
                icon = FileIcon(file.decrypted_filename)
                icon_data = base64.b64encode(icon.data).decode()
                content = '''<img src="data:{};base64,{}" class="scaled-to-fit" />'''.format(icon.type, icon_data)
                if from_history:
                    NotificationCenter().post_notification('BlinkSessionDidShareFile',
                                                           sender=blink_session,
                                                           data=NotificationData(file=file, direction=message.direction))
                return '''<a href='%s#%s' style='text-decoration: none !important'><div style="display: flex;">
                            <div>%s</div>
                                <div style="display: flex; align-items: center;">
                                    <div>
                                        <div style="padding-left: 4px; font-size: 14px; font-weight: 400;">%s</div>
                                        <div style="padding-left: 4px;">%s</div>
                                    </div>
                                </div>
                            </div></a>''' % (file.decrypted_filename,
                                             id,
                                             content,
                                             os.path.basename(file.decrypted_filename),
                                             FileSizeFormatter.format(file.size))

            if message.direction == 'outgoing':
                if is_audio_message:
                    text = translate('chat_window', 'You sent an audio message. Fetching and processing...')
                else:
                    text = translate('chat_window', 'You sent an image: %s. Fetching and processing...') % os.path.basename(file.decrypted_filename)
            else:
                if is_audio_message:
                    text = translate('chat_window', 'Sent you an audio message. Processing...')
                else:
                    text = translate('chat_window', 'Sent you an image: %s. Processing...') % os.path.basename(file.decrypted_filename)

            content = f'<img src={session.chat_widget.encrypted_icon.filename} class="inline-message-icon">{text}'

            if not file.already_exists:
                if file.encrypted and not blink_session.fake_streams.get('messages').can_decrypt and not blink_session.fake_streams.get('messages').can_decrypt_with_others:
                    content = translate('chat_window', "%s can't be decrypted. PGP is disabled") % os.path.basename(file.decrypted_filename)
                    return content

                if from_history or not session.chat_widget.history_loaded:
                    file.downloading = True
                    queue_item = (blink_session, file, message, info)
                    if queue_item not in self.fetch_after_load:
                        self.fetch_after_load.append(queue_item)
                    NotificationCenter().post_notification('BlinkSessionDidShareFile',
                                                           sender=blink_session,
                                                           data=NotificationData(file=file, direction=message.direction))
                    return content

                if hash and file.protocol == 'msrp':
                    SessionManager().get_file(blink_session.contact, blink_session.contact_uri, file.name, file.hash, file.id, account=file.account, conference_file=False)
                else:
                    SessionManager().get_file_from_url(blink_session, file)
                return content

            if is_audio_message:
                return f'''<div><audio controls style="height: 35px; width: 350px"><source src="{file.decrypted_filename}" type={file.type}></audio></div>'''

            file_descriptors  = [FileDescriptor(file.decrypted_filename)]
            image_descriptors = [descriptor for descriptor in file_descriptors if descriptor.thumbnail is not None]

            if not image_descriptors:
                session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Error: image can not be rendered: %s') % os.path.basename(file.decrypted_filename)))
                content = ''
            else:
                for image in image_descriptors:
                    image_data = base64.b64encode(image.thumbnail.data).decode()
                    content = '''<a href="{}" style='display: flex; border: 0 !important'><img src="data:{};base64,{}" class="scaled-to-fit" /></a>'''.format(image.fileurl, image.thumbnail.type, image_data)

            if from_history:
                NotificationCenter().post_notification('BlinkSessionDidShareFile',
                                                       sender=blink_session,
                                                       data=NotificationData(file=file, direction=message.direction))
            return content

    def _NH_BlinkGotMessage(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat
        if session is None:
            return

        if not self.isVisible():
            #self.show_with_messages()
            pass #show now or allow the user to bring up the window?

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
        if message.content_type.startswith('image/'):
            content = '''<img src="data:{};base64,{}" class="scaled-to-fit" />'''.format(message.content_type, message.content.rstrip())
        elif message.content_type.startswith('text/'):
            if MessageManager().check_encryption(message.content_type, message.content) == 'OpenPGP':
                content = f'<img src={session.chat_widget.encrypted_icon.filename} class="inline-message-icon">Encrypted Message'
                content = HtmlProcessor.autolink(content)
                encrypted = True
                session.chat_widget.pending_decryption.append((message))
            else:
                content = message.content
                content = HtmlProcessor.autolink(content if message.content_type == 'text/html' else QTextDocument(content).toHtml())
        elif message.content_type.lower() == FTHTTPDocument.content_type:
            try:
                content = self._parse_fthttp(blink_session, message, account=received_account)
            except ParserError:
                return
            if content is None:
                return
        else:
            return

        try:
            uri = '%s@%s' % (message.sender.uri.user.decode(), message.sender.uri.host.decode())
        except AttributeError:
            uri = '%s@%s' % (message.sender.uri.user, message.sender.uri.host)

        account_manager = AccountManager()
        if account_manager.has_account(uri):
            account = account_manager.get_account(uri)
            sender = ChatSender(message.sender.display_name or account.display_name, uri, session.chat_widget.user_icon.filename)
        else:
            account = None
            sender = ChatSender(message.sender.display_name or session.name, uri, session.icon.filename)

        if message.content_type.lower() == FTHTTPDocument.content_type:
            chat_message = ChatFile(content, sender, direction, id=message.id, timestamp=message.timestamp, history=history, account=received_account)
        else:
            chat_message = ChatMessage(content, sender, direction, id=message.id, timestamp=message.timestamp, history=history, account=received_account)

        if session.chat_widget.history_loaded:
            if message in session.chat_widget.pending_decryption and not encrypted:
                session.chat_widget.pending_decryption.remove(message)
                session.chat_widget.update_message_text(message.id, content)
            else:
                session.chat_widget.add_message(chat_message)
            session.chat_widget.update_message_encryption(message.id, message.is_secure)
            if received_account is not None and received_account.enabled and received_account != blink_session.account:
                blink_session.account = received_account
                NotificationCenter().post_notification('BlinkSessionMessageAccountChanged', sender=blink_session)
                NotificationCenter().post_notification('PGPKeysShouldReload', sender=blink_session)
        else:
            self.render_after_load.append((session, received_account, chat_message))

        if direction != 'outgoing' and message.disposition is not None and 'display' in message.disposition and not encrypted:
            if self.selected_session is session and not self.isMinimized() and self.isActiveWindow():
                MessageManager().send_imdn_message(blink_session, message.id, message.timestamp, 'displayed', received_account)
            else:
                self.pending_displayed_notifications.setdefault(blink_session, []).append((message.id, message.timestamp, received_account))

        if direction != 'outgoing':
            if self.selected_session is session and not self.isMinimized() and self.isActiveWindow():
                pass
            else:
                self.desktop_notify(uri)

        session.remote_composing = False
        settings = SIPSimpleSettings()
        must_play = not settings.audio.silent

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

        if status == 'failed':
            # session.chat_widget.add_message(ChatStatus(translate('chat_window', f'Delivery failed: {notification.data.data.code} - {reason}')))
            pass
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
            content = HtmlProcessor.autolink(content if message.content_type == 'text/html' else QTextDocument(content).toHtml())
            session.chat_widget.update_message_text(message.message_id, content)
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

        else:
            blink_session = notification.sender
            filename = notification.data.filename
            id = notification.data.id

        if notification.data.must_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(filename))

        if AudioDescriptor(filename):
            content = f'''<div><audio controls style="height: 35px; width: 350px" src="{filename}"></audio></div>'''
            blink_session.items.chat.chat_widget.update_message_text(id, content)
            return

        file_descriptors  = [FileDescriptor(filename)]
        image_descriptors = [descriptor for descriptor in file_descriptors if descriptor.thumbnail is not None]

        for image in image_descriptors:
            image_data = base64.b64encode(image.thumbnail.data).decode()
            content = '''<a href="{}"><img src="data:{};base64,{}" class="scaled-to-fit" /></a>'''.format(image.fileurl, image.thumbnail.type, image_data)
            blink_session.items.chat.chat_widget.update_message_text(id, content)

        # TODO if is an image that cannot be decrypted we must update the chat message placeholder

    def _NH_PGPFileDidNotDecrypt(self, notification):
        transfer_session = notification.sender

        blink_session = next(session.blink_session for session in self.session_model.sessions if session.blink_session.contact.settings is transfer_session.contact.settings)

        if blink_session is None:
            return

        session = blink_session.items.chat
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

        last_account = None
        for message in messages:
            encrypted = False
            state = message.state
            account = account_manager.get_account(message.account_id) if account_manager.has_account(message.account_id) else None

            # We don't load messages if the account is not present or not enabled.
            if account is None or not account.enabled:
                continue

            last_account = account

            if message.content_type.startswith('image/'):
                content = '''<img src="data:{};base64,{}" class="scaled-to-fit" />'''.format(message.content_type, message.content.rstrip())
            elif message.content_type.startswith('text/'):
                if MessageManager().check_encryption(message.content_type, message.content) == 'OpenPGP':
                    content = f'<img src="{QUrl.fromLocalFile(session.chat_widget.encrypted_icon.filename).toString()}" class="inline-message-icon">Encrypted Message'
                    content = HtmlProcessor.autolink(content)
                    encrypted = True
                    if message.decrypted != '2':
                        session.chat_widget.pending_decryption.append((message))
                        stream = blink_session.fake_streams.get('messages')
                        if stream and (stream.can_decrypt or stream.can_decrypt_with_others):
                            stream.decrypt(message)
                else:
                    content = message.content
                    content = HtmlProcessor.autolink(content if message.content_type == 'text/html' else QTextDocument(content).toHtml())
            elif message.content_type.lower() == FTHTTPDocument.content_type:
                try:
                    content = self._parse_fthttp(blink_session, message, from_history=True)
                except ParserError as e:
                    continue

                if not content:
                    timestamp = message.timestamp.replace(tzinfo=timezone.utc).astimezone(tzlocal())
                    continue
            elif message.content_type.lower() == 'application/blink-call-history':
                content_list = eval(message.content)

                media_types = {'audio': translate('chat_window', 'audio'),
                               'video': translate('chat_window', 'video'),
                               'file-transfer': translate('chat_window', 'file-transfer')}
                try:
                    media_type = media_types[content_list[2]]
                except KeyError:
                    media_type = media_types['audio']
                session_type = translate('chat_window', 'call') if media_type != 'file-transfer' else ''

                if message.state != 'failed':
                    content = '%s %s %s %s' % (message.direction.capitalize(), media_type, session_type, content_list[0])
                    content = f'<div style="color: #000000">{content}</div>'
                else:
                    content = translate('chat_window', '%s %s %s failed (%s)') % (message.direction.capitalize(), media_type, session_type, content_list[1])
                    content = f'<div style="color: #800000">{content}</div>'
            else:
                continue

            # message.sender = SIPURI.parse(f'sip:{message.remote_uri}')
            if message.direction == 'outgoing':
                uri = message.account_id
            else:
                uri = message.remote_uri

            timestamp = message.timestamp.replace(tzinfo=timezone.utc).astimezone(tzlocal())
            # print(f"t: {timestamp}")
            if account_manager.has_account(uri):
                found_account = account_manager.get_account(uri)
                sender = ChatSender(message.display_name or found_account.display_name, uri, session.chat_widget.user_icon.filename)
            else:
                sender = ChatSender(message.display_name or session.name, uri, session.icon.filename)
            if message.content_type.lower() == FTHTTPDocument.content_type:
                chat_message = ChatFile(content, sender, message.direction, id=message.message_id, timestamp=timestamp, history=True, account=account)
            elif message.content_type.lower() == 'application/blink-call-history':
                chat_message = ChatEvent(content, message.direction, id=message.message_id, timestamp=timestamp)
            else:
                chat_message = ChatMessage(content, sender, message.direction, id=message.message_id, timestamp=timestamp, history=True, account=account)

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

        while self.render_after_load:
            (found_session, received_account, message) = self.render_after_load.popleft()
            if received_account is not None and received_account != last_account and received_account != session.blink_session.account:
                last_account = received_account
            if found_session is session:
                session.chat_widget.add_message(message)
            else:
                self.render_after_load.append((found_session, received_account, message))

        if blink_session.direction == 'outgoing' and last_account is not None and last_account.enabled and last_account != blink_session.account:
            blink_session.account = last_account
            NotificationCenter().post_notification('BlinkSessionMessageAccountChanged', sender=blink_session)
            NotificationCenter().post_notification('PGPKeysShouldReload', sender=blink_session)

        while self.fetch_after_load:
            (blink_session, file, message, info) = self.fetch_after_load.popleft()
            if file.hash is not None and file.protocol == 'msrp':
                SessionManager().get_file(blink_session.contact, blink_session.contact_uri, file.original_name, file.hash, file.id, account=file.account, conference_file=False)
            else:
                SessionManager().get_file_from_url(blink_session, file)
        session.chat_widget._align_chat(True)
        session.chat_widget.show_loading_screen(False)

    def _NH_BlinkMessageHistoryLoadDidFail(self, notification):
        blink_session = notification.sender
        session = blink_session.items.chat
        # TODO Should we attempt to reload history if it fails? -- Tijmen
        session.chat_widget.history_loaded = True
        session.chat_widget.show_loading_screen(False)

    def _NH_BlinkMessageHistoryLastContactsDidSucceed(self, notification):
        contacts = notification.data.contacts
        message_manager = MessageManager()
        for display_name, contact in contacts[::-1]:
            # allow the user to select the 1st session
            message_manager.create_message_session(contact, display_name, selected=False)

    def _NH_BlinkMessageHistoryCallHistoryDidStore(self, notification):
        message = notification.data.message
        contact, contact_uri = URIUtils.find_contact(message.remote_uri, display_name=message.display_name)

        try:
            blink_session = next(session.blink_session for session in self.session_model.sessions if session.blink_session.contact.settings is contact.settings)
        except StopIteration:
            return

        account_manager = AccountManager()

        if not blink_session.items.chat.chat_widget.history_loaded:
            return

        account = account_manager.get_account(message.account_id) if account_manager.has_account(message.account_id) else None

        if account is None or not account.enabled:
            return

        if message.content_type.lower() == 'application/blink-call-history':
            content_list = eval(message.content)

            media_types = {'audio': translate('chat_window', 'audio'),
                           'video': translate('chat_window', 'video'),
                           'file-transfer': translate('chat_window', 'file-transfer')}
            try:
                media_type = media_types[content_list[2]]
            except KeyError:
                media_type = media_types['audio']
            session_type = translate('chat_window', 'call') if media_type != 'file-transfer' else ''

            if message.state != 'failed':
                content = '%s %s %s %s' % (message.direction.capitalize(), media_type, session_type, content_list[0])
                content = f'<div style="color: #000000">{content}</div>'
            else:
                content = translate('chat_window', '%s %s %s failed (%s)') % (message.direction.capitalize(), media_type, session_type, content_list[1])
                content = f'<div style="color: #800000">{content}</div>'

            timestamp = message.timestamp.replace(tzinfo=timezone.utc).astimezone(tzlocal())
            chat_message = ChatEvent(content, message.direction, id=message.message_id, timestamp=timestamp)
            blink_session.items.chat.chat_widget.add_message(chat_message)

    def _NH_ChatStreamGotMessage(self, notification):
        blink_session = notification.sender.blink_session
        session = blink_session.items.chat

        if session is None:
            return

        message = notification.data.message

        if message.content_type.startswith('image/'):
            content = '''<img src="data:{};base64,{}" class="scaled-to-fit" />'''.format(message.content_type, img_content.decode().rstrip())
        elif message.content_type.startswith('text/'):
            content = message.content
            content = HtmlProcessor.autolink(content if message.content_type == 'text/html' else QTextDocument(content).toHtml())
        else:
            return

        uri = '%s@%s' % (message.sender.uri.user.decode(), message.sender.uri.host.decode())
        account_manager = AccountManager()
        if account_manager.has_account(uri):
            account = account_manager.get_account(uri)
            sender = ChatSender(message.sender.display_name or account.display_name, uri, session.chat_widget.user_icon.filename)
        elif blink_session.remote_focus:
            contact, contact_uri = URIUtils.find_contact(uri)
            sender = ChatSender(message.sender.display_name or contact.name, uri, contact.icon.filename)
        else:
            sender = ChatSender(message.sender.display_name or session.name, uri, session.icon.filename)

        is_status_message = any(h.name == 'Message-Type' and h.value == 'status' and h.namespace == 'urn:ag-projects:xml:ns:cpim' for h in message.additional_headers)
        if is_status_message:
            session.chat_widget.add_message(ChatStatus(content))
        else:
            session.chat_widget.add_message(ChatMessage(content, sender, 'incoming'))

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
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.update_message_status(id=notification.data.message.message_id, status='delivered')
        # TODO: implement -Saul

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
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

    def _NH_MediaStreamDidStart(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.add_message(ChatStatus('Connected'))

    def _NH_MediaStreamDidEnd(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        if notification.data.error is not None:
            session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Disconnected: %s') % notification.data.error))
        else:
            session.chat_widget.add_message(ChatStatus(translate('chat_window', 'Disconnected')))
            # Set type back for message as the stream ended cleanly -- Tijmen
            notification.sender.blink_session.chat_type = None

    def _NH_MediaStreamWillEnd(self, notification):
        stream = notification.sender
        if stream.type == 'chat' and stream.blink_session.items.chat is self.selected_session:
            self.otr_widget.hide()
        if stream.type == self.zrtp_widget.stream_type and stream.blink_session.items.chat is self.selected_session:
            self.zrtp_widget.hide()
            self.zrtp_widget.stream_type = None

    # signal handlers
    #
    def _SH_InfoButtonClicked(self, checked):
        if self.sliding_panels:
            self.session_details.slideInWidget(self.info_panel, direction=self.slide_direction)
        else:
            self.session_details.setCurrentWidget(self.info_panel)
        self.selected_session.active_panel = self.info_panel

    def _SH_FilesButtonClicked(self, checked):
        if self.sliding_panels:
            self.session_details.slideInWidget(self.files_panel, direction=self.slide_direction)
        else:
            self.session_details.setCurrentWidget(self.files_panel)
        self.selected_session.active_panel = self.files_panel

    def _SH_ParticipantsButtonClicked(self, checked):
        if self.sliding_panels:
            self.session_details.slideInWidget(self.participants_panel, direction=self.slide_direction)
        else:
            self.session_details.setCurrentWidget(self.participants_panel)
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

    def _SH_IdentityChanged(self, index):
        account = self.identity.itemData(index).account
        try:
            self.selected_session.blink_session.account = account
            NotificationCenter().post_notification('PGPKeysShouldReload', sender=self.selected_session.blink_session)
        except (AttributeError, KeyError):
            pass

    def _SH_IdentityCurrentIndexChanged(self, index):
        if index != -1:
            try:
                self._update_session_info_panel(elements='session')
            except (AttributeError, KeyError):
                pass

    def _SH_SessionModelSessionAdded(self, session):
        model = self.session_model
        position = model.sessions.index(session)
        session.chat_widget = ChatWidget(session, self.tab_widget)
        session.video_widget = VideoWidget(session, session.chat_widget)
        session.active_panel = self.info_panel
        self.tab_widget.insertTab(position, session.chat_widget, session.name)
        self.no_sessions_label.hide()
        selection_model = self.session_list.selectionModel()
        # allow user select the contact
        #selection_model.select(model.index(position), selection_model.SelectionFlag.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.ScrollHint.EnsureVisible)  # or PositionAtCenter
        self.session_list.animation.setStartValue(self.session_widget.geometry())
        self.session_list.show()
        session.chat_widget.chat_input.setFocus(Qt.FocusReason.OtherFocusReason)
        history = HistoryManager()
        history.load(session.blink_session.contact.uri.uri, session.blink_session)

    def _NH_BlinkMessageHistoryMustReload(self, notification):
        history = HistoryManager()
        for session in self.session_model.sessions:
            print(f'Check if we must reload {session.blink_session}')
            if session.blink_session.account.id == notification.data.account:
                history.reload_pending_encrypted(session.blink_session.contact.uri.uri, session.blink_session)
   
    def _SH_SessionModelSessionRemoved(self, session):
        self.tab_widget.removeTab(self.tab_widget.indexOf(session.chat_widget))
        session.chat_widget = None
        session.video_widget = None
        session.active_panel = None
        if not self.session_model.sessions:
            self.close()
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
        self.selected_session = selected[0].topLeft().data(Qt.ItemDataRole.UserRole) if selected else None
        if self.selected_session is not None:
            self.tab_widget.setCurrentWidget(self.selected_session.chat_widget)  # why do we switch the tab here, but do everything else in the selected_session property setter? -Dan
            self.session_details.setCurrentWidget(self.selected_session.active_panel)
            self.participants_list.setModel(self.selected_session.participants_model)
            self.files_list.setModel(self.selected_session.files_model)
            self.control_button.setEnabled(True)
            if not self.isMinimized():
                self.confirm_read_messages(self.selected_session)
        else:
            self.tab_widget.setCurrentWidget(self.dummy_tab)
            self.session_details.setCurrentWidget(self.info_panel)
            self.participants_list.setModel(None)
            self.control_button.setEnabled(False)

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

    def _AH_Connect(self):
        blink_session = self.selected_session.blink_session
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=[StreamDescription('chat')], reinitialize=True)
        blink_session.connect()

    def _AH_ShowTransferredFiles(self):
        if not self.selected_session:
            return
        blink_session = self.selected_session.blink_session
        self.session_list.hide()
        self._SH_FilesButtonClicked(True)

    def _AH_ConnectWithAudio(self):
        stream_descriptions = [StreamDescription('audio')]
        blink_session = self.selected_session.blink_session
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
        stream_descriptions = [StreamDescription('audio'), StreamDescription('video')]
        blink_session = self.selected_session.blink_session
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=stream_descriptions, reinitialize=True)
        blink_session.connect()

    def _AH_Disconnect(self):
        self.selected_session.end()

    def _AH_AddAudio(self):
        self.selected_session.blink_session.add_stream(StreamDescription('audio'))

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
        contact = self.selected_session.blink_session.contact
        selected_uri = uri or contact.uri
        for filename in QFileDialog.getOpenFileNames(self, translate('chat_window', 'Select File(s)'), session_manager.send_file_directory, 'Any file (*.*)')[0]:
            session_manager.send_file(contact, selected_uri, filename)

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
            self.selected_session.end(delete=True)

    def _EH_ShowSessions(self):
        self.session_list.animation.setDirection(QPropertyAnimation.Direction.Forward)
        self.session_list.animation.setStartValue(self.session_widget.geometry())
        self.session_list.animation.setEndValue(self.session_panel.rect())
        self.session_list.scrollToTop()
        self.session_list.show()
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

class HtmlProcessor(object):
    _autolink_re = [re.compile(r"""
                                (?P<body>
                                  https?://(?:[^:@/]+(?::[^@]*)?@)?(?P<host>[a-z0-9.-]+)(?::\d*)?    # scheme :// [ user [ : password ] @ ] host [ : port ]
                                  (?:/(?:[\w/%!$@#*&='~:;,.+-]*(?:\([\w/%!$@#*&='~:;,.+-]*\))?)*)?   # [ / path]
                                  (?:\?(?:[\w/%!$@#*&='~:;,.+-]*(?:\([\w/%!$@#*&='~:;,.+-]*\))?)*)?  # [ ? query]
                                )
                                """, re.IGNORECASE | re.UNICODE | re.VERBOSE),
                    re.compile(r"""
                                (?P<body>
                                  ftps?://(?:[^:@/]+(?::[^@]*)?@)?(?P<host>[a-z0-9.-]+)(?::\d*)?                  # scheme :// [ user [ : password ] @ ] host [ : port ]
                                  (?:/(?:[\w/%!?$@*&='~:,.+-]*(?:\([\w/%!?$@*&='~:,.+-]*\))?)*(?:;type=[aid])?)?  # [ / path [ ;type=a/i/d ] ]
                                )
                                """, re.IGNORECASE | re.UNICODE | re.VERBOSE),
                    re.compile(r'mailto:(?P<body>[\w.-]+@(?P<host>[a-z0-9.-]+))', re.IGNORECASE | re.UNICODE)]

    @classmethod
    def autolink(cls, content):
        if isinstance(content, str):
            doc = html.fromstring(content)
            autolink(doc, link_regexes=cls._autolink_re)
            return html.tostring(doc, encoding='unicode')  # add method='xml' to get <br/> xhtml style tags and doctype=doc.getroottree().docinfo.doctype for prepending the DOCTYPE line
        else:
            autolink(content, link_regexes=cls._autolink_re)
            return content

    @classmethod
    def normalize(cls, content):
        return content


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


class VideoScreenshot(object):
    def __init__(self, surface):
        self.surface = surface
        self.image = None

    @classmethod
    def filename_generator(cls):
        settings = BlinkSettings()
        name = os.path.join(settings.screenshots_directory.normalized, 'VideoCall-{:%Y%m%d-%H.%M.%S}'.format(datetime.now()))
        yield '%s.png' % name
        for x in count(1):
            yield "%s-%d.png" % (name, x)

    def capture(self):
        try:
            self.image = self.surface._image.copy()
        except AttributeError:
            pass
        else:
            settings = SIPSimpleSettings()
            if not settings.audio.silent:
                player = WavePlayer(SIPApplication.alert_audio_bridge.mixer, Resources.get('sounds/screenshot.wav'), volume=30)
                SIPApplication.alert_audio_bridge.add(player)
                player.start()

    @run_in_thread('file-io')
    def save(self):
        if self.image is not None:
            filename = next(filename for filename in self.filename_generator() if not os.path.exists(filename))
            makedirs(os.path.dirname(filename))
            self.image.save(filename)
