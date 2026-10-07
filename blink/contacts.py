
import json
import pickle as pickle
import locale
import os
import re
import socket
import sys
import threading
import time

from PyQt6 import uic
from PyQt6.QtCore import Qt, QAbstractListModel, QAbstractTableModel, QEasingCurve, QModelIndex, QPropertyAnimation, QSortFilterProxyModel
from PyQt6.QtCore import QByteArray, QEvent, QLocale, QMimeData, QPoint, QPointF, QRectF, QRect, QSize, QTimer, QUrl, pyqtSignal, QT_TRANSLATE_NOOP
from PyQt6.QtGui import QAction, QBrush, QColor, QFont, QFontMetrics, QFontMetricsF, QIcon, QKeyEvent, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QItemDelegate, QStyledItemDelegate, QStyle
from PyQt6.QtWidgets import QButtonGroup, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QListView, QMenu, QMessageBox, QRadioButton, QTableView, QWidget

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData, ObserverWeakrefProxy
from application.python.descriptor import WriteOnceAttribute
from application.python.threadpool import ThreadPool, run_in_threadpool
from application.python.types import MarkerType, Singleton
from application.python import Null
from application.system import makedirs, unlink
from collections import OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import lru_cache, partial
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from heapq import heappush
from itertools import count
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request, AuthorizedSession
from google.auth.exceptions import RefreshError as AccessTokenRefreshError
from operator import attrgetter
from requests import RequestException
from threading import Event
from urllib.parse import parse_qsl
from zope.interface import implementer

from sipsimple import addressbook
from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.account.bonjour import BonjourServiceDescription
from sipsimple.configuration import ConfigurationManager, DefaultValue, Setting, SettingsState, SettingsObjectMeta, ObjectNotFoundError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import BaseSIPURI, SIPURI
from sipsimple.threading import run_in_thread, run_in_twisted_thread
from sipsimple.threading.green import Command

from blink.configuration.datatypes import IconDescriptor, FileURL
from blink.configuration.settings import BlinkSettings
from blink import addressbook_notify, addressbook_origin
from blink.contact_repair import merge_plan, repair_plan
from blink.group_kinds import CALLS, CONFERENCE, STAMPED_KINDS, TEL, find_group, group_kind, is_group, stamp_plan
from blink.pstn_normalize import canonical_pstn_uri, is_conference_uri, pstn_e164
from blink.logging import ActivityLog
from blink.resources import ApplicationData, Resources, IconManager, themed_icon
from blink.sessions import SessionManager, StreamDescription
from blink.message_envelopes import this_device_id
from blink.messages import MessageManager
from blink.uris import bare_instance_id, bonjour_placeholder_uri, canonical_uri, is_fileable_address, is_instance_id, placeholder_instance_id
from blink.util import call_in_gui_thread, call_later, run_in_gui_thread, translate
from blink.widgets.buttons import SwitchViewButton
from blink.widgets.color import ColorHelperMixin, follow_theme, is_dark_theme, secondary_text_color
from blink.widgets.util import ContextMenuActions, FontScaledSize, badge_font


__all__ = ['Group', 'Contact', 'ContactModel', 'ContactSearchModel', 'ContactListView', 'ContactSearchListView', 'ContactEditorDialog', 'URIUtils']

translation_table = dict.fromkeys(map(ord, ' \t'), None)


# The Messages group: a real addressbook group, shared through XCAP with Blink
# for macOS (same reserved id), listing who this account has conversations with.
MESSAGES_GROUP_ID = '_messages'
MESSAGES_GROUP_NAME = 'Messages'


def is_messages_group(group_settings):
    """Whether a contact list group is the Messages group (the shared one, or the old virtual one)."""
    return group_settings is not None and (getattr(group_settings, 'id', None) == MESSAGES_GROUP_ID or group_settings is MessageContactsGroup())


def start_contact_conversation(contact, contact_uri):
    """What double-click / Enter on a contact does: open the conversation in the message pane for
    a contact in the Messages group or whose preferred media is messages, otherwise start a session
    with its preferred media."""
    if is_messages_group(getattr(contact.group, 'settings', None)) or contact.preferred_media == 'messages':
        QApplication.instance().main_window.show_conversation_in_pane(contact, contact_uri)
    else:
        SessionManager().create_session(contact, contact_uri, contact.preferred_media.stream_descriptions, connect=contact.preferred_media.autoconnect)


def is_fileable_key(key):
    """Whether a conversation key may become an addressbook contact: an address
    (user@host) or a phone number (blink.uris.is_fileable_address). A Bonjour
    neighbour's instance id or loopback placeholder is not one."""
    key = str(key or '').strip()
    if not key or is_instance_id(key) or placeholder_instance_id(key):
        return False
    return is_fileable_address(key)


def is_bonjour_address(uri):
    """Whether this address is a Bonjour neighbour's, in any spelling: its instance
    id, the placeholder standing in for it (sip:<id>@bonjour.local), or an address a
    neighbour is announced at on this network. Never written to the addressbook: the
    XCAP document is shared by every device of the account, a neighbour exists only
    here and its link-local address means something else on any other network."""
    text = str(uri or '').strip()
    if not text:
        return False
    if is_instance_id(text) or placeholder_instance_id(text):
        return True
    try:
        contact_model = QApplication.instance().main_window.contact_model
    except AttributeError:
        return False
    return URIUtils._bonjour_neighbour_at(contact_model, text) is not None


# Groups the software keeps by itself: their membership is not the user's to
# change by hand (macOS BlinkGroup add/remove_contact_allowed). Deleted is
# patch 49's: removed conversations, membership set by removal and restore.
DELETED_GROUP_ID = '_deleted'


def is_managed_group(group_settings):
    if group_settings is None:
        return False
    if is_messages_group(group_settings):
        return True
    if isinstance(group_settings, VirtualGroup):
        return False
    return (getattr(group_settings, 'id', None) == DELETED_GROUP_ID or is_group(group_settings, CALLS) or
            is_group(group_settings, TEL) or is_group(group_settings, CONFERENCE))


def publish_contact_for_groups(contact):
    """Make a just-created contact safe to put in a group (macOS _publish_contact_for_groups).

    sipsimple serialises a group from each member's __xcapcontact__, which only
    the asynchronous save fills in. When several new contacts are added to a
    group in one go, the group's save can run before a member's and the
    file-io thread fails on a None member. Filling it now is what the save is
    about to do anyway, with the same value.
    """
    try:
        if getattr(contact, '__xcapcontact__', None) is None:
            contact.__xcapcontact__ = contact.__toxcap__()
    except Exception as e:
        ActivityLog().warning(f'[contacts] Cannot prepare {contact.name} for its groups: {e!r}')


def xcap_is_expected():
    """Whether the addressbook is going to be filled from a server: then a missing group
    may only not have arrived yet, and creating one is how an account gets two."""
    try:
        return any(account.enabled and account.xcap.enabled for account in AccountManager().get_accounts() if account is not BonjourAccount())
    except Exception:
        return True


class ContactTrash(object):
    """Sylk Mobile's and Blink for macOS's two-stage contact delete.

    Stage 1, Delete: the contact moves to the Deleted group ('_deleted') and out
    of Messages, and every conversation under its addresses is tombstoned.
    Nothing leaves the disk and nothing is said to the server: Restore puts it
    back as it was, and a newer message brings it back by itself.
    Stage 2, Delete Permanently (from inside Deleted): the contact is deleted
    from XCAP, the history of every address no other contact lists is erased
    (HistoryManager, on the deletion) and the other devices are asked to drop
    those conversations. An address another contact lists gets its messages back.
    """

    @staticmethod
    def _keys(contact):
        keys = []
        for uri in contact.uris:
            key = canonical_uri(str(uri.uri))
            if key and key not in keys and not is_instance_id(key):
                keys.append(key)
        return keys

    @staticmethod
    def deleted_group(create=False):
        manager = addressbook.AddressbookManager()
        try:
            return manager.get_group(DELETED_GROUP_ID)
        except KeyError:
            if not create:
                return None
        group = addressbook.Group(id=DELETED_GROUP_ID)
        group.name = 'Deleted'
        group.position = None
        group.save()
        ActivityLog().info(f'[trash] Created the Deleted group ({DELETED_GROUP_ID})')
        return group

    @classmethod
    def leave_deleted(cls, contact, why):
        """Take a contact out of Deleted (it is back in use); caller saves nothing else."""
        group = cls.deleted_group()
        if group is not None and contact.id in {member.id for member in group.contacts}:
            group.contacts.remove(contact)
            group.save()
            ActivityLog().info(f'[trash] Contact {contact.name or contact.id} left the Deleted group: {why}')

    @classmethod
    def soft_delete(cls, contacts):
        from blink.history import HistoryManager
        history = HistoryManager().message_history
        with addressbook_origin.reason('delete'), addressbook.AddressbookManager.transaction():
            deleted_group = cls.deleted_group(create=True)
            try:
                messages_group = addressbook.AddressbookManager().get_group(MESSAGES_GROUP_ID)
            except KeyError:
                messages_group = None
            for contact in contacts:
                keys = cls._keys(contact)
                for key in keys:
                    history.tombstone_conversation(key)
                if contact.id not in {member.id for member in deleted_group.contacts}:
                    deleted_group.contacts.add(contact)
                if messages_group is not None and contact.id in {member.id for member in messages_group.contacts}:
                    messages_group.contacts.remove(contact)
                ActivityLog().info(f"[trash] Contact {contact.name or contact.id} moved to Deleted, conversations hidden: {', '.join(keys) or 'none'}")
            deleted_group.save()
            if messages_group is not None:
                messages_group.save()

    @classmethod
    def restore(cls, contacts):
        from blink.history import HistoryManager
        history = HistoryManager().message_history
        with addressbook_origin.reason('restore'), addressbook.AddressbookManager.transaction():
            group = cls.deleted_group()
            for contact in contacts:
                for key in cls._keys(contact):
                    history.restore_conversation(key)
                if group is not None and contact.id in {member.id for member in group.contacts}:
                    group.contacts.remove(contact)
                ActivityLog().info(f'[trash] Contact {contact.name or contact.id} restored from Deleted')
            if group is not None:
                group.save()
        # back into Messages when it has a conversation (MessagesGroupFiler files from history)
        if BlinkSettings().interface.show_messages_group:
            history.get_all_contacts()

    @classmethod
    def delete_permanently(cls, contacts):
        from blink.history import HistoryManager
        history = HistoryManager().message_history
        going = {contact.id for contact in contacts}
        claimed = set()
        for other in addressbook.AddressbookManager().get_contacts():
            if other.id not in going:
                claimed.update(canonical_uri(str(uri.uri)) for uri in other.uris)
        kept, purged = [], []
        for contact in contacts:
            for key in cls._keys(contact):
                (kept if key in claimed else purged).append(key)
        with addressbook_origin.reason('delete-permanently'), addressbook.AddressbookManager.transaction():
            group = cls.deleted_group()
            if group is not None:
                for contact in contacts:
                    if contact.id in {member.id for member in group.contacts}:
                        group.contacts.remove(contact)
                group.save()
            for contact in contacts:
                ActivityLog().info(f'[trash] Contact {contact.name or contact.id} deleted permanently')
                contact.delete()       # HistoryManager erases the history no other contact claims
        for key in kept:
            ActivityLog().info(f'[trash] Keeping the conversation with {key}: another contact lists it')
            history.restore_conversation(key)
        MessageManager().announce_conversation_removal(sorted(set(purged)))


