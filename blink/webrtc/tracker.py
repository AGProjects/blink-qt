"""Follow me: a camera with PTZ keeps the person in the picture.

The person is found with the background blur's segmentation model (PP-HumanSeg in numpy,
blink.webrtc.background_blur.Segmenter) on the camera's own frames (a reader of its own, as the
blur's, on SIPApplication.video_device), as often as it can but at most every DETECT_INTERVAL,
in a thread. What counts: the person's center, kept on a third line across (THIRDS: the nearer one
when found) and on the lower third line up and down (LOWER_THIRD), the top of the shape (the
head) never closer to the top edge than HEAD_MARGIN. Off by more than DEAD_ZONE, the camera is turned by
that much of its field of view (GAIN of it, so that it settles without overshooting), at most
every MOVE_INTERVAL, as it takes the camera a moment to get there. No person for LOST_GRACE:
the camera nods, NOD_DEGREES up and down from where it was, until one is seen again.

The PTZ object must turn by degrees (nudge_degrees, field_of_view): blink.webrtc.uvcptz.UvcPtz.

Frames come from the camera Blink sends (CameraSource) or from an extra video of a video room
(ExtraVideoSource: an RTSP camera, as its tile gets them). The video room's director
(blink.webrtc.director) runs one tracker per camera, without nodding, and cuts between them on
personChanged.

FollowMe is the one switch for the camera Blink sends: on or off (saved in ui.conf, video/follow_me),
it follows while something shows that camera (the preview of the preferences, our own tile in a
video room: they are its users) and the camera can. Each of them shows it and toggles it.
"""

import threading
import time

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QImage

from blink.logging import ActivityLog
from blink.util import call_in_gui_thread


__all__ = ['FollowMe', 'ManualPause', 'Trackers', 'PersonTracker', 'CameraSource', 'ExtraVideoSource', 'can_see', 'can_track']


DETECT_INTERVAL = 0.1       # s, at most 10 looks a second (the model takes most of it)
MIN_AREA = 0.01             # of the picture: less than this is nobody
PERSON = 0.5                # probability above which a pixel is the person
DEAD_ZONE = 0.12            # of the picture's width (height): closer to the target than this, the camera stays (it does not chase every move)
OFFSET_LOOKS = 2            # looks in a row off by more than DEAD_ZONE before it turns: a gesture or a lean does not move it
ACQUIRE_LOOKS = 6           # looks in a row with someone (about 0.6 s) before it follows them: no chasing shadows
ACQUIRE_AREA = 0.02         # of the picture, at least, for someone to be followed
RESTLESS = (3, 30.0)        # found again this many times within these seconds: restless, it waits longer before following
RESTLESS_LOOKS = 20         # looks in a row (about 2 s) before it follows, while restless
LOWER_THIRD = 2 / 3         # where the person's center goes, of the height from the top: on the lower third line
HEAD_MARGIN = 0.05          # the top of the person (the head) is kept at least this far from the top edge
THIRDS = (1 / 3, 2 / 3)     # where the person goes across: a third line, not the middle (rule of thirds)
GAIN = 0.6                  # of the error turned at once (cameras turned by steps): less than all, so that it settles
MOVE_INTERVAL = 0.45        # s between two steps: the camera gets there, and its picture comes back, before the next
DRIVE_GAIN = 3.0            # velocity (-1..1) per picture width off (cameras driven): a third of the width off is full speed
DRIVE_STOP = 0.04           # driven: closer to the target than this, it stops (it starts beyond DEAD_ZONE)
LEAD = 0.3                  # s ahead the person is aimed at, from how fast they move across the picture
DRIVE_WATCHDOG = 600        # ms without a look: a driven camera stops
WRONG_WAY_MOVE = 0.025      # of the width between two looks, with the turn instead of against it: a sign the camera pans the other way
WRONG_WAY_LOOKS = 4         # such looks in a row: the camera's pan is reversed (saved), and the tracker carries on
WRONG_WAY_AFTER = 0.5       # s turning hard the same way first: the camera is up to speed (a person walking faster drifts much less)
AUTO_REVERSE = False        # find out by itself that a camera pans the other way: off (a wrong guess runs the motors away); Reverse pan in the PTZ menu
NODS = 3                    # nods when the person is lost, then the camera rests in the middle (the motors)
LOST_GRACE = 2.0            # s it holds where it lost the person (they come back there, mostly) before going to the middle and nodding
NOD_DEGREES = 5.0
NOD_INTERVAL = 700          # ms between the positions of a nod: up, back, down, back
HOME_SETTLE = 2500          # ms for the camera to get back to the middle before it nods there
SEEN_AFTER = 2              # looks in a row with the person before personChanged(True)
GONE_AFTER = 3              # looks in a row without before personChanged(False)


