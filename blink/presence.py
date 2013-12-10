# Copyright (C) 2013 AG Projects. See LICENSE for details.
#

__all__ = ['PresenceManager', 'PendingWatcherDialog']

import base64
import hashlib
import re
import socket
import uuid

from PyQt4 import uic
from PyQt4.QtCore import Qt, QTimer

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null, limit
from datetime import datetime
from dateutil.tz import tzutc
from eventlib.green import urllib2
from itertools import chain
from twisted.internet import reactor
from twisted.internet.error import ConnectionLost
from zope.interface import implements

from sipsimple import addressbook
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.account.bonjour import BonjourPresenceState
from sipsimple.account.xcap import Icon, OfflineStatus
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads import caps, pidf, prescontent, rpid
from sipsimple.payloads import cipid; cipid # needs to be imported to register its namespace and extensions
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

from blink.configuration.datatypes import IconDescriptor, FileURL, PresenceState
from blink.configuration.settings import BlinkSettings
from blink.resources import IconManager, Resources
from blink.util import run_in_gui_thread


epoch = datetime.fromtimestamp(0, tzutc())


class BlinkPresenceState(object):
    def __init__(self, account):
        self.account = account

    @property
    def online_state(self):
        blink_settings = BlinkSettings()

        state = blink_settings.presence.current_state.state
        note = blink_settings.presence.current_state.note

        state = 'offline' if state=='Invisible' else state.lower()

        if self.account is BonjourAccount():
            return BonjourPresenceState(state, note)

        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = 'localhost'
        account_id = hashlib.md5(self.account.id).hexdigest()
        timestamp = ISOTimestamp.now()

        doc = pidf.PIDF(str(self.account.uri))

        person = pidf.Person('PID-%s' % account_id)
        person.timestamp = timestamp
        person.activities = rpid.Activities()
        person.activities.add(state)
        doc.add(person)

        if state == 'offline':
            service = pidf.Service('SID-%s' % account_id)
            service.status = 'closed'
            service.status.extended = state
            service.contact = str(self.account.uri)
            service.timestamp = timestamp
            service.capabilities = caps.ServiceCapabilities()
            doc.add(service)
        else:
            settings = SIPSimpleSettings()
            instance_id = str(uuid.UUID(settings.instance_id))
            service = pidf.Service('SID-%s' % instance_id)
            service.status = 'open'
            service.status.extended = state
            service.contact = str(self.account.contact.public_gruu or self.account.uri)
            service.timestamp = timestamp
            service.capabilities = caps.ServiceCapabilities()
            service.capabilities.audio = True
            service.capabilities.text = False
            service.capabilities.message = False
            service.capabilities.file_transfer = False
            service.capabilities.screen_sharing_server = False
            service.capabilities.screen_sharing_client = False
            service.display_name = self.account.display_name or None
            service.icon = "%s#blink-icon%s" % (self.account.xcap.icon.url, self.account.xcap.icon.etag) if self.account.xcap.icon is not None else None
            service.device_info = pidf.DeviceInfo(instance_id, description=hostname, user_agent=settings.user_agent)
            service.device_info.time_offset = pidf.TimeOffset()
            # TODO: Add real user input data -Saul
            service.user_input = rpid.UserInput()
            service.user_input.idle_threshold = 600
            service.add(pidf.DeviceID(instance_id))
            if note:
                service.notes.add(note)
            doc.add(service)

            device = pidf.Device('DID-%s' % instance_id, device_id=pidf.DeviceID(instance_id))
            device.timestamp = timestamp
            device.notes.add(u'%s at %s' % (settings.user_agent, hostname))
            doc.add(device)

        return doc

    @property
    def offline_state(self):
        blink_settings = BlinkSettings()

        if self.account is BonjourAccount() or not blink_settings.presence.offline_note:
            return None

        account_id = hashlib.md5(self.account.id).hexdigest()
        timestamp = ISOTimestamp.now()

        doc = pidf.PIDF(str(self.account.uri))

        person = pidf.Person('PID-%s' % account_id)
        person.timestamp = timestamp
        person.activities = rpid.Activities()
        person.activities.add('offline')
        doc.add(person)

        service = pidf.Service('SID-%s' % account_id)
        service.status = 'closed'
        service.status.extended = 'offline'
        service.contact = str(self.account.uri)
        service.timestamp = timestamp
        service.capabilities = caps.ServiceCapabilities()
        service.notes.add(blink_settings.presence.offline_note)
        doc.add(service)

        return doc


