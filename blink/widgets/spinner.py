
from __future__ import division

from PyQt5.QtCore import QEvent, QRectF, QSize
from PyQt5.QtSvg import QSvgWidget

from blink.resources import Resources
from blink.widgets.util import QtDynamicProperty


__all__ = ['Spinner']


class Spinner(QSvgWidget):
    icon_file = QtDynamicProperty('icon_file', type=unicode)
    icon_size = QtDynamicProperty('icon_size', type=QSize)
    icon_crop = QtDynamicProperty('icon_crop', type=int)

    def __init__(self, parent=None, icon='icons/spinner.svg'):
        super(Spinner, self).__init__(parent)
        self._original_viewbox = QRectF()
        self.icon_crop = 0
        self.icon_size = None
        self.icon_file = Resources.get(icon)

    def load(self, svg):
        super(Spinner, self).load(svg)
        self._original_viewbox = self.renderer().viewBoxF()
        self._update_viewbox(self.size())

    def event(self, event):
        if event.type() == QEvent.DynamicPropertyChange:
            if event.propertyName() == 'icon_crop':
                self._update_viewbox(self.size())
            elif event.propertyName() == 'icon_file':
                self.load(self.icon_file)
            elif event.propertyName() == 'icon_size':
                self.updateGeometry()
        return super(Spinner, self).event(event)

    def resizeEvent(self, event):
        super(Spinner, self).resizeEvent(event)
        self._update_viewbox(event.size())

    def sizeHint(self):
        return self.icon_size or super(Spinner, self).sizeHint()

    def _update_viewbox(self, size):
        if self._original_viewbox.isEmpty() or size.isEmpty():
            return
        viewbox = self._original_viewbox.adjusted(self.icon_crop, self.icon_crop, -self.icon_crop, -self.icon_crop)
        width = size.width()
        height = size.height()
        if height >= width:
            new_viewbox = QRectF(viewbox.x(), viewbox.y() + viewbox.height()/2 * (1 - height/width), viewbox.width(), viewbox.height() * height/width)
        else:
            new_viewbox = QRectF(viewbox.x() + viewbox.width()/2 * (1 - width/height), viewbox.y(), viewbox.width() * width/height, viewbox.height())
        self.renderer().setViewBox(new_viewbox)
