# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from __future__ import division


__all__ = ['ScreensharingWindow', 'VNCViewer']


import os
import platform

from PyQt4 import uic
from PyQt4.QtCore import Qt, QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRect, QTimer, QUrl
from PyQt4.QtGui  import QApplication, QDesktopServices, QFrame, QIcon, QImage, QMenu, QPainter, QStyle, QStyleOption, QStylePainter, QTransform, QWidget, qRgb

from application.system import makedirs
from collections import defaultdict
from datetime import datetime
from functools import reduce
from itertools import count
from operator  import __or__

from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer
from sipsimple.threading import run_in_thread

from blink.configuration.settings import BlinkSettings
from blink.resources import Resources
from blink.screensharing.vncclient import ServerDefault, TrueColor, HighColor, LowColor


class ButtonMaskMapper(dict):
    class qt:
        NoButton    = Qt.NoButton
        LeftButton  = Qt.LeftButton
        MidButton   = Qt.MidButton
        RightButton = Qt.RightButton

    class vnc:
        NoButton    = 0b00000000
        LeftButton  = 0b00000001
        MidButton   = 0b00000010
        RightButton = 0b00000100
        WheelUp     = 0b00001000
        WheelDown   = 0b00010000
        WheelLeft   = 0b00100000 # not sure about the horizontal wheel button mappings as button 6&7 -Dan
        WheelRight  = 0b01000000 # it seems different on windows (i.e. buttons 6&7 do not translate to horizontal wheel) -Dan

    def __init__(self):
        mapping = {self.qt.NoButton: self.vnc.NoButton, self.qt.LeftButton: self.vnc.LeftButton, self.qt.MidButton: self.vnc.MidButton, self.qt.RightButton: self.vnc.RightButton}
        super(ButtonMaskMapper, self).__init__({int(b1|b2|b3): mapping[b1]|mapping[b2]|mapping[b3] for b1 in mapping for b2 in mapping for b3 in mapping})
        self._button_mask = int(self.qt.LeftButton|self.qt.MidButton|self.qt.RightButton)

    def __getitem__(self, key):
        return super(ButtonMaskMapper, self).__getitem__(int(key & self._button_mask))


class VNCNativeKeyMap(object):
    def __getitem__(self, event):
        return event.nativeVirtualKey()