@implementer(IObserver)
class CallsGroupFiler(object, metaclass=Singleton):
    """File the other party of every audio or video call, as Blink for macOS and Sylk Mobile do.

    A person goes into Calls, and into Tel as well when the address is a phone
    number. A conference room goes into Conference only: it is a place, not
    somebody called. An existing contact is only filed, never edited; a new
    one is named after the party (a room after its number). Groups are found
    by kind, name or reserved id and created when missing, but only once the
    addressbook reflects the server.
    """

    settle_delay = 15  # seconds after the first XCAP reload

    def __init__(self):
        self.xcap_loaded = False
        self.deferred = []          # calls that ended before a missing group could be created
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPSessionDidStart')
        notification_center.add_observer(self, name='SIPSessionDidEnd')
        notification_center.add_observer(self, name='SIPSessionDidFail')
        notification_center.add_observer(self, name='XCAPManagerDidReloadData')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_XCAPManagerDidReloadData(self, notification):
        if not self.xcap_loaded:
            call_later(self.settle_delay, self._addressbook_loaded)

    def _addressbook_loaded(self):
        if self.xcap_loaded:
            return
        self.xcap_loaded = True
        deferred, self.deferred = self.deferred, []
        for remote_uri, display_name, account in deferred:
            ActivityLog().info(f'[contacts] Filing the call with {remote_uri} now that the addressbook has loaded')
            try:
                self.file(remote_uri, display_name, account)
            except Exception as e:
                ActivityLog().exception(f'[contacts] Filing the call with {remote_uri} failed: {e!r}')

    @staticmethod
    def _session_party(session):
        """The other party of a SIP session as user@host, or None."""
        identity = getattr(session, 'remote_identity', None)
        if identity is None:
            return None
        user, host = identity.uri.user, identity.uri.host
        user = user.decode() if isinstance(user, bytes) else user
        host = host.decode() if isinstance(host, bytes) else host
        return '%s@%s' % (user, host)

    def _file_session(self, session):
        account = getattr(session, 'account', None)
        party = self._session_party(session)
        if account is None or account is BonjourAccount() or party is None:
            return
        try:
            self.file(party, session.remote_identity.display_name, account)
        except Exception as e:
            ActivityLog().exception(f'[contacts] Filing the call with {party} failed: {e!r}')

    def _NH_SIPSessionDidStart(self, notification):
        # A conference room is filed as soon as it is joined: hanging up its audio
        # leaves the chat stream going, so the session may not end for a long time.
        session = notification.sender
        if is_conference_uri(self._session_party(session), getattr(session, 'account', None)):
            self._file_session(session)

    def _NH_SIPSessionDidEnd(self, notification):
        # A call (audio or video) is filed when it ends or fails, answered or not.
        # A conference room is filed whatever the media: Join Conference may be chat only.
        session = notification.sender
        streams = [stream.type for stream in (session.streams or session.proposed_streams or ())]
        if 'audio' in streams or 'video' in streams or is_conference_uri(self._session_party(session), getattr(session, 'account', None)):
            self._file_session(session)

    _NH_SIPSessionDidFail = _NH_SIPSessionDidEnd

    def ensure_group(self, identity, reserved_id):
        """The group for `identity`, stamped with its kind if it has none, or created; None while it may not have arrived."""
        groups = list(addressbook.AddressbookManager().get_groups())
        group = find_group(groups, identity)
        if group is not None:
            if not group_kind(group):
                with addressbook_origin.reason('group-kind'):
                    group.kind = identity.kind
                    group.save()
                ActivityLog().info(f"[addressbook] Stamped group '{group.name}' (id={group.id}) with kind={identity.kind}")
            return group
        if not self.xcap_loaded and xcap_is_expected():
            ActivityLog().info(f"[addressbook] No '{identity.name}' group yet, waiting for the addressbook to arrive before creating one")
            return None
        with addressbook_origin.reason('ensure-group'):
            group = addressbook.Group(reserved_id) if reserved_id else addressbook.Group()
            group.name = identity.name
            group.kind = identity.kind
            group.position = None
            group.save()
        ActivityLog().info(f"[addressbook] Created group '{group.name}' (id={group.id}) with kind={identity.kind}")
        return group

    def file(self, remote_uri, display_name, account):
        activity = ActivityLog()
        address = canonical_pstn_uri(remote_uri, account)
        if not is_fileable_address(address, account):
            activity.info(f'[contacts] Not filing the call with {remote_uri}: neither an address nor a number')
            return None
        if is_bonjour_address(address):
            activity.info(f'[contacts] Not filing the call with {remote_uri}: a Bonjour neighbour is not written to the addressbook')
            return None
        conference = is_conference_uri(address, account)
        e164 = pstn_e164(address, account)
        with addressbook_origin.reason('call-history'):
            contact, contact_uri = URIUtils.find_contact(address)
            contact = contact.settings if contact.type == 'addressbook' else MessagesGroupFiler._find_by_canonical(address, addressbook.AddressbookManager().get_contacts())
            created = contact is None
            if created:
                contact = addressbook.Contact()
                contact.name = address.partition('@')[0] if conference else (display_name or address)
                contact.uris = [addressbook.ContactURI(uri=address, type='tel' if e164 else 'SIP')]
                contact.preferred_media = 'audio'
                contact.save()
                publish_contact_for_groups(contact)
            targets = [(CONFERENCE, None)] if conference else [(CALLS, '_calls')] + ([(TEL, None)] if e164 else [])
            joined = []
            for identity, reserved_id in targets:
                group = self.ensure_group(identity, reserved_id)
                if group is None:
                    # not created before the addressbook has loaded: filed again then
                    if (remote_uri, display_name, account) not in self.deferred:
                        self.deferred.append((remote_uri, display_name, account))
                    continue
                if contact.id in {member.id for member in group.contacts}:
                    continue
                with addressbook.AddressbookManager.transaction():
                    group.contacts.add(contact)
                    group.save()
                joined.append(group.name)
        if created or joined:
            activity.info(f"[contacts] {'Created' if created else 'Filed'} contact {contact.name} <{address}>" + (f" -> {', '.join(joined)}" if joined else ''))
        return contact


@implementer(IObserver)
class ContactRepair(object, metaclass=Singleton):
    """Repair the addressbook once per run, after it has loaded, as Blink for macOS does.

    - addresses: a conference room back on its bridge domain (conference-domain),
      a phone number in E.164 by the default account's dial rules (e164)
    - names that are only an old spelling of the contact's address (echoed-name)
    - duplicate addresses within a contact (dup-uri)
    - every phone number filed in Tel and every conference room in Conference
      (file-into-kind-group)
    Nothing is created or deleted but groups and duplicate addresses; a real
    name is never touched. Each action is logged with its reason and stamped
    with it (modified_reason). Sylk Mobile does the same from its side.
    """

    settle_delay = 15  # seconds after the first XCAP reload

    def __init__(self):
        self.done = False
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='XCAPManagerDidReloadData')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        if not xcap_is_expected():
            call_later(5, self.run)

    def _NH_XCAPManagerDidReloadData(self, notification):
        if not self.done:
            # a little later than the filers, so groups created by them are in place
            call_later(self.settle_delay + 5, self.run)

    def run(self):
        if self.done:
            return
        self.done = True
        activity = ActivityLog()
        try:
            # healing what the server handed us: every device does it on its own, nothing to announce
            with AddressbookNotifier().quiet():
                repaired = self.repair_contacts()
                filed = self.file_into_kind_groups()
                merged = self.merge_messages_duplicates()
        except Exception as e:
            activity.exception(f'[addressbook] Repairing the addressbook failed: {e!r}')
            return
        activity.info(f'[addressbook] Repair done: {repaired} contacts repaired, {filed} contacts filed into Tel or Conference, {merged} duplicates merged')

    def repair_contacts(self):
        activity = ActivityLog()
        manager = addressbook.AddressbookManager()
        account = AccountManager().default_account
        repaired = 0
        for contact in list(manager.get_contacts()):
            try:
                plan = repair_plan(contact, account)
            except Exception as e:
                activity.warning(f'[addressbook] Cannot judge contact {contact.name} ({contact.id}) for repair: {e!r}')
                continue
            if not plan:
                continue
            reasons = []
            with addressbook_origin.reason('repair'), addressbook.AddressbookManager.transaction():
                for uri, current, wanted, reason in plan.get('addresses', ()):
                    uri.uri = wanted
                    reasons.append(reason)
                    activity.info(f"[addressbook] Repair ({reason}): address of {contact.name} ({contact.id}) {current} -> {wanted}")
                if 'name' in plan:
                    was, wanted = plan['name']
                    contact.name = wanted
                    reasons.append('echoed-name')
                    activity.info(f"[addressbook] Repair (echoed-name): name {was!r} -> {wanted!r} ({contact.id}), the name was the address")
                for dropped, kept in plan.get('duplicates', ()):
                    # the default must survive: it points at one of these objects
                    default = contact.uris.default
                    if default is not None and getattr(default, 'id', None) == getattr(dropped, 'id', None):
                        contact.uris.default = kept
                    contact.uris.remove(dropped)
                    reasons.append('dup-uri')
                    activity.info(f"[addressbook] Repair (dup-uri): duplicate address {dropped.uri} dropped from {contact.name} ({contact.id}), kept id {getattr(kept, 'id', '?')}")
                contact.save()
            repaired += 1
        return repaired

    def merge_messages_duplicates(self):
        """Merge contacts of the Messages group that share a canonical address, keeping the
        lowest id, the rule every client uses so they all keep the same copy (blink.contact_repair.merge_plan)."""
        activity = ActivityLog()
        manager = addressbook.AddressbookManager()
        try:
            group = manager.get_group(MESSAGES_GROUP_ID)
        except KeyError:
            return 0
        account = AccountManager().default_account
        plan = merge_plan(list(group.contacts), lambda uri: canonical_uri(uri, account))
        deleted = 0
        with addressbook_origin.reason('merge'), addressbook.AddressbookManager.transaction():
            for cluster in plan:
                survivor, losers, donor = cluster['survivor'], cluster['losers'], cluster['name']
                changes = []
                if donor is not None:
                    changes.append(f'name {survivor.name!r} -> {donor.name!r} (from {donor.id})')
                    survivor.name = donor.name
                for uri, owner in cluster['uris']:
                    survivor.uris.add(addressbook.ContactURI(uri=uri.uri, type=uri.type))
                    changes.append(f'+uri {uri.uri} (from {owner.id})')
                activity.info(f"[addressbook] MERGE keep id={survivor.id} name={survivor.name!r} drop={','.join(loser.id for loser in losers)}" + (' -- ' + '; '.join(changes) if changes else ''))
                if changes:
                    try:
                        survivor.save()
                    except Exception as e:
                        activity.error(f'[addressbook]   cannot save {survivor.id}, leaving this cluster alone: {e!r}')
                        continue
                for loser in losers:
                    try:
                        group.contacts.remove(loser)
                    except Exception:
                        pass
                    try:
                        loser.delete()
                    except Exception as e:
                        activity.error(f'[addressbook]   cannot delete {loser.id}: {e!r}')
                    else:
                        deleted += 1
                        activity.info(f'[addressbook]   deleted id={loser.id} name={loser.name!r}')
            if deleted:
                group.save()
        return deleted

    def file_into_kind_groups(self):
        """Every phone number in Tel, every conference room in Conference. Only adds."""
        activity = ActivityLog()
        manager = addressbook.AddressbookManager()
        account = AccountManager().default_account
        contacts = list(manager.get_contacts())

        def addresses(contact):
            return [str(uri.uri) for uri in contact.uris]

        wanted = ((TEL, None, [contact for contact in contacts if any(pstn_e164(address, account) for address in addresses(contact))]),
                  (CONFERENCE, None, [contact for contact in contacts if any(is_conference_uri(address, account) for address in addresses(contact))]))
        filed = 0
        with addressbook_origin.reason('file-into-kind-group'):
            for identity, reserved_id, members in wanted:
                if not members:
                    continue
                group = CallsGroupFiler().ensure_group(identity, reserved_id)
                if group is None:
                    continue
                present = {member.id for member in group.contacts}
                missing = [contact for contact in members if contact.id not in present]
                if not missing:
                    continue
                with addressbook.AddressbookManager.transaction():
                    for contact in missing:
                        group.contacts.add(contact)
                    group.save()
                filed += len(missing)
                activity.info(f"[addressbook] Repair (file-into-kind-group): {len(missing)} contacts filed into '{group.name}': " + ', '.join(sorted(str(contact.name or contact.id) for contact in missing)))
        return filed


