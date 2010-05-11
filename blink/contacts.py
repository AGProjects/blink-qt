# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['BonjourGroup', 'BonjourNeighbour', 'Contact', 'ContactGroup', 'ContactModel', 'ContactSearchModel', 'ContactListView', 'ContactSearchListView']

import cPickle as pickle
import errno
import os

from PyQt4 import uic
from PyQt4.QtCore import Qt, QAbstractListModel, QByteArray, QEvent, QMimeData, QModelIndex, QPointF, QRectF, QSize, QStringList, QTimer
from PyQt4.QtGui  import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QStyle
from PyQt4.QtGui  import QAction, QKeyEvent, QListView, QMenu, QMouseEvent, QSortFilterProxyModel, QStyledItemDelegate

from application.python.decorator import decorator, preserve_signature
from application.python.queue import EventQueue
from application.python.util import Null
from functools import partial
from operator import attrgetter

from sipsimple.util import makedirs

from blink.resources import ApplicationData, Resources


# Functions decorated with updates_contacts_db must only be called from the GUI thread.
#
@decorator
def updates_contacts_db(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        updates_contacts_db.counter += 1
        try:
            result = func(*args, **kw)
        finally:
            updates_contacts_db.counter -= 1
        if updates_contacts_db.counter == 0:
            from blink import Blink
            blink = Blink()
            blink.main_window.contact_model.save()
        return result
    return wrapper
updates_contacts_db.counter = 0


class ContactGroup(object):
    savable = True
    movable = True
    editable = True
    deletable = True

    def __init__(self, name, collapsed=False):
        self.user_collapsed = collapsed
        self.name = name
        self.widget = Null
        self.saved_state = Null

    def __reduce__(self):
        return (self.__class__, (self.name, self.user_collapsed), None)

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.name)

    def __unicode__(self):
        return self.name

    @updates_contacts_db
    def _collapsed_changed(self, state):
        self.user_collapsed = state

    @updates_contacts_db
    def _name_changed(self):
        self.name = str(self.widget.name_editor.text())

    def _get_widget(self):
        return self.__dict__['widget']

    def _set_widget(self, widget):
        old_widget = self.__dict__.get('widget', Null)
        old_widget.collapse_button.clicked.disconnect(self._collapsed_changed)
        old_widget.name_editor.editingFinished.disconnect(self._name_changed)
        widget.collapse_button.clicked.connect(self._collapsed_changed)
        widget.name_editor.editingFinished.connect(self._name_changed)
        widget.collapse_button.setChecked(old_widget.collapse_button.isChecked() if old_widget is not Null else self.user_collapsed)
        self.__dict__['widget'] = widget

    widget = property(_get_widget, _set_widget)
    del _get_widget, _set_widget

    @property
    def collapsed(self):
        return bool(self.widget.collapse_button.isChecked())

    def collapse(self):
        self.widget.collapse_button.setChecked(True)

    def expand(self):
        self.widget.collapse_button.setChecked(False)

    def save_state(self):
        """Saves the current state of the group (collapsed or not)"""
        self.saved_state = self.widget.collapse_button.isChecked()

    def restore_state(self):
        """Restores the last saved state of the group (collapsed or not)"""
        self.widget.collapse_button.setChecked(self.saved_state)


class ContactIconDescriptor(object):
    def __init__(self, filename):
        self.filename = Resources.get(filename)
        self.icon = None
    def __get__(self, obj, objtype):
        if self.icon is None:
            pixmap = QPixmap()
            if pixmap.load(self.filename):
                self.icon = pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                self.icon = pixmap
        return self.icon
    def __set__(self, obj, value):
        raise AttributeError("attribute cannot be set")
    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


class Contact(object):
    savable = True
    movable = True
    editable = True
    deletable = True

    default_user_icon = ContactIconDescriptor('icons/default-avatar.png')

    def __init__(self, group, name, uri, image=None):
        self.group = group
        self.name = name
        self.uri = uri
        self.image = image
        self.icon = self.default_user_icon if image is None else ContactIconDescriptor(image).__get__(self, self.__class__)
        self.status = 'unknown'

    def __repr__(self):
        return '%s(%r, %r, %r, %r)' % (self.__class__.__name__, self.group, self.name, self.uri, self.image)

    def __unicode__(self):
        return u'%s <%s>' % (self.name, self.uri) if self.name else self.uri

    def __reduce__(self):
        return (self.__class__, (self.group, self.name, self.uri, self.image), None)


class BonjourGroup(ContactGroup):
    savable = True
    movable = True
    editable = True
    deletable = False

    def __init__(self, name, collapsed=False):
        super(BonjourGroup, self).__init__(name, collapsed)
        self.previous_position = None


class BonjourNeighbour(Contact):
    savable = False
    movable = False
    editable = False
    deletable = False


ui_class, base_class = uic.loadUiType(Resources.get('contact.ui'))

class ContactWidget(base_class, ui_class):
    def __init__(self, parent=None):
        super(ContactWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)

    def set_contact(self, contact):
        self.name.setText(contact.name)
        self.uri.setText(contact.uri)
        self.icon.setPixmap(contact.icon)

del ui_class, base_class


ui_class, base_class = uic.loadUiType(Resources.get('contact_group.ui'))