class VNCWindowsKeyMap(object):
    __keymap__ = {
        0x10: 0xffe1, # VK_SHIFT    : XK_Shift_L
        0x11: 0xffe3, # VK_CONTROL  : XK_Control_L
        0x12: 0xffe9, # VK_MENU     : XK_Alt_L
        0x5b: 0xffe7, # VK_LWIN     : XK_Meta_L
        0x1b: 0xff1b, # VK_ESCAPE   : XK_Escape
        0x09: 0xff09, # VK_TAB      : XK_Tab
        0x08: 0xff08, # VK_BACK     : XK_BackSpace
        0x0d: 0xff0d, # VK_RETURN   : XK_Return
        0x2d: 0xff63, # VK_INSERT   : XK_Insert
        0x2e: 0xffff, # VK_DELETE   : XK_Delete
        0x13: 0xff13, # VK_PAUSE    : XK_Pause
        0x2c: 0xff61, # VK_SNAPSHOT : XK_Print
        0x24: 0xff50, # VK_HOME     : XK_Home
        0x23: 0xff57, # VK_END      : XK_End
        0x25: 0xff51, # VK_LEFT     : XK_Left
        0x26: 0xff52, # VK_UP       : XK_Up
        0x27: 0xff53, # VK_RIGHT    : XK_Right
        0x28: 0xff54, # VK_DOWN     : XK_Down
        0x21: 0xff55, # VK_PRIOR    : XK_Prior
        0x22: 0xff56, # VK_NEXT     : XK_Next
        0x14: 0xffe5, # VK_CAPITAL  : XK_Caps_Lock
        0x90: 0xff7f, # VK_NUMLOCK  : XK_Num_Lock
        0x91: 0xff14, # VK_SCROLL   : XK_Scroll_Lock
        0x5d: 0xff67, # VK_APPS     : XK_Menu
        0x2f: 0xff6a, # VK_HELP     : XK_Help
        0x1f: 0xff7e, # VK_MODECHANGE : XK_Mode_switch

        0x70: 0xffbe, # VK_F1  : XK_F1
        0x71: 0xffbf, # VK_F2  : XK_F2
        0x72: 0xffc0, # VK_F3  : XK_F3
        0x73: 0xffc1, # VK_F4  : XK_F4
        0x74: 0xffc2, # VK_F5  : XK_F5
        0x75: 0xffc3, # VK_F6  : XK_F6
        0x76: 0xffc4, # VK_F7  : XK_F7
        0x77: 0xffc5, # VK_F8  : XK_F8
        0x78: 0xffc6, # VK_F9  : XK_F9
        0x79: 0xffc7, # VK_F10 : XK_F10
        0x7a: 0xffc8, # VK_F11 : XK_F11
        0x7b: 0xffc9, # VK_F12 : XK_F12
        0x7c: 0xffca, # VK_F13 : XK_F13
        0x7d: 0xffcb, # VK_F14 : XK_F14
        0x7e: 0xffcc, # VK_F15 : XK_F15
        0x7f: 0xffcd, # VK_F16 : XK_F16
        0x80: 0xffce, # VK_F17 : XK_F17
        0x81: 0xffcf, # VK_F18 : XK_F18
        0x82: 0xffd0, # VK_F19 : XK_F19
        0x83: 0xffd1, # VK_F20 : XK_F20
        0x84: 0xffd2, # VK_F21 : XK_F21
        0x85: 0xffd3, # VK_F22 : XK_F22
        0x86: 0xffd4, # VK_F23 : XK_F23
        0x87: 0xffd5, # VK_F24 : XK_F24

        0x93: 0xff2c, # VK_OEM_FJ_MASSHOU : XK_Massyo
        0x94: 0xff2b, # VK_OEM_FJ_TOUROKU : XK_Touroku
    }

    __capsmodifier__ = 0x100 # the native CapsLock modifier

    def __getitem__(self, event):
        key = event.key()
        if Qt.Key_A <= key <= Qt.Key_Z:
            if bool(event.modifiers() & Qt.ShiftModifier) ^ bool(event.nativeModifiers() & self.__capsmodifier__):
                return key
            else:
                return key + 0x20 # make it lowercase
        else:
            native_key = event.nativeVirtualKey()
            return self.__keymap__.get(native_key, native_key)


class VNCKeyMap(object):
    """Return the key mapper for the platform"""

    __keymaps__ = defaultdict(VNCNativeKeyMap)
    __keymaps__['windows'] = VNCWindowsKeyMap()

    def __new__(cls):
        return cls.__keymaps__[platform.system().lower()]


class VNCKey(int):
    __modifiermap__ = {Qt.Key_Shift: Qt.ShiftModifier, Qt.Key_Control: Qt.ControlModifier, Qt.Key_Alt: Qt.AltModifier, Qt.Key_Meta: Qt.MetaModifier, Qt.Key_AltGr: Qt.GroupSwitchModifier}
    __keymap__ = VNCKeyMap()

    def __new__(cls, key, qt_key):
        instance = super(VNCKey, cls).__new__(cls, key)
        instance.qt_key = qt_key
        return instance

    @property
    def qt_modifier(self):
        return self.__modifiermap__.get(self.qt_key, None)

    @classmethod
    def from_event(cls, event):
        return cls(cls.__keymap__[event], event.key())


class ActiveKeys(set):
    @property
    def modifiers(self):
        return reduce(__or__, (key.qt_modifier for key in self if key.qt_modifier is not None), Qt.NoModifier)


