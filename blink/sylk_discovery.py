"""SylkServer discovery: the infrastructure settings a domain publishes, as Sylk Mobile reads them.

A domain served by SylkServer has a DNS TXT record _sylkserver.<domain> holding
one URL, the domain's configuration: a JSON document (wsServer, the conference
and file transfer servers, ICE servers, PSTN settings, test numbers, ...).
Sylk Mobile looks it up when it signs in (lookupSylkServer, downloadSylkConfiguration);
Blink does the same for the domain of each of its SIP accounts when the account
is activated, and again once a day.

The TXT record is asked of the system resolver, with Google's DNS over HTTPS
(dns.google, as mobile) when that fails. What was downloaded is kept in a folder
per domain, <data>/sylkserver/<domain>/configuration.json, and loaded at start,
so the settings are known before the network is, and kept when a later lookup or
download fails.

The activity log says where the settings came from and which keys changed, not
their values (they are in the cache). What the server says about its infrastructure overwrites what the user
set on the domain's accounts: conference.sipBridge becomes the Conference server
(mobile dials and recognises rooms on it), the PSTN rule replacePlus the IDD
prefix (what replaces the + of a number dialled). SylkServerConfigurationDidChange is posted with them
(sender the discovery, data domain and configuration) for what will use them.
"""

import json
import os
import threading

from datetime import datetime, timezone

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.account import AccountManager, BonjourAccount

from blink.logging import ActivityLog
from blink.resources import ApplicationData
from blink.util import call_in_gui_thread, run_in_gui_thread


__all__ = ['SylkServerDiscovery', 'has_sylkserver', 'server_http_url']


cache_folder = 'sylkserver'                 # <data>/sylkserver/<domain>/configuration.json
cache_file = 'configuration.json'
old_cache_name = 'sylkserver.json'          # the first version: every domain in one file
refresh_interval = 24 * 3600        # seconds
dns_timeout = 6                     # seconds, as mobile
download_timeout = 8                # seconds, as mobile


def has_sylkserver(account):
    """Whether the account's domain runs SylkServer: it publishes a configuration (found now
    or in an earlier run). Bonjour has none."""
    if account is None or account is BonjourAccount():
        return False
    try:
        return bool(SylkServerDiscovery().configuration(account.id.domain))
    except Exception:
        return False


def server_http_url(account):
    """The SylkServer web root of the account's domain, as mobile derives it from wsServer
    (wss://host/path/ws -> https://host/path); None without a configuration or a wsServer."""
    if account is None or account is BonjourAccount():
        return None
    configuration = SylkServerDiscovery().configuration(account.id.domain) or {}
    server = configuration.get('wsServer')
    if not isinstance(server, str) or not server.strip():
        return None
    server = server.strip()
    if server.startswith('wss://'):
        server = 'https://' + server[len('wss://'):]
    elif server.startswith('ws://'):
        server = 'http://' + server[len('ws://'):]
    server = server.rstrip('/')
    if server.endswith('/ws'):
        server = server[:-3]
    return server


def _txt_by_resolver(name):
    """The TXT records of name, by the system's name servers.

    sipsimple makes dnspython green (eventlib sockets, sipsimple.lookup), so a query
    from an ordinary thread fails ("TwistedHub hub can only be instantiated once"):
    it is made in a green thread, with sipsimple's DNSResolver (the name servers
    sipsimple uses), and waited for here."""
    done = threading.Event()
    outcome = {}

    def query():
        try:
            from sipsimple.lookup import DNSResolver
            resolver = DNSResolver()
            resolver.timeout = 3
            resolver.lifetime = dns_timeout
            answers = resolver.resolve(name, 'TXT')
            outcome['records'] = [b''.join(record.strings).decode('utf-8', 'replace') for record in answers]
        except Exception as e:
            outcome['error'] = e
        finally:
            done.set()

    from sipsimple.threading.green import call_in_green_thread
    call_in_green_thread(query)
    if not done.wait(dns_timeout + 2):
        raise TimeoutError(f'no answer in {dns_timeout + 2} seconds')
    if 'error' in outcome:
        raise outcome['error']
    return outcome['records']


def _txt_by_https(name):
    import requests
    response = requests.get('https://dns.google/resolve', params={'name': name, 'type': 'TXT'}, timeout=dns_timeout)
    response.raise_for_status()
    data = response.json()
    if data.get('Status') == 3:     # NXDOMAIN
        return []
    return [str(answer.get('data', '')).strip('"') for answer in data.get('Answer') or () if answer.get('type') == 16]


