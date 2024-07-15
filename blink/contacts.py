
import pickle as pickle
import locale
import os
import re
import socket
import sys

from PyQt5 import uic
from PyQt5.QtCore import Qt, QAbstractListModel, QAbstractTableModel, QEasingCurve, QModelIndex, QPropertyAnimation, QSortFilterProxyModel
from PyQt5.QtCore import QByteArray, QEvent, QMimeData, QPointF, QRectF, QRect, QSize, QTimer, QUrl, pyqtSignal, QT_TRANSLATE_NOOP
from PyQt5.QtGui import QBrush, QColor, QIcon, QKeyEvent, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWidgets import QAction, QApplication, QItemDelegate, QStyledItemDelegate, QStyle
from PyQt5.QtWidgets import QButtonGroup, QComboBox, QFileDialog, QHBoxLayout, QListView, QMenu, QRadioButton, QTableView, QWidget

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData, ObserverWeakrefProxy
from application.python.descriptor import WriteOnceAttribute
from application.python.threadpool import ThreadPool, run_in_threadpool
from application.python.types import MarkerType, Singleton
from application.python import Null
from application.system import makedirs, unlink
from collections import OrderedDict, deque
from datetime import datetime
from functools import partial
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from heapq import heappush
from httplib2 import Http, HttpLib2Error
from itertools import count
from oauth2client.client import OAuth2WebServerFlow, AccessTokenRefreshError
from oauth2client.file import Storage
from operator import attrgetter
from threading import Event
from urllib.parse import parse_qsl
from zope.interface import implementer

from sipsimple import addressbook
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.account.bonjour import BonjourServiceDescription
from sipsimple.configuration import ConfigurationManager, DefaultValue, Setting, SettingsState, SettingsObjectMeta, ObjectNotFoundError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import BaseSIPURI, SIPURI
from sipsimple.threading import run_in_thread

from blink.configuration.datatypes import IconDescriptor, FileURL
from blink.configuration.settings import BlinkSettings
from blink.resources import ApplicationData, Resources, IconManager
from blink.sessions import SessionManager, StreamDescription
from blink.messages import MessageManager
from blink.util import call_in_gui_thread, run_in_gui_thread, translate
from blink.widgets.buttons import SwitchViewButton
from blink.widgets.color import ColorHelperMixin
from blink.widgets.util import ContextMenuActions


__all__ = ['Group', 'Contact', 'ContactModel', 'ContactSearchModel', 'ContactListView', 'ContactSearchListView', 'ContactEditorDialog', 'URIUtils']

translation_table = dict.fromkeys(map(ord, ' \t'), None)


@implementer(IObserver)
class VirtualGroupManager(object, metaclass=Singleton):

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
        return list(self.groups.values())

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
        if not (cls.__id__ is None or isinstance(cls.__id__, str)):
            raise TypeError("%s.__id__ must be None or a string" % name)
        super(VirtualGroupMeta, cls).__init__(name, bases, dic)
        if cls.__id__ is not None:
            VirtualGroupManager.__groups__.append(cls)


class VirtualGroup(SettingsState, metaclass=VirtualGroupMeta):
    __id__ = None

    name = Setting(type=str, default='')
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
        except Exception as e:
            log.exception()
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


@implementer(IObserver)
class AllContactsGroup(VirtualGroup):

    __id__ = 'all_contacts'

    name = Setting(type=str, default='All Contacts')
    contacts = WriteOnceAttribute()

    def __init__(self):
        self.contacts = AllContactsList()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='AddressbookContactWasActivated')
        notification_center.add_observer(self, name='AddressbookContactWasDeleted')

    def __establish__(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('VirtualGroupWasActivated', sender=self, data=NotificationData(contacts=list(self.contacts)))

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactWasActivated(self, notification):
        contact = notification.sender
        notification.center.post_notification('VirtualGroupDidAddContact', sender=self, data=NotificationData(contact=contact))

    def _NH_AddressbookContactWasDeleted(self, notification):
        contact = notification.sender
        notification.center.post_notification('VirtualGroupDidRemoveContact', sender=self, data=NotificationData(contact=contact))


class MessageContact(object):
    id = WriteOnceAttribute()

    def __init__(self, name, uris, id):
        self.id = id
        self.name = name
        self.uris = DummyContactURIList(uris)
        self.presence = DummyPresence()
        self.preferred_media = PreferredMedia('messages')

    def __reduce__(self):
        return self.__class__, (self.name, self.uris)


@implementer(IObserver)
class MessageContactsManager(object, metaclass=Singleton):

    contacts = WriteOnceAttribute()

    def __init__(self):
        self.contacts = MessageContactsList()
        self.active = False
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange', sender=BlinkSettings())
        notification_center.add_observer(self, name='BlinkMessageHistoryAllContactsDidSucceed')
        notification_center.add_observer(self, name='BlinkMessageHistoryMessageDidStore')
        notification_center.add_observer(self, name='AddressbookContactWasCreated')
        notification_center.add_observer(self, name='AddressbookContactWasDeleted')

    @property
    def active(self):
        return self.__dict__['active']

    @active.setter
    def active(self, value):
        old_value = self.__dict__.get('active', False)
        new_value = self.__dict__['active'] = value
        if old_value != new_value:
            notification_center = NotificationCenter()
            if new_value:
                notification_center.post_notification('MessageContactsManagerDidActivate', sender=self)
            else:
                notification_center.post_notification('MessageContactsManagerDidDeactivate', sender=self)


    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'interface.show_messages_group' in notification.data.modified:
            if notification.sender.interface.show_messages_group:
                self.active = True
            else:
                self.active = False

    def _NH_SIPApplicationDidStart(self, notification):
        settings = BlinkSettings()
        if settings.interface.show_messages_group:
            self.active = True

    def _NH_BlinkMessageHistoryAllContactsDidSucceed(self, notification):
        contacts = notification.data.contacts
        found_contacts = []
        for (display_name, uri) in contacts:
            contact, contact_uri = URIUtils.find_contact(uri)
            if contact.type in ['dummy']:
                if not display_name:
                    display_name = uri
                contact = Contact(MessageContact(display_name, [contact_uri], uri), None)
            found_contacts.append(contact)
            try:
                self.contacts[contact.settings.id]
            except KeyError:
                self.contacts.add(contact.settings)
                notification.center.post_notification('MessageContactsManagerDidAddContact', sender=self, data=NotificationData(contact=contact.settings))
            else:
                self.contacts.add(contact.settings)
                notification.center.post_notification('MessageContactsManagerDidUpdateContact', sender=self, data=NotificationData(contact=contact.settings))
        deleted_contact_ids = self.contacts.ids - {found_contact.settings.id for found_contact in found_contacts}
        for id in deleted_contact_ids:
            contact = self.contacts.pop(id)
            notification.center.post_notification('MessageContactsManagerDidRemoveContact', sender=self, data=NotificationData(contact=contact))

    def _NH_BlinkMessageHistoryMessageDidStore(self, notification):
        if not self.active:
            return
        if notification.sender is BonjourAccount():
            return

        uri = notification.data.remote_uri
        contact, contact_uri = URIUtils.find_contact(uri)
        if contact.type in ['dummy']:
            display_name = uri
            contact = Contact(MessageContact(display_name, [contact_uri], uri), None)
        try:
            self.contacts[contact.settings.id]
        except KeyError:
            self.contacts.add(contact.settings)
            notification.center.post_notification('MessageContactsManagerDidAddContact', sender=self, data=NotificationData(contact=contact.settings))

    def _NH_AddressbookContactWasCreated(self, notification):
        contact = notification.sender
        removed = None
        for uri in contact.uris:
            try:
                removed = self.contacts.pop(uri.uri)
                notification.center.post_notification('MessageContactsManagerDidRemoveContact', sender=self, data=NotificationData(contact=removed))
            except KeyError:
                pass

        if removed:
            self.contacts.add(contact)
            notification.center.post_notification('MessageContactsManagerDidAddContact', sender=self, data=NotificationData(contact=contact))

    def _NH_AddressbookContactWasDeleted(self, notification):
        contact = notification.sender
        try:
            removed = self.contacts.pop(contact.id)
            notification.center.post_notification('MessageContactsManagerDidRemoveContact', sender=self, data=NotificationData(contact=removed))
        except KeyError:
            pass
        else:
            NotificationCenter().post_notification('BlinkMessageContactsDidChange', sender=self)


class MessageContactsList(object):
    def __init__(self):
        self._contact_map = {}

    def __getitem__(self, id):
        return self._contact_map[id]

    def __contains__(self, id):
        return id in self._contact_map

    def __iter__(self):
        return iter(list(self._contact_map.values()))

    def __len__(self):
        return len(self._contact_map)

    __hash__ = None

    @property
    def ids(self):
        return set(self._contact_map)

    def add(self, contact):
        self._contact_map[contact.id] = contact

    def pop(self, id, *args):
        return self._contact_map.pop(id, *args)

    def remove(self, contact):
        return self._contact_map.pop(contact.id, None)


@implementer(IObserver)
class MessageContactsGroup(VirtualGroup):

    __id__ = '__messages'

    name = Setting(type=str, default='Messages')
    contacts = property(lambda self: self.__manager__.contacts)

    def __init__(self):
        self.__manager__ = MessageContactsManager()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.__manager__)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_MessageContactsManagerDidActivate(self, notification):
        notification.center.post_notification('VirtualGroupWasActivated', sender=self, data=NotificationData(contacts=self.contacts))

    def _NH_MessageContactsManagerDidDeactivate(self, notification):
        notification.center.post_notification('VirtualGroupWasDeactivated', sender=self)

    def _NH_MessageContactsManagerDidAddContact(self, notification):
        notification.center.post_notification('VirtualGroupDidAddContact', sender=self, data=notification.data)

    def _NH_MessageContactsManagerDidRemoveContact(self, notification):
        notification.center.post_notification('VirtualGroupDidRemoveContact', sender=self, data=notification.data)

    def _NH_MessageContactsManagerDidUpdateContact(self, notification):
        notification.center.post_notification('VirtualContactDidChange', sender=notification.data)


class PreferredMedia(str):
    @property
    def stream_descriptions(self):
        streams = set(self.split('+'))
        if 'video' in streams:
            streams.add('audio')
        return [StreamDescription(stream) for stream in streams]

    @property
    def autoconnect(self):
        return self != 'chat' and self != 'messages'


class BonjourNeighbourID(str):
    pass


class BonjourURI(str):
    def __new__(cls, value):
        instance = str.__new__(cls, str(value).partition(':')[2])
        instance.__uri__ = value
        return instance

    @property
    def user(self):
        return self.__uri__.user

    @property
    def host(self):
        return self.__uri__.host

    @property
    def transport(self):
        return self.__uri__.transport


class BonjourNeighbourURI(object):
    def __init__(self, id, uri):
        self.id = id
        self.uri = uri

    @property
    def type(self):
        return self.uri.transport.upper()

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self.id, self.uri.__uri__)

    def __setattr__(self, name, value):
        if name == 'uri' and not isinstance(value, BonjourURI):
            value = BonjourURI(value)
        object.__setattr__(self, name, value)


class BonjourNeighbourURIList(object):
    def __init__(self, uris):
        self._uri_map = OrderedDict((uri.id, uri) for uri in uris)

    def __getitem__(self, id):
        return self._uri_map[id]

    def __contains__(self, id):
        return id in self._uri_map

    def __iter__(self):
        return iter(list(self._uri_map.values()))

    def __len__(self):
        return len(self._uri_map)

    __hash__ = None

    def get(self, key, default=None):
        return self._uri_map.get(key, default)

    def add(self, uri):
        self._uri_map[uri.id] = uri

    def pop(self, id, *args):
        return self._uri_map.pop(id, *args)

    def remove(self, uri):
        self._uri_map.pop(uri.id, None)

    @property
    def default(self):
        return sorted(self, key=lambda item: 0 if item.uri.transport == 'tls' else 1 if item.uri.transport == 'tcp' else 2)[0] if self._uri_map else None


class BonjourPresence(object):
    def __init__(self, state=None, note=None):
        self.state = state
        self.note = note


class BonjourNeighbour(object):
    id = WriteOnceAttribute()

    def __init__(self, id, name, hostname, uris, presence=None):
        self.id = BonjourNeighbourID(id) if isinstance(id, str) else id
        self.name = name
        self.hostname = hostname
        self.uris = BonjourNeighbourURIList(uris)
        self.presence = presence or BonjourPresence()
        self.preferred_media = PreferredMedia('audio')


class BonjourNeighboursList(object):
    def __init__(self):
        self._contact_map = {}

    def __getitem__(self, id):
        return self._contact_map[id]

    def __contains__(self, id):
        return id in self._contact_map

    def __iter__(self):
        return iter(list(self._contact_map.values()))

    def __len__(self):
        return len(self._contact_map)

    __hash__ = None

    def add(self, contact):
        self._contact_map[contact.id] = contact

    def pop(self, id, *args):
        return self._contact_map.pop(id, *args)

    def remove(self, contact):
        return self._contact_map.pop(contact.id, None)


@implementer(IObserver)
class BonjourNeighboursManager(object, metaclass=Singleton):

    contacts = WriteOnceAttribute()

    def __init__(self):
        self.contacts = BonjourNeighboursList()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=BonjourAccount())

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        neighbour, record = notification.data.neighbour, notification.data.record
        contact_id = record.id or neighbour
        contact_uri = BonjourNeighbourURI(neighbour, record.uri)
        try:
            contact = self.contacts[contact_id]
        except KeyError:
            contact = BonjourNeighbour(contact_id, record.name, record.host, [contact_uri], BonjourPresence(record.presence.state, record.presence.note))
            self.contacts.add(contact)
            notification.center.post_notification('BonjourNeighboursManagerDidAddContact', sender=self, data=NotificationData(contact=contact))
        else:
            contact.uris.add(contact_uri)
            notification.center.post_notification('BonjourNeighboursManagerDidUpdateContact', sender=self, data=NotificationData(contact=contact))

    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        contact_id = notification.data.record.id or notification.data.neighbour
        contact = self.contacts[contact_id]
        contact.uris.pop(notification.data.neighbour, None)
        if not contact.uris:
            self.contacts.remove(contact)
            notification.center.post_notification('BonjourNeighboursManagerDidRemoveContact', sender=self, data=NotificationData(contact=contact))
        else:
            notification.center.post_notification('BonjourNeighboursManagerDidUpdateContact', sender=self, data=NotificationData(contact=contact))

    def _NH_BonjourAccountDidUpdateNeighbour(self, notification):
        neighbour, record = notification.data.neighbour, notification.data.record
        contact = self.contacts[record.id or neighbour]
        contact_uri = contact.uris[neighbour]
        contact.name = record.name
        contact.host = record.host
        contact.presence.state = record.presence.state
        contact.presence.note = record.presence.note
        contact_uri.uri = record.uri
        notification.center.post_notification('BonjourNeighboursManagerDidUpdateContact', sender=self, data=NotificationData(contact=contact))


@implementer(IObserver)
class BonjourNeighboursGroup(VirtualGroup):

    __id__ = 'bonjour_neighbours'

    name = Setting(type=str, default='Bonjour Neighbours')
    contacts = property(lambda self: self.__manager__.contacts)

    def __init__(self):
        self.__manager__ = BonjourNeighboursManager()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=BonjourAccount())
        notification_center.add_observer(self, sender=self.__manager__)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountWillActivate(self, notification):
        notification.center.post_notification('VirtualGroupWasActivated', sender=self, data=NotificationData(contacts=[]))

    def _NH_SIPAccountDidDeactivate(self, notification):
        notification.center.post_notification('VirtualGroupWasDeactivated', sender=self)

    def _NH_BonjourNeighboursManagerDidAddContact(self, notification):
        notification.center.post_notification('VirtualGroupDidAddContact', sender=self, data=notification.data)

    def _NH_BonjourNeighboursManagerDidRemoveContact(self, notification):
        notification.center.post_notification('VirtualGroupDidRemoveContact', sender=self, data=notification.data)

    def _NH_BonjourNeighboursManagerDidUpdateContact(self, notification):
        notification.center.post_notification('VirtualContactDidChange', sender=notification.data.contact)


class GoogleContactID(str):
    pass


class GoogleContactIconMetadata(object):
    def __init__(self, metadata):
        metadata = metadata or {'source': {'id': None, 'type': None}}
        self.__dict__.update({name: GoogleContactIconMetadata(value) if isinstance(value, dict) else value for name, value in metadata.items()})

    def __getattr__(self, name):  # stop PyCharm from complaining about undefined attributes
        raise AttributeError(name)


class GoogleContactIcon(object):
    def __init__(self, url, metadata):
        self.url = url
        self.metadata = GoogleContactIconMetadata(metadata)
        self.downloaded_url = None

    @property
    def alternate_url(self):
        if self.metadata.source.type == 'CONTACT':
            return 'https://www.google.com/m8/feeds/photos/media/default/' + self.metadata.source.id
        else:
            return None

    @property
    def needs_update(self):
        return self.url != self.downloaded_url


class GoogleContactIconRetriever(object):
    threadpool = ThreadPool(name='google-icons', min_threads=1, max_threads=10)
    threadpool.start()

    def __init__(self, contact, credentials):
        self.contact = contact
        self.credentials = credentials
        self._event = Event()

    def wait(self):
        return self._event.wait()

    @run_in_threadpool(threadpool)
    def run(self):
        owner = self.contact.name or self.contact.organization or self.contact.id
        icon = self.contact.icon
        http = self.credentials.authorize(Http(timeout=5))
        try:
            if icon.url is not None:
                response, content = http.request(icon.url + '?size={}'.format(IconManager.max_size))
            else:
                response = content = None
        except (HttpLib2Error, socket.error) as e:
            log.warning('could not retrieve icon for {owner}: {exception!s}'.format(owner=owner, exception=e))
        else:
            if response is None:
                icon_manager = IconManager()
                icon_manager.store_data(self.contact.id, None)
                icon.downloaded_url = None
            elif response['status'] == '200' and response['content-type'].startswith('image/'):
                icon_manager = IconManager()
                try:
                    icon_manager.store_data(self.contact.id, content)
                except Exception as e:
                    log.error('could not store icon for {owner}: {exception!s}'.format(owner=owner, exception=e))
                else:
                    icon.downloaded_url = icon.url
            elif response['status'] in ('403', '404') and icon.alternate_url:  # private or unavailable photo. use old GData protocol if alternate_url is available.
                try:
                    response, content = http.request(icon.alternate_url, headers={'GData-Version': '3.0'})
                except (HttpLib2Error, socket.error) as e:
                    log.warning('could not retrieve icon for {owner}: {exception!s}'.format(owner=owner, exception=e))
                else:
                    if response['status'] == '200' and response['content-type'].startswith('image/'):
                        icon_manager = IconManager()
                        try:
                            icon_manager.store_data(self.contact.id, content)
                        except Exception as e:
                            log.error('could not store icon for {owner}: {exception!s}'.format(owner=owner, exception=e))
                        else:
                            icon.downloaded_url = icon.url
                    else:
                        log.error('could not retrieve icon for {} (status={}, content-type={!r})'.format(owner, response['status'], response['content-type']))
            else:
                log.error('could not retrieve icon for {} (status={}, content-type={!r})'.format(owner, response['status'], response['content-type']))
        finally:
            self._event.set()