@implementer(IObserver)
class MessagesGroupFiler(object, metaclass=Singleton):
    """File the people this account has conversations with under the shared Messages group.

    As on macOS: everyone with a conversation in history gets an addressbook
    contact (an existing one, matched by canonical address, else a new one)
    and is filed under the group '_messages', created when missing at the top
    of the list. Membership is only added here and the group is saved only
    when it changed. Nothing is filed before the addressbook has loaded from
    XCAP (or straight away when no account uses XCAP), so a contact the
    server document already holds is found, not created twice. Bonjour
    neighbours are never filed: they live in the Bonjour group.
    """

    settle_delay = 15  # seconds after the first XCAP reload

    def __init__(self):
        self.ready = False
        self.pending = {}           # conversation key -> display name (or None)
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='XCAPManagerDidReloadData')
        notification_center.add_observer(self, name='BlinkMessageHistoryAllContactsDidSucceed')
        notification_center.add_observer(self, name='BlinkMessageHistoryMessageDidStore')
        notification_center.add_observer(self, name='BlinkJournalDidApply')
        # not sender=BlinkSettings(): the configuration is not started yet when the contact model is made
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        accounts = [account for account in AccountManager().get_accounts() if account is not BonjourAccount() and account.enabled]
        if not any(account.xcap.enabled for account in accounts):
            call_later(2, self._become_ready, 'no account uses XCAP')

    def _NH_XCAPManagerDidReloadData(self, notification):
        if not self.ready:
            call_later(self.settle_delay, self._become_ready, 'the addressbook has loaded')

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if isinstance(notification.sender, BlinkSettings) and 'interface.show_messages_group' in notification.data.modified and notification.sender.interface.show_messages_group:
            self._request_conversations()

    def _NH_BlinkMessageHistoryAllContactsDidSucceed(self, notification):
        for display_name, uri in notification.data.contacts:
            self.pending.setdefault(str(uri), display_name or None)
        self._flush()

    def _NH_BlinkMessageHistoryMessageDidStore(self, notification):
        self.pending.setdefault(str(notification.data.remote_uri), None)
        self._flush()

    def _NH_BlinkJournalDidApply(self, notification):
        if getattr(notification.data, 'first_sync', False):
            # the group has just been filled with everyone the journal mentioned: put it where the user looks
            NotificationCenter().post_notification('BlinkMessagesGroupShouldPromote', sender=self)

    def _become_ready(self, reason):
        if self.ready:
            return
        self.ready = True
        ActivityLog().info(f'[contacts] Filing conversations under the Messages group: {reason}')
        self._request_conversations()
        self._flush()

    def _request_conversations(self):
        if self.ready and BlinkSettings().interface.show_messages_group:
            from blink.history import HistoryManager
            HistoryManager().message_history.get_all_contacts()

    def _flush(self):
        if not self.ready or not self.pending or not BlinkSettings().interface.show_messages_group:
            return
        keys, self.pending = self.pending, {}
        try:
            self._file(keys)
        except Exception as e:
            ActivityLog().exception(f'[contacts] Filing conversations under the Messages group failed: {e!r}')

    @staticmethod
    def _find_by_canonical(key, contacts):
        wanted = canonical_uri(key)
        if not wanted:
            return None
        for contact in contacts:
            for uri in contact.uris:
                if canonical_uri(uri.uri) == wanted:
                    return contact
        return None

    def _contact_for(self, key, display_name, existing, created):
        contact, contact_uri = URIUtils.find_contact(key)
        if contact.type == 'addressbook':
            return contact.settings
        if contact.type in ('bonjour', 'google'):
            return None             # a neighbour, or a contact that cannot be put in an XCAP group
        if is_bonjour_address(key):
            ActivityLog().info(f'[contacts] Not filing the conversation with {key}: a Bonjour neighbour is not written to the addressbook')
            return None
        found = self._find_by_canonical(key, existing)
        if found is not None:
            return found
        new_contact = addressbook.Contact()
        # a conference room is named after itself: the room, not the whole address
        new_contact.name = key.partition('@')[0] if is_conference_uri(key) else (display_name or key)
        new_contact.uris = [addressbook.ContactURI(uri=key, type='SIP' if '@' in key else 'tel')]
        new_contact.preferred_media = 'messages'
        new_contact.save()
        publish_contact_for_groups(new_contact)    # several new members are added before the group saves
        existing.append(new_contact)
        created.append(new_contact)
        return new_contact

    def _file(self, keys):
        from blink import addressbook_origin
        activity = ActivityLog()
        manager = addressbook.AddressbookManager()
        existing = list(manager.get_contacts())
        added, created, skipped = [], [], []
        with addressbook_origin.reason('messages'), addressbook.AddressbookManager.transaction():
            try:
                group = manager.get_group(MESSAGES_GROUP_ID)
                new_group = False
            except KeyError:
                group = addressbook.Group(id=MESSAGES_GROUP_ID)
                group.name = MESSAGES_GROUP_NAME
                group.position = None       # a new group goes to the top of the list
                new_group = True
            members = set(group.contacts)
            for key, display_name in keys.items():
                if not is_fileable_key(key):
                    skipped.append(key)
                    continue
                contact = self._contact_for(key, display_name, existing, created)
                if contact is None:
                    skipped.append(key)
                    continue
                if contact not in members:
                    group.contacts.add(contact)
                    members.add(contact)
                    added.append(contact)
                    ContactTrash.leave_deleted(contact, 'a newer message brought the conversation back')
            if new_group or added:
                group.save()
        if new_group:
            activity.info(f'[contacts] Created the Messages group ({MESSAGES_GROUP_ID})')
        for contact in created:
            activity.info(f'[contacts] Created contact {contact.name} <{next(iter(contact.uris)).uri}> for a conversation')
        if added:
            activity.info(f'[contacts] Added {len(added)} contacts to the Messages group: ' + ', '.join(sorted(str(contact.name) for contact in added)))
        if skipped:
            log.debug(f'Not filed under the Messages group (not an address, or a Bonjour neighbour): {", ".join(sorted(skipped))}')


@implementer(IObserver)
class GroupKindStamper(object, metaclass=Singleton):
    """Write `kind` onto the software's groups that exist without one, as Blink for macOS does.

    Once per run, after the first XCAP reload (and a pause, so the addressbook
    manager has applied it): only a group with no kind is written, one
    attribute and nothing else (sipsimple sends only the modified keys, and
    membership only when 'contacts' is among them). A kind someone else wrote
    is never overwritten, and no group is created here.
    """

    settle_delay = 15  # seconds

    def __init__(self):
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            NotificationCenter().add_observer(self, name='XCAPManagerDidReloadData')

    def handle_notification(self, notification):
        if notification.name == 'XCAPManagerDidReloadData':
            NotificationCenter().remove_observer(self, name='XCAPManagerDidReloadData')
            call_in_gui_thread(call_later, self.settle_delay, self.stamp)

    def stamp(self):
        activity = ActivityLog()
        try:
            groups = list(addressbook.AddressbookManager().get_groups())
        except Exception as e:
            activity.warning(f'[addressbook] Cannot stamp group kinds, the addressbook is not readable: {e}')
            return
        if not groups:
            activity.info('[addressbook] No groups in the addressbook yet, not stamping group kinds')
            return
        for group in sorted(groups, key=lambda group: str(group.name or '').lower()):
            activity.info(f"[addressbook]   group '{group.name}' (id={group.id}, kind={group_kind(group) or '-'}, {len(group.contacts)} members)")
        written = 0
        with AddressbookNotifier().quiet(), addressbook_origin.reason('group-kind'), addressbook.AddressbookManager.transaction():
            for identity, group, action in stamp_plan(groups, STAMPED_KINDS):
                if action == 'missing':
                    activity.info(f"[addressbook] No '{identity.name}' group to stamp, nothing is created here")
                elif action == 'foreign':
                    activity.info(f"[addressbook] Group '{group.name}' (id={group.id}) carries kind={group_kind(group)} already, leaving it alone (wanted {identity.kind})")
                elif action == 'stamp':
                    try:
                        group.kind = identity.kind
                        group.save()
                    except Exception as e:
                        activity.error(f"[addressbook] Cannot stamp group '{group.name}' (id={group.id}): {e}")
                    else:
                        written += 1
                        activity.info(f"[addressbook] Stamped group '{group.name}' (id={group.id}) with kind={identity.kind}")
        activity.info(f'[addressbook] Group kinds checked: {written} stamped')


@implementer(IObserver)
class AddressbookReloadLog(object, metaclass=Singleton):
    """Say what arrived with every addressbook document, and which device changed what.

    Per reload: account, ETag, counts, every group with its members, and the
    changes since the last document of that account, attributed by their
    origin stamp (blink.addressbook_origin) as on macOS. The last document's
    snapshot is kept in addressbook_origins/<account>.json, so a change made
    while Blink was not running is still attributed on the next start.
    Read-only with respect to the addressbook.
    """

    change_cap = 50
    member_cap = 30

    def __init__(self):
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            NotificationCenter().add_observer(self, name='XCAPManagerDidReloadData')

    def handle_notification(self, notification):
        if notification.name == 'XCAPManagerDidReloadData':
            try:
                self._log_reload(notification.sender, notification.data)
            except Exception as e:
                ActivityLog().warning(f'[addressbook] Cannot log the addressbook reload: {e!r}')

    def _log_reload(self, xcap_manager, data):
        activity = ActivityLog()
        account = getattr(xcap_manager, 'account', None)
        account_id = getattr(account, 'id', '?')
        document = getattr(data, 'addressbook', None)
        if document is None:
            return
        etag = getattr(getattr(xcap_manager, 'resource_lists', None), 'etag', None)
        contacts = list(document.contacts or ())
        groups = list(document.groups or ())
        activity.info(f'[addressbook] Addressbook of {account_id} reloaded: ETag {etag or "-"}, {len(contacts)} contacts, {len(groups)} groups, {len(document.policies or ())} policies')
        for group in sorted(groups, key=lambda group: str(group.name or '').lower()):
            members = [getattr(member, 'name', None) or getattr(member, 'id', str(member)) for member in (group.contacts or ())]
            kind = (group.attributes or {}).get('kind') or '-'
            shown = ', '.join(str(name) for name in members[:self.member_cap]) + (f', ... {len(members) - self.member_cap} more' if len(members) > self.member_cap else '')
            activity.info(f"[addressbook]   group '{group.name}' (id={group.id}, kind={kind}): {len(members)} members{': ' + shown if members else ''}")

        path = ApplicationData.get(f'addressbook_origins/{account_id}.json')
        previous = addressbook_origin.load_snapshot(path)
        changes, snapshot = addressbook_origin.diff_document(document, previous, this_device_id())
        addressbook_origin.save_snapshot(path, snapshot)
        if changes is None:
            activity.info(f"[addressbook] Addressbook of {account_id}: baseline of {len(snapshot['contacts'])} contacts and {len(snapshot['groups'])} groups, changes are attributed from the next document on")
        elif changes:
            activity.info(f'[addressbook] Addressbook of {account_id}: {len(changes)} changes since the last document')
            for line in addressbook_origin.format_changes(changes, cap=self.change_cap):
                activity.info(f'[addressbook]   {line}')


class _AddressbookNotifyState(object):
    """Per account: a manager runs per account, and one account's burst says nothing about another's."""

    def __init__(self):
        self.throttle = addressbook_notify.SendThrottle()
        self.scheduler = addressbook_notify.FetchScheduler()
        self.send_armed = False
        self.fetch_armed = False
        self.fuse_logged = False
        self.reasons = []           # (op, kind, id, member), when, description -- what the pending tick is about
        self.contacts = set()       # what the ticks we are about to fetch for said had changed
        self.groups = set()
        self.full = False
        self.awaiting_reload = False
        self.retried = False