@implementer(IObserver)
class SylkServerDiscovery(object, metaclass=Singleton):

    def __init__(self):
        self.configurations = {}        # domain: {'configurationUrl', 'fetched', 'configuration'}
        self.checked = {}               # domain: when it was last looked up (time.monotonic) in this run
        self.running = set()            # domains being looked up now
        self.lock = threading.Lock()
        self._started = False
        self._timer = None

    @property
    def folder(self):
        return ApplicationData.get(cache_folder)

    def path(self, domain):
        return os.path.join(self.folder, domain, cache_file)

    @staticmethod
    def _valid_domain(domain):
        return bool(domain) and '/' not in domain and '\\' not in domain and not domain.startswith('.')

    def configuration(self, domain):
        """The settings of a domain, as last downloaded (possibly in an earlier run), or None."""
        entry = self.configurations.get(str(domain).lower())
        return entry.get('configuration') if entry else None

    # Start

    def start(self):
        if self._started:
            return
        self._started = True
        self._load()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPAccountDidActivate')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        from PyQt6.QtCore import QTimer
        self._timer = QTimer()
        self._timer.setInterval(refresh_interval * 1000)
        self._timer.timeout.connect(self._refresh_all)
        self._timer.start()

    def _migrate(self):
        """The first version kept every domain in <data>/sylkserver.json: one folder each now."""
        old = ApplicationData.get(old_cache_name)
        if not os.path.exists(old):
            return
        try:
            with open(old, encoding='utf-8') as cache:
                data = json.load(cache)
            for domain, entry in (data.items() if isinstance(data, dict) else ()):
                if self._valid_domain(domain) and isinstance(entry, dict) and not os.path.exists(self.path(domain)):
                    self._save(domain, entry)
            os.unlink(old)
            ActivityLog().info(f'[sylkserver] Moved the cached configurations from {old} to {self.folder}')
        except Exception as e:
            ActivityLog().warning(f'[sylkserver] Cannot move the cached configurations from {old}: {e}')

    def _load(self):
        activity = ActivityLog()
        self._migrate()
        try:
            domains = sorted(name for name in os.listdir(self.folder) if os.path.isdir(os.path.join(self.folder, name)))
        except FileNotFoundError:
            domains = []
        for domain in domains:
            path = self.path(domain)
            try:
                with open(path, encoding='utf-8') as cache:
                    entry = json.load(cache)
                if not isinstance(entry, dict) or not isinstance(entry.get('configuration'), dict):
                    raise ValueError('no configuration in it')
            except FileNotFoundError:
                continue
            except Exception as e:
                activity.warning(f'[sylkserver] Cannot read the cached configuration of {domain} ({path}): {e}')
                continue
            self.configurations[domain] = entry
        if not self.configurations:
            activity.info(f'[sylkserver] No cached SylkServer configurations yet ({self.folder})')
        for domain, entry in sorted(self.configurations.items()):
            configuration = entry['configuration']
            activity.info(f"[sylkserver] Cached configuration of {domain} (from {entry.get('configurationUrl')}, fetched {entry.get('fetched')}): {len(configuration)} keys")

    def _save(self, domain, entry):
        if not self._valid_domain(domain):
            return
        path = self.path(domain)
        temporary = path + '.tmp'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self.lock:
                text = json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False)
            with open(temporary, 'w', encoding='utf-8') as cache:
                cache.write(text)
            os.replace(temporary, path)
        except Exception as e:
            ActivityLog().warning(f'[sylkserver] Cannot write the cached configuration of {domain} ({path}): {e}')

    # When to look

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountDidActivate(self, notification):
        account = notification.sender
        if account is BonjourAccount():
            return
        self._apply(account, self.configuration(account.id.domain))
        self.discover(account.id.domain)

    def _apply_to_domain(self, domain, configuration):
        for account in AccountManager().get_accounts():
            if account is not BonjourAccount() and account.id.domain == domain:
                self._apply(account, configuration)

    @classmethod
    def _apply(cls, account, configuration):
        """What of the domain's settings goes into the account, overwriting the user's (the server
        knows its infrastructure): conference.sipBridge as its Conference server, and the PSTN
        rule replacePlus (pstn.rules, as mobile reads it, or a top-level rules) as its IDD prefix."""
        if not configuration:
            return
        conference = configuration.get('conference')
        bridge = conference.get('sipBridge') if isinstance(conference, dict) else None
        if isinstance(bridge, str) and bridge.strip():
            cls._set(account, 'server.conference_server', bridge.strip().lower(), 'conference.sipBridge', 'Conference server')
        pstn = configuration.get('pstn')
        rules = pstn.get('rules') if isinstance(pstn, dict) else None
        if not isinstance(rules, dict):
            rules = configuration.get('rules')
        replace_plus = rules.get('replacePlus') if isinstance(rules, dict) else None
        if isinstance(replace_plus, (str, int)) and str(replace_plus).strip():
            value = str(replace_plus).strip()
            # '+' is the IDD prefix setting's own way of saying "keep the +" (None)
            cls._set(account, 'pstn.idd_prefix', None if value == '+' else value, 'rules.replacePlus', 'IDD prefix')

    @staticmethod
    def _set(account, name, value, source, what):
        group, _, attribute = name.partition('.')
        settings = getattr(account, group)
        current = getattr(settings, attribute)
        if (str(current) if current is not None else None) == value:
            return
        try:
            setattr(settings, attribute, value)
            account.save()
        except Exception as e:
            ActivityLog().warning(f'[sylkserver] Cannot set the {what} of {account.id} to {value}: {e}')
            return
        ActivityLog().info(f"[sylkserver] {what} of {account.id} set to {value or 'none'} ({source} of {account.id.domain}, was {current or 'not set'})")

    def _NH_SIPApplicationWillEnd(self, notification):
        if self._timer is not None:
            self._timer.stop()

    def _refresh_all(self):
        domains = {account.id.domain for account in AccountManager().get_accounts() if account is not BonjourAccount() and account.enabled}
        for domain in sorted(domains):
            self.discover(domain, force=True)

    def discover(self, domain, force=False):
        """Look up the domain's TXT record and download its configuration, in a thread of its own;
        once per run unless forced (the daily refresh)."""
        import time
        domain = str(domain or '').strip().lower()
        if not self._valid_domain(domain):
            return
        last = self.checked.get(domain)
        if domain in self.running or (not force and last is not None and time.monotonic() - last < refresh_interval):
            return
        self.checked[domain] = time.monotonic()
        self.running.add(domain)
        threading.Thread(target=self._discover, args=(domain,), name=f'sylkserver-{domain}', daemon=True).start()

    # The lookup (in its own thread)

    def _discover(self, domain):
        try:
            self._lookup_and_download(domain)
        except Exception as e:
            ActivityLog().exception(f'[sylkserver] Discovery for {domain} failed: {e!r}')
        finally:
            call_in_gui_thread(self.running.discard, domain)

    def _lookup_and_download(self, domain):
        activity = ActivityLog()
        name = f'_sylkserver.{domain}'
        records, how = None, None
        try:
            records, how = _txt_by_resolver(name), 'DNS'
        except Exception as e:
            reason = type(e).__name__ if not str(e) else str(e)
            if type(e).__name__ in ('NXDOMAIN', 'NoAnswer'):
                records, how = [], 'DNS'
            else:
                activity.info(f'[sylkserver] DNS lookup of the TXT record {name} failed ({reason}), asking dns.google')
        if records is None:
            try:
                records, how = _txt_by_https(name), 'dns.google'
            except Exception as e:
                activity.warning(f'[sylkserver] TXT record {name} not found: DNS and dns.google failed ({e}); keeping the cached configuration' if domain in self.configurations
                                 else f'[sylkserver] TXT record {name} not found: DNS and dns.google failed ({e})')
                return
        urls = [record.strip() for record in records if record.strip()]
        if not urls:
            activity.info(f'[sylkserver] {domain} publishes no SylkServer configuration (no TXT record {name}, by {how})')
            return
        if len(urls) > 1:
            activity.warning(f"[sylkserver] {name} has {len(urls)} TXT records, expected one: {', '.join(urls)}")
            return
        url = urls[0]
        if not url.startswith(('https://', 'http://')):
            activity.warning(f'[sylkserver] The TXT record {name} is not a URL: {url}')
            return
        activity.info(f'[sylkserver] {domain}: TXT record {name} -> {url} (by {how}), downloading')

        import requests
        try:
            response = requests.get(url, timeout=download_timeout)
            response.raise_for_status()
            configuration = response.json()
        except Exception as e:
            activity.warning(f'[sylkserver] Cannot download the configuration of {domain} from {url}: {e}' + ('; keeping the cached one' if domain in self.configurations else ''))
            return
        if not isinstance(configuration, dict):
            activity.warning(f'[sylkserver] The configuration of {domain} from {url} is not a JSON object')
            return
        if not configuration.get('wsServer'):
            activity.warning(f'[sylkserver] The configuration of {domain} from {url} has no wsServer (mobile does not use it either)')

        entry = {'configurationUrl': url,
                 'fetched': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                 'configuration': configuration}
        with self.lock:
            previous = (self.configurations.get(domain) or {}).get('configuration')
            self.configurations[domain] = entry
        self._log_changes(domain, url, previous, configuration)
        self._save(domain, entry)
        call_in_gui_thread(self._apply_to_domain, domain, configuration)
        if previous != configuration:
            call_in_gui_thread(NotificationCenter().post_notification, 'SylkServerConfigurationDidChange', sender=self,
                               data=NotificationData(domain=domain, configuration=configuration, configuration_url=url))

    def _log_changes(self, domain, url, previous, configuration):
        activity = ActivityLog()
        if previous is None:
            activity.info(f'[sylkserver] Configuration of {domain} (from {url}): {len(configuration)} keys')
            return
        added = sorted(set(configuration) - set(previous))
        removed = sorted(set(previous) - set(configuration))
        changed = sorted(key for key in set(configuration) & set(previous) if configuration[key] != previous[key])
        if not (added or removed or changed):
            activity.info(f'[sylkserver] Configuration of {domain} unchanged ({len(configuration)} keys)')
            return
        # the keys only, not their values
        parts = [f"{what} {', '.join(keys)}" for what, keys in (('added', added), ('removed', removed), ('changed', changed)) if keys]
        activity.info(f"[sylkserver] Configuration of {domain} changed: {'; '.join(parts)}")
