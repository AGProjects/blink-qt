# Copyright (c) 2014 AG Projects. See LICENSE for details.
#

__all__ = ['Graph', 'GraphWidget', 'HeightScaler', 'LogarithmicScaler', 'MaxScaler', 'SoftScaler']

from PyQt4.QtCore import Qt, QLine, QPointF, QMetaObject, pyqtSignal
from PyQt4.QtGui  import QColor, QLinearGradient, QPainterPath, QPen, QPolygonF, QStyle, QStyleOption, QStylePainter, QWidget

from abc import ABCMeta, abstractmethod
from application.python import limit
from collections import deque
from itertools import chain, islice
from math import ceil, log10, modf

from blink.widgets.color import ColorHelperMixin
from blink.widgets.util import QtDynamicProperty


class HeightScaler(object):
    __metaclass__ = ABCMeta

    @abstractmethod
    def get_height(self, max_value):
        return NotImplemented


class LogarithmicScaler(HeightScaler):
    """A scaler that returns the closest next power of 10"""

    def get_height(self, max_value):
        return 10 ** int(ceil(log10(max_value or 1)))


class MaxScaler(HeightScaler):
    """A scaler that returns the max_value"""

    def get_height(self, max_value):
        return max_value


class SoftScaler(HeightScaler):
    """A scaler that returns the closest next value from the series: { ..., 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, ... }"""

    def __init__(self):
        self.point_20 = log10(2)
        self.point_50 = log10(5)

    def get_height(self, max_value):
        fraction, integer = modf(log10(max_value or 0.9))
        if fraction < -self.point_50:
            return 10**integer / 5
        elif fraction < -self.point_20:
            return 10**integer / 2
        elif fraction < 0:
            return 10**integer
        elif fraction < self.point_20:
            return 10**integer * 2
        elif fraction < self.point_50:
            return 10**integer * 5
        else:
            return 10**integer * 10


class Graph(object):
    def __init__(self, data, color, over_boundary_color=None, fill_envelope=False, enabled=True):
        self.data = data
        self.color = color
        self.over_boundary_color = over_boundary_color or color
        self.fill_envelope = fill_envelope
        self.enabled = enabled

    @property
    def max_value(self):
        return max(self.data) if self.data else 0

    @property
    def last_value(self):
        return self.data[-1] if self.data else 0