class GoogleContactURI(object):
    id = property(lambda self: self.uri)

    def __init__(self, uri, type, default=False):
        self.uri = uri.strip() if uri is not None else uri
        self.type = type
        self.default = default

    def __repr__(self):
        return "%s(%r, %r, %r)" % (self.__class__.__name__, self.uri, self.type, self.default)

    @classmethod
    def from_number(cls, number):
        return cls(number.get('canonicalForm') or number['value'], number.get('formattedType', 'Other'), number['metadata'].get('primary', False))

    @classmethod
    def from_im(cls, address):
        return cls(re.sub('^sips?:', '', address['username']), address.get('formattedType', 'Other'), address['metadata'].get('primary', False))

    @classmethod
    def from_email(cls, address):
        return cls(re.sub('^sips?:', '', address['value']), address.get('formattedType', 'Other'), address['metadata'].get('primary', False))


class GoogleContactURIList(object):
    def __init__(self, uris):
        self._uri_map = OrderedDict((uri.id, uri) for uri in uris)

    def __getitem__(self, id):
        return self._uri_map[id]

    def __contains__(self, id):
        return id in self._uri_map

    def __iter__(self):
        return iter(list(self._uri_map.values()))

    def __len__(self):
        return len(self._uri_map)

    __hash__ = None

    def get(self, key, default=None):
        return self._uri_map.get(key, default)

    def add(self, uri):
        self._uri_map[uri.id] = uri

    def pop(self, id, *args):
        return self._uri_map.pop(id, *args)

    def remove(self, uri):
        self._uri_map.pop(uri.id, None)

    @property
    def default(self):
        return next((uri for uri in self if uri.default), None)


class GooglePresence(object):
    def __init__(self, state=None, note=None):
        self.state = state
        self.note = note


class GoogleContact(object):
    id = WriteOnceAttribute()

    def __init__(self, id, name, organization, uris, icon=None, etag=None):
        self.id = GoogleContactID(id)
        self.name = name
        self.organization = organization
        self.uris = GoogleContactURIList(uris)
        self.icon = icon
        self.etag = etag
        self.presence = GooglePresence()
        self.preferred_media = PreferredMedia('audio')

    def __reduce__(self):
        return self.__class__, (self.id, self.name, self.organization, self.uris, self.icon, self.etag)

    def __repr__(self):
        return "<GoogleContact: id={0.id!r}, name={0.name!r}, organization={0.organization!r}, uris={0.uris!r}, icon={0.icon!r}, etag={0.etag!r}>".format(self)

    def update(self, contact_data):
        assert self.id == contact_data['resourceName']

        etag = contact_data['etag']
        name = next((entry['displayName'] for entry in contact_data.get('names', Null)), None)
        organization = next((entry.get('name') for entry in contact_data.get('organizations', Null)), None)
        icon_url, icon_metadata = next(((entry['url'], entry['metadata']) for entry in contact_data.get('photos', Null)), (None, None))

        name = name.strip() if name is not None else 'Unknown'
        organization = organization.strip() if organization is not None else organization

        uris = [GoogleContactURI.from_number(number) for number in contact_data.get('phoneNumbers', Null)]
        uris.extend(GoogleContactURI.from_im(address) for address in contact_data.get('imClients', Null))
        uris.extend(GoogleContactURI.from_email(address) for address in contact_data.get('emailAddresses', Null))

        name = name if not organization else '%s (%s)' % (name, organization)
        self.name = name
        self.organization = organization
        self.uris = GoogleContactURIList(uris)
        self.icon.url = icon_url
        self.icon.metadata = GoogleContactIconMetadata(icon_metadata)
        self.etag = etag

    @classmethod
    def from_google_data(cls, contact_data):
        contact_id = contact_data['resourceName']
        etag = contact_data['etag']

        name = next((entry['displayName'] for entry in contact_data.get('names', Null)), None)
        organization = next((entry.get('name') for entry in contact_data.get('organizations', Null)), None)
        icon_url, icon_metadata = next(((entry['url'], entry['metadata']) for entry in contact_data.get('photos', Null)), (None, None))

        name = name.strip() if name is not None else translate('contact_list', 'Unknown')
        organization = organization.strip() if organization is not None else organization

        uris = [GoogleContactURI.from_number(number) for number in contact_data.get('phoneNumbers', Null)]
        uris.extend(GoogleContactURI.from_im(address) for address in contact_data.get('imClients', Null))
        uris.extend(GoogleContactURI.from_email(address) for address in contact_data.get('emailAddresses', Null))

        icon = GoogleContactIcon(icon_url, icon_metadata)
        name = name if not organization else '%s (%s)' % (name, organization)
        return cls(contact_id, name, organization, uris, icon, etag)


class GoogleContactsList(object):
    def __init__(self):
        self._contact_map = {}

    def __getitem__(self, id):
        return self._contact_map[id]

    def __contains__(self, id):
        return id in self._contact_map

    def __iter__(self):
        return iter(list(self._contact_map.values()))

    def __len__(self):
        return len(self._contact_map)

    __hash__ = None

    @property
    def ids(self):
        return set(self._contact_map)

    def add(self, contact):
        self._contact_map[contact.id] = contact

    def pop(self, id, *args):
        return self._contact_map.pop(id, *args)


class GoogleAuthorizationView(QWebEngineView):
    finished = pyqtSignal()
    accepted = pyqtSignal(str, str)  # accepted.emit(code, email)
    rejected = pyqtSignal()

    success_token = 'Success code='
    failure_token = 'Denied error=access_denied'

    def __init__(self, parent=None):
        super(GoogleAuthorizationView, self).__init__(parent)
        self.email = None
        self.setWindowTitle('Blink Google Authorization')
        self.setWindowIcon(QIcon(Resources.get('icons/blink48.png')))
        self.selectionChanged.connect(self._SH_SelectionChanged)
        self.titleChanged.connect(self._SH_TitleChanged)
        self.urlChanged.connect(self._SH_URLChanged)
        self.resize(500, 630)

    @run_in_gui_thread
    def open(self, url):
        self.load(QUrl.fromEncoded(url.encode()))
        self.show()

    def closeEvent(self, event):
        super(GoogleAuthorizationView, self).closeEvent(event)
        self.finished.emit()
        self.rejected.emit()

    def _SH_SelectionChanged(self):
        self.email = self.page().mainFrame().findFirstElement('input#Email').evaluateJavaScript('this.value') or self.email  # the input changes to None during submit

    # TODO: Check if this is still needed -- Tijmen
    def _SH_TitleChanged(self, title):
        self.setWindowTitle(title)
        if title == self.failure_token:
            self.hide()
            self.finished.emit()
            self.rejected.emit()
        elif title.startswith(self.success_token):
            code = title[len(self.success_token):]
            self.hide()
            self.finished.emit()
            self.accepted.emit(code, self.email)

    def _SH_URLChanged(self, url):
        if '127.0.0.1' in url.host():
            params = dict(parse_qsl(url.query()))
            if 'error' in params:
                self.hide()
                self.finished.emit()
                self.rejected.emit()
            elif 'code' in params:
                self.hide()
                self.finished.emit()
                self.accepted.emit(params['code'], self.email)


class GoogleAuthorizationStorage(Storage):
    def __init__(self, filename):
        self._directory = os.path.dirname(filename)
        super(GoogleAuthorizationStorage, self).__init__(filename)

    def put(self, credentials):
        makedirs(self._directory)
        super(GoogleAuthorizationStorage, self).put(credentials)


class GoogleAuthorization(object):
    client_id = '28246556873-20215d5a5ttd0l3sa7cchsm7hklh2d3c.apps.googleusercontent.com'
    client_secret = '3L8FDV5LELGmMIwr3NhfaZsq'
    redirect_uri = 'http://127.0.0.1'
    scope = 'https://www.googleapis.com/auth/contacts.readonly profile'

    def __init__(self):
        settings = SIPSimpleSettings()
        self.storage = GoogleAuthorizationStorage(ApplicationData.get('google/credentials'))
        self.flow = OAuth2WebServerFlow(client_id=self.client_id, client_secret=self.client_secret, scope=self.scope, redirect_uri=self.redirect_uri, login_hint=settings.google_contacts.username, user_agent=settings.user_agent)
        self.view = GoogleAuthorizationView()
        self.view.accepted.connect(self._SH_AuthorizationAccepted)
        self.view.rejected.connect(self._SH_AuthorizationRejected)

    @property
    def credentials(self):
        return self.storage.get()

    @property
    def email(self):
        return self.flow.login_hint

    @email.setter
    def email(self, email):
        self.flow.login_hint = email

    @run_in_thread('network-io')
    def request_credentials(self):
        credentials = self.storage.get()
        if credentials is None or credentials.invalid:
            self.view.open(self.flow.step1_get_authorize_url())
        else:
            notification_center = NotificationCenter()
            notification_center.post_notification('GoogleAuthorizationWasAccepted', sender=self, data=NotificationData(credentials=credentials, email=self.email))

    @run_in_thread('network-io')
    def _SH_AuthorizationAccepted(self, code, email):
        self.email = email
        credentials = self.flow.step2_exchange(code)
        self.storage.put(credentials)
        notification_center = NotificationCenter()
        notification_center.post_notification('GoogleAuthorizationWasAccepted', sender=self, data=NotificationData(credentials=credentials, email=email))

    @run_in_thread('network-io')
    def _SH_AuthorizationRejected(self):
        notification_center = NotificationCenter()
        notification_center.post_notification('GoogleAuthorizationWasRejected', sender=self)


@implementer(IObserver)
class GoogleContactsManager(object, metaclass=Singleton):

    def __init__(self):
        self.contacts = GoogleContactsList()
        self.running = False
        self.active = False
        self.auth = None
        self._service = None
        self._sync_timer = None
        self._sync_token = None
        self._initialize()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())

    @property
    def active(self):
        return self.__dict__['active']

    @active.setter
    def active(self, value):
        old_value = self.__dict__.get('active', False)
        new_value = self.__dict__['active'] = value
        if old_value != new_value:
            notification_center = NotificationCenter()
            if new_value:
                notification_center.post_notification('GoogleContactsManagerDidActivate', sender=self)
            else:
                notification_center.post_notification('GoogleContactsManagerDidDeactivate', sender=self)

    @run_in_gui_thread
    def _initialize(self):  # object is instantiated from a non-UI thread, while these need to be created in the UI thread
        self.auth = GoogleAuthorization()
        self._sync_timer = QTimer()
        self._sync_timer.setInterval(60 * 1000)  # a minute (in milliseconds)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self.sync_contacts)
        try:
            self.contacts, self._sync_token = pickle.load(open(ApplicationData.get('google/contacts')))
        except Exception:
            pass
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.auth)

    @run_in_gui_thread
    def _start(self):
        if not self.running:
            self.running = True
            self.auth.request_credentials()

    @run_in_gui_thread
    def _stop(self):
        if self.running:
            self.running = False
            self.auth.view.hide()
            self._sync_timer.stop()
            self._terminate()

    @run_in_thread('network-io')
    def _terminate(self):
        self.active = False

    @run_in_thread('network-io', scheduled=True)
    def sync_contacts(self):
        if not self.active:
            return

        # A person's available attributes:
        #
        # addresses, age_range, biographies, birthdays, bragging_rights, cover_photos, email_addresses, events, genders,
        # im_clients, interests, locales, memberships, metadata, names, nicknames, occupations, organizations, phone_numbers,
        # photos, relations, relationship_interests, relationship_statuses, residences, skills, taglines, urls

        person_fields = 'email_addresses,im_clients,metadata,names,organizations,phone_numbers,photos,urls'

        try:
            connections, sync_token = self._get_connections(person_fields, sync_token=self._sync_token)
        except AccessTokenRefreshError:
            self.auth.request_credentials()
            return
        except HttpError as e:
            if e.resp.status == 400 and self._sync_token is not None:  # one reason why we get 400 is that the sync token is expired
                self._sync_token = None
                self.sync_contacts()
                return
            log.warning('Could not fetch Google contacts: {!s}'.format(e))
        except (HttpLib2Error, socket.error) as e:
            log.warning('Could not fetch Google contacts: {!s}'.format(e))
        else:
            added_contacts = []
            modified_contacts = []
            deleted_contact_ids = self.contacts.ids - {contact['resourceName'] for contact in connections} if self._sync_token is None else set()

            for contact_data in connections:
                contact_id = contact_data['resourceName']
                if contact_data['metadata'].get('deleted') is True:
                    if contact_id in self.contacts:
                        deleted_contact_ids.add(contact_id)
                    continue
                try:
                    contact = self.contacts[contact_id]
                except KeyError:
                    contact = GoogleContact.from_google_data(contact_data)
                    if contact.uris:
                        added_contacts.append(contact)
                else:
                    if contact.etag != contact_data['etag']:
                        contact.update(contact_data)
                        if contact.uris:
                            modified_contacts.append(contact)
                        else:
                            deleted_contact_ids.add(contact.id)

            notification_center = NotificationCenter()
            for contact_id in deleted_contact_ids:
                contact = self.contacts.pop(contact_id)
                notification_center.post_notification('GoogleContactsManagerDidRemoveContact', sender=self, data=NotificationData(contact=contact))
            for contact in added_contacts:
                self.contacts.add(contact)
                notification_center.post_notification('GoogleContactsManagerDidAddContact', sender=self, data=NotificationData(contact=contact))
            for contact in modified_contacts:
                notification_center.post_notification('GoogleContactsManagerDidUpdateContact', sender=self, data=NotificationData(contact=contact))

            icon_retrievers = [GoogleContactIconRetriever(contact, self.auth.credentials) for contact in self.contacts if contact.icon.needs_update]
            for retriever in icon_retrievers:
                retriever.run()
            for retriever in icon_retrievers:
                retriever.wait()
                notification_center.post_notification('GoogleContactsManagerDidUpdateContact', sender=self, data=NotificationData(contact=retriever.contact))

            GoogleContactIconRetriever.threadpool.compact()

            self._sync_token = sync_token

            if added_contacts or modified_contacts or deleted_contact_ids or icon_retrievers:
                filename = ApplicationData.get('google/contacts')
                tempname = '{}.{}'.format(filename, os.getpid())
                try:
                    makedirs(os.path.dirname(filename))
                    with open(tempname, 'wb') as f:
                        pickle.dump((self.contacts, self._sync_token), f)
                    if sys.platform == 'win32':
                        unlink(filename)
                    os.rename(tempname, filename)
                except Exception as e:
                    log.error('could not save google contacts: %s' % e)

        call_in_gui_thread(self._sync_timer.start)

    def _get_connections(self, person_fields, sync_token=None):
        connections = []
        request = self._service.people().connections().list(resourceName='people/me', personFields=person_fields, syncToken=sync_token, requestSyncToken=True, pageSize=2000)
        while request is not None:
            response = request.execute()
            connections.extend(response.get('connections', []))
            sync_token = response.get('nextSyncToken', sync_token)
            request = self._service.people().connections().list_next(request, response)
        return connections, sync_token

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_GoogleAuthorizationWasAccepted(self, notification):
        settings = SIPSimpleSettings()
        settings.google_contacts.username = notification.data.email
        settings.save()
        try:
            # self._service = build('people', 'v1', credentials=notification.data.credentials, http=Http(timeout=10), cache_discovery=False)  # todo: what's the best fix for cache?
            # Http can't be used like this in this version, see https://github.com/googleapis/google-api-python-client/issues/851

            self._service = build('people', 'v1', credentials=notification.data.credentials, cache_discovery=False)  # todo: what's the best fix for cache?
        except Exception as e:
            log.error('Error fetching Google contacts: %s' % str(e))
        else:
            self.active = True
            self.sync_contacts()  # sync_contacts is always scheduled in order to not queue posting notifications until after sync_contacts finishes, when called from a notification handler

    def _NH_GoogleAuthorizationWasRejected(self, notification):
        self._service = None
        self.active = False
        self.running = False
        settings = SIPSimpleSettings()
        settings.google_contacts.enabled = False
        settings.save()

    def _NH_SIPApplicationDidStart(self, notification):
        settings = SIPSimpleSettings()
        if settings.google_contacts.enabled:
            self._start()

    def _NH_SIPApplicationWillEnd(self, notification):
        self._stop()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'google_contacts.enabled' in notification.data.modified:
            if notification.sender.google_contacts.enabled:
                self._start()
            else:
                self._stop()


@implementer(IObserver)
class GoogleContactsGroup(VirtualGroup):

    __id__ = 'google_contacts'

    name = Setting(type=str, default='Google Contacts')
    contacts = property(lambda self: self.__manager__.contacts)

    def __init__(self):
        self.__manager__ = GoogleContactsManager()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.__manager__)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_GoogleContactsManagerDidActivate(self, notification):
        notification.center.post_notification('VirtualGroupWasActivated', sender=self, data=NotificationData(contacts=list(self.contacts)))

    def _NH_GoogleContactsManagerDidDeactivate(self, notification):
        notification.center.post_notification('VirtualGroupWasDeactivated', sender=self)

    def _NH_GoogleContactsManagerDidAddContact(self, notification):
        notification.center.post_notification('VirtualGroupDidAddContact', sender=self, data=notification.data)

    def _NH_GoogleContactsManagerDidRemoveContact(self, notification):
        notification.center.post_notification('VirtualGroupDidRemoveContact', sender=self, data=notification.data)

    def _NH_GoogleContactsManagerDidUpdateContact(self, notification):
        notification.center.post_notification('VirtualContactDidChange', sender=notification.data.contact)


class DummyContactURI(object):
    id = property(lambda self: self.uri)

    def __init__(self, uri, type='', default=False):
        self.uri = uri
        self.type = type
        self.default = default

    def __repr__(self):
        return "%s(%r, %r, %r)" % (self.__class__.__name__, self.uri, self.type, self.default)


class DummyContactURIList(object):
    def __init__(self, uris):
        self._uri_map = OrderedDict((uri.id, uri) for uri in uris)

    def __getitem__(self, id):
        return self._uri_map[id]

    def __contains__(self, id):
        return id in self._uri_map

    def __iter__(self):
        return iter(list(self._uri_map.values()))

    def __len__(self):
        return len(self._uri_map)

    __hash__ = None

    def get(self, key, default=None):
        return self._uri_map.get(key, default)

    def add(self, uri):
        self._uri_map[uri.id] = uri

    def pop(self, id, *args):
        return self._uri_map.pop(id, *args)

    def remove(self, uri):
        self._uri_map.pop(uri.id, None)

    @property
    def default(self):
        try:
            return next(uri for uri in self if uri.default)
        except StopIteration:
            return None


