# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['Contact', 'ContactGroup', 'ContactDelegate', 'ContactModel', 'ContactSearchModel']

import sys

from PyQt4.QtCore import Qt, QAbstractListModel, QModelIndex, QSize
from PyQt4.QtGui  import QColor, QPainter, QPalette, QPixmap, QStyle, QSortFilterProxyModel, QStyledItemDelegate

from application.python.util import Null
from functools import partial
from weakref import WeakValueDictionary

from blink.resources import Resources
from blink.ui import ContactWidget, ContactGroupWidget


class ContactGroup(object):
    instances = WeakValueDictionary()

    def __new__(cls, name):
        obj = cls.instances.get(name, None)
        if obj is None:
            obj = object.__new__(cls)
            obj.name = name
            obj.widget = Null
            cls.instances[name] = obj
        return obj

    def __reduce__(self):
        return (self.__class__, (self.name,), None)

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.name)

    def __str__(self):
        return self.name.encode(sys.stdout.encoding)

    def __unicode__(self):
        return self.name

    @property
    def collapsed(self):
        return self.widget.collapsed

    def _get_name(self):
        return self.__dict__['name']

    def _set_name(self, name):
        old_name = self.__dict__.get('name')
        if name == old_name:
            return
        if old_name is not None:
            del self.instances[old_name]
        self.__dict__['name'] = name
        self.instances[name] = self

    name = property(_get_name, _set_name)
    del _get_name, _set_name


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
    default_user_icon = ContactIconDescriptor('icons/default_user_icon.png')

    def __init__(self, group, name, uri, image=None):
        self.group = group
        self.name = name
        self.uri = uri
        self.image = image
        self.icon = self.default_user_icon if image is None else ContactIconDescriptor(image).__get__(self, self.__class__)
        self.status = 'unknown'

    def __repr__(self):
        return '%s(%r, %r, %r, %r)' % (self.__class__.__name__, self.group, self.name, self.uri, self.image)

    def __str__(self):
        return unicode(self).encode(sys.stdout.encoding)

    def __unicode__(self):
        return u'%s <%s>' % (self.name, self.uri) if self.name else self.uri

    def __reduce__(self):
        return (self.__class__, (self.group, self.name, self.uri, self.image), None)


class ContactDelegate(QStyledItemDelegate):
    item_size_hints = {Contact: QSize(200, 36), ContactGroup: QSize(200, 18)}

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
            if type(list_items[position]) is ContactGroup:
                break
            list_view.setRowHidden(position, collapsed)

    def createEditor(self, parent, options, index):
        item = index.model().data(index, Qt.DisplayRole)
        if type(item) is ContactGroup:
            if item.widget is Null:
                item.widget = ContactGroupWidget(item.name, parent)
                item.widget.arrow.toggled.connect(partial(self._update_list_view, item))
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
        item = index.model().data(index, Qt.DisplayRole)
        if item.widget.size() != option.rect.size():
            # For some reason updateEditorGeometry only receives the peak value of
            # the size that the widget ever had, so it will never shrink it. -Dan
            item.widget.resize(option.rect.size())
        item.widget.selected = bool(option.state & QStyle.State_Selected)

    def paint(self, painter, option, index):
        item = index.model().data(index, Qt.DisplayRole)
        handler = getattr(self, 'paint%s' % item.__class__.__name__, Null)
        handler(item, painter, option, index)

    def sizeHint(self, option, index):
        return self.item_size_hints[type(index.model().data(index, Qt.DisplayRole))]


class ContactModel(QAbstractListModel):
    def __init__(self, parent=None):
        super(ContactModel, self).__init__(parent)
        self.items = []
        self.contact_list = parent.contact_list

    @property
    def contact_groups(self):
        return [item for item in self.items if type(item) is ContactGroup]

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsEnabled
        return Qt.ItemFlags(QAbstractListModel.flags(self, index) | Qt.ItemIsEditable)

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        return self.items[index.row()]

    def addContact(self, contact):
        if contact.group in self.items:
            for position in xrange(self.items.index(contact.group)+1, len(self.items)):
                item = self.items[position]
                if type(item) is ContactGroup or item.name > contact.name:
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

    def deleteContacts(self, indexes):
        rows = sorted(index.row() for index in indexes if index.isValid())
        self.beginRemoveRows(QModelIndex(), rows[0], rows[-1])
        for row in reversed(rows):
            self.items.pop(row)
        self.endRemoveRows()

    def addGroup(self, group):
        if group in self.items:
            return
        position = len(self.items)
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.append(group)
        self.contact_list.openPersistentEditor(self.index(position))
        self.endInsertRows()

    def test(self):
        work_group = ContactGroup('Work')
        test_group = ContactGroup('Test')
        for contact in [Contact(work_group, 'Dan Pascu', '31208005167@ag-projects.com', 'icons/avatar.png'), Contact(work_group, 'Lucian Stanescu', '31208005164@ag-projects.com'), Contact(work_group, 'Test number', '3333@ag-projects.com')]:
            if contact.uri.startswith('3333@') or contact.uri.startswith('31208005167@'):
                contact.status = 'online'
            else:
                contact.status = 'busy'
            self.addContact(contact)
        self.addGroup(test_group)


class ContactSearchModel(QSortFilterProxyModel):
    def __init__(self, model, parent=None):
        super(ContactSearchModel, self).__init__(parent)
        self.setSourceModel(model)
        self.setDynamicSortFilter(True)
        self.sort(0)

    def data(self, index, role=Qt.DisplayRole):
        data = super(ContactSearchModel, self).data(index, role)
        return data.toPyObject() if role==Qt.DisplayRole else data

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        source_index = source_model.index(source_row, 0, source_parent)
        item = source_model.data(source_index, Qt.DisplayRole)
        if type(item) is ContactGroup:
            return False
        search_tokens = unicode(self.filterRegExp().pattern()).lower().split()
        searched_item = unicode(item).lower()
        return all(token in searched_item for token in search_tokens)

    def lessThan(self, left_index, right_index):
        left_item = left_index.model().data(left_index, Qt.DisplayRole)
        right_item = right_index.model().data(right_index, Qt.DisplayRole)
        return left_item.name < right_item.name