@implementer(IObserver)
class AddressbookNotifier(object, metaclass=Singleton):
    """application/sylk-addressbook-update, as Blink for macOS sends and answers it.

    After WE write to the server addressbook, one message to our own account
    (X-Sylk-Skip-Journal, no CPIM) tells every other device to refetch it; when
    another device tells us, we refetch the resource-lists document after a
    jitter. The tick carries ids only, XCAP stays the source of truth. The rules
    (debounce, floor, fuse, backoff, freshness) are in blink.addressbook_notify.

    Nothing is announced for a document we are copying from the server
    (data.remote, set by sipsimple when the write is made) nor for writes made
    inside quiet(): this device healing what it was handed, which every other
    device does on its own from the same document. Announcing either is how two
    devices end up waking each other for ever.

    The XCAP manager posts its notifications on the thread that saves (file-io),
    so the decision to note a write is taken there, at once; the timers run on
    the GUI thread.
    """

    xcap_changes = {'XCAPManagerDidAddContact':     ('contact', 'add'),
                    'XCAPManagerDidUpdateContact':  ('contact', 'update'),
                    'XCAPManagerDidRemoveContact':  ('contact', 'remove'),
                    'XCAPManagerDidAddGroup':       ('group', 'add'),
                    'XCAPManagerDidUpdateGroup':    ('group', 'update'),
                    'XCAPManagerDidRemoveGroup':    ('group', 'remove'),
                    # spelled XCAPManage... in sipsimple: the corrected spelling observes nothing
                    'XCAPManageDidAddGroupMember':    ('group', 'add-member'),
                    'XCAPManageDidRemoveGroupMember': ('group', 'remove-member')}

    reasons_cap = 100
    fetch_check_delay = 20.0        # seconds before asking whether a requested fetch delivered a document

    def __init__(self):
        self._started = False
        self._lock = threading.RLock()
        self._quiet = 0
        self._states = {}

    def start(self):
        if self._started:
            return
        self._started = True
        notification_center = NotificationCenter()
        for name in self.xcap_changes:
            notification_center.add_observer(self, name=name)
        notification_center.add_observer(self, name='XCAPManagerDidChangeState')
        notification_center.add_observer(self, name='XCAPManagerDidReloadData')

    @contextmanager
    def quiet(self):
        """Announce nothing written inside, for every account (reentrant).

        The saves made inside run later on the file-io thread, so the quiet
        stretch is closed there too, after them.
        """
        with self._lock:
            self._quiet += 1
        try:
            yield
        finally:
            self._end_quiet()

    @run_in_thread('file-io')
    def _end_quiet(self):
        with self._lock:
            if self._quiet > 0:
                self._quiet -= 1

    def _state(self, account):
        with self._lock:
            key = str(account.id)
            state = self._states.get(key)
            if state is None:
                state = self._states[key] = _AddressbookNotifyState()
            return state

    @staticmethod
    def _account(manager):
        account = getattr(manager, 'account', None)
        return account if isinstance(account, Account) else None

    @staticmethod
    def _settled(manager):
        """Is anything of ours still on its way to the server? Asked both ways: we must not
        announce a change the server has not taken yet, nor apply a fetched document on top
        of our own unflushed journal."""
        try:
            return manager.state == 'insync' and not manager.journal
        except (AttributeError, ReferenceError):
            return True

    @staticmethod
    def _this_device():
        return str(SIPSimpleSettings().instance_id or '')

    @staticmethod
    def _bare(instance_id):
        text = str(instance_id or '').strip()
        return text[9:] if text.startswith('urn:uuid:') else text

    def handle_notification(self, notification):
        change = self.xcap_changes.get(notification.name)
        if change is not None:
            self._note_change(notification.sender, notification.data, *change)
        else:
            handler = getattr(self, '_NH_%s' % notification.name, None)
            if handler is not None:
                call_in_gui_thread(handler, notification)

    # sending

    def _note_change(self, manager, data, kind, op):
        # on the thread that saved
        if getattr(data, 'remote', False):
            return          # copying a fetched document, not a change of ours
        account = self._account(manager)
        if account is None:
            return
        subject = getattr(data, kind, None)
        id = getattr(subject, 'id', None)
        with self._lock:
            if self._quiet:
                return      # healing, see quiet()
            state = self._state(account)
            if not state.throttle.note(kind, id):
                return
            try:
                self._note_reason(state, kind, id, op, data)
            except Exception as e:
                log.debug(f'[addressbook] Cannot describe an addressbook change: {e!r}')
        call_in_gui_thread(self._arm, account)

    @staticmethod
    def _describe_contact(contact):
        if contact is None:
            return '?'
        uris = []
        try:
            for item in (getattr(contact, 'uris', None) or ()):
                uri = getattr(item, 'uri', None) or item
                if uri:
                    uris.append(str(uri))
        except Exception:
            pass
        shown = ', '.join(uris[:3]) + (f' +{len(uris) - 3}' if len(uris) > 3 else '')
        return f"{getattr(contact, 'id', None)} {getattr(contact, 'name', None)!r}" + (f' <{shown}>' if uris else '')

    @staticmethod
    def _describe_group(group):
        if group is None:
            return '?'
        try:
            members = len(getattr(group, 'contacts', None) or ())
        except Exception:
            members = '?'
        return f"{getattr(group, 'id', None)} {getattr(group, 'name', None)!r} ({members} members)"

    def _note_reason(self, state, kind, id, op, data):
        member = None
        if kind == 'group':
            subject = self._describe_group(getattr(data, 'group', None))
            if op in ('add-member', 'remove-member'):
                contact = getattr(data, 'contact', None)
                member = getattr(contact, 'id', None)
                subject = f'{subject}, member {self._describe_contact(contact)}'
        else:
            subject = self._describe_contact(getattr(data, 'contact', None))
        key = (op, kind, str(id), member)
        if len(state.reasons) >= self.reasons_cap or any(entry[0] == key for entry in state.reasons):
            return
        state.reasons.append((key, time.time(), subject))

    def _arm(self, account):
        state = self._state(account)
        with self._lock:
            if state.send_armed or not state.throttle.pending:
                return
            delay = state.throttle.delay()
            if delay is None:
                return
            state.send_armed = True
        # nothing is ever cancelled: an early wakeup finds the burst not due and arms again
        call_later(max(delay, 0.1), self._flush, account)

    def _flush(self, account):
        activity = ActivityLog()
        manager = getattr(account, 'xcap_manager', None)
        state = self._state(account)
        retry = None
        with self._lock:
            state.send_armed = False
            tick = state.throttle.take(self._settled(manager))
            if tick is None:
                # not due, not settled, or the fuse is blown: the ids are kept, only late
                delay = state.throttle.delay()
                if delay is not None:
                    state.send_armed = True
                    fuse_blown = state.throttle.fuse_blown
                    retry = max(delay, 30.0 if fuse_blown else 1.0)
                    if fuse_blown and not state.fuse_logged:
                        state.fuse_logged = True
                        activity.warning(f'[addressbook] Too many addressbook changes announced for {account.id} in {addressbook_notify.NOTIFY_FUSE_WINDOW:.0f}s, holding the next one back: something is writing in a loop')
            else:
                state.fuse_logged = False
                reasons, state.reasons = state.reasons, []
        if tick is None:
            if retry is not None:
                call_later(retry, self._flush, account)
            return
        contact_ids, group_ids, truncated = tick
        content = addressbook_notify.build_tick(self._this_device(), contact_ids, group_ids, truncated)
        activity.info(f"[addressbook] Addressbook of {account.id} changed, telling the other devices: {len(contact_ids)} contacts, {len(group_ids)} groups{' (truncated)' if truncated else ''}")
        for (op, kind, id, member), when, subject in reasons:
            activity.info(f"[addressbook]   {op} {kind} {subject} at {time.strftime('%H:%M:%S', time.localtime(when))}")
        if len(reasons) >= self.reasons_cap:
            activity.info('[addressbook]   ... more not listed')
        MessageManager().send_addressbook_update(account, content)
        self._arm(account)

    # receiving

    @run_in_gui_thread
    def handle_tick(self, account, content, sender_uri=None):
        """A tick from one of our own devices: arm a jittered refetch."""
        activity = ActivityLog()
        if sender_uri is not None:
            # self only: nobody else gets to make us re-read our addressbook
            user, host = (part.decode(errors='replace') if isinstance(part, bytes) else str(part) for part in (sender_uri.user, sender_uri.host))
            sender = f'{user}@{host}'
            if sender.lower() != str(account.id).lower():
                activity.warning(f'[addressbook] Ignoring an addressbook update for {account.id} sent by {sender}')
                return
        tick = addressbook_notify.parse_tick(content)
        if tick is None:
            activity.warning(f'[addressbook] Ignoring an unreadable addressbook update for {account.id}')
            return
        if tick['origin'] and self._bare(tick['origin']) == self._bare(self._this_device()):
            log.debug(f'Ignoring our own addressbook update for {account.id}')
            return
        # freshness is judged here, on arrival, never again when the jitter expires
        if not addressbook_notify.is_fresh(tick['timestamp']):
            activity.info(f'[addressbook] Ignoring a stale addressbook update for {account.id} ({int(time.time()) - tick["timestamp"]}s old)')
            return
        if getattr(account, 'xcap_manager', None) is None or not account.xcap.enabled:
            activity.info(f'[addressbook] Ignoring an addressbook update for {account.id}: the account does not use XCAP')
            return
        state = self._state(account)
        if tick['truncated'] or tick['contact_ids'] is None:
            state.full = True
        state.contacts.update(tick['contact_ids'] or ())
        state.groups.update(tick['group_ids'] or ())
        delay = state.scheduler.schedule()
        if delay is None:
            activity.info(f'[addressbook] Addressbook update for {account.id} merged into the fetch already coming')
            return
        activity.info(f'[addressbook] The addressbook of {account.id} changed on another device ({self._bare(tick["origin"]) or "?"}), fetching it in {round(delay)}s')
        state.fetch_armed = True
        call_later(delay, self._fire_fetch, account)

    def _fire_fetch(self, account):
        activity = ActivityLog()
        state = self._state(account)
        state.fetch_armed = False
        manager = getattr(account, 'xcap_manager', None)
        if manager is None:
            return
        fetch, retry_in, backed_off = state.scheduler.fire(self._settled(manager))
        if not fetch:
            if backed_off:
                activity.warning(f'[addressbook] Too many addressbook fetches for {account.id}, backing off')
            if retry_in is not None:
                state.fetch_armed = True
                call_later(retry_in, self._fire_fetch, account)
            return
        if state.full:
            activity.info(f'[addressbook] Fetching the addressbook of {account.id} (the other device could not say what changed)')
        else:
            activity.info(f'[addressbook] Fetching the addressbook of {account.id} ({len(state.contacts)} contacts, {len(state.groups)} groups changed)')
        # the ids scope the log, never the fetch: XCAP fetches the whole resource-lists document
        state.contacts.clear()
        state.groups.clear()
        state.full = False
        state.awaiting_reload = True
        self._send_fetch_command(manager)
        call_later(self.fetch_check_delay, self._report_fetch_outcome, account)

    @run_in_twisted_thread
    def _send_fetch_command(self, manager):
        # Without the cached etag: a 304 would take the manager straight back to insync
        # without reloading, and another device saying the document changed is better
        # information than an etag the server may not have bumped. Safe only here: we are
        # settled, so no update is waiting to PUT with If-Match.
        try:
            document = manager.resource_lists
            if document.etag is not None:
                log.debug(f'Fetching the addressbook without the cached etag {document.etag}: another device says it changed')
                document.etag = None
        except (AttributeError, ReferenceError):
            pass
        try:
            manager.command_channel.send(Command('fetch', documents=set(addressbook_notify.FETCH_DOCUMENTS)))
        except Exception as e:
            ActivityLog().error(f'[addressbook] Cannot ask for an addressbook fetch: {e!r}')

    def _report_fetch_outcome(self, account):
        """Did the fetch deliver a document? Nothing below us retries a failed one, and the
        tick was the only prompt. One retry, then say so."""
        activity = ActivityLog()
        state = self._state(account)
        if not state.awaiting_reload:
            state.retried = False
            return
        manager = getattr(account, 'xcap_manager', None)
        if manager is not None and not state.retried:
            state.retried = True
            activity.info(f'[addressbook] The addressbook of {account.id} did not arrive, asking once more')
            self._send_fetch_command(manager)
            call_later(self.fetch_check_delay, self._report_fetch_outcome, account)
            return
        state.awaiting_reload = False
        state.retried = False
        activity.warning(f'[addressbook] The addressbook of {account.id} was fetched twice because another device said it changed, and neither attempt returned a usable document: check the XCAP log for a failed GET or a parse error')

    def _NH_XCAPManagerDidChangeState(self, notification):
        # insync is the moment our journal reached the server: a held tick may go now,
        # and a fetch held for a fetch in flight may follow
        if notification.data.state != 'insync':
            return
        account = self._account(notification.sender)
        if account is None:
            return
        state = self._state(account)
        following = state.scheduler.done()
        if following is not None and not state.fetch_armed:
            state.fetch_armed = True
            call_later(following, self._fire_fetch, account)
        self._arm(account)

    def _NH_XCAPManagerDidReloadData(self, notification):
        account = self._account(notification.sender)
        if account is None:
            return
        state = self._state(account)
        if state.awaiting_reload:
            state.awaiting_reload = False
            state.retried = False
            ActivityLog().info(f'[addressbook] The addressbook of {account.id} reloaded after another device changed it')


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
        notification_center.add_observer(self, name='BonjourNeighboursManagerDidAddContact')
        notification_center.add_observer(self, name='BonjourNeighboursManagerDidRemoveContact')
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

    # The virtual Messages group is retired: the shared addressbook group '_messages'
    # (MessagesGroupFiler) lists conversations now, as on macOS. This manager stays
    # inactive and ignores what history reports.
    def _NH_CFGSettingsObjectDidChange(self, notification):
        pass

    def _NH_SIPApplicationDidStart(self, notification):
        pass

    def _NH_BlinkMessageHistoryAllContactsDidSucceed(self, notification):
        if not self.active:
            return
        contacts = notification.data.contacts
        found_contacts = []
        seen_ids = set()
        for (display_name, uri) in contacts:
            contact, contact_uri = URIUtils.find_contact(uri)
            if contact_uri is None:
                # a contact with no address left (a neighbour whose last announcement went away)
                ActivityLog().warning(f'[contacts] No address for the conversation with {uri}, not listed in the Messages group')
                continue
            # Several conversation keys can be one party: a neighbour's old and new keys,
            # whether it is online (one contact) or away (a placeholder per key).
            identity = neighbour_instance_id(contact, str(contact_uri.uri)) or (contact.settings.id if contact.type != 'dummy' else None)
            if identity is not None:
                if identity in seen_ids:
                    continue
                seen_ids.add(identity)
            if contact.type in ['dummy']:
                display_name = self._fallback_name(uri, contact_uri, display_name)
                contact = Contact(MessageContact(display_name, [contact_uri], uri), None)
            found_contacts.append(contact)

            if display_name and contact.settings.name == uri and display_name != uri:
                contact.settings.name = display_name

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
        self._log_members(found_contacts, removed=len(deleted_contact_ids))

    @staticmethod
    def _log_members(contacts, removed=0):
        activity = ActivityLog()
        activity.info(f'[contacts] Messages group has {len(contacts)} contacts' + (f', {removed} removed' if removed else ''))
        for contact in sorted(contacts, key=lambda item: str(item.name or '').lower()):
            try:
                kind = contact.type
            except Exception:
                kind = 'unknown'
            kind = 'history' if isinstance(contact.settings, MessageContact) else kind
            uri = contact.uri.uri if contact.uri is not None else ''
            key = neighbour_instance_id(contact, str(uri)) or uri
            activity.info(f'[contacts]   {contact.name} <{key}> ({kind})')

    @staticmethod
    def _fallback_name(uri, contact_uri, display_name=None):
        instance_id = placeholder_instance_id(contact_uri.uri)
        if instance_id:
            # a Bonjour neighbour who is not on the network right now
            return remembered_bonjour_name(instance_id) or display_name or translate('contact_list', 'Bonjour neighbour')
        return display_name or uri

    def _NH_BonjourNeighboursManagerDidAddContact(self, notification):
        # a neighbour coming or going turns its conversation's row into the neighbour, or back
        if self.active:
            from blink.history import HistoryManager
            HistoryManager().message_history.get_all_contacts()

    _NH_BonjourNeighboursManagerDidRemoveContact = _NH_BonjourNeighboursManagerDidAddContact

    def _NH_BlinkMessageHistoryMessageDidStore(self, notification):
        if not self.active:
            return

        uri = notification.data.remote_uri
        contact, contact_uri = URIUtils.find_contact(uri)
        if contact.type in ['dummy']:
            display_name = self._fallback_name(uri, contact_uri)
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
        # pass a snapshot: the model handles this later in the GUI thread, and a
        # contact added in the meantime would come both from the live list and
        # from its own VirtualGroupDidAddContact
        notification.center.post_notification('VirtualGroupWasActivated', sender=self, data=NotificationData(contacts=list(self.contacts)))

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
        """The one address a neighbour is reached at, chosen as on macOS.

        A neighbour announces itself once per transport. The transport set
        for the Bonjour account wins; the others rank TLS, TCP, UDP so the
        choice between them is stable. A transport this machine does not use
        is taken only when the neighbour announced nothing else.
        """
        if not self._uri_map:
            return None
        try:
            usable = set(SIPSimpleSettings().sip.transport_list)
        except Exception:
            usable = {'tls', 'tcp', 'udp'}
        candidates = [uri for uri in self if str(uri.uri.transport).lower() in usable] or list(self)
        return max(candidates, key=lambda item: bonjour_transport_rank(item.uri.transport))


