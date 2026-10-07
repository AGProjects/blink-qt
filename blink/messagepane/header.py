"""ConversationHeader: the top of the message pane.

Avatar (the contact's photo, else initials on a colour of their own), name,
info line (is typing..., else the address the conversation is on), the lock
with what is known about encryption (A− and A+ before it make the text of the
transcript and the composer smaller or larger), the calendar (years, months and days
with how many messages each has; choosing a day jumps there) and the audio and
video call buttons.
Calls start from the conversation's account: the one its newest message was
on, else the default one (a Bonjour neighbour: the Bonjour account).
"""

import os

from collections import defaultdict
from datetime import date

from PyQt6.QtCore import Qt, QLocale, QRectF, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPixmap
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings

from blink.logging import ActivityLog
from blink.messagepane.format import avatar_colour, initials
from blink.resources import IconManager, Resources, themed_icon
from blink.util import translate
from blink.widgets.color import follow_theme, secondary_text_color
from blink.widgets.labels import ElidedLabel


__all__ = ['ConversationHeader', 'contact_photo', 'draw_avatar']


def contact_photo(contact):
    """The contact's own picture (QIcon), or None when it has only the default avatar."""
    try:
        contact_id = contact.settings.id
    except AttributeError:
        return None
    if getattr(contact, 'type', None) not in ('addressbook', 'google'):
        return None
    icon_manager = IconManager()
    return icon_manager.get(contact_id + '_alt') or icon_manager.get(contact_id) or None


def remote_key_path(key):
    """Where a peer's public key is kept (as MessageManager saves it), for a conversation key."""
    name = str(key or '').replace('/', '_')
    if not name:
        return None
    return os.path.join(SIPSimpleSettings().chat.keys_directory.normalized, name + '.pubkey')


def own_key_paths(account):
    directory = os.path.join(SIPSimpleSettings().chat.keys_directory.normalized, 'private')
    name = account.id.replace('/', '_')
    return os.path.join(directory, name + '.privkey'), os.path.join(directory, name + '.pubkey')


def key_id(path):
    try:
        import pgpy
        key, _ = pgpy.PGPKey.from_file(path)
        return str(key.fingerprint.keyid)
    except Exception:
        return None


