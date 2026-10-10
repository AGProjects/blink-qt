"""Call me, maybe? Where others can call one's account: a SIP client, or a web browser.

As in Sylk Mobile (CallMeMaybeModal): an account whose domain runs SylkServer
(its configuration was found by SylkServerDiscovery, DNS TXT _sylkserver.<domain>)
can be called from a web page of that server, <publicUrl>/call/<account>, publicUrl
being the configuration's (https://<domain> when it publishes none). The dialog
shows the SIP address and the web address, the web address as a QR code too (when
python3-qrcode or segno is installed), and copies them or puts them in an e-mail.
"""

from urllib.parse import quote

from PyQt6.QtCore import QRectF, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QPainter, QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from sipsimple.account import BonjourAccount

from blink.logging import ActivityLog
from blink.util import translate


__all__ = ['call_me_target', 'CallMeMaybeDialog']


def call_me_target(account):
    """The web address others can call the account at (the account selected in the main
    window), when its domain runs SylkServer; else None."""
    from blink.sylk_discovery import SylkServerDiscovery
    if account is None or account is BonjourAccount() or not account.enabled:
        return None
    domain = account.id.domain
    configuration = SylkServerDiscovery().configuration(domain)
    if not configuration:
        return None
    public_url = str(configuration.get('publicUrl') or f'https://{domain}').rstrip('/')
    return f'{public_url}/call/{account.id}'


def _qr_matrix(text):
    """The QR code of text as rows of booleans, or None without a QR library."""
    try:
        import qrcode
        code = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
        code.add_data(text)
        code.make(fit=True)
        return code.get_matrix()
    except ImportError:
        pass
    try:
        import segno
        return [[bool(cell) for cell in row] for row in segno.make(text, error='m', micro=False).matrix]
    except ImportError:
        return None


def _qr_pixmap(text, size=200):
    matrix = _qr_matrix(text)
    if not matrix:
        return None
    count = len(matrix)
    quiet = 2                               # modules of white around it
    module = max(1, size // (count + 2 * quiet))
    side = module * (count + 2 * quiet)
    pixmap = QPixmap(side, side)
    pixmap.fill(QColor('white'))
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('black'))
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.drawRect(QRectF((x + quiet) * module, (y + quiet) * module, module, module))
    painter.end()
    return pixmap


class CallMeMaybeDialog(QDialog):
    def __init__(self, account, call_url, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate('callme', 'Call me, maybe?'))
        self.sip_uri = str(account.id)
        self.call_url = call_url
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(6)

        title = QLabel(translate('callme', 'Call me, maybe?'), self)
        font = QFont(title.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.3)
        title.setFont(font)
        layout.addWidget(title)
        layout.addSpacing(6)

        layout.addWidget(QLabel(translate('callme', 'Others can call you with a SIP client at:'), self))
        layout.addWidget(self._link(f'sip:{self.sip_uri}', self.sip_uri))
        layout.addSpacing(4)
        layout.addWidget(QLabel(translate('callme', 'or with a Web browser at:'), self))
        layout.addWidget(self._link(call_url, call_url))

        pixmap = _qr_pixmap(call_url)
        if pixmap is not None:
            code = QLabel(self)
            code.setPixmap(pixmap)
            code.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addSpacing(8)
            layout.addWidget(code)
        layout.addSpacing(8)
        layout.addWidget(QLabel(translate('callme', 'Share these addresses with others:'), self))

        buttons = QHBoxLayout()
        copy_button = QPushButton(translate('callme', 'Copy'), self)
        email_button = QPushButton(translate('callme', 'E-mail...'), self)
        close_button = QPushButton(translate('callme', 'Close'), self)
        close_button.setDefault(True)
        buttons.addWidget(copy_button)
        buttons.addWidget(email_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        copy_button.clicked.connect(self._copy)
        email_button.clicked.connect(self._email)
        close_button.clicked.connect(self.accept)

    def _link(self, href, text):
        label = QLabel(f'<a href="{href}">{text}</a>', self)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        label.setOpenExternalLinks(True)
        label.setWordWrap(True)
        return label

    @property
    def message(self):
        return (translate('callme', 'You can call me using a Web browser at %s or a SIP client at %s '
                                    'or by using the freely available Sylk client from http://sylkserver.com') % (self.call_url, self.sip_uri))

    def _copy(self):
        QGuiApplication.clipboard().setText(self.call_url)        # as mobile: the web address
        ActivityLog().info(f'[ui] Call me, maybe: {self.call_url} copied to the clipboard')
        self.accept()

    def _email(self):
        subject = quote(translate('callme', 'Call me, maybe?'))
        QDesktopServices.openUrl(QUrl(f'mailto:?subject={subject}&body={quote(self.message)}', QUrl.ParsingMode.TolerantMode))
        self.accept()
