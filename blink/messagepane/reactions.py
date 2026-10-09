"""Reactions: a one-tap emoji answer to a message, as on Sylk Mobile.

A reaction is a reply whose body is the emoji: the reply link (a
sylk-message-metadata companion) goes first, then the emoji as text/plain.
Nothing on the wire marks it as a reaction; whoever receives it sees a
reply quoting the message, and a pure-emoji reply is passed over for the
contact's last-message line (conversation_preview).

A message's menu starts with ReactionStrip: the six most used emoji and a
"+" that opens EmojiPicker when the mouse rests on it (or it is clicked), the full set in Sylk Mobile's categories with the
quick reactions first. Live location shares, notes, files still being sent
and messages that failed are not reacted to.
"""

import unicodedata

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QGuiApplication, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QScrollArea, QStackedWidget, QToolButton, QVBoxLayout,
                             QWidget, QWidgetAction)

from blink.logging import ActivityLog
from blink.message_envelopes import LOCATION_CONTENT_TYPE
from blink.messagepane.format import bubble_kind, delivery_mark
from blink.util import translate


__all__ = ['REACTIONS', 'CATEGORIES', 'reactable', 'emoji_font', 'color_emoji', 'ReactionStrip', 'reaction_action', 'EmojiPicker']


# Sylk Mobile's quick-reaction set, most used first
REACTIONS = (
    '❤️', '👍', '😂', '😮', '😢', '🙏',
    '🔥', '👏', '😍', '😎', '🤔', '😴',
    '🥳', '🤯', '💯', '✅', '❌', '🙌',
    '🤝', '👀', '😅', '🤣', '💪', '🎉',
)

# the strip in the menu shows the first few; the rest are in the picker
STRIP_SIZE = 6

