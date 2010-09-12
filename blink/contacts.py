# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['BonjourGroup', 'BonjourNeighbour', 'Contact', 'ContactGroup', 'ContactModel', 'ContactSearchModel', 'ContactListView', 'ContactSearchListView', 'ContactEditorDialog', 'GoogleContactsDialog']

import cPickle as pickle
import errno
import os
import re
import socket
import sys

from PyQt4 import uic
from PyQt4.QtCore import Qt, QAbstractListModel, QByteArray, QEvent, QMimeData, QModelIndex, QPointF, QRectF, QRegExp, QSize, QStringList, pyqtSignal
from PyQt4.QtGui  import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QStyle
from PyQt4.QtGui  import QAction, QKeyEvent, QListView, QMenu, QMouseEvent, QRegExpValidator, QSortFilterProxyModel, QStyledItemDelegate

from application.notification import IObserver, NotificationCenter
from application.python.decorator import decorator, preserve_signature
from application.python.util import Null
from application.system import unlink
from collections import deque
from eventlet import api
from eventlet.green import httplib, urllib2
from functools import partial
from operator import attrgetter
from twisted.internet import reactor
from twisted.internet.error import ConnectionLost
from zope.interface import implements

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.util import makedirs, run_in_green_thread, run_in_twisted_thread

from blink.configuration.datatypes import AuthorizationToken, InvalidToken
from blink.resources import ApplicationData, Resources, IconCache
from blink.sessions import SessionManager
from blink.util import QSingleton, call_in_gui_thread, call_later, run_in_auxiliary_thread, run_in_gui_thread
from blink.widgets.buttons import SwitchViewButton
from blink.widgets.labels import Status

from blink.google.gdata.client import CaptchaChallenge, RequestError, Unauthorized
from blink.google.gdata.contacts.client import ContactsClient
from blink.google.gdata.contacts.data import ContactsFeed
from blink.google.gdata.contacts.service import ContactsQuery
from blink.google.gdata.gauth import ClientLoginToken


# Functions decorated with updates_contacts_db or ignore_contacts_db_updates must
# only be called from the GUI thread.
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

