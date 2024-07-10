import platform

from PyQt5.QtCore import Qt, QCoreApplication, QLineF, QPointF, QRectF, QSize, QTimer, pyqtSignal, QT_TRANSLATE_NOOP
from PyQt5.QtGui import QBrush, QColor, QLinearGradient, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PyQt5.QtWidgets import QAction, QCommonStyle, QMenu, QPushButton, QStyle, QStyleOptionToolButton, QStylePainter, QToolButton

from blink.resources import Resources
from blink.widgets.color import ColorScheme, ColorUtils, ColorHelperMixin


__all__ = ['ToolButton', 'ConferenceButton', 'StreamButton', 'SegmentButton', 'SingleSegment', 'LeftSegment', 'MiddleSegment', 'RightSegment',
           'RecordButton', 'SwitchViewButton', 'StateButton', 'AccountState']

translate = QCoreApplication.translate


class ToolButton(QToolButton):
    """A custom QToolButton that doesn't show a menu indicator arrow"""

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.features &= ~QStyleOptionToolButton.ToolButtonFeature.HasMenu
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)


class ConferenceButton(ToolButton):
    makeConference  = pyqtSignal()
    breakConference = pyqtSignal()

    def __init__(self, parent=None):
        super(ConferenceButton, self).__init__(parent)
        self.make_conference_action = QAction(translate('conference_button', 'Conference all single sessions'), self, triggered=self.makeConference.emit)
        self.break_conference_action = QAction(translate('conference_button', 'Break selected conference'), self, triggered=self.breakConference.emit)
        self.toggled.connect(self._SH_Toggled)
        self.addAction(self.make_conference_action)

    def _SH_Toggled(self, checked):
        if checked:
            self.removeAction(self.make_conference_action)
            self.addAction(self.break_conference_action)
        else:
            self.removeAction(self.break_conference_action)
            self.addAction(self.make_conference_action)


class StreamButton(QToolButton):
    hidden = pyqtSignal()
    shown  = pyqtSignal()

    def __init__(self, parent=None):
        super(StreamButton, self).__init__(parent)
        self.default_icon = QIcon()
        self.alternate_icon = QIcon()
        self.clicked.connect(self._clicked)

    def _clicked(self):
        super(StreamButton, self).setIcon(self.default_icon)

    def _get_accepted(self):
        return not self.isChecked()

    def _set_accepted(self, accepted):
        super(StreamButton, self).setIcon(self.alternate_icon)
        self.setChecked(not accepted)

    accepted = property(_get_accepted, _set_accepted)
    del _get_accepted, _set_accepted

    def _get_active(self):
        return self.isEnabled()

    def _set_active(self, active):
        self.setEnabled(bool(active))

    active = property(_get_active, _set_active)
    del _get_active, _set_active

    @property
    def in_use(self):
        return self.isVisibleTo(self.parent())

    def setVisible(self, visible):
        super(StreamButton, self).setVisible(visible)
        signal = self.shown if visible else self.hidden
        signal.emit()

    def setIcon(self, icon):
        self.default_icon = icon
        self.alternate_icon = QIcon(icon)
        normal_sizes = icon.availableSizes(QIcon.Mode.Normal, QIcon.State.On)
        selected_sizes = icon.availableSizes(QIcon.Mode.Selected, QIcon.State.On)
        selected_additional_sizes = [size for size in selected_sizes if size not in normal_sizes]
        for size in normal_sizes + selected_additional_sizes:
            pixmap = icon.pixmap(size, QIcon.Mode.Selected, QIcon.State.On)
            self.alternate_icon.addPixmap(pixmap, QIcon.Mode.Normal, QIcon.State.On)
        disabled_sizes = icon.availableSizes(QIcon.Mode.Disabled, QIcon.State.On)
        selected_additional_sizes = [size for size in selected_sizes if size not in disabled_sizes]
        for size in disabled_sizes + selected_additional_sizes:
            pixmap = icon.pixmap(size, QIcon.Mode.Selected, QIcon.State.On)
            self.alternate_icon.addPixmap(pixmap, QIcon.Mode.Disabled, QIcon.State.On)
        super(StreamButton, self).setIcon(icon)


class SegmentTypeMeta(type):
    def __repr__(cls):
        return cls.__name__