# Sylk Mobile's EmojiPicker categories: (key, tab label, emoji)
CATEGORIES = (
    ('smileys', '😀', (
        '😀', '😃', '😄', '😁', '😆', '😅', '🤣', '😂', '🙂', '🙃',
        '😉', '😊', '😇', '🥰', '😍', '🤩', '😘', '😗', '😚', '😙',
        '😋', '😛', '😜', '🤪', '😝', '🤑', '🤗', '🤭', '🤫', '🤔',
        '🤐', '🤨', '😐', '😑', '😶', '😏', '😒', '🙄', '😬', '🤥',
        '😌', '😔', '😪', '🤤', '😴', '😷', '🤒', '🤕', '🤢', '🤮',
        '🥵', '🥶', '🥴', '😵', '🤯', '🤠', '🥳', '😎', '🤓', '🧐',
        '😕', '😟', '🙁', '☹️', '😮', '😯', '😲', '😳', '🥺', '😦',
        '😧', '😨', '😰', '😥', '😢', '😭', '😱', '😖', '😣', '😞',
        '😓', '😩', '😫', '🥱', '😤', '😡', '😠', '🤬', '😈', '👿',
        '💀', '☠️', '💩', '🤡', '👹', '👺', '👻', '👽', '👾', '🤖',
    )),
    ('gestures', '👍', (
        '👋', '🤚', '🖐️', '✋', '🖖', '👌', '🤌', '🤏', '✌️', '🤞',
        '🤟', '🤘', '🤙', '👈', '👉', '👆', '🖕', '👇', '☝️', '👍',
        '👎', '✊', '👊', '🤛', '🤜', '👏', '🙌', '👐', '🤲', '🤝',
        '🙏', '✍️', '💅', '🤳', '💪', '🦾', '🦵', '🦿', '🦶', '👂',
        '🦻', '👃', '🧠', '🦷', '🦴', '👀', '👁️', '👅', '👄', '💋',
    )),
    ('hearts', '❤️', (
        '❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍', '🤎', '💔',
        '❣️', '💕', '💞', '💓', '💗', '💖', '💘', '💝', '💟', '♥️',
        '💯', '💢', '💥', '💫', '💦', '💨', '🕳️', '💣', '💬', '👁️\u200d🗨️',
        '🗨️', '🗯️', '💭', '💤', '✨', '🌟', '⭐', '🌠', '☀️', '🌈',
    )),
    ('animals', '🐶', (
        '🐶', '🐱', '🐭', '🐹', '🐰', '🦊', '🐻', '🐼', '🐨', '🐯',
        '🦁', '🐮', '🐷', '🐽', '🐸', '🐵', '🙈', '🙉', '🙊', '🐒',
        '🐔', '🐧', '🐦', '🐤', '🐣', '🐥', '🦆', '🦅', '🦉', '🦇',
        '🐺', '🐗', '🐴', '🦄', '🐝', '🐛', '🦋', '🐌', '🐞', '🐜',
        '🪰', '🪱', '🦗', '🕷️', '🦂', '🐢', '🐍', '🦎', '🦖', '🦕',
        '🐙', '🦑', '🦐', '🦞', '🦀', '🐡', '🐠', '🐟', '🐬', '🐳',
        '🐋', '🦈', '🐊', '🐅', '🐆', '🦓', '🦍', '🦧', '🐘', '🦛',
        '🦏', '🐪', '🐫', '🦒', '🦘', '🐃', '🐂', '🐄', '🐎', '🐖',
    )),
    ('food', '🍔', (
        '🍏', '🍎', '🍐', '🍊', '🍋', '🍌', '🍉', '🍇', '🍓', '🫐',
        '🍈', '🍒', '🍑', '🥭', '🍍', '🥥', '🥝', '🍅', '🍆', '🥑',
        '🥦', '🥬', '🥒', '🌶️', '🫑', '🌽', '🥕', '🫒', '🧄', '🧅',
        '🥔', '🍠', '🥐', '🥯', '🍞', '🥖', '🥨', '🧀', '🥚', '🍳',
        '🧈', '🥞', '🧇', '🥓', '🥩', '🍗', '🍖', '🦴', '🌭', '🍔',
        '🍟', '🍕', '🥪', '🥙', '🧆', '🌮', '🌯', '🥗', '🥘', '🫕',
        '🥫', '🍝', '🍜', '🍲', '🍛', '🍣', '🍱', '🥟', '🦪', '🍤',
        '🍚', '🍘', '🍥', '🥠', '🥮', '🍢', '🍡', '🍧', '🍨', '🍦',
        '🥧', '🧁', '🍰', '🎂', '🍮', '🍭', '🍬', '🍫', '🍿', '🍩',
        '🍪', '☕', '🍵', '🧃', '🥤', '🧋', '🍶', '🍺', '🍻', '🥂',
    )),
    ('symbols', '✅', (
        '✅', '❌', '❎', '⭕', '🚫', '⛔', '📛', '🔞', '♻️', '✳️',
        '❇️', '✴️', '❄️', '❣️', '♨️', '🆎', '🆑', '🆒', '🆓', '🆔',
        '🆕', '🆖', '🆗', '🆘', '🆙', '🆚', '🅰️', '🅱️', '🅾️', '🅿️',
        '🔴', '🟠', '🟡', '🟢', '🔵', '🟣', '⚫', '⚪', '🟤', '🔺',
        '🔻', '🔸', '🔹', '🔶', '🔷', '🔳', '🔲', '▪️', '▫️', '◾',
        '◽', '◼️', '◻️', '⬛', '⬜', '🟥', '🟧', '🟨', '🟩', '🟦',
        '🟪', '🟫', '♠️', '♥️', '♦️', '♣️', '🃏', '🎴', '🀄', '🎭',
    )),
)


# colour emoji fonts, tried before the application font so emoji are not drawn from a monochrome symbol font
EMOJI_FAMILIES = ('Noto Color Emoji', 'Twemoji', 'JoyPixels', 'Apple Color Emoji', 'Segoe UI Emoji')


def emoji_font(point_size):
    font = QFont(QGuiApplication.font())
    font.setFamilies(list(EMOJI_FAMILIES) + [QGuiApplication.font().family()])
    font.setPointSizeF(point_size)
    return font


def reactable(item):
    """Whether a message takes a reaction: as on mobile, everything that can be replied to except
    a location share (its body keeps changing), a file still being sent and a message that failed."""
    if item is None or getattr(item, 'upload', None) is not None:
        return False
    if bubble_kind(item) == 'note':
        return False
    if str(item.content_type or '') == LOCATION_CONTENT_TYPE:
        return False
    return delivery_mark(item)[1] != 'failed'