class ContactGroupWidget(base_class, ui_class):
    def __init__(self, name, parent=None):
        super(ContactGroupWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.name = name
        self.selected = False
        self.drop_indicator = None
        self.setFocusProxy(parent)
        self.label_widget.setFocusProxy(self)
        self.name_view.setCurrentWidget(self.label_widget)
        self.name_editor.editingFinished.connect(self._end_editing)

    @property
    def editing(self):
        return self.name_view.currentWidget() is self.editor_widget

    def _get_name(self):
        return str(self.name_label.text())

    def _set_name(self, value):
        self.name_label.setText(value)
        self.name_editor.setText(value)

    name = property(_get_name, _set_name)
    del _get_name, _set_name

    def _get_selected(self):
        return self.__dict__['selected']

    def _set_selected(self, value):
        if self.__dict__.get('selected', None) == value:
            return
        self.__dict__['selected'] = value
        self.name_label.setStyleSheet("color: #ffffff; font-weight: bold;" if value else "color: #000000;")
        #self.name_label.setForegroundRole(QPalette.BrightText if value else QPalette.WindowText)
        self.update()

    selected = property(_get_selected, _set_selected)
    del _get_selected, _set_selected

    def _get_drop_indicator(self):
        return self.__dict__['drop_indicator']

    def _set_drop_indicator(self, value):
        if self.__dict__.get('drop_indicator', Null) == value:
            return
        self.__dict__['drop_indicator'] = value
        self.update()

    drop_indicator = property(_get_drop_indicator, _set_drop_indicator)
    del _get_drop_indicator, _set_drop_indicator

    def _start_editing(self):
        #self.name_editor.setText(self.name_label.text())
        self.name_editor.selectAll()
        self.name_view.setCurrentWidget(self.editor_widget)
        self.name_editor.setFocus()

    def _end_editing(self):
        self.name_label.setText(self.name_editor.text())
        self.name_view.setCurrentWidget(self.label_widget)

    def edit(self):
        self._start_editing()

    def paintEvent(self, event):
        painter = QPainter(self)

        background = QLinearGradient(0, 0, self.width(), self.height())
        if self.selected:
            background.setColorAt(0.0, QColor('#dadada'))
            background.setColorAt(1.0, QColor('#c4c4c4'))
            foreground = QColor('#ffffff')
        else:
            background.setColorAt(0.0, QColor('#eeeeee'))
            background.setColorAt(1.0, QColor('#d8d8d8'))
            foreground = QColor('#888888')

        rect = self.rect()

        painter.fillRect(rect, QBrush(background))

        painter.setPen(QColor('#f8f8f8'))
        painter.drawLine(rect.topLeft(), rect.topRight())
        #painter.drawLine(option.rect.topLeft(), option.rect.bottomLeft())

        painter.setPen(QColor('#b8b8b8'))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        #painter.drawLine(option.rect.topRight(), option.rect.bottomRight())

        painter.setRenderHint(QPainter.Antialiasing, True)

        painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
        if self.drop_indicator is ContactListView.AboveItem:
            line_rect = QRectF(rect.adjusted(18, 0, 0, 5-rect.height()))
            arc_rect = line_rect.adjusted(-5, -3, -line_rect.width(), -3)
            path = QPainterPath(line_rect.topRight())
            path.lineTo(line_rect.topLeft())
            path.arcTo(arc_rect, 0, -180)
            painter.drawPath(path)
        elif self.drop_indicator is ContactListView.BelowItem:
            line_rect = QRectF(rect.adjusted(18, rect.height()-5, 0, 0))
            arc_rect = line_rect.adjusted(-5, 2, -line_rect.width(), 2)
            path = QPainterPath(line_rect.bottomRight())
            path.lineTo(line_rect.bottomLeft())
            path.arcTo(arc_rect, 0, 180)
            painter.drawPath(path)
        elif self.drop_indicator is ContactListView.OnItem:
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)

        if self.collapse_button.isChecked():
            arrow = QPolygonF([QPointF(0, 0), QPointF(0, 9), QPointF(8, 4.5)])
            arrow.translate(QPointF(5, 4))
        else:
            arrow = QPolygonF([QPointF(0, 0), QPointF(9, 0), QPointF(4.5, 8)])
            arrow.translate(QPointF(5, 5))
        painter.setBrush(foreground)
        painter.setPen(QPen(painter.brush(), 0, Qt.NoPen))
        painter.drawPolygon(arrow)

        painter.end()

    def event(self, event):
        if type(event) is QKeyEvent and self.editing:
            return True # do not propagate keyboard events while editing
        elif type(event) is QMouseEvent and event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
            self._start_editing()
        return super(ContactGroupWidget, self).event(event)

del ui_class, base_class


