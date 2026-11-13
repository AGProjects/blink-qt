"""The PTZ presets of a camera, one window per camera (its tile's menu -> Edit presets...), as in AG CuteCams.

Point the camera with the pad on its picture, then: Save as new... (a name asked), Move here (the
one picked takes the current position, its name kept), Rename..., Delete. Double-click, or Go to,
moves the camera to one. Works for an RTSP camera over ONVIF (blink.webrtc.ptz: the presets are
the camera's, the names Blink's, kept with the remembered source as preset_names, since ONVIF has
no rename) and a USB camera over V4L2 (blink.webrtc.uvcptz: positions and names both kept by
Blink). Every change asks the camera for its presets again, so the list is what the camera has; the
tiles showing the camera are told (ptz.listeners) so their preset pills follow.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                             QPushButton, QVBoxLayout)

from blink.logging import ActivityLog
from blink.webrtc.ptz import preset_label


__all__ = ['PresetsDialog', 'edit_presets', 'set_preset_name']


_dialogs = {}       # camera key: its open window


def _key(ptz, camera):
    return camera.get('url') or ptz.host


def set_preset_name(ptz, camera, token, name):
    """The name of a preset as shown by Blink; None forgets it (the preset was deleted)."""
    if hasattr(ptz, 'rename_preset'):       # V4L2: the name is kept with the position
        if name:
            ptz.rename_preset(token, name)
        return
    preset_label(camera, token, '')        # the names remembered, when the tile has not them yet
    names = dict(camera.get('preset_names') or {})
    if name:
        names[token] = name
    else:
        names.pop(token, None)
    camera['preset_names'] = names      # the tile's own settings: its pills show it at once
    if camera.get('url'):
        from blink.webrtc.extravideo import set_preset_name as remember
        remember(camera['url'], token, name)


def _changed(ptz):
    for listener in list(ptz.listeners):
        listener()


def edit_presets(ptz, camera, title, parent=None):
    """The presets window of the camera (one per camera)."""
    key = _key(ptz, camera)
    dialog = _dialogs.get(key)
    if dialog is not None and dialog.ptz is not ptz:        # the camera was found again: another Ptz
        dialog.close()
        dialog = None
    if dialog is None:
        dialog = _dialogs[key] = PresetsDialog(ptz, camera, title, parent)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.destroyed.connect(lambda *args, key=key, dialog=dialog: _dialogs.pop(key, None) if _dialogs.get(key) is dialog else None)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


class PresetsDialog(QDialog):
    def __init__(self, ptz, camera, title, parent=None):
        super(PresetsDialog, self).__init__(parent, Qt.WindowType.Window)
        self.ptz = ptz
        self.camera = camera
        self.setWindowTitle(f'Presets — {title}')
        self.resize(380, 360)
        self.busy = False

        layout = QVBoxLayout(self)
        hint = QLabel('Point the camera with the pad on its picture, then save its view.', self)
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.list = QListWidget(self)
        self.list.itemDoubleClicked.connect(lambda item: self._go())
        self.list.currentRowChanged.connect(lambda row: self._update())
        row.addWidget(self.list, 1)
        buttons = QVBoxLayout()
        self.go_button = self._button('Go to', self._go, buttons, 'Move the camera to this preset')
        buttons.addSpacing(8)
        self.new_button = self._button('Save as new…', self.save_new, buttons, 'Save the current view as a new preset')
        self.move_button = self._button('Move here', self._move_here, buttons, 'This preset takes the current view (its name is kept)')
        self.rename_button = self._button('Rename…', self._rename, buttons, 'The name shown in Blink')
        self.delete_button = self._button('Delete', self._delete, buttons, 'Delete this preset')
        buttons.addStretch(1)
        row.addLayout(buttons)
        layout.addLayout(row, 1)
        self.status = QLabel(self)
        self.status.setEnabled(False)
        layout.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self._show()
        self._refresh()

    def _button(self, text, slot, layout, tip):
        button = QPushButton(text, self)
        button.setAutoDefault(False)
        button.setToolTip(tip)
        button.clicked.connect(lambda checked: slot())
        layout.addWidget(button)
        return button

    # The list

    def _show(self, select=None):
        current = self._token()
        self.list.blockSignals(True)
        self.list.clear()
        for token, name in self.ptz.presets:
            item = QListWidgetItem(preset_label(self.camera, token, name))
            item.setData(Qt.ItemDataRole.UserRole, token)
            if not hasattr(self.ptz, 'rename_preset'):
                item.setToolTip(f'Number {token} on the camera' + (f', named “{name}” there' if name else ''))
            self.list.addItem(item)
            if token == (select or current):
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self._update()

    def _token(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _name(self, token):
        return preset_label(self.camera, token, dict(self.ptz.presets).get(token, ''))

    def _update(self):
        picked = self._token() is not None and not self.busy
        for button in (self.go_button, self.move_button, self.rename_button, self.delete_button):
            button.setEnabled(picked)
        self.new_button.setEnabled(not self.busy)

    def _working(self, text):
        self.busy = True
        self.status.setText(text)
        self._update()

    def _refresh(self, select=None, done_text=''):
        """Ask the camera for its presets again, then show them."""
        self._working('Asking the camera for its presets…')

        def answered(presets, error):
            self.busy = False
            self.status.setText(f'The camera did not answer: {error}' if error else done_text)
            self._show(select)
            _changed(self.ptz)
        self.ptz.refresh_presets(answered)

    # Actions

    @staticmethod
    def _touched():
        """Moved by hand: the trackers leave the camera alone for a while."""
        from blink.webrtc.tracker import ManualPause
        ManualPause.instance().touched()

    def _go(self):
        token = self._token()
        if token is None:
            return
        self._touched()
        self.status.setText(f'Going to {self._name(token)}…')
        self.ptz.goto_preset(token, lambda answer, error: self.status.setText(f'Could not go there: {error}' if error else ''))
        _changed(self.ptz)

    def save_new(self):
        name, ok = QInputDialog.getText(self, 'Save preset', 'Name of the current view:', text=f'Preset {len(self.ptz.presets) + 1}')
        name = name.strip()
        if not ok or not name:
            return
        self._working(f'Saving “{name}”…')

        def saved(token, error):
            self.busy = False
            if error or not token:
                self.status.setText(f'The camera did not save it: {error or "no preset number in its answer"}')
                self._update()
                return
            set_preset_name(self.ptz, self.camera, token, name)
            self.ptz.active_preset = token
            ActivityLog().info(f'[ptz] {self.ptz.host}: preset {token} saved as {name}')
            self._refresh(select=token, done_text=f'Saved: {name}')
        self.ptz.set_preset(name, done=saved)

    def _move_here(self):
        token = self._token()
        if token is None:
            return
        name = self._name(token)
        if QMessageBox.question(self, 'Move preset', f'“{name}” takes the camera\'s current view?') != QMessageBox.StandardButton.Yes:
            return
        self._working(f'Moving “{name}”…')

        def saved(answer_token, error):
            self.busy = False
            if error:
                self.status.setText(f'The camera did not save it: {error}')
                self._update()
                return
            self.ptz.active_preset = token
            self._refresh(select=token, done_text=f'Moved: {name}')
        self.ptz.set_preset(name, token=token, done=saved)

    def _rename(self):
        token = self._token()
        if token is None:
            return
        name, ok = QInputDialog.getText(self, 'Rename preset', 'Name:', text=self._name(token))
        name = name.strip()
        if not ok or not name:
            return
        set_preset_name(self.ptz, self.camera, token, name)
        self._show(token)
        self.status.setText(f'Renamed: {name}')
        _changed(self.ptz)

    def _delete(self):
        token = self._token()
        if token is None:
            return
        name = self._name(token)
        if QMessageBox.question(self, 'Delete preset', f'Delete “{name}”?') != QMessageBox.StandardButton.Yes:
            return
        self._working(f'Deleting “{name}”…')

        def removed(answer, error):
            self.busy = False
            if error:
                self.status.setText(f'The camera did not delete it: {error}')
                self._update()
                return
            set_preset_name(self.ptz, self.camera, token, None)
            if self.ptz.active_preset == token:
                self.ptz.active_preset = None
            self._refresh(done_text=f'Deleted: {name}')
        self.ptz.remove_preset(token, removed)
