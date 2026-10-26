"""Add network camera: a camera of the local network (RTSP) as a video source for video rooms.

The cameras found on the network (ONVIF WS-Discovery, Bonjour _rtsp._tcp) are listed; picking
one fills in its name and address (an ONVIF camera's RTSP address is asked from the camera when
tested, with the user and password), and the user and password a camera of the same host was
used with before. Test pulls a picture: only a camera that gave one can be added, and it is
remembered (blink.webrtc.extravideo.remember_source) with its password.
"""

import threading

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QPushButton, QSizePolicy, QVBoxLayout)
from urllib.parse import urlsplit

from blink.logging import ActivityLog
from blink.util import call_in_gui_thread


__all__ = ['NetworkCameraDialog']


def _host(url):
    try:
        return urlsplit(url).hostname or ''
    except ValueError:
        return ''


class NetworkCameraDialog(QDialog):
    def __init__(self, label='', parent=None):
        super(NetworkCameraDialog, self).__init__(parent)
        from blink.webrtc.extravideo import saved_sources
        self.setWindowTitle('Add network camera')
        self.setMinimumWidth(520)
        self.setMaximumWidth(720)
        self.saved = saved_sources()
        self.found = []
        self.xaddr = None           # the ONVIF device service of the camera picked from the list
        self.tested = None          # (url, user, password) that gave a picture
        self._closed = False
        self._searching = False

        layout = QVBoxLayout(self)
        found_header = QHBoxLayout()
        found_header.addWidget(QLabel('Cameras on your network', self))
        found_header.addStretch(1)
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText('Filter')
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(160)
        self.filter_edit.textChanged.connect(self._filter)
        found_header.addWidget(self.filter_edit)
        self.sort_combo = QComboBox(self)
        self.sort_combo.addItems(['By name', 'By address'])
        self.sort_combo.setToolTip('The cameras tested before come first, with the name given to them')
        self.sort_combo.currentIndexChanged.connect(lambda index: self._show_found())
        found_header.addWidget(self.sort_combo)
        self.search_button = QPushButton('Search again', self)
        self.search_button.setAutoDefault(False)
        self.search_button.clicked.connect(self._search)
        found_header.addWidget(self.search_button)
        layout.addLayout(found_header)
        self.found_list = QListWidget(self)
        self.found_list.setMaximumHeight(160)
        self.found_list.currentItemChanged.connect(self._SH_FoundPicked)
        layout.addWidget(self.found_list)
        self.found_status = QLabel(self)
        self.found_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.found_status.setEnabled(False)
        layout.addWidget(self.found_status)

        form = QFormLayout()
        self.label_edit = QLineEdit(label, self)
        self.label_edit.setPlaceholderText('Front door')
        self.label_edit.editingFinished.connect(self._rename)
        form.addRow('Name', self.label_edit)
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText('rtsp://192.168.1.10:554/stream1')
        form.addRow('Address', self.url_edit)
        self.user_edit = QLineEdit(self)
        form.addRow('User', self.user_edit)
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('Password', self.password_edit)
        self.resolution_combo = QComboBox(self)
        from blink.webrtc.extravideo import DEFAULT_RESOLUTION, RESOLUTIONS
        for name, width, height, bitrate in RESOLUTIONS:
            self.resolution_combo.addItem(f'{width}x{height}, about {bitrate} kbit/s', name)
        self.resolution_combo.setCurrentIndex(self.resolution_combo.findData(DEFAULT_RESOLUTION))
        self.resolution_combo.setToolTip('What the camera is sent to the conference at; smaller takes less of your upload')
        form.addRow('Send at', self.resolution_combo)
        layout.addLayout(form)

        test_row = QHBoxLayout()
        self.test_button = QPushButton('Test', self)
        self.test_button.setAutoDefault(False)
        self.test_button.clicked.connect(self._test)
        test_row.addWidget(self.test_button, 0, Qt.AlignmentFlag.AlignTop)
        self.test_status = QLabel('Test the camera: it is added once a picture comes from it.', self)
        self.test_status.setWordWrap(True)
        self.test_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)     # a long error or address does not widen the window
        test_row.addWidget(self.test_status, 1)
        layout.addLayout(test_row)
        self.preview = QLabel(self)
        self.preview.setFixedSize(QSize(320, 180))
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet('background: black; color: gray;')
        layout.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignHCenter)
        note = QLabel('Video only, 10 fps. The camera is remembered with its password.', self)
        note.setWordWrap(True)
        note.setEnabled(False)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.add_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.add_button.setText('Add')
        buttons.accepted.connect(self._add)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for edit in (self.url_edit, self.user_edit, self.password_edit):
            edit.textChanged.connect(self._update)
        self.url_edit.textEdited.connect(lambda text: setattr(self, 'xaddr', None))     # typed: no longer the ONVIF camera picked
        self._update()
        self._show_found()      # the cameras tested before, while the network is searched
        self._search()

    def done(self, result):
        self._closed = True
        super(NetworkCameraDialog, self).done(result)

    # Discovery

    def _search(self):
        if self._searching:
            return
        self._searching = True
        self.search_button.setEnabled(False)
        self.found_status.setText('Looking for cameras (ONVIF, Bonjour)…')

        def work():
            from blink.webrtc.camerafinder import discover
            try:
                cameras = discover()
            except Exception as e:
                ActivityLog().warning(f'[cameras] Discovery failed: {e}')
                cameras = []
            call_in_gui_thread(self._found, cameras)
        threading.Thread(target=work, name='camera discovery', daemon=True).start()

    def _found(self, cameras):
        self._searching = False
        if self._closed:
            return
        self.search_button.setEnabled(True)
        self.found = cameras
        self._show_found()
        tested = sum(1 for camera in cameras if self._saved_for(camera.host) is not None)
        self.found_status.setText((f'{len(cameras)} found' + (f', {tested} tested before' if tested else '')) if cameras else
                                  'None found; enter the camera\'s RTSP address')

    def _saved_for(self, host):
        return next((source for source in self.saved if _host(source['url']) == host), None)

    def _show_found(self):
        """The cameras found and the ones tested before (found now or not): those tested first, under the name given to them."""
        from blink.webrtc.camerafinder import FoundCamera
        cameras = list(self.found)
        hosts = {camera.host for camera in cameras}
        for source in self.saved:
            host = _host(source['url'])
            if host and host not in hosts:
                hosts.add(host)
                cameras.append(FoundCamera(source.get('label') or host, host, 'saved', url=source['url']))

        def address(host):
            try:
                return (0, tuple(int(part) for part in host.split('.')))
            except ValueError:
                return (1, host)

        def name(camera):
            saved = self._saved_for(camera.host)
            return (saved or {}).get('label') or camera.name or camera.host
        if self.sort_combo.currentIndex() == 1:
            key = lambda camera: (self._saved_for(camera.host) is None, address(camera.host))
        else:
            key = lambda camera: (self._saved_for(camera.host) is None, name(camera).lower(), address(camera.host))
        current = self.found_list.currentItem()
        current_host = current.data(Qt.ItemDataRole.UserRole).host if current is not None else None
        self.found_list.blockSignals(True)
        self.found_list.clear()
        for camera in sorted(cameras, key=key):
            saved = self._saved_for(camera.host)
            how = {'onvif': 'ONVIF', 'mdns': 'Bonjour'}.get(camera.how, 'not found now')
            details = [camera.host, how]
            if camera.hardware and camera.hardware != name(camera):
                details.insert(0, camera.hardware)
            item = QListWidgetItem(('\u2713 ' if saved else '    ') + name(camera) + '  \u00b7  ' + ', '.join(details), self.found_list)
            item.setToolTip(('Tested before: ' + saved['url']) if saved else (camera.url or camera.xaddr or camera.host))
            if saved is not None:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            item.setData(Qt.ItemDataRole.UserRole, camera)
            if camera.host == current_host:
                self.found_list.setCurrentItem(item)
        self.found_list.blockSignals(False)
        self._filter(self.filter_edit.text())

    def _rename(self):
        """The name of a camera tested before, changed: kept, and shown in the list."""
        from blink.webrtc.extravideo import rename_source, saved_sources
        item = self.found_list.currentItem()
        label = self.label_edit.text().strip()
        if item is None or not label:
            return
        saved = self._saved_for(item.data(Qt.ItemDataRole.UserRole).host)
        if saved is None or saved.get('label') == label or saved['url'] != self.url_edit.text().strip():
            return
        rename_source(saved['url'], label)
        self.saved = saved_sources()
        self._show_found()

    def _filter(self, text):
        text = text.strip().lower()
        for number in range(self.found_list.count()):
            item = self.found_list.item(number)
            item.setHidden(bool(text) and text not in item.text().lower() and text not in item.toolTip().lower())

    def _SH_FoundPicked(self, item, previous=None):
        if item is None:
            return
        camera = item.data(Qt.ItemDataRole.UserRole)
        saved = next((source for source in self.saved if _host(source['url']) == camera.host), None)
        self.label_edit.setText((saved or {}).get('label') or camera.name or camera.host)
        if saved is not None:
            self.url_edit.setText(saved['url'])
            self.user_edit.setText(saved.get('user') or '')
            self.password_edit.setText(saved.get('password') or '')
            index = self.resolution_combo.findData(saved.get('resolution'))
            if index >= 0:
                self.resolution_combo.setCurrentIndex(index)
        else:
            self.url_edit.setText(camera.url or '')
        self.xaddr = camera.xaddr if camera.how == 'onvif' else None
        if not self.url_edit.text() and self.xaddr:
            self.url_edit.setPlaceholderText('asked from the camera (ONVIF) when tested')
            self.test_status.setText('Enter the camera\'s user and password, then Test.')

    # Test

    def _fields(self):
        return self.url_edit.text().strip(), self.user_edit.text().strip(), self.password_edit.text()

    def _update(self):
        url, user, password = self._fields()
        self.test_button.setEnabled(url.lower().startswith(('rtsp://', 'rtsps://')) or (not url and self.xaddr is not None))
        self.add_button.setEnabled(self.tested is not None and self.tested == (url, user, password))

    def _test(self):
        url, user, password = self._fields()
        xaddr = self.xaddr if not url else None
        self.test_button.setEnabled(False)
        self.tested = None
        self.preview.clear()
        self.test_status.setText('Asking the camera for its stream…' if xaddr else 'Connecting…')

        def work():
            from blink.webrtc.camerafinder import onvif_stream_uri, test_stream
            try:
                stream_url = onvif_stream_uri(xaddr, user, password) if xaddr else url
                image, width, height = test_stream(stream_url, user, password)
            except Exception as e:
                ActivityLog().info(f'[cameras] Test of {url or xaddr} failed: {e}')
                call_in_gui_thread(self._tested, None, url, str(e), user, password)
            else:
                ActivityLog().info(f'[cameras] Test of {stream_url}: {width}x{height}')
                call_in_gui_thread(self._tested, image, stream_url, f'{width}x{height}' if width else '', user, password)
        threading.Thread(target=work, name='camera test', daemon=True).start()

    def _tested(self, image, url, detail, user, password):
        if self._closed:
            return
        if image is None:
            self.test_status.setText(f'No picture: {detail}')
        else:
            self.url_edit.setText(url)      # an ONVIF camera's stream address, now known
            self.preview.setPixmap(QPixmap.fromImage(image).scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.test_status.setText(f'The camera works ({detail} video)' if detail else 'The camera works')
            self.tested = (url, user, password)
        self._update()

    # The result

    def label(self):
        return self.label_edit.text().strip() or _host(self.url_edit.text()) or 'Camera'

    def source(self):
        url, user, password = self._fields()
        return dict(kind='rtsp', url=url, user=user, password=password, resolution=self.resolution_combo.currentData())

    def _add(self):
        from blink.webrtc.extravideo import remember_source
        source = self.source()
        remember_source(dict(source, label=self.label()))
        self.accept()
