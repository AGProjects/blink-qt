
import weakref

from PyQt6.QtCore import Qt, QObject, pyqtSignal, pyqtSlot
try:
    from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage, QDBusVariant
except ImportError:     # PyQt6 built without QtDBus: no portal, Qt's own scheme only
    QDBusConnection = QDBusInterface = QDBusVariant = None
    QDBusMessage = object
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from application.python import limit
from application.python.decorator import decorator, preserve_signature
from math import fmod, isnan


__all__ = ['ColorScheme', 'ColorUtils', 'ColorHelperMixin', 'is_dark_theme', 'secondary_text_color', 'follow_theme', 'apply_theme_change', 'dark_palette', 'light_palette', 'color_scheme', 'PortalColorScheme']


# Objects that draw with colours or images chosen for the theme at the time they were made.
# Each has apply_theme(), called when the application palette changes; theme_order (default
# 10) orders them, so caches (icons) are dropped before the widgets that read them redraw.
_theme_followers = weakref.WeakSet()


class PortalColorScheme(QObject):
    """The desktop's light/dark preference, from the XDG desktop portal (org.freedesktop.appearance
    color-scheme), for platform themes that do not tell Qt (gtk2 on GNOME does not). value is
    'dark', 'light' or None; changed is emitted with the new value."""

    changed = pyqtSignal(object)

    service = 'org.freedesktop.portal.Desktop'
    path = '/org/freedesktop/portal/desktop'
    interface = 'org.freedesktop.portal.Settings'
    namespace = 'org.freedesktop.appearance'
    key = 'color-scheme'

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super(PortalColorScheme, self).__init__()
        self.value = None
        self.available = False
        if QDBusConnection is None:
            return
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return
        settings = QDBusInterface(self.service, self.path, self.interface, bus)
        for method in ('ReadOne', 'Read'):
            reply = settings.call(method, self.namespace, self.key)
            arguments = reply.arguments()
            if arguments:
                self.value = self._scheme(arguments[0])
                self.available = True
                break
        bus.connect(self.service, self.path, self.interface, 'SettingChanged', self._setting_changed)

    @staticmethod
    def _scheme(value):
        while isinstance(value, QDBusVariant):     # Read wraps it twice
            value = value.variant()
        try:
            # 0 is "no preference": GNOME's Default (light) style, what applications draw when not asked for dark
            return {0: 'light', 1: 'dark', 2: 'light'}.get(int(value))
        except (TypeError, ValueError):
            return None

    @pyqtSlot(QDBusMessage)
    def _setting_changed(self, message):
        arguments = message.arguments()
        if len(arguments) < 3 or arguments[0] != self.namespace or arguments[1] != self.key:
            return
        self.available = True
        value = self._scheme(arguments[2])
        if value != self.value:
            self.value = value
            self.changed.emit(value)


def color_scheme():
    """'dark', 'light' or None: what the system says it is using, as Qt knows it (6.5 and
    later) or else as the desktop portal says."""
    try:
        scheme = {Qt.ColorScheme.Dark: 'dark', Qt.ColorScheme.Light: 'light'}.get(QApplication.styleHints().colorScheme())
    except AttributeError:
        scheme = None
    return scheme or PortalColorScheme.instance().value


