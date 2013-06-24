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
from sipsimple.payloads import caps, cipid, pidf, prescontent, rpid
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

from blink.configuration.datatypes import IconDescriptor, PresenceState
from blink.configuration.settings import BlinkSettings
from blink.resources import IconManager, Resources
from blink.util import run_in_gui_thread


epoch = datetime.fromtimestamp(0, tzutc())
sip_prefix_re = re.compile("^sips?:")
unknown_icon = "blink://unknown"


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
        try:
            self.hostname = socket.gethostname()
        except Exception:
            self.hostname = 'localhost'

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SIPAccountWillActivate')
        notification_center.remove_observer(self, name='SIPAccountWillDeactivate')
        notification_center.remove_observer(self, name='SIPAccountDidDiscoverXCAPSupport')
        notification_center.remove_observer(self, name='SystemDidWakeUpFromSleep')
        notification_center.remove_observer(self, name='XCAPManagerDidReloadData')
        notification_center.remove_observer(self, sender=BlinkSettings(), name='CFGSettingsObjectDidChange')

    def publish(self, accounts):
        bonjour_account = BonjourAccount()
        for account in accounts:
            if account is not bonjour_account:
                account.presence_state = self.build_pidf(account)
            else:
                blink_settings = BlinkSettings()
                account.presence_state = BonjourPresenceState(blink_settings.presence.current_state.state, blink_settings.presence.current_state.note)

    def build_pidf(self, account):
        blink_settings = BlinkSettings()
        presence_state = blink_settings.presence.current_state.state
        presence_note = blink_settings.presence.current_state.note

        if presence_state == 'Invisible':
            # Publish an empty offline state so that other clients are also synced
            return self.build_offline_pidf(account, None)

        doc = pidf.PIDF(str(account.uri))
        timestamp = ISOTimestamp.now()

        person = pidf.Person('PID-%s' % hashlib.md5(account.id).hexdigest())
        person.timestamp = pidf.PersonTimestamp(timestamp)
        doc.add(person)

        status = pidf.Status(basic='open')
        status.extended = presence_state.lower()

        person.activities = rpid.Activities()
        person.activities.add(unicode(status.extended))

        settings = SIPSimpleSettings()
        instance_id = str(uuid.UUID(settings.instance_id))

        service = pidf.Service('SID-%s' % instance_id, status=status)
        if presence_note:
            service.notes.add(presence_note)
        service.timestamp = pidf.ServiceTimestamp(timestamp)
        service.contact = pidf.Contact(str(account.contact.public_gruu or account.uri))
        if account.display_name:
            service.display_name = cipid.DisplayName(account.display_name)
        if account.icon:
            service.icon = cipid.Icon("%s#blink-icon%s" % (account.icon.url, account.icon.etag))
        else:
            service.icon = cipid.Icon(unknown_icon)
        service.device_info = pidf.DeviceInfo(instance_id, description=self.hostname, user_agent=settings.user_agent)
        service.device_info.time_offset = pidf.TimeOffset()
        service.capabilities = caps.ServiceCapabilities(audio=True, text=False)
        service.capabilities.message = False
        service.capabilities.file_transfer = False
        service.capabilities.screen_sharing_server = False
        service.capabilities.screen_sharing_client = False
        # TODO: Add real user input data -Saul
        service.user_input = rpid.UserInput()
        service.user_input.idle_threshold = 600
        service.add(pidf.DeviceID(instance_id))
        doc.add(service)

        device = pidf.Device('DID-%s' % instance_id, device_id=pidf.DeviceID(instance_id))
        device.timestamp = pidf.DeviceTimestamp(timestamp)
        device.notes.add(u'%s at %s' % (settings.user_agent, self.hostname))
        doc.add(device)

        return doc

    def build_offline_pidf(self, account, note=None):
        doc = pidf.PIDF(str(account.uri))
        timestamp = ISOTimestamp.now()

        account_hash = hashlib.md5(account.id).hexdigest()
        person = pidf.Person('PID-%s' % account_hash)
        person.timestamp = pidf.PersonTimestamp(timestamp)
        doc.add(person)

        person.activities = rpid.Activities()
        person.activities.add(u'offline')

        service = pidf.Service('SID-%s' % account_hash)
        service.status = pidf.Status(basic='closed')
        service.status.extended = u'offline'
        service.contact = pidf.Contact(str(account.uri))
        service.capabilities = caps.ServiceCapabilities()
        service.timestamp = pidf.ServiceTimestamp(timestamp)
        if note:
            service.notes.add(note)
        doc.add(service)

        return doc

    def set_xcap_offline_note(self, accounts):
        blink_settings = BlinkSettings()
        for account in accounts:
            status = OfflineStatus(self.build_offline_pidf(account, blink_settings.presence.offline_note)) if blink_settings.presence.offline_note else None
            account.xcap_manager.set_offline_status(status)

    def set_xcap_icon(self, accounts):
        blink_settings = BlinkSettings()
        try:
            icon = Icon(file(blink_settings.presence.icon.url.path).read(), 'image/png')
        except Exception:
            icon = None
        for account in accounts:
            account.xcap_manager.set_status_icon(icon)

    @run_in_gui_thread
    def _save_icon(self, icon_data, icon_hash):
        blink_settings = BlinkSettings()
        if icon_data is not None is not icon_hash:
            icon_manager = IconManager()
            icon = icon_manager.store_data('avatar', icon_data)
            blink_settings.presence.icon = IconDescriptor('file://' + icon.filename, icon_hash) if icon is not None else None
        else:
            blink_settings.presence.icon = None
        blink_settings.save()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if notification.sender is BlinkSettings():
            account_manager = AccountManager()
            if set(['presence.icon', 'presence.offline_note']).intersection(notification.data.modified):
                # TODO: use a transaction here as well? -Dan
                accounts = [account for account in account_manager.get_accounts() if hasattr(account, 'xcap') and account.enabled and account.xcap.enabled and account.xcap.discovered]
                if 'presence.offline_note' in notification.data.modified:
                    self.set_xcap_offline_note(accounts)
                if 'presence.icon' in notification.data.modified:
                    self.set_xcap_icon(accounts)
            if 'presence.current_state' in notification.data.modified:
                accounts = [account for account in account_manager.get_accounts() if account.enabled and account.presence.enabled]
                self.publish(accounts)
        else:
            account = notification.sender
            if set(['xcap.enabled', 'xcap.xcap_root']).intersection(notification.data.modified):
                account.icon = None
            if set(['presence.enabled', 'display_name', 'xcap.enabled', 'xcap.xcap_root']).intersection(notification.data.modified) and account.presence.enabled:
                self.publish([account])

    def _NH_SIPAccountWillActivate(self, notification):
        account = notification.sender
        notification.center.add_observer(self, sender=account, name='CFGSettingsObjectDidChange')
        if account is not BonjourAccount():
            notification.center.add_observer(self, sender=account, name='SIPAccountGotSelfPresenceState')
            account.icon = None
        self.publish([account])

    def _NH_SIPAccountWillDeactivate(self, notification):
        account = notification.sender
        notification.center.remove_observer(self, sender=account, name='CFGSettingsObjectDidChange')
        if account is not BonjourAccount():
            notification.center.remove_observer(self, sender=account, name='SIPAccountGotSelfPresenceState')
            account.icon = None

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
                state = next(state for state in blink_settings.presence.state_history if state==new_state)
            except StopIteration:
                blink_settings.presence.state_history = [new_state] + blink_settings.presence.state_history
            else:
                blink_settings.presence.state_history = [new_state] + [state for state in blink_settings.presence.state_history if state!=new_state]
        blink_settings.save()

    def _NH_SIPAccountDidDiscoverXCAPSupport(self, notification):
        account = notification.sender
        with account.xcap_manager.transaction():
            self.set_xcap_offline_note([account])
            self.set_xcap_icon([account])

    def _NH_XCAPManagerDidReloadData(self, notification):
        account = notification.sender.account
        blink_settings = BlinkSettings()

        offline_status = notification.data.offline_status
        status_icon = notification.data.status_icon

        try:
            offline_note = next(note for service in offline_status.pidf.services for note in service.notes)
        except (AttributeError, StopIteration):
            offline_note = None

        blink_settings.presence.offline_note = offline_note
        blink_settings.save()

        if status_icon:
            icon_desc = IconDescriptor(notification.sender.status_icon.uri, notification.sender.status_icon.etag)
            icon_hash = hashlib.sha512(status_icon.data).hexdigest()
            if blink_settings.presence.icon and blink_settings.presence.icon.etag == icon_hash:
                # Icon didn't change
                pass
            else:
                self._save_icon(status_icon.data, icon_hash)
            if icon_desc != account.icon:
                account.icon = icon_desc
                self.publish([account])
        else:
            # TODO: remove local icon?
            pass