class ContactDelegate(QStyledItemDelegate):
    item_size_hints = {Contact: QSize(200, 36), ContactGroup: QSize(200, 18), BonjourNeighbour: QSize(200, 36), BonjourGroup: QSize(200, 18)}

    def __init__(self, parent=None):
        super(ContactDelegate, self).__init__(parent)

        self.oddline_widget = ContactWidget(None)
        self.evenline_widget = ContactWidget(None)
        self.selected_widget = ContactWidget(None)

        palette = self.oddline_widget.palette()
        palette.setColor(QPalette.Window, QColor("#ffffff"))
        self.oddline_widget.setPalette(palette)

        palette = self.evenline_widget.palette()
        palette.setColor(QPalette.Window, QColor("#f0f4ff"))
        self.evenline_widget.setPalette(palette)

        palette = self.selected_widget.palette()
        palette.setBrush(QPalette.Window, palette.highlight())
        palette.setBrush(QPalette.WindowText, palette.highlightedText())
        self.selected_widget.setPalette(palette)

    def _update_list_view(self, group, collapsed):
        list_view = self.parent()
        list_items = list_view.model().items
        for position in xrange(list_items.index(group)+1, len(list_items)):
            if isinstance(list_items[position], ContactGroup):
                break
            list_view.setRowHidden(position, collapsed)

    def createEditor(self, parent, options, index):
        item = index.model().data(index, Qt.DisplayRole)
        if isinstance(item, ContactGroup):
            item.widget = ContactGroupWidget(item.name, parent)
            item.widget.collapse_button.toggled.connect(partial(self._update_list_view, item))
            return item.widget
        else:
            return None

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def paintContact(self, contact, painter, option, index):
        if option.state & QStyle.State_Selected:
            widget = self.selected_widget
        elif index.row() % 2 == 1:
            widget = self.evenline_widget
        else:
            widget = self.oddline_widget

        widget.set_contact(contact)

        item_size = option.rect.size()
        if widget.size() != item_size:
            widget.resize(item_size)

        painter.save()

        pixmap = QPixmap(item_size)
        widget.render(pixmap)
        painter.drawPixmap(option.rect, pixmap)

        if contact.status not in ('offline', 'unknown'):
            status_colors = dict(online='#00ff00', away='#ffff00', busy='#ff0000')
            color = QColor(status_colors[contact.status])
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(color)
            painter.setPen(color.darker(200))
            width, border, radius = 4, 2, 2
            painter.drawRoundedRect(option.rect.topRight().x()-width-border, option.rect.y()+border, width, option.rect.height()-2*border, radius, radius)

        if 0 and (option.state & QStyle.State_MouseOver):
            painter.setRenderHint(QPainter.Antialiasing, True)
            if option.state & QStyle.State_Selected:
                painter.fillRect(option.rect, QColor(240, 244, 255, 40))
            else:
                painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                painter.fillRect(option.rect, QColor(240, 244, 255, 230))

        painter.restore()

    def paintContactGroup(self, group, painter, option, index):
        if group.widget.size() != option.rect.size():
            # For some reason updateEditorGeometry only receives the peak value
            # of the size that the widget ever had, so it will never shrink it.
            group.widget.resize(option.rect.size())
        group.widget.selected = bool(option.state & QStyle.State_Selected)

        if option.state & QStyle.State_Selected and not option.state & QStyle.State_HasFocus:
            # This condition is met when dragging is started on this group.
            # We use this to to draw the dragged item image.
            painter.save()
            pixmap = QPixmap(option.rect.size())
            group.widget.render(pixmap)
            painter.drawPixmap(option.rect, pixmap)
            painter.restore()

    paintBonjourNeighbour = paintContact
    paintBonjourGroup = paintContactGroup

    def paint(self, painter, option, index):
        item = index.model().data(index, Qt.DisplayRole)
        handler = getattr(self, 'paint%s' % item.__class__.__name__, Null)
        handler(item, painter, option, index)

    def sizeHint(self, option, index):
        return self.item_size_hints[type(index.model().data(index, Qt.DisplayRole))]