@decorator
def ignore_contacts_db_updates(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        updates_contacts_db.counter += 1
        try:
            return func(*args, **kw)
        finally:
            updates_contacts_db.counter -= 1
    return wrapper


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
        self.name = unicode(self.widget.name_editor.text())

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
        self.filename = filename
        self.icon = None
    def __get__(self, obj, objtype):
        if self.icon is None:
            pixmap = QPixmap()
            if pixmap.load(ApplicationData.get(self.filename)):
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

    default_user_icon = ContactIconDescriptor(Resources.get('icons/default-avatar.png'))

    def __init__(self, group, name, uri, image=None):
        self.group = group
        self.name = name
        self.uri = uri
        self.image = image
        self.sip_aliases = []
        self.preferred_media = 'Audio'
        self.status = 'unknown'

    def __repr__(self):
        return '%s(%r, %r, %r, %r)' % (self.__class__.__name__, self.group, self.name, self.uri, self.image)

    def __unicode__(self):
        return u'%s <%s>' % (self.name, self.uri) if self.name else self.uri

    def __reduce__(self):
        return (self.__class__, (self.group, self.name, self.uri, self.image), dict(preferred_media=self.preferred_media, sip_aliases=self.sip_aliases))

    def _get_image(self):
        return self.__dict__['image']

    def _set_image(self, image):
        self.__dict__['image'] = image
        self.__dict__['icon'] = self.default_user_icon if image is None else ContactIconDescriptor(image).__get__(self, self.__class__)

    image = property(_get_image, _set_image)
    del _get_image, _set_image

    @property
    def icon(self):
        return self.__dict__['icon']

    @property
    def name_detail(self):
        return self.name

    @property
    def uri_detail(self):
        return self.uri


class NoGroup(object):
    pass

class BonjourGroup(ContactGroup):
    savable = True
    movable = True
    editable = True
    deletable = False

    def __init__(self, name, collapsed=False):
        super(BonjourGroup, self).__init__(name, collapsed)
        self.reference_group = NoGroup


class BonjourNeighbour(Contact):
    savable = False
    movable = False
    editable = False
    deletable = False

    def __init__(self, group, name, hostname, uri, image=None):
        super(BonjourNeighbour, self).__init__(group, name, uri, image)
        self.hostname = hostname

    @property
    def name_detail(self):
        return "%s (%s)" % (self.name, self.hostname)


class GoogleContactsGroup(ContactGroup):
    savable = True
    movable = True
    editable = True
    deletable = False

    def __init__(self, name, collapsed=False):
        super(GoogleContactsGroup, self).__init__(name, collapsed)
        self.id = None
        self.update_timestamp = None

    def __reduce__(self):
        return (self.__class__, (self.name, self.user_collapsed), dict(id=self.id, update_timestamp=self.update_timestamp))


class GoogleContact(Contact):
    savable = True
    movable = False
    editable = False
    deletable = False

    def __init__(self, id, group, name, uri, company=None, uri_type=None, image=None, image_etag=None):
        super(GoogleContact, self).__init__(group, name, uri, image)
        self.id = id
        self.company = company
        self.uri_type = uri_type
        self.image_etag = image_etag

    def __reduce__(self):
        return (self.__class__, (self.id, self.group, self.name, self.uri, self.company, self.uri_type, self.image, self.image_etag), dict(preferred_media=self.preferred_media, sip_aliases=self.sip_aliases))

    def __unicode__(self):
        return u'%s <%s>' % (self.name_detail, self.uri_detail)

    @property
    def name_detail(self):
        if self.company:
            return '%s (%s)' % (self.name, self.company) if self.name else self.company
        else:
            return self.name or self.uri

    @property
    def uri_detail(self):
        return "%s (%s)" % (self.uri, self.uri_type) if self.uri_type else self.uri


class GoogleContactsManager(object):
    implements(IObserver)

    def __init__(self, model):
        self.client = ContactsClient()
        self.contact_model = model
        self.entries_map = dict()
        self.greenlet = None
        self.stop_adding_contacts = False
        self._load_timer = None

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, name='SIPApplicationWillStart')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')

    @property
    def group(self):
        return self.contact_model.google_contacts_group

    def initialize(self):
        self.entries_map.clear()
        for contact in (item for item in self.contact_model.items if type(item) is GoogleContact):
            self.entries_map.setdefault(contact.id, []).append(contact)

    @staticmethod
    def normalize_uri_label(label):
        try:
            label = label.lower()
            label = label.split('#')[1]
            label = re.sub('_', ' ', label)
        except AttributeError:
            label = ''
        except IndexError:
            label = re.sub('\/', '', label)
        finally:
            label = re.sub('generic', '', label)
            return label.strip()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        settings = SIPSimpleSettings()
        authorization_token = settings.google_contacts.authorization_token
        if authorization_token:
            call_in_gui_thread(self.contact_model.addGroup, self.contact_model.google_contacts_group)
            self.load_contacts()
        elif authorization_token is None:
            self.remove_group()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'google_contacts.authorization_token' in notification.data.modified:
            authorization_token = notification.sender.google_contacts.authorization_token
            if self._load_timer is not None and self._load_timer.active():
                self._load_timer.cancel()
            self._load_timer = None
            if authorization_token:
                call_in_gui_thread(self.contact_model.addGroup, self.contact_model.google_contacts_group)
                self.stop_adding_contacts = False
                self.load_contacts()
            elif authorization_token is None:
                if self._load_timer is not None and self._load_timer.active():
                    self._load_timer.cancel()
                self._load_timer = None
                if self.greenlet is not None:
                    api.kill(self.greenlet, api.GreenletExit())
                    self.greenlet = None
                self.stop_adding_contacts = False
                self.remove_group()

    def _NH_SIPApplicationWillEnd(self, notification):
        if self.greenlet is not None:
            api.kill(self.greenlet, api.GreenletExit())

    @run_in_green_thread
    def load_contacts(self):
        if self.greenlet is not None:
            api.kill(self.greenlet, api.GreenletExit())
        self.greenlet = api.getcurrent()
        if self._load_timer is not None and self._load_timer.active():
            self._load_timer.cancel()
        self._load_timer = None

        settings = SIPSimpleSettings()
        self.client.auth_token = ClientLoginToken(settings.google_contacts.authorization_token.token)

        try:
            if self.group.id is None:
                self.group.id = (entry.id.text for entry in self.client.get_groups().entry if entry.title.text=='System Group: My Contacts').next()

            query_params = dict(showdeleted='true')
            query = ContactsQuery(feed=self.client.get_feed_uri(kind='contacts'), group=self.group.id, params=query_params)
            previous_update = self.contact_model.google_contacts_group.update_timestamp
            if previous_update:
                query.updated_min = previous_update
            feed = self.client.get_feed(query.ToUri(), desired_class=ContactsFeed)
            update_timestamp = feed.updated.text if feed else None

            while feed:
                updated_contacts = []
                deleted_contacts = set(entry.id.text for entry in feed.entry if getattr(entry, 'deleted', False))
                self.remove_contacts(deleted_contacts)
                for entry in (entry for entry in feed.entry if entry.id.text not in deleted_contacts):
                    name =  (getattr(entry, 'title', None) or Null).text or None
                    company = ((getattr(entry, 'organization', None) or Null).name or Null).text or None
                    numbers = set((re.sub(r'^00', '+', number.text), number.label or number.rel) for number in getattr(entry, 'phone_number', ()))
                    numbers.update(set((re.sub('^(sip:|sips:)', '', email.address), email.label or email.rel) for email in getattr(entry, 'email', ()) if re.search('^(sip:|sips:)', email.address)))
                    numbers.update(set((re.sub('^(sip:|sips:)', '', web.href), web.label or web.rel) for web in getattr(entry, 'website', ()) if re.search('^(sip:|sips:)', web.href)))
                    numbers.difference_update(set((number, label) for number, label in numbers if label.lower().find('fax') != -1))
                    if not numbers:
                        continue
                    image_data = None
                    image_url, image_etag = entry.get_entry_photo_data()
                    if image_url and image_etag and self.entries_map.get(entry.id.text, Null)[0].image_etag != image_etag:
                        try:
                            image_data = self.client.Get(image_url).read()
                        except Exception:
                            pass
                    updated_contacts.append((entry.id.text, name, company, numbers, image_data, image_etag))
                self.update_contacts(updated_contacts)
                feed = self.client.get_next(feed) if feed.find_next_link() is not None else None
        except Unauthorized:
            settings.google_contacts.authorization_token = AuthorizationToken(InvalidToken)
            settings.save()
        except (ConnectionLost, RequestError, httplib.HTTPException, socket.error):
            self._load_timer = reactor.callLater(60, self.load_contacts)
        else:
            if update_timestamp:
                self.update_group_timestamp(update_timestamp)
            self._load_timer = reactor.callLater(60, self.load_contacts)

    @run_in_gui_thread
    @updates_contacts_db
    def update_contacts(self, contacts):
        if self.stop_adding_contacts:
            return
        icon_cache = IconCache()
        for id, name, company, numbers, image_data, image_etag in contacts:
            entries = self.entries_map.setdefault(id, [])
            existing_numbers = dict((entry.uri, entry) for entry in entries)
            # Save GoogleContact instances that can be reused to hold new contact information
            reusable_entries = deque(entry for entry in entries if entry.uri not in (number for number, label in numbers))
            image = icon_cache.store_image(image_data)
            if image_etag and not image_data:
                try:
                    image = entries[0].image
                    image_etag = entries[0].image_etag
                except IndexError:
                    image, image_etag = None, None
            for number, label in numbers:
                if number in existing_numbers:
                    entry = existing_numbers[number]
                    self.contact_model.updateContact(entry, dict(name=name, company=company, group=self.group, uri_type=self.normalize_uri_label(label), image=image, image_etag=image_etag))
                elif reusable_entries:
                    entry = reusable_entries.popleft()
                    self.contact_model.updateContact(entry, dict(name=name, company=company, group=self.group, uri=number, uri_type=self.normalize_uri_label(label), image=image, image_etag=image_etag))
                else:
                    try:
                        image = entries[0].image
                    except IndexError:
                        pass
                    entry = GoogleContact(id, self.group, name, number, company=company, uri_type=self.normalize_uri_label(label), image=image, image_etag=image_etag)
                    entries.append(entry)
                    self.contact_model.addContact(entry)
            for entry in reusable_entries:
                entries.remove(entry)
                self.contact_model.removeContact(entry)

    @run_in_gui_thread
    @updates_contacts_db
    def remove_contacts(self, contact_ids):
        deleted_contacts = []
        for id in contact_ids:
            deleted_contacts.extend(self.entries_map.pop(id, ()))
        for contact in deleted_contacts:
            self.contact_model.removeContact(contact)

    @run_in_gui_thread
    @updates_contacts_db
    def remove_group(self):
        self.contact_model.removeGroup(self.contact_model.google_contacts_group)
        self.group.id = None
        self.group.update_timestamp = None
        self.entries_map.clear()

    @run_in_gui_thread
    def update_group_timestamp(self, timestamp):
        if not self.stop_adding_contacts:
            self.group.update_timestamp = timestamp


