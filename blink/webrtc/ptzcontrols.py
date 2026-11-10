"""The PTZ controls of a camera over its picture, as in AG CuteCams: a direction pad at the right
edge, zoom + and - under it, the presets as pills along the edge away from the name, and a presets
menu. The camera is an RTSP one over ONVIF (blink.webrtc.ptz) or a USB one over V4L2
(blink.webrtc.uvcptz). PtzControls goes into the video room's tiles; PtzOverlay lays them over any
other picture (the camera preview of the preferences).
"""

from PyQt6.QtCore import Qt, QEvent, QPointF, QRectF, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QApplication, QInputDialog, QMenu, QWidget

from blink.logging import ActivityLog


__all__ = ['PtzControls', 'PtzOverlay', 'CENTER', 'paint_target']


PTZ_RADIUS = 46             # the direction pad, at most; smaller on a small picture
PTZ_MIN_SIZE = (200, 150)   # a picture smaller than this shows no PTZ controls
PTZ_ACTIVE_COLOR = QColor(10, 132, 255)
PRESET_PILL_HEIGHT = 24
PRESET_PILL_GAP = 6
PTZ_IDLE_TIME = 5000        # ms without the mouse moving over the picture before the controls and preset pills hide
NAME_PILL_MARGIN = 8        # as the video room's
CENTER = 'center'           # the control in the middle of the pad: the camera goes back to its middle
CENTER_RADIUS = 0.26        # of the pad's radius


def _white(alpha):
    return QColor(255, 255, 255, int(round(255 * alpha)))


def _pill_font():
    font = QFont(QApplication.font())
    font.setPointSizeF(9)
    font.setWeight(QFont.Weight.Medium)
    return font


def paint_target(painter, rect, key, mirror=False, crop=True):
    """The person a tracker follows (blink.webrtc.tracker.Trackers, by key), marked on a picture of ours as a
    plus in a ring: where the picture shows them (cropped to fill rect, or whole in it; mirrored or not).
    Only on our own pictures, never on what is sent."""
    from blink.webrtc.tracker import Trackers
    target = Trackers.instance().target(key)
    if target is None or rect.width() < 60 or rect.height() < 40:
        return
    x, y, (width, height), state = target
    if not width or not height:
        return
    scale = (max if crop else min)(rect.width() / width, rect.height() / height)
    shown_width, shown_height = width * scale, height * scale
    left = rect.left() + (rect.width() - shown_width) / 2
    top = rect.top() + (rect.height() - shown_height) / 2
    point = QPointF(left + (1 - x if mirror else x) * shown_width, top + y * shown_height)
    if not rect.contains(point):
        return
    size = max(10.0, min(22.0, min(rect.width(), rect.height()) * 0.05))
    color = QColor(52, 199, 89) if state == 'following' else QColor(255, 196, 0) if state == 'paused' else _white(0.7)
    painter.save()
    for pen in (QPen(QColor(0, 0, 0, 140), 4.5), QPen(color, 2)):      # a dark edge: seen on any picture
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(point.x() - size, point.y()), QPointF(point.x() - size * 0.35, point.y()))
        painter.drawLine(QPointF(point.x() + size * 0.35, point.y()), QPointF(point.x() + size, point.y()))
        painter.drawLine(QPointF(point.x(), point.y() - size), QPointF(point.x(), point.y() - size * 0.35))
        painter.drawLine(QPointF(point.x(), point.y() + size * 0.35), QPointF(point.x(), point.y() + size))
        painter.drawEllipse(point, size * 0.6, size * 0.6)
    painter.restore()