class PresencePublicationHandler(object):
    implements(IObserver)

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPAccountWillActivate')
        notification_center.add_observer(self, name='SIPAccountWillDeactivate')
        notification_center.add_observer(self, name='SIPAccountDidDiscoverXCAPSupport')
        notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        notification_center.add_observer(self, name='XCAPManagerDidReloadData')
        notification_center.add_observer(self, sender=BlinkSettings(), name='CFGSettingsObjectDidChange')

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SIPAccountWillActivate')
        notification_center.remove_observer(self, name='SIPAccountWillDeactivate')
        notification_center.remove_observer(self, name='SIPAccountDidDiscoverXCAPSupport')
        notification_center.remove_observer(self, name='SystemDidWakeUpFromSleep')
        notification_center.remove_observer(self, name='XCAPManagerDidReloadData')
        notification_center.remove_observer(self, sender=BlinkSettings(), name='CFGSettingsObjectDidChange')

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if notification.sender is BlinkSettings():
            account_manager = AccountManager()
            if 'presence.offline_note' in notification.data.modified:
                for account in (account for account in account_manager.get_accounts() if hasattr(account, 'xcap') and account.enabled and account.xcap.enabled and account.xcap.discovered):
                    state = BlinkPresenceState(account).offline_state
                    account.xcap_manager.set_offline_status(OfflineStatus(state) if state is not None else None)
            if 'presence.icon' in notification.data.modified:
                icon_data = IconManager().get_image('avatar')
                icon = Icon(icon_data, 'image/png') if icon_data is not None else None
                for account in (account for account in account_manager.get_accounts() if hasattr(account, 'xcap') and account.enabled and account.xcap.enabled and account.xcap.discovered):
                    account.xcap_manager.set_status_icon(icon)
            if 'presence.current_state' in notification.data.modified:
                for account in (account for account in account_manager.get_accounts() if account.enabled and account.presence.enabled):
                    account.presence_state = BlinkPresenceState(account).online_state
        else:
            account = notification.sender
            if set(['xcap.enabled', 'xcap.xcap_root']).intersection(notification.data.modified):
                account.xcap.icon = None
                account.save()
            elif set(['presence.enabled', 'display_name', 'xcap.icon']).intersection(notification.data.modified) and account.presence.enabled:
                account.presence_state = BlinkPresenceState(account).online_state

    def _NH_SIPAccountWillActivate(self, notification):
        account = notification.sender
        notification.center.add_observer(self, sender=account, name='CFGSettingsObjectDidChange')
        notification.center.add_observer(self, sender=account, name='SIPAccountGotSelfPresenceState')
        account.presence_state = BlinkPresenceState(account).online_state

    def _NH_SIPAccountWillDeactivate(self, notification):
        account = notification.sender
        notification.center.remove_observer(self, sender=account, name='CFGSettingsObjectDidChange')
        notification.center.remove_observer(self, sender=account, name='SIPAccountGotSelfPresenceState')

    def _NH_SIPAccountGotSelfPresenceState(self, notification):
        pidf_doc = notification.data.pidf
        services = [service for service in pidf_doc.services if service.status.extended is not None]
        if not services:
            return
        blink_settings = BlinkSettings()
        services.sort(key=lambda obj: obj.timestamp.value if obj.timestamp else epoch, reverse=True)
        service = services[0]
        if service.id in ('SID-%s' % uuid.UUID(SIPSimpleSettings().instance_id), 'SID-%s' % hashlib.md5(notification.sender.id).hexdigest()):
            # Our current state is the winning one
            return
        status = unicode(service.status.extended).title()
        note = None if not service.notes else unicode(list(service.notes)[0])
        if status == 'Offline':
            status = 'Invisible'
            note = None
        new_state = PresenceState(status, note)
        blink_settings.presence.current_state = new_state
        if new_state.note:
            try:
                next(state for state in blink_settings.presence.state_history if state==new_state)
            except StopIteration:
                blink_settings.presence.state_history = [new_state] + blink_settings.presence.state_history
            else:
                blink_settings.presence.state_history = [new_state] + [state for state in blink_settings.presence.state_history if state!=new_state]
        blink_settings.save()

    def _NH_SIPAccountDidDiscoverXCAPSupport(self, notification):
        account = notification.sender
        with account.xcap_manager.transaction():
            state = BlinkPresenceState(account).offline_state
            icon_data = IconManager().get_image('avatar')
            account.xcap_manager.set_offline_status(OfflineStatus(state) if state is not None else None)
            account.xcap_manager.set_status_icon(Icon(icon_data, 'image/png') if icon_data is not None else None)

    @run_in_gui_thread
    def _NH_XCAPManagerDidReloadData(self, notification):
        account = notification.sender.account
        blink_settings = BlinkSettings()
        icon_manager = IconManager()

        offline_status = notification.data.offline_status
        status_icon = notification.data.status_icon

        try:
            offline_note = next(note for service in offline_status.pidf.services for note in service.notes)
        except (AttributeError, StopIteration):
            offline_note = None

        blink_settings.presence.offline_note = offline_note
        blink_settings.save()

        if status_icon:
            icon_hash = hashlib.sha1(status_icon.data).hexdigest()
            icon_desc = IconDescriptor(status_icon.url, icon_hash)
            if not blink_settings.presence.icon or blink_settings.presence.icon.etag != icon_hash:
                icon = icon_manager.store_data('avatar', status_icon.data)
                blink_settings.presence.icon = IconDescriptor(FileURL(icon.filename), icon_hash) if icon is not None else None
                blink_settings.save()
        else:
            icon_desc = None
            icon_manager.remove('avatar')
            blink_settings.presence.icon = None
            blink_settings.save()

        account.xcap.icon = icon_desc
        account.save()


