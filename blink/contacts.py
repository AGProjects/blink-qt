# Copyright (C) 2010-2013 AG Projects. See LICENSE for details.
#

__all__ = ['Group', 'Contact', 'BonjourNeighbour', 'GoogleContact', 'ContactModel', 'ContactSearchModel', 'ContactListView', 'ContactSearchListView', 'ContactEditorDialog', 'GoogleContactsDialog']

import cPickle as pickle
import os
import re
import socket
import sys

from PyQt4 import uic
from PyQt4.QtCore import Qt, QAbstractListModel, QByteArray, QEvent, QMimeData, QModelIndex, QPointF, QRectF, QRegExp, QSize, pyqtSignal
from PyQt4.QtGui  import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QStyle
from PyQt4.QtGui  import QAction, QKeyEvent, QListView, QMenu, QMouseEvent, QRegExpValidator, QSortFilterProxyModel, QStyledItemDelegate

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData, ObserverWeakrefProxy
from application.python.decorator import execute_once
from application.python.descriptor import WriteOnceAttribute
from application.python.types import MarkerType, Singleton
from application.python import Null
from application.system import unlink
from collections import deque
from datetime import datetime
from eventlib import coros, proc
from eventlib.green import httplib, urllib2
from functools import partial
from operator import attrgetter
from twisted.internet import reactor
from twisted.internet.error import ConnectionLost
from zope.interface import implements

from sipsimple import addressbook
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration import ConfigurationManager, DefaultValue, Setting, SettingsState, SettingsObjectMeta, ObjectNotFoundError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread, run_in_twisted_thread
from sipsimple.threading.green import Command, call_in_green_thread, run_in_green_thread

from blink.configuration.datatypes import AuthorizationToken, InvalidToken, IconDescriptor
from blink.resources import ApplicationData, Resources, IconManager
from blink.sessions import SessionManager
from blink.util import QSingleton, call_in_gui_thread, call_later, run_in_gui_thread
from blink.widgets.buttons import SwitchViewButton
from blink.widgets.labels import Status

from blink.google.gdata.client import CaptchaChallenge, RequestError, Unauthorized
from blink.google.gdata.contacts.client import ContactsClient
from blink.google.gdata.contacts.data import ContactsFeed
from blink.google.gdata.contacts.service import ContactsQuery
from blink.google.gdata.gauth import ClientLoginToken


class VirtualGroupManager(object):
    __metaclass__ = Singleton
    implements(IObserver)

    __groups__ = []

    def __init__(self):
        self.groups = {}
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationWillStart')
        notification_center.add_observer(self, name='VirtualGroupWasActivated')
        notification_center.add_observer(self, name='VirtualGroupWasDeleted')

    def has_group(self, id):
        return id in self.groups

    def get_group(self, id):
        return self.groups[id]

    def get_groups(self):
        return self.groups.values()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        [cls() for cls in self.__groups__]

    def _NH_VirtualGroupWasActivated(self, notification):
        group = notification.sender
        self.groups[group.id] = group
        notification.center.post_notification('VirtualGroupManagerDidAddGroup', sender=self, data=NotificationData(group=group))

    def _NH_VirtualGroupWasDeleted(self, notification):
        group = notification.sender
        del self.groups[group.id]
        notification.center.post_notification('VirtualGroupManagerDidRemoveGroup', sender=self, data=NotificationData(group=group))


class VirtualGroupMeta(SettingsObjectMeta):
    def __init__(cls, name, bases, dic):
        if not (cls.__id__ is None or isinstance(cls.__id__, basestring)):
            raise TypeError("%s.__id__ must be None or a string" % name)
        super(VirtualGroupMeta, cls).__init__(name, bases, dic)
        if cls.__id__ is not None:
            VirtualGroupManager.__groups__.append(cls)


class VirtualGroup(SettingsState):
    __metaclass__ = VirtualGroupMeta
    __id__ = None

    name = Setting(type=unicode, default='')
    position = Setting(type=int, default=None, nillable=True)
    collapsed = Setting(type=bool, default=False)

    def __new__(cls):
        if cls.__id__ is None:
            raise ValueError("%s.__id__ must be defined in order to instantiate" % cls.__name__)
        instance = SettingsState.__new__(cls)
        configuration = ConfigurationManager()
        try:
            data = configuration.get(instance.__key__)
        except ObjectNotFoundError:
            pass
        else:
            instance.__setstate__(data)
        return instance

    def __repr__(self):
        return "%s()" % self.__class__.__name__

    @property
    def __key__(self):
        return ['Addressbook', 'VirtualGroups', self.__id__]

    @property
    def id(self):
        return self.__id__

    @run_in_thread('file-io')
    def save(self):
        """
        Store the virtual group into persistent storage.

        This method will post the VirtualGroupDidChange notification on save,
        regardless of whether the contact has been saved to persistent storage
        or not. A CFGManagerSaveFailed notification is posted if saving to the
        persistent configuration storage fails.
        """

        modified_settings = self.get_modified()

        if not modified_settings:
            return

        configuration = ConfigurationManager()
        notification_center = NotificationCenter()

        configuration.update(self.__key__, self.__getstate__())
        notification_center.post_notification('VirtualGroupDidChange', sender=self, data=NotificationData(modified=modified_settings))
        modified_data = modified_settings

        try:
            configuration.save()
        except Exception, e:
            log.err()
            notification_center.post_notification('CFGManagerSaveFailed', sender=configuration, data=NotificationData(object=self, operation='save', modified=modified_data, exception=e))


class AllContactsList(object):
    def __init__(self):
        self.manager = addressbook.AddressbookManager()
    def __iter__(self):
        return iter(self.manager.get_contacts())
    def __getitem__(self, id):
        return self.manager.get_contact(id)
    def __contains__(self, id):
        return self.manager.has_contact(id)
    def __len__(self):
        return len(self.manager.get_contacts())
    __hash__ = None


class AllContactsGroup(VirtualGroup):
    implements(IObserver)

    __id__ = 'all_contacts'

    name = Setting(type=unicode, default='All Contacts')
    contacts = WriteOnceAttribute()

    def __init__(self):
        self.contacts = AllContactsList()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='AddressbookContactWasActivated')
        notification_center.add_observer(self, name='AddressbookContactWasDeleted')

    def __establish__(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('VirtualGroupWasActivated', sender=self)
        for contact in self.contacts:
            notification_center.post_notification('VirtualGroupDidAddContact', sender=self, data=NotificationData(contact=contact))

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactWasActivated(self, notification):
        contact = notification.sender
        notification.center.post_notification('VirtualGroupDidAddContact', sender=self, data=NotificationData(contact=contact))

    def _NH_AddressbookContactWasDeleted(self, notification):
        contact = notification.sender
        notification.center.post_notification('VirtualGroupDidRemoveContact', sender=self, data=NotificationData(contact=contact))


class BonjourNeighbourURI(object):
    def __init__(self, uri, type=None):
        self.uri = uri
        self.type = type

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self.uri, self.type)


class BonjourNeighbour(object):
    def __init__(self, name, uri, hostname, neighbour):
        self.name = name
        self.uris = [BonjourNeighbourURI(uri)]
        self.hostname = hostname
        self.neighbour = neighbour

    @property
    def id(self):
        return self.neighbour


class BonjourNeighboursList(object):
    def __init__(self):
        self.contacts = {}
    def __getitem__(self, id):
        return self.contacts[id]
    def __contains__(self, id):
        return id in self.contacts
    def __iter__(self):
        return iter(self.contacts.values())
    def __len__(self):
        return len(self.contacts)
    __hash__ = None
    def add(self, contact):
        self.contacts[contact.id] = contact
    def pop(self, id, *args):
        return self.contacts.pop(id, *args)