MANUAL_PAUSE = 10000        # ms after the last PTZ control pressed by hand before the trackers turn the cameras again


class ManualPause(QObject):
    """A PTZ control pressed by hand (any camera, any window): every tracker leaves the cameras alone until
    MANUAL_PAUSE after the last press or release; they keep looking meanwhile (a singleton: instance())."""

    changed = pyqtSignal(bool)      # paused

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super(ManualPause, self).__init__()
        self.paused = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(MANUAL_PAUSE)
        self._timer.timeout.connect(self._resume)

    def touched(self):
        """A PTZ control pressed or let go by hand."""
        self._timer.start()
        if not self.paused:
            self.paused = True
            ActivityLog().info(f'[ptz] Controlled by hand: tracking paused for {MANUAL_PAUSE // 1000} s')
            self.changed.emit(True)

    def _resume(self):
        if self.paused:
            self.paused = False
            ActivityLog().info('[ptz] Tracking again')
            self.changed.emit(False)


TARGET_FRESH = 0.6          # s: where the person was seen is shown that long (a look comes every DETECT_INTERVAL)


class Trackers(QObject):
    """The trackers running, by what they look at ('camera': the camera Blink sends; an extra video's id), for
    the pictures of ours to show where the person they follow is (a singleton: instance())."""

    looked = pyqtSignal(str)        # a tracker looked (found the person, or not): its key

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super(Trackers, self).__init__()
        self.trackers = {}

    def register(self, key, tracker):
        self.trackers[key] = tracker

    def unregister(self, key, tracker):
        if self.trackers.get(key) is tracker:
            del self.trackers[key]
            self.looked.emit(key)

    def target(self, key):
        """(x, y, (frame width, height), state) of the person the tracker of key follows, x and y 0..1 of its frame
        (not mirrored), while seen lately; else None."""
        tracker = self.trackers.get(key)
        if tracker is None or not tracker.running or tracker.where is None or tracker.frame_size is None:
            return None
        if time.monotonic() - tracker.where_at > TARGET_FRESH:
            return None
        center, top, middle = tracker.where[:3]
        return center, middle, tracker.frame_size, tracker.state


class CameraSource(object):
    """The frames of the camera Blink sends (BGRA), from a renderer of its own."""

    name = 'camera'
    key = 'camera'

    def __init__(self):
        self._renderer = None

    def start(self, push):
        from sipsimple.application import SIPApplication
        from sipsimple.core import FrameBufferVideoRenderer
        try:
            producer = SIPApplication.video_device.producer
        except AttributeError:
            producer = None
        if producer is None:
            raise RuntimeError('no camera')
        self._renderer = FrameBufferVideoRenderer(lambda frame: push((bytes(frame.data), frame.width, frame.height)))
        self._renderer.producer = producer

    def stop(self):
        renderer, self._renderer = self._renderer, None
        if renderer is not None:
            try:
                renderer.close()
            except Exception:
                pass


class ExtraVideoSource(object):
    """The frames of an extra video of a video room (blink.webrtc.extravideo: an RTSP camera), as its tile
    gets them (QImage, RGB32), taken on the streaming thread."""

    def __init__(self, extra):
        self.extra = extra
        self.name = extra.label
        self.key = extra.id
        self._push = None

    def _frame(self, image):
        push = self._push
        if push is not None:
            push(image)

    def start(self, push):
        self._push = push
        self.extra.participant.frameReady.connect(self._frame, Qt.ConnectionType.DirectConnection)

    def stop(self):
        self._push = None
        try:
            self.extra.participant.frameReady.disconnect(self._frame)
        except TypeError:
            pass


def can_see():
    """Whether the person model is there (it finds the person; a camera without PTZ can still look)."""
    from blink.webrtc.background_blur import blur_available
    return blur_available()


def can_track(ptz):
    from blink.webrtc.background_blur import blur_available
    return ptz is not None and hasattr(ptz, 'nudge_degrees') and ptz.can_turn() and blur_available()


