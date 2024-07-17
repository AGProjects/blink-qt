
import os

from PyQt6 import uic
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QAction, QDesktopServices
from PyQt6.QtWidgets import QMenu

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.system import makedirs
from zope.interface import implementer

from blink.configuration.settings import BlinkSettings
from blink.resources import Resources
from blink.sessions import FileTransferDelegate, FileTransferModel
from blink.util import translate
from blink.widgets.util import ContextMenuActions


__all__ = ['FileTransferWindow']


ui_class, base_class = uic.loadUiType(Resources.get('filetransfer_window.ui'))


@implementer(IObserver)
class FileTransferWindow(base_class, ui_class):

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
        self.actions.open_file = QAction(translate('filetransfer_window', "Open"), self, triggered=self._AH_OpenFile)
        self.actions.open_file_folder = QAction(translate('filetransfer_window', "Open File Folder"), self, triggered=self._AH_OpenFileFolder)
        self.actions.cancel_transfer = QAction(translate('filetransfer_window', "Cancel"), self, triggered=self._AH_CancelTransfer)
        self.actions.retry_transfer = QAction(translate('filetransfer_window', "Retry"), self, triggered=self._AH_RetryTransfer)
        self.actions.remove_entry = QAction(translate('filetransfer_window', "Remove From List"), self, triggered=self._AH_RemoveEntry)
        self.actions.open_downloads_folder = QAction(translate('filetransfer_window', "Open Transfers Folder"), self, triggered=self._AH_OpenTransfersFolder)
        self.actions.clear_list = QAction(translate('filetransfer_window', "Clear List"), self, triggered=self._AH_ClearList)

        self.model.itemAdded.connect(self.update_status)
        self.model.itemRemoved.connect(self.update_status)
        self.model.modelReset.connect(self.update_status)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkFileTransferWillRetry')
        notification_center.add_observer(self, name='BlinkFileTransferDidEnd')

    def show(self, activate=True):
        settings = BlinkSettings()
        makedirs(settings.transfers_directory.normalized)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        super(FileTransferWindow, self).show()
        self.raise_()
        if activate:
            self.activateWindow()

    def update_status(self):
        total = len(self.model.items)
        active = len([item for item in self.model.items if not item.ended])
        text = '%d %s' % (total, translate('filetransfer_window', 'transfer') if total == 1 else translate('filetransfer_window', 'transfers'))
        if active > 0:
            text += translate('filetransfer_window', ' (%d active)') % active
        self.status_label.setText(text)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkFileTransferWillRetry(self, notification):
        self.update_status()

    def _NH_BlinkFileTransferDidEnd(self, notification):
        self.update_status()

    def _SH_ContextMenuRequested(self, pos):
        menu = self.context_menu
        menu.clear()
        index = self.listview.indexAt(pos)
        if index.isValid():
            item = index.data(Qt.ItemDataRole.UserRole)
            if item.ended:
                if not item.failed:
                    menu.addAction(self.actions.open_file)
                    menu.addAction(self.actions.open_file_folder)
                elif item.direction == 'outgoing':
                    menu.addAction(self.actions.retry_transfer)
                menu.addAction(self.actions.remove_entry)
            else:
                if item.direction == 'outgoing':
                    menu.addAction(self.actions.open_file)
                    menu.addAction(self.actions.open_file_folder)
                menu.addAction(self.actions.cancel_transfer)
            menu.addSeparator()
            menu.addAction(self.actions.open_downloads_folder)
            menu.addAction(self.actions.clear_list)
        elif self.model.rowCount() > 0:
            menu.addAction(self.actions.open_downloads_folder)
            menu.addAction(self.actions.clear_list)
        else:
            menu.addAction(self.actions.open_downloads_folder)
        menu.exec(self.mapToGlobal(pos))

    def _AH_OpenFile(self):
        item = self.listview.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.filename))

    def _AH_OpenFileFolder(self):
        item = self.listview.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(item.filename)))

    def _AH_CancelTransfer(self):
        item = self.listview.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        item.end()

    def _AH_RetryTransfer(self):
        item = self.listview.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        item.retry()

    def _AH_RemoveEntry(self):
        item = self.listview.selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        self.model.removeItem(item)

    def _AH_OpenTransfersFolder(self):
        settings = BlinkSettings()
        QDesktopServices.openUrl(QUrl.fromLocalFile(settings.transfers_directory.normalized))

    def _AH_ClearList(self):
        self.model.clear_ended()

del ui_class, base_class

