"""An RTSP camera alone in a window of its own, live, with the PTZ controls of the video room's tiles
(blink.webrtc.ptzcontrols): opened by a double-click on the Test preview of the RTSP cameras window
(blink.webrtc.camerasetup). One window per camera address.

The picture is pulled as by the Test (rtspsrc, video only, over TCP; decodebin; videoconvert,
videoscale to at most 1920x1080) into an appsink, and shown in its own aspect ratio (not cropped).
When the stream fails or ends it is pulled again after RETRY_TIME. The camera is asked whether it
has PTZ (blink.webrtc.ptz.probe); when it has, the pad and the preset pills show under the mouse,
its presets are in the right-click menu (Edit presets... too), the arrow keys pan and tilt while
held, + and - zoom. Double-click (away from the PTZ controls) or F toggles full screen; Escape
leaves full screen, else closes the window.
"""

from PyQt6.QtCore import Qt, QRectF, QTimer
from PyQt6.QtGui import QColor, QFontMetrics, QImage, QPainter
from PyQt6.QtWidgets import QMenu, QWidget

from blink.logging import ActivityLog
from blink.util import call_in_gui_thread
from blink.webrtc.ptzcontrols import PtzControls, NAME_PILL_MARGIN, PRESET_PILL_HEIGHT, _pill_font, _white


__all__ = ['CameraViewer', 'open_camera']


RETRY_TIME = 3000       # ms after a failed or ended stream before it is pulled again
BUS_INTERVAL = 250      # ms between looks at the pipeline's messages
MAX_SIZE = (1920, 1080)

_viewers = {}           # camera address: its window

_KEYS = {Qt.Key.Key_Left: (-1.0, 0.0, 0.0), Qt.Key.Key_Right: (1.0, 0.0, 0.0), Qt.Key.Key_Up: (0.0, 1.0, 0.0), Qt.Key.Key_Down: (0.0, -1.0, 0.0),
         Qt.Key.Key_Plus: (0.0, 0.0, 1.0), Qt.Key.Key_Equal: (0.0, 0.0, 1.0), Qt.Key.Key_Minus: (0.0, 0.0, -1.0)}


def _host(url):
    from urllib.parse import urlsplit
    try:
        return urlsplit(url or '').hostname or ''
    except ValueError:
        return ''


def open_camera(camera, parent=None):
    """camera: {url, user, password, label}; the window of the camera (one per address), raised."""
    viewer = _viewers.get(camera['url'])
    if viewer is not None and (viewer.camera.get('user') or '', viewer.camera.get('password') or '') != (camera.get('user') or '', camera.get('password') or ''):
        viewer.close()      # other credentials: pulled again with those
        viewer = None
    if viewer is None:
        viewer = _viewers[camera['url']] = CameraViewer(camera, parent)
    viewer.show()
    viewer.raise_()
    viewer.activateWindow()
    return viewer