ui_class, base_class = uic.loadUiType(Resources.get('google_contacts_dialog.ui'))

class GoogleContactsDialog(base_class, ui_class):
    __metaclass__ = QSingleton

    def __init__(self, parent=None):
        super(GoogleContactsDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)

        self.authorize_button.clicked.connect(self._SH_AuthorizeButtonClicked)
        self.captcha_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.username_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.password_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.rejected.connect(self._SH_DialogRejected)

        self.captcha_editor.regexp = re.compile('^.+$')
        self.username_editor.regexp = re.compile('^.+$')
        self.password_editor.regexp = re.compile('^.+$')

        self.captcha_token = None
        self.enable_captcha(False)

    def enable_captcha(self, visible):
        self.captcha_label.setVisible(visible)
        self.captcha_editor.setVisible(visible)
        self.captcha_image_label.setVisible(visible)
        inputs = [self.username_editor, self.password_editor]
        if visible:
            inputs.append(self.captcha_editor)
            self.captcha_editor.setText(u'')
            call_later(0, self.captcha_editor.setFocus)
        self.authorize_button.setEnabled(all(input.text_valid for input in inputs))

    def open(self):
        settings = SIPSimpleSettings()
        self.username_editor.setEnabled(True)
        self.username_editor.setText(settings.google_contacts.username or u'')
        self.password_editor.setText(u'')
        super(GoogleContactsDialog, self).show()

    def open_for_incorrect_password(self):
        red = '#cc0000'
        settings = SIPSimpleSettings()
        self.username_editor.setEnabled(False)
        self.username_editor.setText(settings.google_contacts.username)
        self.status_label.value = Status('Error authenticating with Google. Please enter your password:', color=red)
        super(GoogleContactsDialog, self).show()

    @run_in_green_thread
    def _authorize_google_account(self):
        red = '#cc0000'
        captcha_response = unicode(self.captcha_editor.text()) if self.captcha_token else None
        username = unicode(self.username_editor.text())
        password = unicode(self.password_editor.text())
        client = ContactsClient()
        try:
            client.client_login(email=username, password=password, source='Blink', captcha_token=self.captcha_token, captcha_response=captcha_response)
        except CaptchaChallenge, e:
            call_in_gui_thread(self.username_editor.setEnabled, False)
            call_in_gui_thread(setattr, self.status_label, 'value', Status('Error authenticating with Google', color=red))
            try:
                captcha_data = urllib2.urlopen(e.captcha_url).read()
            except (urllib2.HTTPError, urllib2.URLError):
                pass
            else:
                self.captcha_token = e.captcha_token
                call_in_gui_thread(self._set_captcha_image, captcha_data)
                call_in_gui_thread(self.enable_captcha, True)
        except RequestError:
            self.captcha_token = None
            call_in_gui_thread(self.username_editor.setEnabled, True)
            call_in_gui_thread(setattr, self.status_label, 'value', Status('Error authenticating with Google', color=red))
        except Exception:
            self.captcha_token = None
            call_in_gui_thread(self.username_editor.setEnabled, True)
            call_in_gui_thread(setattr, self.status_label, 'value', Status('Error connecting with Google', color=red))
        else:
            self.captcha_token = None
            settings = SIPSimpleSettings()
            settings.google_contacts.authorization_token = AuthorizationToken(client.auth_token.token_string)
            settings.google_contacts.username = username
            settings.save()
            call_in_gui_thread(self.enable_captcha, False)
            call_in_gui_thread(self.accept)
        finally:
            call_in_gui_thread(self.setEnabled, True)

    def _set_captcha_image(self, data):
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            pixmap = pixmap.scaled(200, 70, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.captcha_image_label.setPixmap(pixmap)

    def _SH_AuthorizeButtonClicked(self):
        self.status_label.value = Status('Contacting Google server...')
        self.setEnabled(False)
        self._authorize_google_account()

    @run_in_twisted_thread
    def _SH_DialogRejected(self):
        settings = SIPSimpleSettings()
        settings.google_contacts.authorization_token = None
        settings.save()
        self.captcha_token = None
        call_in_gui_thread(self.enable_captcha, False)

    def _SH_ValidityStatusChanged(self):
        red = '#cc0000'
        if not self.username_editor.text_valid:
            self.status_label.value = Status('Please specify your Google account username', color=red)
        elif not self.password_editor.text_valid:
            self.status_label.value = Status('Please specify your Google account password', color=red)
        elif self.captcha_editor.isVisible() and not self.captcha_editor.text_valid:
            self.status_label.value = Status('Please insert the text in the image below', color=red)
        else:
            self.status_label.value = None
        self.authorize_button.setEnabled(self.username_editor.text_valid and self.password_editor.text_valid and (True if not self.captcha_editor.isVisible() else self.captcha_editor.text_valid))

del ui_class, base_class


ui_class, base_class = uic.loadUiType(Resources.get('contact.ui'))

class ContactWidget(base_class, ui_class):
    def __init__(self, parent=None):
        super(ContactWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)

    def set_contact(self, contact):
        self.name.setText(contact.name_detail or contact.uri)
        self.uri.setText(contact.uri_detail)
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
        self._disable_dnd = False
        self.label_widget.setFocusProxy(self)
        self.name_view.setCurrentWidget(self.label_widget)
        self.name_editor.editingFinished.connect(self._end_editing)
        self.collapse_button.pressed.connect(self._collapse_button_pressed)

    @property
    def editing(self):
        return self.name_view.currentWidget() is self.editor_widget

    def _get_name(self):
        return unicode(self.name_label.text())

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

    def _collapse_button_pressed(self):
        self._disable_dnd = True

    def mousePressEvent(self, event):
        self._disable_dnd = False
        super(ContactGroupWidget, self).mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._disable_dnd:
            return
        super(ContactGroupWidget, self).mouseMoveEvent(event)

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
    item_size_hints = {Contact: QSize(200, 36), ContactGroup: QSize(200, 18), BonjourNeighbour: QSize(200, 36), BonjourGroup: QSize(200, 18), GoogleContact: QSize(200, 36), GoogleContactsGroup: QSize(200, 18)}

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
    paintGoogleContact = paintContact
    paintGoogleContactsGroup = paintContactGroup

    def paint(self, painter, option, index):
        item = index.model().data(index, Qt.DisplayRole)
        handler = getattr(self, 'paint%s' % item.__class__.__name__, Null)
        handler(item, painter, option, index)

    def sizeHint(self, option, index):
        return self.item_size_hints[type(index.model().data(index, Qt.DisplayRole))]


class ContactModel(QAbstractListModel):
    implements(IObserver)

    itemsAdded = pyqtSignal(list)
    itemsRemoved = pyqtSignal(list)

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-contact-group-list', 'application/x-blink-contact-list', 'text/uri-list']

    def __init__(self, parent=None):
        super(ContactModel, self).__init__(parent)
        self.items = []
        self.deleted_items = []
        self.main_window = parent
        self.contact_list = parent.contact_list
        if not hasattr(self, 'beginResetModel'):
            # emulate beginResetModel/endResetModel for QT < 4.6
            self.beginResetModel = Null # or use self.modelAboutToBeReset.emit (it'll be emited twice though in that case)
            self.endResetModel = self.reset
        self.bonjour_group = None

        self.google_contacts_group = None
        self.google_contacts_manager = GoogleContactsManager(self)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BonjourAccountDidAddNeighbour')
        notification_center.add_observer(self, name='BonjourAccountDidRemoveNeighbour')
        notification_center.add_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')
        notification_center.add_observer(self, name='SIPAccountManagerDidStart')
        notification_center.add_observer(self, name='SIPAccountDidActivate')
        notification_center.add_observer(self, name='SIPAccountDidDeactivate')

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
        items = self._pop_items(selected_indexes)
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
        if self.bonjour_group in moved_groups:
            self.bonjour_group.reference_group = NoGroup
        return True

    @updates_contacts_db
    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        group = self.items[index.row()] if index.isValid() else self.contact_groups[-1]
        if group.widget.drop_indicator is None:
            return False
        indexes = [index for index in self.contact_list.selectionModel().selectedIndexes() if self.items[index.row()].movable]
        for contact in self._pop_items(indexes):
            contact.group = group
            self._add_contact(contact)
        return True

    def _DH_TextUriList(self, mime_data, action, index):
        return False

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @ignore_contacts_db_updates
    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        contact = BonjourNeighbour(self.bonjour_group, notification.data.display_name, notification.data.host, unicode(notification.data.uri))
        self.addContact(contact)

    @ignore_contacts_db_updates
    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        uri = unicode(notification.data.uri)
        for contact in [c for c in self.items if type(c) is BonjourNeighbour and c.uri == uri]:
            self.removeContact(contact)

    def _NH_SIPAccountDidActivate(self, notification):
        account = notification.sender
        if account is BonjourAccount():
            self.addGroup(self.bonjour_group)

    def _NH_SIPAccountDidDeactivate(self, notification):
        account = notification.sender
        if account is BonjourAccount():
            self.removeGroup(self.bonjour_group)

    @ignore_contacts_db_updates
    def _NH_SIPAccountManagerDidStart(self, notification):
        if not BonjourAccount().enabled and self.bonjour_group in self.items:
            self.removeGroup(self.bonjour_group)
        if notification.sender.default_account is BonjourAccount():
            group = self.bonjour_group
            contact_groups = self.contact_groups
            try:
                group.reference_group = contact_groups[contact_groups.index(group)+1]
            except IndexError:
                group.reference_group = Null
            if group is not contact_groups[0]:
                self.moveGroup(group, contact_groups[0])
            group.expand()

    @ignore_contacts_db_updates
    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        account = notification.data.account
        old_account = notification.data.old_account
        if account is BonjourAccount():
            group = self.bonjour_group
            contact_groups = self.contact_groups
            try:
                group.reference_group = contact_groups[contact_groups.index(group)+1]
            except IndexError:
                group.reference_group = Null
            if group is not contact_groups[0]:
                self.moveGroup(group, contact_groups[0])
            group.expand()
        elif old_account is BonjourAccount():
            group = self.bonjour_group
            if group.reference_group is not NoGroup:
                self.moveGroup(group, group.reference_group)
                group.reference_group = NoGroup
            if group.collapsed and not group.user_collapsed:
                group.expand()
            elif not group.collapsed and group.user_collapsed:
                group.collapse()

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

    def _add_contact(self, contact):
        if contact.group in self.items:
            for position in xrange(self.items.index(contact.group)+1, len(self.items)):
                item = self.items[position]
                if isinstance(item, ContactGroup) or item.name_detail > contact.name_detail:
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

    def _add_group(self, group):
        position = len(self.items)
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.append(group)
        self.contact_list.openPersistentEditor(self.index(position))
        self.endInsertRows()

    def _pop_contact(self, contact):
        position = self.items.index(contact)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.items[position]
        self.endRemoveRows()
        return contact

    def _pop_group(self, group):
        start = self.items.index(group)
        end = start + len([item for item in self.items if isinstance(item, Contact) and item.group==group])
        self.beginRemoveRows(QModelIndex(), start, end)
        items = self.items[start:end+1]
        del self.items[start:end+1]
        self.endRemoveRows()
        return items

    def _pop_items(self, indexes):
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

    @run_in_auxiliary_thread
    def _store_contacts(self, data):
        makedirs(ApplicationData.directory)
        filename = ApplicationData.get('contacts')
        tmp_filename = ApplicationData.get('contacts.tmp')
        bak_filename = ApplicationData.get('contacts.bak')
        file = open(tmp_filename, 'wb')
        file.write(data)
        file.close()
        try:
            if sys.platform == 'win32':
                unlink(bak_filename)
            os.rename(filename, bak_filename)
        except OSError, e:
            if e.errno != errno.ENOENT:
                raise
        if sys.platform == 'win32':
            unlink(filename)
        os.rename(tmp_filename, filename)

    @updates_contacts_db
    def addContact(self, contact):
        if contact in self.items:
            return
        added_items = [contact] if contact.group in self.items else [contact.group, contact]
        self._add_contact(contact)
        self.itemsAdded.emit(added_items)

    @updates_contacts_db
    def updateContact(self, contact, attributes):
        group = attributes.pop('group')
        for name, value in attributes.iteritems():
            setattr(contact, name, value)
        if contact.group != group:
            new_group = group not in self.items
            self._pop_contact(contact)
            contact.group = group
            self._add_contact(contact)
            if new_group:
                self.itemsAdded.emit([group])
        index = self.index(self.items.index(contact))
        self.dataChanged.emit(index, index)

    @updates_contacts_db
    def removeContact(self, contact):
        if contact not in self.items:
            return
        self._pop_contact(contact)
        if type(contact) is Contact:
            self.deleted_items.append([contact])
        self.itemsRemoved.emit([contact])

    @updates_contacts_db
    def addGroup(self, group):
        if group in self.items:
            return
        self._add_group(group)
        self.itemsAdded.emit([group])

    @updates_contacts_db
    def removeGroup(self, group):
        if group not in self.items:
            return
        items = self._pop_group(group)
        group.widget = Null
        if type(group) is ContactGroup:
            self.deleted_items.append(items)
        self.itemsRemoved.emit(items)

    @updates_contacts_db
    def moveGroup(self, group, reference):
        contact_groups = self.contact_groups
        if group not in contact_groups or contact_groups.index(group)+1 == (contact_groups.index(reference) if reference in contact_groups else len(contact_groups)):
            return
        items = self._pop_group(group)
        position = self.items.index(reference) if reference in contact_groups else len(self.items)
        self.beginInsertRows(QModelIndex(), position, position+len(items)-1)
        self.items[position:position] = items
        self.endInsertRows()
        self.contact_list.openPersistentEditor(self.index(position))

    @updates_contacts_db
    def removeItems(self, indexes):
        items = self._pop_items(indexes)
        for item in (item for item in items if isinstance(item, ContactGroup)):
            item.widget = Null
        self.deleted_items.append(items)
        self.itemsRemoved.emit(items)

    def iter_contacts(self):
        return (item for item in self.items if isinstance(item, Contact))

    def iter_contact_groups(self):
        return (item for item in self.items if isinstance(item, ContactGroup))

    def load(self):
        try:
            try:
                file = open(ApplicationData.get('contacts'))
                items = pickle.load(file)
            except Exception:
                # remove the corrupted contacts file, so it won't be backed up to contacts.bak later
                unlink(ApplicationData.get('contacts'))
                file = open(ApplicationData.get('contacts.bak'))
                items = pickle.load(file)
                file = None # restore contacts from contacts.bak
        except Exception:
            file = None
            icon_cache = IconCache()
            group = ContactGroup('Test')
            contacts = [Contact(group, 'Call Test', '3333@sip2sip.info', image=icon_cache.store(Resources.get('icons/3333@sip2sip.info.png'))),
                        Contact(group, 'Echo Test', '4444@sip2sip.info', image=icon_cache.store(Resources.get('icons/4444@sip2sip.info.png'))),
                        Contact(group, 'Audio Conference', 'conference@sip2sip.info', image=icon_cache.store(Resources.get('icons/conference@sip2sip.info.png'))),
                        Contact(group, 'VUC Conference http://vuc.me', '200901@login.zipdx.com', image=icon_cache.store(Resources.get('icons/200901@login.zipdx.com.png')))]
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
            if type(item) is GoogleContactsGroup:
                self.google_contacts_group = item
        if self.bonjour_group is None:
            self.bonjour_group = BonjourGroup('Bonjour Neighbours')
        if self.google_contacts_group is None:
            self.google_contacts_group = GoogleContactsGroup('Google Contacts')
        self.google_contacts_manager.initialize()
        if file is None:
            self.save()

    def save(self):
        items = [item for item in self.items if item.savable]
        contact_groups = self.contact_groups
        group = self.bonjour_group
        reference = group.reference_group
        if group in contact_groups and reference is not NoGroup and contact_groups.index(group)+1 != (contact_groups.index(reference) if reference in contact_groups else len(contact_groups)):
            items.remove(self.bonjour_group)
            position = items.index(reference) if reference in contact_groups else len(self.items)
            items.insert(position, group)
        if self.google_contacts_group not in contact_groups:
            items.append(self.google_contacts_group)
        self._store_contacts(pickle.dumps(items))


class ContactSearchModel(QSortFilterProxyModel):
    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['text/uri-list']

    def __init__(self, model, parent=None):
        super(ContactSearchModel, self).__init__(parent)
        self.main_window = parent
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
        self.actions.start_audio_session = QAction("Start Audio Call", self, triggered=self._AH_StartAudioCall)
        self.actions.start_chat_session = QAction("Start Chat Session", self, triggered=self._AH_StartChatSession)
        self.actions.send_sms = QAction("Send SMS", self, triggered=self._AH_SendSMS)
        self.actions.send_files = QAction("Send File(s)...", self, triggered=self._AH_SendFiles)
        self.actions.request_remote_desktop = QAction("Request Remote Desktop", self, triggered=self._AH_RequestRemoteDesktop)
        self.actions.share_my_desktop = QAction("Share My Desktop", self, triggered=self._AH_ShareMyDesktop)
        self.needs_restore = False

    def setModel(self, model):
        selection_model = self.selectionModel() or Null
        selection_model.selectionChanged.disconnect(self._SH_SelectionModelSelectionChanged)
        super(ContactListView, self).setModel(model)
        self.selectionModel().selectionChanged.connect(self._SH_SelectionModelSelectionChanged)

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
        try:
            last_group = model.contact_groups[-1]
        except IndexError:
            last_group = Null
        if last_group.widget.drop_indicator is self.BelowItem:
            # draw the bottom part of the drop indicator for the last group if we have one
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
        if not model.deleted_items:
            undo_delete_text = "Undo Delete"
        elif len(model.deleted_items[-1]) == 1:
            item = model.deleted_items[-1][0]
            if type(item) is Contact:
                name = item.name or item.uri
            else:
                name = item.name
            undo_delete_text = 'Undo Delete "%s"' % name
        else:
            undo_delete_text = "Undo Delete (%d items)" % len(model.deleted_items[-1])
        menu = QMenu(self)
        if not selected_items:
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        elif len(selected_items) > 1:
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            menu.addAction(self.actions.delete_selection)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.delete_selection.setEnabled(any(item.deletable for item in selected_items))
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        elif isinstance(selected_items[0], ContactGroup):
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            menu.addAction(self.actions.edit_item)
            menu.addAction(self.actions.delete_item)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setText(undo_delete_text)
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
            self.actions.undo_last_delete.setText(undo_delete_text)
            account_manager = AccountManager()
            default_account = account_manager.default_account
            self.actions.start_audio_session.setEnabled(default_account is not None)
            self.actions.start_chat_session.setEnabled(False)
            self.actions.send_sms.setEnabled(False)
            self.actions.send_files.setEnabled(False)
            self.actions.request_remote_desktop.setEnabled(False)
            self.actions.share_my_desktop.setEnabled(False)
            self.actions.edit_item.setEnabled(contact.editable)
            self.actions.delete_item.setEnabled(contact.deletable)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        menu.exec_(event.globalPos())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Enter, Qt.Key_Return):
            selected_indexes = self.selectionModel().selectedIndexes()
            if len(selected_indexes) == 1:
                contact = self.model().data(selected_indexes[0])
                if not isinstance(contact, Contact):
                    return
                session_manager = SessionManager()
                session_manager.start_call(contact.name, contact.uri, contact=contact, account=BonjourAccount() if isinstance(contact, BonjourNeighbour) else None)
        else:
            super(ContactListView, self).keyPressEvent(event)

    def _AH_AddGroup(self):
        group = ContactGroup("")
        model = self.model()
        selection_model = self.selectionModel()
        model.addGroup(group)
        self.scrollToBottom()
        group.widget.edit()
        selection_model.select(model.index(model.rowCount()-1), selection_model.ClearAndSelect)

    def _AH_AddContact(self):
        model = self.model()
        main_window = model.main_window
        selected_items = ((index.row(), model.data(index)) for index in self.selectionModel().selectedIndexes())
        try:
            item = (item for row, item in sorted(selected_items) if type(item) in (Contact, ContactGroup)).next()
            preferred_group = item if type(item) is ContactGroup else item.group
        except StopIteration:
            try:
                preferred_group = (group for group in model.contact_groups if type(group) is ContactGroup).next()
            except StopIteration:
                preferred_group = None
        main_window.contact_editor_dialog.open_for_add(main_window.search_box.text(), preferred_group)

    def _AH_EditItem(self):
        model = self.model()
        index = self.selectionModel().selectedIndexes()[0]
        item = model.data(index)
        if isinstance(item, ContactGroup):
            self.scrollTo(index)
            item.widget.edit()
        else:
            model.main_window.contact_editor_dialog.open_for_edit(item)

    def _AH_DeleteSelection(self):
        model = self.model()
        indexes = [index for index in self.selectionModel().selectedIndexes() if model.items[index.row()].deletable]
        model.removeItems(indexes)
        self.selectionModel().clearSelection()

    @updates_contacts_db
    def _AH_UndoLastDelete(self):
        model = self.model()
        for item in model.deleted_items.pop():
            handler = model.addGroup if isinstance(item, ContactGroup) else model.addContact
            handler(item)

    def _AH_StartAudioCall(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        session_manager = SessionManager()
        session_manager.start_call(contact.name, contact.uri, contact=contact, account=BonjourAccount() if isinstance(contact, BonjourNeighbour) else None)

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

    def startDrag(self, supported_actions):
        super(ContactListView, self).startDrag(supported_actions)
        if self.needs_restore:
            for group in self.model().contact_groups:
                group.restore_state()
            self.needs_restore = False
        main_window = self.model().main_window
        main_window.switch_view_button.dnd_active = False
        if not main_window.session_model.sessions:
            main_window.switch_view_button.view = SwitchViewButton.ContactView

    def dragEnterEvent(self, event):
        model = self.model()
        event_source = event.source()
        accepted_mime_types = set(model.accepted_mime_types)
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
                    for group in model.contact_groups:
                        group.save_state()
                        group.collapse()
                    self.needs_restore = True
            if has_blink_contacts:
                model.main_window.switch_view_button.dnd_active = True
            event.accept()
            self.setState(self.DraggingState)

    def dragLeaveEvent(self, event):
        super(ContactListView, self).dragLeaveEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()
        for group in self.model().contact_groups:
            group.widget.drop_indicator = None

    def dragMoveEvent(self, event):
        super(ContactListView, self).dragMoveEvent(event)
        if event.source() is self:
            event.setDropAction(Qt.MoveAction)

        model = self.model()
        for mime_type in model.accepted_mime_types:
            if event.provides(mime_type):
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                self.drop_indicator_index = QModelIndex()
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                item = model.data(index)
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
        super(ContactListView, self).dropEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

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

    def _SH_SelectionModelSelectionChanged(self, selected, deselected):
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1)
            selection_model.setCurrentIndex(index, selection_model.Select)


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
        self.actions.start_audio_session = QAction("Start Audio Call", self, triggered=self._AH_StartAudioCall)
        self.actions.start_chat_session = QAction("Start Chat Session", self, triggered=self._AH_StartChatSession)
        self.actions.send_sms = QAction("Send SMS", self, triggered=self._AH_SendSMS)
        self.actions.send_files = QAction("Send File(s)...", self, triggered=self._AH_SendFiles)
        self.actions.request_remote_desktop = QAction("Request Remote Desktop", self, triggered=self._AH_RequestRemoteDesktop)
        self.actions.share_my_desktop = QAction("Share My Desktop", self, triggered=self._AH_ShareMyDesktop)

    def setModel(self, model):
        selection_model = self.selectionModel() or Null
        selection_model.selectionChanged.disconnect(self._SH_SelectionModelSelectionChanged)
        super(ContactSearchListView, self).setModel(model)
        self.selectionModel().selectionChanged.connect(self._SH_SelectionModelSelectionChanged)

    def focusInEvent(self, event):
        super(ContactSearchListView, self).focusInEvent(event)
        model = self.model()
        selection_model = self.selectionModel()
        if not selection_model.selectedIndexes() and model.rowCount() > 0:
            selection_model.select(model.index(0, 0), selection_model.Select)

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
        if not source_model.deleted_items:
            undo_delete_text = "Undo Delete"
        elif len(source_model.deleted_items[-1]) == 1:
            item = source_model.deleted_items[-1][0]
            if type(item) is Contact:
                name = item.name or item.uri
            else:
                name = item.name
            undo_delete_text = 'Undo Delete "%s"' % name
        else:
            undo_delete_text = "Undo Delete (%d items)" % len(source_model.deleted_items[-1])
        menu = QMenu(self)
        if not selected_items:
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.undo_last_delete.setEnabled(len(source_model.deleted_items) > 0)
        elif len(selected_items) > 1:
            menu.addAction(self.actions.delete_selection)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setText(undo_delete_text)
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
            self.actions.undo_last_delete.setText(undo_delete_text)
            account_manager = AccountManager()
            default_account = account_manager.default_account
            self.actions.start_audio_session.setEnabled(default_account is not None)
            self.actions.start_chat_session.setEnabled(False)
            self.actions.send_sms.setEnabled(False)
            self.actions.send_files.setEnabled(False)
            self.actions.request_remote_desktop.setEnabled(False)
            self.actions.share_my_desktop.setEnabled(False)
            self.actions.edit_item.setEnabled(contact.editable)
            self.actions.delete_item.setEnabled(contact.deletable)
            self.actions.undo_last_delete.setEnabled(len(source_model.deleted_items) > 0)
        menu.exec_(event.globalPos())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Enter, Qt.Key_Return):
            selected_indexes = self.selectionModel().selectedIndexes()
            if len(selected_indexes) == 1:
                contact = self.model().data(selected_indexes[0])
                if not isinstance(contact, Contact):
                    return
                session_manager = SessionManager()
                session_manager.start_call(contact.name, contact.uri, contact=contact, account=BonjourAccount() if isinstance(contact, BonjourNeighbour) else None)
        else:
            super(ContactSearchListView, self).keyPressEvent(event)

    def _AH_EditItem(self):
        model = self.model()
        contact = model.data(self.selectionModel().selectedIndexes()[0])
        model.main_window.contact_editor_dialog.open_for_edit(contact)

    def _AH_DeleteSelection(self):
        model = self.model()
        model.sourceModel().removeItems(model.mapToSource(index) for index in self.selectionModel().selectedIndexes() if model.data(index).deletable)

    @updates_contacts_db
    def _AH_UndoLastDelete(self):
        model = self.model().sourceModel()
        for item in model.deleted_items.pop():
            handler = model.addGroup if isinstance(item, ContactGroup) else model.addContact
            handler(item)

    def _AH_StartAudioCall(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        session_manager = SessionManager()
        session_manager.start_call(contact.name, contact.uri, contact=contact, account=BonjourAccount() if isinstance(contact, BonjourNeighbour) else None)

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

    def startDrag(self, supported_actions):
        super(ContactSearchListView, self).startDrag(supported_actions)
        main_window = self.model().main_window
        main_window.switch_view_button.dnd_active = False
        if not main_window.session_model.sessions:
            main_window.switch_view_button.view = SwitchViewButton.ContactView

    def dragEnterEvent(self, event):
        model = self.model()
        accepted_mime_types = set(model.accepted_mime_types)
        provided_mime_types = set(str(x) for x in event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if event.source() is self:
            event.ignore()
            model.main_window.switch_view_button.dnd_active = True
        elif not acceptable_mime_types:
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

    def _DH_TextUriList(self, event, index, rect, item):
        if index.isValid():
            event.accept(rect)
            self.drop_indicator_index = index
        else:
            model = self.model()
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(model.rowCount()-1, 0)).bottom())
            event.ignore(rect)

    def _SH_SelectionModelSelectionChanged(self, selected, deselected):
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1, -1)
            selection_model.setCurrentIndex(index, selection_model.Select)