def dark_palette():
    """A dark palette for when the system says dark and the platform gives us a light one."""
    palette = QPalette()
    colors = {QPalette.ColorRole.Window: '#353535', QPalette.ColorRole.WindowText: '#e8e8e8',
              QPalette.ColorRole.Base: '#2a2a2a', QPalette.ColorRole.AlternateBase: '#323232',
              QPalette.ColorRole.ToolTipBase: '#454545', QPalette.ColorRole.ToolTipText: '#e8e8e8',
              QPalette.ColorRole.PlaceholderText: '#8c8c8c', QPalette.ColorRole.Text: '#e8e8e8',
              QPalette.ColorRole.Button: '#3c3c3c', QPalette.ColorRole.ButtonText: '#e8e8e8',
              QPalette.ColorRole.BrightText: '#ff5555', QPalette.ColorRole.Light: '#505050',
              QPalette.ColorRole.Midlight: '#454545', QPalette.ColorRole.Mid: '#2d2d2d',
              QPalette.ColorRole.Dark: '#1e1e1e', QPalette.ColorRole.Shadow: '#262626',
              QPalette.ColorRole.Highlight: '#2a82da', QPalette.ColorRole.HighlightedText: '#ffffff',
              QPalette.ColorRole.Link: '#5aa0e6', QPalette.ColorRole.LinkVisited: '#a07ad0'}
    if hasattr(QPalette.ColorRole, 'Accent'):
        colors[QPalette.ColorRole.Accent] = '#2a82da'
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#7f7f7f'))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor('#505050'))
    return palette


# Fusion's light palette as Qt builds it without a platform theme (QStyleFactory.create('Fusion')
# .standardPalette() offscreen): (active and inactive, disabled) per role.
_FUSION_LIGHT = {'WindowText': ('#000000', '#bebebe'), 'Button': ('#efefef', '#efefef'), 'Light': ('#ffffff', '#ffffff'),
                 'Midlight': ('#cacaca', '#cacaca'), 'Dark': ('#9f9f9f', '#bebebe'), 'Mid': ('#b8b8b8', '#b8b8b8'),
                 'Text': ('#000000', '#bebebe'), 'BrightText': ('#ffffff', '#ffffff'), 'ButtonText': ('#000000', '#bebebe'),
                 'Base': ('#ffffff', '#efefef'), 'Window': ('#efefef', '#efefef'), 'Shadow': ('#767676', '#b1b1b1'),
                 'Highlight': ('#308cc6', '#919191'), 'HighlightedText': ('#ffffff', '#ffffff'), 'Link': ('#0000ff', '#0000ff'),
                 'LinkVisited': ('#ff00ff', '#ff00ff'), 'AlternateBase': ('#f7f7f7', '#f7f7f7'), 'ToolTipBase': ('#ffffdc', '#ffffdc'),
                 'ToolTipText': ('#000000', '#000000'), 'PlaceholderText': ('#000000', '#000000'), 'Accent': ('#308cc6', '#919191')}


def light_palette():
    """Fusion's light palette, spelled out: the style's standardPalette() is the platform theme's,
    which under gtk2 stays the dark one it read at start."""
    palette = QPalette()
    for name, (normal, disabled) in _FUSION_LIGHT.items():
        role = getattr(QPalette.ColorRole, name, None)
        if role is None:
            continue            # Accent is Qt 6.6 and later
        palette.setColor(QPalette.ColorGroup.Active, role, QColor(normal))
        palette.setColor(QPalette.ColorGroup.Inactive, role, QColor(normal))
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(disabled))
    return palette


def follow_theme(obj):
    _theme_followers.add(obj)


def apply_theme_change():
    for obj in sorted(list(_theme_followers), key=lambda obj: getattr(obj, 'theme_order', 10)):
        try:
            obj.apply_theme()
        except Exception as e:
            from blink.logging import ActivityLog
            ActivityLog().warning(f'[ui] Cannot apply the new theme to {obj.__class__.__name__}: {e!r}')
    # A widget with its own style sheet keeps the palette it was polished with (palette(dark)
    # in a border, the background behind it): setting the sheet again polishes it anew.
    for widget in QApplication.allWidgets():
        if widget.styleSheet():
            widget.setStyleSheet(widget.styleSheet())
    for widget in QApplication.topLevelWidgets():
        widget.update()


def is_dark_theme():
    """Whether the application palette is a dark one (light text on a dark window)."""
    return QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def secondary_text_color(palette, color_group=QPalette.ColorGroup.Normal):
    """The colour of secondary text (the address or last message under a name): a grey between
    the text and the window colours, nearer the text. The palette's Dark role is too faint in a
    light theme and near black in a dark one."""
    weight = 0.7 if is_dark_theme() else 0.62
    text, window = palette.color(color_group, QPalette.ColorRole.WindowText), palette.color(color_group, QPalette.ColorRole.Window)
    return QColor(*(round(t * weight + w * (1 - weight)) for t, w in ((text.red(), window.red()), (text.green(), window.green()), (text.blue(), window.blue()))))


