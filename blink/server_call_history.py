"""The server's call history (CDRTool), as Blink for macOS reads it.

Every five minutes, and when an account is activated or its server settings
change, each SIP account with a Server Settings URL fetches
<settings_url>?action=get_history&realm=<domain> with the account's
credentials. The answer is {"received": [...], "placed": [...]}, one dict per
call: sessionId (the Call-ID), fromTag, toTag, remoteParty, startTime,
stopTime, timezone, duration, status, media, proxyIP, sipTraceUrl and,
on newer servers, outcome.

Each call becomes a call detail record with source 'server' and goes to
MessageHistory.store_call_record, as a record from another device does: it merges it into the row this device stored
for the same Call-ID, where the server's view wins the fields it knows best
(duration, status, outcome, sipTraceUrl, ...), or stores it as a new row
for a call this device did not see.
"""

import urllib.parse

from datetime import datetime, timezone

from PyQt6.QtCore import QTimer

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.account import Account, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread

from blink.logging import ActivityLog
from blink.message_envelopes import build_call_record
from blink.util import run_in_gui_thread


__all__ = ['ServerCallHistory']


POLL_INTERVAL = 300     # seconds, as on macOS
DEFAULT_TIMEZONE = 'Europe/Amsterdam'   # what CDRTool assumes when a call has none

# Outcomes CDRTool sends; anything else is derived from duration and status.
_SERVER_OUTCOMES = ('completed', 'missed', 'cancelled', 'failed', 'voicemail', 'answered_elsewhere', 'rejected')