def bonjour_preferred_transport():
    """The transport set for the Bonjour account, TCP when unset.

    TCP rather than TLS by default: on a link-local network TLS has no name
    to verify and no CA that knows a neighbour's self-signed certificate, so
    it costs a handshake, proves nothing, and fails most often between
    different builds.
    """
    try:
        transport = str(BonjourAccount().sip.transport or '').lower()
    except Exception:
        transport = ''
    return transport if transport in ('tcp', 'tls', 'udp') else 'tcp'


def bonjour_transport_rank(transport):
    """How much an announcement over this transport is wanted; higher wins."""
    transport = str(transport or '').lower()
    if transport == bonjour_preferred_transport():
        return 3
    return {'tls': 2, 'tcp': 1, 'udp': 0}.get(transport, -1)


def bonjour_info(neighbour):
    """The second line of a Bonjour neighbour's tile.

    Their presence note, else their presence state, as on macOS. Never the
    address: it is a transport detail that changes with the network, and the
    computer is already in the name, "Name (computer)".
    """
    presence = neighbour.presence
    if presence.note:
        return presence.note
    state = str(presence.state or '').strip()
    if state:
        return state.title()
    return translate('contact_list', 'On the local network')


# {instance id: {'name': ..., 'host': ...}} for every Bonjour neighbour ever
# met, so a conversation still has a name while its neighbour is away. Local
# on purpose, as on macOS: there is no server behind a link-local network.
_bonjour_names = None


def _bonjour_names_file():
    return ApplicationData.get('bonjour_neighbours.json')


def _load_bonjour_names():
    global _bonjour_names
    if _bonjour_names is None:
        try:
            with open(_bonjour_names_file(), encoding='utf-8') as names_file:
                data = json.load(names_file)
            _bonjour_names = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _bonjour_names = {}
    return _bonjour_names


def remember_bonjour_neighbour(instance_id, name, host):
    instance_id = bare_instance_id(instance_id)
    if not instance_id or not name:
        return
    names = _load_bonjour_names()
    entry = {'name': str(name), 'host': str(host or '')}
    if names.get(instance_id) == entry:
        return
    names[instance_id] = entry
    try:
        makedirs(ApplicationData.directory)
        with open(_bonjour_names_file(), 'w', encoding='utf-8') as names_file:
            json.dump(names, names_file, indent=1, sort_keys=True)
    except OSError as e:
        log.warning('Cannot save Bonjour neighbour names: %s' % e)


def remembered_bonjour_name(instance_id):
    """'Name (computer)' for a neighbour met before, or None."""
    entry = _load_bonjour_names().get(bare_instance_id(instance_id))
    if not isinstance(entry, dict) or not entry.get('name'):
        return None
    return '%s (%s)' % (entry['name'], entry['host']) if entry.get('host') else entry['name']


def neighbour_instance_id(contact, uri=None):
    """The instance id of a Bonjour neighbour, or of a placeholder standing in for one; else None.

    What a neighbour's address is shown as: the transport address changes with
    the network, the instance id is who they are.
    """
    if getattr(contact, 'type', None) == 'bonjour':
        instance_id = bare_instance_id(contact.settings.id)
        if is_instance_id(instance_id):
            return instance_id
        return None
    return placeholder_instance_id(uri) if uri else None


@lru_cache(maxsize=4096)
def _conversation_key(uri, account):
    from blink.history import conversation_key
    return conversation_key(uri, account)


def contact_conversation_keys(contact):
    """The keys history files a contact's conversations under: every address, as
    the default account's dial rules spell it, and a neighbour's instance id."""
    keys = set()
    account = AccountManager().default_account
    try:
        uris = list(contact.uris)
    except (AttributeError, TypeError):
        uris = []
    for uri in uris:
        address = str(getattr(uri, 'uri', uri) or '')
        if address:
            keys.add(_conversation_key(address, account))
    default = contact.uri
    instance_id = neighbour_instance_id(contact, str(default.uri) if default is not None else None)
    if instance_id:
        keys.add(instance_id)
    keys.discard('')
    return keys



def conversation_uri(contact):
    """(uri, conversation key) a contact's conversation opens on: the address the last
    message was with, else the default one; a Bonjour neighbour by its instance id.
    (None, None) for a contact without an address."""
    default = contact.uri
    if default is None:
        return None, None
    instance_id = neighbour_instance_id(contact, str(default.uri))
    if instance_id:
        return default, instance_id
    from blink.history import ConversationPreviews
    previews = ConversationPreviews()
    account = AccountManager().default_account
    best, best_time = None, None
    try:
        uris = list(contact.uris)
    except (AttributeError, TypeError):
        uris = []
    for uri in uris:
        key = _conversation_key(str(uri.uri), account)
        when = previews.message_times.get(key)
        if when is not None and (best_time is None or when > best_time):
            best, best_time = (uri, key), when
    return best or (default, _conversation_key(str(default.uri), account))


def row_time_kind(group_settings):
    """What the time on the right of a contact row is in this group: 'message' in Messages,
    'call' in Calls and Tel, None elsewhere."""
    if is_messages_group(group_settings):
        return 'message'
    if group_settings is None or isinstance(group_settings, VirtualGroup):
        return None
    try:
        if is_group(group_settings, CALLS) or is_group(group_settings, TEL):
            return 'call'
    except Exception:
        pass
    return None


def format_row_time(when, now=None):
    """The time on the right of a contact row: HH:MM today, Yesterday, the weekday within
    the last week, day and month this year, else day/month/year. `when` is an aware datetime."""
    if when is None:
        return ''
    when = when.astimezone()
    now = (now or datetime.now().astimezone()).astimezone(when.tzinfo)
    days = (now.date() - when.date()).days
    if days <= 0:
        return when.strftime('%H:%M')
    if days == 1:
        return translate('contact_list', 'Yesterday')
    if days < 7:
        return QLocale().dayName(when.isoweekday(), QLocale.FormatType.LongFormat)
    if when.year == now.year:
        return f"{when.day} {QLocale().monthName(when.month, QLocale.FormatType.ShortFormat)}"
    return when.strftime('%d/%m/%y')


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
            remember_bonjour_neighbour(contact_id, record.name, record.host)
            self.contacts.add(contact)
            self._log_address(contact, None)
            notification.center.post_notification('BonjourNeighboursManagerDidAddContact', sender=self, data=NotificationData(contact=contact))
        else:
            previous = contact.uris.default
            contact.uris.add(contact_uri)
            self._log_address(contact, previous)
            notification.center.post_notification('BonjourNeighboursManagerDidUpdateContact', sender=self, data=NotificationData(contact=contact))

    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        contact_id = notification.data.record.id or notification.data.neighbour
        contact = self.contacts[contact_id]
        previous = contact.uris.default
        contact.uris.pop(notification.data.neighbour, None)
        if contact.uris:
            self._log_address(contact, previous)
        if not contact.uris:
            self.contacts.remove(contact)
            notification.center.post_notification('BonjourNeighboursManagerDidRemoveContact', sender=self, data=NotificationData(contact=contact))
        else:
            notification.center.post_notification('BonjourNeighboursManagerDidUpdateContact', sender=self, data=NotificationData(contact=contact))

    @staticmethod
    def _log_address(contact, previous):
        current = contact.uris.default
        if current is None or current is previous:
            return
        ActivityLog().info('[bonjour] Neighbour %s (%s) is reached at %s' % (contact.name, bare_instance_id(contact.id), current.uri.__uri__))

    def _NH_BonjourAccountDidUpdateNeighbour(self, notification):
        neighbour, record = notification.data.neighbour, notification.data.record
        contact = self.contacts[record.id or neighbour]
        contact_uri = contact.uris[neighbour]
        contact.name = record.name
        contact.host = record.host
        remember_bonjour_neighbour(contact.id, record.name, record.host)
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
        session = AuthorizedSession(self.credentials)
        try:
            if icon.url is not None:
                response = session.get(icon.url + '?size={}'.format(IconManager.max_size))
                content = response.content
                response_status = response.status_code
            else:
                response = content = None
        except (RequestException, socket.error) as e:
            log.warning('could not retrieve icon for {owner}: {exception!s}'.format(owner=owner, exception=e))
        else:
            if response is None:
                icon_manager = IconManager()
                icon_manager.store_data(self.contact.id, None)
                icon.downloaded_url = None
            elif response_status == 200 and response.headers.get('content-type','').startswith('image/'):
                icon_manager = IconManager()
                try:
                    icon_manager.store_data(self.contact.id, content)
                except Exception as e:
                    log.error('could not store icon for {owner}: {exception!s}'.format(owner=owner, exception=e))
                else:
                    icon.downloaded_url = icon.url
            elif response_status in (403, 404) and icon.alternate_url:  # private or unavailable photo. use old GData protocol if alternate_url is available.
                try:
                    response = session.get(icon.alternate_url, headers={'GData-Version': '3.0'})
                    content = response.content
                    response_status = response.status_code
                except (RequestException, socket.error) as e:
                    log.warning('could not retrieve icon for {owner}: {exception!s}'.format(owner=owner, exception=e))
                else:
                    if response_status == 200 and response.headers.get('content-type', '').startswith('image/'):
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


class GoogleAuthorizationStorage:
    def __init__(self, filename):
        self.filename = filename
        self._directory = os.path.dirname(filename)

    def get(self):
        if not os.path.exists(self.filename):
            return None
        return Credentials.from_authorized_user_file(self.filename)

    def put(self, credentials):
        os.makedirs(self._directory, exist_ok=True)
        with open(self.filename, 'w') as f:
            f.write(credentials.to_json())


class GoogleAuthorization(object):
    client_id = '28246556873-20215d5a5ttd0l3sa7cchsm7hklh2d3c.apps.googleusercontent.com'
    client_secret = '3L8FDV5LELGmMIwr3NhfaZsq'
    redirect_uri = 'http://127.0.0.1'
    scope = ('openid '
             'https://www.googleapis.com/auth/userinfo.profile '
             'https://www.googleapis.com/auth/contacts.readonly')

    def __init__(self):
        settings = SIPSimpleSettings()
        self.storage = GoogleAuthorizationStorage(ApplicationData.get('google/credentials'))
        self.flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=self.scope.split(),
            redirect_uri=self.redirect_uri,
        )
        self.flow.login_hint = settings.google_contacts.username
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
        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                self.storage.put(credentials)
            except AccessTokenRefreshError:
                credentials = None

        if credentials is None or not credentials.valid:
            self._open_authorization()
            return

        notification_center = NotificationCenter()
        notification_center.post_notification('GoogleAuthorizationWasAccepted', sender=self, data=NotificationData(credentials=credentials, email=self.email))

    def _open_authorization(self):
        auth_url, _ = self.flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            login_hint=self.email,
            prompt='consent',
        )
        self.view.open(auth_url)

    @run_in_thread('network-io')
    def _SH_AuthorizationAccepted(self, code, email):
        self.email = email
        self.flow.fetch_token(code=code)
        credentials = self.flow.credentials
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
        except (RequestException, socket.error) as e:
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

    size_hint = FontScaledSize(200, 24, lines=1, padding=8)

    virtual = property(lambda self: isinstance(self.settings, VirtualGroup))

    movable = True
    editable = True
    # the Messages and Deleted groups come back by themselves: not the user's to delete
    deletable = property(lambda self: not self.virtual and not is_messages_group(self.settings) and getattr(self.settings, 'id', None) != DELETED_GROUP_ID)

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
    theme_order = 0     # the default avatar is drawn for the theme (themed_icon)

    def __init__(self, filename):
        self.filename = filename
        self.icon = None
        follow_theme(self)

    def apply_theme(self):
        self.icon = None

    def __get__(self, instance, owner):
        if self.icon is None:
            self.icon = themed_icon(self.filename)
            self.icon.filename = self.filename
        return self.icon

    def __set__(self, instance, value):
        raise AttributeError("attribute cannot be set")

    def __delete__(self, instance):
        raise AttributeError("attribute cannot be deleted")