class ContactModel(QAbstractListModel):
    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-contact-group-list', 'application/x-blink-contact-list', 'text/uri-list']

    def __init__(self, parent=None):
        super(ContactModel, self).__init__(parent)
        self.items = []
        self.deleted_items = []
        self.contact_list = parent.contact_list
        if not hasattr(self, 'beginResetModel'):
            # emulate beginResetModel/endResetModel for QT < 4.6
            self.beginResetModel = Null # or use self.modelAboutToBeReset.emit (it'll be emited twice though in that case)
            self.endResetModel = self.reset
        self.save_queue = EventQueue(self.store_contacts, name='ContactsSavingThread')
        self.save_queue.start()
        self.bonjour_group = None

    @property
    def contact_groups(self):
        return [item for item in self.items if isinstance(item, ContactGroup)]

    def flags(self, index):
        if index.isValid():
            return QAbstractListModel.flags(self, index) | Qt.ItemIsDropEnabled | Qt.ItemIsDragEnabled | Qt.ItemIsEditable
        else:
            return QAbstractListModel.flags(self, index) | Qt.ItemIsDropEnabled

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        return self.items[index.row()]

    def supportedDropActions(self):
        return Qt.CopyAction | Qt.MoveAction

    def mimeTypes(self):
        return QStringList(['application/x-blink-contact-list'])

    def mimeData(self, indexes):
        mime_data = QMimeData()
        contacts = [item for item in (self.items[index.row()] for index in indexes if index.isValid()) if isinstance(item, Contact)]
        groups = [item for item in (self.items[index.row()] for index in indexes if index.isValid()) if isinstance(item, ContactGroup)]
        if contacts:
            mime_data.setData('application/x-blink-contact-list', QByteArray(pickle.dumps(contacts)))
        if groups:
            mime_data.setData('application/x-blink-contact-group-list', QByteArray(pickle.dumps(groups)))
        return mime_data

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # this is here just to keep the default Qt DnD API happy
        # the custom handler is in handleDroppedData
        return False

    def handleDroppedData(self, mime_data, action, index):
        if action == Qt.IgnoreAction:
            return True

        for mime_type in self.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                return handler(mime_data, action, index)
        else:
            return False

    @updates_contacts_db
    def _DH_ApplicationXBlinkContactGroupList(self, mime_data, action, index):
        contact_groups = self.contact_groups
        group = self.items[index.row()] if index.isValid() else contact_groups[-1]
        drop_indicator = group.widget.drop_indicator
        if group.widget.drop_indicator is None:
            return False
        selected_indexes = self.contact_list.selectionModel().selectedIndexes()
        moved_groups = set(self.items[index.row()] for index in selected_indexes if index.isValid() and self.items[index.row()].movable)
        if group is contact_groups[0] and group in moved_groups:
            drop_group = (group for group in contact_groups if group not in moved_groups).next()
            drop_position = self.contact_list.AboveItem
        elif group is contact_groups[-1] and group in moved_groups:
            drop_group = (group for group in reversed(contact_groups) if group not in moved_groups).next()
            drop_position = self.contact_list.BelowItem
        elif group in moved_groups:
            position = contact_groups.index(group)
            if drop_indicator is self.contact_list.AboveItem:
                drop_group = (group for group in reversed(contact_groups[:position]) if group not in moved_groups).next()
                drop_position = self.contact_list.BelowItem
            else:
                drop_group = (group for group in contact_groups[position:] if group not in moved_groups).next()
                drop_position = self.contact_list.AboveItem
        else:
            drop_group = group
            drop_position = drop_indicator
        items = self.popItems(selected_indexes)
        contact_groups = self.contact_groups # they changed so refresh them
        if drop_position is self.contact_list.AboveItem:
            position = self.items.index(drop_group)
        else:
            position = len(self.items) if drop_group is contact_groups[-1] else self.items.index(contact_groups[contact_groups.index(drop_group)+1])
        self.beginInsertRows(QModelIndex(), position, position+len(items)-1)
        self.items[position:position] = items
        self.endInsertRows()
        for index, item in enumerate(items):
            if isinstance(item, ContactGroup):
                self.contact_list.openPersistentEditor(self.index(position+index))
            else:
                self.contact_list.setRowHidden(position+index, item.group.collapsed)
        return True

    @updates_contacts_db
    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        group = self.items[index.row()] if index.isValid() else self.contact_groups[-1]
        if group.widget.drop_indicator is None:
            return False
        indexes = [index for index in self.contact_list.selectionModel().selectedIndexes() if self.items[index.row()].movable]
        for contact in self.popItems(indexes):
            contact.group = group
            self.addContact(contact)
        return True

    def _DH_TextUriList(self, mime_data, action, index):
        return False

    @staticmethod
    def range_iterator(indexes):
        """Return contiguous ranges from indexes"""
        start = last = None
        for index in sorted(indexes):
            if start is None:
                start = index
            elif index-last>1:
                yield (start, last)
                start = index
            last = index
        else:
            if indexes:
                yield (start, last)

    @staticmethod
    def reversed_range_iterator(indexes):
        """Return contiguous ranges from indexes starting from the end"""
        end = last = None
        for index in reversed(sorted(indexes)):
            if end is None:
                end = index
            elif last-index>1:
                yield (last, end)
                end = index
            last = index
        else:
            if indexes:
                yield (last, end)

    @updates_contacts_db
    def addContact(self, contact):
        if contact.group in self.items:
            for position in xrange(self.items.index(contact.group)+1, len(self.items)):
                item = self.items[position]
                if isinstance(item, ContactGroup) or item.name > contact.name:
                    break
            else:
                position = len(self.items)
            self.beginInsertRows(QModelIndex(), position, position)
            self.items.insert(position, contact)
            self.endInsertRows()
            self.contact_list.setRowHidden(position, contact.group.collapsed)
        else:
            position = len(self.items)
            self.beginInsertRows(QModelIndex(), position, position+1)
            self.items.append(contact.group)
            self.items.append(contact)
            self.contact_list.openPersistentEditor(self.index(position))
            self.endInsertRows()

    @updates_contacts_db
    def removeContact(self, contact):
        if contact not in self.items:
            return
        position = self.items.index(contact)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.items[position]
        self.endRemoveRows()

    @updates_contacts_db
    def addGroup(self, group):
        if group in self.items:
            return
        position = len(self.items)
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.append(group)
        self.contact_list.openPersistentEditor(self.index(position))
        self.endInsertRows()

    @updates_contacts_db
    def removeGroup(self, group):
        if group not in self.items:
            return
        start = self.items.index(group)
        end = start + len([item for item in self.items if isinstance(item, Contact) and item.group==group])
        self.beginRemoveRows(QModelIndex(), start, end)
        del self.items[start:end+1]
        self.endRemoveRows()

    @updates_contacts_db
    def moveGroup(self, group, position):
        contact_groups = self.contact_groups
        if group not in contact_groups or contact_groups.index(group) == position:
            return
        contacts_count = len([item for item in self.items if isinstance(item, Contact) and item.group==group])
        start = self.items.index(group)
        end = start + contacts_count
        self.beginRemoveRows(QModelIndex(), start, end)
        items = self.items[start:end+1]
        del self.items[start:end+1]
        self.endRemoveRows()
        if position >= len(contact_groups)-1:
            self.beginInsertRows(QModelIndex(), len(self.items), len(self.items)+contacts_count)
            self.items.extend(items)
            self.endInsertRows()
            self.contact_list.openPersistentEditor(self.index(len(self.items)-contacts_count-1))
        else:
            if contact_groups.index(group) < position:
                position += 1
            start = self.items.index(contact_groups[position])
            end = start + contacts_count
            self.beginInsertRows(QModelIndex(), start, end)
            self.items[start:start] = items
            self.endInsertRows()
            self.contact_list.openPersistentEditor(self.index(start))

    @updates_contacts_db
    def removeItems(self, indexes):
        rows = set(index.row() for index in indexes if index.isValid())
        removed_groups = set(self.items[row] for row in rows if isinstance(self.items[row], ContactGroup))
        rows.update(row for row, item in enumerate(self.items) if isinstance(item, Contact) and item.group in removed_groups)
        for start, end in self.reversed_range_iterator(rows):
            self.beginRemoveRows(QModelIndex(), start, end)
            deleted_items = self.items[start:end+1]
            for item in (item for item in deleted_items if isinstance(item, ContactGroup)):
                item.widget = Null
            self.deleted_items.append(deleted_items)
            del self.items[start:end+1]
            self.endRemoveRows()

    @updates_contacts_db
    def popItems(self, indexes):
        items = []
        rows = set(index.row() for index in indexes if index.isValid())
        removed_groups = set(self.items[row] for row in rows if isinstance(self.items[row], ContactGroup))
        rows.update(row for row, item in enumerate(self.items) if isinstance(item, Contact) and item.group in removed_groups)
        for start, end in self.reversed_range_iterator(rows):
            self.beginRemoveRows(QModelIndex(), start, end)
            items[0:0] = self.items[start:end+1]
            del self.items[start:end+1]
            self.endRemoveRows()
        return items

    def load(self):
        try:
            try:
                file = open(ApplicationData.get('contacts'))
                items = pickle.load(file)
            except Exception:
                # remove the corrupted contacts file, so it won't be backed up to contacts.bak later
                try:
                    os.unlink(ApplicationData.get('contacts'))
                except Exception:
                    pass
                file = open(ApplicationData.get('contacts.bak'))
                items = pickle.load(file)
                file = None # restore contacts from contacts.bak
        except Exception:
            file = None
            group = ContactGroup('Test')
            contacts = [Contact(group, 'Play James Bond Theme', '3333@sip2sip.info', 'icons/3333@sip2sip.info.png'),
                        Contact(group, 'Test Your Microphone', '4444@sip2sip.info', 'icons/4444@sip2sip.info.png'),
                        Contact(group, 'Test Multi-Party Chatserver', '123@chatserver.ag-projects.com'),
                        Contact(group, 'VUC Conference http://vuc.me', '200901@login.zipdx.com', 'icons/200901@login.zipdx.com.png')]
            contacts.sort(key=attrgetter('name'))
            items = [group] + contacts
        self.beginResetModel()
        self.items = items
        self.endResetModel()
        for position, item in enumerate(self.items):
            if isinstance(item, ContactGroup):
                self.contact_list.openPersistentEditor(self.index(position))
            else:
                self.contact_list.setRowHidden(position, item.group.collapsed)
            if type(item) is BonjourGroup:
                self.bonjour_group = item
        if self.bonjour_group is None:
            self.bonjour_group = BonjourGroup('Bonjour Neighbours')
        if file is None:
            self.save()

    def save(self):
        items = [item for item in self.items if item.savable]
        contact_groups = self.contact_groups
        if self.bonjour_group in contact_groups and self.bonjour_group.previous_position != contact_groups.index(self.bonjour_group) and self.bonjour_group.previous_position is not None:
            items.remove(self.bonjour_group)
            if self.bonjour_group.previous_position >= len(contact_groups)-1:
                items.append(self.bonjour_group)
            else:
                position = self.bonjour_group.previous_position
                if contact_groups.index(self.bonjour_group) < position:
                    position += 1
                items.insert(items.index(contact_groups[position]), self.bonjour_group)
        self.save_queue.put(pickle.dumps(items))

    def store_contacts(self, data):
        makedirs(ApplicationData.directory)
        filename = ApplicationData.get('contacts')
        tmp_filename = ApplicationData.get('contacts.tmp')
        bak_filename = ApplicationData.get('contacts.bak')
        file = open(tmp_filename, 'wb')
        file.write(data)
        file.close()
        try:
            os.rename(filename, bak_filename)
        except OSError, e:
            if e.errno != errno.ENOENT:
                raise
        os.rename(tmp_filename, filename)


