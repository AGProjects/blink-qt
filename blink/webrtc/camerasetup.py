"""Network cameras (RTSP) as video sources for video rooms: Find on network..., Add network camera
(the Join Conference panel), and Devices -> RTSP cameras -> Manage cameras..., where Save keeps
the window open and Forget removes a saved camera.

The cameras found on the network (ONVIF WS-Discovery, Bonjour _rtsp._tcp) and the ones saved
are listed (saved first, in bold, under the name given to them; those discovery does not list
are "online" or "offline" by whether their RTSP port takes a connection: not all cameras answer
discovery), with a filter, a sort and "Only my local network". A Verified column ticks the saved
cameras a picture came from with the address, user and password saved. Picking one fills in its
name and address (an ONVIF camera's RTSP address is asked from the camera when tested, as it is
when only an IP address is typed), and its user and password when saved. Test pulls a picture
and shows it. Any rtsp:// address (or a bare IP) can be saved, tested or not (the camera may be
offline now); it is remembered (blink.webrtc.extravideo.remember_source) with its password.
rtsp://user:password@host/... typed or pasted: the user and password go into their fields.
Renaming a saved camera saves it.
"""

import threading

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout)
from urllib.parse import unquote, urlsplit, urlunsplit

from blink.logging import ActivityLog
from blink.util import call_in_gui_thread
from blink.widgets.lineedit import add_password_reveal


__all__ = ['NetworkCameraDialog', 'rtsp_address', 'split_credentials', 'manage_cameras']


LOCAL_ONLY_KEY = 'videoroom/rtsp_local_only'     # Only my local network, as last set
TEST_HINT = 'Test pulls a picture from the camera; it can be saved untested too.'


def _host(url):
    try:
        return urlsplit(url).hostname or ''
    except ValueError:
        return ''


def rtsp_address(text):
    """The camera's address as typed: an rtsp:// or rtsps:// URL, or a bare host or IP (with a port and a path or
    not), taken as rtsp://. '' when it is neither."""
    text = (text or '').strip()
    if not text:
        return ''
    if '://' not in text:
        text = 'rtsp://' + text
    return text if text.lower().startswith(('rtsp://', 'rtsps://')) and _host(text) else ''


def split_credentials(text):
    """rtsp://user:password@host/... as typed: (the address without them, user, password); user None when the
    address has none."""
    url = rtsp_address(text)
    if not url:
        return text, None, None
    try:
        parts = urlsplit(url)
        user, password, host, port = parts.username, parts.password, parts.hostname or '', parts.port
    except ValueError:
        return text, None, None
    if user is None:
        return url, None, None
    netloc = (f'[{host}]' if ':' in host else host) + (f':{port}' if port else '')
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), unquote(user), unquote(password or '')


def _bare(url):
    """Whether the address has no path: only the host (and port) was given."""
    try:
        parts = urlsplit(url)
        return parts.path in ('', '/') and not parts.query
    except ValueError:
        return False


def _ui_settings():
    from blink.resources import ui_settings
    return ui_settings()


_manager = None


def manage_cameras(url='', parent=None):
    """Devices -> RTSP cameras: the one window to find, test, save, rename and forget cameras; url: the saved
    camera to show selected, or empty."""
    global _manager
    if _manager is None or _manager.closed:
        _manager = NetworkCameraDialog(parent=parent, managing=True)
        _manager.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        _manager.show()
    if url:
        _manager.select_host(_host(url))
    _manager.raise_()
    _manager.activateWindow()
    return _manager