class BonjourNeighboursGroup(VirtualGroup):
    implements(IObserver)

    __id__ = 'bonjour_neighbours'

    name = Setting(type=unicode, default='Bonjour Neighbours')
    contacts = WriteOnceAttribute()

    def __init__(self):
        self.contacts = BonjourNeighboursList()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=BonjourAccount())

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountWillActivate(self, notification):
        notification.center.post_notification('VirtualGroupWasActivated', sender=self)

    def _NH_SIPAccountDidDeactivate(self, notification):
        notification.center.post_notification('VirtualGroupWasDeactivated', sender=self)

    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        contact = BonjourNeighbour(notification.data.display_name, unicode(notification.data.uri), notification.data.host, notification.data.neighbour)
        self.contacts.add(contact)
        notification.center.post_notification('VirtualGroupDidAddContact', sender=self, data=NotificationData(contact=contact))

    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        contact = self.contacts.pop(notification.data.neighbour)
        notification.center.post_notification('VirtualGroupDidRemoveContact', sender=self, data=NotificationData(contact=contact))

    def _NH_BonjourAccountDidUpdateNeighbour(self, notification):
        contact = self.contacts[notification.data.neighbour]
        contact.display_name = notification.data.display_name
        contact.host = notification.data.host
        contact.uri = unicode(notification.data.uri)
        notification.center.post_notification('VirtualContactDidChange', sender=contact)


class GoogleContactID(unicode):
    pass


class GoogleContactIcon(object):
    def __init__(self, data, etag):
        self.data = data
        self.etag = etag


class GoogleContactURI(object):
    def __init__(self, uri, type):
        self.uri = uri
        self.type = type

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self.uri, self.type)

    @classmethod
    def from_number(cls, number):
        return cls(number.text, cls._get_label(number))

    @classmethod
    def from_email(cls, email):
        return cls(re.sub('^sips?:', '', email.address), cls._get_label(email))

    @staticmethod
    def _get_label(entry):
        if entry.label:
            return entry.label.strip()
        else:
            return entry.rel.rpartition('#')[2].replace('_', ' ').strip().title()


class GoogleContact(object):
    id = WriteOnceAttribute()

    def __init__(self, id, name, company, icon, uris):
        self.id = GoogleContactID(id)
        self.name = name
        self.company = company
        self.icon = icon
        self.uris = uris

    def __reduce__(self):
        return (self.__class__, (self.id, self.name, self.company, self.icon, self.uris))


class GoogleContactsList(object):
    def __init__(self):
        self.contacts = {}
        self.timestamp = None
    def __getitem__(self, id):
        return self.contacts[id]
    def __contains__(self, id):
        return id in self.contacts
    def __iter__(self):
        return iter(self.contacts.values())
    def __len__(self):
        return len(self.contacts)
    __hash__ = None
    def add(self, contact):
        self.contacts[contact.id] = contact
    def pop(self, id, *args):
        return self.contacts.pop(id, *args)


class GoogleContactsManager(object):
    implements(IObserver)

    contacts = WriteOnceAttribute()

    def __init__(self):
        self.client = ContactsClient()
        self.command_proc = None
        self.command_channel = coros.queue()
        self.last_fetch_time = datetime.fromtimestamp(0)
        self.not_executed_fetch = None
        self.active = False
        self.state = 'stopped'
        self.timer = None
        self.need_sync = True
        try:
            self.contacts = pickle.load(open(ApplicationData.get('google_contacts')))
        except Exception:
            self.contacts = GoogleContactsList()
        self._initialize()

    def _get_state(self):
        return self.__dict__['state']

    def _set_state(self, value):
        old_value = self.__dict__.get('state', Null)
        self.__dict__['state'] = value
        if old_value != value and old_value is not Null:
            notification_center = NotificationCenter()
            notification_center.post_notification('GoogleContactsManagerDidChangeState', sender=self, data=NotificationData(prev_state=old_value, state=value))

    state = property(_get_state, _set_state)
    del _get_state, _set_state

    @execute_once
    @run_in_green_thread
    def _initialize(self):
        self.command_proc = proc.spawn(self._run)

    def _run(self):
        while True:
            command = self.command_channel.wait()
            try:
                handler = getattr(self, '_CH_%s' % command.name)
                handler(command)
            except:
                self.command_proc = None
                raise

    def start(self):
        """
        Starts the Google contacts manager. This method needs to be called in
        a green thread.
        """
        command = Command('start')
        self.command_channel.send(command)
        command.wait()

    def stop(self):
        """
        Stops the Google contacts manager. This method blocks until all the
        operations are stopped and needs to be called in a green thread.
        """
        command = Command('stop')
        self.command_channel.send(command)
        command.wait()

    # Command handlers
    #

    def _CH_start(self, command):
        if self.state != 'stopped':
            command.signal()
            return
        self.state = 'initializing'
        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()
        notification_center.post_notification('GoogleContactsManagerWillStart', sender=self)
        notification_center.add_observer(self, sender=settings, name='CFGSettingsObjectDidChange')
        if settings.google_contacts.authorization_token is not None:
            self.active = True
            notification_center.post_notification('GoogleContactsManagerDidActivate', sender=self)
            for contact in self.contacts:
                notification_center.post_notification('GoogleContactsManagerDidAddContact', sender=self, data=NotificationData(contact=contact))
            self.command_channel.send(Command('initialize'))
        notification_center.post_notification('GoogleContactsManagerDidStart', sender=self)
        command.signal()

    def _CH_stop(self, command):
        if self.state == 'stopped':
            command.signal()
            return
        notification_center = NotificationCenter()
        notification_center.post_notification('GoogleContactsManagerWillEnd', sender=self)
        notification_center.remove_observer(self, sender=SIPSimpleSettings(), name='CFGSettingsObjectDidChange')
        if self.active:
            self.active = False
            notification_center.post_notification('GoogleContactsManagerDidDeactivate', sender=self)
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None
        self.client = None
        self.state = 'stopped'
        self._save_contacts()
        notification_center.post_notification('GoogleContactsManagerDidEnd', sender=self)
        command.signal()

    def _CH_initialize(self, command):
        self.state = 'initializing'
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None
        self.state = 'fetching'
        self.command_channel.send(Command('fetch'))

    def _CH_fetch(self, command):
        if self.state not in ('insync', 'fetching'):
            self.not_executed_fetch = command
            return
        self.not_executed_fetch = None
        self.state = 'fetching'
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None

        settings = SIPSimpleSettings()
        self.client.auth_token = ClientLoginToken(settings.google_contacts.authorization_token)

        try:
            group_id = next(entry.id.text for entry in self.client.get_groups().entry if entry.title.text=='System Group: My Contacts')
            if self.need_sync:
                query = ContactsQuery(feed=self.client.get_feed_uri(kind='contacts'), group=group_id, params={})
                feed = self.client.get_feed(query.ToUri(), desired_class=ContactsFeed)
                all_contact_ids = set()
                while feed:
                    all_contact_ids.update(entry.id.text for entry in feed.entry)
                    feed = self.client.get_next(feed) if feed.find_next_link() is not None else None
                deleted_contacts = [contact for contact in self.contacts if contact.id not in all_contact_ids]
                self.need_sync = False
            else:
                deleted_contacts = []
            query = ContactsQuery(feed=self.client.get_feed_uri(kind='contacts'), group=group_id, params={'showdeleted': 'true'})
            if self.contacts.timestamp is not None:
                query.updated_min = self.contacts.timestamp
            feed = self.client.get_feed(query.ToUri(), desired_class=ContactsFeed)
            update_timestamp = feed.updated.text if feed else None

            added_contacts = []
            updated_contacts = []

            while feed:
                deleted_contacts.extend(self.contacts[entry.id.text] for entry in feed.entry if entry.deleted and entry.id.text in self.contacts)
                for entry in (entry for entry in feed.entry if not entry.deleted):
                    name = entry.title.text
                    try:
                        company = entry.organization.name.text
                    except AttributeError:
                        company = None
                    uris = [GoogleContactURI.from_number(number) for number in entry.phone_number]
                    uris.extend(GoogleContactURI.from_email(email) for email in entry.email)
                    icon_url, icon_etag = entry.get_entry_photo_data()
                    try:
                        contact = self.contacts[entry.id.text]
                    except KeyError:
                        if icon_url:
                            try:
                                icon_data = self.client.Get(icon_url).read()
                            except Exception:
                                icon_data = icon_etag = None
                        else:
                            icon_data = icon_etag = None
                        icon = GoogleContactIcon(icon_data, icon_etag)
                        contact = GoogleContact(entry.id.text, name, company, icon, uris)
                        added_contacts.append(contact)
                    else:
                        contact.name = name
                        contact.company = company
                        contact.uris = uris
                        if icon_url and contact.icon.etag != icon_etag != None:
                            try:
                                contact.icon.data = self.client.Get(icon_url).read()
                            except Exception:
                                contact.icon.data = None
                                contact.icon.etag = None
                            else:
                                contact.icon.etag = icon_etag
                        updated_contacts.append(contact)
                feed = self.client.get_next(feed) if feed.find_next_link() is not None else None
        except Unauthorized:
            settings.google_contacts.authorization_token = InvalidToken
            settings.save()
        except (ConnectionLost, RequestError, httplib.HTTPException, socket.error):
            self.timer = self._schedule_command(60, Command('fetch', command.event))
        else:
            notification_center = NotificationCenter()
            for contact in deleted_contacts:
                self.contacts.pop(contact.id, None)
                notification_center.post_notification('GoogleContactsManagerDidRemoveContact', sender=self, data=NotificationData(contact=contact))
            for contact in added_contacts:
                self.contacts.add(contact)
                notification_center.post_notification('GoogleContactsManagerDidAddContact', sender=self, data=NotificationData(contact=contact))
            for contact in updated_contacts:
                notification_center.post_notification('GoogleContactsManagerDidUpdateContact', sender=self, data=NotificationData(contact=contact))
            if update_timestamp is not None:
                self.contacts.timestamp = update_timestamp
            if added_contacts or updated_contacts or deleted_contacts:
                self._save_contacts()
            self.last_fetch_time = datetime.utcnow()
            self.state = 'insync'
            self.timer = self._schedule_command(60, Command('fetch'))
            command.signal()

    # Notification handlers
    #

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'google_contacts.authorization_token' in notification.data.modified:
            if self.timer is not None and self.timer.active():
                self.timer.cancel()
            self.timer = None
            authorization_token = notification.sender.google_contacts.authorization_token
            if authorization_token is not None:
                if not self.active:
                    self.active = True
                    notification.center.post_notification('GoogleContactsManagerDidActivate', sender=self)
                    for contact in self.contacts:
                        notification.center.post_notification('GoogleContactsManagerDidAddContact', sender=self, data=NotificationData(contact=contact))
                self.need_sync = True
                self.command_channel.send(Command('initialize'))
            else:
                if self.active:
                    self.active = False
                    for contact in self.contacts:
                        notification.center.post_notification('GoogleContactsManagerDidRemoveContact', sender=self, data=NotificationData(contact=contact))
                    notification.center.post_notification('GoogleContactsManagerDidDeactivate', sender=self)

    def _save_contacts(self):
        contacts_filename = ApplicationData.get('google_contacts')
        contacts_tempname = contacts_filename + '.tmp'
        try:
            file = open(contacts_tempname, 'wb')
            pickle.dump(self.contacts, file)
            file.close()
            if sys.platform == 'win32':
                unlink(contacts_filename)
            os.rename(contacts_tempname, contacts_filename)
        except Exception, e:
            log.error("could not save google contacts: %s" % e)

    def _schedule_command(self, timeout, command):
        timer = reactor.callLater(timeout, self.command_channel.send, command)
        timer.command = command
        return timer