class ContactSearchModel(QSortFilterProxyModel):
    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['text/uri-list']

    def __init__(self, model, parent=None):
        super(ContactSearchModel, self).__init__(parent)
        self.setSourceModel(model)
        self.setDynamicSortFilter(True)
        self.sort(0)

    def flags(self, index):
        if index.isValid():
            return QSortFilterProxyModel.flags(self, index) | Qt.ItemIsDropEnabled | Qt.ItemIsDragEnabled
        else:
            return QSortFilterProxyModel.flags(self, index) | Qt.ItemIsDropEnabled

    def data(self, index, role=Qt.DisplayRole):
        data = super(ContactSearchModel, self).data(index, role)
        return data.toPyObject() if role==Qt.DisplayRole else data

    def supportedDropActions(self):
        return Qt.CopyAction

    def mimeTypes(self):
        return QStringList(['application/x-blink-contact-list'])

    def mimeData(self, indexes):
        mime_data = QMimeData()
        contacts = [self.data(index) for index in indexes if index.isValid()]
        if contacts:
            mime_data.setData('application/x-blink-contact-list', QByteArray(pickle.dumps(contacts)))
        return mime_data

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # this is here just to keep the default Qt DnD API happy
        # the custom handler is in handleDroppedData
        return False

    def handleDroppedData(self, mime_data, action, index):
        if action == Qt.IgnoreAction:
            return True

        for mime_type in self.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                return handler(mime_data, action, index)
        else:
            return False

    def _DH_TextUriList(self, mime_data, action, index):
        return False

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        source_index = source_model.index(source_row, 0, source_parent)
        item = source_model.data(source_index, Qt.DisplayRole)
        if isinstance(item, ContactGroup):
            return False
        search_tokens = unicode(self.filterRegExp().pattern()).lower().split()
        searched_item = unicode(item).lower()
        return all(token in searched_item for token in search_tokens)

    def lessThan(self, left_index, right_index):
        left_item = left_index.model().data(left_index, Qt.DisplayRole)
        right_item = right_index.model().data(right_index, Qt.DisplayRole)
        return left_item.name < right_item.name