class NetworkCameraDialog(QDialog):
    def __init__(self, label='', parent=None, managing=False):
        super(NetworkCameraDialog, self).__init__(parent)
        from blink.webrtc.extravideo import saved_sources
        self.managing = managing
        self.setWindowTitle('RTSP cameras' if managing else 'Add network camera')
        self.setMinimumWidth(560)
        self.setMaximumWidth(760)
        self.saved = saved_sources()
        self.found = []
        self.online = {}            # saved address: True/False once checked, for those not discovered
        self.local = {}             # host: whether it is on one of this computer's networks
        self.macs = {}              # saved address: the camera's MAC address, when online and on our networks
        self.xaddr = None           # the ONVIF device service of the camera picked from the list
        self.tested = None          # (url, user, password) that gave a picture
        self.testing = False
        self.test_id = 0            # the test running: an answer of an earlier one (the camera changed since) is dropped
        self.editing = None         # the saved camera the fields are of: Save replaces it; None: a new camera
        self.filled = None          # (user, password) filled in from a saved camera
        self.closed = False
        self._searching = False

        layout = QVBoxLayout(self)
        found_header = QHBoxLayout()
        found_header.addWidget(QLabel('Cameras on your network', self))
        found_header.addStretch(1)
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText('Filter')
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(160)
        self.filter_edit.textChanged.connect(lambda text: self._show_found())
        found_header.addWidget(self.filter_edit)
        self.sort_combo = QComboBox(self)
        self.sort_combo.addItems(['By name', 'By address'])
        self.sort_combo.setToolTip('The saved cameras come first, with the name given to them')
        self.sort_combo.currentIndexChanged.connect(lambda index: self._show_found())
        found_header.addWidget(self.sort_combo)
        self.search_button = QPushButton('Search again', self)
        self.search_button.setAutoDefault(False)
        self.search_button.clicked.connect(self._search)
        found_header.addWidget(self.search_button)
        layout.addLayout(found_header)
        self.found_list = QTreeWidget(self)
        self.found_list.setColumnCount(2)
        self.found_list.setHeaderLabels(['Camera', 'Verified'])
        self.found_list.setRootIsDecorated(False)
        self.found_list.setUniformRowHeights(True)
        self.found_list.setMaximumHeight(180)
        header = self.found_list.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 70)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.found_list.headerItem().setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
        self.found_list.currentItemChanged.connect(self._SH_FoundPicked)
        layout.addWidget(self.found_list)
        status_row = QHBoxLayout()
        self.found_status = QLabel(self)
        self.found_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.found_status.setEnabled(False)
        status_row.addWidget(self.found_status, 1)
        self.local_only = QCheckBox('Only my local network', self)
        self.local_only.setToolTip('Only the cameras whose address is on one of this computer\'s networks (its IP addresses and netmasks)')
        try:
            self.local_only.setChecked(_ui_settings().value(LOCAL_ONLY_KEY, False, type=bool))
        except Exception:
            pass
        self.local_only.toggled.connect(self._SH_LocalOnlyToggled)
        status_row.addWidget(self.local_only)
        layout.addLayout(status_row)

        self.mode_label = QLabel(self)
        font = self.mode_label.font()
        font.setBold(True)
        self.mode_label.setFont(font)
        layout.addWidget(self.mode_label)
        form = QFormLayout()
        self.label_edit = QLineEdit(label, self)
        self.label_edit.setPlaceholderText('Front door')
        self.label_edit.editingFinished.connect(self._rename)
        form.addRow('Name', self.label_edit)
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText('rtsp://192.168.1.10:554/stream1')
        self.url_edit.setToolTip('An IP address is enough: the stream is then asked from the camera (ONVIF)')
        form.addRow('Address', self.url_edit)
        self.user_edit = QLineEdit(self)
        form.addRow('User', self.user_edit)
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        add_password_reveal(self.password_edit)
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
        self.test_status = QLabel(TEST_HINT, self)
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

        # no Return key: Return in the address only takes the user and password out of it (it can be saved untested)
        buttons = QDialogButtonBox(self)
        self.forget_button = None
        if managing:
            self.add_button = buttons.addButton('Save', QDialogButtonBox.ButtonRole.ActionRole)
            self.add_button.clicked.connect(self._add)
            self.forget_button = buttons.addButton('Forget', QDialogButtonBox.ButtonRole.ActionRole)
            self.forget_button.setToolTip('Remove this camera, and its password')
            self.forget_button.clicked.connect(self._forget)
            new_button = buttons.addButton('New', QDialogButtonBox.ButtonRole.ActionRole)
            new_button.setToolTip('Empty the fields for another camera')
            new_button.clicked.connect(self._new_camera)
            close_button = buttons.addButton(QDialogButtonBox.StandardButton.Close)
            close_button.clicked.connect(self.reject)
        else:
            self.add_button = buttons.addButton('Add', QDialogButtonBox.ButtonRole.ActionRole)
            self.add_button.clicked.connect(self._add)
            cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
            cancel_button.clicked.connect(self.reject)
        for button in buttons.buttons():
            button.setAutoDefault(False)
            button.setDefault(False)
        layout.addWidget(buttons)

        for edit in (self.url_edit, self.user_edit, self.password_edit):
            edit.textChanged.connect(self._fields_changed)
        self.url_edit.textEdited.connect(lambda text: setattr(self, 'xaddr', None) if text else None)     # typed: no longer the ONVIF camera picked
        self.url_edit.editingFinished.connect(self._take_credentials)
        self._update()
        self._show_found()      # the saved cameras, while the network is searched
        self._check_online()
        self._search()

    def done(self, result):
        self.closed = True
        if self.testing:
            self.test_id += 1   # its answer is dropped
        super(NetworkCameraDialog, self).done(result)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.url_edit.hasFocus():
                self._take_credentials()
            return      # Return does not add or close
        super(NetworkCameraDialog, self).keyPressEvent(event)

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
        if self.closed:
            return
        self.search_button.setEnabled(True)
        self.found = cameras
        self._classify_local()
        self._show_found()
        saved = sum(1 for camera in cameras if self._saved_for(camera.host) is not None)
        self.found_status.setText((f'{len(cameras)} found' + (f', {saved} saved' if saved else '')) if cameras else
                                  'None found; enter the camera\'s address')

    def _check_online(self):
        """Not all cameras answer discovery: the saved ones are asked on their RTSP port whether they are online."""
        from blink.webrtc.camerafinder import check_online
        for source in self.saved:
            url = source['url']
            self.online.pop(url, None)

            def work(url=url):
                try:
                    online, mac = check_online(url)
                except Exception:
                    online, mac = False, None
                call_in_gui_thread(self._online_checked, url, online, mac)
            threading.Thread(target=work, name=f'camera online {_host(url)}', daemon=True).start()
        self._classify_local()

    def _online_checked(self, url, online, mac):
        if self.closed:
            return
        self.online[url] = online
        if mac:
            self.macs[url] = mac
        self._show_found()

    def _classify_local(self):
        """Which of the cameras listed are on this computer's networks, for Only my local network."""
        hosts = {camera.host for camera in self.found} | {_host(source['url']) for source in self.saved}
        hosts = {host for host in hosts if host and host not in self.local}
        if not hosts:
            return

        def work():
            from blink.webrtc.camerafinder import classify_local
            try:
                answer = classify_local(hosts)
            except Exception as e:
                ActivityLog().warning(f'[cameras] Cannot tell which cameras are on the local network: {e}')
                return
            call_in_gui_thread(self._local_classified, answer)
        threading.Thread(target=work, name='camera networks', daemon=True).start()

    def _local_classified(self, answer):
        if self.closed or not answer:
            return
        self.local.update(answer)
        self._show_found()

    def _SH_LocalOnlyToggled(self, checked):
        try:
            _ui_settings().setValue(LOCAL_ONLY_KEY, checked)
        except Exception:
            pass
        self._show_found()

    def _saved_for(self, host):
        return next((source for source in self.saved if _host(source['url']) == host), None)

    def _camera_name(self, camera):
        saved = self._saved_for(camera.host)
        return (saved or {}).get('label') or camera.name or camera.host

    def _show_found(self):
        """The cameras found and the ones saved (found now or not): those saved first, under the name given to them."""
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

        if self.sort_combo.currentIndex() == 1:
            key = lambda camera: (self._saved_for(camera.host) is None, address(camera.host))
        else:
            key = lambda camera: (self._saved_for(camera.host) is None, self._camera_name(camera).lower(), address(camera.host))
        current = self.found_list.currentItem()
        current_host = current.data(0, Qt.ItemDataRole.UserRole).host if current is not None else None
        text = self.filter_edit.text().strip().lower()
        local_only = self.local_only.isChecked()
        self.found_list.blockSignals(True)
        self.found_list.clear()
        for camera in sorted(cameras, key=key):
            saved = self._saved_for(camera.host)
            how = {'onvif': 'ONVIF', 'mdns': 'Bonjour'}.get(camera.how)
            if how is None:     # saved, not discovered (not all cameras answer discovery): online when its port answers
                online = self.online.get(saved['url']) if saved else None
                how = 'online' if online else ('offline' if online is False else 'checking…')
            details = [camera.host, how]
            mac = self.macs.get(saved['url']) if saved else None
            if mac:
                details.insert(1, mac)
            if camera.hardware and camera.hardware != self._camera_name(camera):
                details.insert(0, camera.hardware)
            title = self._camera_name(camera) + '  ·  ' + ', '.join(details)
            tooltip = ('Saved: ' + saved['url']) if saved else (camera.url or camera.xaddr or camera.host)
            if local_only and self.local.get(camera.host) is False:
                continue        # not on this computer's networks (shown while that is not known yet)
            if text and text not in title.lower() and text not in tooltip.lower():
                continue
            verified = bool(saved and saved.get('verified'))
            item = QTreeWidgetItem(self.found_list, [title, '✓' if verified else ''])
            item.setToolTip(0, tooltip)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
            if saved is not None:
                item.setToolTip(1, 'A picture came from it with the address, user and password saved' if verified else
                                   'Not verified: saved without a picture from it with these details. Test it.')
                font = QFont(item.font(0))
                font.setBold(True)
                item.setFont(0, font)
            item.setData(0, Qt.ItemDataRole.UserRole, camera)
            if camera.host == current_host:
                self.found_list.setCurrentItem(item)
        self.found_list.blockSignals(False)

    def select_host(self, host, fill=True):
        """Select the line of that host; fill: its saved address, user and password go into the fields."""
        for number in range(self.found_list.topLevelItemCount()):
            item = self.found_list.topLevelItem(number)
            if item.data(0, Qt.ItemDataRole.UserRole).host == host:
                self.found_list.blockSignals(not fill)
                try:
                    self.found_list.setCurrentItem(item)
                finally:
                    self.found_list.blockSignals(False)
                self.found_list.scrollToItem(item)
                return

    def _rename(self):
        """The name of a saved camera, changed: kept, and shown in the list."""
        from blink.webrtc.extravideo import rename_source, saved_sources
        label = self.label_edit.text().strip()
        saved = self.editing
        if saved is None or not label or saved.get('label') == label or saved['url'] != self._fields()[0]:
            return
        rename_source(saved['url'], label)
        self.saved = saved_sources()
        self.editing = next((source for source in self.saved if source['url'] == saved['url']), None)
        self._show_found()
        self._update()

    def _SH_FoundPicked(self, item, previous=None):
        if item is None:
            return
        self._forget_test()     # another camera: not the picture of the one before
        camera = item.data(0, Qt.ItemDataRole.UserRole)
        saved = self._saved_for(camera.host)
        self.label_edit.setText((saved or {}).get('label') or camera.name or camera.host)
        if saved is not None:
            self.url_edit.setText(saved['url'])
            self.user_edit.setText(saved.get('user') or '')
            self.password_edit.setText(saved.get('password') or '')
            self.filled = (saved.get('user') or '', saved.get('password') or '')
            index = self.resolution_combo.findData(saved.get('resolution'))
            if index >= 0:
                self.resolution_combo.setCurrentIndex(index)
            self.editing = saved
        else:
            self.editing = None
            self.url_edit.setText(camera.url or '')
            if self.filled is not None and self._fields()[1:] == self.filled:
                # another camera's user and password, not to be sent to this one
                self.user_edit.clear()
                self.password_edit.clear()
            self.filled = None
        self.xaddr = camera.xaddr if camera.how == 'onvif' else None
        if not self.url_edit.text() and self.xaddr:
            self.url_edit.setPlaceholderText('asked from the camera (ONVIF) when tested')
            self.test_status.setText('Enter the camera\'s user and password, then Test.')
        self._update()

    # The fields

    def _fields(self):
        """(url, user, password): the address as an rtsp:// URL ('' when it is not one, see rtsp_address)."""
        return rtsp_address(self.url_edit.text()), self.user_edit.text().strip(), self.password_edit.text()

    def _take_credentials(self):
        """rtsp://user:password@host/... typed or pasted: the user and password go into their fields."""
        url, user, password = split_credentials(self.url_edit.text())
        if user is None:
            return
        self.url_edit.setText(url)
        self.user_edit.setText(user)
        self.password_edit.setText(password)
        self._update()

    def _fields_changed(self, *args):
        if self.tested is not None and self._fields() != self.tested:
            self._forget_test()     # the picture was of other details
        self._update()

    def _update(self):
        """Test: always, but while a test runs (the address is checked when pressed); Add/Save: an rtsp:// address,
        tested or not (the camera may be offline now); Forget: a saved camera is being edited."""
        url, user, password = self._fields()
        self.test_button.setEnabled(not self.testing)
        self.add_button.setEnabled(bool(url))
        if self.forget_button is not None:
            self.forget_button.setEnabled(self.editing is not None)
        if self.editing is not None:
            self.mode_label.setText('Editing “%s”' % (self.editing.get('label') or _host(self.editing['url'])))
            self.mode_label.setToolTip('Saving replaces this camera, also when its address is changed. New empties the fields for another one.'
                                       if self.managing else 'Adding it also saves the changes made to this camera')
        else:
            self.mode_label.setText('New camera')
            self.mode_label.setToolTip('Saving adds a camera (one with the same address is replaced)')
        if self.managing:
            self.add_button.setText('Update' if self.editing is not None else 'Save')

    # Test

    def _forget_test(self):
        """The preview and the test's result go: they are of another camera, or of other details."""
        self.testing = False
        self.test_id += 1
        self.tested = None
        self.preview.clear()
        self.test_status.setText(TEST_HINT)

    def _test(self):
        self._take_credentials()
        url, user, password = self._fields()
        typed = self.url_edit.text().strip()
        xaddr = self.xaddr if not typed else None
        if not xaddr and not url:
            self.test_status.setText('Enter the camera\'s IP address or RTSP address (rtsp://192.168.1.10:554/stream1), or pick a camera from the list.' if not typed
                                     else 'Not a camera address: an IP address, or rtsp://192.168.1.10:554/stream1')
            return
        self.testing = True
        self.test_id += 1
        test_id = self.test_id
        self.tested = None
        self.preview.clear()
        bare = not xaddr and _bare(url)
        self.test_status.setText('Asking the camera for its stream…' if xaddr else 'Asking the camera for its stream (ONVIF)…' if bare else 'Connecting…')
        self._update()

        def work():
            from blink.webrtc.camerafinder import onvif_stream_uri, test_stream
            stream_url = url
            try:
                if xaddr:
                    stream_url = onvif_stream_uri(xaddr, user, password)
                elif bare:
                    # only the IP: the stream's path asked from the camera over ONVIF, else the address as it is
                    host = _host(url)
                    try:
                        stream_url = onvif_stream_uri('http://%s/onvif/device_service' % (f'[{host}]' if ':' in host else host), user, password)
                    except Exception as e:
                        ActivityLog().info(f'[cameras] {host}: no stream address over ONVIF ({e}), trying {url}')
                        stream_url = url
                image, width, height = test_stream(stream_url, user, password)
            except Exception as e:
                ActivityLog().info(f'[cameras] Test of {stream_url or xaddr} failed: {e}')
                call_in_gui_thread(self._tested, None, stream_url, str(e), user, password, test_id)
            else:
                ActivityLog().info(f'[cameras] Test of {stream_url}: {width}x{height}')
                call_in_gui_thread(self._tested, image, stream_url, f'{width}x{height}' if width else '', user, password, test_id)
        threading.Thread(target=work, name='camera test', daemon=True).start()

    def _tested(self, image, url, detail, user, password, test_id):
        if self.closed or test_id != self.test_id:
            return
        self.testing = False
        if image is None:
            self.test_status.setText(f'No picture: {detail}')
        else:
            self.url_edit.setText(url)      # an ONVIF camera's stream address, now known
            self.preview.setPixmap(QPixmap.fromImage(image).scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.test_status.setText(f'The camera works ({detail} video)' if detail else 'The camera works')
            self.tested = (url, user, password)
            saved = self.editing
            if saved is not None and not saved.get('verified') and (saved['url'], saved.get('user') or '', saved.get('password') or '') == self.tested:
                from blink.webrtc.extravideo import mark_verified, saved_sources
                mark_verified(url)
                self.saved = saved_sources()
                self.editing = next((source for source in self.saved if source['url'] == url), None)
                self._show_found()
        self._update()

    # The result

    def label(self):
        return self.label_edit.text().strip() or _host(self._fields()[0]) or 'Camera'

    def source(self):
        url, user, password = self._fields()
        return dict(kind='rtsp', url=url, user=user, password=password, resolution=self.resolution_combo.currentData())

    def _add(self):
        from blink.webrtc.extravideo import remember_source, saved_sources
        self._take_credentials()
        url, user, password = self._fields()
        if not url:
            return
        tested = self.tested == (url, user, password)
        if not tested:
            ActivityLog().info(f'[cameras] Saving {_host(url) or url} untested')
        editing = self.editing
        replaces = editing['url'] if editing is not None else None
        # verified: a picture came with these details, now or before (an edit that changed only the name or resolution)
        verified = tested or bool(editing is not None and editing.get('verified') and
                                  (editing['url'], editing.get('user') or '', editing.get('password') or '') == (url, user, password))
        remember_source(dict(self.source(), label=self.label(), verified=verified), replaces=replaces)
        self.url_edit.setText(url)      # as saved: rtsp://, when only the IP was typed
        if not self.managing:
            self.accept()
            return
        self.saved = saved_sources()
        self._check_online()
        self.editing = next((source for source in self.saved if source['url'] == url), None)
        self.filled = (user, password)
        self._show_found()
        self.select_host(_host(url), fill=False)
        if replaces:
            text = 'Updated: %s' if tested else 'Updated, not tested: %s'
        else:
            text = 'Saved: %s' if tested else 'Saved, not tested: %s'
        self.test_status.setText(text % self.label())
        self._update()

    def _forget(self):
        from blink.webrtc.extravideo import forget_source, saved_sources
        saved = self.editing
        if saved is None:
            return
        forget_source(saved['url'])
        ActivityLog().info(f'[cameras] Forgot {_host(saved["url"]) or saved["url"]}')
        self.saved = saved_sources()
        self._clear_fields()
        self._show_found()
        self.test_status.setText('Forgotten: %s' % (saved.get('label') or _host(saved['url'])))
        self._update()

    def _new_camera(self):
        self._clear_fields()
        self.test_status.setText('Enter the camera\'s address (an IP is enough), user and password.')
        self.label_edit.setFocus()
        self._update()

    def _clear_fields(self):
        self._forget_test()
        for edit in (self.label_edit, self.url_edit, self.user_edit, self.password_edit):
            edit.clear()
        self.url_edit.setPlaceholderText('rtsp://192.168.1.10:554/stream1')
        self.filled = self.editing = self.xaddr = None
        self.found_list.blockSignals(True)
        self.found_list.setCurrentItem(None)
        self.found_list.clearSelection()
        self.found_list.blockSignals(False)
        self.test_status.setText(TEST_HINT)