class DummyPresence(object):
    def __init__(self, state=None, note=None):
        self.state = state
        self.note = note


class DummyContact(object):
    def __init__(self, name, uris):
        self.name = name
        self.uris = DummyContactURIList(uris)
        self.presence = DummyPresence()
        self.preferred_media = PreferredMedia('audio')

    def __reduce__(self):
        return self.__class__, (self.name, self.uris)


class RelocationInfo(object):
    def __init__(self, successor):
        self.successor = successor


@implementer(IObserver)
class Group(object):

    size_hint = QSize(200, 18)

    virtual = property(lambda self: isinstance(self.settings, VirtualGroup))

    movable = True
    editable = True
    deletable = property(lambda self: not self.virtual)

    def __init__(self, group):
        self.settings = group
        self.widget = Null
        self.saved_state = None
        self.relocation_info = None
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=group)

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.settings)

    def __getstate__(self):
        return self.settings.id, dict(widget=Null, saved_state=self.saved_state, relocation_info=self.relocation_info)

    def __setstate__(self, state):
        group_id, state = state
        if isinstance(group_id, addressbook.ID):
            manager = addressbook.AddressbookManager()
        else:
            manager = VirtualGroupManager()
        self.settings = manager.get_group(group_id)
        self.__dict__.update(state)

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
            del self.settings.save  # re-enable saving after the name was provided
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

    def __get__(self, instance, owner):
        if self.icon is None:
            self.icon = QIcon(self.filename)
            self.icon.filename = self.filename
        return self.icon

    def __set__(self, instance, value):
        raise AttributeError("attribute cannot be set")

    def __delete__(self, instance):
        raise AttributeError("attribute cannot be deleted")


@implementer(IObserver)
class Contact(object):

    size_hint = QSize(220, 42)

    native = property(lambda self: self.type == 'addressbook')

    movable = property(lambda self: self.type == 'addressbook' and self.group.settings is not MessageContactsGroup())
    editable = property(lambda self: self.type == 'addressbook')
    deletable = property(lambda self: self.type == 'addressbook')

    default_user_icon = ContactIconDescriptor(Resources.get('icons/default-avatar.png'))

    stylish_icons = True

    def __init__(self, contact, group):
        self.settings = contact
        self.group = group
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=contact)

    def __gt__(self, other):
        if isinstance(other, Contact):
            return locale.strcoll(self.name, other.name) > 0
        return NotImplemented

    def __ge__(self, other):
        if isinstance(other, Contact):
            return locale.strcoll(self.name, other.name) >= 0
        return NotImplemented

    def __lt__(self, other):
        if isinstance(other, Contact):
            return locale.strcoll(self.name, other.name) < 0
        return NotImplemented

    def __le__(self, other):
        if isinstance(other, Contact):
            return locale.strcoll(self.name, other.name) <= 0
        return NotImplemented

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.settings, self.group)

    def __getstate__(self):
        return self.settings.id, dict(group=self.group)

    def __setstate__(self, state):
        contact_id, state = state
        if isinstance(contact_id, addressbook.ID):
            group = AllContactsGroup()
        elif isinstance(contact_id, GoogleContactID):
            group = GoogleContactsGroup()
        elif isinstance(contact_id, (BonjourNeighbourID, BonjourServiceDescription)):
            group = BonjourNeighboursGroup()
        else:
            group = None
        self.settings = group.contacts[contact_id]  # problem if group is None -Dan
        self.__dict__.update(state)

    def __unicode__(self):
        return self.name or ''

    @property
    def type(self):
        try:
            return self.__dict__['type']
        except KeyError:
            if isinstance(self.settings, addressbook.Contact):
                type = 'addressbook'
            elif isinstance(self.settings, BonjourNeighbour):
                type = 'bonjour'
            elif isinstance(self.settings, GoogleContact):
                type = 'google'
            elif isinstance(self.settings, DummyContact):
                type = 'dummy'
            else:
                type = 'unknown'
            return self.__dict__.setdefault('type', type)

    @property
    def name(self):
        if self.type == 'bonjour':
            return '%s (%s)' % (self.settings.name, self.settings.hostname)
        elif self.type == 'google':
            return self.settings.name or self.settings.organization or ''
        else:
            return self.settings.name

    @property
    def unread_messages(self):
        main_window = QApplication.instance().main_window
        try:
            return main_window.unread_messages[self.uri.uri]
        except (KeyError, AttributeError):
            return 0

    @property
    def location(self):
        if self.type == 'bonjour':
            return self.settings.hostname
        else:
            return None

    @property
    def info(self):
        try:
            return self.note or (self.uri.uri.split('@')[1] if self.type == 'bonjour' else self.uri.uri)
        except (AttributeError, TypeError):
            return ''

    @property
    def uris(self):
        return self.settings.uris

    @property
    def uri(self):
        try:
            return self.settings.uris.default or next(iter(self.settings.uris))
        except StopIteration:
            return None

    @property
    def state(self):
        return self.settings.presence.state

    @property
    def note(self):
        return self.settings.presence.note

    @property
    def preferred_media(self):
        return PreferredMedia(self.settings.preferred_media)

    @property
    def icon(self):
        try:
            return self.__dict__['icon']
        except KeyError:
            if self.type == 'addressbook':
                icon_manager = IconManager()
                icon = icon_manager.get(self.settings.id + '_alt') or icon_manager.get(self.settings.id) or self.default_user_icon
            elif self.type == 'google':
                icon_manager = IconManager()
                icon = icon_manager.get(self.settings.id) or self.default_user_icon
            else:
                icon = self.default_user_icon
            return self.__dict__.setdefault('icon', icon)

    @property
    def pixmap(self):
        try:
            return self.__dict__['pixmap']
        except KeyError:
            size = 32
            if self.stylish_icons:
                pixmap = QPixmap(size, size)
                pixmap.fill(Qt.GlobalColor.transparent)
                path = QPainterPath()
                path.addRoundedRect(0, 0, size, size, 3.7, 3.7)
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                painter.setClipPath(path)
                self.icon.paint(painter, pixmap.rect(), Qt.AlignmentFlag.AlignCenter)
                painter.end()
            else:
                pixmap = self.icon.pixmap(size)
            return self.__dict__.setdefault('pixmap', pixmap)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactDidChange(self, notification):
        if {'icon', 'alternate_icon'}.intersection(notification.data.modified):
            self.__dict__.pop('icon', None)
            self.__dict__.pop('pixmap', None)
        notification.center.post_notification('BlinkContactDidChange', sender=self)

    def _NH_VirtualContactDidChange(self, notification):
        self.__dict__.pop('icon', None)
        self.__dict__.pop('pixmap', None)
        notification.center.post_notification('BlinkContactDidChange', sender=self)


@implementer(IObserver)
class ContactDetail(object):

    size_hint = QSize(200, 36)

    native = property(lambda self: self.type == 'addressbook')

    editable = property(lambda self: self.type == 'addressbook')
    deletable = property(lambda self: self.type == 'addressbook')

    default_user_icon = ContactIconDescriptor(Resources.get('icons/default-avatar.png'))

    stylish_icons = True

    def __init__(self, contact):
        self.settings = contact
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=contact)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.settings)

    def __getstate__(self):
        return self.settings.id, {}

    def __setstate__(self, state):
        contact_id, state = state
        if isinstance(contact_id, addressbook.ID):
            group = AllContactsGroup()
        elif isinstance(contact_id, GoogleContactID):
            group = GoogleContactsGroup()
        elif isinstance(contact_id, (BonjourNeighbourID, BonjourServiceDescription)):
            group = BonjourNeighboursGroup()
        else:
            group = None
        self.settings = group.contacts[contact_id]  # problem if group is None -Dan
        self.__dict__.update(state)

    def __unicode__(self):
        return self.name or ''

    @property
    def type(self):
        try:
            return self.__dict__['type']
        except KeyError:
            if isinstance(self.settings, addressbook.Contact):
                type = 'addressbook'
            elif isinstance(self.settings, BonjourNeighbour):
                type = 'bonjour'
            elif isinstance(self.settings, GoogleContact):
                type = 'google'
            elif isinstance(self.settings, DummyContact):
                type = 'dummy'
            else:
                type = 'unknown'
            return self.__dict__.setdefault('type', type)

    @property
    def name(self):
        if self.type == 'bonjour':
            return '%s (%s)' % (self.settings.name, self.settings.hostname)
        elif self.type == 'google':
            return self.settings.name or self.settings.organization or ''
        else:
            return self.settings.name

    @property
    def location(self):
        if self.type == 'bonjour':
            return self.settings.hostname
        else:
            return None

    @property
    def info(self):
        try:
            return self.note or ('@' + self.uri.uri.host if self.type == 'bonjour' else self.uri.uri)
        except AttributeError:
            return ''

    @property
    def uris(self):
        return self.settings.uris

    @property
    def uri(self):
        try:
            return self.settings.uris.default or next(iter(self.settings.uris))
        except StopIteration:
            return None

    @property
    def state(self):
        return self.settings.presence.state

    @property
    def note(self):
        return self.settings.presence.note

    @property
    def preferred_media(self):
        return PreferredMedia(self.settings.preferred_media)

    @property
    def icon(self):
        try:
            return self.__dict__['icon']
        except KeyError:
            if self.type == 'addressbook':
                icon_manager = IconManager()
                icon = icon_manager.get(self.settings.id + '_alt') or icon_manager.get(self.settings.id) or self.default_user_icon
            elif self.type == 'google':
                icon_manager = IconManager()
                icon = icon_manager.get(self.settings.id) or self.default_user_icon
            else:
                icon = self.default_user_icon
            return self.__dict__.setdefault('icon', icon)

    @property
    def pixmap(self):
        try:
            return self.__dict__['pixmap']
        except KeyError:
            size = 32
            if self.stylish_icons:
                pixmap = QPixmap(size, size)
                pixmap.fill(Qt.GlobalColor.transparent)
                path = QPainterPath()
                path.addRoundedRect(0, 0, size, size, 3.7, 3.7)
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                painter.setClipPath(path)
                self.icon.paint(painter, pixmap.rect(), Qt.AlignmentFlag.AlignCenter)
                painter.end()
            else:
                pixmap = self.icon.pixmap(size)
            return self.__dict__.setdefault('pixmap', pixmap)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactDidChange(self, notification):
        if {'icon', 'alternate_icon'}.intersection(notification.data.modified):
            self.__dict__.pop('icon', None)
            self.__dict__.pop('pixmap', None)
        notification.center.post_notification('BlinkContactDetailDidChange', sender=self)

    def _NH_VirtualContactDidChange(self, notification):
        self.__dict__.pop('icon', None)
        self.__dict__.pop('pixmap', None)
        notification.center.post_notification('BlinkContactDetailDidChange', sender=self)


@implementer(IObserver)
class ContactURI(object):

    size_hint = QSize(200, 24)

    native = property(lambda self: isinstance(self.contact, addressbook.Contact))

    editable = property(lambda self: isinstance(self.contact, addressbook.Contact))
    deletable = property(lambda self: isinstance(self.contact, addressbook.Contact))

    def __init__(self, contact, uri):
        self.contact = contact
        self.uri = uri
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=contact)

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.contact, self.uri)

    def __getstate__(self):
        if isinstance(self.contact, addressbook.Contact):
            uri_id = self.uri.id
            state_dict = dict()
        else:
            uri_id = None
            state_dict = dict(uri=self.uri)
        return self.contact.id, uri_id, state_dict

    def __setstate__(self, state):
        contact_id, uri_id, state = state
        if isinstance(contact_id, addressbook.ID):
            group = AllContactsGroup()
        elif isinstance(contact_id, GoogleContactID):
            group = GoogleContactsGroup()
        elif isinstance(contact_id, (BonjourNeighbourID, BonjourServiceDescription)):
            group = BonjourNeighboursGroup()
        else:
            group = None
        self.contact = group.contacts[contact_id]  # problem if group is None -Dan
        if uri_id is not None:
            self.uri = self.contact.uris[uri_id]
        self.__dict__.update(state)

    def __unicode__(self):
        return '%s (%s)' % (self.uri.uri, self.uri.type) if self.uri.type else str(self.uri.uri)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactDidChange(self, notification):
        modified_uris    = notification.data.modified.get('uris', Null)
        modified_default = notification.data.modified.get('uris.default', Null)
        if self.uri.id in modified_uris.modified or self.uri in (modified_default.old, modified_default.new) and self.uri not in modified_uris.removed:
            notification.center.post_notification('BlinkContactURIDidChange', sender=self)


ui_class, base_class = uic.loadUiType(Resources.get('contact.ui'))


class ContactWidget(base_class, ui_class):
    def __init__(self, parent=None):
        super(ContactWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.info_label.setForegroundRole(QPalette.ColorRole.Dark)
        # AlternateBase set to #f0f4ff or #e0e9ff

    def paintEvent(self, event):
        super(ContactWidget, self).paintEvent(event)
        if self.backgroundRole() == QPalette.ColorRole.Highlight and self.state_label.state is not None:
            rect = self.state_label.geometry()
            rect.setWidth(self.width() - rect.x())
            gradient = QLinearGradient(0, 0, 1, 0)
            gradient.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectBoundingMode)
            gradient.setColorAt(0.0, Qt.GlobalColor.transparent)
            gradient.setColorAt(1.0, Qt.GlobalColor.white)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(rect, QBrush(gradient))
            painter.end()

    def init_from_contact(self, contact):
        self.name_label.setText(contact.name)
        self.info_label.setText(contact.info)
        self.icon_label.setPixmap(contact.pixmap)
        self.state_label.state = contact.state
        try:
            self.unread_label.setText(str(contact.unread_messages))
            # self.unread_label.setToolTip(translate('contact_list', '%s unread messages') % str(contact.unread_messages))
            self.unread_label.setVisible(bool(contact.unread_messages))
        except AttributeError:
            self.unread_label.setVisible(False)
        else:
            background_color = self.state_label.state_colors['available'].stroke
            self.unread_label.setStyleSheet(f'color: #ffffff; padding: 4px; font-weight: bold; background-color: {background_color.name()}; border-radius: 4px;')


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
        # self.name_label.setForegroundRole(QPalette.ColorRole.BrightText if value else QPalette.ColorRole.WindowText)
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

    def edit(self):
        self._start_editing()

    def _start_editing(self):
        # self.name_editor.setText(self.name_label.text())
        self.name_editor.selectAll()
        self.name_view.setCurrentWidget(self.editor_widget)
        self.name_editor.setFocus()

    def _end_editing(self):
        self.name_label.setText(self.name_editor.text())
        self.name_view.setCurrentWidget(self.label_widget)

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
        rect = self.rect()

        background = QLinearGradient(0, 0, self.width(), self.height())
        if self.selected:
            background.setColorAt(0.0, QColor('#cacaca'))
            background.setColorAt(1.0, QColor('#b4b4b4'))
            upper_color = QColor('#f0f0f0')
            lower_color = QColor('#a4a4a4')
            foreground = QColor('#ffffff')
        else:
            background.setColorAt(0.0, QColor('#eeeeee'))
            background.setColorAt(1.0, QColor('#d8d8d8'))
            upper_color = QColor('#f8f8f8')
            lower_color = QColor('#c4c4c4')
            foreground = QColor('#888888')

        painter.fillRect(rect, QBrush(background))
        painter.setPen(upper_color)
        painter.drawLine(rect.topLeft(), rect.topRight())
        painter.setPen(lower_color)
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
        if self.drop_indicator is ContactListView.DropIndicatorPosition.AboveItem:
            line_rect = QRectF(rect.adjusted(18, 0, 0, 5 - rect.height()))
            arc_rect = line_rect.adjusted(-5, -3, -line_rect.width(), -3)
            path = QPainterPath(line_rect.topRight())
            path.lineTo(line_rect.topLeft())
            path.arcTo(arc_rect, 0, -180)
            painter.drawPath(path)
        elif self.drop_indicator is ContactListView.DropIndicatorPosition.BelowItem:
            line_rect = QRectF(rect.adjusted(18, rect.height() - 5, 0, 0))
            arc_rect = line_rect.adjusted(-5, 2, -line_rect.width(), 2)
            path = QPainterPath(line_rect.bottomRight())
            path.lineTo(line_rect.bottomLeft())
            path.arcTo(arc_rect, 0, 180)
            painter.drawPath(path)
        elif self.drop_indicator is ContactListView.DropIndicatorPosition.OnItem:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)

        if self.collapse_button.isChecked():
            arrow = QPolygonF([QPointF(0, 0), QPointF(0, 9), QPointF(8, 4.5)])
            arrow.translate(QPointF(5, 4))
        else:
            arrow = QPolygonF([QPointF(0, 0), QPointF(9, 0), QPointF(4.5, 8)])
            arrow.translate(QPointF(5, 5))
        painter.setBrush(foreground)
        painter.setPen(QPen(painter.brush(), 0, Qt.PenStyle.NoPen))
        painter.drawPolygon(arrow)

        painter.end()

    def event(self, event):
        if type(event) is QKeyEvent and self.editing:
            return True  # do not propagate keyboard events while editing
        elif type(event) is QMouseEvent and event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            self._start_editing()
        return super(GroupWidget, self).event(event)


del ui_class, base_class