class PersonTracker(QObject):
    stateChanged = pyqtSignal(str)      # 'following', 'lost' (holding, nodding), 'resting' (still, until someone is there for sure), 'paused' (PTZ by hand), 'off'
    personChanged = pyqtSignal(bool)    # the person came into sight, or went out of it (debounced)

    def __init__(self, ptz, parent=None, source=None, nod=True, move_interval=MOVE_INTERVAL):
        """ptz None: a fixed camera, that only looks (personChanged) and never turns."""
        super(PersonTracker, self).__init__(parent)
        self.ptz = ptz
        self.source = source or CameraSource()
        self.name = ptz.host if ptz is not None else self.source.name
        self.nod = nod and ptz is not None                 # lost: nod (Follow me), or stay where it was (the director's cameras)
        self.move_interval = move_interval
        self.state = 'off'
        self.seen = False               # the person is in sight (as personChanged said last)
        self.where = None               # (horizontal center, top, vertical center) where the person was seen last, 0..1
        self.where_at = 0.0             # when
        self.frame_size = None          # (width, height) of the frames looked at
        self._streak = 0                # looks in a row with (> 0) or without (< 0) the person
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._frame = None
        self._running = False
        self._thread = None
        self._last_move = 0.0
        self._lost_since = None
        self._previous = None           # (time, horizontal center, vertical center) of the look before
        self._speed = (0.0, 0.0)        # how fast the person moves across the picture (per second), smoothed
        self._driving = (False, False)  # pan, tilt being driven (between DEAD_ZONE and DRIVE_STOP)
        self._target = None             # the third line the person is kept on, across (THIRDS)
        self._locked = False            # following someone (seen for sure); not: it stays still, looking
        self._acquiring = 0             # looks in a row with someone, while not following
        self._found_times = []          # when it started following, lately (restless)
        self._off_looks = 0             # looks in a row off the target by more than DEAD_ZONE
        self._last_pan = 0.0            # the pan velocity driven at the look before
        self._pan_since = None          # since when it has been driven hard that way
        self._wrong_way = 0             # looks in a row that say the camera pans the other way
        self._homing = False            # lost: going back to the middle, the nod after
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(DRIVE_WATCHDOG)
        self._watchdog.timeout.connect(self._halt)
        self._nod_offset = 0.0          # degrees of tilt the nod is away from where it started
        self._nod_step = 0
        self._nod_timer = QTimer(self)
        self._nod_timer.setInterval(NOD_INTERVAL)
        self._nod_timer.timeout.connect(self._nod)

    def __repr__(self):
        return f'<PersonTracker {self.name} {self.state}>'

    def start(self):
        if self._running:
            return
        self.source.start(self._on_frame)
        self._running = True
        Trackers.instance().register(self.source.key, self)
        ManualPause.instance().changed.connect(self._SH_ManualPause)
        self._thread = threading.Thread(target=self._run, name='person-tracker', daemon=True)
        self._thread.start()
        ActivityLog().info(f'[ptz] {self.name}: ' + ('following the person' if self.ptz is not None else 'looking for the person (fixed)'))
        self._set_state('following')

    def stop(self):
        if not self._running:
            return
        with self._lock:
            self._running = False
            self._frame = None
            self._wakeup.notify_all()
        self.source.stop()
        Trackers.instance().unregister(self.source.key, self)
        try:
            ManualPause.instance().changed.disconnect(self._SH_ManualPause)
        except TypeError:
            pass
        self._halt()
        self._watchdog.stop()
        self._stop_nodding(back=True)
        ActivityLog().info(f'[ptz] {self.name}: no longer looking')
        self._set_state('off')

    @property
    def running(self):
        return self._running

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            self.stateChanged.emit(state)

    # pjsip's video thread or a streaming thread: keep the newest frame ((BGRA bytes, width, height) or a QImage)

    def _on_frame(self, frame):
        with self._lock:
            if self._running:
                self._frame = frame
                self._wakeup.notify_all()

    # The detection thread

    def _run(self):
        import numpy as np
        from blink.webrtc.background_blur import Segmenter
        try:
            segmenter = Segmenter()
        except Exception as e:
            ActivityLog().warning(f'[ptz] Cannot load the person model ({e}): not following')
            call_in_gui_thread(self.stop)
            return
        while True:
            with self._lock:
                while self._running and self._frame is None:
                    self._wakeup.wait(1.0)
                if not self._running:
                    return
                frame, self._frame = self._frame, None
            started = time.monotonic()
            if isinstance(frame, QImage):
                image = frame if frame.format() == QImage.Format.Format_RGB32 else frame.convertToFormat(QImage.Format.Format_RGB32)
            else:
                data, width, height = frame
                image = QImage(data, width, height, width * 4, QImage.Format.Format_RGB32) if len(data) >= width * height * 4 else None
            if image is not None and not image.isNull():
                self.frame_size = (image.width(), image.height())
                try:
                    found = _person(np, segmenter.probability(image))
                except Exception as e:
                    ActivityLog().warning(f'[ptz] Looking for the person failed: {e}')
                    found = None
                call_in_gui_thread(self._observed, found)
            rest = DETECT_INTERVAL - (time.monotonic() - started)
            if rest > 0:
                time.sleep(rest)

    # The GUI thread: turning the camera

    def _observed(self, found):
        if not self._running:
            return
        now = time.monotonic()
        self._streak = (max(self._streak, 0) + 1) if found is not None else (min(self._streak, 0) - 1)
        if found is not None:
            self.where = found
            self.where_at = now
        if not self.seen and self._streak >= SEEN_AFTER:
            self.seen = True
            self.personChanged.emit(True)
        elif self.seen and self._streak <= -GONE_AFTER:
            self.seen = False
            self.personChanged.emit(False)
        Trackers.instance().looked.emit(self.source.key)       # our pictures show where the person is
        if found is None:
            self._halt()
            self._previous = None
            self._target = None         # found again: the third nearer to where they are
        if ManualPause.instance().paused:
            self._set_state('paused')
            self._lost_since = None
            return              # the user drives: it only looks
        if not self._locked:
            self._acquire(found, now)
            return
        if found is None:
            # lost: it holds where they went for LOST_GRACE (they come back there, mostly), then for sure:
            # to the middle, a few nods, and it rests until someone is there for sure
            if self._lost_since is None:
                self._lost_since = now
            elif now - self._lost_since >= LOST_GRACE:
                self._locked = False
                self._acquiring = 0
                if self.nod:
                    ActivityLog().info(f'[ptz] {self.name}: lost the person for sure, back to the middle, nodding, then resting')
                    self.go_home()
                    self._homing = True
                    QTimer.singleShot(HOME_SETTLE, self._start_nodding)
                else:
                    ActivityLog().info(f'[ptz] {self.name}: lost the person for sure, resting where they went')
            self._set_state('lost')
            return
        self._lost_since = None
        self._set_state('following')
        if self.ptz is None:
            return
        center, top, middle, area = found
        if self._previous is not None:
            self._check_pan_way(center - self._previous[1])
        if self._previous is not None and now > self._previous[0]:
            elapsed = now - self._previous[0]
            speed = ((center - self._previous[1]) / elapsed, (middle - self._previous[2]) / elapsed)
            self._speed = tuple(0.5 * old + 0.5 * new for old, new in zip(self._speed, speed))
        self._previous = (now, center, middle)
        self._choose_third(center)
        if getattr(self.ptz, 'can_drive', lambda: False)():
            self._drive(center + self._speed[0] * LEAD, _vertical_error(middle + self._speed[1] * LEAD, top))
            return
        x_error = center - self._target
        y_error = _vertical_error(middle, top)
        off = abs(x_error) > DEAD_ZONE or abs(y_error) > DEAD_ZONE
        self._off_looks = self._off_looks + 1 if off else 0
        if self._off_looks < OFFSET_LOOKS or now - self._last_move < self.move_interval:
            return          # close enough, or only just off (a gesture), or the camera is still getting there
        horizontal, vertical = self.ptz.field_of_view()
        pan = x_error * horizontal * GAIN if abs(x_error) > DEAD_ZONE else 0.0
        tilt = y_error * vertical * GAIN if abs(y_error) > DEAD_ZONE else 0.0
        if pan or tilt:
            self._last_move = now
            self.ptz.nudge_degrees(pan, tilt)

    def _acquire(self, found, now):
        """Not following anyone: someone is followed once there for sure (ACQUIRE_LOOKS in a row, big enough;
        longer while restless). Until then the camera stays still."""
        if found is None or found[3] < ACQUIRE_AREA:
            self._acquiring = 0
            self._set_state('lost' if self._homing or self._nod_timer.isActive() else 'resting')
            return
        self._acquiring += 1
        self._found_times = [moment for moment in self._found_times if now - moment < RESTLESS[1]]
        restless = len(self._found_times) >= RESTLESS[0]
        if self._acquiring < (RESTLESS_LOOKS if restless else ACQUIRE_LOOKS):
            return
        self._locked = True
        self._acquiring = 0
        self._found_times.append(now)
        self._lost_since = None
        self._homing = False
        self._off_looks = 0
        self._target = None
        if self._nod_timer.isActive():
            self._stop_nodding(back=False)
        ActivityLog().info(f'[ptz] {self.name}: following the person' + (' (after waiting: it was restless)' if restless else ''))
        self._set_state('following')

    def _check_pan_way(self, moved):
        """Turning right moves the person left across the picture: when they move right with it, look after
        look while it turns hard, the camera pans the other way than it says. Reversed then (as the PTZ
        menu's Reverse pan, saved), so that it does not run away from the person."""
        pan = self._last_pan
        if not AUTO_REVERSE or abs(pan) < 0.5 or self._pan_since is None or time.monotonic() - self._pan_since < WRONG_WAY_AFTER or \
                abs(moved) < WRONG_WAY_MOVE or not hasattr(self.ptz, 'pan_reversed'):
            self._wrong_way = 0
            return
        if (moved > 0) == (pan > 0):
            self._wrong_way += 1
            if self._wrong_way >= WRONG_WAY_LOOKS:
                self._wrong_way = 0
                self.ptz.pan_reversed = not self.ptz.pan_reversed
                ActivityLog().warning(f'[ptz] {self.name}: pans the other way than it says: reversed ({"on" if self.ptz.pan_reversed else "off"})')
                self._halt()
        else:
            self._wrong_way = 0

    def _choose_third(self, center):
        """The third line the person is kept on: the nearer one when they are found, then the same one."""
        if self._target is None:
            self._target = min(THIRDS, key=lambda third: abs(third - center))
        # it stays while the person is in sight: their motion across the picture is mostly the camera's own
        # turning, and switching on it sends the camera back and forth

    def _drive(self, center, y_error):
        """Velocity from how far off the person is (where they will be in LEAD): started beyond DEAD_ZONE,
        kept until within DRIVE_STOP, so that it does not stop and go around the target."""
        x_error = center - self._target
        driving = []
        velocity = []
        for error, was in ((x_error, self._driving[0]), (y_error, self._driving[1])):
            on = abs(error) > (DRIVE_STOP if was else DEAD_ZONE)
            driving.append(on)
            velocity.append(max(-1.0, min(1.0, error * DRIVE_GAIN)) if on else 0.0)
        self._driving = tuple(driving)
        hard = abs(velocity[0]) >= 0.5
        if not hard or self._pan_since is None or (velocity[0] > 0) != (self._last_pan > 0):
            self._pan_since = time.monotonic() if hard else None
        self._last_pan = velocity[0]
        self.ptz.drive(*velocity)
        if any(driving):
            self._watchdog.start()      # no look for a while: it stops rather than turn on blindly

    def _halt(self):
        """A driven camera stops."""
        if self.ptz is not None and any(self._driving) and getattr(self.ptz, 'can_drive', lambda: False)():
            self.ptz.drive(0.0, 0.0)
        self._driving = (False, False)
        self._last_pan = 0.0
        self._pan_since = None

    def _SH_ManualPause(self, paused):
        if paused:
            self._halt()                # the camera is the user's now: nothing of ours keeps turning it
            self._stop_nodding(back=False)
            self._homing = False
            self._set_state('paused')
        else:
            self._last_move = 0.0
            self._set_state('following')

    def go_home(self):
        """Back to the middle (a camera with PTZ that can)."""
        self._halt()
        if self.ptz is not None and hasattr(self.ptz, 'go_home'):
            self.ptz.go_home()

    def _start_nodding(self):
        if not self._running or not self._homing:
            return
        self._homing = False
        self._nod_step = 0
        self._nod_offset = 0.0
        self._nod_timer.start()

    def _nod(self):
        offsets = (NOD_DEGREES, 0.0, -NOD_DEGREES, 0.0)     # up, back, down, back
        if self._nod_step >= NODS * len(offsets):
            self._nod_timer.stop()      # done nodding: it rests in the middle until the person is seen again
            return
        target = offsets[self._nod_step % len(offsets)]
        self._nod_step += 1
        self.ptz.nudge_degrees(0.0, target - self._nod_offset)
        self._nod_offset = target

    def _stop_nodding(self, back):
        self._nod_timer.stop()
        if back and self._nod_offset:
            self.ptz.nudge_degrees(0.0, -self._nod_offset)
        self._nod_offset = 0.0


