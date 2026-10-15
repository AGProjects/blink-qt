"""A contact has a picture on the server AND one the user set on this computer.

Neither is obviously right (the server copy may come from another device, the
local one may be the picture this computer has always shown), so the user picks,
per contact. One row per contact, the server picture in one column and the local
one in the other, each with a Keep tick box; ticking one unticks the other, so a
row always has exactly one. Select All above each column ticks the whole column.
Confirm is the only way out, so every row leaves with an answer.

docs/CONTACT-AVATARS.md, "Pictures set on this device".
"""

__all__ = ['AvatarChoiceDialog']

import datetime
import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from blink.util import translate


PICTURE_SIZE = 60
NAME_WIDTH = 180
COLUMN_WIDTH = 230
MARGIN = 8
MAX_VISIBLE_ROWS = 6


def describe_time(value):
    """A time in milliseconds or seconds since the epoch, in local time."""
    if not value:
        return translate('avatar_choice', 'unknown time')
    try:
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000.0
        return datetime.datetime.fromtimestamp(seconds).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return translate('avatar_choice', 'unknown time')


class AvatarChoiceDialog(QDialog):
    """items: dicts with contact_id, name, detail, server_path, server_device, server_time,
    local_path, local_time, default ('server' or 'local').

    choose() returns {contact_id: 'server' | 'local'}.
    """

    def __init__(self, items, account, parent=None):
        super().__init__(parent)
        self.items = items
        self.boxes = {}         # contact id: {'server': QCheckBox, 'local': QCheckBox}
        self.setWindowTitle(translate('avatar_choice', 'Contact Pictures'))
        self.setModal(True)
        self.setWindowFlags((self.windowFlags() | Qt.WindowType.CustomizeWindowHint) & ~Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        intro = QLabel(translate('avatar_choice', 'These contacts have a picture on the server (%s) and a different one set on this computer. '
                                                  'Choose which to keep: the one kept is used on all your devices.') % account)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # the header and the rows share fixed column widths, so they line up
        header = QHBoxLayout()
        header.setContentsMargins(MARGIN + 1, 0, MARGIN + 1, 0)     # + the scroll area's frame
        header.setSpacing(0)
        contact_title = self._bold(translate('avatar_choice', 'Contact'))
        contact_title.setFixedWidth(NAME_WIDTH)
        header.addWidget(contact_title)
        for side, title in (('server', translate('avatar_choice', 'On the server')), ('local', translate('avatar_choice', 'On this computer'))):
            column = QWidget()
            column.setFixedWidth(COLUMN_WIDTH)
            box = QHBoxLayout(column)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(self._bold(title))
            select_all = QPushButton(translate('avatar_choice', 'Select All'))
            select_all.setAutoDefault(False)
            select_all.clicked.connect(lambda checked=False, side=side: self._select_all(side))
            box.addWidget(select_all)
            box.addStretch()
            header.addWidget(column)
        header.addStretch()
        layout.addLayout(header)

        rows = QWidget()
        grid = QGridLayout(rows)
        grid.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        grid.setHorizontalSpacing(0)
        grid.setColumnStretch(3, 1)
        for row, item in enumerate(items):
            name = QLabel(f"<b>{self._escape(item['name'])}</b><br><small>{self._escape(item.get('detail') or '')}</small>")
            name.setTextFormat(Qt.TextFormat.RichText)
            name.setFixedWidth(NAME_WIDTH)
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignVCenter)
            boxes = {}
            for column, side in enumerate(('server', 'local'), 1):
                if side == 'server':
                    source = translate('avatar_choice', 'Uploaded by %s') % item.get('server_device')
                    when = describe_time(item.get('server_time'))
                else:
                    source = translate('avatar_choice', 'Set on this computer')
                    when = describe_time(item.get('local_time'))
                cell, box = self._cell(item[f'{side}_path'], source, when)
                box.setChecked(item.get('default') == side)
                box.clicked.connect(lambda checked=False, contact_id=item['contact_id'], side=side: self._tick(contact_id, side))
                boxes[side] = box
                grid.addWidget(cell, row, column)
            self.boxes[item['contact_id']] = boxes
            if not any(box.isChecked() for box in boxes.values()):
                boxes['server'].setChecked(True)

        scroll = QScrollArea()
        scroll.setWidget(rows)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.StyledPanel)
        row_height = PICTURE_SIZE + 24
        scroll.setMinimumHeight(min(len(items), MAX_VISIBLE_ROWS) * row_height + 8)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(scroll)

        buttons = QHBoxLayout()
        buttons.addStretch()
        confirm = QPushButton(translate('avatar_choice', 'Confirm'))
        confirm.setDefault(True)
        confirm.clicked.connect(self.accept)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)
        self.resize(NAME_WIDTH + 2 * COLUMN_WIDTH + 2 * MARGIN + 40, self.sizeHint().height())

    @staticmethod
    def _escape(text):
        import html
        return html.escape(str(text or ''))

    @staticmethod
    def _bold(text):
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _cell(self, path, source, when):
        cell = QWidget()
        cell.setFixedWidth(COLUMN_WIDTH)
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 4, 0, 4)
        picture = QLabel()
        picture.setFixedSize(PICTURE_SIZE, PICTURE_SIZE)
        pixmap = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        if not pixmap.isNull():
            side = min(pixmap.width(), pixmap.height())       # the centre square, as uploaded
            pixmap = pixmap.copy((pixmap.width() - side) // 2, (pixmap.height() - side) // 2, side, side)
            picture.setPixmap(pixmap.scaled(PICTURE_SIZE, PICTURE_SIZE, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(picture)
        column = QVBoxLayout()
        box = QCheckBox(translate('avatar_choice', 'Keep'))
        column.addWidget(box)
        info = QLabel(f'<small>{self._escape(source)}<br>{self._escape(when)}</small>')
        info.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(info)
        layout.addLayout(column)
        layout.addStretch()
        return cell, box

    def _tick(self, contact_id, side):
        """Exactly one box per row: the one clicked is kept, the other is not."""
        boxes = self.boxes[contact_id]
        boxes[side].setChecked(True)
        boxes['local' if side == 'server' else 'server'].setChecked(False)

    def _select_all(self, side):
        for contact_id in self.boxes:
            self._tick(contact_id, side)

    def reject(self):
        pass        # Confirm is the only way out

    def closeEvent(self, event):
        event.ignore()

    def choose(self):
        self.exec()
        return {contact_id: 'server' if boxes['server'].isChecked() else 'local' for contact_id, boxes in self.boxes.items()}