class ContactIcon(object):
    def __init__(self, data, descriptor):
        self.data = data
        self.descriptor = descriptor

    @classmethod
    def fetch(cls, url, etag=None, descriptor_etag=None):
        headers = {'If-None-Match': etag} if etag else {}
        req = urllib2.Request(url, headers=headers)
        try:
            response = urllib2.urlopen(req)
            content = response.read()
            info = response.info()
        except (ConnectionLost, urllib2.URLError, urllib2.HTTPError):
            return None
        content_type = info.getheader('content-type')
        etag = info.getheader('etag')
        if etag.startswith('W/'):
            etag = etag[2:]
        etag = etag.replace('\"', '')
        if content_type == prescontent.PresenceContentDocument.content_type:
            try:
                pres_content = prescontent.PresenceContentDocument.parse(content)
                data = base64.decodestring(pres_content.data.value)
            except Exception:
                return None
        return cls(data, IconDescriptor(url, descriptor_etag or etag))


class PresenceSubscriptionHandler(object):
    implements(IObserver)

    sip_prefix_re = re.compile("^sips?:")

    def __init__(self):
        self._pidf_map = {}
        self._winfo_map = {}
        self._winfo_timers = {}

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPAccountWillActivate')
        notification_center.add_observer(self, name='SIPAccountWillDeactivate')
        notification_center.add_observer(self, name='SIPAccountGotPresenceState')
        notification_center.add_observer(self, name='SIPAccountGotPresenceWinfo')

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SIPAccountWillActivate')
        notification_center.remove_observer(self, name='SIPAccountWillDeactivate')
        notification_center.remove_observer(self, name='SIPAccountGotPresenceState')
        notification_center.remove_observer(self, name='SIPAccountGotPresenceWinfo')
        self._pidf_map.clear()
        self._winfo_map.clear()
        for timer in self._winfo_timers.values():
            if timer.active():
                timer.cancel()
        self._winfo_timers.clear()

    @run_in_green_thread
    def _process_presence_data(self, uris=None):
        addressbook_manager = addressbook.AddressbookManager()

        def service_sort_key(service):
            timestamp = service.timestamp.value if service.timestamp else epoch
            if service.status.extended is not None:
                return (100, timestamp)
            elif service.status.basic == 'open':
                return (10, timestamp)
            else:
                return (0, timestamp)

        current_pidf_map = {}
        contact_pidf_map = {}

        # If no URIs were provided, process all of them
        if not uris:
            uris = list(chain(*(item.iterkeys() for item in self._pidf_map.itervalues())))

        for uri, pidf_list in chain(*(x.iteritems() for x in self._pidf_map.itervalues())):
            current_pidf_map.setdefault(uri, []).extend(pidf_list)

        for uri in uris:
            pidf_list = current_pidf_map.get(uri, [])
            for contact in (contact for contact in addressbook_manager.get_contacts() if uri in (self.sip_prefix_re.sub('', contact_uri.uri) for contact_uri in contact.uris)):
                contact_pidf_map.setdefault(contact, []).extend(pidf_list)

        for contact, pidf_list in contact_pidf_map.iteritems():
            if not pidf_list:
                state = note = icon = None
            else:
                services = list(chain(*(list(pidf_doc.services) for pidf_doc in pidf_list)))
                services.sort(key=service_sort_key, reverse=True)
                service = services[0]
                if service.status.extended:
                    state = unicode(service.status.extended)
                else:
                    state = 'available' if service.status.basic=='open' else 'offline'
                note = unicode(next(iter(service.notes))) if service.notes else None
                icon_url = unicode(service.icon) if service.icon else None

                if icon_url:
                    url, token, icon_hash = icon_url.partition('#blink-icon')
                    if token:
                        if contact.icon and icon_hash == contact.icon.etag:
                            # Fast path, icon hasn't changed
                            icon = None
                        else:
                            # New icon, client uses fast path mechanism
                            icon = ContactIcon.fetch(icon_url, etag=None, descriptor_etag=icon_hash)
                    else:
                        icon = ContactIcon.fetch(icon_url, etag=contact.icon.etag if contact.icon else None)
                else:
                    icon = None
            self._update_presence_state(contact, state, note, icon)

    @run_in_gui_thread
    def _update_presence_state(self, contact, state, note, icon):
        icon_manager = IconManager()
        contact.presence.state = state
        contact.presence.note = note
        if icon is not None:
            icon_manager.store_data(contact.id, icon.data)
            contact.icon = icon.descriptor
        contact.save()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        account = notification.sender
        if '__id__' in notification.data.modified:
            old_id = notification.data.modified['__id__'].old
            self._pidf_map.pop(old_id, None)
            self._winfo_map.pop(old_id, None)
            self._process_presence_data()
            return
        if set(['enabled', 'presence.enabled']).intersection(notification.data.modified):
            if not account.enabled or not account.presence.enabled:
                self._pidf_map.pop(account.id, None)
                self._winfo_map.pop(account.id, None)
                self._process_presence_data()

    def _NH_SIPAccountWillActivate(self, notification):
        if notification.sender is not BonjourAccount():
            notification.center.add_observer(self, sender=notification.sender, name='CFGSettingsObjectDidChange')
            notification.center.add_observer(self, sender=notification.sender, name='SIPAccountGotPresenceState')
            notification.center.add_observer(self, sender=notification.sender, name='SIPAccountGotPresenceWinfo')

    def _NH_SIPAccountWillDeactivate(self, notification):
        if notification.sender is not BonjourAccount():
            notification.center.remove_observer(self, sender=notification.sender, name='CFGSettingsObjectDidChange')
            notification.center.remove_observer(self, sender=notification.sender, name='SIPAccountGotPresenceState')
            notification.center.remove_observer(self, sender=notification.sender, name='SIPAccountGotPresenceWinfo')

    def _NH_SIPAccountGotPresenceState(self, notification):
        account = notification.sender
        new_pidf_map = dict((self.sip_prefix_re.sub('', uri), resource.pidf_list) for uri, resource in notification.data.resource_map.iteritems())
        if notification.data.full_state:
            self._pidf_map.setdefault(account.id, {}).clear()
        self._pidf_map[account.id].update(new_pidf_map)
        self._process_presence_data(new_pidf_map.keys())

    def _NH_SIPAccountGotPresenceWinfo(self, notification):
        addressbook_manager = addressbook.AddressbookManager()
        account = notification.sender
        watcher_list = notification.data.watcher_list

        self._winfo_map.setdefault(account.id, {})
        if notification.data.state == 'full':
            self._winfo_map[account.id].clear()

        for watcher in watcher_list:
            uri = self.sip_prefix_re.sub('', watcher.sipuri)
            if uri != account.id:
                # Skip own URI, XCAP may be down and policy may not be inplace yet
                self._winfo_map[account.id].setdefault(watcher.status, set()).add(uri)

        pending_watchers = self._winfo_map[account.id].setdefault('pending', set()) | self._winfo_map[account.id].setdefault('waiting', set())
        for uri in pending_watchers:
            # check if there is a policy
            try:
                next(policy for policy in addressbook_manager.get_policies() if policy.uri == uri and policy.presence.policy != 'default')
            except StopIteration:
                # check if there is a contact
                try:
                    next(contact for contact in addressbook_manager.get_contacts() if contact.presence.policy != 'default' and uri in (addr.uri for addr in contact.uris))
                except StopIteration:
                    # TODO: add display name -Saul
                    if uri not in self._winfo_timers:
                        self._winfo_timers[uri] = reactor.callLater(600, self._winfo_timers.pop, uri, None)
                        notification.center.post_notification('SIPAccountGotPendingWatcher', sender=account, data=NotificationData(uri=uri, display_name=None, event='presence'))