class ContextMenuActions(object):
    pass


class ContactListView(QListView):
    def __init__(self, parent=None):
        super(ContactListView, self).__init__(parent)
        self.setItemDelegate(ContactDelegate(self))
        self.setDropIndicatorShown(False)
        self.drop_indicator_index = QModelIndex()
        self.actions = ContextMenuActions()
        self.actions.add_group = QAction("Add Group", self, triggered=self._AH_AddGroup)
        self.actions.add_contact = QAction("Add Contact", self, triggered=self._AH_AddContact)
        self.actions.edit_item = QAction("Edit", self, triggered=self._AH_EditItem)
        self.actions.delete_item = QAction("Delete", self, triggered=self._AH_DeleteSelection)
        self.actions.delete_selection = QAction("Delete Selection", self, triggered=self._AH_DeleteSelection)
        self.actions.undo_last_delete = QAction("Undo Last Delete", self, triggered=self._AH_UndoLastDelete)
        self.actions.start_audio_session = QAction("Start Audio Session", self, triggered=self._AH_StartAudioSession)
        self.actions.start_chat_session = QAction("Start Chat Session", self, triggered=self._AH_StartChatSession)
        self.actions.send_sms = QAction("Send SMS", self, triggered=self._AH_SendSMS)
        self.actions.send_files = QAction("Send File(s)...", self, triggered=self._AH_SendFiles)
        self.actions.request_remote_desktop = QAction("Request Remote Desktop", self, triggered=self._AH_RequestRemoteDesktop)
        self.actions.share_my_desktop = QAction("Share My Desktop", self, triggered=self._AH_ShareMyDesktop)
        self.restore_timer = QTimer(self)
        self.restore_timer.setSingleShot(True)
        self.restore_timer.setInterval(1250)
        self.restore_timer.timeout.connect(self._restore_groups)
        self.needs_restore = False

    def _restore_groups(self):
        for group in self.model().contact_groups:
            group.restore_state()
        self.needs_restore = False

    def paintEvent(self, event):
        super(ContactListView, self).paintEvent(event)
        if self.drop_indicator_index.isValid():
            rect = self.visualRect(self.drop_indicator_index)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()
        model = self.model()
        contact_groups = model.contact_groups
        if contact_groups and contact_groups[-1].widget.drop_indicator is self.BelowItem:
            # draw the bottom part of the drop indicator for the last group
            last_group = contact_groups[-1]
            rect = self.visualRect(model.index(model.items.index(last_group)))
            line_rect = QRectF(rect.adjusted(18, rect.height(), 0, 5))
            arc_rect = line_rect.adjusted(-5, -3, -line_rect.width(), -3)
            path = QPainterPath(line_rect.topRight())
            path.lineTo(line_rect.topLeft())
            path.arcTo(arc_rect, 0, -180)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawPath(path)
            painter.end()

    def contextMenuEvent(self, event):
        model = self.model()
        selected_items = [model.data(index) for index in self.selectionModel().selectedIndexes()]
        menu = QMenu(self)
        if not selected_items:
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        elif len(selected_items) > 1:
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            menu.addAction(self.actions.delete_selection)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.delete_selection.setEnabled(any(item.deletable for item in selected_items))
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        elif isinstance(selected_items[0], ContactGroup):
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            menu.addAction(self.actions.edit_item)
            menu.addAction(self.actions.delete_item)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.edit_item.setEnabled(selected_items[0].editable)
            self.actions.delete_item.setEnabled(selected_items[0].deletable)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        else:
            contact = selected_items[0]
            menu.addAction(self.actions.start_audio_session)
            menu.addAction(self.actions.start_chat_session)
            menu.addAction(self.actions.send_sms)
            menu.addSeparator()
            menu.addAction(self.actions.send_files)
            menu.addSeparator()
            self.actions.request_remote_desktop.setText("Request Desktop from %s" % (contact.name or contact.uri))
            self.actions.share_my_desktop.setText("Share My Desktop with %s" % (contact.name or contact.uri))
            menu.addAction(self.actions.request_remote_desktop)
            menu.addAction(self.actions.share_my_desktop)
            menu.addSeparator()
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            menu.addAction(self.actions.edit_item)
            menu.addAction(self.actions.delete_item)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.edit_item.setEnabled(contact.editable)
            self.actions.delete_item.setEnabled(contact.deletable)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        menu.exec_(event.globalPos())

    def dragEnterEvent(self, event):
        event_source = event.source()
        accepted_mime_types = set(self.model().accepted_mime_types)
        provided_mime_types = set(str(x) for x in event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        has_blink_contacts = 'application/x-blink-contact-list' in provided_mime_types
        has_blink_groups = 'application/x-blink-contact-group-list' in provided_mime_types
        if not acceptable_mime_types:
            event.ignore() # no acceptable mime types found
        elif has_blink_contacts and has_blink_groups:
            event.ignore() # we can't handle drops for both groups and contacts at the same time
        elif event_source is not self and (has_blink_contacts or has_blink_groups):
            event.ignore() # we don't handle drops for blink contacts or groups from other sources
        else:
            if event_source is self:
                event.setDropAction(Qt.MoveAction)
            if has_blink_contacts or has_blink_groups:
                if not self.needs_restore:
                    for group in self.model().contact_groups:
                        group.save_state()
                        group.collapse()
                    self.needs_restore = True
                self.restore_timer.stop()
            event.accept()
            self.setState(self.DraggingState)

    def dragLeaveEvent(self, event):
        super(ContactListView, self).dragLeaveEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()
        for group in self.model().contact_groups:
            group.widget.drop_indicator = None
        if self.needs_restore:
            self.restore_timer.start()

    def dragMoveEvent(self, event):
        super(ContactListView, self).dragMoveEvent(event)
        if event.source() is self:
            event.setDropAction(Qt.MoveAction)

        for mime_type in self.model().accepted_mime_types:
            if event.provides(mime_type):
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                self.drop_indicator_index = QModelIndex()
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                item = self.model().data(index)
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                handler(event, index, rect, item)
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if event.source() is self:
            event.setDropAction(Qt.MoveAction)
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        for group in model.contact_groups:
            group.widget.drop_indicator = None
            if self.needs_restore:
                group.restore_state()
        self.needs_restore = False
        super(ContactListView, self).dropEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def _AH_AddGroup(self):
        group = ContactGroup("")
        self.model().addGroup(group)
        self.scrollToBottom()
        group.widget.edit()

    def _AH_AddContact(self):
        model = self.model()
        selected_rows = sorted(index.row() for index in self.selectionModel().selectedIndexes() if type(model.data(index)) in (Contact, ContactGroup))
        if selected_rows:
            item = model.items[selected_rows[0]]
            preferred_group = item if type(item) is ContactGroup else item.group
        else:
            try:
                preferred_group = (group for group in model.contact_groups if type(group) is ContactGroup).next()
            except StopIteration:
                preferred_group = None

    def _AH_EditItem(self):
        index = self.selectionModel().selectedIndexes()[0]
        item = self.model().data(index)
        if isinstance(item, ContactGroup):
            self.scrollTo(index)
            item.widget.edit()

    def _AH_DeleteSelection(self):
        model = self.model()
        indexes = [index for index in self.selectionModel().selectedIndexes() if model.items[index.row()].deletable]
        model.removeItems(indexes)
        self.selectionModel().clearSelection()

    def _AH_UndoLastDelete(self):
        model = self.model()
        for item in model.deleted_items.pop():
            handler = model.addGroup if isinstance(item, ContactGroup) else model.addContact
            handler(item)

    def _AH_StartAudioSession(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_StartChatSession(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_SendSMS(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_SendFiles(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_RequestRemoteDesktop(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_ShareMyDesktop(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _DH_ApplicationXBlinkContactGroupList(self, event, index, rect, item):
        model = self.model()
        groups = model.contact_groups
        for group in groups:
            group.widget.drop_indicator = None
        if not index.isValid():
            drop_groups = (groups[-1], Null)
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(model.items.index(groups[-1]))).bottom())
        elif isinstance(item, ContactGroup):
            index = groups.index(item)
            rect.setHeight(rect.height()/2)
            if rect.contains(event.pos()):
                drop_groups = (groups[index-1], groups[index]) if index>0 else (Null, groups[index])
            else:
                drop_groups = (groups[index], groups[index+1]) if index<len(groups)-1 else (groups[index], Null)
                rect.translate(0, rect.height())
        selected_rows = sorted(index.row() for index in self.selectionModel().selectedIndexes() if model.items[index.row()].movable)
        if selected_rows:
            first = groups.index(model.items[selected_rows[0]])
            last = groups.index(model.items[selected_rows[-1]])
            contiguous_selection = len(selected_rows) == last-first+1
        else:
            contiguous_selection = False
        selected_groups = set(model.items[row] for row in selected_rows)
        overlapping_groups = len(selected_groups.intersection(drop_groups))
        allowed_overlapping = 0 if contiguous_selection else 1
        if event.source() is not self or overlapping_groups <= allowed_overlapping:
            drop_groups[0].widget.drop_indicator = self.BelowItem
            drop_groups[1].widget.drop_indicator = self.AboveItem
        if groups[-1] in drop_groups:
            self.viewport().update()
        event.accept(rect)

    def _DH_ApplicationXBlinkContactList(self, event, index, rect, item):
        model = self.model()
        groups = model.contact_groups
        for group in groups:
            group.widget.drop_indicator = None
        if not any(model.items[index.row()].movable for index in self.selectionModel().selectedIndexes()):
            event.accept(rect)
            return
        if not index.isValid():
            group = groups[-1]
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(model.items.index(group))).bottom())
        elif isinstance(item, ContactGroup):
            group = item
        selected_contact_groups = set(model.items[index.row()].group for index in self.selectionModel().selectedIndexes() if model.items[index.row()].movable)
        if type(group) is ContactGroup and (event.source() is not self or len(selected_contact_groups) > 1 or group not in selected_contact_groups):
            group.widget.drop_indicator = self.OnItem
        event.accept(rect)

    def _DH_TextUriList(self, event, index, rect, item):
        model = self.model()
        if not index.isValid():
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.items)-1)).bottom())
        if isinstance(item, Contact):
            event.accept(rect)
            self.drop_indicator_index = index
        else:
            event.ignore(rect)