def _vertical_error(middle, top):
    """How far to tilt up (> 0) or down: the center to the lower third line, but up as much as it takes to
    keep the head HEAD_MARGIN from the top edge."""
    error = LOWER_THIRD - middle
    if top < HEAD_MARGIN:
        error = max(error, HEAD_MARGIN - top)
    return error


def _person(np, probability):
    """(horizontal center, top, vertical center, area) of the person, 0..1 of the picture, or None when there is nobody."""
    mask = probability > PERSON
    size = mask.shape[0] * mask.shape[1]
    if mask.sum() < MIN_AREA * size:
        return None
    columns = mask.sum(axis=0).astype(np.float32)
    center = float((columns * np.arange(mask.shape[1])).sum() / columns.sum()) / mask.shape[1]
    counts = mask.sum(axis=1).astype(np.float32)
    rows = np.nonzero(counts >= max(2, mask.shape[1] * 0.02))[0]
    if rows.size == 0:
        return None
    middle = float((counts * np.arange(mask.shape[0])).sum() / counts.sum()) / mask.shape[0]
    return center, float(rows[0]) / mask.shape[0], middle, float(mask.sum()) / size


class FollowMe(QObject):
    """Following the person with the camera Blink sends (a singleton: FollowMe.instance())."""

    changed = pyqtSignal()      # enabled, available or state changed

    SETTING = 'video/follow_me'
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super(FollowMe, self).__init__()
        self._users = {}            # {id(owner): the camera name it shows, None for none}
        self._camera_name = None
        self.ptz = None             # blink.webrtc.uvcptz.UvcPtz of that camera, when it has PTZ
        self.tracker = None
        self.suspended = False      # a video room's director drives the camera

    @property
    def enabled(self):
        from blink.resources import ui_settings
        return str(ui_settings().value(self.SETTING, 'false')).lower() == 'true'

    def set_enabled(self, enabled):
        from blink.resources import ui_settings
        if enabled != self.enabled:
            ui_settings().setValue(self.SETTING, 'true' if enabled else 'false')
            ActivityLog().info(f'[ptz] Follow me {"on" if enabled else "off"}')
        self._apply()
        self.changed.emit()

    def toggle(self):
        """On or off; False when it cannot be turned on (no camera that can turn now)."""
        if not self.enabled and not self.available:
            return False
        self.set_enabled(not self.enabled)
        return True

    @property
    def available(self):
        """Whether the camera shown can follow (PTZ by degrees, and the person model is there)."""
        return can_track(self.ptz)

    @property
    def state(self):
        """'following', 'lost' (nodding) or 'off'."""
        return self.tracker.state if self.tracker is not None and self.tracker.running else 'off'

    def show(self, owner, camera_name):
        """owner shows the camera Blink sends, by this name (None: none, the screen or the camera off)."""
        self._users[id(owner)] = camera_name
        self._update_camera()

    def hide(self, owner):
        self._users.pop(id(owner), None)
        self._update_camera()

    def _update_camera(self):
        names = [name for name in self._users.values() if name]
        name = names[-1] if names else None
        if name != self._camera_name:
            self._camera_name = name
            self._stop()
            from blink.webrtc.uvcptz import find
            try:
                self.ptz = find(name) if name else None
            except Exception as e:
                ActivityLog().warning(f'[ptz] {name}: {e}')
                self.ptz = None
        self._apply()
        self.changed.emit()

    def _apply(self):
        """Follow when on, shown and possible, and the director does not drive; else not."""
        if self.enabled and self._camera_name and self.available and not self.suspended:
            if self.tracker is None or not self.tracker.running:
                tracker = PersonTracker(self.ptz, self)
                tracker.stateChanged.connect(lambda state: self.changed.emit())
                try:
                    tracker.start()
                except Exception as e:
                    ActivityLog().warning(f'[ptz] {self.ptz.host}: cannot follow: {e}')
                    tracker.deleteLater()
                    return
                self.tracker = tracker
        else:
            self._stop()

    def _stop(self):
        tracker, self.tracker = self.tracker, None
        if tracker is not None:
            tracker.stop()
            tracker.deleteLater()

    def suspend(self, suspended):
        """The director of a video room drives the cameras (it has its own tracker for this one), or no longer."""
        self.suspended = suspended
        self._apply()
        self.changed.emit()

    def user_drove(self):
        """A PTZ control pressed by hand: every tracker pauses (ManualPause), Follow me stays on."""
        ManualPause.instance().touched()