class GoogleContactsGroup(VirtualGroup):
    implements(IObserver)

    __id__ = 'google_contacts'

    name = Setting(type=unicode, default='Google Contacts')
    contacts = property(lambda self: self.__manager__.contacts)

    def __init__(self):
        self.__manager__ = GoogleContactsManager()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        notification_center.add_observer(self, sender=self.__manager__)
        call_in_green_thread(self.__manager__.start)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillEnd(self, notification):
        call_in_green_thread(self.__manager__.stop)

    def _NH_GoogleContactsManagerDidActivate(self, notification):
        notification.center.post_notification('VirtualGroupWasActivated', sender=self)

    def _NH_GoogleContactsManagerDidDeactivate(self, notification):
        notification.center.post_notification('VirtualGroupWasDeactivated', sender=self)

    def _NH_GoogleContactsManagerDidAddContact(self, notification):
        notification.center.post_notification('VirtualGroupDidAddContact', sender=self, data=notification.data)

    def _NH_GoogleContactsManagerDidRemoveContact(self, notification):
        notification.center.post_notification('VirtualGroupDidRemoveContact', sender=self, data=notification.data)

    def _NH_GoogleContactsManagerDidUpdateContact(self, notification):
        notification.center.post_notification('VirtualContactDidChange', sender=notification.data.contact)


class Group(object):
    implements(IObserver)

    size_hint = QSize(200, 18)

    virtual = property(lambda self: isinstance(self.settings, VirtualGroup))

    movable = True
    editable = True
    deletable = property(lambda self: not self.virtual)

    def __init__(self, group):
        self.settings = group
        self.widget = Null
        self.saved_state = None
        self.reference_group = None
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=group)

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.settings)

    def __reduce__(self):
        return (self.__class__, (self.settings,), None)

    def __unicode__(self):
        return self.settings.name

    def _get_widget(self):
        return self.__dict__['widget']

    def _set_widget(self, widget):
        old_widget = self.__dict__.get('widget', Null)
        old_widget.collapse_button.clicked.disconnect(self._collapsed_changed)
        old_widget.name_editor.editingFinished.disconnect(self._name_changed)
        widget.collapse_button.clicked.connect(self._collapsed_changed)
        widget.name_editor.editingFinished.connect(self._name_changed)
        widget.collapse_button.setChecked(old_widget.collapse_button.isChecked() if old_widget is not Null else self.settings.collapsed)
        widget.name = self.name
        self.__dict__['widget'] = widget

    widget = property(_get_widget, _set_widget)
    del _get_widget, _set_widget

    @property
    def name(self):
        return self.settings.name

    @property
    def position(self):
        return self.settings.position

    @property
    def collapsed(self):
        return self.widget.collapse_button.isChecked()

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

    def reset_state(self):
        """Resets the collapsed state of the group to the one saved in the configuration"""
        if self.collapsed and not self.settings.collapsed:
            self.expand()
        elif not self.collapsed and self.settings.collapsed:
            self.collapse()

    def _collapsed_changed(self, state):
        self.settings.collapsed = state
        self.settings.save()

    def _name_changed(self):
        if self.settings.save is Null:
            del self.settings.save # re-enable saving after the name was provided
        self.settings.name = self.widget.name_editor.text()
        self.settings.save()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookGroupDidChange(self, notification):
        if 'name' in notification.data.modified:
            self.widget.name = notification.sender.name


class ContactIconDescriptor(object):
    def __init__(self, filename):
        self.filename = filename
        self.icon = None
    def __get__(self, obj, objtype):
        if self.icon is None:
            self.icon = QIcon(ApplicationData.get(self.filename))
        return self.icon
    def __set__(self, obj, value):
        raise AttributeError("attribute cannot be set")
    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