class ContactDelegate(QStyledItemDelegate, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ContactDelegate, self).__init__(parent)

        self.contact_oddline_widget  = ContactWidget(None)
        self.contact_evenline_widget = ContactWidget(None)
        self.contact_selected_widget = ContactWidget(None)

        self.contact_oddline_widget.setBackgroundRole(QPalette.ColorRole.Base)
        self.contact_oddline_widget.setForegroundRole(QPalette.ColorRole.WindowText)
        self.contact_evenline_widget.setBackgroundRole(QPalette.ColorRole.AlternateBase)
        self.contact_evenline_widget.setForegroundRole(QPalette.ColorRole.WindowText)
        self.contact_selected_widget.setBackgroundRole(QPalette.ColorRole.Highlight)
        self.contact_selected_widget.setForegroundRole(QPalette.ColorRole.HighlightedText)
        self.contact_selected_widget.name_label.setForegroundRole(QPalette.ColorRole.HighlightedText)
        self.contact_selected_widget.info_label.setForegroundRole(QPalette.ColorRole.HighlightedText)

        # No theme except Oxygen honors the BackgroundRole
        palette = self.contact_oddline_widget.palette()
        palette.setColor(QPalette.ColorRole.Window, palette.color(QPalette.ColorRole.Base))
        self.contact_oddline_widget.setPalette(palette)

        palette = self.contact_evenline_widget.palette()
        palette.setColor(QPalette.ColorRole.Window, palette.color(QPalette.ColorRole.AlternateBase))
        self.contact_evenline_widget.setPalette(palette)

        palette = self.contact_selected_widget.palette()
        palette.setColor(QPalette.ColorRole.Window, palette.color(QPalette.ColorRole.Highlight))
        self.contact_selected_widget.setPalette(palette)

    def _update_list_view(self, group, collapsed):
        list_view = self.parent()
        list_items = list_view.model().items
        for position in range(list_items.index(group) + 1, len(list_items)):
            if isinstance(list_items[position], Group):
                break
            list_view.setRowHidden(position, collapsed)

    def createEditor(self, parent, options, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(item, Group):
            item.widget = GroupWidget(parent)
            item.widget.collapse_button.toggled.connect(partial(self._update_list_view, item))  # the partial still creates a memory cycle -Dan
            return item.widget
        else:
            return None

    def editorEvent(self, event, model, option, index):
        arrow_rect = QRect(0, 0, 14, option.rect.height())
        arrow_rect.moveTopRight(option.rect.topRight())
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier and arrow_rect.contains(event.pos()):
            model.contact_list.detail_model.contact = index.data(Qt.ItemDataRole.UserRole).settings
            detail_view = model.contact_list.detail_view
            detail_view.animation.setDirection(QPropertyAnimation.Direction.Forward)
            detail_view.animation.setStartValue(option.rect)
            detail_view.animation.setEndValue(model.contact_list.geometry())
            detail_view.raise_()
            detail_view.show()
            detail_view.animation.start()
            return True
        return super(ContactDelegate, self).editorEvent(event, model, option, index)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def paintContact(self, contact, painter, option, index):
        if option.state & QStyle.StateFlag.State_Selected:
            widget = self.contact_selected_widget
        elif index.row() % 2 == 1:
            widget = self.contact_evenline_widget
        else:
            widget = self.contact_oddline_widget
        item_size = option.rect.size()
        widget.setFixedSize(item_size)
        widget.init_from_contact(contact)

        painter.save()
        pixmap = QPixmap(item_size)
        widget.render(pixmap)
        painter.drawPixmap(option.rect, pixmap)

        if option.state & QStyle.StateFlag.State_MouseOver:
            self.drawExpansionIndicator(contact, option, painter, widget)

        if 0 and (option.state & QStyle.StateFlag.State_MouseOver):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(option.rect, QColor(240, 244, 255, 40))
            else:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                painter.fillRect(option.rect, QColor(240, 244, 255, 230))

        painter.restore()

    def drawExpansionIndicator(self, contact, option, painter, widget):
        pen_thickness = 1.6

        if contact.state is not None:
            foreground_color = option.palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
            background_color = widget.state_label.state_colors[contact.state]
            base_contrast_color = self.calc_light_color(background_color)
            gradient = QLinearGradient(0, 0, 1, 0)
            gradient.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectBoundingMode)
            gradient.setColorAt(0.0, self.color_with_alpha(base_contrast_color, 0.3 * 255))
            gradient.setColorAt(1.0, self.color_with_alpha(base_contrast_color, 0.8 * 255))
            contrast_color = QBrush(gradient)
        else:
            # foreground_color = option.palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
            # background_color = option.palette.color(QPalette.ColorRole.Window)
            foreground_color = widget.palette().color(QPalette.ColorGroup.Normal, widget.foregroundRole())
            background_color = widget.palette().color(widget.backgroundRole())
            contrast_color = self.calc_light_color(background_color)
        line_color = self.deco_color(background_color, foreground_color)

        pen = QPen(line_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        contrast_pen = QPen(QBrush(contrast_color), pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

        # this fits best with a state_label of width 14
        arrow_rect = QRect(0, 0, 14, 14)
        arrow_rect.moveBottomRight(widget.state_label.geometry().bottomRight())
        arrow_rect.translate(option.rect.topLeft())

        arrow = QPolygonF([QPointF(-3, -1.5), QPointF(0.5, 2.5), QPointF(4, -1.5)])
        arrow.translate(1, 1)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(arrow_rect.center())
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawPolyline(arrow)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawPolyline(arrow)
        painter.restore()

    def paintGroup(self, group, painter, option, index):
        if group.widget.size() != option.rect.size():
            # For some reason updateEditorGeometry only receives the peak value
            # of the size that the widget ever had, so it will never shrink it.
            group.widget.resize(option.rect.size())
        group.widget.selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if option.state & QStyle.StateFlag.State_Selected and not option.state & QStyle.StateFlag.State_HasFocus:
            # This condition is met when dragging is started on this group.
            # We use this to to draw the dragged item image.
            painter.save()
            pixmap = QPixmap(option.rect.size())
            group.widget.render(pixmap)
            painter.drawPixmap(option.rect, pixmap)
            painter.restore()

    def paint(self, painter, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        handler = getattr(self, 'paint%s' % item.__class__.__name__, Null)
        handler(item, painter, option, index)

    def sizeHint(self, option, index):
        return index.data(Qt.ItemDataRole.SizeHintRole)


class ContactDetailDelegate(QStyledItemDelegate, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ContactDetailDelegate, self).__init__(parent)
        self.widget = ContactWidget(None)
        self.widget.setBackgroundRole(QPalette.ColorRole.Base)
        # No theme except Oxygen honors the BackgroundRole
        palette = self.widget.palette()
        palette.setColor(QPalette.ColorRole.Window, palette.color(QPalette.ColorRole.Base))
        self.widget.setPalette(palette)

    def editorEvent(self, event, model, option, index):
        arrow_rect = QRect(0, 0, 14, option.rect.height())
        arrow_rect.moveTopRight(option.rect.topRight())
        if index.row() == 0 and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier and arrow_rect.contains(event.pos()):
            detail_view = self.parent()
            detail_view.animation.setDirection(QPropertyAnimation.Direction.Backward)
            detail_view.animation.start()
            return True
        return super(ContactDetailDelegate, self).editorEvent(event, model, option, index)

    def paintContactDetail(self, contact, painter, option, index):
        widget = self.widget
        item_size = option.rect.size()
        widget.setFixedSize(item_size)
        widget.init_from_contact(contact)

        painter.save()
        pixmap = QPixmap(item_size)
        widget.render(pixmap)
        painter.drawPixmap(option.rect, pixmap)

        self.drawCollapseIndicator(contact, option, painter, widget)

        painter.restore()

    def paintContactURI(self, contact_uri, painter, option, index):
        widget = option.widget
        style = widget.style()

        painter.save()
        painter.setClipRect(option.rect)

        # draw the background
        style.proxy().drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, widget)

        # draw the check mark
        if option.features & option.ViewItemFeature.HasCheckIndicator:
            self.drawCheckMark(option, painter, widget)

        # draw the icon
        mode = QIcon.Mode.Disabled if not option.state & QStyle.StateFlag.State_Enabled else QIcon.Mode.Selected if option.state & QStyle.StateFlag.State_Selected else QIcon.Mode.Normal
        state = QIcon.State.On if option.state & QStyle.StateFlag.State_Open else QIcon.State.Off
        icon_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemDecoration, option, widget)
        option.icon.paint(painter, icon_rect, option.decorationAlignment, mode, state)

        # draw the text
        if contact_uri.uri.uri:
            color_group = QPalette.ColorGroup.Disabled if not option.state & QStyle.StateFlag.State_Enabled else QPalette.ColorGroup.Normal if option.state & QStyle.StateFlag.State_Active else QPalette.ColorGroup.Inactive
            text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, widget)
            text_rect.setRight(option.rect.right() - 5)
            if contact_uri.uri.type:
                painter.setPen(option.palette.color(color_group, QPalette.ColorRole.HighlightedText if option.state & QStyle.StateFlag.State_Selected else QPalette.ColorRole.Dark))
                painter.drawText(text_rect, Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, contact_uri.uri.type)
                text_rect.adjust(0, 0, -option.fontMetrics.size(Qt.TextFlag.TextSingleLine, contact_uri.uri.type).width() - 5, 0)
            text_color = option.palette.color(color_group, QPalette.ColorRole.HighlightedText if option.state & QStyle.StateFlag.State_Selected else QPalette.ColorRole.Text)
            text_width = text_rect.width()
            if option.fontMetrics.size(Qt.TextFlag.TextSingleLine, contact_uri.uri.uri).width() > text_width:
                fade_start = 1 - 50.0 / text_width if text_width > 50 else 0.0
                gradient = QLinearGradient(text_rect.x(), 0, text_rect.right(), 0)
                gradient.setColorAt(fade_start, text_color)
                gradient.setColorAt(1.0, Qt.GlobalColor.transparent)
                painter.setClipRect(text_rect)
                painter.setPen(QPen(QBrush(gradient), 1.0))
            else:
                painter.setPen(text_color)
            painter.drawText(text_rect, Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, contact_uri.uri.uri)

        painter.restore()

    def drawCollapseIndicator(self, contact, option, painter, widget):
        pen_thickness = 1.6

        if contact.state is not None:
            foreground_color = option.palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
            background_color = widget.state_label.state_colors[contact.state]
            base_contrast_color = self.calc_light_color(background_color)
            gradient = QLinearGradient(0, 0, 1, 0)
            gradient.setCoordinateMode(QLinearGradient.ObjectBoundingMode)
            gradient.setColorAt(0.0, self.color_with_alpha(base_contrast_color, 0.3 * 255))
            gradient.setColorAt(1.0, self.color_with_alpha(base_contrast_color, 0.8 * 255))
            contrast_color = QBrush(gradient)
        else:
            foreground_color = widget.palette().color(QPalette.ColorGroup.Normal, widget.foregroundRole())
            background_color = widget.palette().color(widget.backgroundRole())
            contrast_color = self.calc_light_color(background_color)
        line_color = self.deco_color(background_color, foreground_color)

        pen = QPen(line_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        contrast_pen = QPen(contrast_color, pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

        # this fits best with a state_label of width 14
        arrow_rect = QRect(0, 0, 14, 14)
        arrow_rect.moveBottomRight(widget.state_label.geometry().bottomRight())
        arrow_rect.translate(option.rect.topLeft())

        arrow = QPolygonF([QPointF(3, 1.5), QPointF(-0.5, -2.5), QPointF(-4, 1.5)])
        arrow.translate(2, 1)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(arrow_rect.center())
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawPolyline(arrow)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawPolyline(arrow)
        painter.restore()

    def drawCheckMark(self, option, painter, widget):
        if option.checkState == Qt.CheckState.Unchecked:
            return

        palette = option.palette
        rect = widget.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, widget)

        x = int(rect.center().x() - 3.5)
        y = int(rect.center().y() - 2.5)

        pen_thickness = 2.0
        color = palette.color(QPalette.ColorRole.WindowText)
        background = palette.color(QPalette.ColorRole.Highlight if option.state & QStyle.StateFlag.State_Selected else QPalette.ColorRole.Window)
        pen = QPen(self.deco_color(background, color), pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        contrast_pen = QPen(self.calc_light_color(background), pen_thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

        if option.checkState == Qt.CheckState.PartiallyChecked:
            dashes = [1.0, 2.0]
            pen_thickness = 1.3
            pen.setWidthF(pen_thickness)
            contrast_pen.setWidthF(pen_thickness)
            pen.setDashPattern(dashes)
            contrast_pen.setDashPattern(dashes)

        offset = min(pen_thickness, 1.0)

        painter.save()
        painter.translate(0, -1)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(contrast_pen)
        painter.translate(0, offset)
        painter.drawLine(x + 9, y, x + 3, y + 7)
        painter.drawLine(x, y + 4, x + 3, y + 7)
        painter.setPen(pen)
        painter.translate(0, -offset)
        painter.drawLine(x + 9, y, x + 3, y + 7)
        painter.drawLine(x, y + 4, x + 3, y + 7)
        painter.restore()

    def paint(self, painter, option, index):
        self.initStyleOption(option, index)
        item = index.data(Qt.ItemDataRole.UserRole)
        handler = getattr(self, 'paint%s' % item.__class__.__name__, Null)
        handler(item, painter, option, index)

    def sizeHint(self, option, index):
        return index.data(Qt.ItemDataRole.SizeHintRole)


class Operation(object):
    __params__ = ()
    __priority__ = None

    def __init__(self, **params):
        for name, value in params.items():
            setattr(self, name, value)
        for param in set(self.__params__).difference(params):
            raise ValueError("missing operation parameter: '%s'" % param)
        self.timestamp = datetime.utcnow()


class AddContactOperation(Operation):
    __params__ = ('contact', 'group_ids', 'icon', 'alternate_icon')
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
        for key, value in state.items():
            if isinstance(value, dict):
                normalized_state[key] = self._normalize_state(value)
            elif value is not DefaultValue:
                normalized_state[key] = value
        return normalized_state


class GroupList(metaclass=MarkerType):     pass
class GroupElement(metaclass=MarkerType):  pass
class GroupContacts(metaclass=MarkerType): pass


class GroupContactList(tuple):
    def __new__(cls, *args):
        instance = tuple.__new__(cls, *args)
        instance.__contactmap__ = dict((item.settings, item) for item in instance)
        return instance

    def __contains__(self, item):
        return item in self.__contactmap__ or tuple.__contains__(self, item)

    def __getitem__(self, index):
        if isinstance(index, (int, slice)):
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
                raise KeyError(index)
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
        return self

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


@implementer(IObserver)
class ContactModel(QAbstractListModel):

    itemsAdded = pyqtSignal(list)
    itemsRemoved = pyqtSignal(list)

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-group-list', 'application/x-blink-contact-list', 'text/uri-list']
    # TODO: Maybe translate? -Tijmen
    test_contacts = (dict(id='test_call',       name='Test Call',       preferred_media='audio+chat', uri='echo@conference.sip2sip.info', icon=Resources.get('icons/test-call.png')),
                     dict(id='test_conference', name='Test Conference', preferred_media='audio+chat', uri='test@conference.sip2sip.info', icon=Resources.get('icons/test-conference.png')))

    def __init__(self, parent=None):
        super(ContactModel, self).__init__(parent)
        self.state = 'stopped'
        self.items = ItemList()
        self.deleted_items = []
        self.contact_list = parent.contact_list
        self.virtual_group_manager = VirtualGroupManager()

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationWillStart')
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        notification_center.add_observer(self, name='SIPApplicationDidEnd')
        notification_center.add_observer(self, name='SIPAccountManagerDidStart')
        notification_center.add_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')
        notification_center.add_observer(self, name='AddressbookContactDidChange')
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
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsEditable
        else:
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item
        elif role == Qt.ItemDataRole.SizeHintRole:
            return item.size_hint
        elif role == Qt.ItemDataRole.DisplayRole:
            return str(item)
        return None

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction

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
        if action == Qt.DropAction.IgnoreAction:
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
            drop_position = self.contact_list.DropIndicatorPosition.AboveItem
        elif group is groups[-1] and group in moved_groups:
            drop_group = next(group for group in reversed(groups) if group not in moved_groups)
            drop_position = self.contact_list.DropIndicatorPosition.BelowItem
        elif group in moved_groups:
            position = groups.index(group)
            if drop_indicator is self.contact_list.DropIndicatorPosition.AboveItem:
                drop_group = next(group for group in reversed(groups[:position]) if group not in moved_groups)
                drop_position = self.contact_list.BelowItem
            else:
                drop_group = next(group for group in groups[position:] if group not in moved_groups)
                drop_position = self.contact_list.DropIndicatorPosition.AboveItem
        else:
            drop_group = group
            drop_position = drop_indicator
        items = self._pop_items(selected_indexes)
        groups = self.items[GroupList]  # get group list again as it changed
        if drop_position is self.contact_list.DropIndicatorPosition.AboveItem:
            position = self.items.index(drop_group)
        else:
            position = len(self.items) if drop_group is groups[-1] else self.items.index(groups[groups.index(drop_group) + 1])
        self.beginInsertRows(QModelIndex(), position, position + len(items) - 1)
        self.items[position:position] = items
        self.endInsertRows()
        for index, item in enumerate(items):
            if isinstance(item, Group):
                self.contact_list.openPersistentEditor(self.index(position + index))
            else:
                self.contact_list.setRowHidden(position + index, item.group.collapsed)
        bonjour_group = self.bonjour_group
        if bonjour_group in moved_groups:
            bonjour_group.relocation_info = None
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
        if not index.isValid():
            return False
        item = self.items[index.row()]
        if not isinstance(item, Contact):
            return False

        # TODO: support directories? -Saul
        files = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile() and os.path.isfile(url.toLocalFile())]
        if not files:
            return False

        contact = item
        session_manager = SessionManager()
        for filename in files:
            session_manager.send_file(contact, contact.uri, filename)

        return True

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        from blink import Blink
        self.state = 'starting'
        blink = Blink()
        if blink.first_run:
            test_group = addressbook.Group(id='test')
            test_group.name = 'Test'
            test_group.contacts = [self._create_contact(**entry) for entry in self.test_contacts]
            changed_items = list(test_group.contacts) + [test_group]
            self._atomic_update(save=changed_items)
        else:
            addressbook_manager = addressbook.AddressbookManager()

            # upgrade test contacts if test_call doesn't exist but test_audio and/or test_microphone do (test_call replaced test_audio + test_microphone). to be removed later -Dan

            obsolete_contacts = [contact for contact in addressbook_manager.get_contacts() if contact.id in {'test_audio', 'test_microphone'}]
            need_upgrade  = bool(obsolete_contacts and not addressbook_manager.has_contact('test_call'))

            changed_items = deque()
            deleted_items = obsolete_contacts if need_upgrade else []

            if need_upgrade:
                try:
                    test_group = addressbook_manager.get_group('test')
                except KeyError:
                    test_group = addressbook.Group(id='test')
                    test_group.name = 'Test'
                changed_items.append(test_group)
                for entry in self.test_contacts:
                    try:
                        contact = addressbook_manager.get_contact(entry['id'])
                    except KeyError:
                        contact = self._create_contact(**entry)
                    else:
                        self._update_contact(contact, **entry)
                    test_group.contacts.add(contact)
                    changed_items.appendleft(contact)
            else:
                for entry in self.test_contacts:
                    try:
                        contact = addressbook_manager.get_contact(entry['id'])
                    except KeyError:
                        continue
                    else:
                        if self._update_contact(contact, icon=entry['icon']):
                            changed_items.appendleft(contact)
            self._atomic_update(save=changed_items, delete=deleted_items)

    def _NH_SIPApplicationDidStart(self, notification):
        self.state = 'started'
        self._update_group_positions()

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
        group = Group(notification.sender)
        self.addGroup(group)
        for contact in notification.data.contacts:
            self.addContact(Contact(contact, group))

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
            icon_manager.remove(contact.settings.id + '_alt')
        elif notification.sender is GoogleContactsGroup():
            icon_manager = IconManager()
            icon_manager.remove(contact.settings.id)

    def _NH_BlinkContactDidChange(self, notification):
        contact = notification.sender
        position = self.items.index(contact)
        move_point = self._find_contact_move_point(contact)
        if move_point is not None:
            self.beginMoveRows(QModelIndex(), position, position, QModelIndex(), move_point)
            del self.items[position]
            self.items.insert(self._find_contact_insertion_point(contact), contact)
            self.endMoveRows()
        index = self.index(self.items.index(contact))
        self.dataChanged.emit(index, index)

    def _NH_SIPAccountManagerDidStart(self, notification):
        if notification.sender.default_account is BonjourAccount():
            groups = self.items[GroupList]
            bonjour_group = self.bonjour_group
            try:
                bonjour_group.relocation_info = RelocationInfo(successor=groups[groups.index(bonjour_group) + 1])
            except IndexError:
                bonjour_group.relocation_info = RelocationInfo(successor=None)
            if bonjour_group is not groups[0]:
                self.moveGroup(bonjour_group, successor=groups[0])
            bonjour_group.expand()

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        account = notification.data.account
        old_account = notification.data.old_account
        if account is BonjourAccount():
            groups = self.items[GroupList]
            bonjour_group = self.bonjour_group
            try:
                bonjour_group.relocation_info = RelocationInfo(successor=groups[groups.index(bonjour_group) + 1])
            except IndexError:
                bonjour_group.relocation_info = RelocationInfo(successor=None)
            if bonjour_group is not groups[0]:
                self.moveGroup(bonjour_group, successor=groups[0])
            bonjour_group.expand()
        elif old_account is BonjourAccount() and old_account.enabled:
            bonjour_group = self.bonjour_group
            if bonjour_group.relocation_info is not None:
                self.moveGroup(bonjour_group, successor=bonjour_group.relocation_info.successor)
                bonjour_group.relocation_info = None
            bonjour_group.reset_state()

    def _NH_AddressbookContactDidChange(self, notification):
        # make sure the presence policy and subscribe flag are synchronized
        contact = notification.sender
        if contact.presence.policy == 'default':
            contact.presence.policy = 'allow' if contact.presence.subscribe else 'block'
            contact.save()
        elif contact.presence.subscribe != (True if contact.presence.policy == 'allow' else False):
            contact.presence.subscribe = True if contact.presence.policy == 'allow' else False
            contact.save()

    @staticmethod
    def range_iterator(indexes):
        """Return contiguous ranges from indexes"""
        start = last = None
        for index in sorted(indexes):
            if start is None:
                start = index
            elif index - last > 1:
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
            elif last - index > 1:
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

    def _create_contact(self, id, name, preferred_media, uri, icon):
        contact = addressbook.Contact(id)
        contact.name = name
        contact.preferred_media = preferred_media
        contact.uris = [addressbook.ContactURI(uri=uri, type='SIP')]
        contact.icon = IconDescriptor(FileURL(icon), str(int(os.stat(icon).st_mtime)))
        icon_manager = IconManager()
        icon_manager.store_file(id, icon)
        return contact

    def _update_contact(self, contact, **data):
        modified = False
        if 'name' in data:
            contact.name = data['name']
            modified = True
        if 'preferred_media' in data:
            contact.preferred_media = data['preferred_media']
            modified = True
        if 'uri' in data and data['uri'] not in {uri.uri for uri in contact.uris}:
            uri = addressbook.ContactURI(uri=data['uri'], type='SIP')
            contact.uris.add(uri)
            if len(contact.uris) > 1:
                contact.uris.default = uri
            modified = True
        if 'icon' in data:
            icon_descriptor = IconDescriptor(FileURL(data['icon']), str(int(os.stat(data['icon']).st_mtime)))
            if contact.icon != icon_descriptor:
                icon_manager = IconManager()
                icon_manager.store_file(contact.id, data['icon'])
                contact.icon = icon_descriptor
                modified = True
        return modified

    def _find_contact_move_point(self, contact):
        position = self.items.index(contact)
        prev_item = self.items[position - 1] if position > 0 else None
        next_item = self.items[position + 1] if position + 1 < len(self.items) else None
        prev_ok = prev_item is None or isinstance(prev_item, Group) or prev_item <= contact
        next_ok = next_item is None or isinstance(next_item, Group) or next_item >= contact
        if prev_ok and next_ok:
            return None
        for position in range(self.items.index(contact.group) + 1, len(self.items)):
            item = self.items[position]
            if isinstance(item, Group) or item > contact:
                break
        else:
            position = len(self.items)
        return position

    def _find_contact_insertion_point(self, contact):
        for position in range(self.items.index(contact.group) + 1, len(self.items)):
            item = self.items[position]
            if isinstance(item, Group) or item > contact:
                break
        else:
            position = len(self.items)
        return position

    def _find_group_insertion_point(self, group):
        if group.settings.position is None:
            return 0  # insert new groups at the top
        for item in self.items[GroupList]:
            if item.relocation_info is None and item.settings.position >= group.settings.position:
                position = self.items.index(item)
                break
            elif item.relocation_info is not None and item.settings.position == group.settings.position - 1:
                item.relocation_info.successor = group
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
        items = self.items[start:end + 1]
        del self.items[start:end + 1]
        self.endRemoveRows()
        return items

    def _pop_items(self, indexes):
        items = []
        rows = set(index.row() for index in indexes if index.isValid())
        removed_groups = set(self.items[row] for row in rows if isinstance(self.items[row], Group))
        rows.update(row for row, item in enumerate(self.items) if isinstance(item, Contact) and item.group in removed_groups)
        for start, end in self.reversed_range_iterator(rows):
            self.beginRemoveRows(QModelIndex(), start, end)
            items[0:0] = self.items[start:end + 1]
            del self.items[start:end + 1]
            self.endRemoveRows()
        return items

    def _update_group_positions(self):
        if self.state != 'started':
            return
        groups = self.items[GroupList]
        bonjour_group = self.bonjour_group
        if bonjour_group is groups[0] and bonjour_group.relocation_info is not None:
            groups.pop(0)
            if bonjour_group.relocation_info.successor is not None:
                groups.insert(groups.index(bonjour_group.relocation_info.successor), bonjour_group)
            else:
                groups.append(bonjour_group)
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

    def moveGroup(self, group, successor):
        groups = self.items[GroupList]
        if group not in groups or groups.index(group) + 1 == (groups.index(successor) if successor in groups else len(groups)):
            return
        items = self._pop_group(group)
        position = self.items.index(successor) if successor in groups else len(self.items)
        self.beginInsertRows(QModelIndex(), position, position + len(items) - 1)
        self.items[position:position] = items
        self.endInsertRows()
        self.contact_list.openPersistentEditor(self.index(position))
        self._update_group_positions()

    def removeItems(self, indexes):
        all_contacts_group = AllContactsGroup()
        icon_manager = IconManager()
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
                icon = icon_manager.get(item.settings.id)
                icon_data = icon and icon.content
                alternate_icon = icon_manager.get(item.settings.id + '_alt')
                alternate_icon_data = alternate_icon and alternate_icon.content
                undo_operations.append(AddContactOperation(contact=RecallState(item.settings), group_ids=group_ids, icon=icon_data, alternate_icon=alternate_icon_data))
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
        self.contact_list = parent.search_list
        self.setSourceModel(model)
        self.setDynamicSortFilter(True)
        self.sort(0)

    def flags(self, index):
        if index.isValid():
            return QSortFilterProxyModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled | Qt.ItemFlag.ItemIsDragEnabled
        else:
            return QSortFilterProxyModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        source_index = source_model.index(source_row, 0, source_parent)
        item = source_index.data(Qt.ItemDataRole.UserRole)
        if isinstance(item, Group) or not item.group.virtual:
            return False
        search_tokens = self.filterRegularExpression().pattern().lower().split()
        searched_item = ' '.join([item.name] + [uri.uri for uri in item.uris]).lower()  # should we only search in the username part of the uris? -Dan
        return all(token in searched_item for token in search_tokens)

    def lessThan(self, left_index, right_index):
        return left_index.data(Qt.ItemDataRole.DisplayRole) < right_index.data(Qt.ItemDataRole.DisplayRole)

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction

    def mimeTypes(self):
        return ['application/x-blink-contact-list']

    def mimeData(self, indexes):
        mime_data = QMimeData()
        contacts = [index.data(Qt.ItemDataRole.UserRole) for index in indexes if index.isValid()]
        if contacts:
            mime_data.setData('application/x-blink-contact-list', QByteArray(pickle.dumps(contacts)))
        return mime_data

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # this is here just to keep the default Qt DnD API happy
        # the custom handler is in handleDroppedData
        return False

    def handleDroppedData(self, mime_data, action, index):
        if action == Qt.DropAction.IgnoreAction:
            return True

        for mime_type in self.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                return handler(mime_data, action, index)
        else:
            return False

    def _DH_TextUriList(self, mime_data, action, index):
        if not index.isValid():
            return False

        # TODO: support directories? -Saul
        files = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile() and os.path.isfile(url.toLocalFile())]
        if not files:
            return False

        contact = index.data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        for filename in files:
            session_manager.send_file(contact, contact.uri, filename)

        return True


@implementer(IObserver)
class ContactDetailModel(QAbstractListModel):

    contactDeleted = pyqtSignal()

    # The MIME types we accept in drop operations, in the order they should be handled
    accepted_mime_types = ['application/x-blink-session', 'text/uri-list']

    def __init__(self, parent=None):
        super(ContactDetailModel, self).__init__(parent)
        self.contact = None
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkContactDetailDidChange')
        notification_center.add_observer(self, name='BlinkContactURIDidChange')
        notification_center.add_observer(self, name='VirtualGroupDidRemoveContact')
        notification_center.add_observer(self, name='VirtualContactDidChange')

    @property
    def contact_detail(self):
        return self.items[0] if self.items else None

    def _get_contact(self):
        return self.__dict__['contact']

    def _set_contact(self, contact):
        old_contact = self.__dict__.get('contact', Null)
        if contact is old_contact:
            return
        notification_center = NotificationCenter()
        if old_contact:
            notification_center.remove_observer(self, sender=old_contact)
        if contact is not None:
            notification_center.add_observer(self, sender=contact)
        self.__dict__['contact'] = contact
        self.beginResetModel()
        if contact is None:
            self.items = []
        else:
            self.items = [ContactDetail(contact)] + [ContactURI(contact, uri) for uri in contact.uris]
        self.endResetModel()

    contact = property(_get_contact, _set_contact)
    del _get_contact, _set_contact

    def flags(self, index):
        if index.isValid():
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled | Qt.ItemFlag.ItemIsDragEnabled
        else:
            return QAbstractListModel.flags(self, index) | Qt.ItemFlag.ItemIsDropEnabled

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        item = self.items[row]
        if role == Qt.ItemDataRole.UserRole:
            return item
        elif role == Qt.ItemDataRole.DisplayRole:
            return str(item)
        elif role == Qt.ItemDataRole.SizeHintRole:
            return item.size_hint
        elif role == Qt.ItemDataRole.CheckStateRole and row > 0:
            if item.uri is self.contact.uris.default:
                return Qt.CheckState.Checked
            elif self.contact.uris.default is None and row == 1:
                return Qt.CheckState.PartiallyChecked
            return Qt.CheckState.Unchecked
        return None

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction

    def mimeTypes(self):
        return ['application/x-blink-contact-list', 'application/x-blink-contact-uri-list']

    def mimeData(self, indexes):
        mime_data = QMimeData()
        items = [self.items[index.row()] for index in indexes if index.isValid()]
        contact_list = [item for item in items if isinstance(item, ContactDetail)]
        contact_uris = [item for item in items if isinstance(item, ContactURI)]
        if contact_list:
            mime_data.setData('application/x-blink-contact-list', QByteArray(pickle.dumps(contact_list)))
        if contact_uris:
            mime_data.setData('application/x-blink-contact-uri-list', QByteArray(pickle.dumps((self.contact_detail, contact_uris))))
        return mime_data

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # this is here just to keep the default Qt DnD API happy
        # the custom handler is in handleDroppedData
        return False

    def handleDroppedData(self, mime_data, action, index):
        if action == Qt.DropAction.IgnoreAction:
            return True

        for mime_type in self.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                name = mime_type.replace('/', ' ').replace('-', ' ').title().replace(' ', '')
                handler = getattr(self, '_DH_%s' % name)
                return handler(mime_data, action, index)
        else:
            return False

    def _DH_ApplicationXBlinkSession(self, mime_data, action, index):
        return False

    def _DH_TextUriList(self, mime_data, action, index):
        if not index.isValid():
            contact_uri = self.contact_detail.uri
        else:
            item = self.items[index.row()]
            if isinstance(item, ContactURI):
                contact_uri = item.uri
            else:
                contact_uri = self.contact_detail.uri

        # TODO: support directories? -Saul
        files = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile() and os.path.isfile(url.toLocalFile())]
        if not files:
            return False

        contact = self.contact_detail
        session_manager = SessionManager()
        for filename in files:
            session_manager.send_file(contact, contact_uri, filename)

        return True

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactDidChange(self, notification):
        if notification.sender is self.contact and 'uris' in notification.data.modified:
            modified_uris = notification.data.modified['uris']
            for row in sorted((row for row, item in enumerate(self.items) if row > 0 and item.uri in modified_uris.removed), reverse=True):
                self.beginRemoveRows(QModelIndex(), row, row)
                del self.items[row]
                self.endRemoveRows()
            if modified_uris.added:
                position = len(self.items)
                self.beginInsertRows(QModelIndex(), position, position + len(modified_uris.added) - 1)
                self.items += [ContactURI(notification.sender, uri) for uri in modified_uris.added]
                self.endInsertRows()

    def _NH_VirtualContactDidChange(self, notification):
        if notification.sender is self.contact:
            old_uris = set(item.uri for item in self.items[1:])
            added_uris = [uri for uri in self.contact.uris if uri not in old_uris]
            removed_uris = old_uris.difference(self.contact.uris)
            modified_uris = old_uris.difference(removed_uris)
            for row in sorted((row for row, item in enumerate(self.items) if row > 0 and item.uri in removed_uris), reverse=True):
                self.beginRemoveRows(QModelIndex(), row, row)
                del self.items[row]
                self.endRemoveRows()
            if added_uris:
                position = len(self.items)
                self.beginInsertRows(QModelIndex(), position, position + len(added_uris) - 1)
                self.items += [ContactURI(self.contact, uri) for uri in added_uris]
                self.endInsertRows()
            for row in (row for row, item in enumerate(self.items) if row > 0 and item.uri in modified_uris):
                index = self.index(row)
                self.dataChanged.emit(index, index)

    def _NH_VirtualGroupDidRemoveContact(self, notification):
        if notification.data.contact is self.contact:
            self.contact = None
            self.contactDeleted.emit()

    def _NH_BlinkContactDetailDidChange(self, notification):
        if self.items and notification.sender is self.contact_detail:
            index = self.index(0)
            self.dataChanged.emit(index, index)

    def _NH_BlinkContactURIDidChange(self, notification):
        if notification.sender in self.items:
            index = self.index(self.items.index(notification.sender))
            self.dataChanged.emit(index, index)


