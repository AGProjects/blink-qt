"""Follow me in a video room: every camera we bring with PTZ (ours, a USB camera over V4L2, and our
extra videos: RTSP cameras over ONVIF, other USB cameras) keeps us in the picture, each on its own
(blink.webrtc.tracker.PersonTracker); never the other participants' cameras. Nothing else changes: the
room shows what it showed, and audio is left alone. A camera that loses us rests where we went, until
it sees us again. It is the one Follow me setting (blink.webrtc.tracker.FollowMe: the join dialog, the
button on our tile, the menus, D in the window); FollowMe's own tracker is suspended meanwhile, as our
camera is one of these.
"""

from PyQt6.QtCore import QObject, pyqtSignal

from blink.logging import ActivityLog


__all__ = ['Director']


RTSP_MOVE_INTERVAL = 1.0    # s between two turns of an RTSP camera: its picture comes a moment late


class _Camera(object):
    def __init__(self, id, name, ptz, tracker):
        self.id = id
        self.name = name
        self.ptz = ptz
        self.tracker = tracker

    def __repr__(self):
        return f'<Camera {self.name} {self.id}>'


class Director(QObject):
    changed = pyqtSignal()          # started or stopped
    sightChanged = pyqtSignal()     # a camera got someone in sight, or lost them

    def __init__(self, window):
        super(Director, self).__init__(window)
        self.window = window
        self.session = window.session
        self.cameras = []
        self.active = None          # nothing is cut to: kept for the window, always None
        self._restarting = False

    @property
    def running(self):
        return bool(self.cameras)

    def candidates(self):
        """[(id, name, ptz, source, move interval)] of our cameras sending now that can turn."""
        from blink.webrtc.tracker import CameraSource, ExtraVideoSource, FollowMe, MOVE_INTERVAL, can_track
        from blink.webrtc.videoroom import VideoroomSession
        candidates = []
        follow_me = FollowMe.instance()
        if not self.session.video_muted and not VideoroomSession.sending_screen_device() and can_track(follow_me.ptz):
            candidates.append((self.session.id, 'My camera', follow_me.ptz, CameraSource(), MOVE_INTERVAL))
        for extra in list(self.session.extra_videos.values()):
            tile = self.window.tiles.get(extra.participant)
            if extra.state == 'established' and tile is not None and can_track(tile.ptz):
                interval = RTSP_MOVE_INTERVAL if extra.source.get('kind') == 'rtsp' else MOVE_INTERVAL
                candidates.append((extra.participant.id, extra.label, tile.ptz, ExtraVideoSource(extra), interval))
        return candidates

    @staticmethod
    def possible(candidates):
        return bool(candidates)

    def start(self):
        """False when no camera of ours can turn (or the person model is not there)."""
        from blink.webrtc.tracker import FollowMe, PersonTracker
        if self.running:
            return True
        candidates = self.candidates()
        if not candidates:
            return False
        FollowMe.instance().suspend(True)
        for id, name, ptz, source, interval in candidates:
            tracker = PersonTracker(ptz, self, source=source, move_interval=interval)
            camera = _Camera(id, name, ptz, tracker)
            tracker.personChanged.connect(lambda seen: self.sightChanged.emit())
            tracker.stateChanged.connect(lambda state: FollowMe.instance().changed.emit())     # our tile's Follow me button shows it
            try:
                tracker.start()
            except Exception as e:
                ActivityLog().warning(f'[director] {name}: cannot follow: {e}')
                continue
            self.cameras.append(camera)
        if not self.cameras:
            self.stop()
            return False
        ActivityLog().info(f'[director] {self.session.room}: follow me with {", ".join(camera.name for camera in self.cameras)}')
        self.changed.emit()
        return True

    def stop(self, restore=True):
        from blink.webrtc.tracker import FollowMe
        cameras, self.cameras = self.cameras, []
        for camera in cameras:
            camera.tracker.stop()
            camera.tracker.deleteLater()
        FollowMe.instance().suspend(False)
        if cameras and restore:
            ActivityLog().info(f'[director] {self.session.room}: follow me stopped')
        self.changed.emit()

    def restart(self):
        """Again with the cameras there are now (one came or went)."""
        self._restarting = True
        try:
            self.stop(restore=False)
            return self.start()
        finally:
            self._restarting = False

    def toggle(self):
        if self.running:
            self.stop()
            return True
        return self.start()

    def cut(self, camera):
        """Nothing is cut to any more: each camera only follows."""

    def cut_to(self, number):
        pass

    def forget(self, id):
        """A camera went (its extra video ended): the others carry on."""
        camera = next((camera for camera in self.cameras if camera.id == id), None)
        if camera is not None:
            camera.tracker.stop()
            self.cameras.remove(camera)
            if not self.cameras:
                self.stop()
