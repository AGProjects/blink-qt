"""Share conference: every address a room can be joined at, to copy or e-mail, as Sylk Mobile's
ShareConferenceLinkModal shows them.

From the SylkServer configuration of the room's domain (the account's own, or another
server's for a room joined there):

  - Web: <publicUrl>/conference/<room> (the Sylk web app)
  - SIP: <room>@<conference domain> (any SIP client: audio and MSRP chat)
  - PSTN access number and room number, when the server has a telephone bridge (conference.pstnBridge)
  - the QR code of sylk://<domain>/conference/<room>, which Sylk Mobile opens
"""

from urllib.parse import quote

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QGuiApplication
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from blink.callme import _qr_pixmap
from blink.logging import ActivityLog
from blink.util import translate


__all__ = ['ShareConferenceDialog', 'conference_addresses', 'share_icon', 'share_available']


def _icon_share(painter):
    """A box with an arrow leaving it upwards, in the call bar's 24x18 box (blink.videowindow.draw_icon)."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath
    path = QPainterPath()
    path.moveTo(8.5, 7.5)
    path.lineTo(6, 7.5)
    path.lineTo(6, 17)
    path.lineTo(18, 17)
    path.lineTo(18, 7.5)
    path.lineTo(15.5, 7.5)
    painter.strokePath(path, painter.pen())
    painter.drawLine(QPointF(12, 1.5), QPointF(12, 11.5))
    arrow = QPainterPath()
    arrow.moveTo(8.8, 4.7)
    arrow.lineTo(12, 1.5)
    arrow.lineTo(15.2, 4.7)
    painter.strokePath(arrow, painter.pen())


from blink.videowindow import ICONS, draw_icon
ICONS.setdefault('share', _icon_share)


def share_icon(color=None, size=20):
    """The share icon as a QIcon, drawn in color (for tool buttons); by default dark under a light
    theme and light under a dark one, as themed_icon draws the other header buttons."""
    if color is None:
        from PyQt6.QtGui import QColor, QPalette
        from PyQt6.QtWidgets import QApplication
        application = QApplication.instance()
        dark_theme = application is not None and application.palette().color(QPalette.ColorRole.Window).lightness() < 128
        color = QColor('#d0d0d0' if dark_theme else '#303030')
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QIcon, QPainter, QPixmap
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    draw_icon(painter, 'share', QRectF(0, 0, size * 2, size * 2), color)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def _room_domain(sip_uri, account, target=None):
    """The domain whose SylkServer configuration describes the room."""
    if target is not None:
        return target.domain
    from blink.webrtc import local_room_host, room_target
    user, _, host = sip_uri.partition('@')
    if not local_room_host(host, account):
        remote = room_target(user, host, account)
        if remote is not None:
            return remote.domain
    return account.id.domain


def share_available(sip_uri, account, target=None):
    """Whether the room has a SylkServer configuration to tell its addresses."""
    from blink.sylk_discovery import SylkServerDiscovery
    if account is None or not sip_uri or '@' not in sip_uri:
        return False
    try:
        return bool(SylkServerDiscovery().configuration(_room_domain(sip_uri, account, target)))
    except Exception:
        return False


def conference_addresses(sip_uri, account, target=None):
    """The addresses of the room sip_uri (its MSRP switch address): dict(web, sip, pstn, room, sylk)."""
    from blink.sylk_discovery import SylkServerDiscovery
    sip_uri = sip_uri.replace('sip:', '', 1)
    room = sip_uri.partition('@')[0]
    domain = _room_domain(sip_uri, account, target)
    configuration = SylkServerDiscovery().configuration(domain) or {}
    public_url = str(configuration.get('publicUrl') or 'https://%s' % domain).rstrip('/')
    conference = configuration.get('conference') if isinstance(configuration.get('conference'), dict) else {}
    pstn = conference.get('pstnBridge') or configuration.get('pstnBridge')
    sylk_domain = configuration.get('defaultDomain') or domain
    return dict(room=room,
                web='%s/conference/%s' % (public_url, quote(room)),
                sip=sip_uri,
                pstn=str(pstn) if pstn else None,
                sylk='sylk://%s/conference/%s' % (sylk_domain, quote(room)))


class ShareConferenceDialog(QDialog):
    def __init__(self, sip_uri, account, target=None, title=None, parent=None):
        super(ShareConferenceDialog, self).__init__(parent)
        self.addresses = conference_addresses(sip_uri, account, target)
        self.setWindowTitle(translate('share_conference', 'Share conference'))
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(6)

        heading = QLabel(title or translate('share_conference', 'Share conference'), self)
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.3)
        heading.setFont(font)
        layout.addWidget(heading)
        layout.addSpacing(6)
        layout.addWidget(QLabel(translate('share_conference', 'Others can join this conference:'), self))

        addresses = self.addresses
        self._row(layout, translate('share_conference', 'With a Web browser'), addresses['web'], addresses['web'])
        self._row(layout, translate('share_conference', 'With a SIP client'), 'sip:%s' % addresses['sip'], addresses['sip'])
        if addresses['pstn']:
            self._row(layout, translate('share_conference', 'By telephone, PSTN access number'), 'tel:%s' % addresses['pstn'], addresses['pstn'])
            self._row(layout, translate('share_conference', 'Room number'), None, addresses['room'])

        pixmap = _qr_pixmap(addresses['sylk'])
        if pixmap is not None:
            code = QLabel(self)
            code.setPixmap(pixmap)
            code.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            code.setToolTip(translate('share_conference', 'Scan with Sylk Mobile: %s') % addresses['sylk'])
            layout.addSpacing(8)
            layout.addWidget(code)

        layout.addSpacing(8)
        buttons = QHBoxLayout()
        copy_button = QPushButton(translate('share_conference', 'Copy'), self)
        copy_button.setToolTip(translate('share_conference', 'Copy all the addresses'))
        email_button = QPushButton(translate('share_conference', 'E-mail...'), self)
        close_button = QPushButton(translate('share_conference', 'Close'), self)
        close_button.setDefault(True)
        buttons.addWidget(copy_button)
        buttons.addWidget(email_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        copy_button.clicked.connect(self._copy)
        email_button.clicked.connect(self._email)
        close_button.clicked.connect(self.accept)

    def _row(self, layout, what, href, text):
        layout.addSpacing(4)
        label = QLabel(what + ':', self)
        label.setEnabled(False)
        layout.addWidget(label)
        row = QHBoxLayout()
        value = QLabel(f'<a href="{href}">{text}</a>' if href else text, self)
        value.setTextFormat(Qt.TextFormat.RichText)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction | Qt.TextInteractionFlag.TextSelectableByMouse)
        value.setOpenExternalLinks(bool(href))
        value.setWordWrap(True)
        copy = QPushButton(translate('share_conference', 'Copy'), self)
        copy.setAutoDefault(False)
        copy.clicked.connect(lambda checked=False, text=text: self._copy_one(text))
        row.addWidget(value, 1)
        row.addWidget(copy)
        layout.addLayout(row)

    @property
    def lines(self):
        addresses = self.addresses
        lines = ['• Web: %s' % addresses['web']]
        if addresses['pstn']:
            lines.append('• PSTN access number: %s' % addresses['pstn'])
            lines.append('• Room number: %s' % addresses['room'])
        lines.append('• SIP: %s' % addresses['sip'])
        return '\n'.join(lines)

    def _copy_one(self, text):
        QGuiApplication.clipboard().setText(text)
        ActivityLog().info(f'[ui] Share conference: {text} copied to the clipboard')

    def _copy(self):
        QGuiApplication.clipboard().setText(self.lines)
        ActivityLog().info(f'[ui] Share conference: the addresses of {self.addresses["sip"]} copied to the clipboard')
        self.accept()

    def _email(self):
        subject = quote(translate('share_conference', 'Join conference, maybe?'))
        body = translate('share_conference', 'You can join the conference using one of these:') + '\n\n' + self.lines
        QDesktopServices.openUrl(QUrl(f'mailto:?subject={subject}&body={quote(body)}', QUrl.ParsingMode.TolerantMode))
        self.accept()
