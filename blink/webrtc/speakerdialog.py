"""Speaker selection: the layout of a video room for everyone in it, as Sylk Mobile's
SpeakerSelectionModal (and Sylk desktop's speaker selection) sets it.

  - Grid: no speakers, everyone shares the window
  - 1 speaker: one participant fills the window, the others are in the carousel
  - 2 speakers: two side by side, the others in the carousel

Only participants with video can be speakers, ourselves included. Applied with
videoroom-configure: the server tells everyone (the configure event), so the
window changes when it says so.
"""

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QFont, QPainterPath
from PyQt6.QtWidgets import (QButtonGroup, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                             QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from blink.util import translate
from blink.widgets.style import make_segmented


__all__ = ['SpeakerSelectionDialog']


def _icon_speakers(painter):
    """A large figure in a frame with a small one beside it, in the call bar's 24x18 box (blink.videowindow.draw_icon)."""
    pen = painter.pen()
    frame = QPainterPath()
    frame.addRoundedRect(QRectF(2.5, 2.5, 14, 13), 2.5, 2.5)
    painter.strokePath(frame, pen)
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(pen.color())
    painter.drawEllipse(QPointF(9.5, 7.2), 2.3, 2.3)
    body = QPainterPath()
    body.moveTo(5.2, 14.5)
    body.cubicTo(5.6, 11, 13.4, 11, 13.8, 14.5)
    body.closeSubpath()
    painter.drawPath(body)
    painter.drawEllipse(QPointF(20, 9), 1.6, 1.6)
    small = QPainterPath()
    small.moveTo(17.4, 15)
    small.cubicTo(17.7, 12.2, 22.3, 12.2, 22.6, 15)
    small.closeSubpath()
    painter.drawPath(small)
    painter.restore()


from blink.videowindow import ICONS
ICONS.setdefault('speakers', _icon_speakers)


GRID, ONE, TWO = 'grid', 'one', 'two'


class SpeakerSelectionDialog(QDialog):
    """candidates: [(publisher id, name)] of the participants with video, ours included;
    speakers: the current ones (ids). apply(ids) is called with the choice."""

    def __init__(self, candidates, speakers, apply, parent=None):
        super(SpeakerSelectionDialog, self).__init__(parent)
        self.candidates = sorted(candidates, key=lambda candidate: candidate[1].lower())
        self.apply = apply
        known = {id for id, name in self.candidates}
        speakers = [id for id in speakers if id in known][:2]
        self.slots = [speakers[0] if speakers else None, speakers[1] if len(speakers) > 1 else None]
        self.setWindowTitle(translate('speaker_selection', 'Speaker selection'))
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(10)

        heading = QLabel(translate('speaker_selection', 'Speaker selection'), self)
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.3)
        heading.setFont(font)
        heading.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(heading)

        # the layout: one segmented control
        modes = QHBoxLayout()
        modes.setSpacing(0)
        modes.addStretch(1)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for mode, label in ((GRID, translate('speaker_selection', 'Grid')),
                            (ONE, translate('speaker_selection', '1 speaker')),
                            (TWO, translate('speaker_selection', '2 speakers'))):
            button = QToolButton(self)
            button.setText(label)
            button.setCheckable(True)
            button.setMinimumWidth(110)
            button.setMinimumHeight(30)
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            modes.addWidget(button)
            button.clicked.connect(lambda checked, mode=mode: self._set_mode(mode))
        make_segmented('speaker_mode', *self.mode_buttons.values())
        modes.addStretch(1)
        layout.addLayout(modes)

        self.pages = QStackedWidget(self)
        hint = QLabel(translate('speaker_selection', 'Everyone shares the window equally, no pinned speakers.'), self)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        self.pages.addWidget(hint)
        columns = QWidget(self)
        columns_layout = QHBoxLayout(columns)
        columns_layout.setContentsMargins(0, 0, 0, 0)
        self.headers, self.lists = [], []
        for slot in (0, 1):
            column = QVBoxLayout()
            header = QLabel(columns)
            header_font = QFont(header.font())
            header_font.setBold(True)
            header.setFont(header_font)
            listing = QListWidget(columns)
            listing.setMinimumHeight(220)
            listing.setSelectionMode(QListWidget.SelectionMode.NoSelection)
            listing.itemClicked.connect(lambda item, slot=slot: self._pick(slot, item.data(Qt.ItemDataRole.UserRole)))
            column.addWidget(header)
            column.addWidget(listing)
            columns_layout.addLayout(column)
            self.headers.append(header)
            self.lists.append(listing)
        self.pages.addWidget(columns)
        layout.addWidget(self.pages)

        note = QLabel(translate('speaker_selection', 'The layout changes for everyone in the room.'), self)
        note.setEnabled(False)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton(translate('speaker_selection', 'Cancel'), self)
        self.apply_button = QPushButton(translate('speaker_selection', 'Apply'), self)
        self.apply_button.setDefault(True)
        cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(self._SH_ApplyClicked)
        buttons.addWidget(cancel_button)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

        self.mode = TWO if self.slots[1] else ONE if self.slots[0] else GRID
        self.mode_buttons[self.mode].setChecked(True)
        self._set_mode(self.mode)

    def _name(self, id):
        return next((name for candidate, name in self.candidates if candidate == id), '')

    def _set_mode(self, mode):
        self.mode = mode
        if mode == GRID:
            self.slots = [None, None]
        elif mode == ONE:
            self.slots[1] = None
        self.pages.setCurrentIndex(0 if mode == GRID else 1)
        for widget in (self.headers[1], self.lists[1]):
            widget.setVisible(mode == TWO)
        self._refresh()

    def _pick(self, slot, id):
        """As in Sylk Mobile: a click picks, a click on the picked one clears it; one person is in one slot only."""
        if self.slots[slot] == id:
            self.slots[slot] = None
        else:
            other = 1 - slot
            if self.slots[other] == id:
                self.slots[other] = None
            self.slots[slot] = id
        self._refresh()

    def _refresh(self):
        titles = (translate('speaker_selection', 'Speaker') if self.mode == ONE else translate('speaker_selection', 'Speaker 1'),
                  translate('speaker_selection', 'Speaker 2'))
        for slot, (header, listing) in enumerate(zip(self.headers, self.lists)):
            chosen = self.slots[slot]
            header.setText(titles[slot] + (f' — {self._name(chosen)}' if chosen else ''))
            listing.clear()
            if not self.candidates:
                item = QListWidgetItem(translate('speaker_selection', 'No participants with video'))
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                listing.addItem(item)
                continue
            for id, name in self.candidates:
                item = QListWidgetItem(('●  ' if id == chosen else '○  ') + name)
                item.setData(Qt.ItemDataRole.UserRole, id)
                if id == chosen:
                    font = QFont(item.font())
                    font.setBold(True)
                    item.setFont(font)
                listing.addItem(item)
        # 1 speaker needs one; 2 speakers at least one (as mobile, an empty second slot is fine)
        self.apply_button.setEnabled(self.mode == GRID or self.slots[0] is not None or self.slots[1] is not None)

    def _SH_ApplyClicked(self):
        if self.mode == GRID:
            ids = []
        elif self.mode == ONE:
            ids = [self.slots[0]] if self.slots[0] else []
        else:
            ids = [id for id in self.slots if id]
        self.apply(ids)
        self.accept()