class SegmentType(object, metaclass=SegmentTypeMeta):
    style_sheet = ''


class SingleSegment(SegmentType):
    style_sheet = """
                     QToolButton {
                         background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #bababa);
                         border-color: #545454;
                         border-radius: 4px;
                         border-width: 1px;
                         border-style: solid;
                     }
                     
                     QToolButton:pressed {
                         background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5, stop:0 #dddddd, stop:1 #777777);
                     }
                  """


class LeftSegment(SegmentType):
    style_sheet = """
                     QToolButton {
                         background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #bababa);
                         border-color: #545454;
                         border-top-left-radius: 4px;
                         border-bottom-left-radius: 4px;
                         border-width: 1px;
                         border-style: solid;
                     }
                     
                     QToolButton:pressed {
                         background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5, stop:0 #dddddd, stop:1 #777777);
                     }
                  """


class MiddleSegment(SegmentType):
    style_sheet = """
                     QToolButton {
                         background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #bababa);
                         border-color: #545454;
                         border-width: 1px;
                         border-left-width: 0px;
                         border-style: solid;
                     }
                     
                     QToolButton:pressed {
                         background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5, stop:0 #dddddd, stop:1 #777777);
                     }
                  """


class RightSegment(SegmentType):
    style_sheet = """
                     QToolButton {
                         background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #bababa);
                         border-color: #545454;
                         border-top-right-radius: 4px;
                         border-bottom-right-radius: 4px;
                         border-width: 1px;
                         border-left-width: 0px;
                         border-style: solid;
                     }
                     
                     QToolButton:pressed {
                         background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5, stop:0 #dddddd, stop:1 #777777);
                     }
                  """


class SegmentButton(QToolButton):
    SingleSegment = SingleSegment
    LeftSegment   = LeftSegment
    MiddleSegment = MiddleSegment
    RightSegment  = RightSegment

    hidden = pyqtSignal()
    shown  = pyqtSignal()

    def __init__(self, parent=None):
        super(SegmentButton, self).__init__(parent)
        self.type = SingleSegment

    def _get_type(self):
        return self.__dict__['type']

    def _set_type(self, value):
        if not issubclass(value, SegmentType):
            raise ValueError("Invalid type: %r" % value)
        self.__dict__['type'] = value
        self.setStyleSheet(value.style_sheet)

    type = property(_get_type, _set_type)
    del _get_type, _set_type

    def setVisible(self, visible):
        super(SegmentButton, self).setVisible(visible)
        signal = self.shown if visible else self.hidden
        signal.emit()


class RecordButton(SegmentButton):
    def __init__(self, parent=None):
        super(RecordButton, self).__init__(parent)
        self.timer_id = None
        self.toggled.connect(self._SH_Toggled)
        self.animation_icons = []
        self.animation_icon_index = 0

    def _get_animation_icon_index(self):
        return self.__dict__['animation_icon_index']

    def _set_animation_icon_index(self, index):
        self.__dict__['animation_icon_index'] = index
        self.update()

    animation_icon_index = property(_get_animation_icon_index, _set_animation_icon_index)
    del _get_animation_icon_index, _set_animation_icon_index

    def setIcon(self, icon):
        super(RecordButton, self).setIcon(icon)
        on_icon = QIcon(icon)
        off_icon = QIcon(icon)
        for size in off_icon.availableSizes(QIcon.Mode.Normal, QIcon.State.On):
            pixmap = off_icon.pixmap(size, QIcon.Mode.Normal, QIcon.State.Off)
            off_icon.addPixmap(pixmap, QIcon.Mode.Normal, QIcon.State.On)
        self.animation_icons = [on_icon, off_icon]

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.icon = self.animation_icons[self.animation_icon_index]
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)

    def timerEvent(self, event):
        self.animation_icon_index = (self.animation_icon_index+1) % len(self.animation_icons)

    def _SH_Toggled(self, checked):
        if checked:
            self.timer_id = self.startTimer(1000)
            self.animation_icon_index = 0
        else:
            self.killTimer(self.timer_id)
            self.timer_id = None