@implementer(IObserver)
class ContactListView(QListView):

    def __init__(self, parent=None):
        super(ContactListView, self).__init__(parent)
        self.setItemDelegate(ContactDelegate(self))
        self.setDropIndicatorShown(False)
        self.detail_model = ContactDetailModel(self)
        self.detail_view = ContactDetailView(self)
        self.detail_view.setModel(self.detail_model)
        self.detail_view.hide()
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.actions.add_group = QAction(translate("contact_list", "Add new group"), self, triggered=self._AH_AddGroup)
        self.actions.add_contact = QAction(translate("contact_list", "Add new contact"), self, triggered=self._AH_AddContact)
        self.actions.add_item = QAction(translate("contact_list", "Add"), self, triggered=self._AH_AddItem)
        self.actions.edit_item = QAction(translate("contact_list", "Edit"), self, triggered=self._AH_EditItem)
        self.actions.delete_item = QAction(translate("contact_list", "Delete"), self, triggered=self._AH_DeleteSelection)
        self.actions.delete_selection = QAction(translate("contact_list", "Delete Selection"), self, triggered=self._AH_DeleteSelection)
        self.actions.undo_last_delete = QAction(translate("contact_list", "Undo Last Delete"), self, triggered=self._AH_UndoLastDelete)
        self.actions.send_sms = QAction(translate("contact_list", "Send Messages"), self, triggered=self._AH_SendSMS)
        self.actions.start_audio_call = QAction(translate("contact_list", "Start Audio Call"), self, triggered=self._AH_StartAudioCall)
        self.actions.start_video_call = QAction(translate("contact_list", "Start Video Call"), self, triggered=self._AH_StartVideoCall)
        self.actions.start_chat_session = QAction(translate("contact_list", "Start MSRP Chat"), self, triggered=self._AH_StartChatSession)
        self.actions.send_files = QAction(translate("contact_list", "Send File(s)..."), self, triggered=self._AH_SendFiles)
        self.actions.request_screen = QAction(translate("contact_list", "Request Screen"), self, triggered=self._AH_RequestScreen)
        self.actions.share_my_screen = QAction(translate("contact_list", "Share My Screen"), self, triggered=self._AH_ShareMyScreen)
        self.actions.transfer_call = QAction(translate("contact_list", "Transfer Active Call"), self, triggered=self._AH_TransferCall)
        self.drop_indicator_index = QModelIndex()
        self.needs_restore = False
        self.doubleClicked.connect(self._SH_DoubleClicked)  # activated is emitted on single click
        notification_center = NotificationCenter()
        notification_center.add_observer(self, 'BlinkSessionDidChangeState')
        notification_center.add_observer(self, 'BlinkSessionDidRemoveStream')
        notification_center.add_observer(self, 'BlinkActiveSessionDidChange')

    def selectionChanged(self, selected, deselected):
        super(ContactListView, self).selectionChanged(selected, deselected)
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1)
            selection_model.setCurrentIndex(index, selection_model.SelectionFlag.Select)
        self.context_menu.hide()

    def contextMenuEvent(self, event):
        model = self.model()
        selected_items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
        if not model.deleted_items:
            undo_delete_text = translate("contact_list", "Undo Delete")
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
                    name = translate('contact_list', 'Contact')
                else:
                    name = contact.name or translate('contact_list', 'Contact')
            undo_delete_text = translate('contact_list', 'Undo Delete "%s"') % name
        else:
            undo_delete_text = translate('contact_list', "Undo Delete (%d items)") % len(model.deleted_items[-1])
        menu = self.context_menu
        menu.clear()
        if not selected_items:
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        elif len(selected_items) > 1:
            menu.addAction(self.actions.delete_selection)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.delete_selection.setEnabled(any(item.deletable for item in selected_items))
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
            menu.addSeparator()
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
        elif isinstance(selected_items[0], Group):
            menu.addAction(self.actions.edit_item)
            menu.addAction(self.actions.delete_item)
            menu.addAction(self.actions.undo_last_delete)
            menu.addSeparator()
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.edit_item.setEnabled(selected_items[0].editable)
            self.actions.delete_item.setEnabled(selected_items[0].deletable)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        else:
            contact = selected_items[0]
            account_manager = AccountManager()
            session_manager = SessionManager()
            can_call = account_manager.default_account is not None and contact.uri is not None
            can_transfer = contact.uri is not None and session_manager.active_session is not None and session_manager.active_session.state == 'connected'

            if len(contact.uris) > 1 and can_call:
                call_submenu = menu.addMenu(translate('contact_list', 'Send Messages'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(partial(self._AH_SendSMS, uri))
                    call_submenu.addAction(call_item)

                call_submenu = menu.addMenu(translate('contact_list', 'Start Audio Call'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(partial(self._AH_StartAudioCall, uri))
                    call_submenu.addAction(call_item)

                call_submenu = menu.addMenu(translate('contact_list', 'Start Video Call'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(partial(self._AH_StartVideoCall, uri))
                    call_submenu.addAction(call_item)

                call_submenu = menu.addMenu(translate('contact_list', 'Send File(s)...'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(partial(self._AH_SendFiles, uri))
                    call_submenu.addAction(call_item)

                call_submenu = menu.addMenu(translate('contact_list', 'Request Screen'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(partial(self._AH_RequestScreen, uri))
                    call_submenu.addAction(call_item)

                call_submenu = menu.addMenu(translate('contact_list', 'Share My Screen'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(partial(self._AH_ShareMyScreen, uri))
                    call_submenu.addAction(call_item)

                call_submenu = menu.addMenu(translate('contact_list', 'Start MSRP Chat'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(partial(self._AH_StartChatSession, uri))
                    call_submenu.addAction(call_item)

            else:
                menu.addAction(self.actions.send_sms)
                menu.addAction(self.actions.start_audio_call)
                menu.addAction(self.actions.start_video_call)
                menu.addAction(self.actions.send_files)
                menu.addAction(self.actions.request_screen)
                menu.addAction(self.actions.share_my_screen)
                menu.addAction(self.actions.start_chat_session)

                self.actions.start_audio_call.setEnabled(can_call)
                self.actions.start_video_call.setEnabled(can_call)
                self.actions.start_chat_session.setEnabled(can_call)
                self.actions.send_sms.setEnabled(can_call)
                self.actions.send_files.setEnabled(can_call)
                self.actions.request_screen.setEnabled(can_call)
                self.actions.share_my_screen.setEnabled(can_call)

            if len(contact.uris) > 1 and can_transfer:
                call_submenu = menu.addMenu(translate('contact_list', 'Transfer Call'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(lambda: self._AH_TransferCall(uri))
                    call_submenu.addAction(call_item)
            else:
                menu.addAction(self.actions.transfer_call)
                self.actions.transfer_call.setEnabled(can_transfer)

            if contact.group.settings is MessageContactsGroup():
                menu.addSeparator()
                if isinstance(contact.settings, MessageContact):
                    menu.addAction(self.actions.add_item)
                menu.addAction(self.actions.edit_item)
            else:
                menu.addSeparator()
                menu.addAction(self.actions.edit_item)
                menu.addAction(self.actions.delete_item)
                menu.addAction(self.actions.undo_last_delete)
                self.actions.undo_last_delete.setText(undo_delete_text)
                self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
                self.actions.delete_item.setEnabled(contact.deletable)

            menu.addSeparator()
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)

            self.actions.edit_item.setEnabled(contact.editable)
        menu.exec(event.globalPos())

    def hideEvent(self, event):
        self.context_menu.hide()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            selected_indexes = self.selectionModel().selectedIndexes()
            item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if len(selected_indexes) == 1 else None
            if isinstance(item, Contact):
                if item.group.settings is MessageContactsGroup():
                    session_manager = MessageManager()
                    session_manager.create_message_session(item.uri.uri)
                else:
                    session_manager = SessionManager()
                    session_manager.create_session(item, item.uri, item.preferred_media.stream_descriptions, connect=item.preferred_media.autoconnect)
        elif event.key() == Qt.Key.Key_Space:
            selected_indexes = self.selectionModel().selectedIndexes()
            item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if len(selected_indexes) == 1 else None
            if isinstance(item, Contact) and self.detail_view.isHidden() and self.detail_view.animation.state() == QPropertyAnimation.State.Stopped:
                self.detail_model.contact = item.settings
                self.detail_view.animation.setDirection(QPropertyAnimation.Forward)
                self.detail_view.animation.setStartValue(self.visualRect(selected_indexes[0]))
                self.detail_view.animation.setEndValue(self.geometry())
                self.detail_view.raise_()
                self.detail_view.show()
                self.detail_view.animation.start()
        else:
            super(ContactListView, self).keyPressEvent(event)

    def paintEvent(self, event):
        super(ContactListView, self).paintEvent(event)
        if self.drop_indicator_index.isValid():
            rect = self.visualRect(self.drop_indicator_index)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()
        model = self.model()
        try:
            last_group = model.items[GroupList][-1]
        except IndexError:
            last_group = Null
        if last_group.widget.drop_indicator is self.DropIndicatorPosition.BelowItem:
            # draw the bottom part of the drop indicator for the last group if we have one
            rect = self.visualRect(model.index(model.items.index(last_group)))
            line_rect = QRectF(rect.adjusted(18, rect.height(), 0, 5))
            arc_rect = line_rect.adjusted(-5, -3, -line_rect.width(), -3)
            path = QPainterPath(line_rect.topRight())
            path.lineTo(line_rect.topLeft())
            path.arcTo(arc_rect, 0, -180)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawPath(path)
            painter.end()

    def startDrag(self, supported_actions):
        super(ContactListView, self).startDrag(supported_actions)
        if self.needs_restore:
            for group in self.model().items[GroupList]:
                group.restore_state()
            self.needs_restore = False
        main_window = QApplication.instance().main_window
        main_window.switch_view_button.dnd_active = False
        if not main_window.session_model.sessions:
            main_window.switch_view_button.view = SwitchViewButton.ContactView

    def dragEnterEvent(self, event):
        model = self.model()
        event_source = event.source()
        accepted_mime_types = set(model.accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        has_blink_contacts = 'application/x-blink-contact-list' in provided_mime_types
        has_blink_groups = 'application/x-blink-group-list' in provided_mime_types
        if not acceptable_mime_types:
            event.ignore()  # no acceptable mime types found
        elif has_blink_contacts and has_blink_groups:
            event.ignore()  # we can't handle drops for both groups and contacts at the same time
        elif event_source is not self and (has_blink_contacts or has_blink_groups):
            event.ignore()  # we don't handle drops for blink contacts or groups from other sources
        else:
            if event_source is self:
                event.setDropAction(Qt.DropAction.MoveAction)
            if has_blink_contacts or has_blink_groups:
                if not self.needs_restore:
                    for group in model.items[GroupList]:
                        group.save_state()
                        group.collapse()
                    self.needs_restore = True
            if has_blink_contacts:
                QApplication.instance().main_window.switch_view_button.dnd_active = True
            event.accept()

    def dragLeaveEvent(self, event):
        super(ContactListView, self).dragLeaveEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()
        for group in self.model().items[GroupList]:
            group.widget.drop_indicator = None

    def dragMoveEvent(self, event):
        super(ContactListView, self).dragMoveEvent(event)
        if event.source() is self:
            event.setDropAction(Qt.DropAction.MoveAction)

        model = self.model()
        mime_data = event.mimeData()

        for mime_type in model.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                self.drop_indicator_index = QModelIndex()
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                item = index.data(Qt.ItemDataRole.UserRole)
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
            event.setDropAction(Qt.DropAction.MoveAction)
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.pos())):
            event.accept()
        for group in model.items[GroupList]:
            group.widget.drop_indicator = None
        super(ContactListView, self).dropEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def _AH_AddGroup(self):
        group = Group(addressbook.Group())
        group.settings.save = Null  # disable saving until the user provides the name
        model = self.model()
        selection_model = self.selectionModel()
        model.addGroup(group)
        self.scrollToTop()
        group.widget.edit()
        selection_model.select(model.index(model.items.index(group)), selection_model.SelectionFlag.ClearAndSelect)

    def _AH_AddContact(self):
        groups = set()
        for index in self.selectionModel().selectedIndexes():
            item = index.data(Qt.ItemDataRole.UserRole)
            if isinstance(item, Group) and not item.virtual:
                groups.add(item)
            elif isinstance(item, Contact) and not item.group.virtual:
                groups.add(item.group)
        preferred_group = groups.pop() if len(groups) == 1 else None
        main_window = QApplication.instance().main_window
        main_window.contact_editor_dialog.open_for_add(main_window.search_box.text(), preferred_group)

    def _AH_AddItem(self):
        index = self.selectionModel().selectedIndexes()[0]
        item = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(item, Group):
            self.scrollTo(index)
            item.widget.edit()
        else:
            QApplication.instance().main_window.contact_editor_dialog.open_for_add(item.uri.uri)

    def _AH_EditItem(self):
        index = self.selectionModel().selectedIndexes()[0]
        item = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(item, Group):
            self.scrollTo(index)
            item.widget.edit()
        else:
            QApplication.instance().main_window.contact_editor_dialog.open_for_edit(item.settings)

    def _AH_DeleteSelection(self):
        self.model().removeItems(self.selectionModel().selectedIndexes())
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
                if operation.icon is not None and contact.icon is not None:
                    icon_manager.store_data(contact.id, operation.icon)
                if operation.alternate_icon is not None and contact.alternate_icon is not None:
                    icon_manager.store_data(contact.id + '_alt', operation.alternate_icon)
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

    def _AH_StartAudioCall(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('audio')])

    def _AH_StartVideoCall(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('audio'), StreamDescription('video')])

    def _AH_StartChatSession(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('chat')], connect=False)

    def _AH_SendSMS(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = MessageManager()
        try:
            uri = uri.uri
        except AttributeError:
            uri = uri
        session_manager.create_message_session(uri or contact.uri.uri)

    def _AH_SendFiles(self, uri=None):
        session_manager = SessionManager()
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        for filename in QFileDialog.getOpenFileNames(self, 'Select File(s)', session_manager.send_file_directory, 'Any file (*.*)')[0]:
            session_manager.send_file(contact, uri or contact.uri, filename)

    def _AH_RequestScreen(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('screen-sharing', mode='viewer'), StreamDescription('audio')])

    def _AH_ShareMyScreen(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('screen-sharing', mode='server'), StreamDescription('audio')])

    def _AH_TransferCall(self):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.active_session.transfer(contact.uri)

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
            rect.setHeight(int(rect.height() / 2))
            if rect.contains(event.pos()):
                drop_groups = (groups[index - 1], groups[index]) if index > 0 else (Null, groups[index])
            else:
                drop_groups = (groups[index], groups[index + 1]) if index < len(groups) - 1 else (groups[index], Null)
                rect.translate(0, rect.height())
        selected_rows = sorted(index.row() for index in self.selectionModel().selectedIndexes() if model.items[index.row()].movable)
        if selected_rows:
            first = groups.index(model.items[selected_rows[0]])
            last = groups.index(model.items[selected_rows[-1]])
            contiguous_selection = len(selected_rows) == last - first + 1
        else:
            contiguous_selection = False
        selected_groups = set(model.items[row] for row in selected_rows)
        try:
            overlapping_groups = len(selected_groups.intersection(drop_groups))
        except TypeError:
            overlapping_groups = 0
        allowed_overlapping = 0 if contiguous_selection else 1
        if event.source() is not self or overlapping_groups <= allowed_overlapping:
            drop_groups[0].widget.drop_indicator = self.DropIndicatorPosition.BelowItem
            drop_groups[1].widget.drop_indicator = self.DropIndicatorPosition.AboveItem
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
            group.widget.drop_indicator = self.DropIndicatorPosition.OnItem
        event.accept(rect)

    def _DH_TextUriList(self, event, index, rect, item):
        model = self.model()
        if not index.isValid():
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(len(model.items) - 1)).bottom())
        if isinstance(item, Contact):
            event.accept(rect)
            self.drop_indicator_index = index
        else:
            event.ignore(rect)

    def _SH_DoubleClicked(self, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(item, Contact):
            if item.group.settings is MessageContactsGroup():
                session_manager = MessageManager()
                session_manager.create_message_session(item.uri.uri)
            else:
                session_manager = SessionManager()
                session_manager.create_session(item, item.uri, item.preferred_media.stream_descriptions, connect=item.preferred_media.autoconnect)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidChangeState(self, notification):
        session_manager = SessionManager()
        if notification.sender is session_manager.active_session and self.context_menu.isVisible():
            selected_items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
            if len(selected_items) == 1 and isinstance(selected_items[0], Contact):
                contact = selected_items[0]
                self.actions.transfer_call.setEnabled(contact.uri is not None and notification.sender.state == 'connected')

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        session_manager = SessionManager()
        if notification.sender is session_manager.active_session and self.context_menu.isVisible():
            selected_items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
            if len(selected_items) == 1 and isinstance(selected_items[0], Contact):
                contact = selected_items[0]
                self.actions.transfer_call.setEnabled(contact.uri is not None and 'audio' in notification.sender.streams)

    def _NH_BlinkActiveSessionDidChange(self, notification):
        if self.context_menu.isVisible():
            selected_items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
            if len(selected_items) == 1 and isinstance(selected_items[0], Contact):
                contact = selected_items[0]
                active_session = notification.data.active_session
                self.actions.transfer_call.setEnabled(contact.uri is not None and active_session is not None and active_session.state == 'connected')


@implementer(IObserver)
class ContactSearchListView(QListView):

    def __init__(self, parent=None):
        super(ContactSearchListView, self).__init__(parent)
        self.setItemDelegate(ContactDelegate(self))
        self.setDropIndicatorShown(False)
        self.detail_model = ContactDetailModel(self)
        self.detail_view = ContactDetailView(self)
        self.detail_view.setModel(self.detail_model)
        self.detail_view.hide()
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.actions.add_item = QAction(translate("contact_list", "Add"), self, triggered=self._AH_AddItem)
        self.actions.edit_item = QAction(translate("contact_list", "Edit"), self, triggered=self._AH_EditItem)
        self.actions.delete_item = QAction(translate("contact_list", "Delete"), self, triggered=self._AH_DeleteSelection)
        self.actions.delete_selection = QAction(translate("contact_list", "Delete Selection"), self, triggered=self._AH_DeleteSelection)
        self.actions.undo_last_delete = QAction(translate("contact_list", "Undo Last Delete"), self, triggered=self._AH_UndoLastDelete)
        self.actions.start_audio_call = QAction(translate("contact_list", "Start Audio Call"), self, triggered=self._AH_StartAudioCall)
        self.actions.start_video_call = QAction(translate("contact_list", "Start Video Call"), self, triggered=self._AH_StartVideoCall)
        self.actions.start_chat_session = QAction(translate("contact_list", "Start MSRP Chat"), self, triggered=self._AH_StartChatSession)
        self.actions.send_sms = QAction(translate("contact_list", "Send Messages"), self, triggered=self._AH_SendSMS)
        self.actions.send_files = QAction(translate("contact_list", "Send File(s)..."), self, triggered=self._AH_SendFiles)
        self.actions.request_screen = QAction(translate("contact_list", "Request Screen"), self, triggered=self._AH_RequestScreen)
        self.actions.share_my_screen = QAction(translate("contact_list", "Share My Screen"), self, triggered=self._AH_ShareMyScreen)
        self.actions.transfer_call = QAction(translate("contact_list", "Transfer Active Call"), self, triggered=self._AH_TransferCall)
        self.drop_indicator_index = QModelIndex()
        self.doubleClicked.connect(self._SH_DoubleClicked)  # activated is emitted on single click
        notification_center = NotificationCenter()
        notification_center.add_observer(self, 'BlinkSessionDidChangeState')
        notification_center.add_observer(self, 'BlinkSessionDidRemoveStream')
        notification_center.add_observer(self, 'BlinkActiveSessionDidChange')

    def selectionChanged(self, selected, deselected):
        super(ContactSearchListView, self).selectionChanged(selected, deselected)
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1, -1)
            selection_model.setCurrentIndex(index, selection_model.SelectionFlag.Select)
        self.context_menu.hide()

    def contextMenuEvent(self, event):
        model = self.model()
        source_model = model.sourceModel()
        selected_items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
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
                    name = translate('contact_list', 'Contact')
                else:
                    name = contact.name or translate('contact_list', 'Contact')
            undo_delete_text = translate('contact_list', 'Undo Delete "%s"') % name
        else:
            undo_delete_text = translate('contact_list', "Undo Delete (%d items)") % len(source_model.deleted_items[-1])
        menu = self.context_menu
        menu.clear()
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
            menu.addAction(self.actions.start_audio_call)
            menu.addAction(self.actions.start_video_call)
            menu.addAction(self.actions.start_chat_session)
            menu.addAction(self.actions.send_sms)
            menu.addAction(self.actions.send_files)
            menu.addAction(self.actions.request_screen)
            menu.addAction(self.actions.share_my_screen)
            menu.addAction(self.actions.transfer_call)
            menu.addSeparator()
            if contact.group.settings is MessageContactsGroup():
                if isinstance(contact.settings, MessageContact):
                    menu.addAction(self.actions.add_item)
            menu.addAction(self.actions.edit_item)
            menu.addAction(self.actions.delete_item)
            menu.addAction(self.actions.undo_last_delete)
            self.actions.undo_last_delete.setText(undo_delete_text)
            account_manager = AccountManager()
            session_manager = SessionManager()
            can_call = account_manager.default_account is not None and contact.uri is not None
            can_transfer = contact.uri is not None and session_manager.active_session is not None and session_manager.active_session.state == 'connected'
            self.actions.start_audio_call.setEnabled(can_call)
            self.actions.start_video_call.setEnabled(can_call)
            self.actions.start_chat_session.setEnabled(can_call)
            self.actions.send_sms.setEnabled(can_call)
            self.actions.send_files.setEnabled(can_call)
            self.actions.request_screen.setEnabled(can_call)
            self.actions.share_my_screen.setEnabled(can_call)
            self.actions.transfer_call.setEnabled(can_transfer)
            self.actions.edit_item.setEnabled(contact.editable)
            self.actions.delete_item.setEnabled(contact.deletable)
            self.actions.undo_last_delete.setEnabled(len(source_model.deleted_items) > 0)
        menu.exec(event.globalPos())

    def focusInEvent(self, event):
        super(ContactSearchListView, self).focusInEvent(event)
        model = self.model()
        selection_model = self.selectionModel()
        if not selection_model.selectedIndexes() and model.rowCount() > 0:
            selection_model.setCurrentIndex(model.index(-1, -1), selection_model.SelectionFlag.NoUpdate)

    def hideEvent(self, event):
        self.context_menu.hide()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            selected_indexes = self.selectionModel().selectedIndexes()
            item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if len(selected_indexes) == 1 else None
            if isinstance(item, Contact):
                if item.group.settings is MessageContactsGroup():
                    session_manager = MessageManager()
                    session_manager.create_message_session(item.uri.uri)
                else:
                    session_manager = SessionManager()
                    session_manager.create_session(item, item.uri, item.preferred_media.stream_descriptions, connect=item.preferred_media.autoconnect)
        elif event.key() == Qt.Key.Key_Escape:
            QApplication.instance().main_window.search_box.clear()
        elif event.key() == Qt.Key.Key_Space:
            selected_indexes = self.selectionModel().selectedIndexes()
            item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if len(selected_indexes) == 1 else None
            if isinstance(item, Contact) and self.detail_view.isHidden() and self.detail_view.animation.state() == QPropertyAnimation.State.Stopped:
                self.detail_model.contact = item.settings
                self.detail_view.animation.setDirection(QPropertyAnimation.Forward)
                self.detail_view.animation.setStartValue(self.visualRect(selected_indexes[0]))
                self.detail_view.animation.setEndValue(self.geometry())
                self.detail_view.raise_()
                self.detail_view.show()
                self.detail_view.animation.start()
        else:
            super(ContactSearchListView, self).keyPressEvent(event)

    def paintEvent(self, event):
        super(ContactSearchListView, self).paintEvent(event)
        if self.drop_indicator_index.isValid():
            rect = self.visualRect(self.drop_indicator_index)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()

    def startDrag(self, supported_actions):
        super(ContactSearchListView, self).startDrag(supported_actions)
        main_window = QApplication.instance().main_window
        main_window.switch_view_button.dnd_active = False
        if not main_window.session_model.sessions:
            main_window.switch_view_button.view = SwitchViewButton.ContactView

    def dragEnterEvent(self, event):
        accepted_mime_types = set(self.model().accepted_mime_types)
        provided_mime_types = set(event.mimeData().formats())
        acceptable_mime_types = accepted_mime_types & provided_mime_types
        if event.source() is self:
            event.ignore()
            QApplication.instance().main_window.switch_view_button.dnd_active = True
        elif not acceptable_mime_types:
            event.ignore()
        else:
            event.accept()

    def dragLeaveEvent(self, event):
        super(ContactSearchListView, self).dragLeaveEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def dragMoveEvent(self, event):
        super(ContactSearchListView, self).dragMoveEvent(event)

        mime_data = event.mimeData()

        for mime_type in self.model().accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                self.drop_indicator_index = QModelIndex()
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                item = index.data(Qt.ItemDataRole.UserRole)
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

    def _AH_AddItem(self):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        QApplication.instance().main_window.contact_editor_dialog.open_for_add(contact.uri.uri)

    def _AH_EditItem(self):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        QApplication.instance().main_window.contact_editor_dialog.open_for_edit(contact.settings)

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
                if operation.icon is not None and contact.icon is not None:
                    icon_manager.store_data(contact.id, operation.icon)
                if operation.alternate_icon is not None and contact.alternate_icon is not None:
                    icon_manager.store_data(contact.id + '_alt', operation.alternate_icon)
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

    def _AH_StartAudioCall(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('audio')])

    def _AH_StartVideoCall(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('audio'), StreamDescription('video')])

    def _AH_StartChatSession(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('chat')], connect=False)

    def _AH_SendSMS(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = MessageManager()
        session_manager.create_message_session(uri or contact.uri.uri)

    def _AH_SendFiles(self, uri=None):
        session_manager = SessionManager()
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        for filename in QFileDialog.getOpenFileNames(self, translate('contact_list', 'Select File(s)'), session_manager.send_file_directory, 'Any file (*.*)')[0]:
            session_manager.send_file(contact, uri or contact.uri, filename)

    def _AH_RequestScreen(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('screen-sharing', mode='viewer'), StreamDescription('audio')])

    def _AH_ShareMyScreen(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.create_session(contact, uri or contact.uri, [StreamDescription('screen-sharing', mode='server'), StreamDescription('audio')])

    def _AH_TransferCall(self, uri=None):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        session_manager = SessionManager()
        session_manager.active_session.transfer(uri or contact.uri)

    def _DH_TextUriList(self, event, index, rect, item):
        if index.isValid():
            event.accept(rect)
            self.drop_indicator_index = index
        else:
            model = self.model()
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(model.rowCount() - 1, 0)).bottom())
            event.ignore(rect)

    def _SH_DoubleClicked(self, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(item, Contact):
            if item.group.settings is MessageContactsGroup():
                session_manager = MessageManager()
                session_manager.create_message_session(item.uri.uri)
            else:
                session_manager = SessionManager()
                session_manager.create_session(item, item.uri, item.preferred_media.stream_descriptions, connect=item.preferred_media.autoconnect)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidChangeState(self, notification):
        session_manager = SessionManager()
        if notification.sender is session_manager.active_session and self.context_menu.isVisible():
            selected_items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
            if len(selected_items) == 1 and isinstance(selected_items[0], Contact):
                contact = selected_items[0]
                self.actions.transfer_call.setEnabled(contact.uri is not None and notification.sender.state == 'connected')

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        session_manager = SessionManager()
        if notification.sender is session_manager.active_session and self.context_menu.isVisible():
            selected_items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
            if len(selected_items) == 1 and isinstance(selected_items[0], Contact):
                contact = selected_items[0]
                self.actions.transfer_call.setEnabled(contact.uri is not None and 'audio' in notification.sender.streams)

    def _NH_BlinkActiveSessionDidChange(self, notification):
        if self.context_menu.isVisible():
            selected_items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
            if len(selected_items) == 1 and isinstance(selected_items[0], Contact):
                contact = selected_items[0]
                active_session = notification.data.active_session
                self.actions.transfer_call.setEnabled(contact.uri is not None and active_session is not None and active_session.state == 'connected')


@implementer(IObserver)
class ContactDetailView(QListView):

    def __init__(self, contact_list):
        super(ContactDetailView, self).__init__(contact_list.parent())
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor('#eeeeee'))
        self.setPalette(palette)
        self.contact_list = contact_list
        self.setItemDelegate(ContactDetailDelegate(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragEnabled(True)
        self.setDragDropMode(QListView.DragDropMode.DragDrop)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setDropIndicatorShown(False)
        self.animation = QPropertyAnimation(self, b'geometry')
        self.animation.setDuration(250)
        self.animation.setEasingCurve(QEasingCurve.Type.Linear)
        self.animation.finished.connect(self._SH_AnimationFinished)
        self.context_menu = QMenu(self)
        self.actions = ContextMenuActions()
        self.actions.delete_contact = QAction(translate("contact_list", "Delete Contact"), self, triggered=self._AH_DeleteContact)
        self.actions.edit_contact = QAction(translate("contact_list", "Edit Contact"), self, triggered=self._AH_EditContact)
        self.actions.make_uri_default = QAction(translate("contact_list", "Set Address As Default"), self, triggered=self._AH_MakeURIDefault)
        self.actions.send_sms = QAction(translate("contact_list", "Send Messages"), self, triggered=self._AH_SendSMS)
        self.actions.start_audio_call = QAction(translate("contact_list", "Start Audio Call"), self, triggered=self._AH_StartAudioCall)
        self.actions.start_video_call = QAction(translate("contact_list", "Start Video Call"), self, triggered=self._AH_StartVideoCall)
        self.actions.start_chat_session = QAction(translate("contact_list", "Start MSRP Chat"), self, triggered=self._AH_StartChatSession)
        self.actions.send_files = QAction(translate("contact_list", "Send File(s)..."), self, triggered=self._AH_SendFiles)
        self.actions.request_screen = QAction(translate("contact_list", "Request Screen"), self, triggered=self._AH_RequestScreen)
        self.actions.share_my_screen = QAction(translate("contact_list", "Share My Screen"), self, triggered=self._AH_ShareMyScreen)
        self.actions.transfer_call = QAction(translate("contact_list", "Transfer Active Call"), self, triggered=self._AH_TransferCall)
        self.drop_indicator_index = QModelIndex()
        self.doubleClicked.connect(self._SH_DoubleClicked)  # activated is emitted on single click
        contact_list.installEventFilter(self)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, 'BlinkSessionDidChangeState')
        notification_center.add_observer(self, 'BlinkSessionDidRemoveStream')
        notification_center.add_observer(self, 'BlinkActiveSessionDidChange')

    def setModel(self, model):
        old_model = self.model() or Null
        old_model.contactDeleted.disconnect(self._SH_ModelContactDeleted)
        super(ContactDetailView, self).setModel(model)
        model.contactDeleted.connect(self._SH_ModelContactDeleted)

    def selectionChanged(self, selected, deselected):
        super(ContactDetailView, self).selectionChanged(selected, deselected)
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1)
            selection_model.setCurrentIndex(index, selection_model.SelectionFlag.Select)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            new_size = event.size()
            geometry = self.animation.endValue()
            if geometry is not None:
                old_size = geometry.size()
                geometry.setSize(new_size)
                self.animation.setEndValue(geometry)
                geometry = self.animation.startValue()
                geometry.setWidth(geometry.width() + new_size.width() - old_size.width())
                self.animation.setStartValue(geometry)
            self.resize(new_size)
        return False

    def contextMenuEvent(self, event):
        account_manager = AccountManager()
        session_manager = SessionManager()
        model = self.model()
        selected_indexes = self.selectionModel().selectedIndexes()
        selected_item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        contact_has_uris = model.rowCount() > 1
        menu = self.context_menu
        menu.clear()
        menu.addAction(self.actions.send_sms)
        menu.addAction(self.actions.start_audio_call)
        menu.addAction(self.actions.start_video_call)
        menu.addAction(self.actions.send_files)
        menu.addAction(self.actions.request_screen)
        menu.addAction(self.actions.share_my_screen)
        menu.addAction(self.actions.transfer_call)
        menu.addAction(self.actions.start_chat_session)
        menu.addSeparator()
        if isinstance(selected_item, ContactURI) and model.contact_detail.editable:
            menu.addAction(self.actions.make_uri_default)
            self.actions.make_uri_default.setEnabled(selected_item.uri is not model.contact.uris.default)
        menu.addAction(self.actions.edit_contact)
        menu.addAction(self.actions.delete_contact)
        can_call = account_manager.default_account is not None and contact_has_uris
        can_transfer = contact_has_uris and session_manager.active_session is not None and session_manager.active_session.state == 'connected'
        self.actions.start_audio_call.setEnabled(can_call)
        self.actions.start_video_call.setEnabled(can_call)
        self.actions.start_chat_session.setEnabled(can_call)
        self.actions.send_sms.setEnabled(can_call)
        self.actions.send_files.setEnabled(can_call)
        self.actions.request_screen.setEnabled(can_call)
        self.actions.share_my_screen.setEnabled(can_call)
        self.actions.transfer_call.setEnabled(can_transfer)
        self.actions.edit_contact.setEnabled(model.contact_detail.editable)
        self.actions.delete_contact.setEnabled(model.contact_detail.deletable)
        menu.exec(event.globalPos())

    def hideEvent(self, event):
        self.context_menu.hide()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
            selected_indexes = self.selectionModel().selectedIndexes()
            item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
            if isinstance(item, ContactURI):
                selected_uri = item.uri
            else:
                selected_uri = contact.uri
            if item.group.settings is MessageContactsGroup():
                session_manager = MessageManager()
                session_manager.create_message_session(selected_uri)
            else:
                session_manager = SessionManager()
                session_manager.create_session(contact, selected_uri, contact.preferred_media.stream_descriptions, connect=contact.preferred_media.autoconnect)
        elif event.key() == Qt.Key.Key_Escape:
            self.animation.setDirection(QPropertyAnimation.Backward)
            self.animation.start()
        else:
            super(ContactDetailView, self).keyPressEvent(event)

    def paintEvent(self, event):
        super(ContactDetailView, self).paintEvent(event)
        if self.drop_indicator_index.isValid():
            rect = self.visualRect(self.drop_indicator_index)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QBrush(QColor('#dc3169')), 2.0))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.end()

    def startDrag(self, supported_actions):
        super(ContactDetailView, self).startDrag(supported_actions)
        main_window = QApplication.instance().main_window
        main_window.switch_view_button.dnd_active = False
        if not main_window.session_model.sessions:
            main_window.switch_view_button.view = SwitchViewButton.ContactView

    def dragEnterEvent(self, event):
        if event.source() is self:
            QApplication.instance().main_window.switch_view_button.dnd_active = True
        if set(event.mimeData().formats()).isdisjoint(self.model().accepted_mime_types):
            event.ignore()
        else:
            event.accept()

    def dragLeaveEvent(self, event):
        super(ContactDetailView, self).dragLeaveEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def dragMoveEvent(self, event):
        super(ContactDetailView, self).dragMoveEvent(event)

        model = self.model()
        mime_data = event.mimeData()

        for mime_type in model.accepted_mime_types:
            if mime_data.hasFormat(mime_type):
                self.viewport().update(self.visualRect(self.drop_indicator_index))
                self.drop_indicator_index = QModelIndex()
                index = self.indexAt(event.pos())
                rect = self.visualRect(index)
                item = index.data(Qt.ItemDataRole.UserRole)
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
        super(ContactDetailView, self).dropEvent(event)
        self.viewport().update(self.visualRect(self.drop_indicator_index))
        self.drop_indicator_index = QModelIndex()

    def _AH_DeleteContact(self):
        self.contact_list._AH_DeleteSelection()

    def _AH_EditContact(self):
        QApplication.instance().main_window.contact_editor_dialog.open_for_edit(self.model().contact)

    def _AH_MakeURIDefault(self):
        model = self.model()
        contact_uri = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        model.contact.uris.default = contact_uri.uri
        model.contact.save()

    def _AH_StartAudioCall(self, uri=None):
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        selected_indexes = self.selectionModel().selectedIndexes()
        item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = uri or contact.uri
        session_manager = SessionManager()
        session_manager.create_session(contact, selected_uri, [StreamDescription('audio')])

    def _AH_StartVideoCall(self, uri=None):
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        selected_indexes = self.selectionModel().selectedIndexes()
        item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = uri or contact.uri
        session_manager = SessionManager()
        session_manager.create_session(contact, selected_uri, [StreamDescription('audio'), StreamDescription('video')])

    def _AH_StartChatSession(self, uri=None):
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        selected_indexes = self.selectionModel().selectedIndexes()
        item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = uri or contact.uri
        session_manager = SessionManager()
        session_manager.create_session(contact, selected_uri, [StreamDescription('chat')], connect=False)

    def _AH_SendSMS(self, uri=None):
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        selected_indexes = self.selectionModel().selectedIndexes()
        item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = uri or contact.uri.uri
        session_manager = MessageManager()
        session_manager.create_message_session(selected_uri)

    def _AH_SendFiles(self, uri=None):
        session_manager = SessionManager()
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        selected_indexes = self.selectionModel().selectedIndexes()
        item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = uri or contact.uri
        for filename in QFileDialog.getOpenFileNames(self, translate('contact_list', 'Select File(s)'), session_manager.send_file_directory, 'Any file (*.*)')[0]:
            session_manager.send_file(contact, selected_uri, filename)

    def _AH_RequestScreen(self, uri=None):
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        selected_indexes = self.selectionModel().selectedIndexes()
        item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = uri or contact.uri
        session_manager = SessionManager()
        session_manager.create_session(contact, selected_uri, [StreamDescription('screen-sharing', mode='viewer'), StreamDescription('audio')])

    def _AH_ShareMyScreen(self, uri=None):
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        selected_indexes = self.selectionModel().selectedIndexes()
        item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = uri or contact.uri
        session_manager = SessionManager()
        session_manager.create_session(contact, selected_uri, [StreamDescription('screen-sharing', mode='server'), StreamDescription('audio')])

    def _AH_TransferCall(self, uri=None):
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        selected_indexes = self.selectionModel().selectedIndexes()
        item = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else None
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = uri or contact.uri
        session_manager = SessionManager()
        session_manager.active_session.transfer(selected_uri)

    def _DH_ApplicationXBlinkSession(self, event, index, rect, item):
        event.ignore(rect)

    def _DH_TextUriList(self, event, index, rect, item):
        if index.isValid():
            event.accept(rect)
            self.drop_indicator_index = index
        else:
            model = self.model()
            rect = self.viewport().rect()
            rect.setTop(self.visualRect(model.index(model.rowCount() - 1, 0)).bottom())
            event.accept(rect)

    def _SH_AnimationFinished(self):
        if self.animation.direction() == QPropertyAnimation.Direction.Forward:
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.hide()
            self.contact_list.setFocus(Qt.FocusReason.OtherFocusReason)

    def _SH_ModelContactDeleted(self):
        if self.isVisible():
            if self.animation.state() == QPropertyAnimation.State.Running:
                self.animation.pause()
                self.animation.setDirection(QPropertyAnimation.Direction.Backward)
                self.animation.resume()
            else:
                self.animation.setDirection(QPropertyAnimation.Direction.Backward)
                self.animation.start()

    def _SH_DoubleClicked(self, index):
        contact = self.contact_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        item = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(item, ContactURI):
            selected_uri = item.uri
        else:
            selected_uri = contact.uri
        if item.group.settings is MessageContactsGroup():
            session_manager = MessageManager()
            session_manager.create_message_session(selected_uri)
        else:
            session_manager = SessionManager()
            session_manager.create_session(contact, selected_uri, contact.preferred_media.stream_descriptions, connect=contact.preferred_media.autoconnect)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidChangeState(self, notification):
        session_manager = SessionManager()
        if notification.sender is session_manager.active_session and self.context_menu.isVisible():
            contact_has_uris = self.model().rowCount() > 1
            self.actions.transfer_call.setEnabled(contact_has_uris and notification.sender.state == 'connected')

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        session_manager = SessionManager()
        if notification.sender is session_manager.active_session and self.context_menu.isVisible():
            contact_has_uris = self.model().rowCount() > 1
            self.actions.transfer_call.setEnabled(contact_has_uris and 'audio' in notification.sender.streams)

    def _NH_BlinkActiveSessionDidChange(self, notification):
        if self.context_menu.isVisible():
            contact_has_uris = self.model().rowCount() > 1
            active_session = notification.data.active_session
            self.actions.transfer_call.setEnabled(contact_has_uris and active_session is not None and active_session.state == 'connected')


# The contact editor dialog
#

class ContactURIItem(object):
    def __init__(self, id, uri, type=None, default=False, ghost=False):
        self.id = id
        self.uri = uri
        self.type = type
        self.default = default
        self.ghost = ghost

    def __repr__(self):
        return "%s(%r, %r, type=%r, default=%r, ghost=%r)" % (self.__class__.__name__, self.id, self.uri, self.type, self.default, self.ghost)


class URITypeComboBox(QComboBox):
    builtin_types = (None,
                     QT_TRANSLATE_NOOP('contact_editor', "Mobile"),
                     QT_TRANSLATE_NOOP('contact_editor', "Home"),
                     QT_TRANSLATE_NOOP('contact_editor', "Work"),
                     QT_TRANSLATE_NOOP('contact_editor', "SIP"),
                     QT_TRANSLATE_NOOP('contact_editor', "XMPP"),
                     QT_TRANSLATE_NOOP('contact_editor', "Other"))

    def __init__(self, parent=None, types=()):
        super(URITypeComboBox, self).__init__(parent)
        self.setEditable(True)
        self.addItems((translate('contact_editor', item) for item in self.builtin_types))
        self.addItems(sorted(set(types) - set(self.builtin_types)))


class EmbeddedRadioButton(QRadioButton):
    """An embedded radio button that passes mouse events to its parent"""

    def mousePressEvent(self, event):
        super(EmbeddedRadioButton, self).mousePressEvent(event)
        self.parent().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super(EmbeddedRadioButton, self).mouseReleaseEvent(event)
        self.parent().mouseReleaseEvent(event)


class DefaultURIButton(QWidget):
    def __init__(self, parent=None, button_group=Null):
        super(DefaultURIButton, self).__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self.setAutoFillBackground(False)
        self.button = EmbeddedRadioButton(self)
        self.button.installEventFilter(self)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.layout.addWidget(self.button)
        self.layout.setAlignment(self.button, Qt.AlignmentFlag.AlignCenter)
        button_group.addButton(self.button)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusIn:
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        return False

    def isChecked(self):
        return self.button.isChecked()

    def setChecked(self, state):
        self.button.setChecked(state)


class ContactURIDelegate(QItemDelegate):
    def createEditor(self, parent, option, index):
        column = index.column()
        if column == ContactURIModel.TypeColumn:
            return URITypeComboBox(parent, types=index.model().uri_types)
        elif column == ContactURIModel.DefaultColumn:
            return DefaultURIButton(parent, index.model().button_group)
        return super(ContactURIDelegate, self).createEditor(parent, option, index)

    def setEditorData(self, widget, index):
        column = index.column()
        if column == ContactURIModel.TypeColumn:
            widget.setCurrentIndex(widget.findText(index.data(Qt.ItemDataRole.EditRole)))
        elif column == ContactURIModel.DefaultColumn:
            widget.setChecked(index.data(Qt.ItemDataRole.EditRole))
        else:
            super(ContactURIDelegate, self).setEditorData(widget, index)

    def setModelData(self, widget, model, index):
        column = index.column()
        if column == ContactURIModel.TypeColumn:
            model.setData(index, widget.currentText(), Qt.ItemDataRole.EditRole)
        elif column == ContactURIModel.DefaultColumn:
            model.setData(index, widget.isChecked(), Qt.ItemDataRole.EditRole)
        else:
            super(ContactURIDelegate, self).setModelData(widget, model, index)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def drawDisplay(self, painter, option, rect, text):
        if option.fontMetrics.size(Qt.TextFlag.TextSingleLine, text).width() > rect.width():
            # draw elided text using a fading gradient
            color_group = QPalette.ColorGroup.Disabled if not option.state & QStyle.StateFlag.State_Enabled else QPalette.ColorGroup.Normal if option.state & QStyle.StateFlag.State_Active else QPalette.ColorGroup.Inactive
            text_margin = option.widget.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, option.widget) + 1
            text_rect = rect.adjusted(text_margin, 0, -text_margin, 0)  # remove width padding
            width = text_rect.width()
            fade_start = 1 - 50.0 / width if width > 50 else 0.0
            gradient = QLinearGradient(0, 0, width, 0)
            gradient.setColorAt(fade_start, option.palette.color(color_group, QPalette.ColorRole.HighlightedText if option.state & QStyle.StateFlag.State_Selected else QPalette.ColorRole.Text))
            gradient.setColorAt(1.0, Qt.GlobalColor.transparent)
            painter.save()
            painter.setPen(QPen(QBrush(gradient), 1.0))
            painter.setClipRect(text_rect)
            painter.drawText(text_rect, Qt.TextFlag.TextSingleLine | int(option.displayAlignment), text)
            painter.restore()
        else:
            super(ContactURIDelegate, self).drawDisplay(painter, option, rect, text)


class ContactURIModel(QAbstractTableModel):
    columns = (QT_TRANSLATE_NOOP('contact_editor', 'Address'),
               QT_TRANSLATE_NOOP('contact_editor', 'Type'),
               QT_TRANSLATE_NOOP('contact_editor', 'Default'))

    AddressColumn = 0
    TypeColumn    = 1
    DefaultColumn = 2

    default_uri_type = 'SIP'

    def __init__(self, parent=None):
        super(ContactURIModel, self).__init__(parent)
        self.table_view = parent.addresses_table
        self.items = []
        self.uri_types = []
        self.button_group = QButtonGroup(parent)

    def flags(self, index):
        if index.isValid():
            return QAbstractTableModel.flags(self, index) | Qt.ItemFlag.ItemIsEditable
        else:
            return QAbstractTableModel.flags(self, index)

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, column = index.row(), index.column()
        item = self.items[row]
        if role == Qt.ItemDataRole.UserRole:
            return item
        elif role == Qt.ItemDataRole.DisplayRole:
            if column == ContactURIModel.AddressColumn:
                return translate('contact_list', 'Edit to add address') if item.ghost else str(item.uri or '')
        elif role == Qt.ItemDataRole.EditRole:
            if column == ContactURIModel.AddressColumn:
                return str(item.uri or '')
            elif column == ContactURIModel.TypeColumn:
                return item.type or ''
            elif column == ContactURIModel.DefaultColumn:
                return item.default
        elif role == Qt.ItemDataRole.ForegroundRole:
            if column == ContactURIModel.AddressColumn and item.ghost:
                return self.table_view.palette().brush(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text).color()
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        row, column = index.row(), index.column()
        if column == ContactURIModel.AddressColumn:
            item = self.items[row]
            item.uri = value
            if item.ghost and value:
                item.ghost = False
                self._add_item(ContactURIItem(None, None, self.default_uri_type, False, ghost=True))
        elif column == ContactURIModel.TypeColumn:
            self.items[row].type = value or None
        elif column == ContactURIModel.DefaultColumn:
            if value:
                for position, item in enumerate(self.items):
                    item.default = position == row
            else:
                self.items[row].default = False
        else:
            return False
        return True

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return translate('contact_editor', self.columns[section])
        return super(ContactURIModel, self).headerData(section, orientation, role)

    def init_with_address(self, address=None):
        items = [ContactURIItem(None, address, self.default_uri_type, False)] if address else []
        items.append(ContactURIItem(None, None, self.default_uri_type, False, ghost=True))
        self.beginResetModel()
        self.items = items
        self.uri_types = []
        self.button_group = QButtonGroup(self.table_view)
        self.endResetModel()
        for row in range(len(items)):
            self.table_view.openPersistentEditor(self.index(row, ContactURIModel.TypeColumn))
            self.table_view.openPersistentEditor(self.index(row, ContactURIModel.DefaultColumn))
        self.table_view.horizontalHeader().setSectionResizeMode(ContactURIModel.AddressColumn, self.table_view.horizontalHeader().ResizeMode.Stretch)

    def init_with_contact(self, contact):
        items = [ContactURIItem(uri.id, uri.uri, uri.type, default=uri is contact.uris.default) for uri in contact.uris]
        items.append(ContactURIItem(None, None, self.default_uri_type, False, ghost=True))
        self.beginResetModel()
        self.items = items
        self.uri_types = [uri.type for uri in contact.uris]
        self.button_group = QButtonGroup(self.table_view)
        self.endResetModel()
        for row in range(len(items)):
            self.table_view.openPersistentEditor(self.index(row, ContactURIModel.TypeColumn))
            self.table_view.openPersistentEditor(self.index(row, ContactURIModel.DefaultColumn))
        self.table_view.horizontalHeader().setSectionResizeMode(ContactURIModel.AddressColumn, self.table_view.horizontalHeader().ResizeMode.Stretch)

    def update_from_contact(self, contact):
        added_items = [item for item in self.items if item.id is None and not item.ghost]
        try:
            default_item = next(item for item in self.items if item.default)
        except StopIteration:
            default_item = None
        else:
            if default_item not in added_items:
                default_item = None  # only care for the default URI if it was a newly added one, else use the one from the contact
        items = [ContactURIItem(uri.id, uri.uri, uri.type, default=default_item is None and uri is contact.uris.default) for uri in contact.uris]
        items.extend(added_items)
        items.append(ContactURIItem(None, None, self.default_uri_type, False, ghost=True))
        self.beginResetModel()
        self.items = items
        self.uri_types = [item.type for item in items]
        self.button_group = QButtonGroup(self.table_view)
        self.endResetModel()
        for row in range(len(items)):
            self.table_view.openPersistentEditor(self.index(row, ContactURIModel.TypeColumn))
            self.table_view.openPersistentEditor(self.index(row, ContactURIModel.DefaultColumn))
        self.table_view.horizontalHeader().setSectionResizeMode(ContactURIModel.AddressColumn, self.table_view.horizontalHeader().ResizeMode.Stretch)

    def reset(self):
        self.beginResetModel()
        self.items = []
        self.uri_types = []
        self.button_group = QButtonGroup(self.table_view)
        self.endResetModel()

    def _add_item(self, item):
        position = len(self.items)
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.insert(position, item)
        self.endInsertRows()
        self.table_view.openPersistentEditor(self.index(position, ContactURIModel.TypeColumn))
        self.table_view.openPersistentEditor(self.index(position, ContactURIModel.DefaultColumn))

    def _remove_items(self, indexes):
        for row in sorted(set(index.row() for index in indexes if index.isValid()), reverse=True):
            self.beginRemoveRows(QModelIndex(), row, row)
            del self.items[row]
            self.endRemoveRows()


class ContactURITableView(QTableView):
    def __init__(self, parent=None):
        super(ContactURITableView, self).__init__(parent)
        self.setItemDelegate(ContactURIDelegate(self))
        self.context_menu = QMenu(self)
        self.context_menu.addAction(translate('contact_editor', "Delete"), self._AH_DeleteSelection)
        self.horizontalHeader().setSectionResizeMode(self.horizontalHeader().ResizeMode.ResizeToContents)

    def selectionChanged(self, selected, deselected):
        super(ContactURITableView, self).selectionChanged(selected, deselected)
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1, -1)
            selection_model.setCurrentIndex(index, selection_model.SelectionFlag.Select)

    def contextMenuEvent(self, event):
        selected_items = [item for item in (index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()) if not item.ghost]
        if selected_items:
            self.context_menu.exec(event.globalPos())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            selected_items = [item for item in (index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()) if not item.ghost]
            if selected_items:
                self._AH_DeleteSelection()
        else:
            super(ContactURITableView, self).keyPressEvent(event)

    def _AH_DeleteSelection(self):
        model = self.model()
        model._remove_items([index for index in self.selectionModel().selectedIndexes() if not index.data(Qt.ItemDataRole.UserRole).ghost])
        self.selectionModel().clearSelection()