class ContactSearchListView(QListView):
    def __init__(self, parent=None):
        super(ContactSearchListView, self).__init__(parent)
        self.setItemDelegate(ContactDelegate(self))
        self.setDropIndicatorShown(False)
        self.drop_indicator_index = QModelIndex()
        self.actions = ContextMenuActions()
        self.actions.edit_item = QAction("Edit", self, triggered=self._AH_EditItem)
        self.actions.delete_item = QAction("Delete", self, triggered=self._AH_DeleteSelection)
        self.actions.delete_selection = QAction("Delete Selection", self, triggered=self._AH_DeleteSelection)
        self.actions.undo_last_delete = QAction("Undo Last Delete", self, triggered=self._AH_UndoLastDelete)
        self.actions.start_audio_session = QAction("Start Audio Session", self, triggered=self._AH_StartAudioSession)
        self.actions.start_chat_session = QAction("Start Chat Session", self, triggered=self._AH_StartChatSession)
        self.actions.send_sms = QAction("Send SMS", self, triggered=self._AH_SendSMS)
        self.actions.send_files = QAction("Send File(s)...", self, triggered=self._AH_SendFiles)
        self.actions.request_remote_desktop = QAction("Request Remote Desktop", self, triggered=self._AH_RequestRemoteDesktop)
        self.actions.share_my_desktop = QAction("Share My Desktop", self, triggered=self._AH_ShareMyDesktop)

    def paintEvent(self, event):
        super(ContactSearchListView, self).paintEvent(event)
        if self.drop_indicator_index.isValid():
            rect = self.visualRect(self.drop_indicator_index)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()

    def contextMenuEvent(self, event):
        model = self.model()
        source_model = model.sourceModel()
        selected_items = [model.data(index) for index in self.selectionModel().selectedIndexes()]
        menu = QMenu(self)
        if not selected_items:
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setEnabled(len(source_model.deleted_items) > 0)
        elif len(selected_items) > 1:
            menu.addAction(self.actions.delete_selection)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.delete_selection.setEnabled(any(item.deletable for item in selected_items))
            self.actions.undo_last_delete.setEnabled(len(source_model.deleted_items) > 0)
        else:
            contact = selected_items[0]
            menu.addAction(self.actions.start_audio_session)
            menu.addAction(self.actions.start_chat_session)
            menu.addAction(self.actions.send_sms)
            menu.addSeparator()
            menu.addAction(self.actions.send_files)
            menu.addSeparator()
            self.actions.request_remote_desktop.setText("Request Desktop from %s" % (contact.name or contact.uri))
            self.actions.share_my_desktop.setText("Share My Desktop with %s" % (contact.name or contact.uri))
            menu.addAction(self.actions.request_remote_desktop)
            menu.addAction(self.actions.share_my_desktop)
            menu.addSeparator()
            menu.addAction(self.actions.edit_item)
            menu.addAction(self.actions.delete_item)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.edit_item.setEnabled(contact.editable)
            self.actions.delete_item.setEnabled(contact.deletable)
            self.actions.undo_last_delete.setEnabled(len(source_model.deleted_items) > 0)
        menu.exec_(event.globalPos())

    def dragEnterEvent(self, event):
        accepted_mime_types = set(self.model().accepted_mime_types)
        provided_mime_types = set(str(x) for x in event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if event.source() is self or not acceptable_mime_types:
            event.ignore()
        else:
            event.accept()
            self.setState(self.DraggingState)

    def dragLeaveEvent(self, event):
        super(ContactSearchListView, self).dragLeaveEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def dragMoveEvent(self, event):
        super(ContactSearchListView, self).dragMoveEvent(event)

        for mime_type in self.model().accepted_mime_types:
            if event.provides(mime_type):
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                self.drop_indicator_index = QModelIndex()
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                item = self.model().data(index)
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                handler(event, index, rect, item)
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                break
        else:
            event.ignore()

    def dropEvent(self, event):
        model = self.model()
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        super(ContactSearchListView, self).dropEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def _AH_EditItem(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_DeleteSelection(self):
        model = self.model()
        model.sourceModel().removeItems(model.mapToSource(index) for index in self.selectionModel().selectedIndexes() if model.data(index).deletable)

    def _AH_UndoLastDelete(self):
        model = self.model().sourceModel()
        for item in model.deleted_items.pop():
            handler = model.addGroup if isinstance(item, ContactGroup) else model.addContact
            handler(item)

    def _AH_StartAudioSession(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_StartChatSession(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_SendSMS(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_SendFiles(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_RequestRemoteDesktop(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _AH_ShareMyDesktop(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])

    def _DH_TextUriList(self, event, index, rect, item):
        if index.isValid():
            event.accept(rect)
            self.drop_indicator_index = index
        else:
            model = self.model()
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(model.rowCount()-1, 0)).bottom())
            event.ignore(rect)