class VNCViewer(QWidget):
    button_mask_map = ButtonMaskMapper()

    def __init__(self, vncclient=None, parent=None):
        super(VNCViewer, self).__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.WheelFocus)
        #self.setCursor(Qt.BlankCursor)
        self.client = vncclient or parent.client
        self.client.started.connect(self._SH_ClientStarted)
        self.client.finished.connect(self._SH_ClientFinished)
        self.client.imageSizeChanged.connect(self._SH_ImageSizeChanged)
        self.client.imageChanged.connect(self._SH_ImageUpdated)
        self.client.passwordRequested.connect(self._SH_PasswordRequested, Qt.BlockingQueuedConnection)
        self.colors_8bit = [qRgb((i&0x07) << 5, (i&0x38) << 2, i&0xc0) for i in range(256)]
        self.scale = False
        self.view_only = False
        self._client_active = False
        self._has_mouse_over = False
        self._active_keys = ActiveKeys()

    def _get_scale(self):
        return self.__dict__['scale']
    def _set_scale(self, scale):
        self.__dict__['scale'] = scale
        if not scale:
            image = self.client.image or QImage()
            if not image.isNull():
                self.resize(image.size())
        elif self.parent() is not None:
            self.resize(self.parent().size())
        self.update()
    scale = property(_get_scale, _set_scale)
    del _get_scale, _set_scale

    def _get_view_only(self):
        return self.__dict__['view_only']
    def _set_view_only(self, view_only):
        old_value = self.__dict__.get('view_only', None)
        new_value = self.__dict__['view_only'] = view_only
        if old_value is None or old_value == new_value:
            return
        if self._client_active and self._has_mouse_over and self.hasFocus() and not view_only:
            self.grabKeyboard()
        else:
            self.releaseKeyboard()
    view_only = property(_get_view_only, _set_view_only)
    del _get_view_only, _set_view_only

    @property
    def transform(self):
        try:
            return self.__dict__['transform'] if self.scale else QTransform()
        except KeyError:
            transform = QTransform()
            image = self.client.image
            if image is not None and not image.isNull():
                scale = min(self.width()/image.width(), self.height()/image.height())
                transform.translate((self.width() - image.width()*scale)/2, (self.height() - image.height()*scale)/2)
                transform.scale(scale, scale)
                transform = self.__dict__.setdefault('transform', transform)
            return transform

    def event(self, event):
        event_type = event.type()
        if event_type in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick, QEvent.MouseMove, QEvent.Wheel):
            if not self.view_only:
                event.accept()
                self.mouseEvent(event)
            else:
                event.ignore()
            return True
        elif event_type in (QEvent.KeyPress, QEvent.KeyRelease):
            if not self.view_only:
                event.accept()
                self.keyEvent(event)
            else:
                event.ignore()
            return True
        return super(VNCViewer, self).event(event)

    def paintEvent(self, event):
        event_rect = event.rect()
        image = self.client.image
        painter = QPainter(self)
        if image is None or image.isNull():
            pass
        elif self.scale:
            inverse_transform, invertible = self.transform.inverted()
            image_rect = inverse_transform.mapRect(event_rect).adjusted(-1, -1, 1, 1).intersected(image.rect())
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.setTransform(self.transform)
            if image.format() == QImage.Format_Indexed8:
                image_area = image.copy(image_rect)
                image_area.setColorTable(self.colors_8bit)
                painter.drawImage(image_rect, image_area)
            else:
                painter.drawImage(image_rect, image, image_rect)
        elif image.format() == QImage.Format_Indexed8:
            image_area = image.copy(event_rect)
            image_area.setColorTable(self.colors_8bit)
            painter.drawImage(event_rect, image_area)
        else:
            painter.drawImage(event_rect, image, event_rect)

    def resizeEvent(self, event):
        self.__dict__.pop('transform', None)
        super(VNCViewer, self).resizeEvent(event)

    def enterEvent(self, event):
        self._has_mouse_over = True
        if self._client_active and self.hasFocus() and not self.view_only:
            self.grabKeyboard()
        super(VNCViewer, self).enterEvent(event)

    def leaveEvent(self, event):
        self._has_mouse_over = False
        self.releaseKeyboard()
        super(VNCViewer, self).leaveEvent(event)

    def focusInEvent(self, event):
        if self._client_active and self._has_mouse_over and not self.view_only:
            self.grabKeyboard()
        super(VNCViewer, self).focusInEvent(event)

    def focusOutEvent(self, event):
        self.releaseKeyboard()
        self._reset_keyboard()
        super(VNCViewer, self).focusOutEvent(event)

    def mouseEvent(self, event):
        if self.scale:
            inverse_transform, invertible = self.transform.inverted()
            image_pos = inverse_transform.map(event.pos())
            x = round(image_pos.x())
            y = round(image_pos.y())
        else:
            x = event.x()
            y = event.y()
        button_mask = self.button_mask_map[event.buttons()]
        if event.type() == QEvent.Wheel:
            if event.delta() > 0:
                wheel_button_mask = self.button_mask_map.vnc.WheelUp if event.orientation()==Qt.Vertical else self.button_mask_map.vnc.WheelLeft
            else:
                wheel_button_mask = self.button_mask_map.vnc.WheelDown if event.orientation()==Qt.Vertical else self.button_mask_map.vnc.WheelRight
            self.client.mouse_event(x, y, button_mask | wheel_button_mask)
        self.client.mouse_event(x, y, button_mask)

    def keyEvent(self, event):
        vnc_key = VNCKey.from_event(event)
        key_down = event.type()==QEvent.KeyPress
        if vnc_key:
            expected_modifiers = self._active_keys.modifiers
            keyboard_modifiers = event.modifiers()
            if vnc_key.qt_key in VNCKey.__modifiermap__ and vnc_key.qt_key != Qt.Key_AltGr and vnc_key != 0xffe7:
                keyboard_modifiers ^= vnc_key.qt_modifier # we want the modifier mask as it was before this modifier key was pressed/released
            if (keyboard_modifiers ^ expected_modifiers) & expected_modifiers:
                self._reset_keyboard(preserve_modifiers=keyboard_modifiers)
            if key_down:
                if vnc_key in self._active_keys:
                    self.client.key_event(vnc_key, False) # key was already pressed and now we got another press event. emulate the missing key release event.
                else:
                    self._active_keys.add(vnc_key)
                self.client.key_event(vnc_key, True)
            else:
                if vnc_key in self._active_keys:
                    self._active_keys.remove(vnc_key)
                    self.client.key_event(vnc_key, False)
                else:
                    self._reset_keyboard(preserve_modifiers=event.modifiers())

    def _reset_keyboard(self, preserve_modifiers=Qt.NoModifier):
        dated_keys = {key for key in self._active_keys if key.qt_modifier is None or not (key.qt_modifier & preserve_modifiers)}
        for vnc_key in dated_keys:
            self.client.key_event(vnc_key, False)
        self._active_keys -= dated_keys

    def _SH_ClientStarted(self):
        self._client_active = True
        if self._has_mouse_over and self.hasFocus() and not self.view_only:
            self.grabKeyboard()

    def _SH_ClientFinished(self):
        self._client_active = False
        self.releaseKeyboard()

    def _SH_ImageSizeChanged(self, size):
        self.__dict__.pop('transform', None)
        if not self.scale:
            self.resize(size)
        elif self.parent() is not None:
            self.resize(self.parent().size())

    def _SH_ImageUpdated(self, x, y, w, h):
        if self.scale:
            self.update(self.transform.mapRect(QRect(x, y, w, h)).adjusted(-1, -1, 1, 1).intersected(self.rect()))
        else:
            self.update(x, y, w, h)

    def _SH_PasswordRequested(self, with_username):
        dialog = ScreensharingDialog(self)
        if with_username:
            username, password = dialog.get_credentials()
        else:
            username, password = None, dialog.get_password()
        self.client.username = username
        self.client.password = password