def _emoji_button(emoji, point_size, parent):
    button = QToolButton(parent)
    button.setText(emoji)
    button.setFont(emoji_font(point_size))
    button.setAutoRaise(True)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(translate('message_pane', 'React with %s') % emoji)
    return button


class ReactionStrip(QWidget):
    """A row of quick reactions and a "+" for the rest, for the top of a message's menu."""

    chosen = pyqtSignal(str)
    more = pyqtSignal(QPoint)       # where on the screen the picker goes: under the "+"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(0)
        # each button as wide as the widest glyph plus a little: the style's tool button margins
        # and the menu's width would otherwise leave wide gaps between the emoji
        metrics = QFontMetrics(emoji_font(15))
        glyph = max(max(metrics.horizontalAdvance(emoji) for emoji in REACTIONS[:STRIP_SIZE]), metrics.height())
        side = glyph + 6
        self._buttons = []          # (button, emoji)
        for emoji in REACTIONS[:STRIP_SIZE]:
            button = _emoji_button(emoji, 15, self)
            button.setStyleSheet('QToolButton { padding: 0px; margin: 0px; }')
            button.setFixedSize(side, side)
            button.clicked.connect(lambda checked=False, emoji=emoji: self._chose(emoji))
            layout.addWidget(button)
            self._buttons.append((button, emoji))
        plus = _HoverButton(self)
        plus.setFixedSize(side, side)
        plus.setText('+')
        font = plus.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        plus.setFont(font)
        plus.setAutoRaise(True)
        plus.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        plus.setCursor(Qt.CursorShape.PointingHandCursor)
        plus.setToolTip(translate('message_pane', 'Pick another emoji'))
        plus.clicked.connect(self._more)
        plus.hovered.connect(self._more)        # resting on it is enough
        layout.addWidget(plus)
        layout.addStretch(1)
        self._plus = plus
        self._more_sent = False
        self._chosen = False

    def hit(self, global_position):
        """The emoji, '+' or None under a point on the screen."""
        for button, emoji in self._buttons + [(self._plus, '+')]:
            if button.rect().contains(button.mapFromGlobal(global_position)):
                return emoji
        return None

    def _chose(self, emoji):
        if self._chosen:            # once: the button's own click and the menu's filter may both come
            return
        self._chosen = True
        self.chosen.emit(emoji)

    def _more(self):
        if not self._more_sent:     # once: the hover and a click may both come
            self._more_sent = True
            self.more.emit(self._plus.mapToGlobal(QPoint(0, self._plus.height())))


class _HoverButton(QToolButton):
    """A tool button that also fires when the mouse rests on it for a moment."""

    hovered = pyqtSignal()

    delay = 250     # ms: passing over it on the way elsewhere does not open anything

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.delay)
        self._timer.timeout.connect(self.hovered.emit)

    def enterEvent(self, event):
        super().enterEvent(event)
        self._timer.start()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._timer.stop()


class _MenuClicks(QObject):
    """Clicks on the strip, taken wherever they arrive. With some Qt versions and platforms (Qt 6.4
    under XWayland) the menu keeps the mouse release for itself and the emoji buttons never see it,
    so the menu and the strip are watched and a release over a button chooses it."""

    def __init__(self, strip, parent):
        super().__init__(parent)
        self.strip = strip

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease) and event.button() == Qt.MouseButton.LeftButton:
            position = event.globalPosition().toPoint()
            target = self.strip.hit(position)
            if target is None:
                return False
            if kind == QEvent.Type.MouseButtonRelease:
                if target == '+':
                    self.strip._more()
                else:
                    self.strip._chose(target)
            return True             # the press too, so the menu does not act on it
        return False


def reaction_action(menu, on_chosen, on_more):
    """The strip as a menu entry; choosing closes the menu first."""
    strip = ReactionStrip(menu)
    clicks = _MenuClicks(strip, menu)
    for watched in [menu, strip] + strip.findChildren(QToolButton):
        watched.installEventFilter(clicks)
    strip.chosen.connect(lambda emoji: (menu.close(), on_chosen(emoji)))     # menu.close() only hides it: the strip lives on
    strip.more.connect(lambda position: (menu.close(), on_more(position)))
    action = QWidgetAction(menu)
    action.setDefaultWidget(strip)
    return action