@implementer(IObserver)
class Contact(object):

    size_hint = FontScaledSize(220, 42, lines=2, padding=6)

    native = property(lambda self: self.type == 'addressbook')

    movable = property(lambda self: self.type == 'addressbook' and not is_managed_group(self.group.settings))
    editable = property(lambda self: self.type == 'addressbook')
    # Delete moves an addressbook contact to Deleted from any group, as on macOS (ContactTrash); in Deleted it has its own actions
    deletable = property(lambda self: self.type == 'addressbook' and getattr(self.group.settings, 'id', None) != DELETED_GROUP_ID)

    default_user_icon = ContactIconDescriptor(Resources.get('icons/default-avatar.png'))

    stylish_icons = True

    theme_order = 1

    def __init__(self, contact, group):
        self.settings = contact
        self.group = group
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=contact)
        follow_theme(self)

    def apply_theme(self):
        self.__dict__.pop('icon', None)
        self.__dict__.pop('pixmap', None)

    @property
    def sort_key(self):
        """Where the contact goes in its group: in Messages newest message first, in Calls and Tel
        newest call first, the ones without either after them; by name otherwise and on a tie."""
        name = locale.strxfrm(self.name or '')
        if row_time_kind(getattr(self.group, 'settings', None)) is None:
            return (0, 0, name)
        when = self.row_time
        return (0, -when.timestamp(), name) if when is not None else (1, 0, name)

    def __gt__(self, other):
        if isinstance(other, Contact):
            return self.sort_key > other.sort_key
        return NotImplemented

    def __ge__(self, other):
        if isinstance(other, Contact):
            return self.sort_key >= other.sort_key
        return NotImplemented

    def __lt__(self, other):
        if isinstance(other, Contact):
            return self.sort_key < other.sort_key
        return NotImplemented

    def __le__(self, other):
        if isinstance(other, Contact):
            return self.sort_key <= other.sort_key
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
    def display_name(self):
        """The first line in the contact list: the name, then the organization in brackets."""
        name = self.name or ''
        organization = (getattr(self.settings, 'organization', None) or '').strip() if self.type == 'addressbook' else ''
        if not organization or organization.lower() == name.lower():
            return name
        return f'{name} ({organization})' if name else organization

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
            # In the Messages group a row is a conversation, and a Bonjour
            # conversation is the neighbour's instance id.
            in_messages_group = is_messages_group(getattr(self.group, 'settings', None))
            if in_messages_group:
                # they are typing, else the last typed message of the conversation, when there is one
                from blink.history import ConversationPreviews, ConversationTyping
                keys = self.conversation_keys
                if ConversationTyping().is_typing(keys):
                    return translate('contact_list', '✎ is typing…')
                preview = ConversationPreviews().preview(keys)
                if preview:
                    return preview
            instance_id = neighbour_instance_id(self, str(self.uri.uri) if self.uri is not None else None)
            if instance_id and (in_messages_group or self.type != 'bonjour'):
                return instance_id
            if self.type == 'bonjour':
                return bonjour_info(self.settings)
            return self.note or self.uri.uri
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
    def conversation_keys(self):
        return contact_conversation_keys(self)

    @property
    def row_time(self):
        """When the last message (Messages group) or call (Calls, Tel) with this contact was, or None."""
        kind = row_time_kind(getattr(self.group, 'settings', None))
        if kind is None:
            return None
        from blink.history import ConversationPreviews
        previews = ConversationPreviews()
        keys = self.conversation_keys
        return previews.message_time(keys) if kind == 'message' else previews.call_time(keys)

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

    size_hint = FontScaledSize(200, 36, lines=2, padding=4)

    native = property(lambda self: self.type == 'addressbook')

    editable = property(lambda self: self.type == 'addressbook')
    deletable = property(lambda self: self.type == 'addressbook')

    default_user_icon = ContactIconDescriptor(Resources.get('icons/default-avatar.png'))

    stylish_icons = True

    theme_order = 1

    def __init__(self, contact):
        self.settings = contact
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=contact)
        follow_theme(self)

    def apply_theme(self):
        self.__dict__.pop('icon', None)
        self.__dict__.pop('pixmap', None)

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
            if self.type == 'bonjour':
                return bonjour_info(self.settings)
            return self.note or self.uri.uri
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
        notification.center.post_notification('BlinkContactDetailDidChange', sender=self)

    def _NH_VirtualContactDidChange(self, notification):
        self.__dict__.pop('icon', None)
        self.__dict__.pop('pixmap', None)
        notification.center.post_notification('BlinkContactDetailDidChange', sender=self)


@implementer(IObserver)
class ContactURI(object):

    size_hint = FontScaledSize(200, 24, lines=1, padding=8)

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
        self.unread_label.setFont(badge_font(self.font()))
        palette = self.info_label.palette()
        for color_group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(color_group, QPalette.ColorRole.WindowText, secondary_text_color(QApplication.palette(), color_group))
        self.info_label.setPalette(palette)
        self.info_label.setForegroundRole(QPalette.ColorRole.WindowText)
        self.time_label.setPalette(palette)
        self.time_label.setForegroundRole(QPalette.ColorRole.WindowText)
        time_font = QFont(self.font())
        if time_font.pointSizeF() > 0:
            time_font.setPointSizeF(max(time_font.pointSizeF() - 1, 6))
        self.time_label.setFont(time_font)
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
        self.name_label.setText(getattr(contact, 'display_name', contact.name))
        self.info_label.setTextFormat(Qt.TextFormat.PlainText)     # may quote a message
        time_text = format_row_time(getattr(contact, 'row_time', None))
        self.time_label.setText(time_text)
        self.time_label.setVisible(bool(time_text))
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
            # the label only holds the badge's place: ContactDelegate paints it, round and antialiased
            # on the second line, under the time and right-aligned with it
            metrics = QFontMetrics(self.unread_label.font())
            height = metrics.height() + 2
            width = max(height, metrics.horizontalAdvance(self.unread_label.text()) + height // 2 + 2)
            self.unread_label.setFixedSize(width, height)
            self.unread_label.setStyleSheet('color: transparent; background: transparent; padding: 0px;')

    def paint_unread_badge(self, painter, origin):
        """The unread count as a white number on a circle (a pill for wider numbers)."""
        label = self.unread_label
        if label.isHidden() or not label.text() or label.text() == '0':
            return
        rect = QRectF(label.geometry()).translated(QPointF(label.parentWidget().mapTo(self, QPoint(0, 0)))).translated(QPointF(origin))
        radius = rect.height() / 2
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.state_label.state_colors['available'].stroke)
        painter.drawRoundedRect(rect, radius, radius)
        painter.setPen(QColor('#ffffff'))
        painter.setFont(label.font())
        # across by the advance (digits are drawn to sit centred in it; the ink of a 1 is lopsided),
        # down by the ink of a digit (the line box's descent would push the number up)
        metrics = QFontMetricsF(label.font())
        ink = metrics.tightBoundingRect('0')
        center = rect.center()
        painter.drawText(QPointF(center.x() - metrics.horizontalAdvance(label.text()) / 2, center.y() - ink.y() - ink.height() / 2), label.text())
        painter.restore()


del ui_class, base_class


ui_class, base_class = uic.loadUiType(Resources.get('contact_group.ui'))