class PtzControls(object):
    """Pan, tilt, zoom and presets of a tile's camera, under the mouse, as in AG CuteCams: a direction
    pad at the right edge, zoom + and - under it, the presets as pills along the edge away from the
    name. The camera is an RTSP one over ONVIF (blink.webrtc.ptz) or a USB one over V4L2
    (blink.webrtc.uvcptz); held, a control moves it, let go, it stops."""

    ptz_mirrored = False        # the tile shows the picture mirrored (our own camera)
    ptz_name = 'Camera'         # what the camera is called in messages
    label_corner = ('bottom', 'left')       # where the tile's name is: the presets go along the other edge

    def _ptz_setup(self):
        self.ptz = None                 # Ptz or UvcPtz when the camera has PTZ
        self.ptz_camera = {}
        self.ptz_held = None            # the control held: (pan, tilt, zoom)
        self.ptz_idle = False           # the mouse has not moved over the tile for PTZ_IDLE_TIME
        self.ptz_idle_timer = QTimer(self)
        self.ptz_idle_timer.setSingleShot(True)
        self.ptz_idle_timer.setInterval(PTZ_IDLE_TIME)
        self.ptz_idle_timer.timeout.connect(self._SH_PtzIdle)

    def set_ptz(self, ptz, camera=None):
        """ptz: blink.webrtc.ptz.Ptz or blink.webrtc.uvcptz.UvcPtz of the tile's camera, or None; camera: its settings (ptz_zoom, preset_names)."""
        if self.ptz is not None:
            if self.ptz_held is not None:
                self.ptz.stop()
            if self._SH_PtzChanged in self.ptz.listeners:
                self.ptz.listeners.remove(self._SH_PtzChanged)
        self.ptz, self.ptz_held = ptz, None
        self.ptz_camera = camera or {}
        if ptz is not None:
            ptz.listeners.append(self._SH_PtzChanged)
        self._ptz_update()

    def _SH_PtzChanged(self):
        """The camera moved: a limit reached disables its arrow (hides its zoom button), and stops it when held."""
        if self.ptz_held is not None and self.ptz_blocked(self.ptz_held):
            self.ptz_release()
        self._ptz_update()

    def _ptz_camera_control(self, control):
        """(pan, tilt, zoom) for the camera of a control as on the screen: pan the other way when mirrored."""
        pan, tilt, zoom = control
        return (-pan if self.ptz_mirrored else pan), tilt, zoom

    def ptz_blocked(self, control):
        """Whether the camera can go no further the way of this control (pan, tilt, zoom), as on the screen."""
        if self.ptz is None or control is None or control == CENTER:
            return False
        return any(value and self.ptz.at_limit(axis, value) for axis, value in zip(('pan', 'tilt', 'zoom'), self._ptz_camera_control(control)))

    def _ptz_update(self):
        self.update()

    def _SH_PtzIdle(self):
        self.ptz_idle = True
        self._ptz_update()

    @property
    def ptz_zoom(self):
        return self.ptz is not None and self.ptz.zoom_for(self.ptz_camera)

    @property
    def ptz_shown(self):
        return self.ptz is not None and getattr(self, 'in_grid', True) and ((self.underMouse() and not self.ptz_idle) or self.ptz_held is not None) and \
            self.width() >= PTZ_MIN_SIZE[0] and self.height() >= PTZ_MIN_SIZE[1]

    def _ptz_geometry(self):
        """(the pad's center, its radius, [(zoom, its circle)]): at the right edge, centered."""
        radius = min(PTZ_RADIUS, self.height() * 0.2)
        zoom_radius = radius * 0.34
        zooms = 2 * zoom_radius + 8 if self.ptz_zoom else 0
        center = QPointF(self.width() - NAME_PILL_MARGIN - radius - 4, self.height() / 2 - zooms / 2)
        buttons = []
        if zooms:
            y = center.y() + radius + 8 + zoom_radius
            for zoom, x in ((-1.0, center.x() - zoom_radius - 4), (1.0, center.x() + zoom_radius + 4)):
                if self.ptz_blocked((0.0, 0.0, zoom)):
                    continue        # as far as it goes: the button is gone, the other stays in place
                buttons.append((zoom, QRectF(x - zoom_radius, y - zoom_radius, 2 * zoom_radius, 2 * zoom_radius)))
        return center, radius, buttons

    def ptz_control_at(self, position):
        """(pan, tilt, zoom) of the control at position, CENTER for the middle of the pad, or None."""
        if not self.ptz_shown:
            return None
        center, radius, buttons = self._ptz_geometry()
        for zoom, circle in buttons:
            if circle.contains(position):
                return (0.0, 0.0, zoom)
        dx, dy = position.x() - center.x(), position.y() - center.y()
        distance = (dx * dx + dy * dy) ** 0.5
        if distance <= radius * CENTER_RADIUS:
            return CENTER if hasattr(self.ptz, 'go_home') else None
        if distance > radius or distance < radius * 0.3:
            return None
        if abs(dx) > abs(dy):
            return (1.0 if dx > 0 else -1.0, 0.0, 0.0)
        return (0.0, -1.0 if dy > 0 else 1.0, 0.0)       # up on the screen is up for the camera

    def _preset_pills(self):
        """[(rect, token, text)] of the preset train, centered on the edge away from the name pill; a last
        ('+N', token None) when they do not all fit."""
        if not self.ptz_shown or not self.ptz.presets:
            return []
        from blink.webrtc.ptz import preset_label
        metrics = QFontMetrics(_pill_font())
        labels = [(token, metrics.elidedText(preset_label(self.ptz_camera, token, name), Qt.TextElideMode.ElideRight, 140))
                  for token, name in self.ptz.presets]
        width = lambda text: metrics.horizontalAdvance(text) + 20
        available = self.width() - 2 * NAME_PILL_MARGIN
        shown = []
        used = 0
        for index, (token, text) in enumerate(labels):
            rest = len(labels) - index - 1
            more = width(f'+{rest}') + PRESET_PILL_GAP if rest else 0
            needed = width(text) + (PRESET_PILL_GAP if shown else 0)
            if used + needed + more > available:
                break
            shown.append((token, text))
            used += needed
        hidden = len(labels) - len(shown)
        if hidden:
            shown.append((None, f'+{hidden}'))
            used += width(f'+{hidden}') + (PRESET_PILL_GAP if len(shown) > 1 else 0)
        vertical = 'bottom' if self.label_corner[0] == 'top' else 'top'
        y = NAME_PILL_MARGIN if vertical == 'top' else self.height() - NAME_PILL_MARGIN - PRESET_PILL_HEIGHT
        x = (self.width() - used) / 2
        pills = []
        for token, text in shown:
            rect = QRectF(x, y, width(text), PRESET_PILL_HEIGHT)
            pills.append((rect, token, text))
            x += rect.width() + PRESET_PILL_GAP
        return pills

    def preset_pill_at(self, position):
        """(token, or None for '+N') of the pill at position, or False."""
        return next((token for rect, token, text in self._preset_pills() if rect.contains(position)), False)

    def ptz_owns(self, position):
        """Whether a click at position is for the PTZ controls (a control held, the pad, zoom or a preset)."""
        return self.ptz_held is not None or self.ptz_control_at(position) is not None or self.preset_pill_at(position) is not False

    def _paint_presets(self, painter):
        painter.setFont(_pill_font())
        for rect, token, text in self._preset_pills():
            active = token is not None and token == self.ptz.active_preset
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(PTZ_ACTIVE_COLOR if active else QColor(0, 0, 0, 150))
            painter.drawRoundedRect(rect, PRESET_PILL_HEIGHT / 2, PRESET_PILL_HEIGHT / 2)
            painter.setPen(_white(1.0 if active else 0.9))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_ptz(self, painter):
        center, radius, buttons = self._ptz_geometry()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawEllipse(center, radius, radius)
        held = self.ptz_held
        size = radius * 0.26
        for pan, tilt in ((0, 1), (0, -1), (-1, 0), (1, 0)):
            # an arrow head pointing out, at two thirds of the radius
            tip = QPointF(center.x() + pan * radius * 0.8, center.y() - tilt * radius * 0.8)
            back = QPointF(center.x() + pan * radius * 0.5, center.y() - tilt * radius * 0.5)
            side = QPointF(tilt * size, pan * size)
            active = held is not None and held[:2] == (float(pan), float(tilt))
            blocked = self.ptz_blocked((float(pan), float(tilt), 0.0))       # as far as it goes that way: dimmed, does nothing
            painter.setBrush(PTZ_ACTIVE_COLOR if active else _white(0.2 if blocked else 0.85))
            painter.drawPolygon(QPolygonF([tip, back + side, back - side]))
        # the middle: a button, back to the camera's middle
        if hasattr(self.ptz, 'go_home'):
            painter.setBrush(_white(0.22))
            painter.drawEllipse(center, radius * CENTER_RADIUS, radius * CENTER_RADIUS)
            painter.setBrush(_white(0.85))
            painter.drawEllipse(center, radius * 0.09, radius * 0.09)
        else:
            painter.setBrush(_white(0.35))
            painter.drawEllipse(center, radius * 0.12, radius * 0.12)
        for zoom, circle in buttons:
            active = held is not None and held[2] == zoom
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 140))
            painter.drawEllipse(circle)
            pen = QPen(PTZ_ACTIVE_COLOR if active else _white(0.85), max(2.0, circle.width() * 0.1))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            c, arm = circle.center(), circle.width() * 0.25
            painter.drawLine(QPointF(c.x() - arm, c.y()), QPointF(c.x() + arm, c.y()))
            if zoom > 0:
                painter.drawLine(QPointF(c.x(), c.y() - arm), QPointF(c.x(), c.y() + arm))

    @staticmethod
    def ptz_touched():
        """A PTZ control pressed or let go by hand: the trackers leave the cameras alone for a while."""
        from blink.webrtc.tracker import ManualPause
        ManualPause.instance().touched()

    def ptz_press(self, control):
        if self.ptz is None or control is None or self.ptz_blocked(control):
            return          # a disabled arrow: the click is still the pad's, it does nothing
        self.ptz_touched()
        if control == CENTER:
            if self.ptz_held is not None:
                self.ptz_release()
            self.ptz.go_home()          # its physical middle
            return
        from blink.webrtc.ptz import SPEED
        if self.ptz_held is not None:
            self.ptz.stop()
        self.ptz_held = control
        pan, tilt, zoom = self._ptz_camera_control(control)     # mirrored: right on the screen is the camera's left
        self.ptz.move(pan * SPEED, tilt * SPEED, zoom * SPEED)
        self._ptz_update()

    def ptz_release(self):
        if self.ptz is not None and self.ptz_held is not None:
            self.ptz_touched()
            self.ptz.stop()
        self.ptz_held = None
        self._ptz_update()

    def _ptz_wake(self):
        """The mouse moved over the tile: the controls show again, until it rests PTZ_IDLE_TIME."""
        if self.ptz is None:
            return
        if self.ptz_idle:
            self.ptz_idle = False
            self._ptz_update()
        self.ptz_idle_timer.start()

    def mouseMoveEvent(self, event):
        if self.ptz is not None:
            self._ptz_wake()
            position = event.position()
            control = self.ptz_control_at(position)
            if (control is not None and not self.ptz_blocked(control)) or self.preset_pill_at(position) is not False:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                pill = self.preset_pill_at(position)
                if pill is None:
                    self.setToolTip('More presets')
                elif pill is not False:
                    from blink.webrtc.ptz import preset_label
                    self.setToolTip('Go to ' + preset_label(self.ptz_camera, pill, dict(self.ptz.presets).get(pill, '')))
                else:
                    self.setToolTip('')
            elif not getattr(self, 'replaceable', False):
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self.setToolTip('')
        super(PtzControls, self).mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if self.ptz is not None and event.button() == Qt.MouseButton.LeftButton:
            control = self.ptz_control_at(event.position())
            if control is not None:
                self.ptz_press(control)
                event.accept()
                return
            pill = self.preset_pill_at(event.position())
            if pill is not False:
                if pill is None:
                    self.ptz_more_presets(event.globalPosition().toPoint())
                else:
                    self.ptz_touched()
                    self.ptz.goto_preset(pill)
                    self._ptz_update()
                event.accept()
                return
        super(PtzControls, self).mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.ptz_held is not None:
            self.ptz_release()
            event.accept()
            return
        super(PtzControls, self).mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.ptz is not None and event.button() == Qt.MouseButton.LeftButton and self.ptz_owns(event.position()):
            self.mousePressEvent(event)     # a second quick press on the pad: still a move, not full screen
            return
        super(PtzControls, self).mouseDoubleClickEvent(event)

    # Presets menu

    def ptz_more_presets(self, position):
        """The '+N' pill: the tile's menu when its window makes one (it has the presets), else the presets menu."""
        window = self.window()
        if hasattr(window, '_tile_menu'):
            window._tile_menu(self, position)
        else:
            menu = QMenu(self)
            self.add_ptz_menu(menu)
            menu.exec(position)

    def ptz_error(self, text):
        window = self.window()
        if hasattr(window, 'flash'):
            window.flash(text, duration=6)
        else:
            ActivityLog().warning(f'[ptz] {text}')

    def add_ptz_menu(self, menu):
        """The presets of the camera: go to one, save the position as one, delete one."""
        from blink.webrtc.ptz import preset_label
        ptz, camera = self.ptz, self.ptz_camera
        presets = menu.addMenu('Go to preset')
        presets.setEnabled(bool(ptz.presets))
        for token, name in ptz.presets:
            action = presets.addAction(preset_label(camera, token, name))
            action.setCheckable(True)
            action.setChecked(token == ptz.active_preset)
            action.triggered.connect(lambda checked, token=token: (self.ptz_touched(), ptz.goto_preset(token), self._ptz_update()))
        menu.addAction('Save position as preset...').triggered.connect(self._save_ptz_preset)
        if hasattr(ptz, 'pan_reversed'):
            reverse = menu.addAction('Reverse pan')
            reverse.setCheckable(True)
            reverse.setChecked(ptz.pan_reversed)
            reverse.setToolTip('The camera turns the other way than the arrows say')
            reverse.triggered.connect(lambda checked: setattr(ptz, 'pan_reversed', checked))
        delete = menu.addMenu('Delete preset')
        delete.setEnabled(bool(ptz.presets))
        for token, name in ptz.presets:
            delete.addAction(preset_label(camera, token, name)).triggered.connect(lambda checked, token=token: self._delete_ptz_preset(token))
        menu.addSeparator()

    def _save_ptz_preset(self):
        ptz = self.ptz
        name, ok = QInputDialog.getText(self.window(), 'Save preset', f'Save the position of {self.ptz_name} as:', text=f'Preset {len(ptz.presets) + 1}')
        name = name.strip()
        if not ok or not name or self.ptz is not ptz:
            return

        def saved(token, error):
            if error:
                self.ptz_error(f'{self.ptz_name}: cannot save the preset: {error}')
            else:
                ptz.active_preset = token
                ptz.refresh_presets(lambda presets, error: self._ptz_update())
        ptz.set_preset(name, done=saved)

    def _delete_ptz_preset(self, token):
        ptz = self.ptz

        def deleted(answer, error):
            if error:
                self.ptz_error(f'{self.ptz_name}: cannot delete the preset: {error}')
            if ptz.active_preset == token:
                ptz.active_preset = None
            ptz.refresh_presets(lambda presets, error: self._ptz_update())
        ptz.remove_preset(token, deleted)