ui_class, base_class = uic.loadUiType(Resources.get('contact_editor.ui'))


@implementer(IObserver)
class ContactEditorDialog(base_class, ui_class):

    def __init__(self, parent=None):
        super(ContactEditorDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.contact_uri_model = ContactURIModel(self)
        self.addresses_table.setModel(self.contact_uri_model)
        self.edited_contact = None
        self.target_group = None
        self.name_editor.textChanged.connect(self._SH_NameEditorTextChanged)
        self.accepted.connect(self._SH_Accepted)
        self.rejected.connect(self._SH_Rejected)
        self.rejected.connect(self.contact_uri_model.reset)

    def setupUi(self, contact_editor):
        super(ContactEditorDialog, self).setupUi(contact_editor)
        self.preferred_media.setItemData(0, translate('contact_editor', 'audio'))
        self.preferred_media.setItemData(1, translate('contact_editor', 'video'))
        self.preferred_media.setItemData(2, translate('contact_editor', 'chat'))
        self.preferred_media.setItemData(3, translate('contact_editor', 'audio+chat'))
        self.preferred_media.setItemData(4, translate('contact_editor', 'messages'))
        self.addresses_table.verticalHeader().setDefaultSectionSize(URITypeComboBox().sizeHint().height())

    def open_for_add(self, sip_address='', target_group=None):
        self.edited_contact = None
        self.target_group = target_group
        self.contact_uri_model.init_with_address(sip_address)
        self.name_editor.setText('')
        self.icon_selector.init_with_contact(None)
        self.presence.setChecked(True)
        self.preferred_media.setCurrentIndex(0)
        self.accept_button.setText(translate('contact_editor', 'Add'))
        self.accept_button.setEnabled(False)
        self.show()

    def open_for_edit(self, contact):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=contact)
        self.edited_contact = contact
        self.contact_uri_model.init_with_contact(contact)
        self.name_editor.setText(contact.name)
        self.icon_selector.init_with_contact(contact)
        self.presence.setChecked(contact.presence.subscribe)
        self.auto_answer.setChecked(contact.auto_answer)
        self.preferred_media.setCurrentIndex(self.preferred_media.findData(contact.preferred_media))
        self.accept_button.setText(translate('contact_editor', 'Ok'))
        self.accept_button.setEnabled(True)
        self.show()

    def _SH_NameEditorTextChanged(self, text):
        self.accept_button.setEnabled(text != '')

    def _SH_Accepted(self):
        if self.edited_contact is not None:
            notification_center = NotificationCenter()
            notification_center.remove_observer(self, sender=self.edited_contact)

        contact_model = self.parent().contact_model
        icon_manager = IconManager()

        if self.edited_contact is None:
            contact = addressbook.Contact()
        else:
            contact = self.edited_contact

        for id in set(contact.uris.ids()).difference(item.id for item in self.contact_uri_model.items):
            contact.uris.remove(contact.uris[id])
        for item in (item for item in self.contact_uri_model.items if item.uri):
            try:
                contact_uri = contact.uris[item.id]
            except KeyError:
                contact_uri = addressbook.ContactURI()
                contact.uris.add(contact_uri)
            contact_uri.uri = item.uri
            contact_uri.type = item.type
            if item.default:
                contact.uris.default = contact_uri

        contact.name = self.name_editor.text()
        contact.preferred_media = self.preferred_media.itemData(self.preferred_media.currentIndex())
        if self.presence.isChecked():
            contact.presence.policy = 'allow'
            contact.presence.subscribe = True
        else:
            contact.presence.policy = 'block'
            contact.presence.subscribe = False

        if self.auto_answer.isChecked():
            contact.auto_answer = True
        else:
            contact.auto_answer = False

        if self.icon_selector.filename is self.icon_selector.NotSelected:
            pass
        elif self.icon_selector.filename is None:
            icon_manager.remove(contact.id + '_alt')
            contact.alternate_icon = None
        else:
            icon_descriptor = IconDescriptor(FileURL(self.icon_selector.filename), str(int(os.stat(self.icon_selector.filename).st_mtime)))
            if contact.alternate_icon != icon_descriptor:
                icon_manager.store_file(contact.id + '_alt', icon_descriptor.url.path)
                contact.alternate_icon = icon_descriptor

        modified_settings = [contact]
        if self.target_group is not None:
            self.target_group.settings.contacts.add(contact)
            modified_settings.append(self.target_group.settings)
        contact_model._atomic_update(save=modified_settings)

        self.contact_uri_model.reset()
        self.edited_contact = None
        self.target_group = None

    def _SH_Rejected(self):
        if self.edited_contact is not None:
            notification_center = NotificationCenter()
            notification_center.remove_observer(self, sender=self.edited_contact)
        self.contact_uri_model.reset()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactDidChange(self, notification):
        contact = notification.sender
        modified_attributes = set(notification.data.modified)
        if 'name' in modified_attributes:
            self.name_editor.setText(contact.name)
        if 'presence.subscribe' in modified_attributes:
            self.presence.setChecked(contact.presence.subscribe)
        if 'preferred_media' in modified_attributes:
            self.preferred_media.setCurrentIndex(self.preferred_media.findData(contact.preferred_media))
        if modified_attributes.intersection(('uris', 'uris.default')):
            self.contact_uri_model.update_from_contact(contact)
        if 'icon' in modified_attributes:
            self.icon_selector.update_from_contact(contact)