class PresenceManager(object):

    def __init__(self):
        self.publication_handler = PresencePublicationHandler()
        self.subscription_handler = PresenceSubscriptionHandler()

    def start(self):
        self.publication_handler.start()
        self.subscription_handler.start()

    def stop(self):
        self.publication_handler.stop()
        self.subscription_handler.stop()


ui_class, base_class = uic.loadUiType(Resources.get('pending_watcher.ui'))

class PendingWatcherDialog(base_class, ui_class):
    def __init__(self, account, uri, display_name, parent=None):
        super(PendingWatcherDialog, self).__init__(parent)
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        with Resources.directory:
            self.setupUi(self)
        addressbook_manager = addressbook.AddressbookManager()
        try:
            self.contact = next(contact for contact in addressbook_manager.get_contacts() if uri in (addr.uri for addr in contact.uris))
        except StopIteration:
            self.contact = None
        else:
            display_name = self.contact.name
            icon_manager = IconManager()
            icon = icon_manager.get(self.contact.id)
            if icon is not None:
                self.user_icon.setPixmap(icon.pixmap(32))
        self.account_label.setText(u'For account %s' % account.id)
        self.name_label.setText(display_name or uri)
        self.uri_label.setText(uri)
        font = self.name_label.font()
        font.setPointSizeF(self.uri_label.fontInfo().pointSizeF() + 3)
        font.setFamily("Sans Serif")
        self.name_label.setFont(font)
        self.accept_button.released.connect(self._accept_watcher)
        self.block_button.released.connect(self._block_watcher)
        self.position = None
        self.timer = QTimer()
        self.timer.timeout.connect(self._SH_TimerFired)
        self.timer.start(60000)

    def _SH_TimerFired(self):
        self.timer.stop()
        self.close()

    def _accept_watcher(self):
        self.timer.stop()
        if not self.contact:
            self.contact = addressbook.Contact()
            self.contact.name = self.name_label.text()
            self.contact.uris = [addressbook.ContactURI(uri=self.uri_label.text())]
        self.contact.presence.policy = 'allow'
        self.contact.presence.subscribe = True
        self.contact.save()

    def _block_watcher(self):
        self.timer.stop()
        policy = addressbook.Policy()
        policy.uri = self.uri_label.text()
        policy.name = self.name_label.text()
        policy.presence.policy = 'block'
        policy.save()

    def show(self, position=1):
        from blink import Blink
        blink = Blink()
        screen_geometry = blink.desktop().screenGeometry(self)
        available_geometry = blink.desktop().availableGeometry(self)
        main_window_geometry = blink.main_window.geometry()
        main_window_framegeometry = blink.main_window.frameGeometry()

        horizontal_decorations = main_window_framegeometry.width() - main_window_geometry.width()
        vertical_decorations = main_window_framegeometry.height() - main_window_geometry.height()
        width = limit(self.sizeHint().width(), min=self.minimumSize().width(), max=min(self.maximumSize().width(), available_geometry.width()-horizontal_decorations))
        height = limit(self.sizeHint().height(), min=self.minimumSize().height(), max=min(self.maximumSize().height(), available_geometry.height()-vertical_decorations))
        total_width = width + horizontal_decorations
        total_height = height + vertical_decorations
        x = limit(screen_geometry.center().x() - total_width/2, min=available_geometry.left(), max=available_geometry.right()-total_width)
        if position is None:
            y = -1
        elif position % 2 == 0:
            y = screen_geometry.center().y() + (position-1)*total_height/2
        else:
            y = screen_geometry.center().y() - position*total_height/2
        if available_geometry.top() <= y <= available_geometry.bottom() - total_height:
            self.setGeometry(x, y, width, height)
        else:
            self.resize(width, height)

        self.position = position
        super(PendingWatcherDialog, self).show()

del ui_class, base_class