class Contact(object):
    implements(IObserver)

    size_hint = QSize(200, 36)

    native = property(lambda self: isinstance(self.settings, addressbook.Contact))

    movable = property(lambda self: isinstance(self.settings, addressbook.Contact))
    editable = property(lambda self: isinstance(self.settings, addressbook.Contact))
    deletable = property(lambda self: isinstance(self.settings, addressbook.Contact))

    default_user_icon = ContactIconDescriptor(Resources.get('icons/default-avatar.png'))

    def __init__(self, contact, group):
        self.settings = contact
        self.group = group
        self.status = 'unknown'
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=contact)

    def __gt__(self, other):
        if isinstance(other, Contact):
            return self.name > other.name
        return NotImplemented

    def __ge__(self, other):
        if isinstance(other, Contact):
            return self.name >= other.name
        return NotImplemented

    def __lt__(self, other):
        if isinstance(other, Contact):
            return self.name < other.name
        return NotImplemented

    def __le__(self, other):
        if isinstance(other, Contact):
            return self.name <= other.name
        return NotImplemented

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.settings, self.group)

    def __reduce__(self):
        return (self.__class__, (self.settings, self.group), None)

    def __unicode__(self):
        return u'%s <%s>' % (self.name, self.uri) if self.name else self.uri

    @property
    def name(self):
        if isinstance(self.settings, BonjourNeighbour):
            return '%s (%s)' % (self.settings.name, self.settings.hostname)
        elif isinstance(self.settings, GoogleContact):
            return self.settings.name or self.settings.company
        else:
            return self.settings.name

    @property
    def uris(self):
        return self.settings.uris

    @property
    def info(self):
        return self.uri

    @property
    def uri(self):
        try:
            return next(uri.uri for uri in self.settings.uris)
        except StopIteration:
            return u''

    @property
    def icon(self):
        try:
            return self.__dict__['icon']
        except KeyError:
            if isinstance(self.settings, addressbook.Contact):
                icon_manager = IconManager()
                icon = icon_manager.get(self.settings.id) or self.default_user_icon
            elif isinstance(self.settings, GoogleContact):
                pixmap = QPixmap()
                if pixmap.loadFromData(self.settings.icon.data):
                    icon = QIcon(pixmap)
                else:
                    icon = self.default_user_icon
            else:
                icon = self.default_user_icon
            return self.__dict__.setdefault('icon', icon)

    @property
    def pixmap(self):
        try:
            return self.__dict__['pixmap']
        except KeyError:
            return self.__dict__.setdefault('pixmap', self.icon.pixmap(32))

    def _get_state(self):
        return self.__dict__['state']

    def _set_state(self, value):
        old_value = self.__dict__.get('state', Null)
        self.__dict__['state'] = value
        if old_value != value and old_value is not Null:
            notification_center = NotificationCenter()
            notification_center.post_notification('BlinkContactDidChange', sender=self, data=NotificationData(prev_state=old_value, state=value))

    state = property(_get_state, _set_state)
    del _get_state, _set_state

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactDidChange(self, notification):
        if 'icon' in notification.data.modified:
            self.__dict__.pop('icon', None)
            self.__dict__.pop('pixmap', None)
        notification.center.post_notification('BlinkContactDidChange', sender=self)


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
        self.show()

    def open_for_incorrect_password(self):
        red = '#cc0000'
        settings = SIPSimpleSettings()
        self.username_editor.setEnabled(False)
        self.username_editor.setText(settings.google_contacts.username)
        self.status_label.value = Status('Error authenticating with Google. Please enter your password:', color=red)
        self.show()

    @run_in_green_thread
    def _authorize_google_account(self):
        red = '#cc0000'
        captcha_response = self.captcha_editor.text() if self.captcha_token else None
        username = self.username_editor.text()
        password = self.password_editor.text()
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
        self.info_label.setForegroundRole(QPalette.Dark)
        # AlternateBase set to #f0f4ff or #e0e9ff

    def _get_name(self):
        return self.name_label.text()

    def _set_name(self, value):
        self.name_label.setText(value)

    #name = property(_get_name, _set_name)
    del _get_name, _set_name

    def _get_info(self):
        return self.info_label.text()

    def _set_info(self, value):
        self.info_label.setText(value)

    #info = property(_get_info, _set_info)
    del _get_info, _set_info

    def _get_icon(self):
        return self.icon_label.pixmap()

    def _set_icon(self, icon):
        self.icon_label.setPixmap(icon.pixmap(32))

    #icon = property(_get_icon, _set_icon)
    del _get_icon, _set_icon

    def init_from_contact(self, contact):
        self.name_label.setText(contact.name)
        self.info_label.setText(contact.info)
        self.icon_label.setPixmap(contact.icon.pixmap(32))

del ui_class, base_class


ui_class, base_class = uic.loadUiType(Resources.get('contact_group.ui'))