class SwitchViewButton(QPushButton):
    ContactView = 0
    SessionView = 1

    viewChanged = pyqtSignal(int)

    button_text = {ContactView: QT_TRANSLATE_NOOP('switch_view_button', 'Switch to Calls'), SessionView: QT_TRANSLATE_NOOP('switch_view_button', 'Switch to Contacts')}
    button_dnd_text = {ContactView: QT_TRANSLATE_NOOP('switch_view_button', 'Drag here to add to a conference'), SessionView: QT_TRANSLATE_NOOP('switch_view_button', 'Drag here to go back to contacts')}

    dnd_style_sheet1 = """
                          QPushButton {
                              background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #d3ffdc);
                              border-color: #237523;
                              border-radius: 4px;
                              border-width: 2px;
                              border-style: solid;
                          }
                       """

    dnd_style_sheet2 = """
                          QPushButton {
                              background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #c2ffce);
                              border-color: #dc3169;
                              border-radius: 4px;
                              border-width: 2px;
                              border-style: solid;
                          }
                       """

    def __init__(self, parent=None):
        super(SwitchViewButton, self).__init__(parent)
        self.setAcceptDrops(True)
        self.__dict__['dnd_active'] = False
        self.view = self.ContactView
        self.original_height = 20  # used to restore button size after DND
        self.dnd_timer = QTimer(self)
        self.dnd_timer.setInterval(100)
        self.dnd_timer.timeout.connect(self._update_dnd)
        self.dnd_timer.phase = 0
        self.clicked.connect(self._change_view)

    def _get_view(self):
        return self.__dict__['view']

    def _set_view(self, value):
        if self.__dict__.get('view', None) == value:
            return
        if value not in (self.ContactView, self.SessionView):
            raise ValueError("invalid view value: %r" % value)
        self.__dict__['view'] = value
        if self.dnd_active:
            text = self.button_dnd_text[value]
        else:
            text = self.button_text[value]
        self.setText(translate('switch_view_button', text))
        self.viewChanged.emit(value)

    view = property(_get_view, _set_view)
    del _get_view, _set_view

    def _get_dnd_active(self):
        return self.__dict__['dnd_active']

    def _set_dnd_active(self, value):
        if self.__dict__.get('dnd_active', None) == value:
            return
        self.__dict__['dnd_active'] = value
        if value is True:
            self.dnd_timer.phase = 0
            self.original_height = self.height()
            self.setStyleSheet(self.dnd_style_sheet1)
            self.setText(translate('switch_view_button', self.button_dnd_text[self.view]))
            self.setFixedHeight(40)
        else:
            self.setStyleSheet('')
            self.setText(translate('switch_view_button', self.button_text[self.view]))
            self.setFixedHeight(self.original_height)

    dnd_active = property(_get_dnd_active, _set_dnd_active)
    del _get_dnd_active, _set_dnd_active

    def _change_view(self):
        self.view = self.ContactView if self.view is self.SessionView else self.SessionView

    def _update_dnd(self):
        self.dnd_timer.phase += 1
        if self.dnd_timer.phase == 11:
            self.dnd_timer.stop()
            self.click()
            self.setStyleSheet(self.dnd_style_sheet1)
        else:
            style_sheet = (self.dnd_style_sheet1, self.dnd_style_sheet2)[self.dnd_timer.phase % 2]
            self.setStyleSheet(style_sheet)

    def dragEnterEvent(self, event):
        if self.dnd_active:
            event.accept()
            self._update_dnd()
            self.dnd_timer.start()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        if self.dnd_active:
            self.dnd_timer.stop()
            self.dnd_timer.phase = 0
            self.setStyleSheet(self.dnd_style_sheet1)
        super(SwitchViewButton, self).dragLeaveEvent(event)

    def dropEvent(self, event):
        if self.dnd_active:
            self.dnd_timer.stop()
            self.dnd_timer.phase = 0
            self.setStyleSheet(self.dnd_style_sheet1)
        event.ignore()