# The contact editor dialog
#

class ContactEditorGroupModel(QSortFilterProxyModel):
    def __init__(self, contact_model, parent=None):
        super(ContactEditorGroupModel, self).__init__(parent)
        self.setSourceModel(contact_model)

    def data(self, index, role=Qt.DisplayRole):
        if role in (Qt.DisplayRole, Qt.EditRole):
            return super(ContactEditorGroupModel, self).data(index, Qt.DisplayRole).toPyObject().name
        elif role == Qt.UserRole:
            return super(ContactEditorGroupModel, self).data(index, Qt.DisplayRole).toPyObject()
        else:
            return super(ContactEditorGroupModel, self).data(index, role)

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        item = source_model.data(source_model.index(source_row, 0, source_parent), Qt.DisplayRole)
        return True if type(item) is ContactGroup else False


ui_class, base_class = uic.loadUiType(Resources.get('contact_editor.ui'))

class ContactEditorDialog(base_class, ui_class):
    def __init__(self, contact_model, parent=None):
        super(ContactEditorDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.edited_contact = None
        self.group.setModel(ContactEditorGroupModel(contact_model, parent))
        self.sip_address_editor.setValidator(QRegExpValidator(QRegExp("\S+"), self))
        self.sip_address_editor.textChanged.connect(self.enable_accept_button)
        self.clear_button.clicked.connect(self.reset_icon)
        self.accepted.connect(self.process_contact)

    def open_for_add(self, sip_address=u'', target_group=None):
        self.sip_address_editor.setText(sip_address)
        self.display_name_editor.setText(u'')
        self.sip_aliases_editor.setText(u'')
        for index in xrange(self.group.count()):
            if self.group.itemData(index).toPyObject() is target_group:
                break
        else:
            index = 0
        self.group.setCurrentIndex(index)
        self.icon_selector.filename = None
        self.preferred_media.setCurrentIndex(0)
        self.accept_button.setText(u'Add')
        self.accept_button.setEnabled(sip_address != u'')
        self.show()

    def open_for_edit(self, contact):
        self.edited_contact = contact
        self.sip_address_editor.setText(contact.uri)
        self.display_name_editor.setText(contact.name)
        self.sip_aliases_editor.setText(u'; '.join(contact.sip_aliases))
        for index in xrange(self.group.count()):
            if self.group.itemData(index).toPyObject() is contact.group:
                break
        else:
            index = 0
        self.group.setCurrentIndex(index)
        self.icon_selector.filename = contact.image
        self.preferred_media.setCurrentIndex(self.preferred_media.findText(contact.preferred_media))
        self.accept_button.setText(u'Ok')
        self.accept_button.setEnabled(True)
        self.show()

    def reset_icon(self):
        self.icon_selector.filename = None

    def enable_accept_button(self, text):
        self.accept_button.setEnabled(text != u'')

    @updates_contacts_db
    def process_contact(self):
        contact_model = self.parent().contact_model
        uri = unicode(self.sip_address_editor.text())
        name = unicode(self.display_name_editor.text())
        image = IconCache().store(self.icon_selector.filename)
        preferred_media = unicode(self.preferred_media.currentText())
        sip_aliases = [alias.strip() for alias in unicode(self.sip_aliases_editor.text()).split(u';')]
        group_index = self.group.currentIndex()
        group_name = self.group.currentText()
        if group_name != self.group.itemText(group_index):
            # user edited the group name. first look if we already have a group with that name
            index = self.group.findText(group_name)
            if index >= 0:
                group = self.group.itemData(index).toPyObject()
            else:
                group = ContactGroup(unicode(group_name))
        else:
            group = self.group.itemData(group_index).toPyObject()
        if self.edited_contact is None:
            contact = Contact(group, name, uri, image=image)
            contact.preferred_media = preferred_media
            contact.sip_aliases = sip_aliases
            contact_model.addContact(contact)
        else:
            attributes = dict(group=group, name=name, uri=uri, image=image, preferred_media=preferred_media, sip_aliases=sip_aliases)
            contact_model.updateContact(self.edited_contact, attributes)
            self.edited_contact = None

del ui_class, base_class


