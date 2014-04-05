# Copyright (C) 2013 AG Projects. See LICENSE for details.
#

__all__ = ['ChatWindow']

import os

from PyQt4 import uic
from PyQt4.QtCore import Qt, QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRect, QSettings, QTimer, pyqtSignal
from PyQt4.QtGui  import QAction, QBrush, QColor, QIcon, QLabel, QLinearGradient, QListView, QMenu, QPainter, QPalette, QPen, QPolygonF, QTextCursor, QTextDocument, QTextEdit
from PyQt4.QtGui  import QApplication, QDesktopServices
from PyQt4.QtWebKit import QWebPage, QWebSettings, QWebView

from abc import ABCMeta, abstractmethod
from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import MarkerType
from collections import MutableSet
from datetime import datetime, timedelta
from lxml import etree
from weakref import proxy
from zope.interface import implements

from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings

from blink.configuration.datatypes import FileURL
from blink.configuration.settings import BlinkSettings
from blink.contacts import URIUtils
from blink.resources import IconManager, Resources
from blink.sessions import ChatSessionModel, ChatSessionListView, StreamDescription
from blink.util import run_in_gui_thread
from blink.widgets.color import ColorHelperMixin
from blink.widgets.graph import Graph
from blink.widgets.util import ContextMenuActions


# Chat style classes
#

class ChatStyleError(Exception): pass


class ChatHtmlTemplates(object):
    def __init__(self, style_path):
        try:
            self.message = open(os.path.join(style_path, 'html/message.html')).read().decode('utf-8')
            self.message_continuation = open(os.path.join(style_path, 'html/message_continuation.html')).read().decode('utf-8')
            self.notification = open(os.path.join(style_path, 'html/notification.html')).read().decode('utf-8')
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
            self.variants = tuple(sorted(name[:-len('.style')] for name in os.listdir(self.path) if name.endswith('.style')))
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
        self.__hardroot = Link() # sentinel node for doubly linked list
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

    def __get__(self, obj, objtype):
        if obj is None:
            return self
        return self.name in obj.__cssclasses__

    def __set__(self, obj, value):
        if value:
            obj.__cssclasses__.add(self.name)
        else:
            obj.__cssclasses__.discard(self.name)

    def __delete__(self, obj):
        raise AttributeError('attribute cannot be deleted')


class AnyValue: __metaclass__ = MarkerType

class ChatContentStringAttribute(object):
    """A string attribute that is also added as a css class"""

    def __init__(self, name, allowed_values=AnyValue):
        self.name = name
        self.allowed_values = allowed_values

    def __get__(self, obj, objtype):
        if obj is None:
            return self
        try:
            return obj.__dict__[self.name]
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


class ChatContent(object):
    __metaclass__ = ABCMeta

    __cssclasses__ = ()

    continuation_interval = timedelta(0, 5*60) # 5 minutes

    history = ChatContentBooleanOption('history')
    focus = ChatContentBooleanOption('focus')
    consecutive = ChatContentBooleanOption('consecutive')
    mention = ChatContentBooleanOption('mention') # keep it here? or keep it at all? -Dan

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
        return self.timestamp.strftime('%d %b %Y')

    @property
    def time(self):
        return self.timestamp.strftime('%H:%M')

    @property
    def text_direction(self):
        try:
            return self.__dict__['text_direction']
        except KeyError:
            document = QTextDocument()
            document.setHtml(self.message)
            return self.__dict__.setdefault('text_direction', 'rtl' if document.firstBlock().textDirection() == Qt.RightToLeft else 'ltr')

    def add_css_class(self, name):
        self.__cssclasses__.add(name)

    def is_related_to(self, other):
        return type(self) is type(other) and self.history == other.history and timedelta(0) <= self.timestamp - other.timestamp <= self.continuation_interval

    @abstractmethod
    def to_html(self, style, **kw):
        raise NotImplementedError


class ChatNotification(ChatContent):
    __cssclasses__ = ('event',)

    def to_html(self, style, **kw):
        return style.html.notification.format(message=self, **kw)


class ChatEvent(ChatNotification):
    __cssclasses__ = ('event',)


class ChatStatus(ChatNotification):
    __cssclasses__ = ('status',)


class ChatMessage(ChatContent):
    __cssclasses__ = ('message',)

    direction = ChatContentStringAttribute('direction', allowed_values=('incoming', 'outgoing'))
    autoreply = ChatContentBooleanOption('autoreply')

    def __init__(self, message, sender, direction, history=False, focus=False):
        super(ChatMessage, self).__init__(message, history, focus)
        self.sender = sender
        self.direction = direction

    def is_related_to(self, other):
        return super(ChatMessage, self).is_related_to(other) and self.sender == other.sender and self.direction == other.direction

    def to_html(self, style, **kw):
        if self.consecutive:
            return style.html.message_continuation.format(message=self, **kw)
        else:
            return style.html.message.format(message=self, **kw)


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
        self.iconpath = iconpath

    def __eq__(self, other):
        if not isinstance(other, ChatSender):
            return NotImplemented
        return self.name == other.name and self.uri == other.uri

    def __ne__(self, other):
        return not (self == other)

    @property
    def color(self):
        return self.__colors__[hash(self.uri) % len(self.__colors__)]