del ui_class, base_class


class URIUtils(object):
    number_trim_re = re.compile(r'\(\s?0\s?\)|[-()\s]')
    number_re = re.compile(r'^\s*\+?[-\d\s()]+$')

    @classmethod
    def is_number(cls, token):
        return cls.number_re.match(token) is not None

    @classmethod
    def trim_number(cls, token):
        return cls.number_trim_re.sub('', token)

    @classmethod
    def find_contact(cls, uri, display_name=None, exact=True, instance_id=None):
        contact_model = QApplication.instance().main_window.contact_model
        if isinstance(uri, BaseSIPURI):
            uri = SIPURI.new(uri)
        else:
            if '@' not in uri:
                uri += '@' + AccountManager().default_account.id.domain
            if not uri.startswith(('sip:', 'sips:')):
                uri = 'sip:' + uri
            uri = SIPURI.parse(str(uri).translate(translation_table))

        if cls.is_number(uri.user.decode()):
            uri.user = cls.trim_number(uri.user.decode()).encode()
            is_number = True
        else:
            is_number = False

        # Exact URI matches
        for contact in (contact for contact in contact_model.iter_contacts() if contact.group.virtual):
            for contact_uri in contact.uris:
                if uri.matches(contact_uri.uri):
                    return contact, contact_uri

        if not exact and is_number:
            number = uri.user.decode().lstrip('0')
            counter = count()
            matched_numbers = []
            for contact in (contact for contact in contact_model.iter_contacts() if contact.group.virtual):
                for contact_uri in contact.uris:
                    uri_str = contact_uri.uri
                    if uri_str.startswith(('sip:', 'sips:')):
                        uri_str = uri_str.partition(':')[2]
                    contact_user = uri_str.partition('@')[0]
                    if cls.is_number(contact_user):
                        contact_user = cls.trim_number(contact_user)  # these could be expensive, maybe cache -Dan
                        if contact_user.endswith(number):
                            ratio = len(number) * 100 / len(contact_user)
                            if ratio >= 50:
                                heappush(matched_numbers, (100 - ratio, next(counter), contact, contact_uri))
            if matched_numbers:
                return matched_numbers[0][2:]  # ratio, index, contact, uri

        if instance_id:
            display_name = display_name or "Bonjour %s" % instance_id or "%s@%s" % (uri.user.decode(), uri.host.decode())
            contact = Contact(DummyContact(display_name, [DummyContactURI(str(uri), default=True)]), None)
        else:
            display_name = display_name or "%s@%s" % (uri.user.decode(), uri.host.decode())
            contact = Contact(DummyContact(display_name, [DummyContactURI(str(uri).partition(':')[2], default=True)]), None)
        return contact, contact.uri