class HCYColor(object):
    """Hue/chroma/luma color space"""

    luma_r = 0.2126
    luma_g = 0.7152
    luma_b = 0.0722

    def __init__(self, color):
        r = self._gamma(color.redF())
        g = self._gamma(color.greenF())
        b = self._gamma(color.blueF())

        p = max(r, g, b)
        n = min(r, g, b)
        d = 6.0 * (p - n)

        y = r * self.luma_r + g * self.luma_g + b * self.luma_b

        if n == p:
            self.h = 0.0
        elif r == p:
            self.h = ((g - b) / d)
        elif g == p:
            self.h = ((b - r) / d) + (1.0 / 3.0)
        else:
            self.h = ((r - g) / d) + (2.0 / 3.0)

        if r == g == b:
            self.c = 0.0
        else:
            self.c = max((y - n)/y, (p - y)/(1 - y))

        self.y = y
        self.a = color.alphaF()

    @staticmethod
    def _gamma(value):
        return limit(value, 0.0, 1.0) ** 2.2

    @staticmethod
    def _igamma(value):
        return limit(value, 0.0, 1.0) ** (1.0 / 2.2)

    @classmethod
    def luma(cls, color):
        return cls._gamma(color.redF()) * cls.luma_r + cls._gamma(color.greenF()) * cls.luma_g + cls._gamma(color.blueF()) * cls.luma_b

    def qColor(self):
        r = fmod(self.h, 1.0)
        h = r+1.0 if r < 0.0 else r
        c = limit(self.c, min=0.0, max=1.0)
        y = limit(self.y, min=0.0, max=1.0)

        hs = h * 6.0
        if hs < 1.0:
            th = hs
            tm = self.luma_r + self.luma_g * th
        elif hs < 2.0:
            th = 2.0 - hs
            tm = self.luma_g + self.luma_r * th
        elif hs < 3.0:
            th = hs - 2.0
            tm = self.luma_g + self.luma_b * th
        elif hs < 4.0:
            th = 4.0 - hs
            tm = self.luma_b + self.luma_g * th
        elif hs < 5.0:
            th = hs - 4.0
            tm = self.luma_b + self.luma_r * th
        else:
            th = 6.0 - hs
            tm = self.luma_r + self.luma_b * th

        # calculate RGB channels in the sorted order
        if tm >= y:
            tp = y + y * c * (1.0 - tm) / tm
            to = y + y * c * (th - tm) / tm
            tn = y - (y * c)
        else:
            tp = y + (1.0 - y) * c
            to = y + (1.0 - y) * c * (th - tm) / (1.0 - tm)
            tn = y - (1.0 - y) * c * tm / (1.0 - tm)

        # return RGB channels in the appropriate order
        if hs < 1.0:
            return QColor.fromRgbF(self._igamma(tp), self._igamma(to), self._igamma(tn), self.a)
        elif hs < 2.0:
            return QColor.fromRgbF(self._igamma(to), self._igamma(tp), self._igamma(tn), self.a)
        elif hs < 3.0:
            return QColor.fromRgbF(self._igamma(tn), self._igamma(tp), self._igamma(to), self.a)
        elif hs < 4.0:
            return QColor.fromRgbF(self._igamma(tn), self._igamma(to), self._igamma(tp), self.a)
        elif hs < 5.0:
            return QColor.fromRgbF(self._igamma(to), self._igamma(tn), self._igamma(tp), self.a)
        else:
            return QColor.fromRgbF(self._igamma(tp), self._igamma(tn), self._igamma(to), self.a)


