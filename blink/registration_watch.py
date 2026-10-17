"""Says so when an account's registration gets no result at all.

The SDK reports a registration only when it ends: registered, or failed. While it is
still looking up where to register (NAPTR, SRV, A) and the name servers do not answer,
nothing is reported and nothing is sent, so the logs stay silent and the account just
does not register. A watch is armed when an account that registers is activated; if
neither success nor failure has come when it fires, that is logged, and the registrar
lookup is made again by Blink itself, step by step with short timeouts, so the step
that gets no answer (and from which name servers) is in the activity log and in the
SIP tab of the logs window. When the system's name servers do not answer, dns.google is
asked the same question, which tells a DNS server problem from a network that is down.
"""

import threading
import time

from application.notification import NotificationCenter, NotificationData

from blink.logging import ActivityLog


__all__ = ['RegistrationWatch']


class RegistrationWatch(object):

    first_delay = 20        # seconds after activation before a missing result is reported
    repeat_delay = 60       # seconds between reports while there is still no result
    query_timeout = 3       # seconds per name server
    query_lifetime = 5      # seconds per question

    def __init__(self):
        self._timers = {}   # account id -> threading.Timer
        self._lock = threading.Lock()

    def arm(self, account, delay=None):
        """Watch an account that registers until its registration succeeds or fails."""
        from sipsimple.account import Account
        if not isinstance(account, Account) or not account.enabled or not account.sip.register:
            return
        self.disarm(account)
        timer = threading.Timer(self.first_delay if delay is None else delay, self._fired, args=(account, time.monotonic()))
        timer.daemon = True
        with self._lock:
            self._timers[account.id] = timer
        timer.start()

    def disarm(self, account):
        with self._lock:
            timer = self._timers.pop(account.id, None)
        if timer is not None:
            timer.cancel()

    def _fired(self, account, armed):
        with self._lock:
            if self._timers.get(account.id) is None:
                return
        waited = int(time.monotonic() - armed)
        if not account.enabled or not account.sip.register:
            self.disarm(account)
            return
        ActivityLog().warning(f'[register] Account {account.id}: no registration result after {waited} seconds, nothing was sent yet; checking where it registers')
        self._probe(account)
        self.arm(account, self.repeat_delay)

    def _probe(self, account):
        """Look up the registrar the way the SDK does, with short timeouts, in a green thread
        (sipsimple's resolver sockets are eventlib's)."""
        from sipsimple.threading.green import call_in_green_thread
        call_in_green_thread(self._lookup, account)

    def _lookup(self, account):
        from sipsimple.lookup import DNSResolver
        proxy = account.sip.outbound_proxy
        resolver = DNSResolver()
        resolver.timeout = self.query_timeout
        resolver.lifetime = self.query_lifetime
        servers = ', '.join(str(server) for server in getattr(resolver, 'nameservers', [])) or 'none configured'
        prefix = f'[register] Account {account.id}:'
        if proxy is not None:
            hosts = [str(proxy.host)]
            source = f'outbound proxy {proxy.host}'
        else:
            domain = str(account.id.domain)
            source = f'domain {domain}'
            hosts = []
            for service in ('_sips._tcp', '_sip._tcp', '_sip._udp'):
                name = f'{service}.{domain}'
                records = self._query(resolver, name, 'SRV', prefix, servers)
                if records is None:
                    return          # no answer: said so, the rest would only wait as long
                hosts.extend(str(record.target).rstrip('.') for record in records if str(record.target).rstrip('.') not in hosts)
            if not hosts:
                hosts = [domain]    # no SRV records: the domain itself (RFC 3263)
        for host in hosts:
            if self._query(resolver, host, 'A', prefix, servers) is None:
                return
        self._say(f'{prefix} registrar lookup of {source} answered by {servers}: the name servers work, the SDK lookup should not be waiting')

    def _query(self, resolver, name, kind, prefix, servers):
        """The records of name, [] when there are none, None when nobody answered (logged)."""
        import dns.exception
        import dns.resolver
        timeouts = tuple(cls for cls in (getattr(dns.resolver, 'LifetimeTimeout', None), dns.exception.Timeout) if cls is not None)
        try:
            answer = resolver.resolve(name, kind)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.resolver.NoNameservers as e:
            self._say(f'{prefix} DNS lookup {kind} {name} failed: no name server could answer ({servers}): {e}', error=True)
            self._ask_google(name, kind, prefix)
            return None
        except timeouts:
            self._say(f'{prefix} DNS lookup {kind} {name} failed: no answer in {self.query_lifetime} seconds from {servers}', error=True)
            self._ask_google(name, kind, prefix)
            return None
        except Exception as e:
            self._say(f'{prefix} DNS lookup {kind} {name} failed: {e!r}', error=True)
            return None
        return list(answer)

    def _ask_google(self, name, kind, prefix):
        """The same question over HTTPS: an answer means only the name servers are failing."""
        from sipsimple.threading import run_in_thread

        @run_in_thread('network-io')
        def ask():
            import requests
            try:
                response = requests.get('https://dns.google/resolve', params={'name': name, 'type': kind}, timeout=self.query_lifetime)
                response.raise_for_status()
                answers = [entry.get('data', '') for entry in response.json().get('Answer', [])]
            except Exception as e:
                self._say(f'{prefix} dns.google could not be asked either ({e.__class__.__name__}): the network seems to be down', error=True)
            else:
                self._say(f'{prefix} dns.google answers {kind} {name}: {", ".join(answers) or "no records"}; the system name servers are not answering', error=True)
        ask()

    @staticmethod
    def _say(message, error=False):
        """In the activity log and, as a DNS line, in the SIP tab of the logs window."""
        if error:
            ActivityLog().warning(message)
        else:
            ActivityLog().info(message)
        from datetime import datetime
        NotificationCenter().post_notification('UILogMessage', data=NotificationData(message=f'{datetime.now()}: {message}\n', section='sip'))
