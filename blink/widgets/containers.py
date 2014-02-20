# Copyright (c) 2014 AG Projects. See LICENSE for details.
#

__all__ = ['SlidingStackedWidget']

from PyQt4.QtCore import QEasingCurve, QParallelAnimationGroup, QPropertyAnimation, QPoint, pyqtSignal
from PyQt4.QtGui  import QStackedWidget

from blink.widgets.util import QtDynamicProperty


class SlidingStackedWidget(QStackedWidget):
    animationEasingCurve = QtDynamicProperty('animationEasingCurve', int)
    animationDuration = QtDynamicProperty('animationDuration', int)
    verticalMode = QtDynamicProperty('verticalMode', bool)
    wrap = QtDynamicProperty('wrap', bool)

    animationFinished = pyqtSignal()

    LeftToRight, RightToLeft, TopToBottom, BottomToTop, Automatic = range(5)

    def __init__(self, parent=None):
        super(SlidingStackedWidget, self).__init__(parent)
        self.animationEasingCurve = QEasingCurve.Linear
        self.animationDuration = 250
        self.verticalMode = False
        self.wrap = False
        self._active = False
        self._animation_group = QParallelAnimationGroup()
        self._animation_group.finished.connect(self._SH_AnimationGroupFinished)

    def slideInNext(self):
        next_index = self.currentIndex() + 1
        if self.wrap or next_index < self.count():
            self.slideInIndex(next_index % self.count(), direction=self.BottomToTop if self.verticalMode else self.RightToLeft)

    def slideInPrev(self):
        previous_index = self.currentIndex() - 1
        if self.wrap or previous_index >= 0:
            self.slideInIndex(previous_index % self.count(), direction=self.TopToBottom if self.verticalMode else self.LeftToRight)

    def slideInIndex(self, index, direction=Automatic):
        self.slideInWidget(self.widget(index), direction)

    def slideInWidget(self, widget, direction=Automatic):
        if self.indexOf(widget) == -1 or widget is self.currentWidget():
            return

        if self._active:
            return

        self._active = True

        prev_widget = self.currentWidget()
        next_widget = widget

        if direction == self.Automatic:
            if self.indexOf(prev_widget) < self.indexOf(next_widget):
                direction = self.BottomToTop if self.verticalMode else self.RightToLeft
            else:
                direction = self.TopToBottom if self.verticalMode else self.LeftToRight

        width = self.frameRect().width()
        height = self.frameRect().height()

        # the following is important, to ensure that the new widget has correct geometry information when sliding in the first time
        next_widget.setGeometry(0, 0, width, height)

        if direction in (self.TopToBottom, self.BottomToTop):
            offset = QPoint(0, height if direction==self.TopToBottom else -height)
        elif direction in (self.LeftToRight, self.RightToLeft):
            offset = QPoint(width if direction==self.LeftToRight else -width, 0)

        # re-position the next widget outside of the display area
        prev_widget_position = prev_widget.pos()
        next_widget_position = next_widget.pos()

        next_widget.move(next_widget_position - offset)
        next_widget.show()
        next_widget.raise_()

        prev_widget_animation = QPropertyAnimation(prev_widget, "pos")
        prev_widget_animation.setDuration(self.animationDuration)
        prev_widget_animation.setEasingCurve(QEasingCurve(self.animationEasingCurve))
        prev_widget_animation.setStartValue(prev_widget_position)
        prev_widget_animation.setEndValue(prev_widget_position + offset)

        next_widget_animation = QPropertyAnimation(next_widget, "pos")
        next_widget_animation.setDuration(self.animationDuration)
        next_widget_animation.setEasingCurve(QEasingCurve(self.animationEasingCurve))
        next_widget_animation.setStartValue(next_widget_position - offset)
        next_widget_animation.setEndValue(next_widget_position)

        self._animation_group.clear()
        self._animation_group.addAnimation(prev_widget_animation)
        self._animation_group.addAnimation(next_widget_animation)
        self._animation_group.start()

    def _SH_AnimationGroupFinished(self):
        prev_widget_animation = self._animation_group.animationAt(0)
        next_widget_animation = self._animation_group.animationAt(1)
        prev_widget = prev_widget_animation.targetObject()
        next_widget = next_widget_animation.targetObject()
        self.setCurrentWidget(next_widget)
        prev_widget.hide() # this may have been done already by QStackedWidget when changing the current widget above -Dan
        prev_widget.move(prev_widget_animation.startValue()) # move the outshifted widget back to its original position
        self._animation_group.clear()
        self._active = False
        self.animationFinished.emit()