class StateButtonStyle(QCommonStyle, ColorHelperMixin):
    _pixel_metrics = {QStyle.PixelMetric.PM_MenuButtonIndicator: 11, QStyle.PixelMetric.PM_DefaultFrameWidth: 3, QStyle.PixelMetric.PM_ButtonMargin: 1, QStyle.PixelMetric.PM_ButtonShiftHorizontal: 0, QStyle.PixelMetric.PM_ButtonShiftVertical: 0,
                      QStyle.PixelMetric.PM_ButtonIconSize: 32}

    def polish(self, widget):
        widget.setAttribute(Qt.WidgetAttribute.WA_Hover)
        super(StateButtonStyle, self).polish(widget)

    def pixelMetric(self, metric, option=None, widget=None):
        return self._pixel_metrics[metric]

    def sizeFromContents(self, element, option, size, widget=None):
        if element == QStyle.ContentsType.CT_ToolButton:
            return self.toolButtonSizeFromContents(option, size, widget)
        else:
            return super(StateButtonStyle, self).sizeFromContents(element, option, size, widget)

    def toolButtonSizeFromContents(self, option, size, widget):
        # Make width >= height to avoid super-skinny buttons
        margin = 2 * (self._pixel_metrics[QStyle.PixelMetric.PM_DefaultFrameWidth] + self._pixel_metrics[QStyle.PixelMetric.PM_ButtonMargin])
        if option.features & QStyleOptionToolButton.ToolButtonFeature.MenuButtonPopup:
            margin_size = QSize(margin+1, margin)
            menu_width = self._pixel_metrics[QStyle.PixelMetric.PM_MenuButtonIndicator]
        else:
            margin_size = QSize(margin, margin)
            menu_width = 0
        if size.width() - menu_width < size.height():
            size.setWidth(size.height() + menu_width)
        return size + margin_size

    def drawComplexControl(self, control, option, painter, widget=None):
        if control == QStyle.ComplexControl.CC_ToolButton:
            painter.save()
            self.drawToolButtonComplexControl(option, painter, widget)
            painter.restore()
        else:
            super(StateButtonStyle, self).drawComplexControl(control, option, painter, widget)

    def drawToolButtonComplexControl(self, option, painter, widget):
        button_color = option.palette.color(QPalette.ColorRole.Button)

        if option.state & (QStyle.StateFlag.State_On|QStyle.StateFlag.State_Sunken):
            self.drawToolButtonSunkenBezel(painter, QRectF(option.rect).adjusted(1, 1, -1, -1), button_color)
        else:
            enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
            hoover = enabled and bool(option.state & QStyle.StateFlag.State_MouseOver)
            has_focus = enabled and bool(option.state & QStyle.StateFlag.State_HasFocus)
            self.drawToolButtonBezel(painter, QRectF(option.rect), button_color, hoover=hoover, has_focus=has_focus)
        if option.features & QStyleOptionToolButton.ToolButtonFeature.MenuButtonPopup:
            self.drawToolButtonMenuIndicator(option, painter, widget)
        self.drawToolButtonContent(option, painter, widget)

    def drawToolButtonBezel(self, painter, rect, color, hoover=False, has_focus=False):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        glow_rect = rect
        shadow_rect  = rect.adjusted(1, 1, -1, -1)
        border_rect  = rect.adjusted(2, 2, -2, -2)
        content_rect = rect.adjusted(3, 3, -3, -3)

        focus_color = QColor('#3aa7dd')
        hoover_color = QColor('#6ed6ff')
        shadow_color = ColorScheme.shade(self.background_bottom_color(color), ColorScheme.ShadowShade, 0.0)
        border_color_top = ColorScheme.shade(self.background_top_color(color), ColorScheme.LightShade, 0.0)
        border_color_bottom = ColorScheme.shade(self.background_bottom_color(color), ColorScheme.MidlightShade, 0.5)

        # glow
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        blend = QLinearGradient(glow_rect.topLeft(), glow_rect.bottomLeft())
        if hoover:
            blend.setColorAt(0.0, self.color_with_alpha(hoover_color, 0x45))
            blend.setColorAt(0.9, self.color_with_alpha(hoover_color, 0x45))
            blend.setColorAt(1.0, self.color_with_alpha(ColorUtils.mix(hoover_color, shadow_color, 0.4), 0x55))
        elif has_focus:
            blend.setColorAt(0.0, self.color_with_alpha(focus_color, 0x45))
            blend.setColorAt(0.9, self.color_with_alpha(focus_color, 0x45))
            blend.setColorAt(1.0, self.color_with_alpha(ColorUtils.mix(focus_color, shadow_color, 0.4), 0x55))
        else:
            blend.setColorAt(0.0, Qt.GlobalColor.transparent)  # or @0.5
            blend.setColorAt(0.9, self.color_with_alpha(shadow_color, 0x10))
            # blend.setColorAt(1-4.0/glow_rect.height(), self.color_with_alpha(shadow_color, 0x10))  # this is for exactly 4 pixels from bottom
            blend.setColorAt(1.0, self.color_with_alpha(shadow_color, 0x30)) # 0x25, 0x30 or 0x35
        painter.setBrush(blend)
        painter.drawRoundedRect(glow_rect, 5, 5)  # 5 or 6

        # shadow
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if hoover:
            painter.setBrush(hoover_color)
        elif has_focus:
            painter.setBrush(focus_color)
        else:
            blend = QLinearGradient(shadow_rect.topLeft(), shadow_rect.bottomLeft())
            blend.setColorAt(0.00, self.color_with_alpha(shadow_color, 0x10))
            blend.setColorAt(1.00, self.color_with_alpha(shadow_color, 0x80))
            painter.setBrush(blend)
        painter.drawRoundedRect(shadow_rect, 4, 4)  # 4 or 5

        # border
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        blend = QLinearGradient(border_rect.topLeft(), border_rect.bottomLeft())
        blend.setColorAt(0.0, border_color_top)
        blend.setColorAt(1.0, border_color_bottom)
        painter.setBrush(blend)
        painter.drawRoundedRect(border_rect, 4, 4)

        # content
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        grad = QLinearGradient(content_rect.topLeft(), content_rect.bottomLeft())
        grad.setColorAt(0.0, self.background_top_color(color))
        grad.setColorAt(1.0, self.background_bottom_color(color))
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(content_rect, 4, 4)

    def drawToolButtonSunkenBezel(self, painter, rect, color):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        hole_rect    = rect.adjusted(1, 1, -1, -1)
        shadow_rect  = rect.adjusted(2, 2, -2, -2)
        content_rect = rect.adjusted(3, 3, -3, -3)

        shade_color  = ColorScheme.shade(self.background_bottom_color(color), ColorScheme.MidlightShade, 0.5)
        shadow_color = ColorScheme.shade(self.background_bottom_color(color), ColorScheme.ShadowShade, 0.0)

        if self.calc_shadow_color(color).value() > color.value():
            content_grad = QLinearGradient(0, content_rect.top(), 0, content_rect.bottom()+content_rect.height()*0.2)
            content_grad.setColorAt(0.0, self.background_bottom_color(color))
            content_grad.setColorAt(1.0, self.background_top_color(color))
        else:
            content_grad = QLinearGradient(0, content_rect.top()-content_rect.height()*0.2, 0, content_rect.bottom())
            content_grad.setColorAt(0.0, self.background_top_color(color))
            content_grad.setColorAt(1.0, self.background_bottom_color(color))

        # hole edge
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        blend = QLinearGradient(hole_rect.topLeft(), hole_rect.bottomLeft())
        blend.setColorAt(0.0, self.color_with_alpha(shadow_color, 0x80))
        blend.setColorAt(1.0, self.color_with_alpha(shadow_color, 0x20))
        painter.setBrush(blend)
        painter.drawRoundedRect(hole_rect, 4, 4)  # 4 or 5

        # shadow
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.setBrush(content_grad)
        painter.drawRoundedRect(shadow_rect, 4, 4)  # 5 or 6
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        blend = QLinearGradient(shadow_rect.topLeft(), shadow_rect.bottomLeft())
        blend.setColorAt(0.0, self.color_with_alpha(shadow_color, 0x40))
        blend.setColorAt(0.1, self.color_with_alpha(shadow_color, 0x07))
        blend.setColorAt(0.9, self.color_with_alpha(shadow_color, 0x07))
        blend.setColorAt(1.0, shade_color)
        painter.setBrush(blend)
        painter.drawRoundedRect(shadow_rect, 4, 4)  # 5 or 6

        # content
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.setBrush(content_grad)
        painter.drawRoundedRect(content_rect, 4, 4)

    def drawToolButtonMenuIndicator(self, option, painter, widget=None):
        arrow_rect = self.proxy().subControlRect(QStyle.ComplexControl.CC_ToolButton, option, QStyle.SubControl.SC_ToolButtonMenu, widget)

        text_color = option.palette.color(QPalette.ColorRole.WindowText if option.state & QStyle.StateFlag.State_AutoRaise else QPalette.ColorRole.ButtonText)
        button_color = option.palette.color(QPalette.ColorRole.Button)
        background_color = self.background_color(button_color, 0.5)

        painter.save()

        # draw separating vertical line
        if option.state & (QStyle.StateFlag.State_On|QStyle.StateFlag.State_Sunken):
            top_offset, bottom_offset = 4, 3
        else:
            top_offset, bottom_offset = 2, 2

        if option.direction == Qt.LayoutDirection.LeftToRight:
            separator_line = QLineF(arrow_rect.x()-3, arrow_rect.top()+top_offset, arrow_rect.x()-3, arrow_rect.bottom()-bottom_offset)
        else:
            separator_line = QLineF(arrow_rect.right()+3, arrow_rect.top()+top_offset, arrow_rect.right()+3, arrow_rect.bottom()-bottom_offset)

        light_gradient = QLinearGradient(separator_line.p1(), separator_line.p2())
        light_gradient.setColorAt(0.0, ColorScheme.shade(self.background_top_color(button_color), ColorScheme.LightShade, 0.0))
        light_gradient.setColorAt(1.0, ColorScheme.shade(self.background_bottom_color(button_color), ColorScheme.MidlightShade, 0.5))
        separator_color = ColorScheme.shade(self.background_bottom_color(button_color), ColorScheme.MidShade, 0.0)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QPen(light_gradient, 1))
        painter.drawLine(separator_line.translated(-1, 0))
        painter.drawLine(separator_line.translated(+1, 0))
        painter.setPen(QPen(separator_color, 1))
        painter.drawLine(separator_line)

        # draw arrow
        arrow = QPolygonF([QPointF(-3, -1.5), QPointF(0.5, 2.5), QPointF(4, -1.5)])
        if option.direction == Qt.LayoutDirection.LeftToRight:
            arrow.translate(-2, 1)
        else:
            arrow.translate(+2, 1)
        pen_thickness = 1.6

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(arrow_rect.center())

        painter.translate(0, +1)
        painter.setPen(QPen(self.calc_light_color(background_color), pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPolyline(arrow)
        painter.translate(0, -1)
        painter.setPen(QPen(self.deco_color(background_color, text_color), pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPolyline(arrow)

        painter.restore()

    def drawToolButtonContent(self, option, painter, widget):
        if option.state & QStyle.StateFlag.State_Enabled:
            pixmap = widget.pixmap(QIcon.Mode.Normal)
        else:
            pixmap = widget.pixmap(QIcon.Mode.Disabled)
        if not pixmap.isNull():
            margin = self._pixel_metrics[QStyle.PixelMetric.PM_DefaultFrameWidth] + self._pixel_metrics[QStyle.PixelMetric.PM_ButtonMargin]
            if option.features & QStyleOptionToolButton.ToolButtonFeature.MenuButtonPopup and option.direction == Qt.LayoutDirection.LeftToRight:
                right_offset = 1
            else:
                right_offset = 0
            content_rect = QRectF(self.proxy().subControlRect(QStyle.ComplexControl.CC_ToolButton, option, QStyle.SubControl.SC_ToolButton, widget)).adjusted(margin, margin, -margin-right_offset, -margin)
            pixmap_rect  = QRectF(pixmap.rect())
            pixmap_rect.moveCenter(content_rect.center())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.drawPixmap(pixmap_rect.topLeft(), pixmap)


class StateButton(QToolButton):
    default_color = QColor('#efedeb')

    def __init__(self, parent=None):
        super(StateButton, self).__init__(parent)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Button, self.default_color)
        self.setPalette(palette)
        self.setStyle(StateButtonStyle())

    def pixmap(self, mode=QIcon.Mode.Normal, state=QIcon.State.Off):
        pixmap = self.icon().pixmap(self.iconSize(), mode, state)
        if pixmap.isNull():
            return pixmap

        size = max(pixmap.width(), pixmap.height())
        offset_x = int((size - pixmap.width())/2)
        offset_y = int((size - pixmap.height())/2)
        if platform.system() == 'Darwin':
            if size == 48:
                # default icon
                offset_x = offset_x + 8
                offset_y = offset_y + 8
            else:
                # user chosen icon
                offset_x = offset_x + 14
                offset_y = offset_y + 16

        new_pixmap = QPixmap(size, size)
        new_pixmap.fill(Qt.GlobalColor.transparent)
        path = QPainterPath()
        path.addRoundedRect(0, 0, size, size, 3.7, 3.7)
        painter = QPainter(new_pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setClipPath(path)
        painter.drawPixmap(offset_x, offset_y, pixmap)
        painter.end()

        return new_pixmap


class PresenceState(object):
    def __init__(self, name, color, icon, internal=False):
        self.name = name
        self.color = color
        self.icon = icon
        self.internal = internal

    def __eq__(self, other):
        if isinstance(other, PresenceState):
            return self.name == other.name
        return NotImplemented

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    def __repr__(self):
        return self.name

    @property
    def Internal(self):
        return PresenceState(self.name, self.color, self.icon, True)


class AccountState(StateButton):
    Invisible = PresenceState(QT_TRANSLATE_NOOP('presence_state', 'Invisible'), '#efedeb', Resources.get('icons/state-invisible.svg'))
    Available = PresenceState(QT_TRANSLATE_NOOP('presence_state', 'Available'), '#00ff00', Resources.get('icons/state-available.svg'))
    Away = PresenceState(QT_TRANSLATE_NOOP('presence_state', 'Away'), '#ffff00', Resources.get('icons/state-away.svg'))
    Busy = PresenceState(QT_TRANSLATE_NOOP('presence_state', 'Busy'), '#ff0000', Resources.get('icons/state-busy.svg'))

    stateChanged = pyqtSignal()

    history_size = 7

    def __init__(self, parent=None):
        super(AccountState, self).__init__(parent)
        menu = QMenu(self)
        for state in (self.Available, self.Away, self.Busy, self.Invisible):
            action = menu.addAction(QIcon(state.icon), translate('presence_state', state.name))
            action.state = state
            action.note = None
        menu.addSeparator()
        menu.triggered.connect(self._SH_MenuTriggered)
        self.setMenu(menu)
        self.state = self.Invisible
        self.note = None

    def _get_history(self):
        return [(action.state.name, action.note) for action in self.menu().actions()[5:]]

    def _set_history(self, values):
        menu = self.menu()
        for action in menu.actions()[5:]:
            menu.removeAction(action)
        for state_name, note in values:
            try:
                state = getattr(self, state_name)
            except AttributeError:
                continue
            action = menu.addAction(QIcon(state.icon), note)
            action.state = state
            action.note = note

    history = property(_get_history, _set_history)
    del _get_history, _set_history

    def _SH_MenuTriggered(self, action):
        if hasattr(action, 'state'):
            self.setState(action.state, action.note)

    def mousePressEvent_no(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.popupMode() == QToolButton.ToolButtonPopupMode.MenuButtonPopup:
            option = QStyleOptionToolButton()
            self.initStyleOption(option)
            position = self.style().subControlRect(QStyle.ComplexControl.CC_ToolButton, option, QStyle.SubControl.SC_ToolButtonMenu, self).center()
            event = event.__class__(event.type(), position, self.mapToGlobal(position), event.button(), event.buttons(), event.modifiers())
        return super(AccountState, self).mousePressEvent(event)

    def setState(self, state, note=None):
        if state == self.state and note == self.note:
            return
        self.state = state
        self.note = note
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Button, QColor(state.color))
        self.setPalette(palette)
        if note and not state.internal:
            menu = self.menu()
            actions = menu.actions()[5:]
            try:
                action = next(action for action in actions if action.state is state and action.note == note)
            except StopIteration:
                action = QAction(QIcon(state.icon), note, menu)
                if len(actions) == 0:
                    menu.addAction(action)
                else:
                    if len(actions) >= self.history_size:
                        menu.removeAction(actions[-1])
                    menu.insertAction(actions[0], action)
                action.state = state
                action.note = note
            else:
                if action is not actions[0]:
                    menu.removeAction(action)
                    menu.insertAction(actions[0], action)
        self.stateChanged.emit()