class EmojiPicker(QFrame):
    """A popup with the quick reactions and Sylk Mobile's emoji categories; a click chooses and closes it."""

    chosen = pyqtSignal(str)

    columns = 10
    cell = 36

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        tabs = QHBoxLayout()
        tabs.setSpacing(0)
        layout.addLayout(tabs)
        self.pages = QStackedWidget(self)
        layout.addWidget(self.pages)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.idClicked.connect(self.pages.setCurrentIndex)
        sections = [('recent', REACTIONS[0], REACTIONS)] + [(key, label, emojis) for key, label, emojis in CATEGORIES]
        for number, (key, label, emojis) in enumerate(sections):
            tab = _emoji_button(label, 13, self)
            tab.setToolTip(translate('message_pane', 'Quick reactions') if key == 'recent' else '')
            tab.setCheckable(True)
            tab.setChecked(number == 0)
            group.addButton(tab, number)
            tabs.addWidget(tab)
            self.pages.addWidget(self._page(emojis))
        tabs.addStretch(1)
        self.setFixedWidth(self.columns * self.cell + 2 * 6 + self.style().pixelMetric(self.style().PixelMetric.PM_ScrollBarExtent) + 8)

    def _page(self, emojis):
        area = QScrollArea(self)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        grid_widget = QWidget(area)
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        for number, emoji in enumerate(emojis):
            button = _emoji_button(emoji, 17, grid_widget)
            button.setFixedSize(QSize(self.cell, self.cell))
            button.clicked.connect(lambda checked=False, emoji=emoji: self._choose(emoji))
            grid.addWidget(button, number // self.columns, number % self.columns)
        grid.setRowStretch(grid.rowCount(), 1)
        area.setWidget(grid_widget)
        area.setFixedHeight(6 * self.cell + 4)
        return area

    def _choose(self, emoji):
        # handed on before closing: the picker deletes itself when closed
        try:
            self.chosen.emit(emoji)
        except Exception as e:
            import traceback
            ActivityLog().error(f'[ui] Reaction {emoji} from the emoji picker failed: {e!r}\n{traceback.format_exc()}')
        self.close()

    def popup(self, global_position):
        """Show it at a point on the screen, kept on the screen."""
        self.adjustSize()
        screen = QGuiApplication.screenAt(global_position) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        x = min(max(global_position.x(), available.left()), available.right() - self.width())
        y = global_position.y()
        if y + self.height() > available.bottom():
            y = max(available.top(), global_position.y() - self.height())
        self.move(QPoint(x, y))
        self.show()


def _is_emoji_char(ch):
    cp = ord(ch)
    return (cp >= 0x80 and (unicodedata.category(ch) == 'So' or 0x1F000 <= cp <= 0x1FAFF or 0x25FB <= cp <= 0x25FE)) or cp in (0x203C, 0x2049)


def _is_emoji_joiner(ch):
    cp = ord(ch)
    return cp in (0x200D, 0xFE0F, 0x20E3) or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F


def color_emoji(document):
    """Draw the emoji of a QTextDocument from a colour emoji font. Symbols the text font has too
    (❤ ☕ ✔ ☀ …) are otherwise taken from it and drawn as black outlines, whatever the variation
    selector asks for. Document positions count UTF-16 units, hence the bookkeeping."""
    text = document.toPlainText()
    runs, position, start = [], 0, None
    for ch in text:
        if _is_emoji_char(ch) or (start is not None and _is_emoji_joiner(ch)):
            if start is None:
                start = position
        elif start is not None:
            runs.append((start, position))
            start = None
        position += 2 if ord(ch) > 0xFFFF else 1
    if start is not None:
        runs.append((start, position))
    if not runs:
        return
    emoji_format = QTextCharFormat()
    emoji_format.setFontFamilies(list(EMOJI_FAMILIES))
    cursor = QTextCursor(document)
    for begin, end in runs:
        cursor.setPosition(begin)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(emoji_format)
