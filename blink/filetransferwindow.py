# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

__all__ = ['FileTransferWindow']

import os

from PyQt4 import uic
from PyQt4.QtCore import Qt, QUrl
from PyQt4.QtGui  import QAction, QDesktopServices, QMenu

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.system import makedirs
from zope.interface import implements

from sipsimple.configuration.settings import SIPSimpleSettings

from blink.resources import Resources
from blink.sessions import FileTransferDelegate, FileTransferModel
from blink.widgets.util import ContextMenuActions


ui_class, base_class = uic.loadUiType(Resources.get('filetransfer_window.ui'))

class FileTransferWindow(base_class, ui_class):
    implements(IObserver)

    def __init__(self, parent=None):
        super(FileTransferWindow, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)

        self.model = FileTransferModel(self)
        self.listview.setModel(self.model)
        self.listview.setItemDelegate(FileTransferDelegate(self.listview))
        self.listview.customContextMenuRequested.connect(self._SH_ContextMenuRequested)

        self.context_menu = QMenu(self.listview)
        self.actions = ContextMenuActions()
        self.actions.open_file = QAction("Open", self, triggered=self._AH_OpenFile)
        self.actions.open_folder = QAction("Open Containing Folder", self, triggered=self._AH_OpenContainingFolder)
        self.actions.cancel_transfer = QAction("Cancel", self, triggered=self._AH_CancelTransfer)
        self.actions.remove_entry = QAction("Remove From List", self, triggered=self._AH_RemoveEntry)
        self.actions.open_downloads_folder = QAction("Open Downloads Folder", self, triggered=self._AH_OpenDownloadsFolder)
        self.actions.clear_list = QAction("Clear List", self, triggered=self._AH_ClearList)

        self.model.itemAdded.connect(self.update_status)
        self.model.itemRemoved.connect(self.update_status)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='FileTransferDidEnd')

    def show(self, activate=True):
        settings = SIPSimpleSettings()
        directory = settings.file_transfer.directory.normalized
        makedirs(directory)
        self.setAttribute(Qt.WA_ShowWithoutActivating, not activate)
        super(FileTransferWindow, self).show()
        self.raise_()
        if activate:
            self.activateWindow()

    def update_status(self):
        total = len(self.model.items)
        active = len([item for item in self.model.items if not item.ended])
        text = u'%d %s' % (total, 'transfer' if total==1 else 'transfers')
        if active > 0:
            text += u' (%d active)' % active
        self.status_label.setText(text)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_FileTransferDidEnd(self, notification):
        self.update_status()

    def _SH_ContextMenuRequested(self, pos):
        menu = self.context_menu
        menu.clear()
        index = self.listview.indexAt(pos)
        if index.isValid():
            item = index.data(Qt.UserRole)
            if item.ended:
                if item.direction == 'incoming' and item.ended and not item.failed:
                    menu.addAction(self.actions.open_file)
                    menu.addAction(self.actions.open_folder)
                menu.addAction(self.actions.remove_entry)
            else:
                menu.addAction(self.actions.cancel_transfer)
            menu.addSeparator()
            menu.addAction(self.actions.open_downloads_folder)
            menu.addAction(self.actions.clear_list)
        elif self.model.rowCount() > 0:
            menu.addAction(self.actions.open_downloads_folder)
            menu.addAction(self.actions.clear_list)
        else:
            menu.addAction(self.actions.open_downloads_folder)
        menu.exec_(self.mapToGlobal(pos))

    def _AH_OpenFile(self):
        item = self.listview.selectedIndexes()[0].data(Qt.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.filename))

    def _AH_OpenContainingFolder(self):
        item = self.listview.selectedIndexes()[0].data(Qt.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(item.filename)))

    def _AH_CancelTransfer(self):
        item = self.listview.selectedIndexes()[0].data(Qt.UserRole)
        item.end()

    def _AH_RemoveEntry(self):
        item = self.listview.selectedIndexes()[0].data(Qt.UserRole)
        self.model.removeItem(item)

    def _AH_OpenDownloadsFolder(self):
        settings = SIPSimpleSettings()
        directory = settings.file_transfer.directory.normalized
        QDesktopServices.openUrl(QUrl.fromLocalFile(directory))

    def _AH_ClearList(self):
        self.model.clear_ended()

del ui_class, base_class