class PresenceSubscriptionHandler(object):
    implements(IObserver)

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

    def _download_icon(self, url, etag):
        headers = {'If-None-Match': etag} if etag else {}
        req = urllib2.Request(url, headers=headers)
        try:
            response = urllib2.urlopen(req)
            content = response.read()
            info = response.info()
        except (ConnectionLost, urllib2.HTTPError, urllib2.URLError):
            return None, None
        content_type = info.getheader('content-type')
        etag = info.getheader('etag')
        if etag.startswith('W/'):
            etag = etag[2:]
        etag = etag.replace('\"', '')
        if content_type == prescontent.PresenceContentDocument.content_type:
            try:
                pres_content = prescontent.PresenceContentDocument.parse(content)
                content = base64.decodestring(pres_content.data.value)
            except Exception:
                return None, None
        return content, etag

    @run_in_green_thread
    def _process_presence_data(self, uris=None):
        addressbook_manager = addressbook.AddressbookManager()

        current_pidf_map = {}
        contact_pidf_map = {}

        # If no URIs were provided, process all of them
        if not uris:
            uris = list(chain(*(item.iterkeys() for item in self._pidf_map.itervalues())))

        for uri, pidf_list in chain(*(x.iteritems() for x in self._pidf_map.itervalues())):
            current_pidf_map.setdefault(uri, []).extend(pidf_list)

        for uri in uris:
            pidf_list = current_pidf_map.get(uri, [])
            for contact in (contact for contact in addressbook_manager.get_contacts() if uri in (sip_prefix_re.sub('', contact_uri.uri) for contact_uri in contact.uris)):
                contact_pidf_map.setdefault(contact, []).extend(pidf_list)

        for contact, pidf_list in contact_pidf_map.iteritems():
            if not pidf_list:
                state = note = icon_descriptor = icon_data = None
            else:
                services = list(chain(*(list(pidf_doc.services) for pidf_doc in pidf_list)))
                services.sort(key=lambda obj: obj.timestamp.value if obj.timestamp else epoch, reverse=True)
                service = services[0]
                if service.status.extended:
                    state = unicode(service.status.extended)
                else:
                    state = 'available' if service.status.basic=='open' else 'offline'
                note = unicode(next(iter(service.notes))) if service.notes else None
                icon = unicode(service.icon) if service.icon else None

                # review this logic (add NotChanged, NoIcon, ... markers to better represent the icon data and icon descriptor) -Dan
                icon_data = icon_descriptor = None
                if icon and icon != unknown_icon:
                    if 'blink-icon' in icon and contact.icon and icon == contact.icon.url:
                        # Fast path, icon hasn't changed
                        pass
                    else:
                        icon_data, etag = self._download_icon(icon, contact.icon.etag if contact.icon else None)
                        if icon_data:
                            icon_descriptor = IconDescriptor(icon, etag)
            self._update_contact_presence_state(contact, state, note, icon_descriptor, icon_data)

    @run_in_gui_thread
    def _update_contact_presence_state(self, contact, state, note, icon_descriptor, icon_data):
        contact.presence.state = state
        contact.presence.note = note
        if icon_data:
            IconManager().store_data(contact.id, icon_data)
            contact.icon = icon_descriptor
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
        new_pidf_map = dict((sip_prefix_re.sub('', uri), resource.pidf_list) for uri, resource in notification.data.resource_map.iteritems())
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
            uri = sip_prefix_re.sub('', watcher.sipuri)
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