class GraphWidget(QWidget, ColorHelperMixin):
    graphStyle = QtDynamicProperty('graphStyle', type=int)
    graphHeight = QtDynamicProperty('graphHeight', type=float)
    minHeight = QtDynamicProperty('minHeight', type=float)
    lineThickness = QtDynamicProperty('lineThickness', type=float)
    horizontalPixelsPerUnit = QtDynamicProperty('horizontalPixelsPerUnit', type=int)
    boundary = QtDynamicProperty('boundary', type=float)
    boundaryColor = QtDynamicProperty('boundaryColor', type=QColor)
    smoothEnvelope = QtDynamicProperty('smoothEnvelope', type=bool)
    smoothFactor = QtDynamicProperty('smoothFactor', type=float)
    fillEnvelope = QtDynamicProperty('fillEnvelope', type=bool)
    fillTransparency = QtDynamicProperty('fillTransparency', type=int)

    EnvelopeStyle, BarStyle = range(2)
    AutomaticHeight = 0

    updated = pyqtSignal()

    def __init__(self, parent=None):
        super(GraphWidget, self).__init__(parent)
        self.graphStyle = self.EnvelopeStyle
        self.graphHeight = self.AutomaticHeight
        self.minHeight = 0
        self.lineThickness = 1.6
        self.horizontalPixelsPerUnit = 2
        self.boundary = None
        self.boundaryColor = None
        self.smoothEnvelope = True
        self.smoothFactor = 0.1
        self.fillEnvelope = True
        self.fillTransparency = 50
        self.scaler = SoftScaler()
        self.graphs = []
        self.__dict__['graph_width'] = 0
        self.__dict__['graph_height'] = 0
        self.__dict__['max_value'] = 0

    def _get_scaler(self):
        return self.__dict__['scaler']

    def _set_scaler(self, scaler):
        if not isinstance(scaler, HeightScaler):
            raise TypeError("scaler must be a HeightScaler instance")
        self.__dict__['scaler'] = scaler

    scaler = property(_get_scaler, _set_scaler)
    del _get_scaler, _set_scaler

    @property
    def graph_width(self):
        return self.__dict__['graph_width']

    @property
    def graph_height(self):
        return self.__dict__['graph_height']

    @property
    def max_value(self):
        return self.__dict__['max_value']

    def paintEvent(self, event):
        option = QStyleOption()
        option.initFrom(self)

        contents_rect = self.style().subElementRect(QStyle.SE_FrameContents, option, self) or self.contentsRect() # the SE_FrameContents rect is Null unless the stylesheet defines decorations

        if self.graphStyle == self.BarStyle:
            graph_width = self.__dict__['graph_width'] = int(ceil(float(contents_rect.width()) / self.horizontalPixelsPerUnit))
        else:
            graph_width = self.__dict__['graph_width'] = int(ceil(float(contents_rect.width() - 1) / self.horizontalPixelsPerUnit) + 1)

        max_value = self.__dict__['max_value'] = max(chain([0], *(islice(reversed(graph.data), graph_width) for graph in self.graphs if graph.enabled)))

        if self.graphHeight == self.AutomaticHeight or self.graphHeight < 0:
            graph_height = self.__dict__['graph_height'] = max(self.scaler.get_height(max_value), self.minHeight)
        else:
            graph_height = self.__dict__['graph_height'] = max(self.graphHeight, self.minHeight)

        if self.graphStyle == self.BarStyle:
            height_scaling = float(contents_rect.height()) / graph_height
        else:
            height_scaling = float(contents_rect.height() - self.lineThickness) / graph_height

        painter = QStylePainter(self)
        painter.drawPrimitive(QStyle.PE_Widget, option)

        painter.setClipRect(contents_rect)

        painter.save()
        painter.translate(contents_rect.x() + contents_rect.width() - 1, contents_rect.y() + contents_rect.height() - 1)
        painter.scale(-1, -1)

        painter.setRenderHint(QStylePainter.Antialiasing, self.graphStyle != self.BarStyle)

        for graph in (graph for graph in self.graphs if graph.enabled and graph.data):
            if self.boundary is not None and 0 < self.boundary < graph_height:
                boundary_width = min(5.0/height_scaling, self.boundary-0, graph_height-self.boundary)
                pen_color = QLinearGradient(0, (self.boundary - boundary_width) * height_scaling, 0, (self.boundary + boundary_width) * height_scaling)
                pen_color.setColorAt(0, graph.color)
                pen_color.setColorAt(1, graph.over_boundary_color)
                brush_color = QLinearGradient(0, (self.boundary - boundary_width) * height_scaling, 0, (self.boundary + boundary_width) * height_scaling)
                brush_color.setColorAt(0, self.color_with_alpha(graph.color, self.fillTransparency))
                brush_color.setColorAt(1, self.color_with_alpha(graph.over_boundary_color, self.fillTransparency))
            else:
                pen_color = graph.color
                brush_color = self.color_with_alpha(graph.color, self.fillTransparency)
            dataset = islice(reversed(graph.data), graph_width)
            if self.graphStyle == self.BarStyle:
                lines = [QLine(x*self.horizontalPixelsPerUnit, 0, x*self.horizontalPixelsPerUnit, y*height_scaling) for x, y in enumerate(dataset)]
                painter.setPen(QPen(pen_color, self.lineThickness))
                painter.drawLines(lines)
            else:
                painter.translate(0, +self.lineThickness/2 - 1)

                if self.smoothEnvelope and self.smoothFactor > 0:
                    min_value = 0
                    max_value = graph_height * height_scaling
                    cx_offset = self.horizontalPixelsPerUnit / 3.0
                    smoothness = self.smoothFactor

                    last_values = deque(3*[dataset.next() * height_scaling], maxlen=3) # last 3 values: 0 last, 1 previous, 2 previous previous

                    envelope = QPainterPath()
                    envelope.moveTo(0, last_values[0])
                    for x, y in enumerate(dataset, 1):
                        x = x * self.horizontalPixelsPerUnit
                        y = y * height_scaling * (1 - smoothness) + last_values[0] * smoothness
                        last_values.appendleft(y)
                        c1x = x - cx_offset * 2
                        c2x = x - cx_offset
                        c1y = limit((1 + smoothness) * last_values[1] - smoothness * last_values[2], min_value, max_value) # same gradient as previous previous value to previous value
                        c2y = limit((1 - smoothness) * last_values[0] + smoothness * last_values[1], min_value, max_value) # same gradient as previous value to last value
                        envelope.cubicTo(c1x, c1y, c2x, c2y, x, y)
                else:
                    envelope = QPainterPath()
                    envelope.addPolygon(QPolygonF([QPointF(x*self.horizontalPixelsPerUnit, y*height_scaling) for x, y in enumerate(dataset)]))

                if self.fillEnvelope or graph.fill_envelope:
                    first_element = envelope.elementAt(0)
                    last_element = envelope.elementAt(envelope.elementCount() - 1)
                    fill_path = QPainterPath()
                    fill_path.moveTo(last_element.x, last_element.y)
                    fill_path.lineTo(last_element.x + 1, last_element.y)
                    fill_path.lineTo(last_element.x + 1, -self.lineThickness)
                    fill_path.lineTo(-self.lineThickness, -self.lineThickness)
                    fill_path.lineTo(-self.lineThickness, first_element.y)
                    fill_path.connectPath(envelope)
                    painter.fillPath(fill_path, brush_color)

                painter.strokePath(envelope, QPen(pen_color, self.lineThickness, join=Qt.RoundJoin))

                painter.translate(0, -self.lineThickness/2 + 1)

        if self.boundary is not None and self.boundaryColor:
            painter.setRenderHint(QStylePainter.Antialiasing, False)
            painter.setPen(QPen(self.boundaryColor, 1.0))
            painter.drawLine(0, self.boundary*height_scaling, contents_rect.width(), self.boundary*height_scaling)

        painter.restore()

        # queue the 'updated' signal to be emited after returning to the main loop
        QMetaObject.invokeMethod(self, 'updated', Qt.QueuedConnection)

    def add_graph(self, graph):
        if not isinstance(graph, Graph):
            raise TypeError("graph should be an instance of Graph")
        self.graphs.append(graph)
        if graph.enabled:
            self.update()

    def remove_graph(self, graph):
        self.graphs.remove(graph)
        self.update()

    def clear(self):
        self.graphs = []
        self.update()