class ChatWebPage(QWebPage):
    def __init__(self, parent=None):
        super(ChatWebPage, self).__init__(parent)
        self.setLinkDelegationPolicy(QWebPage.DelegateAllLinks)
        self.linkClicked.connect(self._SH_LinkClicked)
        #self.downloadRequested.connect(self._SH_DownloadRequested)
        #self.setForwardUnsupportedContent(True)
        #self.unsupportedContent.connect(self._SH_UnsupportedContent)
        #allowed_actions = {QWebPage.InspectElement, QWebPage.CopyLinkToClipboard, QWebPage.CopyImageToClipboard, QWebPage.CopyImageUrlToClipboard}
        disable_actions = {QWebPage.OpenLink, QWebPage.OpenLinkInNewWindow, QWebPage.DownloadLinkToDisk, QWebPage.OpenImageInNewWindow, QWebPage.DownloadImageToDisk,
                           QWebPage.Back, QWebPage.Forward, QWebPage.Stop, QWebPage.Reload}
        for action in (self.action(action) for action in disable_actions):
            action.setVisible(False)

    def acceptNavigationRequest(self, frame, request, navigation_type):
        if navigation_type in (QWebPage.NavigationTypeBackOrForward, QWebPage.NavigationTypeReload):
            return False
        return super(ChatWebPage, self).acceptNavigationRequest(frame, request, navigation_type)

    def triggerAction(self, action, checked=False):
        if action == QWebPage.OpenLink:
            return
        super(ChatWebPage, self).triggerAction(action, checked)

    def _SH_LinkClicked(self, url):
        QDesktopServices.openUrl(url)

    #def _SH_DownloadRequested(self, request):
    #    print "-- download requested", request.url().toString()

    #def _SH_UnsupportedContent(self, reply):
    #    print "-- unsupported", reply.url().toString()


class ChatWebView(QWebView):
    sizeChanged = pyqtSignal()

    def __init__(self, parent=None):
        super(ChatWebView, self).__init__(parent)
        palette = self.palette()
        palette.setBrush(QPalette.Base, Qt.transparent)
        self.setPalette(palette)
        self.setPage(ChatWebPage(self))
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.settings().setAttribute(QWebSettings.DeveloperExtrasEnabled, True) # temporary for debugging -Dan

    def setChatFont(self, family, size):
        settings = self.settings()
        settings.setFontFamily(QWebSettings.StandardFont, family)
        settings.setFontFamily(QWebSettings.FixedFont, family)
        settings.setFontFamily(QWebSettings.SerifFont, family)
        settings.setFontFamily(QWebSettings.SansSerifFont, family)
        settings.setFontSize(QWebSettings.DefaultFontSize, size)
        settings.setFontSize(QWebSettings.DefaultFixedFontSize, size)
        self.update()

    def contextMenuEvent(self, event):
        menu = self.page().createStandardContextMenu()
        if any(action.isVisible() and not action.isSeparator() for action in menu.actions()):
            menu.exec_(event.globalPos())

    def createWindow(self, window_type):
        print "create window of type", window_type
        return None

    def resizeEvent(self, event):
        super(ChatWebView, self).resizeEvent(event)
        self.sizeChanged.emit()


class ChatTextInput(QTextEdit):
    textEntered = pyqtSignal(unicode)

    def __init__(self, parent=None):
        super(ChatTextInput, self).__init__(parent)
        self.setTabStopWidth(22)
        self.document().documentLayout().documentSizeChanged.connect(self._SH_DocumentLayoutSizeChanged)
        self.history = []
        self.history_index = 0 # negative indexes with 0 indicating the text being typed.
        self.stashed_content = None

    @property
    def empty(self):
        document = self.document()
        last_block = document.lastBlock()
        return document.characterCount() <= 1 and not last_block.textList()

    def keyPressEvent(self, event):
        key, modifiers = event.key(), event.modifiers()
        if key in (Qt.Key_Enter, Qt.Key_Return) and modifiers == Qt.NoModifier:
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
                    cursor.movePosition(cursor.End)
                    cursor.deletePreviousChar()
                text = self.toHtml()
                self.clear()
                self.textEntered.emit(text)
            event.accept()
        elif key == Qt.Key_Up and modifiers == Qt.ControlModifier:
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
        elif key == Qt.Key_Down and modifiers == Qt.ControlModifier:
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
        self.setFixedHeight(min(new_size.height()+self.contentsMargins().top()+self.contentsMargins().bottom(), self.parent().height()/2))

    def setHtml(self, text):
        super(ChatTextInput, self).setHtml(text)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)


class IconDescriptor(object):
    def __init__(self, filename):
        self.filename = filename
        self.icon = None
    def __get__(self, obj, objtype):
        if self.icon is None:
            self.icon = QIcon(self.filename)
            self.icon.filename = self.filename
        return self.icon
    def __set__(self, obj, value):
        raise AttributeError("attribute cannot be set")
    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


ui_class, base_class = uic.loadUiType(Resources.get('chat_widget.ui'))