class ColorScheme(object):
    ShadowShade   = 0
    DarkShade     = 1
    MidShade      = 2
    MidlightShade = 3
    LightShade    = 4

    @staticmethod
    def shade(color, role, contrast, chroma_adjust=0.0):
        contrast = limit(contrast, min=-1.0, max=1.0)
        y = ColorUtils.luma(color)
        yi = 1.0 - y

        # handle very dark colors (base, mid, dark, shadow == midlight, light)
        if y < 0.006:
            if role == ColorScheme.LightShade:
                return ColorUtils.shade(color, 0.05 + 0.95 * contrast, chroma_adjust)
            elif role == ColorScheme.MidShade:
                return ColorUtils.shade(color, 0.01 + 0.20 * contrast, chroma_adjust)
            elif role == ColorScheme.DarkShade:
                return ColorUtils.shade(color, 0.02 + 0.40 * contrast, chroma_adjust)
            else:
                return ColorUtils.shade(color, 0.03 + 0.60 * contrast, chroma_adjust)

        # handle very light colors (base, midlight, light == mid, dark, shadow)
        if y > 0.93:
            if role == ColorScheme.MidlightShade:
                return ColorUtils.shade(color, -0.02 - 0.20 * contrast, chroma_adjust)
            elif role == ColorScheme.DarkShade:
                return ColorUtils.shade(color, -0.06 - 0.60 * contrast, chroma_adjust)
            elif role == ColorScheme.ShadowShade:
                return ColorUtils.shade(color, -0.10 - 0.90 * contrast, chroma_adjust)
            else:
                return ColorUtils.shade(color, -0.04 - 0.40 * contrast, chroma_adjust)

        # handle everything else
        light_amount = (0.05 + y * 0.55) * (0.25 + contrast * 0.75)
        dark_amount  = (     - y       ) * (0.55 + contrast * 0.35)
        if role == ColorScheme.LightShade:
            return ColorUtils.shade(color, light_amount, chroma_adjust)
        elif role == ColorScheme.MidlightShade:
            return ColorUtils.shade(color, (0.15 + 0.35 * yi) * light_amount, chroma_adjust)
        elif role == ColorScheme.MidShade:
            return ColorUtils.shade(color, (0.35 + 0.15 * y) * dark_amount, chroma_adjust)
        elif role == ColorScheme.DarkShade:
            return ColorUtils.shade(color, dark_amount, chroma_adjust)
        else:
            return ColorUtils.darken(ColorUtils.shade(color, dark_amount, chroma_adjust), 0.5 + 0.3 * y)


class ColorUtils(object):
    @staticmethod
    def luma(color):
        return HCYColor.luma(color)

    @staticmethod
    def lighten(color, amount=0.5, chroma_inverse_gain=1.0):
        color = HCYColor(color)
        color.y = 1.0 - limit((1.0 - color.y) * (1.0 - amount),      min=0.0, max=1.0)
        color.c = 1.0 - limit((1.0 - color.c) * chroma_inverse_gain, min=0.0, max=1.0)
        return color.qColor()

    @staticmethod
    def darken(color, amount=0.5, chroma_gain=1.0):
        color = HCYColor(color)
        color.y = limit(color.y * (1.0 - amount), min=0.0, max=1.0)
        color.c = limit(color.c * chroma_gain,    min=0.0, max=1.0)
        return color.qColor()

    @staticmethod
    def shade(color, luma_amount, chroma_amount=0.0):
        color = HCYColor(color)
        color.y = limit(color.y + luma_amount,   min=0.0, max=1.0)
        color.c = limit(color.c + chroma_amount, min=0.0, max=1.0)
        return color.qColor()

    @staticmethod
    def mix(color1, color2, bias=0.5):
        def mix_real(a, b, bias):
            return a + (b - a) * bias
        if bias <= 0.0:
            return color1
        if bias >= 1.0:
            return color2
        if isnan(bias):
            return color1
        r = mix_real(color1.redF(),   color2.redF(),   bias)
        g = mix_real(color1.greenF(), color2.greenF(), bias)
        b = mix_real(color1.blueF(),  color2.blueF(),  bias)
        a = mix_real(color1.alphaF(), color2.alphaF(), bias)
        return QColor.fromRgbF(r, g, b, a)