class GroupWidget(base_class, ui_class):
    def __init__(self, parent=None):
        super(GroupWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
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
        return self.name_label.text()

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
        super(GroupWidget, self).mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._disable_dnd:
            return
        super(GroupWidget, self).mouseMoveEvent(event)

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
        return super(GroupWidget, self).event(event)

del ui_class, base_class


class ContactDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super(ContactDelegate, self).__init__(parent)

        self.contact_oddline_widget  = ContactWidget(None)
        self.contact_evenline_widget = ContactWidget(None)
        self.contact_selected_widget = ContactWidget(None)

        self.contact_oddline_widget.setBackgroundRole(QPalette.Base)
        self.contact_evenline_widget.setBackgroundRole(QPalette.AlternateBase)
        self.contact_selected_widget.setBackgroundRole(QPalette.Highlight)
        self.contact_selected_widget.name_label.setForegroundRole(QPalette.HighlightedText)
        self.contact_selected_widget.info_label.setForegroundRole(QPalette.HighlightedText)

        # No theme except Oxygen honors the BackgroundRole
        palette = self.contact_oddline_widget.palette()
        palette.setColor(QPalette.Window, palette.color(QPalette.Base))
        self.contact_oddline_widget.setPalette(palette)

        palette = self.contact_evenline_widget.palette()
        palette.setColor(QPalette.Window, palette.color(QPalette.AlternateBase))
        self.contact_evenline_widget.setPalette(palette)

        palette = self.contact_selected_widget.palette()
        palette.setColor(QPalette.Window, palette.color(QPalette.Highlight))
        self.contact_selected_widget.setPalette(palette)

    def _update_list_view(self, group, collapsed):
        list_view = self.parent()
        list_items = list_view.model().items
        for position in xrange(list_items.index(group)+1, len(list_items)):
            if isinstance(list_items[position], Group):
                break
            list_view.setRowHidden(position, collapsed)

    def createEditor(self, parent, options, index):
        item = index.model().data(index, Qt.DisplayRole)
        if isinstance(item, Group):
            item.widget = GroupWidget(parent)
            item.widget.collapse_button.toggled.connect(partial(self._update_list_view, item))
            return item.widget
        else:
            return None

    def editorEvent_no(self, event, model, option, index):
        print "editor event", event, model, option, index, event.type(), event.pos(), option.rect, option.rect.adjusted(option.rect.width()-18, 0, 0, -18), option.rect.adjusted(option.rect.width()-18, 0, 0, -18).contains(event.pos())
        return super(ContactDelegate, self).editorEvent(event, model, option, index)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def paintContact(self, contact, painter, option, index):
        if option.state & QStyle.State_Selected:
            widget = self.contact_selected_widget
        elif index.row() % 2 == 1:
            widget = self.contact_evenline_widget
        else:
            widget = self.contact_oddline_widget
        item_size = option.rect.size()
        if widget.size() != item_size:
            widget.resize(item_size)
        widget.init_from_contact(contact)

        painter.save()
        pixmap = QPixmap(item_size)
        widget.render(pixmap)
        painter.drawPixmap(option.rect, pixmap)

        if contact.status not in ('offline', 'unknown'):
            status_colors = dict(available='#00ff00', away='#ffff00', busy='#ff0000')
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

    def drawExpansionIndicator(self, option, painter):
        arrow_rect = QRectF(0, 0, 18, 18)
        arrow_rect.moveTopRight(option.rect.topRight())

        text_color = option.palette.color(QPalette.WindowText if option.state & QStyle.State_AutoRaise else QPalette.ButtonText)
        button_color = option.palette.color(QPalette.Button)
        background_color = self.background_color(button_color, 0.5)

        painter.save()

        arrow = QPolygonF([QPointF(-3, -1.5), QPointF(0.5, 2.5), QPointF(4, -1.5)])
        if option.direction == Qt.LeftToRight:
            arrow.translate(-2, 1)
        else:
            arrow.translate(+2, 1)
        pen_thickness = 1.6

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(arrow_rect.center())

        painter.translate(0, +1)
        painter.setPen(QPen(self.calc_light_color(background_color), pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline(arrow)
        painter.translate(0, -1)
        painter.setPen(QPen(self.deco_color(background_color, text_color), pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline(arrow)

        painter.restore()

    def paintGroup(self, group, painter, option, index):
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

    def paint(self, painter, option, index):
        item = index.model().data(index, Qt.DisplayRole)
        handler = getattr(self, 'paint%s' % item.__class__.__name__, Null)
        handler(item, painter, option, index)

    def sizeHint(self, option, index):
        return index.model().data(index, Qt.DisplayRole).size_hint


class Operation(object):
    __params__ = ()
    __priority__ = None
    def __init__(self, **params):
        for name, value in params.iteritems():
            setattr(self, name, value)
        for param in set(self.__params__).difference(params):
            raise ValueError("missing operation parameter: '%s'" % param)
        self.timestamp = datetime.utcnow()

class AddContactOperation(Operation):
    __params__ = ('contact', 'group_ids') # store icon data as well? -Dan
    __priority__ = 0

class AddGroupOperation(Operation):
    __params__ = ('group',)
    __priority__ = 1

class AddGroupMemberOperation(Operation):
    __params__ = ('group_id', 'contact_id')
    __priority__ = 2


class RecallState(object):
    def __init__(self, obj):
        self.id = obj.id
        self.state = self._normalize_state(obj.__getstate__())

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self.id, self.state)

    def _normalize_state(self, state):
        normalized_state = {}
        for key, value in state.iteritems():
            if isinstance(value, dict):
                normalized_state[key] = self._normalize_state(value)
            elif value is not DefaultValue:
                normalized_state[key] = value
        return normalized_state


class GroupList:     __metaclass__ = MarkerType
class GroupElement:  __metaclass__ = MarkerType
class GroupContacts: __metaclass__ = MarkerType


class GroupContactList(tuple):
    def __new__(cls, *args):
        instance = tuple.__new__(cls, *args)
        instance.__contactmap__ = dict((item.settings, item) for item in instance)
        return instance

    def __contains__(self, item):
        return item in self.__contactmap__ or tuple.__contains__(self, item)

    def __getitem__(self, index):
        if isinstance(index, (int, long, slice)):
            return tuple.__getitem__(self, index)
        else:
            return self.__contactmap__[index]


class ItemList(list):
    def __init__(self, *args):
        list.__init__(self, *args)
        self.__groupmap__ = dict((item.settings, item) for item in self if isinstance(item, Group))

    def __add__(self, other):
        return self.__class__(list.__add__(self, other))

    def __contains__(self, item):
        return item in self.__groupmap__ or list.__contains__(self, item)

    def __delitem__(self, index):
        list.__delitem__(self, index)
        self.__groupmap__ = dict((item.settings, item) for item in self if isinstance(item, Group))

    def __delslice__(self, i, j):
        list.__delslice__(self, i, j)
        self.__groupmap__ = dict((item.settings, item) for item in self if isinstance(item, Group))

    def __getitem__(self, index):
        if index is GroupList:
            return [item for item in self if isinstance(item, Group)]
        elif isinstance(index, tuple):
            try:
                operation, key = index
            except ValueError:
                raise KeyError(key)
            if operation is GroupElement:
                return self.__groupmap__[key]
            elif operation is GroupContacts:
                group = key if isinstance(key, Group) else self.__groupmap__[key]
                return GroupContactList(item for item in self if isinstance(item, Contact) and item.group is group)
            else:
                raise KeyError(key)
        return list.__getitem__(self, index)

    def __iadd__(self, other):
        list.__iadd__(self, other)
        self.__groupmap__.update((item.settings, item) for item in other if isinstance(item, Group))

    def __imul__(self, factor):
        raise NotImplementedError

    def __setitem__(self, index, item):
        list.__setitem__(self, index, item)
        self.__groupmap__ = dict((item.settings, item) for item in self if isinstance(item, Group))

    def __setslice__(self, i, j, value):
        list.__setslice__(self, i, j, value)
        self.__groupmap__ = dict((item.settings, item) for item in self if isinstance(item, Group))

    def append(self, item):
        list.append(self, item)
        if isinstance(item, Group):
            self.__groupmap__[item.settings] = item

    def extend(self, iterable):
        list.extend(self, iterable)
        self.__groupmap__ = dict((item.settings, item) for item in self if isinstance(item, Group))

    def insert(self, index, item):
        list.insert(self, index, item)
        if isinstance(item, Group):
            self.__groupmap__[item.settings] = item

    def pop(self, *args):
        item = list.pop(self, *args)
        self.__groupmap__.pop(item.settings, None)

    def remove(self, item):
        list.remove(self, item)
        self.__groupmap__.pop(item.settings, None)


class ContactModel(QAbstractListModel):
    implements(IObserver)

    itemsAdded = pyqtSignal(list)
    itemsRemoved = pyqtSignal(list)

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-group-list', 'application/x-blink-contact-list', 'text/uri-list']

    def __init__(self, parent=None):
        super(ContactModel, self).__init__(parent)
        self.state = 'stopped'
        self.items = ItemList()
        self.deleted_items = []
        self.main_window = parent
        self.contact_list = parent.contact_list
        self.virtual_group_manager = VirtualGroupManager()

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationWillStart')
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        notification_center.add_observer(self, name='SIPAccountManagerDidStart')
        notification_center.add_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')
        notification_center.add_observer(self, name='AddressbookGroupWasActivated')
        notification_center.add_observer(self, name='AddressbookGroupWasDeleted')
        notification_center.add_observer(self, name='AddressbookGroupDidChange')
        notification_center.add_observer(self, name='VirtualGroupWasActivated')
        notification_center.add_observer(self, name='VirtualGroupWasDeactivated')
        notification_center.add_observer(self, name='VirtualGroupDidAddContact')
        notification_center.add_observer(self, name='VirtualGroupDidRemoveContact')
        notification_center.add_observer(self, name='BlinkContactDidChange')

    @property
    def bonjour_group(self):
        try:
            return self.items[GroupElement, BonjourNeighboursGroup()]
        except KeyError:
            return None

    @property
    def google_contacts_group(self):
        try:
            return self.items[GroupElement, GoogleContactsGroup()]
        except KeyError:
            return None

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
        return ['application/x-blink-contact-list']

    def mimeData(self, indexes):
        mime_data = QMimeData()
        contacts = [item for item in (self.items[index.row()] for index in indexes if index.isValid()) if isinstance(item, Contact)]
        groups = [item for item in (self.items[index.row()] for index in indexes if index.isValid()) if isinstance(item, Group)]
        if contacts:
            mime_data.setData('application/x-blink-contact-list', QByteArray(pickle.dumps(contacts)))
        if groups:
            mime_data.setData('application/x-blink-group-list', QByteArray(pickle.dumps(groups)))
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

    def _DH_ApplicationXBlinkGroupList(self, mime_data, action, index):
        groups = self.items[GroupList]
        group = self.items[index.row()] if index.isValid() else groups[-1]
        drop_indicator = group.widget.drop_indicator
        if group.widget.drop_indicator is None:
            return False
        selected_indexes = self.contact_list.selectionModel().selectedIndexes()
        moved_groups = set(self.items[index.row()] for index in selected_indexes if index.isValid() and self.items[index.row()].movable)
        if group is groups[0] and group in moved_groups:
            drop_group = next(group for group in groups if group not in moved_groups)
            drop_position = self.contact_list.AboveItem
        elif group is groups[-1] and group in moved_groups:
            drop_group = (group for group in reversed(groups) if group not in moved_groups).next()
            drop_position = self.contact_list.BelowItem
        elif group in moved_groups:
            position = groups.index(group)
            if drop_indicator is self.contact_list.AboveItem:
                drop_group = (group for group in reversed(groups[:position]) if group not in moved_groups).next()
                drop_position = self.contact_list.BelowItem
            else:
                drop_group = (group for group in groups[position:] if group not in moved_groups).next()
                drop_position = self.contact_list.AboveItem
        else:
            drop_group = group
            drop_position = drop_indicator
        items = self._pop_items(selected_indexes)
        groups = self.items[GroupList] # get group list again as it changed
        if drop_position is self.contact_list.AboveItem:
            position = self.items.index(drop_group)
        else:
            position = len(self.items) if drop_group is groups[-1] else self.items.index(groups[groups.index(drop_group)+1])
        self.beginInsertRows(QModelIndex(), position, position+len(items)-1)
        self.items[position:position] = items
        self.endInsertRows()
        for index, item in enumerate(items):
            if isinstance(item, Group):
                self.contact_list.openPersistentEditor(self.index(position+index))
            else:
                self.contact_list.setRowHidden(position+index, item.group.collapsed)
        bonjour_group = self.bonjour_group
        if bonjour_group in moved_groups:
            bonjour_group.reference_group = None
        self._update_group_positions()
        return True

    def _DH_ApplicationXBlinkContactList(self, mime_data, action, index):
        group = self.items[index.row()] if index.isValid() else self.items[GroupList][-1]
        if group.widget.drop_indicator is None:
            return False
        all_contacts_group = AllContactsGroup()
        movable_contacts = [self.items[index.row()] for index in self.contact_list.selectionModel().selectedIndexes() if index.isValid() and self.items[index.row()].movable]
        modified_settings = set()
        for contact in movable_contacts:
            if contact.group.settings is not all_contacts_group:
                contact.group.settings.contacts.remove(contact.settings)
                modified_settings.add(contact.group.settings)
            group.settings.contacts.add(contact.settings)
            modified_settings.add(group.settings)
        self._atomic_update(save=modified_settings)
        return True

    def _DH_TextUriList(self, mime_data, action, index):
        return False

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        from blink import Blink
        blink = Blink()
        self.state = 'starting'
        if blink.first_run:
            icon_manager = IconManager()
            def make_contact(id, name, preferred_media, uri, icon):
                icon_manager.store_file(id, icon)
                contact = addressbook.Contact(id)
                contact.name = name
                contact.preferred_media = preferred_media
                contact.uris = [addressbook.ContactURI(uri=uri)]
                contact.icon = IconDescriptor('file://' + icon)
                return contact
            test_contacts = [{'id': 'test_audio',      'name': 'Test Call',         'preferred_media': 'audio', 'uri': '3333@sip2sip.info',            'icon': Resources.get('icons/test-call.png')},
                             {'id': 'test_microphone', 'name': 'Test Microphone',   'preferred_media': 'audio', 'uri': '4444@sip2sip.info',            'icon': Resources.get('icons/test-echo.png')},
                             {'id': 'test_conference', 'name': 'Test Conference',   'preferred_media': 'chat',  'uri': 'test@conference.sip2sip.info', 'icon': Resources.get('icons/test-conference.png')},
                             {'id': 'test_zipdx',      'name': 'VUC http://vuc.me', 'preferred_media': 'audio', 'uri': '200901@login.zipdx.com',       'icon': Resources.get('icons/vuc-conference.png')}]
            test_group = addressbook.Group(id='test')
            test_group.name = 'Test'
            test_group.contacts = [make_contact(**entry) for entry in test_contacts]
            modified_settings = list(test_group.contacts) + [test_group]
            self._atomic_update(save=modified_settings)

    def _NH_SIPApplicationDidStart(self, notification):
        self.state = 'started'

    def _NH_SIPApplicationWillEnd(self, notification):
        self.state = 'stopping'

    def _NH_SIPApplicationDidEnd(self, notification):
        self.state = 'stopped'

    def _NH_AddressbookGroupWasActivated(self, notification):
        group = Group(notification.sender)
        self.addGroup(group)
        for contact in notification.sender.contacts:
            self.addContact(Contact(contact, group))

    def _NH_AddressbookGroupWasDeleted(self, notification):
        group = self.items[GroupElement, notification.sender]
        self.removeGroup(group)

    def _NH_AddressbookGroupDidChange(self, notification):
        if 'contacts' not in notification.data.modified:
            return
        group = self.items[GroupElement, notification.sender]
        group_contacts = self.items[GroupContacts, notification.sender]
        for contact in notification.data.modified['contacts'].removed:
            self.removeContact(group_contacts[contact])
        for contact in notification.data.modified['contacts'].added:
            self.addContact(Contact(contact, group))

    def _NH_VirtualGroupWasActivated(self, notification):
        self.addGroup(Group(notification.sender))

    def _NH_VirtualGroupWasDeactivated(self, notification):
        group = self.items[GroupElement, notification.sender]
        self.removeGroup(group)

    def _NH_VirtualGroupDidAddContact(self, notification):
        group = self.items[GroupElement, notification.sender]
        self.addContact(Contact(notification.data.contact, group))

    def _NH_VirtualGroupDidRemoveContact(self, notification):
        contact = self.items[GroupContacts, notification.sender][notification.data.contact]
        self.removeContact(contact)
        if notification.sender is AllContactsGroup():
            icon_manager = IconManager()
            icon_manager.remove(contact.settings.id)

    def _NH_BlinkContactDidChange(self, notification):
        index = self.index(self.items.index(notification.sender))
        self.dataChanged.emit(index, index)

    def _NH_SIPAccountManagerDidStart(self, notification):
        if notification.sender.default_account is BonjourAccount():
            groups = self.items[GroupList]
            bonjour_group = self.bonjour_group
            try:
                bonjour_group.reference_group = groups[groups.index(bonjour_group)+1]
            except IndexError:
                bonjour_group.reference_group = None
            if bonjour_group is not groups[0]:
                self.moveGroup(bonjour_group, groups[0])
            bonjour_group.expand()

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        account = notification.data.account
        old_account = notification.data.old_account
        if account is BonjourAccount():
            groups = self.items[GroupList]
            bonjour_group = self.bonjour_group
            try:
                bonjour_group.reference_group = groups[groups.index(bonjour_group)+1]
            except IndexError:
                bonjour_group.reference_group = None
            if bonjour_group is not groups[0]:
                self.moveGroup(bonjour_group, groups[0])
            bonjour_group.expand()
        elif old_account is BonjourAccount() and old_account.enabled:
            bonjour_group = self.bonjour_group
            if bonjour_group.reference_group is not None:
                self.moveGroup(bonjour_group, bonjour_group.reference_group)
                bonjour_group.reference_group = None
            bonjour_group.reset_state()

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

    @run_in_thread('file-io')
    def _atomic_update(self, save=(), delete=()):
        with addressbook.AddressbookManager.transaction():
            [item.save() for item in save]
            [item.delete() for item in delete]

    def _find_contact_insertion_point(self, contact):
        for position in xrange(self.items.index(contact.group)+1, len(self.items)):
            item = self.items[position]
            if isinstance(item, Group) or item.name > contact.name:
                break
        else:
            position = len(self.items)
        return position

    def _find_group_insertion_point(self, group):
        for item in self.items[GroupList]:
            if item.settings.position >= group.settings.position:
                position = self.items.index(item)
                break
        else:
            position = len(self.items)
        return position

    def _add_contact(self, contact):
        position = self._find_contact_insertion_point(contact)
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.insert(position, contact)
        self.endInsertRows()
        self.contact_list.setRowHidden(position, contact.group.collapsed)

    def _add_group(self, group):
        position = self._find_group_insertion_point(group)
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.insert(position, group)
        self.endInsertRows()
        self.contact_list.openPersistentEditor(self.index(position))

    def _pop_contact(self, contact):
        position = self.items.index(contact)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.items[position]
        self.endRemoveRows()
        return contact

    def _pop_group(self, group):
        start = self.items.index(group)
        end = start + len(self.items[GroupContacts, group])
        self.beginRemoveRows(QModelIndex(), start, end)
        items = self.items[start:end+1]
        del self.items[start:end+1]
        self.endRemoveRows()
        return items

    def _pop_items(self, indexes):
        items = []
        rows = set(index.row() for index in indexes if index.isValid())
        removed_groups = set(self.items[row] for row in rows if isinstance(self.items[row], Group))
        rows.update(row for row, item in enumerate(self.items) if isinstance(item, Contact) and item.group in removed_groups)
        for start, end in self.reversed_range_iterator(rows):
            self.beginRemoveRows(QModelIndex(), start, end)
            items[0:0] = self.items[start:end+1]
            del self.items[start:end+1]
            self.endRemoveRows()
        return items

    def _update_group_positions(self):
        if self.state != 'started':
            return
        groups = self.items[GroupList]
        bonjour_group = self.bonjour_group
        if bonjour_group is groups[0] and bonjour_group.reference_group is not None:
            groups.pop(0)
            groups.insert(groups.index(bonjour_group.reference_group), bonjour_group)
        for position, group in enumerate(groups):
            group.settings.position = position
            group.settings.save()

    def addContact(self, contact):
        if contact in self.items:
            return
        self._add_contact(contact)
        self.itemsAdded.emit([contact])

    def removeContact(self, contact):
        if contact not in self.items:
            return
        self._pop_contact(contact)
        self.itemsRemoved.emit([contact])

    def addGroup(self, group):
        if group in self.items or group.settings in self.items:
            return
        self._add_group(group)
        self.itemsAdded.emit([group])
        self._update_group_positions()

    def removeGroup(self, group):
        if group not in self.items:
            return
        items = self._pop_group(group)
        group.widget = Null
        self.itemsRemoved.emit(items)
        self._update_group_positions()

    def moveGroup(self, group, reference):
        groups = self.items[GroupList]
        if group not in groups or groups.index(group)+1 == (groups.index(reference) if reference in groups else len(groups)):
            return
        items = self._pop_group(group)
        position = self.items.index(reference) if reference in groups else len(self.items)
        self.beginInsertRows(QModelIndex(), position, position+len(items)-1)
        self.items[position:position] = items
        self.endInsertRows()
        self.contact_list.openPersistentEditor(self.index(position))
        self._update_group_positions()

    def removeItems(self, indexes):
        all_contacts_group = AllContactsGroup()
        removed_items = deque()
        removed_members = []
        undo_operations = []
        for item in (self.items[index.row()] for index in indexes if self.items[index.row()].deletable):
            if isinstance(item, Group):
                removed_items.appendleft(item.settings)
                undo_operations.append(AddGroupOperation(group=RecallState(item.settings)))
            elif item.group.settings is all_contacts_group:
                removed_items.append(item.settings)
                group_ids = [contact.group.settings.id for contact in self.iter_contacts() if contact.settings is item.settings and not contact.group.virtual]
                undo_operations.append(AddContactOperation(contact=RecallState(item.settings), group_ids=group_ids))
            elif item.group.settings not in removed_items:
                item.group.settings.contacts.remove(item.settings)
                removed_members.append(item.group.settings)
                undo_operations.append(AddGroupMemberOperation(group_id=item.group.settings.id, contact_id=item.settings.id))
        self.deleted_items.append(sorted(undo_operations, key=attrgetter('__priority__')))
        self._atomic_update(save=removed_members, delete=removed_items)

    def iter_contacts(self):
        return (item for item in self.items if isinstance(item, Contact))

    def iter_groups(self):
        return (item for item in self.items if isinstance(item, Group))


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

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        source_index = source_model.index(source_row, 0, source_parent)
        item = source_model.data(source_index, Qt.DisplayRole)
        if isinstance(item, Group) or not item.group.virtual:
            return False
        search_tokens = self.filterRegExp().pattern().lower().split()
        searched_item = unicode(item).lower()
        return all(token in searched_item for token in search_tokens)

    def lessThan(self, left_index, right_index):
        left_item = left_index.model().data(left_index, Qt.DisplayRole)
        right_item = right_index.model().data(right_index, Qt.DisplayRole)
        return left_item.name < right_item.name

    def supportedDropActions(self):
        return Qt.CopyAction

    def mimeTypes(self):
        return ['application/x-blink-contact-list']

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
            last_group = model.items[GroupList][-1]
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
            operation = model.deleted_items[-1][0]
            if type(operation) is AddContactOperation:
                state = operation.contact.state
                name = state.get('name', 'Contact')
            elif type(operation) is AddGroupOperation:
                state = operation.group.state
                name = state.get('name', 'Group')
            else:
                addressbook_manager = addressbook.AddressbookManager()
                try:
                    contact = addressbook_manager.get_contact(operation.contact_id)
                except KeyError:
                    name = 'Contact'
                else:
                    name = contact.name or 'Contact'
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
        elif isinstance(selected_items[0], Group):
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
        group = Group(addressbook.Group())
        group.settings.save = Null # disable saving until the user provides the name
        model = self.model()
        selection_model = self.selectionModel()
        model.addGroup(group)
        self.scrollToTop()
        group.widget.edit()
        selection_model.select(model.index(model.items.index(group)), selection_model.ClearAndSelect)

    def _AH_AddContact(self):
        model = self.model()
        groups = set()
        for index in self.selectionModel().selectedIndexes():
            item = model.data(index)
            if isinstance(item, Group) and not item.virtual:
                groups.add(item)
            elif isinstance(item, Contact) and not item.group.virtual:
                groups.add(item.group)
        preferred_group = groups.pop() if len(groups)==1 else None
        model.main_window.contact_editor_dialog.open_for_add(model.main_window.search_box.text(), preferred_group)

    def _AH_EditItem(self):
        model = self.model()
        index = self.selectionModel().selectedIndexes()[0]
        item = model.data(index)
        if isinstance(item, Group):
            self.scrollTo(index)
            item.widget.edit()
        else:
            model.main_window.contact_editor_dialog.open_for_edit(item)

    def _AH_DeleteSelection(self):
        model = self.model()
        model.removeItems(self.selectionModel().selectedIndexes())
        self.selectionModel().clearSelection()

    def _AH_UndoLastDelete(self):
        model = self.model()
        addressbook_manager = addressbook.AddressbookManager()
        icon_manager = IconManager()
        modified_settings = []
        for operation in model.deleted_items.pop():
            if type(operation) is AddContactOperation:
                contact = addressbook.Contact(operation.contact.id)
                contact.__setstate__(operation.contact.state)
                modified_settings.append(contact)
                for group_id in operation.group_ids:
                    try:
                        group = addressbook_manager.get_group(group_id)
                    except KeyError:
                        pass
                    else:
                        group.contacts.add(contact)
                        modified_settings.append(group)
                if contact.icon is not None and contact.icon.is_local:
                    icon_file = contact.icon.url[len('file://'):]
                    icon_manager.store_file(contact.id, icon_file)
            elif type(operation) is AddGroupOperation:
                group = addressbook.Group(operation.group.id)
                group.__setstate__(operation.group.state)
                modified_settings.append(group)
            elif type(operation) is AddGroupMemberOperation:
                try:
                    group = addressbook_manager.get_group(operation.group_id)
                    contact = addressbook_manager.get_contact(operation.contact_id)
                except KeyError:
                    pass
                else:
                    group.contacts.add(contact)
                    modified_settings.append(group)
        model._atomic_update(save=modified_settings)

    def _AH_StartAudioCall(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        session_manager = SessionManager()
        session_manager.start_call(contact.name, contact.uri, contact=contact, account=BonjourAccount() if isinstance(contact, BonjourNeighbour) else None)

    def _AH_StartChatSession(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def _AH_SendSMS(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def _AH_SendFiles(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def _AH_RequestRemoteDesktop(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def _AH_ShareMyDesktop(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def startDrag(self, supported_actions):
        super(ContactListView, self).startDrag(supported_actions)
        if self.needs_restore:
            for group in self.model().items[GroupList]:
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
        has_blink_groups = 'application/x-blink-group-list' in provided_mime_types
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
                    for group in model.items[GroupList]:
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
        for group in self.model().items[GroupList]:
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
        for group in model.items[GroupList]:
            group.widget.drop_indicator = None
        super(ContactListView, self).dropEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def _DH_ApplicationXBlinkGroupList(self, event, index, rect, item):
        model = self.model()
        groups = model.items[GroupList]
        for group in groups:
            group.widget.drop_indicator = None
        if not index.isValid():
            drop_groups = (groups[-1], Null)
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(model.items.index(groups[-1]))).bottom())
        elif isinstance(item, Group):
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
        groups = model.items[GroupList]
        for group in groups:
            group.widget.drop_indicator = None
        if not any(model.items[index.row()].movable for index in self.selectionModel().selectedIndexes()):
            event.accept(rect)
            return
        if not index.isValid():
            group = groups[-1]
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(model.items.index(group))).bottom())
        elif isinstance(item, Group):
            group = item
        selected_groups = set(model.items[index.row()].group for index in self.selectionModel().selectedIndexes() if model.items[index.row()].movable)
        if not group.virtual and (event.source() is not self or len(selected_groups) > 1 or group not in selected_groups):
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
            operation = source_model.deleted_items[-1][0]
            if type(operation) is AddContactOperation:
                state = operation.contact.state
                name = state.get('name', 'Contact')
            elif type(operation) is AddGroupOperation:
                state = operation.group.state
                name = state.get('name', 'Group')
            else:
                addressbook_manager = addressbook.AddressbookManager()
                try:
                    contact = addressbook_manager.get_contact(operation.contact_id)
                except KeyError:
                    name = 'Contact'
                else:
                    name = contact.name or 'Contact'
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
        model.sourceModel().removeItems(model.mapToSource(index) for index in self.selectionModel().selectedIndexes())

    def _AH_UndoLastDelete(self):
        model = self.model().sourceModel()
        addressbook_manager = addressbook.AddressbookManager()
        icon_manager = IconManager()
        modified_settings = []
        for operation in model.deleted_items.pop():
            if type(operation) is AddContactOperation:
                contact = addressbook.Contact(operation.contact.id)
                contact.__setstate__(operation.contact.state)
                modified_settings.append(contact)
                for group_id in operation.group_ids:
                    try:
                        group = addressbook_manager.get_group(group_id)
                    except KeyError:
                        pass
                    else:
                        group.contacts.add(contact)
                        modified_settings.append(group)
                if contact.icon is not None and contact.icon.is_local:
                    icon_file = contact.icon.url[len('file://'):]
                    icon_manager.store_file(contact.id, icon_file)
            elif type(operation) is AddGroupOperation:
                group = addressbook.Group(operation.group.id)
                group.__setstate__(operation.group.state)
                modified_settings.append(group)
            elif type(operation) is AddGroupMemberOperation:
                try:
                    group = addressbook_manager.get_group(operation.group_id)
                    contact = addressbook_manager.get_contact(operation.contact_id)
                except KeyError:
                    pass
                else:
                    group.contacts.add(contact)
                    modified_settings.append(group)
        model._atomic_update(save=modified_settings)

    def _AH_StartAudioCall(self):
        contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        session_manager = SessionManager()
        session_manager.start_call(contact.name, contact.uri, contact=contact, account=BonjourAccount() if isinstance(contact, BonjourNeighbour) else None)

    def _AH_StartChatSession(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def _AH_SendSMS(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def _AH_SendFiles(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def _AH_RequestRemoteDesktop(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

    def _AH_ShareMyDesktop(self):
        #contact = self.model().data(self.selectionModel().selectedIndexes()[0])
        pass

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

ui_class, base_class = uic.loadUiType(Resources.get('contact_editor.ui'))

class ContactEditorDialog(base_class, ui_class):
    def __init__(self, parent=None):
        super(ContactEditorDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.edited_contact = None
        self.target_group = None
        self.sip_address_editor.setValidator(QRegExpValidator(QRegExp("\S+"), self))
        self.sip_address_editor.textChanged.connect(self.enable_accept_button)
        self.clear_button.clicked.connect(self.reset_icon)
        self.accepted.connect(self.process_contact)

    def open_for_add(self, sip_address=u'', target_group=None):
        self.edited_contact = None
        self.target_group = target_group
        self.sip_address_editor.setText(sip_address)
        self.display_name_editor.setText(u'')
        self.icon_selector.filename = None
        self.preferred_media.setCurrentIndex(0)
        self.accept_button.setText(u'Add')
        self.accept_button.setEnabled(sip_address != u'')
        self.show()

    def open_for_edit(self, contact):
        self.edited_contact = contact
        self.sip_address_editor.setText(contact.uri)
        self.display_name_editor.setText(contact.name)
        if contact.settings.icon is not None and contact.settings.icon.is_local:
            self.icon_selector.filename = contact.settings.icon.url[len('file://'):]
        else:
            self.icon_selector.filename = None
        self.preferred_media.setCurrentIndex(self.preferred_media.findText(contact.settings.preferred_media.title()))
        self.accept_button.setText(u'Ok')
        self.accept_button.setEnabled(True)
        self.show()

    def reset_icon(self):
        self.icon_selector.filename = None

    def enable_accept_button(self, text):
        self.accept_button.setEnabled(text != u'')

    def process_contact(self):
        contact_model = self.parent().contact_model
        icon_manager = IconManager()
        if self.edited_contact is None:
            contact = addressbook.Contact()
        else:
            contact = self.edited_contact.settings
        try:
            uri = next(iter(contact.uris))
        except StopIteration:
            contact.uris.add(addressbook.ContactURI(uri=self.sip_address_editor.text()))
        else:
            uri.uri = self.sip_address_editor.text()
        contact.name = self.display_name_editor.text()
        contact.preferred_media = self.preferred_media.currentText().lower()
        if self.icon_selector.filename is not None:
            icon_file = ApplicationData.get(self.icon_selector.filename)
            icon_descriptor = IconDescriptor('file://' + icon_file)
        else:
            icon_file = icon_descriptor = None
        if contact.icon != icon_descriptor:
            if icon_file is not None:
                icon_manager.store_file(contact.id, icon_file)
            else:
                icon_manager.remove(contact.id)
            contact.icon = icon_descriptor
        modified_settings = [contact]
        if self.target_group is not None:
            self.target_group.settings.contacts.add(contact)
            modified_settings.append(self.target_group.settings)
        contact_model._atomic_update(save=modified_settings)
        self.edited_contact = None
        self.target_group = None

del ui_class, base_class


