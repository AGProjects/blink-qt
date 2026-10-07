"""Where this computer is, once: for "Send Current Location".

QtPositioning asks the desktop's location service (GeoClue on Linux, which may
ask the user first); the answer, or why there is none, comes back to the
callback within timeout seconds. Without QtPositioning, or without a location
service, positioning_available() says why and the menu item is disabled.
"""

from PyQt6.QtCore import QObject

try:
    from PyQt6.QtPositioning import QGeoPositionInfo, QGeoPositionInfoSource
except ImportError as e:
    QGeoPositionInfoSource = None
    _import_error = str(e)
else:
    _import_error = None

from blink.logging import ActivityLog
from blink.util import translate


__all__ = ['positioning_available', 'CurrentPosition']


timeout = 20        # seconds


_logged = None


def positioning_available():
    """(True, '') or (False, why): why is shown in the menu, and logged once."""
    global _logged
    if QGeoPositionInfoSource is None:
        why = translate('location', 'QtPositioning cannot be loaded (python3-pyqt6.qtpositioning): %s') % _import_error
    else:
        sources = QGeoPositionInfoSource.availableSources()
        if not sources:
            why = translate('location', 'Qt has no positioning plugins (install libqt6positioning6-plugins)')
        elif 'geoclue2' not in sources:
            why = translate('location', 'Qt has no GeoClue plugin, only: %s') % ', '.join(sources)
        else:
            why = ''
    if why != _logged:
        _logged = why
        if why:
            ActivityLog().warning(f'[location] Current location not available: {why}')
        else:
            ActivityLog().info(f'[location] Current location from {", ".join(QGeoPositionInfoSource.availableSources())}')
    return not why, why


class CurrentPosition(QObject):
    """One position request; done(coords dict or None, why) is called once."""

    _running = set()        # keeps a request alive until it answers

    def __init__(self, done, parent=None):
        super().__init__(parent)
        self.done = done
        self.source = None
        if QGeoPositionInfoSource is not None:
            # GeoClue wants to know who asks (the .desktop file's name), for its permission prompt
            from PyQt6.QtGui import QGuiApplication
            parameters = {'desktopId': QGuiApplication.desktopFileName() or 'blink'}
            if 'geoclue2' in QGeoPositionInfoSource.availableSources():
                self.source = QGeoPositionInfoSource.createSource('geoclue2', parameters, self)
            if self.source is None:
                self.source = QGeoPositionInfoSource.createDefaultSource(parameters, self)

    @classmethod
    def request(cls, done):
        request = cls(done)
        if request.source is None:
            done(None, positioning_available()[1] or translate('location', 'No location service is available'))
            return
        cls._running.add(request)
        request.source.positionUpdated.connect(request._SH_Position)
        request.source.errorOccurred.connect(request._SH_Error)
        ActivityLog().info(f'[location] Asking {request.source.sourceName()} for the current position')
        request.source.requestUpdate(timeout * 1000)

    def _finish(self, coords, why):
        if self not in self._running:
            return
        self._running.discard(self)
        try:
            self.done(coords, why)
        finally:
            self.deleteLater()

    def _SH_Position(self, info):
        coordinate = info.coordinate()
        if not coordinate.isValid():
            self._finish(None, translate('location', 'The position is not known'))
            return
        coords = {'latitude': coordinate.latitude(), 'longitude': coordinate.longitude(),
                  'timestamp': info.timestamp().toUTC().toString('yyyy-MM-ddTHH:mm:ss.zzzZ') if info.timestamp().isValid() else None}
        if info.hasAttribute(QGeoPositionInfo.Attribute.HorizontalAccuracy):
            coords['accuracy'] = info.attribute(QGeoPositionInfo.Attribute.HorizontalAccuracy)
        ActivityLog().info(f'[location] Current position known' + (f' to {coords["accuracy"]:.0f} m' if coords.get('accuracy') else ''))
        self._finish(coords, '')

    def _SH_Error(self, error):
        reasons = {QGeoPositionInfoSource.Error.AccessError: translate('location', 'Access to the location was refused. Turn on Location Services in the '
                                                                             'system settings (Privacy, Location Services) and allow Blink.'),
                   QGeoPositionInfoSource.Error.ClosedError: translate('location', 'The location service is turned off'),
                   QGeoPositionInfoSource.Error.UpdateTimeoutError: translate('location', 'No position within %d seconds') % timeout}
        why = reasons.get(error, translate('location', 'The position could not be found'))
        ActivityLog().warning(f'[location] {why}')
        self._finish(None, why)