class GroupWidget(base_class, ui_class):
    def __init__(self, parent=None):
        super(GroupWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        font = self.name_label.font()
        font.setWeight(QFont.Weight(550))         # between medium and semi-bold: above the contacts' regular weight, softer than bold
        self.name_label.setFont(font)
        self.name_editor.setFont(font)
        self.selected = False
        self.drop_indicator = None
        self._disable_dnd = False
        follow_theme(self)
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

    def apply_theme(self):
        selected = self.__dict__.get('selected', False)
        self.__dict__['selected'] = None
        self.selected = selected

    def _set_selected(self, value):
        if self.__dict__.get('selected', None) == value:
            return
        self.__dict__['selected'] = value
        if value:
            self.name_label.setStyleSheet("color: #ffffff; font-weight: 550;")
        else:
            self.name_label.setStyleSheet("color: #e0e0e0; font-weight: 550;" if is_dark_theme() else "color: #000000; font-weight: 550;")
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
        if is_dark_theme():
            if self.selected:
                background.setColorAt(0.0, QColor('#5a5a5a'))
                background.setColorAt(1.0, QColor('#4a4a4a'))
                upper_color = QColor('#6a6a6a')
                lower_color = QColor('#2a2a2a')
                foreground = QColor('#ffffff')
            else:
                background.setColorAt(0.0, QColor('#3c3c3c'))
                background.setColorAt(1.0, QColor('#323232'))
                upper_color = QColor('#4a4a4a')
                lower_color = QColor('#222222')
                foreground = QColor('#aaaaaa')
        elif self.selected:
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

        # centred on the bar, sized to match the bold name
        if self.collapse_button.isChecked():
            arrow = QPolygonF([QPointF(0, 0), QPointF(0, 11), QPointF(9.5, 5.5)])
            arrow.translate(QPointF(5, (rect.height() - 11) / 2))
        else:
            arrow = QPolygonF([QPointF(0, 0), QPointF(11, 0), QPointF(5.5, 9.5)])
            arrow.translate(QPointF(4, (rect.height() - 9.5) / 2))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
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


def render_row(widget, painter, rect):
    """Paint a row widget into a list item at the screen's pixel ratio: a 1x pixmap
    is scaled up, and blurry, on a HiDPI screen."""
    device = painter.device()
    ratio = device.devicePixelRatioF() if device is not None else 1.0
    if widget.layout() is not None:
        widget.layout().activate()
    pixmap = QPixmap(rect.size() * ratio)
    pixmap.setDevicePixelRatio(ratio)
    widget.render(pixmap)
    painter.drawPixmap(rect.topLeft(), pixmap)


class ContactDelegate(QStyledItemDelegate, ColorHelperMixin):
    def __init__(self, parent=None):
        super(ContactDelegate, self).__init__(parent)
        self._create_widgets()
        follow_theme(self)

    def apply_theme(self):
        self._create_widgets()

    def _create_widgets(self):
        """The three widgets rows are rendered from (odd, even, selected), coloured for the current theme."""
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
        self.contact_selected_widget.time_label.setForegroundRole(QPalette.ColorRole.HighlightedText)

        # No theme except Oxygen honors the BackgroundRole
        palette = self.contact_oddline_widget.palette()
        palette.setColor(QPalette.ColorRole.Window, palette.color(QPalette.ColorRole.Base))
        self.contact_oddline_widget.setPalette(palette)

        palette = self.contact_evenline_widget.palette()
        if is_dark_theme():
            # dark theme: contact.ui fixes AlternateBase to a light blue, under the theme's light text
            palette.setColor(QPalette.ColorRole.AlternateBase, QApplication.palette().color(QPalette.ColorRole.AlternateBase))
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
        render_row(widget, painter, option.rect)
        widget.paint_unread_badge(painter, option.rect.topLeft())

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
        self._create_widget()
        follow_theme(self)

    def apply_theme(self):
        self._create_widget()

    def _create_widget(self):
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
        render_row(widget, painter, option.rect)

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
            instance_id = neighbour_instance_id(contact_uri.contact, contact_uri.uri.uri)
            uri_text = instance_id or contact_uri.uri.uri
            type_text = None if instance_id else contact_uri.uri.type
            color_group = QPalette.ColorGroup.Disabled if not option.state & QStyle.StateFlag.State_Enabled else QPalette.ColorGroup.Normal if option.state & QStyle.StateFlag.State_Active else QPalette.ColorGroup.Inactive
            text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, widget)
            text_rect.setRight(option.rect.right() - 5)
            if type_text:
                painter.setPen(option.palette.color(color_group, QPalette.ColorRole.HighlightedText) if option.state & QStyle.StateFlag.State_Selected else secondary_text_color(option.palette, color_group))
                painter.drawText(text_rect, Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, type_text)
                text_rect.adjust(0, 0, -option.fontMetrics.size(Qt.TextFlag.TextSingleLine, type_text).width() - 5, 0)
            text_color = option.palette.color(color_group, QPalette.ColorRole.HighlightedText if option.state & QStyle.StateFlag.State_Selected else QPalette.ColorRole.Text)
            text_width = text_rect.width()
            if option.fontMetrics.size(Qt.TextFlag.TextSingleLine, uri_text).width() > text_width:
                fade_start = 1 - 50.0 / text_width if text_width > 50 else 0.0
                gradient = QLinearGradient(text_rect.x(), 0, text_rect.right(), 0)
                gradient.setColorAt(fade_start, text_color)
                gradient.setColorAt(1.0, Qt.GlobalColor.transparent)
                painter.setClipRect(text_rect)
                painter.setPen(QPen(QBrush(gradient), 1.0))
            else:
                painter.setPen(text_color)
            painter.drawText(text_rect, Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, uri_text)

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
        AddressbookReloadLog().start()
        GroupKindStamper().start()
        MessagesGroupFiler().start()
        CallsGroupFiler().start()
        ContactRepair().start()
        AddressbookNotifier().start()

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
        notification_center.add_observer(self, name='BlinkMessagesGroupShouldPromote')
        notification_center.add_observer(self, name='BlinkConversationPreviewsDidChange')

    def _NH_BlinkConversationPreviewsDidChange(self, notification):
        # Messages group rows quote the last message and show its time, Calls and Tel rows the last call's
        # and are ordered by it: a changed row moves to its place (rows moved, not reset, so the
        # selection and the scroll position stay); the previews are already coalesced
        keys = notification.data.keys
        changed = [item for item in self.items if isinstance(item, Contact) and row_time_kind(getattr(item.group, 'settings', None)) is not None
                   and (keys is None or not keys.isdisjoint(item.conversation_keys))]
        for contact in changed:
            self._reposition_contact(contact)

    def _reposition_contact(self, contact):
        """Move a contact to its place in its group if its sort key changed, and repaint it."""
        try:
            position = self.items.index(contact)
        except ValueError:
            return
        move_point = self._find_contact_move_point(contact)
        if move_point is not None and move_point not in (position, position + 1):
            self.beginMoveRows(QModelIndex(), position, position, QModelIndex(), move_point)
            del self.items[position]
            self.items.insert(self._find_contact_insertion_point(contact), contact)
            self.endMoveRows()
        index = self.index(self.items.index(contact))
        self.dataChanged.emit(index, index)

    def _NH_BlinkMessagesGroupShouldPromote(self, notification):
        groups = self.items[GroupList]
        group = next((group for group in groups if getattr(group.settings, 'id', None) == MESSAGES_GROUP_ID), None)
        if group is None or groups[0] is group:
            return
        self.moveGroup(group, groups[0])
        ActivityLog().info('[contacts] Moved the Messages group to the top of the contact list')

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
        self._update_deleted_group_visibility()

    def _update_deleted_group_visibility(self):
        # the Deleted group is shown only while it holds somebody
        for position, item in enumerate(self.items):
            if isinstance(item, Group) and getattr(item.settings, 'id', None) == DELETED_GROUP_ID:
                self.contact_list.setRowHidden(position, len(item.settings.contacts) == 0)

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
        self._update_deleted_group_visibility()

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
        try:
            self.items[GroupContacts, notification.sender][notification.data.contact]
        except KeyError:
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
        try:
            position = self.items.index(contact)
        except ValueError:
            return

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
            uris = list(contact.uris)
            if neighbour_instance_id(contact) and contact.uris.default is not None:
                uris = [contact.uris.default]  # one row: the neighbour, not one per transport
            self.items = [ContactDetail(contact)] + [ContactURI(contact, uri) for uri in uris]
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
        self.actions.restore_contact = QAction(translate("contact_list", "Restore"), self, triggered=self._AH_RestoreContact)
        self.actions.remove_from_group = QAction(translate("contact_list", "Remove from Group"), self, triggered=self._AH_RemoveFromGroup)
        self.actions.delete_permanently = QAction(translate("contact_list", "Delete Permanently"), self, triggered=self._AH_DeletePermanently)
        self.actions.send_sms = QAction(translate("contact_list", "Send Messages"), self, triggered=self._AH_SendSMS)
        self.actions.start_audio_call = QAction(translate("contact_list", "Start Audio Call"), self, triggered=self._AH_StartAudioCall)
        self.actions.start_video_call = QAction(translate("contact_list", "Start Video Call"), self, triggered=self._AH_StartVideoCall)
        self.actions.start_chat_session = QAction(translate("contact_list", "Start MSRP Chat"), self, triggered=self._AH_StartChatSession)
        self.actions.send_files = QAction(translate("contact_list", "Send File(s)..."), self, triggered=self._AH_SendFiles)
        self.actions.request_screen = QAction(translate("contact_list", "Request Screen"), self, triggered=self._AH_RequestScreen)
        self.actions.share_my_screen = QAction(translate("contact_list", "Share My Screen"), self, triggered=self._AH_ShareMyScreen)
        self.actions.transfer_call = QAction(translate("contact_list", "Transfer Active Call"), self, triggered=self._AH_TransferCall)
        self.actions.remove_conversation = QAction(translate("contact_list", "Remove Conversation"), self, triggered=self._AH_RemoveConversation)
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
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        elif len(selected_items) > 1:
            menu.addAction(self.actions.delete_selection)
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.delete_selection.setEnabled(any(item.deletable for item in selected_items))
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
            menu.addSeparator()
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
        elif isinstance(selected_items[0], Group):
            menu.addAction(self.actions.edit_item)
            menu.addAction(self.actions.delete_item)
            menu.addSeparator()
            menu.addAction(self.actions.add_group)
            menu.addAction(self.actions.add_contact)
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.edit_item.setEnabled(selected_items[0].editable)
            self.actions.delete_item.setEnabled(selected_items[0].deletable)
            self.actions.undo_last_delete.setEnabled(len(model.deleted_items) > 0)
        elif isinstance(selected_items[0], Contact) and getattr(selected_items[0].group.settings, 'id', None) == DELETED_GROUP_ID:
            # a deleted contact is not called or written to: it can only be restored or deleted for good
            menu.addAction(self.actions.restore_contact)
            menu.addAction(self.actions.delete_permanently)
        else:
            contact = selected_items[0]
            account_manager = AccountManager()
            session_manager = SessionManager()
            can_call = account_manager.default_account is not None and contact.uri is not None
            can_transfer = contact.uri is not None and session_manager.active_session is not None and session_manager.active_session.state == 'connected'

            # a Bonjour neighbour is reached at the one address its transport ranking picks (contact.uri)
            many_uris = len(contact.uris) > 1 and contact.type != 'bonjour'
            if many_uris and can_call:
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

            if many_uris and can_transfer:
                call_submenu = menu.addMenu(translate('contact_list', 'Transfer Call'))
                for uri in contact.uris:
                    uri_text = '%s (%s)' % (uri.uri, uri.type) if uri.type not in ('SIP', 'Other') else uri.uri
                    call_item = QAction(uri_text, self)
                    call_item.triggered.connect(lambda: self._AH_TransferCall(uri))
                    call_submenu.addAction(call_item)
            else:
                menu.addAction(self.actions.transfer_call)
                self.actions.transfer_call.setEnabled(can_transfer)

            if is_messages_group(contact.group.settings):
                menu.addSeparator()
                if isinstance(contact.settings, MessageContact):
                    menu.addAction(self.actions.add_item)
                menu.addAction(self.actions.edit_item)
                menu.addAction(self.actions.delete_item)
                self.actions.delete_item.setEnabled(contact.deletable)
                menu.addSeparator()
                menu.addAction(self.actions.remove_conversation)
            elif contact.type == 'bonjour':
                # a neighbour's conversation is not in the Messages group (never in the addressbook)
                menu.addSeparator()
                menu.addAction(self.actions.remove_conversation)
            else:
                menu.addSeparator()
                menu.addAction(self.actions.edit_item)
                if contact.type == 'addressbook' and not contact.group.virtual and not is_managed_group(contact.group.settings):
                    menu.addAction(self.actions.remove_from_group)
                menu.addAction(self.actions.delete_item)
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
            if isinstance(item, Contact) and getattr(item.group.settings, 'id', None) != DELETED_GROUP_ID:   # a deleted contact is not called
                start_contact_conversation(item, item.uri)
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
                index = self.indexAt(event.position().toPoint())
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
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.position().toPoint())):
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
        indexes = self.selectionModel().selectedIndexes()
        items = [index.data(Qt.ItemDataRole.UserRole) for index in indexes]
        # an addressbook contact goes to Deleted first, whichever group it is selected in (Remove from
        # Group is what takes it out of one user group only)
        trash = [item for item in items if isinstance(item, Contact) and item.type == 'addressbook' and
                 getattr(item.group.settings, 'id', None) != DELETED_GROUP_ID]
        rest = [index for index, item in zip(indexes, items) if item not in trash]
        if trash:
            contacts = list({item.settings.id: item.settings for item in trash}.values())
            names = [contact.name or next(iter(contact.uris)).uri for contact in contacts]
            question = (translate('contact_list', "Move '%s' to the Deleted group?") % names[0] if len(contacts) == 1 else
                        translate('contact_list', 'Move %d contacts to the Deleted group?') % len(contacts))
            text = question + '\n\n' + translate('contact_list', 'Their messages are hidden, not deleted, and can be restored from the Deleted group. Delete them permanently from there to remove them for good.')
            if QMessageBox.question(self, translate('contact_list', 'Delete Contact'), text) == QMessageBox.StandardButton.Yes:
                ContactTrash.soft_delete(contacts)
        if rest:
            self.model().removeItems(rest)
        self.selectionModel().clearSelection()

    def _AH_RemoveFromGroup(self):
        indexes = [index for index in self.selectionModel().selectedIndexes()
                   if isinstance(index.data(Qt.ItemDataRole.UserRole), Contact) and not is_managed_group(index.data(Qt.ItemDataRole.UserRole).group.settings)]
        if indexes:
            self.model().removeItems(indexes)
        self.selectionModel().clearSelection()

    def _selected_deleted_contacts(self):
        items = [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedIndexes()]
        return list({item.settings.id: item.settings for item in items if isinstance(item, Contact) and item.type == 'addressbook' and
                     getattr(item.group.settings, 'id', None) == DELETED_GROUP_ID}.values())

    def _AH_RestoreContact(self):
        contacts = self._selected_deleted_contacts()
        if contacts:
            ContactTrash.restore(contacts)

    def _AH_DeletePermanently(self):
        contacts = self._selected_deleted_contacts()
        if not contacts:
            return
        names = [contact.name or contact.id for contact in contacts]
        question = (translate('contact_list', "Permanently delete '%s'?") % names[0] if len(contacts) == 1 else
                    translate('contact_list', 'Permanently delete %d contacts?') % len(contacts))
        text = question + '\n\n' + translate('contact_list', 'The contact is removed from the server address book, and its messages are deleted on all your devices. This cannot be undone.')
        if QMessageBox.warning(self, translate('contact_list', 'Delete Permanently'), text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            ContactTrash.delete_permanently(contacts)
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

    def _AH_RemoveConversation(self):
        contact = self.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        title = translate('contact_list', 'Remove Conversation')
        if contact.type == 'bonjour' or placeholder_instance_id(contact.uri.uri):
            message = translate('contact_list', 'Do you want to remove all messages exchanged with %s on this computer? This cannot be undone.') % contact.name
        else:
            message = translate('contact_list', 'Do you want to remove all messages exchanged with %s on all devices? This cannot be undone.') % contact.name
        if QMessageBox.warning(self, title, message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        message_manager = MessageManager()
        blink_session = message_manager.create_message_session(contact.uri.uri, selected=False)
        if blink_session is not None:
            message_manager.remove_conversation(blink_session)

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
            if rect.contains(event.position().toPoint()):
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
        if not group.virtual and not is_managed_group(group.settings) and (event.source() is not self or len(selected_groups) > 1 or group not in selected_groups):
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
        if isinstance(item, Contact) and getattr(item.group.settings, 'id', None) != DELETED_GROUP_ID:   # a deleted contact is not called
            start_contact_conversation(item, item.uri)

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
            self.actions.undo_last_delete.setText(undo_delete_text)
            self.actions.undo_last_delete.setEnabled(len(source_model.deleted_items) > 0)
        elif len(selected_items) > 1:
            menu.addAction(self.actions.delete_selection)
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
            if contact.type == 'bonjour':
                pass                # a neighbour is not in the addressbook: nothing to add, edit or delete
            else:
                if is_messages_group(contact.group.settings):
                    if isinstance(contact.settings, MessageContact):
                        menu.addAction(self.actions.add_item)
                menu.addAction(self.actions.edit_item)
                menu.addAction(self.actions.delete_item)
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
            if isinstance(item, Contact) and getattr(item.group.settings, 'id', None) != DELETED_GROUP_ID:   # a deleted contact is not called
                start_contact_conversation(item, item.uri)
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
                index = self.indexAt(event.position().toPoint())
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
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.position().toPoint())):
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
        # as in the contact list: an addressbook contact goes to Deleted (two-stage delete)
        model = self.model()
        indexes = [model.mapToSource(index) for index in self.selectionModel().selectedIndexes()]
        items = [index.data(Qt.ItemDataRole.UserRole) for index in indexes]
        contacts = list({item.settings.id: item.settings for item in items if isinstance(item, Contact) and item.type == 'addressbook' and
                         getattr(item.group.settings, 'id', None) != DELETED_GROUP_ID}.values())
        if contacts:
            names = [contact.name or next(iter(contact.uris)).uri for contact in contacts]
            question = (translate('contact_list', "Move '%s' to the Deleted group?") % names[0] if len(contacts) == 1 else
                        translate('contact_list', 'Move %d contacts to the Deleted group?') % len(contacts))
            text = question + '\n\n' + translate('contact_list', 'Their messages are hidden, not deleted, and can be restored from the Deleted group. Delete them permanently from there to remove them for good.')
            if QMessageBox.question(self, translate('contact_list', 'Delete Contact'), text) == QMessageBox.StandardButton.Yes:
                ContactTrash.soft_delete(contacts)

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
        if isinstance(item, Contact) and getattr(item.group.settings, 'id', None) != DELETED_GROUP_ID:   # a deleted contact is not called
            start_contact_conversation(item, item.uri)

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

    def apply_theme(self):
        palette = QPalette()            # the application's
        if not is_dark_theme():
            palette.setColor(QPalette.ColorRole.AlternateBase, QColor('#eeeeee'))
        self.setPalette(palette)

    def __init__(self, contact_list):
        super(ContactDetailView, self).__init__(contact_list.parent())
        self.apply_theme()
        follow_theme(self)
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
            start_contact_conversation(contact, selected_uri)
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
                index = self.indexAt(event.position().toPoint())
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
        if model.handleDroppedData(event.mimeData(), event.dropAction(), self.indexAt(event.position().toPoint())):
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
        start_contact_conversation(contact, selected_uri)

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
        # a phone number is stored as the addressbook stores one, as macOS and mobile do: bare E.164, type tel
        uri_type = self.default_uri_type
        e164 = pstn_e164(address, AccountManager().default_account) if address else None
        if e164:
            address, uri_type = e164, 'tel'
        items = [ContactURIItem(None, address, uri_type, False)] if address else []
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