def color_key(instance, color):
    return color.rgba()


def color_ratio_key(instance, color, ratio):
    return color.rgba() << 32 | int(ratio*512)


def background_color_key(instance, background, color):
    return background.rgba() << 32 | color.rgba()


@decorator
def cache_result(key_func):
    def cache_results(function):
        @preserve_signature(function)
        def wrapper(*args, **kw):
            key = key_func(*args, **kw)
            try:
                return wrapper.__cache__[key]
            except KeyError:
                return wrapper.__cache__.setdefault(key, function(*args, **kw))
        wrapper.__cache__ = {}
        return wrapper
    return cache_results


class ColorHelperMixin(object):
    _contrast = 0.3
    _bgcontrast = min(1.0, 0.9*_contrast/0.7)

    @cache_result(color_key)
    def low_threshold(self, color):
        darker = ColorScheme.shade(color, ColorScheme.MidShade, 0.5)
        return ColorUtils.luma(darker) > ColorUtils.luma(color)

    @cache_result(color_key)
    def high_threshold(self, color):
        lighter = ColorScheme.shade(color, ColorScheme.LightShade, 0.5)
        return ColorUtils.luma(lighter) < ColorUtils.luma(color)

    @cache_result(color_key)
    def background_top_color(self, color):
        if self.low_threshold(color):
            return ColorScheme.shade(color, ColorScheme.MidlightShade, 0.0)
        else:
            other_luma = ColorUtils.luma(ColorScheme.shade(color, ColorScheme.LightShade, 0.0))
            color_luma = ColorUtils.luma(color)
            return ColorUtils.shade(color, (other_luma - color_luma) * self._bgcontrast)

    @cache_result(color_key)
    def background_bottom_color(self, color):
        if self.low_threshold(color):
            return ColorScheme.shade(color, ColorScheme.MidShade, 0.0)
        else:
            other_luma = ColorUtils.luma(ColorScheme.shade(color, ColorScheme.MidShade, 0.0))
            color_luma = ColorUtils.luma(color)
            return ColorUtils.shade(color, (other_luma - color_luma) * self._bgcontrast)

    @cache_result(color_key)
    def calc_light_color(self, color):
        if self.high_threshold(color):
            return color
        else:
            return ColorScheme.shade(color, ColorScheme.LightShade, self._contrast)

    @cache_result(color_key)
    def calc_dark_color(self, color):
        if self.low_threshold(color):
            return ColorUtils.mix(self.calc_light_color(color), color, 0.3 + 0.7 * self._contrast)
        else:
            return ColorScheme.shade(color, ColorScheme.MidShade, self._contrast)

    @cache_result(color_key)
    def calc_shadow_color(self, color):
        if self.low_threshold(color):
            shadow_color = ColorUtils.mix(Qt.GlobalColor.black, color, color.alphaF())
        else:
            shadow_color = ColorScheme.shade(ColorUtils.mix(Qt.GlobalColor.black, color, color.alphaF()), ColorScheme.ShadowShade, self._contrast)
        shadow_color.setAlpha(color.alpha())  # make sure shadow color has the same alpha channel as the input
        return shadow_color

    @cache_result(color_ratio_key)
    def background_color(self, color, ratio):
        if ratio < 0.5:
            return ColorUtils.mix(self.background_top_color(color), color, 2.0*ratio)
        else:
            return ColorUtils.mix(color, self.background_bottom_color(color), 2.0*ratio-1)

    @cache_result(background_color_key)
    def deco_color(self, background, color):
        return ColorUtils.mix(background, color, 0.4 + 0.8*self._contrast)

    def color_with_alpha(self, color, alpha):
        color = QColor(color)
        color.setAlpha(int(alpha))
        return color

    def alpha_color(self, color, alpha):
        if 0.0 <= alpha < 1.0:
            color.setAlphaF(alpha * color.alphaF())
        return color