class CameraViewer(PtzControls, QWidget):
    def __init__(self, camera, parent=None):
        super(CameraViewer, self).__init__(parent, Qt.WindowType.Window)
        self.camera = dict(camera)
        self.ptz_name = self.camera.get('label') or _host(self.camera['url']) or 'Camera'
        self.setWindowTitle(self.ptz_name)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(960, 540)
        self.image = None
        self.status = 'Connecting…'
        self.closed = False
        self.pipeline = None
        self.generation = 0         # of the pipeline: frames of an older one are dropped
        self.key_held = None
        self._ptz_setup()
        self.bus_timer = QTimer(self)
        self.bus_timer.setInterval(BUS_INTERVAL)
        self.bus_timer.timeout.connect(self._poll_bus)
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.setInterval(RETRY_TIME)
        self.retry_timer.timeout.connect(self._start)
        self._start()
        from blink.webrtc.ptz import probe
        probe(self.camera, self._SH_PtzProbed)

    def _SH_PtzProbed(self, ptz):
        from PyQt6 import sip
        if self.closed or sip.isdeleted(self):
            return
        self.set_ptz(ptz, self.camera)

    # The stream

    def _start(self):
        self._stop()
        if self.closed:
            return
        from blink.webrtc.videoroom import Gst, _make
        from blink.webrtc.extravideo import _video_stream_only
        if Gst is None:
            self.status = 'No GStreamer: the camera cannot be shown'
            self.update()
            return
        self.generation += 1
        generation = self.generation
        try:
            pipeline = Gst.Pipeline.new('camera-viewer')
            source = _make('rtspsrc', location=self.camera['url'], latency=200, protocols='tcp')
            if self.camera.get('user'):
                source.set_property('user-id', self.camera['user'])
                source.set_property('user-pw', self.camera.get('password') or '')
            decodebin = _make('decodebin')
            convert = _make('videoconvert')
            scale = _make('videoscale')
            sink = _make('appsink', emit_signals=True, max_buffers=1, drop=True, sync=False,
                         caps=Gst.Caps.from_string(f'video/x-raw,format=BGRx,width=[1,{MAX_SIZE[0]}],height=[1,{MAX_SIZE[1]}],pixel-aspect-ratio=1/1'))
        except Exception as e:
            self.status = f'Cannot show the camera: {e}'
            self.update()
            return
        for element in (source, decodebin, convert, scale, sink):
            pipeline.add(element)
        convert.link(scale)
        scale.link(sink)
        source.connect('select-stream', _video_stream_only)

        def rtsp_pad(element, pad):
            sink_pad = decodebin.get_static_pad('sink')
            if not sink_pad.is_linked():
                pad.link(sink_pad)

        def decoded(element, pad):
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps is not None and caps.get_size() and caps.get_structure(0).get_name().startswith('video/') and not convert.get_static_pad('sink').is_linked():
                pad.link(convert.get_static_pad('sink'))

        def sample(appsink):
            sample_ = appsink.emit('pull-sample')
            if sample_ is None:
                return Gst.FlowReturn.OK
            structure = sample_.get_caps().get_structure(0)
            width, height = structure.get_value('width'), structure.get_value('height')
            buffer = sample_.get_buffer()
            ok, mapping = buffer.map(Gst.MapFlags.READ)
            if ok:
                try:
                    image = QImage(bytes(mapping.data), width, height, width * 4, QImage.Format.Format_RGB32).copy()
                finally:
                    buffer.unmap(mapping)
                call_in_gui_thread(self._frame, image, generation)
            return Gst.FlowReturn.OK

        source.connect('pad-added', rtsp_pad)
        decodebin.connect('pad-added', decoded)
        sink.connect('new-sample', sample)
        self.pipeline = pipeline
        pipeline.set_state(Gst.State.PLAYING)
        self.bus_timer.start()
        ActivityLog().info(f'[cameras] {_host(self.camera["url"])}: shown in its own window')

    def _stop(self):
        self.bus_timer.stop()
        if self.pipeline is not None:
            from blink.webrtc.videoroom import Gst
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        self.generation += 1

    def _poll_bus(self):
        if self.pipeline is None:
            return
        from blink.webrtc.videoroom import Gst
        bus = self.pipeline.get_bus()
        while True:
            message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is None:
                return
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                reason = error.message
            else:
                reason = 'the stream ended'
            ActivityLog().info(f'[cameras] {_host(self.camera["url"])}: {reason}, again in {RETRY_TIME // 1000} s')
            self.status = f'{reason[:1].upper()}{reason[1:]}; trying again…'
            self.image = None
            self._stop()
            self.retry_timer.start()
            self.update()
            return

    def _frame(self, image, generation):
        from PyQt6 import sip
        if self.closed or sip.isdeleted(self) or generation != self.generation:
            return
        self.image = image
        self.status = ''
        self.update()

    def closeEvent(self, event):
        self.closed = True
        self.retry_timer.stop()
        self._stop()
        self.set_ptz(None)
        self.ptz_idle_timer.stop()
        if _viewers.get(self.camera['url']) is self:
            _viewers.pop(self.camera['url'], None)
        super(CameraViewer, self).closeEvent(event)

    # Drawing

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self.image is not None:
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2, size.width(), size.height())
            painter.drawImage(target, self.image)
        else:
            painter.setPen(_white(0.6))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.status)
        # the name, bottom left (the presets go along the top)
        painter.setFont(_pill_font())
        width = QFontMetrics(_pill_font()).horizontalAdvance(self.ptz_name) + 20
        pill = QRectF(NAME_PILL_MARGIN, self.height() - NAME_PILL_MARGIN - PRESET_PILL_HEIGHT, width, PRESET_PILL_HEIGHT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(pill, PRESET_PILL_HEIGHT / 2, PRESET_PILL_HEIGHT / 2)
        painter.setPen(_white(0.95))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, self.ptz_name)
        if self.ptz_shown:
            self._paint_ptz(painter)
            self._paint_presets(painter)

    # PTZ errors in the window

    def ptz_error(self, text):
        ActivityLog().warning(f'[ptz] {text}')
        self.setWindowTitle(f'{self.ptz_name} — {text}')
        QTimer.singleShot(6000, lambda: None if self.closed else self.setWindowTitle(self.ptz_name))

    # Mouse and keys

    def enterEvent(self, event):
        self._ptz_wake()
        super(CameraViewer, self).enterEvent(event)

    def leaveEvent(self, event):
        self.ptz_idle_timer.stop()
        self.update()
        super(CameraViewer, self).leaveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.ptz is not None and event.button() == Qt.MouseButton.LeftButton and self.ptz_owns(event.position()):
            super(CameraViewer, self).mouseDoubleClickEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_full_screen()
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        if self.ptz is not None:
            self.add_ptz_menu(menu)
        menu.addAction('Exit full screen' if self.isFullScreen() else 'Full screen').triggered.connect(self._toggle_full_screen)
        menu.addAction('Close').triggered.connect(self.close)
        menu.exec(event.globalPos())

    def _toggle_full_screen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.showNormal() if self.isFullScreen() else self.close()
            return
        if key == Qt.Key.Key_F and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            self._toggle_full_screen()
            return
        control = _KEYS.get(key)
        if control is not None and self.ptz is not None:
            if not event.isAutoRepeat() and self.key_held != control:
                if control[2] and not self.ptz_zoom:
                    return
                self.key_held = control
                self._ptz_wake()
                self.ptz_press(control)
            return
        super(CameraViewer, self).keyPressEvent(event)

    def keyReleaseEvent(self, event):
        control = _KEYS.get(event.key())
        if control is not None and not event.isAutoRepeat() and control == self.key_held:
            self.key_held = None
            self.ptz_release()
            return
        super(CameraViewer, self).keyReleaseEvent(event)