ui_class, base_class = uic.loadUiType(Resources.get('screensharing_dialog.ui'))

class ScreensharingDialog(base_class, ui_class):
    def __init__(self, parent=None):
        super(ScreensharingDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.setWindowModality(Qt.WindowModal)
        parent.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() in (QEvent.Close, QEvent.Hide):
            self.reject()
        return False

    def get_credentials(self):
        self.message_label.setText(u'Screen sharing requires authentication')
        self.username_label.show()
        self.username_editor.show()
        self.username_editor.clear()
        self.password_editor.clear()
        self.username_editor.setFocus(Qt.OtherFocusReason)
        self.setMinimumHeight(190)
        self.resize(self.minimumSize())
        result = self.exec_()
        return (self.username_editor.text(), self.password_editor.text()) if result==self.Accepted else (None, None)

    def get_password(self):
        self.message_label.setText(u'Screen sharing requires a password')
        self.username_label.hide()
        self.username_editor.hide()
        self.username_editor.clear()
        self.password_editor.clear()
        self.password_editor.setFocus(Qt.OtherFocusReason)
        self.setMinimumHeight(165)
        self.resize(self.minimumSize())
        result = self.exec_()
        return self.password_editor.text() if result==self.Accepted else None

del ui_class, base_class


ui_class, base_class = uic.loadUiType(Resources.get('screensharing_toolbox.ui'))

class ScreensharingToolbox(base_class, ui_class):
    exposedPixels = 3

    def __init__(self, parent):
        super(ScreensharingToolbox, self).__init__(parent)
        with Resources.directory:
            self.setupUi()
        parent.installEventFilter(self)
        self.animation = QPropertyAnimation(self, 'pos')
        self.animation.setDuration(250)
        self.animation.setDirection(QPropertyAnimation.Forward)
        self.animation.setEasingCurve(QEasingCurve.Linear) # or OutCirc with 300ms
        self.retract_timer = QTimer(self)
        self.retract_timer.setInterval(3000)
        self.retract_timer.setSingleShot(True)
        self.retract_timer.timeout.connect(self.retract)
        self.resize(self.size().expandedTo(self.toolbox_layout.minimumSize()))

    def setupUi(self):
        super(ScreensharingToolbox, self).setupUi(self)

        # fix the SVG icons, as the generated code loads them as pixmaps, losing their ability to scale -Dan
        scale_icon = QIcon()
        scale_icon.addFile(Resources.get('icons/scale.svg'), mode=QIcon.Normal, state=QIcon.Off)
        viewonly_icon = QIcon()
        viewonly_icon.addFile(Resources.get('icons/viewonly.svg'), mode=QIcon.Normal, state=QIcon.Off)
        screenshot_icon = QIcon()
        screenshot_icon.addFile(Resources.get('icons/screenshot.svg'), mode=QIcon.Normal, state=QIcon.Off)
        fullscreen_icon = QIcon()
        fullscreen_icon.addFile(Resources.get('icons/fullscreen.svg'), mode=QIcon.Normal, state=QIcon.Off)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Normal, state=QIcon.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Active, state=QIcon.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Disabled, state=QIcon.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Selected, state=QIcon.On)
        minimize_icon = QIcon()
        minimize_icon.addFile(Resources.get('icons/minimize.svg'), mode=QIcon.Normal, state=QIcon.Off)
        minimize_icon.addFile(Resources.get('icons/minimize-active.svg'), mode=QIcon.Active, state=QIcon.Off)
        close_icon = QIcon()
        close_icon.addFile(Resources.get('icons/close.svg'), mode=QIcon.Normal, state=QIcon.Off)
        close_icon.addFile(Resources.get('icons/close-active.svg'), mode=QIcon.Active, state=QIcon.Off)

        self.scale_action.setIcon(scale_icon)
        self.viewonly_action.setIcon(viewonly_icon)
        self.screenshot_action.setIcon(screenshot_icon)
        self.fullscreen_action.setIcon(fullscreen_icon)
        self.minimize_action.setIcon(minimize_icon)
        self.close_action.setIcon(close_icon)

        self.scale_button.setIcon(scale_icon)
        self.viewonly_button.setIcon(viewonly_icon)
        self.screenshot_button.setIcon(screenshot_icon)
        self.fullscreen_button.setIcon(fullscreen_icon)
        self.minimize_button.setIcon(minimize_icon)
        self.close_button.setIcon(close_icon)

        self.scale_button.setDefaultAction(self.scale_action)
        self.viewonly_button.setDefaultAction(self.viewonly_action)
        self.screenshot_button.setDefaultAction(self.screenshot_action)
        self.fullscreen_button.setDefaultAction(self.fullscreen_action)
        self.minimize_button.setDefaultAction(self.minimize_action)
        self.close_button.setDefaultAction(self.close_action)

        self.color_depth_button.clear()
        self.color_depth_button.addItem('Default Color Depth', ServerDefault)
        self.color_depth_button.addItem('TrueColor (24 bits)', TrueColor)
        self.color_depth_button.addItem('HighColor (16 bits)', HighColor)
        self.color_depth_button.addItem('LowColor (8 bits)', LowColor)

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() == QEvent.Resize:
            new_x = (watched.width() - self.width()) / 2
            self.move(new_x, self.y())
            self.animation.setStartValue(QPoint(new_x, -self.height() + self.exposedPixels))
            self.animation.setEndValue(QPoint(new_x, 0))
        return False

    def enterEvent(self, event):
        super(ScreensharingToolbox, self).enterEvent(event)
        self.retract_timer.stop()
        self.expose()

    def leaveEvent(self, event):
        super(ScreensharingToolbox, self).leaveEvent(event)
        self.retract_timer.start()

    def paintEvent(self, event): # make the widget style aware
        option = QStyleOption()
        option.init(self)
        painter = QStylePainter(self)
        painter.drawPrimitive(QStyle.PE_Widget, option)

    def expose(self):
        if self.animation.state() == QPropertyAnimation.Running and self.animation.direction() == QPropertyAnimation.Forward:
            return
        elif self.animation.state() == QPropertyAnimation.Stopped and self.pos() == self.animation.endValue():
            return
        self.animation.setDirection(QPropertyAnimation.Forward)
        self.animation.start()

    def retract(self):
        if self.animation.state() == QPropertyAnimation.Running and self.animation.direction() == QPropertyAnimation.Backward:
            return
        elif self.animation.state() == QPropertyAnimation.Stopped and self.pos() == self.animation.startValue():
            return
        self.animation.setDirection(QPropertyAnimation.Backward)
        self.animation.start()