class ChatWidget(base_class, ui_class):
    default_user_icon = IconDescriptor(Resources.get('icons/default-avatar.png'))

    def __init__(self, session, parent=None):
        super(ChatWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.session = session
        self.style = ChatMessageStyle('Stockholm')
        self.style_variant = self.style.default_variant
        #self.style_variant = 'Blue - Red'
        #self.style = ChatMessageStyle('Smooth Operator')
        #self.style_variant = self.style.default_variant
        #self.style_variant = 'Classic'
        #self.style_variant = 'Time-Icon'
        chat_template = open(Resources.get('chat/template.html')).read()
        self.chat_view.setChatFont(self.style.font_family, self.style.font_size)
        self.chat_view.setHtml(chat_template.format(base_url=FileURL(self.style.path)+'/', style_url=self.style_variant+'.style'))
        self.chat_element = self.chat_view.page().mainFrame().findFirstElement('#chat')
        self.composing_timer = QTimer()
        self.last_message = None
        # connect to signals
        self.chat_input.textChanged.connect(self._SH_ChatInputTextChanged)
        self.chat_input.textEntered.connect(self._SH_ChatInputTextEntered)
        self.chat_view.sizeChanged.connect(self._SH_ChatViewSizeChanged)
        self.chat_view.page().mainFrame().contentsSizeChanged.connect(self._SH_ChatViewFrameContentsSizeChanged)
        self.composing_timer.timeout.connect(self._SH_ComposingTimerTimeout)

    def add_message(self, message):
        insertion_point = self.chat_element.findFirst('#insert')
        if message.is_related_to(self.last_message):
            message.consecutive = True
            insertion_point.replace(message.to_html(self.style, user_icons='show-icons'))
        else:
            insertion_point.removeFromDocument()
            self.chat_element.appendInside(message.to_html(self.style, user_icons='show-icons'))
        self.last_message = message

    def _align_chat(self, scroll=False):
        #frame_height = self.chat_view.page().mainFrame().contentsSize().height()
        widget_height = self.chat_view.size().height()
        content_height = self.chat_element.geometry().height()
        #print widget_height, frame_height, content_height
        if widget_height > content_height:
            self.chat_element.setStyleProperty('position', 'relative')
            self.chat_element.setStyleProperty('top', '%dpx' % (widget_height-content_height))
        else:
            self.chat_element.setStyleProperty('position', 'static')
            self.chat_element.setStyleProperty('top', None)
        frame = self.chat_view.page().mainFrame()
        if scroll or frame.scrollBarMaximum(Qt.Vertical) - frame.scrollBarValue(Qt.Vertical) <= widget_height*0.2:
            #print "scroll requested or scrollbar is closer than %dpx to the bottom" % (widget_height*0.2)
            #self._print_scrollbar_position()
            self._scroll_to_bottom()
            #self._print_scrollbar_position()

    def _scroll_to_bottom(self):
        frame = self.chat_view.page().mainFrame()
        frame.setScrollBarValue(Qt.Vertical, frame.scrollBarMaximum(Qt.Vertical))

    def _print_scrollbar_position(self):
        frame = self.chat_view.page().mainFrame()
        print "%d out of %d, %d+%d=%d (%d)" % (frame.scrollBarValue(Qt.Vertical), frame.scrollBarMaximum(Qt.Vertical), frame.scrollBarValue(Qt.Vertical), self.chat_view.size().height(),
                                               frame.scrollBarValue(Qt.Vertical)+self.chat_view.size().height(), frame.contentsSize().height())

    def _SH_ChatViewSizeChanged(self):
        #print "chat view size changed"
        self._align_chat(scroll=True)

    def _SH_ChatViewFrameContentsSizeChanged(self, size):
        #print "frame contents size changed to %r (current=%r)" % (size, self.chat_view.page().mainFrame().contentsSize())
        self._align_chat(scroll=True)

    def _SH_ChatInputTextChanged(self):
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
        #doc = QTextDocument()
        #doc.setHtml(text)
        #plain_text = doc.toPlainText()
        #if len(plain_text) == 7 and plain_text[0] == '#':
        #    body = self.chat_view.page().mainFrame().findFirstElement('body')
        #    body.setStyleProperty('background', plain_text)
        #    return
        self.composing_timer.stop()
        blink_session = self.session.blink_session
        if blink_session.state == 'initialized':
            blink_session.connect() # what if it was initialized, but is doesn't have a chat stream? -Dan
        elif blink_session.state == 'ended':
            blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, [StreamDescription('chat')], reinitialize=True)
            blink_session.connect()
        elif blink_session.state == 'connected/*':
            if self.session.chat_stream is None:
                self.session.blink_session.add_stream(StreamDescription('chat'))
                if self.session.chat_stream is None:
                    self.add_message(ChatStatus('Could not add chat stream'))
                    return
        else: # cannot send chat message in any other state (what about when connecting -Dan)
            self.add_message(ChatStatus("Cannot send chat messages in the '%s' state" % blink_session.state))
            return

        chat_stream = self.session.chat_stream
        try:
            chat_stream.send_message(text, content_type='text/html')
        except Exception, e:
            self.add_message(ChatStatus('Error sending chat message: %s' % e)) # decide what type to use here. -Dan
            return
        # TODO: cache this
        identity = chat_stream.local_identity
        if identity is not None:
            display_name = identity.display_name
            uri = '%s@%s' % (identity.uri.user, identity.uri.host)
        else:
            account = chat_stream.blink_session.account
            display_name = account.display_name
            uri = account.id
        icon = IconManager().get('avatar') or self.default_user_icon
        sender = ChatSender(display_name, uri, icon.filename)
        self.add_message(ChatMessage(text, sender, 'outgoing'))

    def _SH_ComposingTimerTimeout(self):
        self.composing_timer.stop()
        chat_stream = self.session.chat_stream or Null
        try:
            chat_stream.send_composing_indication('idle')
        except Exception:
            pass