def is_read_only_group(group_settings):
    """Messages, Calls and Tel: a record of who this account has messages or calls with,
    kept by the software (MessagesGroupFiler, CallsGroupFiler). The contact editor shows
    them, ticked when the contact is in one, but never offers them as a choice: putting
    somebody in one by hand states something untrue, and taking somebody out is undone
    by their next message or call (macOS ContactController.isReadOnlyGroup)."""
    if group_settings is None or isinstance(group_settings, VirtualGroup):
        return False
    return is_messages_group(group_settings) or is_group(group_settings, CALLS) or is_group(group_settings, TEL)


def is_selectable_group(group_settings):
    """Whether the contact editor lists this group: the ones the user files people into,
    plus the read-only ones, which the contact is shown in and so would look lost if left
    out (macOS ContactController.selectableGroups). Deleted and Conference are not listed."""
    if group_settings is None or isinstance(group_settings, VirtualGroup):
        return False
    return is_read_only_group(group_settings) or not is_managed_group(group_settings)


class GroupSelectionMenu(QMenu):
    """A menu of check boxes that stays open while they are toggled, so several groups
    can be picked in one go; any other item closes it as usual."""

    def _toggle_active(self):
        action = self.activeAction()
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()
            return True
        return False

    def mouseReleaseEvent(self, event):
        if not self._toggle_active():
            super(GroupSelectionMenu, self).mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._toggle_active():
            return
        super(GroupSelectionMenu, self).keyPressEvent(event)


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
        self.original_groups = []       # addressbook groups the edited contact was in when opened
        self.selected_groups = []       # addressbook groups the contact will be in on Ok
        self.created_groups = []        # groups made with Add Group... that the contact list may not show yet
        self.groups_menu = GroupSelectionMenu(self.groups_button)
        self.groups_menu.aboutToShow.connect(self._SH_GroupsMenuAboutToShow)
        self.groups_button.setMenu(self.groups_menu)
        self.name_editor.textChanged.connect(self._SH_NameEditorTextChanged)
        self.accepted.connect(self._SH_Accepted)
        self.rejected.connect(self._SH_Rejected)
        self.rejected.connect(self.contact_uri_model.reset)

    def setupUi(self, contact_editor):
        super(ContactEditorDialog, self).setupUi(contact_editor)
        self.preferred_media.setItemData(0, translate('contact_editor', 'messages'))
        self.preferred_media.setItemData(1, translate('contact_editor', 'audio'))
        self.preferred_media.setItemData(2, translate('contact_editor', 'video'))
        self.preferred_media.setItemData(3, translate('contact_editor', 'chat'))
        self.preferred_media.setItemData(4, translate('contact_editor', 'audio+chat'))
        self.addresses_table.verticalHeader().setDefaultSectionSize(URITypeComboBox().sizeHint().height())

    def open_for_add(self, sip_address='', target_group=None):
        self.edited_contact = None
        self.target_group = target_group
        self.contact_uri_model.init_with_address(sip_address)
        self.name_editor.setText('')
        self.organization_editor.setText('')
        self.icon_selector.init_with_contact(None)
        self.presence.setChecked(True)
        self.preferred_media.setCurrentIndex(self.preferred_media.findData('messages'))   # new contacts default to messages
        self.original_groups = []
        self.selected_groups = [target_group.settings] if target_group is not None and is_selectable_group(target_group.settings) else []
        self._update_groups_button()
        self.accept_button.setText(translate('contact_editor', 'Add'))
        self.accept_button.setEnabled(False)
        self.show()

    def open_for_edit(self, contact):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=contact)
        self.edited_contact = contact
        self.contact_uri_model.init_with_contact(contact)
        self.name_editor.setText(contact.name)
        self.organization_editor.setText(contact.organization or '')
        self.icon_selector.init_with_contact(contact)
        self.presence.setChecked(contact.presence.subscribe)
        self.auto_answer.setChecked(contact.auto_answer)
        self.preferred_media.setCurrentIndex(self.preferred_media.findData(contact.preferred_media))
        self.original_groups = [group for group in self._selectable_groups() if contact.id in {member.id for member in group.contacts}]
        self.selected_groups = list(self.original_groups)
        self._update_groups_button()
        self.accept_button.setText(translate('contact_editor', 'Ok'))
        self.accept_button.setEnabled(True)
        self.show()

    def _selectable_groups(self):
        """The addressbook groups the editor lists, in contact list order."""
        try:
            contact_model = self.parent().contact_model
        except AttributeError:
            return []
        return [group.settings for group in contact_model.items[GroupList] if is_selectable_group(group.settings)]

    def _update_groups_button(self):
        count = len(self.selected_groups)
        if count == 0:
            title = translate('contact_editor', 'No Selected Groups')
        elif count == 1:
            title = translate('contact_editor', 'One Selected Group')
        else:
            title = translate('contact_editor', '%d Selected Groups') % count
        self.groups_button.setText(title)
        self.groups_button.setToolTip(', '.join(group.name for group in self.selected_groups if group.name))

    def _SH_GroupsMenuAboutToShow(self):
        # built every time it opens, so a group added, renamed or removed meanwhile is current
        groups = self._selectable_groups()
        listed = {group.id for group in groups}
        self.selected_groups = [group for group in self.selected_groups if group.id in listed or group in self.created_groups]
        self.groups_menu.clear()
        for group in groups:
            action = self.groups_menu.addAction(group.name or translate('contact_editor', 'Unnamed Group'))
            action.setCheckable(True)
            action.setChecked(group in self.selected_groups)
            action.setEnabled(not is_read_only_group(group))
            action.setData(group)
            action.triggered.connect(partial(self._SH_GroupToggled, group))
        self.groups_menu.addSeparator()
        for title, handler in ((translate('contact_editor', 'Select All'), self._SH_SelectAllGroups),
                               (translate('contact_editor', 'Deselect All'), self._SH_DeselectAllGroups),
                               (translate('contact_editor', 'Add Group...'), self._SH_AddGroup)):
            action = self.groups_menu.addAction(title)
            action.triggered.connect(handler)
        self._update_groups_button()

    def _sync_group_actions(self):
        for action in self.groups_menu.actions():
            group = action.data()
            if action.isCheckable() and group is not None:
                action.setChecked(group in self.selected_groups)
        self._update_groups_button()

    def _SH_GroupToggled(self, group, checked):
        if is_read_only_group(group):     # disabled in the menu; the same rule where it cannot be routed around
            return
        if checked and group not in self.selected_groups:
            self.selected_groups.append(group)
        elif not checked and group in self.selected_groups:
            self.selected_groups.remove(group)
        self._update_groups_button()

    def _SH_SelectAllGroups(self, checked=False):
        # all the ones that are the user's to choose, plus whatever the software already decided
        self.selected_groups = [group for group in self._selectable_groups() if not is_read_only_group(group) or group in self.selected_groups]
        self._sync_group_actions()

    def _SH_DeselectAllGroups(self, checked=False):
        self.selected_groups = [group for group in self.selected_groups if is_read_only_group(group)]
        self._sync_group_actions()

    def _SH_AddGroup(self, checked=False):
        name, ok = QInputDialog.getText(self, translate('contact_editor', 'Add Group'), translate('contact_editor', 'Group name:'))
        name = name.strip() if ok else ''
        if not name:
            return
        group = addressbook.Group()
        group.name = name
        self.parent().contact_model._atomic_update(save=[group])
        self.created_groups.append(group)
        self.selected_groups.append(group)
        ActivityLog().info(f'[contacts] Created group {name} from the contact editor')
        self._update_groups_button()

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

        # A Bonjour neighbour's address is never added to the addressbook (is_bonjour_address);
        # one the contact already carries is left as it is.
        refused = [item.uri for item in self.contact_uri_model.items if item.uri and item.id not in contact.uris.ids() and is_bonjour_address(item.uri)]
        if refused:
            ActivityLog().info(f"[contacts] Not adding {', '.join(str(uri) for uri in refused)} to {self.name_editor.text() or 'a contact'}: a Bonjour neighbour is not written to the addressbook")
            QMessageBox.information(self, translate('contact_editor', 'Bonjour Neighbour'),
                                    translate('contact_editor', 'A Bonjour neighbour is reached on this network only, so it is not saved in the address book: %s') % ', '.join(str(uri) for uri in refused))
        if self.edited_contact is None and not any(item.uri and item.uri not in refused for item in self.contact_uri_model.items):
            self.contact_uri_model.reset()
            self._reset_groups()
            self.target_group = None
            return

        for id in set(contact.uris.ids()).difference(item.id for item in self.contact_uri_model.items):
            contact.uris.remove(contact.uris[id])
        for item in (item for item in self.contact_uri_model.items if item.uri and item.uri not in refused):
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
        contact.organization = self.organization_editor.text().strip()     # shared with the other clients (ag-projects:sipsimple)
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
        added = [group for group in self.selected_groups if group not in self.original_groups]
        # a read-only group is never left by hand: Deselect All and the menu both keep them
        removed = [group for group in self.original_groups if group not in self.selected_groups and not is_read_only_group(group)]
        if added and self.edited_contact is None:
            publish_contact_for_groups(contact)
        for group in added:
            if contact.id not in {member.id for member in group.contacts}:
                group.contacts.add(contact)
                modified_settings.append(group)
        for group in removed:
            if contact.id in {member.id for member in group.contacts}:
                group.contacts.remove(contact)
                modified_settings.append(group)
        if added or removed:
            ActivityLog().info(f"[contacts] {contact.name}: added to {', '.join(group.name for group in added) or 'no group'}, removed from {', '.join(group.name for group in removed) or 'no group'}")
        contact_model._atomic_update(save=modified_settings)

        self._reset_groups()
        self.contact_uri_model.reset()
        self.edited_contact = None
        self.target_group = None

    def _reset_groups(self):
        self.original_groups = []
        self.selected_groups = []
        self.created_groups = []
        self.groups_menu.clear()

    def _SH_Rejected(self):
        if self.edited_contact is not None:
            notification_center = NotificationCenter()
            notification_center.remove_observer(self, sender=self.edited_contact)
        self.contact_uri_model.reset()
        self._reset_groups()
        self.edited_contact = None
        self.target_group = None

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

    @staticmethod
    def _bonjour_neighbour_at(contact_model, uri):
        """The Bonjour neighbour announced at this user@host, or None."""
        if isinstance(uri, BaseSIPURI):
            user, host = uri.user, uri.host
            user = user.decode() if isinstance(user, bytes) else user
            host = host.decode() if isinstance(host, bytes) else host
        else:
            text = str(uri or '').strip()
            for scheme in ('sips:', 'sip:'):
                if text.lower().startswith(scheme):
                    text = text[len(scheme):]
                    break
            user, _, host = text.split(';', 1)[0].rpartition('@')
            host = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
        if not user or not host:
            return None
        for contact in (contact for contact in contact_model.iter_contacts() if contact.type == 'bonjour' and contact.uri is not None):
            for contact_uri in contact.uris:
                neighbour_user, neighbour_host = contact_uri.uri.user, contact_uri.uri.host
                neighbour_user = neighbour_user.decode() if isinstance(neighbour_user, bytes) else neighbour_user
                neighbour_host = neighbour_host.decode() if isinstance(neighbour_host, bytes) else neighbour_host
                if (neighbour_user, neighbour_host.lower()) == (user, host.lower()):
                    return contact
        return None

    @classmethod
    def find_contact(cls, uri, display_name=None, exact=True, instance_id=None):
        contact_model = QApplication.instance().main_window.contact_model

        # A Bonjour conversation is keyed by the neighbour's instance id, whatever
        # address the neighbour has on the network today. A key read back from
        # history is the bare id; while the neighbour is away it is addressed by
        # the sip:<id>@bonjour.local placeholder.
        if not isinstance(uri, BaseSIPURI):
            neighbour_id = bare_instance_id(uri) if is_instance_id(uri) else placeholder_instance_id(uri)
            if neighbour_id:
                instance_id = instance_id or neighbour_id
                uri = bonjour_placeholder_uri(neighbour_id)
        if instance_id:
            bare_id = bare_instance_id(instance_id)
            for contact in (contact for contact in contact_model.iter_contacts() if contact.type == 'bonjour' and contact.uri is not None):
                if bare_instance_id(contact.settings.id) == bare_id:
                    return contact, contact.uri
        else:
            # An address a neighbour is announced at, typed or left over in
            # history without its port and transport, is still that neighbour:
            # it must be reached link-local, never through a SIP account.
            neighbour = cls._bonjour_neighbour_at(contact_model, uri)
            if neighbour is not None:
                return neighbour, neighbour.uri

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


