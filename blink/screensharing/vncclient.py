# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

__all__ = ['VNCClient', 'RFBSettings', 'ServerDefault', 'TrueColor', 'HighColor', 'LowColor']


from PyQt4.QtCore import QObject, QSize, QSocketNotifier, QThread, pyqtSignal
from PyQt4.QtGui  import QApplication

from application.notification import NotificationCenter
from application.python import Null
from application.python.descriptor import WriteOnceAttribute

from blink.event import EventBase
from blink.screensharing._rfb  import RFBClient, RFBClientError


class RFBSettings(object):
    depth = WriteOnceAttribute()
    quality = WriteOnceAttribute()
    compression = WriteOnceAttribute()
    encodings = WriteOnceAttribute()

    def __init__(self, depth, quality, compression, encodings):
        if depth not in (8, 16, 24, 32, None):
            raise ValueError("invalid depth value: %r (should be one of 8, 16, 24, 32 or None)" % depth)
        allowed_levels = range(10)
        if quality not in allowed_levels:
            raise ValueError("invalid quality value: %r (should be between 0..9)" % quality)
        if compression not in allowed_levels:
            raise ValueError("invalid compression value: %r (should be between 0..9)" % compression)
        if not isinstance(encodings, str) or not encodings:
            raise ValueError("invalid encodings value: %r (should be a non-empty string)" % encodings)
        self.depth = depth
        self.quality = quality
        self.compression = compression
        self.encodings = encodings

    def __eq__(self, other):
        if isinstance(other, RFBSettings):
            return (self.depth, self.quality, self.compression, self.encodings) == (other.depth, other.quality, other.compression, other.encodings)
        return NotImplemented

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        return '{0.__class__.__name__}(depth={0.depth!r}, quality={0.quality!r}, compression={0.compression!r}, encodings={0.encodings!r})'.format(self)


ServerDefault = RFBSettings(depth=None, quality=9, compression=9, encodings='copyrect zlib zrle ultra hextile corre rre raw')
TrueColor     = RFBSettings(depth=24,   quality=9, compression=9, encodings='copyrect zlib zrle ultra hextile corre rre raw')
HighColor     = RFBSettings(depth=16,   quality=9, compression=9, encodings='copyrect zlib zrle ultra hextile corre rre raw')
LowColor      = RFBSettings(depth=8,    quality=9, compression=9, encodings='copyrect zlib zrle ultra hextile corre rre raw')


class RFBConfigureClientEvent(EventBase):
    pass


class RFBKeyEvent(EventBase):
    def __init__(self, key, down):
        super(RFBKeyEvent, self).__init__()
        self.key = key
        self.down = down


class RFBMouseEvent(EventBase):
    def __init__(self, x, y, button_mask):
        super(RFBMouseEvent, self).__init__()
        self.x = x
        self.y = y
        self.button_mask = button_mask


class RFBCutTextEvent(EventBase):
    def __init__(self, text):
        super(RFBCutTextEvent, self).__init__()
        self.text = text


class VNCClient(QObject):
    started = pyqtSignal()
    finished = pyqtSignal()
    imageSizeChanged = pyqtSignal(QSize)
    imageChanged = pyqtSignal(int, int, int, int)
    passwordRequested = pyqtSignal(bool)
    textCut = pyqtSignal(unicode)

    def __init__(self, host, port, settings, parent=None):
        super(VNCClient, self).__init__(parent)
        self.thread = QThread()
        self.moveToThread(self.thread)
        self.host = host
        self.port = port
        self.settings = settings
        self.username = None
        self.password = None
        self.rfb_client = None
        self.socket_notifier = None
        self.thread.started.connect(self._SH_ThreadStarted)
        self.thread.finished.connect(self._SH_ThreadFinished)

    def _get_settings(self):
        return self.__dict__['settings']
    def _set_settings(self, settings):
        old_settings = self.__dict__.get('settings', None)
        if settings == old_settings:
            return
        self.__dict__['settings'] = settings
        if self.thread.isRunning():
            QApplication.postEvent(self, RFBConfigureClientEvent())
    settings = property(_get_settings, _set_settings)
    del _get_settings, _set_settings

    @property
    def image(self):
        return self.rfb_client.image if self.rfb_client is not None else None

    def start(self):
        self.thread.start()

    def stop(self):
        self.thread.quit()

    def key_event(self, key, down):
        if self.thread.isRunning():
            QApplication.postEvent(self, RFBKeyEvent(key, down))

    def mouse_event(self, x, y, button_mask):
        if self.thread.isRunning():
            QApplication.postEvent(self, RFBMouseEvent(x, y, button_mask))

    def cut_text_event(self, text):
        if text and self.thread.isRunning():
            QApplication.postEvent(self, RFBCutTextEvent(text))

    def _SH_ThreadStarted(self):
        self.started.emit()
        notification_center = NotificationCenter()
        notification_center.post_notification('VNCClientWillStart', sender=self)
        self.rfb_client = RFBClient(parent=self)
        try:
            self.rfb_client.connect()
        except RFBClientError:
            self.thread.quit()
        else:
            self.socket_notifier = QSocketNotifier(self.rfb_client.socket, QSocketNotifier.Read, self)
            self.socket_notifier.activated.connect(self._SH_SocketNotifierActivated)
            notification_center.post_notification('VNCClientDidStart', sender=self)

    def _SH_ThreadFinished(self):
        self.finished.emit()
        notification_center = NotificationCenter()
        notification_center.post_notification('VNCClientWillEnd', sender=self)
        if self.socket_notifier is not None:
            self.socket_notifier.activated.disconnect(self._SH_SocketNotifierActivated)
            self.socket_notifier = None
        self.rfb_client = None
        notification_center.post_notification('VNCClientDidEnd', sender=self)

    def _SH_SocketNotifierActivated(self, sock):
        self.socket_notifier.setEnabled(False)
        try:
            self.rfb_client.handle_server_message()
        except RFBClientError:
            self.thread.quit()
        else:
            self.socket_notifier.setEnabled(True)

    def _SH_ConfigureRFBClient(self):
        if self.rfb_client is not None:
            self.rfb_client.configure()

    def customEvent(self, event):
        handler = getattr(self, '_EH_%s' % event.name, Null)
        handler(event)

    def _EH_RFBConfigureClientEvent(self, event):
        if self.rfb_client is not None:
            self.rfb_client.configure()

    def _EH_RFBKeyEvent(self, event):
        if self.rfb_client is not None:
            self.rfb_client.send_key_event(event.key, event.down)

    def _EH_RFBMouseEvent(self, event):
        if self.rfb_client is not None:
            self.rfb_client.send_pointer_event(event.x, event.y, event.button_mask)

    def _EH_RFBCutTextEvent(self, event):
        if self.rfb_client is not None:
            self.rfb_client.send_client_cut_text(event.text.encode('utf8'))