class PtzOverlay(PtzControls, QWidget):
    """The PTZ controls over a picture that is not a tile of the video room: a transparent widget over
    a VideoSurface (the camera preview of the preferences), its size, for the camera named (V4L2).
    While the camera follows the person (blink.webrtc.tracker.FollowMe) a pill at the bottom says
    so; a control pressed by hand turns Follow me off."""

    ptz_name = 'The camera'

    def __init__(self, surface):
        super(PtzOverlay, self).__init__(surface)
        self.surface = surface
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self._ptz_setup()
        self._ptz_camera_name = None
        self.setGeometry(surface.rect())
        surface.installEventFilter(self)
        from blink.webrtc.tracker import Trackers
        Trackers.instance().looked.connect(self._SH_TrackerLooked)
        self.hide()

    @property
    def ptz_mirrored(self):
        return self.surface.mirror

    def attach(self, name):
        """The camera shown (its name as the SDK gives it), None when none: the controls when it has PTZ."""
        if name != self._ptz_camera_name:
            self._ptz_camera_name = name
            from blink.webrtc.uvcptz import find
            try:
                ptz = find(name) if name else None
            except Exception as e:
                ActivityLog().warning(f'[ptz] {name}: {e}')
                ptz = None
            self.set_ptz(ptz)
        self.setVisible(self.ptz is not None)       # without, the clicks are the surface's
        if self.ptz is not None:
            self.raise_()

    def _SH_TrackerLooked(self, key):
        if key == 'camera' and self.isVisible():
            self.update()

    def eventFilter(self, watched, event):
        if watched is self.surface and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.surface.rect())
        return False

    def enterEvent(self, event):
        self._ptz_wake()
        self.update()
        super(PtzOverlay, self).enterEvent(event)

    def leaveEvent(self, event):
        self.ptz_idle_timer.stop()
        self.update()
        super(PtzOverlay, self).leaveEvent(event)

    def contextMenuEvent(self, event):
        if self.ptz is not None:
            menu = QMenu(self)
            self.add_ptz_menu(menu)
            menu.exec(event.globalPos())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        paint_target(painter, QRectF(self.rect()), 'camera', mirror=self.surface.mirror, crop=self.surface.crop)
        if self.ptz_shown:
            self._paint_ptz(painter)
            self._paint_presets(painter)
        from blink.webrtc.tracker import FollowMe
        state = FollowMe.instance().state
        if self.ptz is not None and state != 'off':
            text = {'lost': 'Looking for you (F stops)', 'resting': 'Waiting for you (F stops)',
                    'paused': 'Paused: you drive (follows again in 10 s)'}.get(state, 'Following you (F stops)')
            painter.setFont(_pill_font())
            width = QFontMetrics(_pill_font()).horizontalAdvance(text) + 20
            pill = QRectF((self.width() - width) / 2, self.height() - NAME_PILL_MARGIN - PRESET_PILL_HEIGHT, width, PRESET_PILL_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150) if state in ('lost', 'resting', 'paused') else PTZ_ACTIVE_COLOR)
            painter.drawRoundedRect(pill, PRESET_PILL_HEIGHT / 2, PRESET_PILL_HEIGHT / 2)
            painter.setPen(_white(0.95))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