def draw_avatar(painter, rect, photo, letters, colour):
    """A round avatar in rect: the photo (QIcon), else the letters in white on the colour."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    rect = QRectF(rect)
    path = QPainterPath()
    path.addEllipse(rect)
    if photo is not None:
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        pixmap = photo.pixmap(QSize(int(rect.width()), int(rect.height())), ratio)
        painter.setClipPath(path)
        painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
    else:
        painter.fillPath(path, QColor(colour))
        font = QFont(painter.font())
        font.setBold(True)
        font.setPixelSize(max(int(rect.height() * 0.4), 6))
        painter.setFont(font)
        painter.setPen(QColor('#ffffff'))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, letters)
    painter.restore()


class AvatarLabel(QLabel):
    size = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.size, self.size)
        self.photo = None
        self.letters = ''
        self.colour = QColor('#90a4ae')

    def set_contact(self, photo, letters, colour):
        self.photo, self.letters, self.colour = photo, letters, QColor(colour)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(self.font())
        draw_avatar(painter, QRectF(0, 0, self.size, self.size), self.photo, self.letters, self.colour)
        painter.end()

    def draw(self, painter, rect):
        """The same avatar elsewhere (next to the peer's messages)."""
        draw_avatar(painter, rect, self.photo, self.letters, self.colour)


class ConversationHeader(QWidget):
    dayChosen = pyqtSignal(object)      # a date to jump to
    fontStep = pyqtSignal(int)          # -1 smaller, +1 larger

    def __init__(self, parent=None):
        super().__init__(parent)
        self.contact = self.uri = self.key = None
        self.account = None
        self.day_counts = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(10, 6, 8, 6)
        row.setSpacing(8)
        outer.addLayout(row)
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)

        self.avatar = AvatarLabel(self)
        row.addWidget(self.avatar)

        text = QVBoxLayout()
        text.setSpacing(0)
        self.name_label = ElidedLabel(self)
        font = QFont(self.name_label.font())
        font.setWeight(QFont.Weight.DemiBold)
        self.name_label.setFont(font)
        self.info_label = ElidedLabel(self)
        for label in (self.name_label, self.info_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addStretch(1)
        text.addWidget(self.name_label)
        text.addWidget(self.info_label)
        text.addStretch(1)
        row.addLayout(text, 1)

        self.lock_button = self._tool_button()
        self.lock_menu = QMenu(self.lock_button)
        self.lock_menu.aboutToShow.connect(self._fill_lock_menu)
        self.lock_button.setMenu(self.lock_menu)
        self.lock_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.lock_button.setStyleSheet('QToolButton::menu-indicator { image: none; }')
        self.calendar_button = self._tool_button(translate('message_pane', 'Jump to a date'))
        self.calendar_menu = QMenu(self.calendar_button)
        self.calendar_menu.aboutToShow.connect(self._fill_calendar_menu)
        self.calendar_button.setMenu(self.calendar_menu)
        self.calendar_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.calendar_button.setStyleSheet('QToolButton::menu-indicator { image: none; }')
        self.audio_button = self._tool_button(translate('message_pane', 'Audio call'))
        self.video_button = self._tool_button(translate('message_pane', 'Video call'))
        self.audio_button.clicked.connect(lambda: self._start_call('audio'))
        self.video_button.clicked.connect(lambda: self._start_call('video'))
        self.font_buttons = []
        for text, step, tip in (('A−', -1, translate('message_pane', 'Smaller text')), ('A+', 1, translate('message_pane', 'Larger text'))):
            button = self._tool_button(tip)
            button.setText(text)
            button.clicked.connect(lambda checked=False, step=step: self.fontStep.emit(step))
            self.font_buttons.append(button)
        # ■ stops the clip playing anywhere in the application (one at a time; leaving a conversation does not stop it)
        self.stop_audio_button = self._tool_button(translate('message_pane', 'Stop playing'))
        self.stop_audio_button.setText('■')
        self.stop_audio_button.hide()
        from blink.messagepane.audio import audio_available
        if audio_available():
            from blink.messagepane.audio import AudioPlayer
            player = AudioPlayer.instance()
            self.stop_audio_button.clicked.connect(player.stop)
            player.changed.connect(lambda *args: self.stop_audio_button.setVisible(player.message_id is not None))
        for button in (self.stop_audio_button, *self.font_buttons, self.lock_button, self.calendar_button, self.audio_button, self.video_button):
            row.addWidget(button)

        self.apply_theme()
        follow_theme(self)

    def _tool_button(self, tooltip=None):
        button = QToolButton(self)
        button.setAutoRaise(True)
        button.setIconSize(QSize(20, 20))
        button.setFixedSize(30, 30)
        if tooltip:
            button.setToolTip(tooltip)
        return button

    def apply_theme(self):
        palette = self.info_label.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, QPalette.ColorRole.WindowText, secondary_text_color(self.palette(), group))
        self.info_label.setPalette(palette)
        self.audio_button.setIcon(themed_icon(Resources.get('icons/handset.png'), '#d0d0d0'))
        self.video_button.setIcon(themed_icon(Resources.get('icons/camera.png'), '#d0d0d0'))
        self.calendar_button.setIcon(themed_icon(Resources.get('icons/clock.svg'), '#d0d0d0'))
        self.update_lock()

    # Contents

    def set_conversation(self, contact, uri, key, account):
        self.contact, self.uri, self.key, self.account = contact, uri, key, account
        self.day_counts = {}
        self.calendar_button.setEnabled(False)
        name = getattr(contact, 'name', '') or str(uri.uri)
        self.name_label.setText(name)
        self.name_label.setToolTip(name)
        self.avatar.set_contact(contact_photo(contact), initials(name, str(uri.uri)), avatar_colour(key))
        self.update_info()
        self.update_lock()

    def set_day_counts(self, key, counts):
        if key == self.key:
            self.day_counts = counts
            self.calendar_button.setEnabled(bool(counts))

    def _fill_calendar_menu(self):
        """Years, then months, then days, newest first, each with its number of messages."""
        menu = self.calendar_menu
        menu.clear()
        days = {}
        for text, count in self.day_counts.items():
            try:
                days[date.fromisoformat(text)] = count
            except ValueError:
                pass
        if not days:
            menu.addAction(translate('message_pane', 'No messages')).setEnabled(False)
            return
        locale = QLocale()
        years = defaultdict(lambda: defaultdict(dict))
        for day, count in days.items():
            years[day.year][day.month][day] = count
        for year in sorted(years, reverse=True):
            months = years[year]
            year_menu = menu.addMenu(f'{year}   ({sum(sum(month.values()) for month in months.values())})')
            for month in sorted(months, reverse=True):
                month_days = months[month]
                month_menu = year_menu.addMenu(f'{locale.monthName(month, QLocale.FormatType.LongFormat)}   ({sum(month_days.values())})')
                for day in sorted(month_days, reverse=True):
                    label = f'{locale.dayName(day.isoweekday(), QLocale.FormatType.ShortFormat)} {day.day}   ({month_days[day]})'
                    month_menu.addAction(label, lambda day=day: self.dayChosen.emit(day))

    def set_account(self, account):
        self.account = account
        self.update_lock()

    def update_info(self):
        if self.contact is None:
            return
        from blink.history import ConversationTyping
        if ConversationTyping().is_typing([self.key]):
            self.info_label.setText(translate('message_pane', 'is typing…'))
        else:
            self.info_label.setText(str(self.uri.uri))

    # Encryption

    def _encryption_state(self):
        """(own key ready, peer key path or None)"""
        account = self.account
        own_ready = False
        if account is not None and account.sms.enable_pgp:
            private_path, _ = own_key_paths(account)
            own_ready = os.path.exists(private_path)
        peer_path = remote_key_path(self.key)
        return own_ready, peer_path if peer_path and os.path.exists(peer_path) else None

    def update_lock(self):
        if self.contact is None or not hasattr(self, 'lock_button'):
            return
        own_ready, peer_path = self._encryption_state()
        if own_ready and peer_path:
            self.lock_button.setIcon(QIcon(Resources.get('icons/lock-green-18.svg')))
            self.lock_button.setToolTip(translate('message_pane', 'Messages are encrypted with OpenPGP'))
        else:
            self.lock_button.setIcon(QIcon(Resources.get('icons/lock-grey-12.svg')))
            if not own_ready:
                self.lock_button.setToolTip(translate('message_pane', 'Messages are not encrypted: this account has no PGP key'))
            else:
                self.lock_button.setToolTip(translate('message_pane', 'Messages are not encrypted: no public key of %s') % self.key)

    def _fill_lock_menu(self):
        menu = self.lock_menu
        menu.clear()
        own_ready, peer_path = self._encryption_state()
        account = self.account
        if peer_path:
            menu.addAction(translate('message_pane', 'Public key of %s: %s') % (self.key, key_id(peer_path) or '?')).setEnabled(False)
        else:
            menu.addAction(translate('message_pane', 'No public key of %s') % self.key).setEnabled(False)
        if account is not None:
            _, own_public = own_key_paths(account)
            if own_ready and os.path.exists(own_public):
                menu.addAction(translate('message_pane', 'My key (%s): %s') % (account.id, key_id(own_public) or '?')).setEnabled(False)
            else:
                menu.addAction(translate('message_pane', 'No PGP key for %s') % account.id).setEnabled(False)
        menu.addSeparator()
        lookup = menu.addAction(translate('message_pane', 'Look Up Public Key'), self._lookup_key)
        lookup.setEnabled(account is not None and account is not BonjourAccount() and account.sms.enable_pgp)
        send = menu.addAction(translate('message_pane', 'Send My Public Key'), self._send_my_key)
        send.setEnabled(account is not None and own_ready and os.path.exists(own_key_paths(account)[1]))
        self.update_lock()

    def _message_session(self):
        from blink.messages import MessageManager
        return MessageManager().create_message_session(str(self.uri.uri), selected=False)

    def _lookup_key(self):
        from blink.messages import MessageManager
        session = self._message_session()
        MessageManager().send_message(session.account, session.contact, 'Public key request', 'application/sylk-api-pgp-key-lookup')
        ActivityLog().info(f'[pgp] Asked the server for the public key of {self.key} from account {session.account.id}')

    def _send_my_key(self):
        from blink.messages import MessageManager
        session = self._message_session()
        try:
            with open(own_key_paths(session.account)[1], 'rb') as f:
                public_key = f.read().decode()
        except OSError as e:
            ActivityLog().warning(f'[pgp] Cannot read the public key of {session.account.id}: {e}')
            return
        MessageManager().send_message(session.account, session.contact, public_key, 'text/pgp-public-key')
        ActivityLog().info(f'[pgp] Sent the public key of {session.account.id} to {self.key}')

    # Calls

    def _start_call(self, media):
        if self.contact is None:
            return
        from blink.sessions import SessionManager, StreamDescription
        streams = [StreamDescription('audio')] if media == 'audio' else [StreamDescription('audio'), StreamDescription('video')]
        SessionManager().create_session(self.contact, self.uri, streams, account=self.account)
