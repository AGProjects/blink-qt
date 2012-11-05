# Copyright (c) 2012 AG Projects. See LICENSE for details.
#

__all__ = ['ColorScheme', 'ColorUtils']

from PyQt4.QtGui import QColor
from application.python import limit
from math import fmod, isnan


class HCYColor(object):
    """Hue/chroma/luma colorspace"""

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


