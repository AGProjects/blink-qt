"""Application style: the platform's style with push buttons drawn as rounded, filled rectangles.

Under some Linux platform styles a push button has the window's own colour and no visible
edge, so only its label shows. Current desktops (GNOME/Yaru, KDE Breeze) draw a filled
rounded shape; this draws the same for every push button, in the palette's colours, so it
follows light and dark."""

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPalette, QPen
from PyQt6.QtWidgets import QProxyStyle, QStyle, QStyleFactory, QStyleOptionButton, QStyleOptionToolButton, QWidget


__all__ = ['BlinkStyle', 'make_segmented']


def make_segmented(name, *buttons):
    """Draw the buttons as one segmented control: side by side with no gap, rounded only at the
    outer ends, a line between them. The buttons must be adjacent in a layout with no spacing
    between them; hidden ones are left out and the others close up."""
    for button in buttons:
        button.setProperty('segmented', name)


def _blend(a, b, ratio):
    """a mixed with b: ratio 0 is a, 1 is b."""
    return QColor(round(a.red() + (b.red() - a.red()) * ratio),
                  round(a.green() + (b.green() - a.green()) * ratio),
                  round(a.blue() + (b.blue() - a.blue()) * ratio))


class BlinkStyle(QProxyStyle):
    radius = 6.0

    def __init__(self, base_style_name):
        base = QStyleFactory.create(base_style_name) if base_style_name else None
        super().__init__(base or QStyleFactory.create('Fusion'))

    # Push and tool buttons are drawn here entirely, not through the base style: platform styles
    # that paint with the toolkit (gtk2) never ask the proxy for the button panel.

    def drawControl(self, element, option, painter, widget=None):
        if element == QStyle.ControlElement.CE_PushButton and isinstance(option, QStyleOptionButton):
            self.drawControl(QStyle.ControlElement.CE_PushButtonBevel, option, painter, widget)
            label = QStyleOptionButton(option)
            label.rect = self.subElementRect(QStyle.SubElement.SE_PushButtonContents, option, widget)
            self.drawControl(QStyle.ControlElement.CE_PushButtonLabel, label, painter, widget)
            return
        if element == QStyle.ControlElement.CE_PushButtonBevel and isinstance(option, QStyleOptionButton):
            if option.features & QStyleOptionButton.ButtonFeature.Flat and not option.state & (QStyle.StateFlag.State_Sunken | QStyle.StateFlag.State_On):
                return
            self._draw_button_panel(option, painter)
            if option.features & QStyleOptionButton.ButtonFeature.HasMenu:
                size = self.pixelMetric(QStyle.PixelMetric.PM_MenuButtonIndicator, option, widget)
                arrow = QStyleOptionButton(option)
                arrow.rect = option.rect.adjusted(option.rect.width() - size - 4, 0, -4, 0)
                self.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorArrowDown, arrow, painter, widget)
            return
        super().drawControl(element, option, painter, widget)

    def drawComplexControl(self, control, option, painter, widget=None):
        if control == QStyle.ComplexControl.CC_ToolButton and isinstance(option, QStyleOptionToolButton):
            self._draw_tool_button(option, painter, widget)
            return
        super().drawComplexControl(control, option, painter, widget)

    def drawPrimitive(self, element, option, painter, widget=None):
        if element in (QStyle.PrimitiveElement.PE_PanelButtonCommand, QStyle.PrimitiveElement.PE_PanelButtonTool):
            self._draw_button_panel(option, painter, self._segment(widget))
            return
        if element == QStyle.PrimitiveElement.PE_FrameDefaultButton:
            return      # the default button is marked by _draw_button_panel
        super().drawPrimitive(element, option, painter, widget)

    def _draw_tool_button(self, option, painter, widget):
        """The shape is drawn always, also for auto-raise buttons (which show it only under the mouse)."""
        button_rect = self.subControlRect(QStyle.ComplexControl.CC_ToolButton, option, QStyle.SubControl.SC_ToolButton, widget)
        menu_rect = self.subControlRect(QStyle.ComplexControl.CC_ToolButton, option, QStyle.SubControl.SC_ToolButtonMenu, widget)

        panel = QStyleOptionToolButton(option)
        if not option.activeSubControls & QStyle.SubControl.SC_ToolButton:
            panel.state &= ~QStyle.StateFlag.State_Sunken
        self._draw_button_panel(panel, painter, self._segment(widget))

        if option.subControls & QStyle.SubControl.SC_ToolButtonMenu:
            arrow = QStyleOptionToolButton(option)
            arrow.rect = menu_rect
            self.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorArrowDown, arrow, painter, widget)
        elif option.features & QStyleOptionToolButton.ToolButtonFeature.HasMenu:
            size = self.pixelMetric(QStyle.PixelMetric.PM_MenuButtonIndicator, option, widget)
            arrow = QStyleOptionToolButton(option)
            arrow.rect = QRect(button_rect.right() - size + 2, button_rect.bottom() - size + 2, size - 4, size - 4)
            self.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorArrowDown, arrow, painter, widget)

        label = QStyleOptionToolButton(panel)
        frame_width = self.pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth, option, widget)
        label.rect = button_rect.adjusted(frame_width, frame_width, -frame_width, -frame_width)
        self.drawControl(QStyle.ControlElement.CE_ToolButtonLabel, label, painter, widget)

    @staticmethod
    def _segment(widget):
        """For a button of a segmented control: 'left', 'middle' or 'right'; else None."""
        name = widget.property('segmented') if widget is not None else None
        if not name or widget.parentWidget() is None:
            return None
        parent = widget.parentWidget()
        members = sorted((child for child in parent.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
                          if child.property('segmented') == name and child.isVisibleTo(parent)), key=lambda child: child.x())
        if len(members) < 2 or widget not in members:
            return None
        index = members.index(widget)
        return 'left' if index == 0 else 'right' if index == len(members) - 1 else 'middle'

    def _draw_button_panel(self, option, painter, segment=None):
        palette = option.palette
        state = option.state
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        sunken = bool(state & (QStyle.StateFlag.State_Sunken | QStyle.StateFlag.State_On))
        hover = enabled and bool(state & QStyle.StateFlag.State_MouseOver)
        focus = enabled and bool(state & QStyle.StateFlag.State_HasFocus) and bool(state & QStyle.StateFlag.State_KeyboardFocusChange)
        default = isinstance(option, QStyleOptionButton) and bool(option.features & QStyleOptionButton.ButtonFeature.DefaultButton)

        group = QPalette.ColorGroup.Normal if enabled else QPalette.ColorGroup.Disabled
        window = palette.color(group, QPalette.ColorRole.Window)
        text = palette.color(group, QPalette.ColorRole.ButtonText)
        dark = window.lightness() < 128

        # a fill set apart from the window: a step towards the text colour
        fill = _blend(window, text, 0.07 if dark else 0.045)
        # idle: a faint edge; under the mouse or pressed it comes forward
        border = _blend(window, text, 0.11 if dark else 0.085)
        separator = _blend(window, text, 0.16 if dark else 0.13)    # between the segments of a segmented control
        if default:
            border = _blend(window, text, 0.24 if dark else 0.20)   # the default button: a firmer edge, still neutral
        if sunken or hover:
            # the button under the mouse stands out: a darker fill and a clear neutral edge
            border = _blend(window, text, 0.40 if dark else 0.34)
        if sunken:
            fill = _blend(window, text, 0.30 if dark else 0.24)
        elif hover:
            fill = _blend(window, text, 0.22 if dark else 0.17)
        if not enabled:
            fill = _blend(window, fill, 0.45)
            border = _blend(window, border, 0.45)
            separator = _blend(window, separator, 0.45)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(1.5, 1.5, -1.5, -1.5)
        if segment is None:
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, self.radius, self.radius)
        else:
            # one segment: the rounded shape runs on past the inner sides and is cut off at the
            # button's edge, so only the outer ends are round; a line separates it from the left one
            outer = QRectF(option.rect)
            extend = 2 * self.radius
            left = rect.left() if segment == 'left' else outer.left() - extend
            right = rect.right() if segment == 'right' else outer.right() + 1 + extend
            shape = QRectF(left, rect.top(), right - left, rect.height())
            painter.setClipRect(outer)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(shape, self.radius, self.radius)
            if segment != 'left':
                painter.setPen(QPen(separator, 1.0))
                painter.drawLine(QPointF(outer.left() + 0.5, rect.top()), QPointF(outer.left() + 0.5, rect.bottom()))
            painter.setClipping(False)

        if focus:
            painter.setPen(QPen(_blend(window, text, 0.55 if dark else 0.5), 2.0))     # neutral, not the accent colour
            painter.setBrush(Qt.BrushStyle.NoBrush)
            inset = 1.0
            painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), self.radius - inset, self.radius - inset)
        painter.restore()

