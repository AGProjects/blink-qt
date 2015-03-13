# Copyright (C) 2015 AG Projects. See LICENSE for details.
#


__all__ = ['ZRTPWidget']


from PyQt4 import uic
from PyQt4.QtCore import pyqtSignal

from blink.resources import Resources


ui_class, base_class = uic.loadUiType('resources/zrtp_widget.ui')

class ZRTPWidget(base_class, ui_class):
    closed = pyqtSignal()
    nameChanged = pyqtSignal()
    statusChanged = pyqtSignal()

    def __init__(self, parent=None):
        super(ZRTPWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.peer_name = ''
        self.peer_verified = False
        self.close_button.clicked.connect(self.hide)
        self.peer_name_value.editingFinished.connect(self._check_name_changes)
        self.validate_button.clicked.connect(self._SH_ValidateButtonClicked)

    def _get_peer_name(self):
        return self.peer_name_value.text()

    def _set_peer_name(self, name):
        self.__dict__['peer_name'] = name
        self.peer_name_value.setText(name)

    peer_name = property(_get_peer_name, _set_peer_name)
    del _get_peer_name, _set_peer_name

    def _get_peer_verified(self):
        return self.__dict__['peer_verified']

    def _set_peer_verified(self, verified):
        self.__dict__['peer_verified'] = verified
        if verified:
            self.validate_button.setText(u'Invalidate')
            self.status_value.setText(u'<span style=\"color: #55ff00;\">Verified</span>')
        else:
            self.validate_button.setText(u'Validate')
            self.status_value.setText(u'<span style=\"color: #ff5500;\">Not verified</span>')
        self.validate_button.setChecked(verified)

    peer_verified = property(_get_peer_verified, _set_peer_verified)
    del _get_peer_verified, _set_peer_verified

    def _get_sas(self):
        return self.sas_value.text()

    def _set_sas(self, sas):
        self.sas_value.setText(sas)

    sas = property(_get_sas, _set_sas)
    del _get_sas, _set_sas

    def hideEvent(self, event):
        if not event.spontaneous():
            self.closed.emit()
            self._check_name_changes()

    def _check_name_changes(self):
        peer_name = self.peer_name_value.text()
        if peer_name != self.__dict__['peer_name']:
            self.__dict__['peer_name'] = peer_name
            self.nameChanged.emit()

    def _SH_ValidateButtonClicked(self, checked):
        self.hide()
        self.peer_verified = checked
        self.statusChanged.emit()

del ui_class, base_class