def server_call_outcome(call, direction):
    outcome = str(call.get('outcome') or '').strip()
    if outcome in _SERVER_OUTCOMES:
        return outcome
    try:
        duration = int(call.get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        return 'completed'
    if direction == 'outgoing':
        return 'cancelled' if str(call.get('status') or '').strip() == '487' else 'failed'
    return 'missed'


def _party(text):
    """(user@host, display name) of a CDRTool remoteParty ('"Name" <sip:user@host;params>' or 'user@host')."""
    text = str(text or '').strip()
    display_name = ''
    if '<' in text and '>' in text:
        display_name = text[:text.index('<')].strip().strip('"').strip()
        text = text[text.index('<') + 1:text.index('>')]
    for scheme in ('sips:', 'sip:', 'tel:'):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    text = text.split(';', 1)[0].split('?', 1)[0]
    return text, display_name


def _time(value, zone):
    """A CDRTool local time ('YYYY-MM-DD HH:MM:SS' in the call's time zone) as an aware UTC datetime, or None."""
    text = str(value or '').strip()
    if not text or text.startswith('0000'):
        return None
    try:
        when = datetime.strptime(' '.join(text.split()), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=zone)
    return when.astimezone(timezone.utc)


def _zone(name):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = str(name or '').replace('\\/', '/').strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def server_call_record(call, direction):
    """A call detail record (source 'server') of one CDRTool call, or None when it is not a usable call."""
    call_id = str(call.get('sessionId') or '').strip()
    if not call_id:
        return None
    media = call.get('media') or ['audio']
    if isinstance(media, str):
        media = [part.strip() for part in media.split(',') if part.strip()]
    if not {'audio', 'video'} & {str(part).lower() for part in media}:
        return None
    remote_party, display_name = _party(call.get('remoteParty'))
    if not remote_party:
        return None
    zone = _zone(call.get('timezone'))
    start_time = _time(call.get('startTime'), zone)
    stop_time = _time(call.get('stopTime'), zone) or start_time
    try:
        duration = int(float(call.get('duration') or 0))
    except (TypeError, ValueError):
        duration = 0
    reported = str(call.get('direction') or '').strip()
    if reported and reported != direction:
        ActivityLog().warning(f'[calls] Server history call {call_id} is in the {direction} list but reports direction={reported}')
    url = str(call.get('sipTraceUrl') or '').strip()
    return build_call_record(call_id, direction, server_call_outcome(call, direction), duration=duration,
                             status=call.get('status') or None, remote_party=remote_party,
                             display_name=str(call.get('displayName') or display_name or ''),
                             start_time=start_time, stop_time=stop_time, media=[str(part).lower() for part in media],
                             from_tag=str(call.get('fromTag') or ''), to_tag=str(call.get('toTag') or ''),
                             proxy_ip=call.get('proxyIP') or None, call_timezone=call.get('timezone') or None,
                             sip_trace_url=url if url.lower().startswith(('https://', 'http://')) else None,
                             source='server')


def history_url(account):
    settings_url = account.server.settings_url
    if not settings_url:
        return None
    parts = urllib.parse.urlparse(str(settings_url))
    return urllib.parse.urlunparse(parts._replace(query=f'action=get_history&realm={account.id.domain}'))


@implementer(IObserver)
class ServerCallHistory(object, metaclass=Singleton):

    def __init__(self):
        self.timers = {}        # account id: QTimer
        self.running = set()    # account ids with a fetch under way
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPAccountDidActivate')
        notification_center.add_observer(self, name='SIPAccountDidDeactivate')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

    def start(self, account):
        self.stop(account)
        if account is BonjourAccount() or not isinstance(account, Account) or not account.enabled:
            return
        if not account.server.settings_url:
            ActivityLog().info(f'[calls] No server call history for {account.id}: the Server Settings URL is not set')
            return
        timer = QTimer()
        timer.setInterval(POLL_INTERVAL * 1000)
        timer.timeout.connect(lambda account=account: self.fetch(account))
        timer.start()
        self.timers[account.id] = timer
        self.fetch(account)

    def stop(self, account):
        timer = self.timers.pop(account.id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def fetch(self, account):
        if account.id in self.running:
            return
        url = history_url(account)
        if url is None:
            return
        self.running.add(account.id)
        self._fetch(account, url)

    @run_in_thread('server-call-history')
    def _fetch(self, account, url):
        try:
            calls = self._get(account, url)
            if calls is not None:
                self._store(account, calls)
        finally:
            self._done(account)

    @run_in_gui_thread
    def _done(self, account):
        self.running.discard(account.id)

    def _get(self, account, url):
        import requests
        from requests.auth import HTTPBasicAuth, HTTPDigestAuth
        settings = SIPSimpleSettings()
        username = account.auth.username or account.id.username
        password = account.auth.password or ''
        verify = settings.tls.verify_server
        try:
            response = requests.get(url, timeout=15, verify=verify)
            if response.status_code == 401:
                challenge = response.headers.get('WWW-Authenticate', '').lower()
                auth = HTTPBasicAuth(username, password) if challenge.startswith('basic') else HTTPDigestAuth(username, password)
                response = requests.get(url, timeout=15, verify=verify, auth=auth)
            if response.status_code == 401:
                ActivityLog().error(f'[calls] Server call history of {account.id} refused the credentials ({url})')
                return None
            response.raise_for_status()
            calls = response.json()
        except requests.RequestException as e:
            ActivityLog().error(f'[calls] Server call history of {account.id} could not be retrieved from {url}: {e}')
            return None
        except ValueError as e:
            ActivityLog().error(f'[calls] Server call history of {account.id} from {url} is not JSON: {e}')
            return None
        if not isinstance(calls, dict):
            ActivityLog().error(f'[calls] Server call history of {account.id} from {url} has no received/placed lists')
            return None
        return calls

    def _store(self, account, calls):
        from blink.history import MessageHistory
        message_history = MessageHistory()
        counts = {}
        for direction, key in (('incoming', 'received'), ('outgoing', 'placed')):
            stored = skipped = 0
            for call in calls.get(key) or []:
                record = server_call_record(call, direction) if isinstance(call, dict) else None
                if record is None:
                    skipped += 1
                    continue
                message_history.store_call_record(account, record, origin='server call history')
                stored += 1
            counts[key] = (stored, skipped)
        (received, received_skipped), (placed, placed_skipped) = counts['received'], counts['placed']
        ActivityLog().info(f'[calls] Server call history of {account.id}: {received} received, {placed} placed'
                           + (f', {received_skipped + placed_skipped} skipped (no Call-ID, party or audio/video)' if received_skipped + placed_skipped else ''))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountDidActivate(self, notification):
        self.start(notification.sender)

    def _NH_SIPAccountDidDeactivate(self, notification):
        self.stop(notification.sender)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        account = notification.sender
        if not isinstance(account, Account):
            return
        modified = notification.data.modified
        if {'server.settings_url', 'auth.password', 'auth.username', 'enabled'} & set(modified):
            if account.enabled:
                self.start(account)
            else:
                self.stop(account)
