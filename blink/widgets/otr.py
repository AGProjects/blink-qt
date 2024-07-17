
import re

from PyQt6 import uic
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QStyle, QStyleOption, QStylePainter

from blink.resources import Resources
from blink.util import translate
from blink.sessions import SMPVerification


__all__ = ['OTRWidget']


ui_class, base_class = uic.loadUiType(Resources.get('otr_widget.ui'))


class OTRWidget(base_class, ui_class):
    closed = pyqtSignal()
    nameChanged = pyqtSignal()
    statusChanged = pyqtSignal()

    color_table = {'green': 'hsv(100, 85%, 100%)', 'orange': 'hsv(20, 85%, 100%)'}

    def __init__(self, parent=None):
        super(OTRWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.__dict__.update(peer_verified=False, smp_status=SMPVerification.Unavailable)  # interdependent properties (they need to preexist as their setters read each other)
        self.peer_name = ''
        self.peer_verified = False
        self.peer_fingerprint = ''
        self.my_fingerprint = ''
        self.smp_status = SMPVerification.Unavailable
        self.verification_stack.wrap = True
        self.verification_stack.animationDuration = 200
        self.close_button.clicked.connect(self.hide)
        self.switch_button.clicked.connect(self.verification_stack.slideInNext)
        self.peer_name_value.editingFinished.connect(self._check_name_changes)
        self.validate_button.clicked.connect(self._SH_ValidateButtonClicked)
        self.verification_stack.currentChanged.connect(self._SH_VerificationStackPanelChanged)

    @property
    def peer_name(self):
        return self.peer_name_value.text()

    @peer_name.setter
    def peer_name(self, name):
        self.__dict__['peer_name'] = name
        self.peer_name_value.setText(name)

    @property
    def peer_verified(self):
        return self.__dict__['peer_verified']

    @peer_verified.setter
    def peer_verified(self, verified):
        self.__dict__['peer_verified'] = verified
        self.validate_button.setText(translate('otr_widget', 'Invalidate') if verified else translate('otr_widget', 'Validate'))
        self.validate_button.setChecked(verified)
        self.validate_button.setEnabled(verified or self.verification_stack.currentWidget() is not self.smp_panel or self.smp_status is SMPVerification.Succeeded)
        self.peer_fingerprint_value.setStyleSheet('QLabel {{ color: {}; }}'.format(self.color_table['green'] if verified else self.color_table['orange']))
        self.smp_status_value.setText(self.smp_status_text)

    @property
    def peer_fingerprint(self):
        return self.__dict__['peer_fingerprint']

    @peer_fingerprint.setter
    def peer_fingerprint(self, fingerprint):
        self.__dict__['peer_fingerprint'] = fingerprint
        self.peer_fingerprint_value.setText(self._encode_fingerprint(fingerprint))

    @property
    def my_fingerprint(self):
        return self.__dict__['my_fingerprint']

    @my_fingerprint.setter
    def my_fingerprint(self, fingerprint):
        self.__dict__['my_fingerprint'] = fingerprint
        self.my_fingerprint_value.setText(self._encode_fingerprint(fingerprint))

    @property
    def smp_status(self):
        return self.__dict__['smp_status']

    @smp_status.setter
    def smp_status(self, status):
        self.__dict__['smp_status'] = status
        self.validate_button.setEnabled(self.peer_verified or self.verification_stack.currentWidget() is not self.smp_panel or self.smp_status is SMPVerification.Succeeded)
        self.smp_status_value.setText(self.smp_status_text)

    @property
    def smp_status_text(self):
        if self.peer_verified:
            return translate('otr_widget', '<span style="color: {[green]};">Verified</span>').format(self.color_table)
        elif self.smp_status is SMPVerification.Succeeded:
            return translate('otr_widget', '<span style="color: {[green]};">Succeeded</span>').format(self.color_table)
        elif self.smp_status is SMPVerification.Failed:
            return translate('otr_widget', '<span style="color: {[orange]};">Failed</span>').format(self.color_table)
        else:
            return '{}'.format(self.smp_status.value)

    def hideEvent(self, event):
        if not event.spontaneous():
            self.closed.emit()
            self._check_name_changes()

    def paintEvent(self, event):
        option = QStyleOption()
        option.initFrom(self)
        painter = QStylePainter(self)
        painter.setRenderHint(QStylePainter.RenderHint.Antialiasing, True)
        painter.drawPrimitive(QStyle.PrimitiveElement.PE_Widget if self.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground) else QStyle.PrimitiveElement.PE_Frame, option)

    @staticmethod
    def _encode_fingerprint(fingerprint):
        return re.sub('....', lambda match: match.group(0) + {match.endpos: '', match.endpos//2: '<br/>'}.get(match.end(), ' '), fingerprint.encode().hex().upper())

    def _check_name_changes(self):
        peer_name = self.peer_name_value.text()
        if peer_name != self.__dict__['peer_name']:
            self.__dict__['peer_name'] = peer_name
            self.nameChanged.emit()

    def _SH_ValidateButtonClicked(self, checked):
        self.hide()
        self.peer_verified = checked
        self.statusChanged.emit()

    def _SH_VerificationStackPanelChanged(self, index):
        self.validate_button.setEnabled(self.peer_verified or self.verification_stack.currentWidget() is not self.smp_panel or self.smp_status is SMPVerification.Succeeded)

del ui_class, base_class