del ui_class, base_class


class NoSessionsLabel(QLabel):
    def __init__(self, chat_window):
        super(NoSessionsLabel, self).__init__(chat_window.session_panel)
        self.chat_window = chat_window
        font = self.font()
        font.setFamily("Sans Serif")
        font.setPointSize(20)
        self.setFont(font)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("""QLabel { border: 1px inset palette(dark); border-radius: 3px; background-color: white; color: #545454; }""")
        self.setText("No Sessions")
        chat_window.session_panel.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            self.resize(event.size())
        return False


ui_class, base_class = uic.loadUiType(Resources.get('chat_window.ui'))

class ChatWindow(base_class, ui_class, ColorHelperMixin):
    implements(IObserver)

    sliding_panels = True

    def __init__(self, parent=None):
        super(ChatWindow, self).__init__(parent)
        with Resources.directory:
            self.setupUi()

        self.selected_item = None
        self.session_model = ChatSessionModel(self)
        self.session_list.setModel(self.session_model)
        self.session_widget.installEventFilter(self)
        self.state_label.installEventFilter(self)

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

        geometry = QSettings().value("chat_window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='BlinkSessionNewIncoming')
        notification_center.add_observer(self, name='BlinkSessionNewOutgoing')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForIncoming')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForOutgoing')
        notification_center.add_observer(self, name='ChatStreamGotMessage')
        notification_center.add_observer(self, name='ChatStreamGotComposingIndication')
        notification_center.add_observer(self, name='ChatStreamDidSendMessage')
        notification_center.add_observer(self, name='ChatStreamDidDeliverMessage')
        notification_center.add_observer(self, name='ChatStreamDidNotDeliverMessage')
        notification_center.add_observer(self, name='MediaStreamDidInitialize')
        notification_center.add_observer(self, name='MediaStreamDidStart')
        notification_center.add_observer(self, name='MediaStreamDidFail')
        notification_center.add_observer(self, name='MediaStreamDidEnd')

        #self.splitter.splitterMoved.connect(self._SH_SplitterMoved) # check this and decide on what size to have in the window (see Notes) -Dan

    def _SH_SplitterMoved(self, pos, index):
        print "-- splitter:", pos, index, self.splitter.sizes()

    def setupUi(self):
        super(ChatWindow, self).setupUi(self)

        self.control_icon = QIcon(Resources.get('icons/cog.svg'))
        self.cancel_icon = QIcon(Resources.get('icons/cancel.png'))
        self.lock_grey_icon = QIcon(Resources.get('icons/lock-grey-12.svg'))
        self.lock_green_icon = QIcon(Resources.get('icons/lock-green-12.svg'))

        # re-apply the stylesheet for self.session_info_container_widget to account for all its subwidget role properties that were set after it
        self.info_panel_container_widget.setStyleSheet(self.info_panel_container_widget.styleSheet())

        # fix the SVG icons as the generated code loads them as pixmaps, losing their ability to scale -Dan
        def svg_icon(filename_off, filename_on):
            icon = QIcon()
            icon.addFile(filename_off, mode=QIcon.Normal, state=QIcon.Off)
            icon.addFile(filename_on,  mode=QIcon.Normal, state=QIcon.On)
            icon.addFile(filename_on,  mode=QIcon.Active, state=QIcon.On)
            return icon
        self.mute_button.setIcon(svg_icon(Resources.get('icons/mic-on.svg'), Resources.get('icons/mic-off.svg')))
        self.hold_button.setIcon(svg_icon(Resources.get('icons/pause.svg'), Resources.get('icons/paused.svg')))
        self.record_button.setIcon(svg_icon(Resources.get('icons/record.svg'), Resources.get('icons/recording.svg')))
        self.control_button.setIcon(self.control_icon)

        self.control_menu = QMenu(self.control_button)
        self.control_button.setMenu(self.control_menu)
        self.control_button.actions = ContextMenuActions()
        self.control_button.actions.connect = QAction("Connect", self, triggered=self._AH_Connect)
        self.control_button.actions.connect_with_audio = QAction("Connect with audio", self, triggered=self._AH_ConnectWithAudio)
        self.control_button.actions.disconnect = QAction("Disconnect", self, triggered=self._AH_Disconnect)
        self.control_button.actions.add_audio = QAction("Add audio", self, triggered=self._AH_AddAudio)
        self.control_button.actions.remove_audio = QAction("Remove audio", self, triggered=self._AH_RemoveAudio)
        self.control_button.actions.dump_session = QAction("Dump session", self, triggered=self._AH_DumpSession) # remove later -Dan
        self.control_button.actions.main_window = QAction("Main Window", self, triggered=self._AH_MainWindow, shortcut='Ctrl+B', shortcutContext=Qt.ApplicationShortcut)

        self.addAction(self.control_button.actions.main_window) # make this active even when it's not in the contol_button's menu

        self.session_list = ChatSessionListView(self)
        self.session_list.setObjectName('session_list')

        self.no_sessions_label = NoSessionsLabel(self)
        self.no_sessions_label.setObjectName('no_sessions_label')

        self.slide_direction = self.session_details.RightToLeft # decide if we slide from one direction only -Dan
        self.slide_direction = self.session_details.Automatic
        self.session_details.animationDuration = 300
        self.session_details.animationEasingCurve = QEasingCurve.OutCirc

        self.audio_latency_graph = Graph([], color=QColor(0, 100, 215), over_boundary_color=QColor(255, 0, 100))
        self.video_latency_graph = Graph([], color=QColor(0, 215, 100), over_boundary_color=QColor(255, 100, 0))
        self.audio_packet_loss_graph = Graph([], color=QColor(0, 100, 215), over_boundary_color=QColor(255, 0, 100))
        self.video_packet_loss_graph = Graph([], color=QColor(0, 215, 100), over_boundary_color=QColor(255, 100, 0))

        self.incoming_traffic_graph = Graph([], color=QColor(255, 50, 50))
        self.outgoing_traffic_graph = Graph([], color=QColor(0, 100, 215))

        self.latency_graph.add_graph(self.audio_latency_graph)
        self.latency_graph.add_graph(self.video_latency_graph)
        self.packet_loss_graph.add_graph(self.audio_packet_loss_graph)
        self.packet_loss_graph.add_graph(self.video_packet_loss_graph)

        # the graph added 2nd will be displayed on top
        self.traffic_graph.add_graph(self.incoming_traffic_graph)
        self.traffic_graph.add_graph(self.outgoing_traffic_graph)

        self.info_panel_files_button.hide()
        self.info_panel_participants_button.hide()
        self.participants_panel_files_button.hide()

        while self.tab_widget.count():
            self.tab_widget.removeTab(0) # remove the tab(s) added in designer
        self.tab_widget.tabBar().hide()
        self.dummy_tab = ChatWidget(None, self.tab_widget)
        self.dummy_tab.setDisabled(True)
        self.tab_widget.addTab(self.dummy_tab, "Dummy")
        self.tab_widget.setCurrentWidget(self.dummy_tab)

        self.session_list.hide()
        self.new_messages_button.hide()
        self.hold_button.hide()
        self.record_button.hide()
        self.control_button.setEnabled(False)

        self.info_label.setForegroundRole(QPalette.Dark)

        # prepare self.session_widget so we can take over some of its painting and behaviour
        self.session_widget.setAttribute(Qt.WA_Hover, True)
        self.session_widget.hovered = False

    def _get_selected_session(self):
        return self.__dict__['selected_session']

    def _set_selected_session(self, session):
        old_session = self.__dict__.get('selected_session', None)
        new_session = self.__dict__['selected_session'] = session
        if new_session != old_session:
            notification_center = NotificationCenter()
            if old_session is not None:
                notification_center.remove_observer(self, sender=old_session)
                notification_center.remove_observer(self, sender=old_session.blink_session)
            if new_session is not None:
                notification_center.add_observer(self, sender=new_session)
                notification_center.add_observer(self, sender=new_session.blink_session)
                self._update_widgets_for_session() # clean this up -Dan (too many functions called in 3 different places: on selection changed, here and on notifications handlers)
                self._update_control_menu()
                self._update_panel_buttons()
                self._update_session_info_panel(elements={'session', 'media', 'statistics', 'status'}, update_visibility=True)

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
        blink_session = self.selected_session.blink_session
        state = blink_session.state
        if state=='connecting/*' and blink_session.direction=='outgoing' or state=='connected/sent_proposal':
            self.control_button.setMenu(None)
            self.control_button.setIcon(self.cancel_icon)
        elif state == 'connected/received_proposal':
            self.control_button.setEnabled(False)
        else:
            self.control_button.setEnabled(True)
            self.control_button.setIcon(self.control_icon)
            menu.clear()
            if state not in ('connecting/*', 'connected/*'):
                menu.addAction(self.control_button.actions.connect)
                menu.addAction(self.control_button.actions.connect_with_audio)
            else:
                menu.addAction(self.control_button.actions.disconnect)
                if state == 'connected':
                    menu.addAction(self.control_button.actions.add_audio if 'audio' not in blink_session.streams else self.control_button.actions.remove_audio)
            #menu.addAction(self.control_button.actions.dump_session) # remove this later -Dan
            self.control_button.setMenu(menu)

    def _update_panel_buttons(self):
        self.info_panel_participants_button.setVisible(self.selected_session.blink_session.remote_focus)
        self.files_panel_participants_button.setVisible(self.selected_session.blink_session.remote_focus)

    def _update_session_info_panel(self, elements={}, update_visibility=False):
        blink_session = self.selected_session.blink_session
        have_session = blink_session.state in ('connecting/*', 'connected/*', 'ending')
        have_audio = 'audio' in blink_session.streams
        have_chat = 'chat' in blink_session.streams

        if update_visibility:
            self.status_value_label.setEnabled(have_session)
            self.duration_value_label.setEnabled(have_session)
            self.account_value_label.setEnabled(have_session)
            self.remote_agent_value_label.setEnabled(have_session)
            self.sip_addresses_value_label.setEnabled(have_session)
            self.audio_value_widget.setEnabled(have_audio)
            self.audio_addresses_value_label.setEnabled(have_audio)
            self.audio_ice_status_value_label.setEnabled(have_audio)
            self.chat_value_widget.setEnabled(have_chat)
            self.chat_addresses_value_label.setEnabled(have_chat)

        session_info = blink_session.info
        audio_info = blink_session.info.streams.audio
        video_info = blink_session.info.streams.video
        chat_info = blink_session.info.streams.chat

        if 'status' in elements and blink_session.state in ('initialized', 'connecting/*', 'connected/*', 'ended'):
            state_map = {'initialized': 'Disconnected',
                         'connecting/dns_lookup': 'Finding destination',
                         'connecting': 'Connecting',
                         'connecting/ringing': 'Ringing',
                         'connecting/starting': 'Starting media',
                         'connected': 'Connected'}

            if blink_session.state == 'ended':
                self.status_value_label.setForegroundRole(QPalette.AlternateBase if blink_session.state.error else QPalette.WindowText)
                self.status_value_label.setText(blink_session.state.reason)
            elif blink_session.state in state_map:
                self.status_value_label.setForegroundRole(QPalette.WindowText)
                self.status_value_label.setText(state_map[blink_session.state])

            want_duration = blink_session.state == 'connected/*' or blink_session.state == 'ended' and not blink_session.state.error
            self.status_title_label.setVisible(not want_duration)
            self.status_value_label.setVisible(not want_duration)
            self.duration_title_label.setVisible(want_duration)
            self.duration_value_label.setVisible(want_duration)

        if 'session' in elements:
            self.account_value_label.setText(blink_session.account.id)
            self.remote_agent_value_label.setText(session_info.remote_user_agent or u'N/A')
            if session_info.local_address and session_info.remote_address:
                self.sip_addresses_value_label.setText(u'%s \u21c4 %s:%s' % (session_info.local_address, session_info.transport, session_info.remote_address))
            elif session_info.local_address:
                self.sip_addresses_value_label.setText(u'%s \u21c4 N/A' % session_info.local_address)
            else:
                self.sip_addresses_value_label.setText(u'N/A')

        if 'media' in elements:
            self.audio_value_label.setText(audio_info.codec or 'N/A')
            self.audio_encryption_label.setVisible(audio_info.encryption is not None)

            if audio_info.local_address and audio_info.remote_address:
                self.audio_addresses_value_label.setText(u'%s \u21c4 %s' % (audio_info.local_address, audio_info.remote_address))
            else:
                self.audio_addresses_value_label.setText(u'N/A')

            if audio_info.ice_status == None:
                self.audio_ice_status_value_label.setText(u'N/A')
            elif audio_info.ice_status == 'disabled':
                self.audio_ice_status_value_label.setText(u'Disabled')
            elif audio_info.ice_status == 'gathering':
                self.audio_ice_status_value_label.setText(u'Gathering candidates')
            elif audio_info.ice_status == 'gathering_complete':
                self.audio_ice_status_value_label.setText(u'Gathered candidates')
            elif audio_info.ice_status == 'negotiating':
                self.audio_ice_status_value_label.setText(u'Negotiating')
            elif audio_info.ice_status == 'succeeded':
                if 'relay' in {candidate.type.lower() for candidate in (audio_info.local_rtp_candidate, audio_info.remote_rtp_candidate)}:
                    self.audio_ice_status_value_label.setText(u'Using relay')
                else:
                    self.audio_ice_status_value_label.setText(u'Peer to peer')
            elif audio_info.ice_status == 'failed':
                self.audio_ice_status_value_label.setText(u"Couldn't negotiate ICE")

            if any(len(path) > 1 for path in (chat_info.full_local_path, chat_info.full_remote_path)):
                self.chat_value_label.setText(u'Using relay')
            elif chat_info.full_local_path and chat_info.full_remote_path:
                self.chat_value_label.setText(u'Peer to peer')
            else:
                self.chat_value_label.setText(u'N/A')
            self.chat_encryption_label.setVisible(chat_info.remote_address is not None and chat_info.transport=='tls')

            if chat_info.local_address and chat_info.remote_address:
                self.chat_addresses_value_label.setText(u'%s \u21c4 %s:%s' % (chat_info.local_address, chat_info.transport, chat_info.remote_address))
            else:
                self.chat_addresses_value_label.setText(u'N/A')

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

    def show(self):
        super(ChatWindow, self).show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        QSettings().setValue("chat_window/geometry", self.saveGeometry())
        super(ChatWindow, self).closeEvent(event)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if watched is self.session_widget:
            if event_type == QEvent.HoverEnter:
                watched.hovered = True
            elif event_type == QEvent.HoverLeave:
                watched.hovered = False
            elif event_type == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                self._EH_ShowSessions()
        elif watched is self.state_label:
            if event_type == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier:
                upper_half = QRect(0, 0, self.state_label.width(), self.state_label.height()/2)
                if upper_half.contains(event.pos()):
                    self._EH_CloseSession()
                else:
                    self._EH_ShowSessions()
            elif event_type == QEvent.Paint: # and self.session_widget.hovered:
                watched.event(event)
                self.drawSessionWidgetIndicators()
                return True
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
            gradient.setCoordinateMode(QLinearGradient.ObjectBoundingMode)
            gradient.setColorAt(0.0, self.color_with_alpha(base_contrast_color, 0.3*255))
            gradient.setColorAt(1.0, self.color_with_alpha(base_contrast_color, 0.8*255))
            contrast_color = QBrush(gradient)
        else:
            background_color = palette.color(QPalette.Window)
            contrast_color = self.calc_light_color(background_color)
        foreground_color = palette.color(QPalette.Normal, QPalette.WindowText)
        line_color = self.deco_color(background_color, foreground_color)

        pen = QPen(line_color, pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        contrast_pen = QPen(contrast_color, pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)

        # draw the expansion indicator at the bottom (works best with a state_label of width 14)
        arrow_rect = QRect(0, 0, 14, 14)
        arrow_rect.moveBottomRight(rect.bottomRight())

        arrow = QPolygonF([QPointF(-3, -1.5), QPointF(0.5, 2.5), QPointF(4, -1.5)])
        arrow.translate(1, 1)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
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
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.translate(cross_rect.center())
        painter.translate(+1.5, +1)
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawLine(-3.5, -3.5, 3.5, 3.5)
        painter.drawLine(-3.5, 3.5, 3.5, -3.5)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawLine(-3.5, -3.5, 3.5, 3.5)
        painter.drawLine(-3.5, 3.5, 3.5, -3.5)
        painter.restore()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification): # this should not run in the gui thread -Dan
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange')

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        blink_settings = BlinkSettings()
        if notification.sender is settings:
            if 'audio.muted' in notification.data.modified:
                self.mute_button.setChecked(settings.audio.muted)
        elif notification.sender is blink_settings:
            if 'presence.icon' in notification.data.modified:
                QWebSettings.clearMemoryCaches()

    def _NH_BlinkSessionNewIncoming(self, notification):
        if 'chat' in notification.sender.streams.types:
            self.show()

    def _NH_BlinkSessionNewOutgoing(self, notification):
        if 'chat' in notification.sender.stream_descriptions.types:
            self.show()

    def _NH_BlinkSessionDidReinitializeForIncoming(self, notification):
        model = self.session_model
        position = model.sessions.index(notification.sender.items.chat)
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.EnsureVisible) # or PositionAtCenter
        if 'chat' in notification.sender.streams.types:
            self.show()

    def _NH_BlinkSessionDidReinitializeForOutgoing(self, notification):
        model = self.session_model
        position = model.sessions.index(notification.sender.items.chat)
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.EnsureVisible) # or PositionAtCenter
        if 'chat' in notification.sender.stream_descriptions.types:
            self.show()

    # use BlinkSessionNewIncoming/Outgoing to show the chat window if there is a chat stream available (like with reinitialize) instead of using the sessionAdded signal from the model -Dan
    # or maybe not. sessionAdded means it was added to the model, while during NewIncoming/Outgoing we do not know that yet. but then we have a problem with the DidReinitialize since
    # they do not check if the session is in the model. maybe the right approach is to always have BlinkSessions in the model and if we need any other kind of sessions we create a
    # different class for them that posts different notifications. in that case we can do in in NewIncoming/Outgoing -Dan

    def _NH_BlinkSessionWillAddStream(self, notification):
        if notification.data.stream.type == 'chat':
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

    def _NH_ChatStreamGotMessage(self, notification):
        blink_session = notification.sender.blink_session
        session = blink_session.items.chat
        if session is None:
            return
        message = notification.data.message
        if not message.content_type.startswith('text/'):
            # TODO: check with OSX version what special messages we could get -Saul
            return
        if message.body.startswith('?OTRv2?'):
            # TODO: add support for OTR -Saul
            return
        uri = '%s@%s' % (message.sender.uri.user, message.sender.uri.host)
        account_manager = AccountManager()
        if account_manager.has_account(uri):
            account = account_manager.get_account(uri)
            icon = IconManager().get('avatar') or session.chat_widget.default_user_icon
            sender = ChatSender(message.sender.display_name or account.display_name, uri, icon.filename)
        elif blink_session.remote_focus:
            contact, contact_uri = URIUtils.find_contact(uri)
            sender = ChatSender(message.sender.display_name or contact.name, uri, contact.icon.filename)
        else:
            sender = ChatSender(message.sender.display_name or session.name, uri, session.icon.filename)
        content = message.body if message.content_type=='text/html' else QTextDocument(message.body).toHtml()
        session.chat_widget.add_message(ChatMessage(content, sender, 'incoming'))
        session.remote_composing = False

    def _NH_ChatStreamGotComposingIndication(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.update_composing_indication(notification.data)

    def _NH_ChatStreamDidSendMessage(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        # TODO: do we want to use this? Play the message sent tone? -Saul

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        # TODO: implement -Saul

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        # TODO: implement -Saul

    def _NH_MediaStreamDidInitialize(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        notification.sender._blink_fail_reason = None
        #session.chat_widget.add_message(ChatStatus('Connecting...')) # disable it until we can replace it in the DOM -Dan

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
        stream = notification.sender
        if stream._blink_fail_reason:
            session.chat_widget.add_message(ChatStatus('Disconnected: %s' % stream._blink_fail_reason))
        else:
            session.chat_widget.add_message(ChatStatus('Disconnected'))

    def _NH_MediaStreamDidFail(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        notification.sender._blink_fail_reason = notification.data.reason

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
        self.latency_label.setText(u'Network Latency: %dms, max=%dms' % (max(self.audio_latency_graph.last_value, self.video_latency_graph.last_value), self.latency_graph.max_value))

    def _SH_PacketLossGraphUpdated(self):
        self.packet_loss_label.setText(u'Packet Loss: %.1f%%, max=%.1f%%' % (max(self.audio_packet_loss_graph.last_value, self.video_packet_loss_graph.last_value), self.packet_loss_graph.max_value))

    def _SH_TrafficGraphUpdated(self):
        #incoming_traffic = TrafficNormalizer.normalize(self.incoming_traffic_graph.last_value)
        #outgoing_traffic = TrafficNormalizer.normalize(self.outgoing_traffic_graph.last_value)
        incoming_traffic = TrafficNormalizer.normalize(self.incoming_traffic_graph.last_value*8, bits_per_second=True)
        outgoing_traffic = TrafficNormalizer.normalize(self.outgoing_traffic_graph.last_value*8, bits_per_second=True)
        self.traffic_label.setText(u"""<p>Traffic: <span style="color: #d70000;">\u2193</span> %s <span style="color: #0064d7;">\u2191</span> %s</p>""" % (incoming_traffic, outgoing_traffic))

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

    def _SH_ControlButtonClicked(self, checked):
        # this is only called if the control button doesn't have a menu attached
        if self.selected_session.blink_session.state == 'connected/sent_proposal':
            self.selected_session.blink_session.sip_session.cancel_proposal()
        else:
            self.selected_session.end()

    def _SH_SessionModelSessionAdded(self, session):
        model = self.session_model
        position = model.sessions.index(session)
        session.chat_widget = ChatWidget(session, self.tab_widget)
        session.active_panel = self.info_panel
        self.tab_widget.insertTab(position, session.chat_widget, session.name)
        self.no_sessions_label.hide()
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.EnsureVisible) # or PositionAtCenter
        session.chat_widget.chat_input.setFocus(Qt.OtherFocusReason)

    def _SH_SessionModelSessionRemoved(self, session):
        self.tab_widget.removeTab(self.tab_widget.indexOf(session.chat_widget))
        session.chat_widget = None
        session.active_panel = None
        if not self.session_model.sessions:
            self.close()
            self.no_sessions_label.show()
        elif not self.session_list.isVisibleTo(self):
            self.session_list.animation.setDirection(QPropertyAnimation.Forward)
            self.session_list.animation.setStartValue(self.session_widget.geometry())
            self.session_list.animation.setEndValue(self.session_panel.rect())
            self.session_list.show()
            self.session_list.animation.start()

    def _SH_SessionModelSessionAboutToBeRemoved(self, session):
        # choose another one to select (a chat only or ended session if available, else one with audio but keep audio on hold? or select nothing and display the dummy tab?)
        #selection_model = self.session_list.selectionModel()
        #selection_model.clearSelection()
        pass

    def _SH_SessionListSelectionChanged(self, selected, deselected):
        #print "-- chat selection changed %s -> %s" % ([x.row() for x in deselected.indexes()], [x.row() for x in selected.indexes()])
        self.selected_session = selected[0].topLeft().data(Qt.UserRole) if selected else None
        if self.selected_session is not None:
            self.tab_widget.setCurrentWidget(self.selected_session.chat_widget)  # why do we switch the tab here, but do everything else in the selected_session property setter? -Dan
            self.session_details.setCurrentWidget(self.selected_session.active_panel)
            self.participants_list.setModel(self.selected_session.participants_model)
            self.control_button.setEnabled(True)
        else:
            self.tab_widget.setCurrentWidget(self.dummy_tab)
            self.session_details.setCurrentWidget(self.info_panel)
            self.participants_list.setModel(None)
            self.control_button.setEnabled(False)

    def _AH_Connect(self):
        blink_session = self.selected_session.blink_session
        if blink_session.state == 'ended':
            blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=[StreamDescription('chat')], reinitialize=True)
        blink_session.connect()

    def _AH_ConnectWithAudio(self):
        stream_descriptions = [StreamDescription('audio'), StreamDescription('chat')]
        blink_session = self.selected_session.blink_session
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=stream_descriptions, reinitialize=True)
        blink_session.connect()

    def _AH_Disconnect(self):
        self.selected_session.end()

    def _AH_AddAudio(self):
        self.selected_session.blink_session.add_stream(StreamDescription('audio'))

    def _AH_RemoveAudio(self):
        self.selected_session.blink_session.remove_stream(self.selected_session.blink_session.streams.get('audio'))

    def _AH_DumpSession(self):
        blink_session = self.selected_session.blink_session
        print "state:   %r" % blink_session.state
        print "streams: %r" % [stream for stream in blink_session.streams]
        print "hold:    %r/%r" % (blink_session.local_hold, blink_session.remote_hold)
        print "conf:    %r" % blink_session.client_conference
        print "active:  %r" % blink_session.active

    def _AH_MainWindow(self):
        blink = QApplication.instance()
        blink.main_window.show()

    def _EH_CloseSession(self):
        if self.selected_session is not None:
            self.selected_session.end(delete=True)

    def _EH_ShowSessions(self):
        self.session_list.animation.setDirection(QPropertyAnimation.Forward)
        self.session_list.animation.setStartValue(self.session_widget.geometry())
        self.session_list.animation.setEndValue(self.session_panel.rect())
        self.session_list.scrollToTop()
        self.session_list.show()
        self.session_list.animation.start()

del ui_class, base_class


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