del ui_class, base_class


ui_class, base_class = uic.loadUiType(Resources.get('screensharing_window.ui'))

class ScreensharingWindow(base_class, ui_class):
    def __init__(self, vncclient, parent=None):
        super(ScreensharingWindow, self).__init__(parent)
        self.vncclient = vncclient
        self.clipboard = QApplication.clipboard()
        with Resources.directory:
            self.setupUi()
        self.scale_action.triggered.connect(self._SH_ScaleActionTriggered)
        self.viewonly_action.triggered.connect(self._SH_ViewOnlyActionTriggered)
        self.screenshot_action.triggered.connect(self._SH_ScreenshotActionTriggered)
        self.fullscreen_action.triggered.connect(self._SH_FullscreenActionTriggered)
        self.minimize_action.triggered.connect(self._SH_MinimizeActionTriggered)
        self.close_action.triggered.connect(self._SH_CloseActionTriggered)
        self.screenshots_folder_action.triggered.connect(self._SH_ScreenshotsFolderActionTriggered)
        self.screenshot_button.customContextMenuRequested.connect(self._SH_ScreenshotButtonContextMenuRequested)
        self.color_depth_button.currentIndexChanged[int].connect(self._SH_ColorDepthButtonCurrentIndexChanged)
        self.fullscreen_toolbox.color_depth_button.currentIndexChanged[int].connect(self._SH_ColorDepthButtonCurrentIndexChanged)
        self.vncclient.textCut.connect(self._SH_VNCClientTextCut)
        self.clipboard.changed.connect(self._SH_ClipboardChanged)

    def setupUi(self):
        super(ScreensharingWindow, self).setupUi(self)
        self.scroll_area.viewport().setObjectName('vnc_viewport')
        self.scroll_area.viewport().setStyleSheet('QWidget#vnc_viewport { background-color: #101010; }') # #101010, #004488, #004480, #0044aa, #0055ff, #0066cc, #3366cc, #0066d5

        self.vncviewer = VNCViewer(self.vncclient)
        self.vncviewer.setGeometry(self.scroll_area.viewport().geometry())
        self.scroll_area.setWidget(self.vncviewer)
        self.vncviewer.setFocus(Qt.OtherFocusReason)

        self.fullscreen_toolbox = ScreensharingToolbox(self)
        self.fullscreen_toolbox.hide()

        self.scale_action = self.fullscreen_toolbox.scale_action
        self.viewonly_action = self.fullscreen_toolbox.viewonly_action
        self.screenshot_action = self.fullscreen_toolbox.screenshot_action
        self.fullscreen_action = self.fullscreen_toolbox.fullscreen_action
        self.minimize_action = self.fullscreen_toolbox.minimize_action
        self.close_action = self.fullscreen_toolbox.close_action

        self.scale_button.setIcon(self.scale_action.icon())
        self.viewonly_button.setIcon(self.viewonly_action.icon())
        self.screenshot_button.setIcon(self.screenshot_action.icon())
        self.fullscreen_button.setIcon(self.fullscreen_action.icon())

        self.scale_button.setDefaultAction(self.scale_action)
        self.viewonly_button.setDefaultAction(self.viewonly_action)
        self.screenshot_button.setDefaultAction(self.screenshot_action)
        self.fullscreen_button.setDefaultAction(self.fullscreen_action)

        self.color_depth_button.clear()
        self.color_depth_button.addItem('Default Color Depth', ServerDefault)
        self.color_depth_button.addItem('TrueColor (24 bits)', TrueColor)
        self.color_depth_button.addItem('HighColor (16 bits)', HighColor)
        self.color_depth_button.addItem('LowColor (8 bits)', LowColor)

        self.screenshot_button_menu = QMenu(self)
        self.screenshots_folder_action = self.screenshot_button_menu.addAction('Open screenshots folder')

    def closeEvent(self, event):
        super(ScreensharingWindow, self).closeEvent(event)
        self.vncclient.stop()

    def _SH_ColorDepthButtonCurrentIndexChanged(self, index):
        # synchronize the 2 color depth buttons
        self.color_depth_button.blockSignals(True)
        self.fullscreen_toolbox.color_depth_button.blockSignals(True)
        self.color_depth_button.setCurrentIndex(index)
        self.fullscreen_toolbox.color_depth_button.setCurrentIndex(index)
        self.color_depth_button.blockSignals(False)
        self.fullscreen_toolbox.color_depth_button.blockSignals(False)
        self.vncclient.settings = self.color_depth_button.itemData(index, Qt.UserRole)

    def _SH_ScaleActionTriggered(self):
        if self.scale_action.isChecked():
            self.vncviewer.scale = True
            self.scroll_area.setWidgetResizable(True)
        else:
            self.scroll_area.setWidgetResizable(False)
            self.vncviewer.scale = False

    def _SH_ViewOnlyActionTriggered(self):
        self.vncviewer.view_only = self.viewonly_action.isChecked()

    def _SH_ScreenshotActionTriggered(self):
        screenshot = Screenshot(self.vncclient)
        screenshot.capture()
        screenshot.save()

    def _SH_FullscreenActionTriggered(self):
        if self.fullscreen_action.isChecked():
            self.scroll_area.setFrameShape(QFrame.NoFrame)
            self.toolbar.hide()
            self.showFullScreen()
            self.fullscreen_toolbox.animation.stop()
            self.fullscreen_toolbox.move(self.fullscreen_toolbox.x(), 0)
            self.fullscreen_toolbox.show()
            self.fullscreen_toolbox.retract_timer.start()
        else:
            self.fullscreen_toolbox.retract_timer.stop()
            self.fullscreen_toolbox.animation.stop()
            self.fullscreen_toolbox.hide()
            self.scroll_area.setFrameShape(QFrame.StyledPanel)
            self.toolbar.show()
            self.showNormal()

    def _SH_MinimizeActionTriggered(self):
        self.showMinimized()

    def _SH_CloseActionTriggered(self):
        self.fullscreen_action.trigger()
        self.close()

    def _SH_ScreenshotButtonContextMenuRequested(self, pos):
        if self.fullscreen_action.isChecked():
            self.screenshot_button_menu.exec_(self.fullscreen_toolbox.screenshot_button.mapToGlobal(pos))
        else:
            self.screenshot_button_menu.exec_(self.screenshot_button.mapToGlobal(pos))

    def _SH_ScreenshotsFolderActionTriggered(self, pos):
        settings = BlinkSettings()
        QDesktopServices.openUrl(QUrl.fromLocalFile(settings.screen_sharing.screenshots_directory.normalized))

    def _SH_VNCClientTextCut(self, text):
        self.clipboard.blockSignals(True)
        self.clipboard.setText(text)
        self.clipboard.blockSignals(False)

    def _SH_ClipboardChanged(self, mode):
        data = self.clipboard.mimeData(mode)
        self.vncclient.cut_text_event(data.html() or data.text())

del ui_class, base_class


class Screenshot(object):
    def __init__(self, vncclient):
        self.vncclient = vncclient
        self.image = None

    @classmethod
    def filename_generator(cls):
        settings = BlinkSettings()
        name = os.path.join(settings.screen_sharing.screenshots_directory.normalized, 'ScreenSharing-{:%Y%m%d-%H.%M.%S}'.format(datetime.now()))
        yield '%s.png' % name
        for x in count(1):
            yield "%s-%d.png" % (name, x)

    def capture(self):
        try:
            self.image = self.vncclient.image.copy()
        except AttributeError:
            pass
        else:
            player = WavePlayer(SIPApplication.alert_audio_bridge.mixer, Resources.get('sounds/screenshot.wav'), volume=30)
            SIPApplication.alert_audio_bridge.add(player)
            player.start()

    @run_in_thread('file-io')
    def save(self):
        if self.image is not None:
            filename = next(filename for filename in self.filename_generator() if not os.path.exists(filename))
            makedirs(os.path.dirname(filename))
            self.image.save(filename)


