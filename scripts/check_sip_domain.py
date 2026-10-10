#!/usr/bin/env python3
"""Check the SIP infrastructure of a domain, the way a client finds and uses it.

    check_sip_domain.py sylk.link
    check_sip_domain.py sylk.link -a ag@sylk.link
    check_sip_domain.py example.com -a alice@example.com -p secret

1. DNS, with the SIP SIMPLE SDK's resolver, as RFC 3263 has it: the NAPTR records
   of the domain, the SRV records they point to (or the _sips._tcp / _sip._tcp /
   _sip._udp SRV records when there is no NAPTR) and the A records of every
   target; then the routes the SDK itself would try, in order.
2. TLS (when the domain offers it): every address of every TLS target is
   connected to with openssl s_client and its certificate verified against the
   system's certificate authorities and the target's name (or --tls-name, or
   sip2sip.info when the server presents it: sylk.link's servers do); the certificate's
   subject, issuer, expiry and names are shown, and whether the SIP domain is
   among them.
3. Registration, once, as sip-register3 does: with an account of the domain
   taken from Blink's configuration (~/.blink/config, or -a to choose one), or
   given with -a and -p. Then it unregisters.

Nothing of Blink's is written: the SDK runs on a temporary configuration.
The exit status is 0 when every check passed.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from getpass import getpass
from optparse import OptionParser
from threading import Event

from application import log
from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from sipsimple.account import Account, AccountManager
from sipsimple.application import SIPApplication
from sipsimple.configuration.backend.file import FileBackend
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI
from sipsimple.storage import FileStorage
from sipsimple.threading.green import run_in_green_thread


SYSTEM_CA_FILES = ('/etc/ssl/certs/ca-certificates.crt', '/etc/pki/tls/certs/ca-bundle.crt', '/etc/ssl/cert.pem')
NAPTR_SERVICES = {'SIPS+D2T': 'tls', 'SIP+D2T': 'tcp', 'SIP+D2U': 'udp'}
SRV_SERVICES = (('_sips._tcp', 'tls'), ('_sip._tcp', 'tcp'), ('_sip._udp', 'udp'))
DEFAULT_PORTS = {'tls': 5061, 'tcp': 5060, 'udp': 5060}
# the name the servers of sylk.link (and other domains they serve) carry in their certificate:
# when a server presents it, it is the name verified (as Blink's tls_name_exceptions)
SHARED_TLS_NAME = 'sip2sip.info'


# Output

def out(text=''):
    sys.stdout.write(text + '\n')
    sys.stdout.flush()


def section(title):
    out()
    out(title)
    out('=' * len(title))


def ok(text):
    out(f'  [ OK ] {text}')


def fail(text):
    out(f'  [FAIL] {text}')


def note(text):
    out(f'  [ -- ] {text}')


def text(value):
    return value.decode(errors='replace') if isinstance(value, bytes) else str(value)


# Blink's accounts

def blink_accounts(config_directory):
    path = os.path.join(os.path.expanduser(config_directory), 'config')
    if not os.path.exists(path):
        return {}
    try:
        accounts = FileBackend(path).load().get('Accounts') or {}
    except Exception as e:
        out(f'Cannot read {path}: {e}')
        return {}
    return {key: value for key, value in accounts.items() if '@' in key and isinstance(value, dict)}


# The checks

@implementer(IObserver)
class DomainCheck(object):

    def __init__(self, options, domain):
        self.options = options
        self.domain = domain.lower()
        self.application = SIPApplication()
        self.directory = tempfile.mkdtemp(prefix='check_sip_domain-')
        self.done = Event()
        self.failures = 0
        self.account = None
        self.credentials = None         # (account id, username, password)
        self.tls_targets = []           # [(hostname, port)]
        self.addresses = {}             # hostname: [addresses]
        self.tls_name = options.tls_name            # None: sip2sip.info when presented, else each server's own name
        self.presented_tls_name = None              # sip2sip.info when a server presented it

    def run(self):
        log.level.current = log.level.WARNING
        self.credentials = self._find_credentials()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.application)
        notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidFail')
        notification_center.add_observer(self, name='SIPAccountRegistrationGotAnswer')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidEnd')
        notification_center.add_observer(self, name='SIPEngineTransportGotCertificateError')
        try:
            self.application.start(FileStorage(self.directory))
            self.done.wait()
            thread = getattr(self.application, 'thread', None)
            if thread is not None:
                thread.join(10)
        finally:
            shutil.rmtree(self.directory, ignore_errors=True)
        return self.failures

    def _find_credentials(self):
        options = self.options
        accounts = blink_accounts(options.config_directory)
        account_id = options.account
        if account_id is None:
            candidates = sorted(key for key in accounts if key.partition('@')[2].lower() == self.domain)
            account_id = candidates[0] if candidates else None
        if account_id is None:
            return None
        settings = accounts.get(account_id, {})
        auth = settings.get('auth') or {}
        password = options.password if options.password is not None else auth.get('password')
        if password is None and sys.stdin.isatty():
            password = getpass(f'Password of {account_id}: ')
        if password is None:
            return None
        return account_id, auth.get('username') or None, password

    def handle_notification(self, notification):
        if notification.name == 'SIPApplicationDidEnd':
            self.done.set()         # the reactor is going away: not through a green thread
            return
        self._dispatch(notification)

    @run_in_green_thread
    def _dispatch(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        settings = SIPSimpleSettings()
        settings.sip.udp_port = 0           # random ports: Blink may be running
        settings.sip.tcp_port = 0
        settings.sip.tls_port = 0
        settings.tls.verify_server = True
        settings.tls.ca_list = next((path for path in SYSTEM_CA_FILES if os.path.exists(path)), None)
        certificate = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'resources', 'tls', 'default.crt')
        settings.tls.certificate = os.path.normpath(certificate) if os.path.exists(certificate) else None
        settings.audio.input_device = None
        settings.audio.output_device = None
        settings.audio.alert_device = None
        settings.save()

    def _NH_SIPApplicationDidStart(self, notification):
        self.check()

    def check(self):
        try:
            self.check_dns()
            self.check_routes()
            self.check_tls()
        except Exception as e:
            self.failures += 1
            fail(f'Unexpected error: {e!r}')
        if self.options.no_register:
            self.application.stop()
        else:
            self.register()

    # 1. DNS

    def _resolve(self, name, kind):
        from sipsimple.lookup import DNSResolver
        import dns.resolver
        resolver = DNSResolver()
        resolver.timeout = 3
        resolver.lifetime = 10
        try:
            return list(resolver.resolve(name, kind))
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []

    def check_dns(self):
        section(f'DNS records of {self.domain}')
        srv_names = []          # [(srv name, transport)]
        try:
            naptrs = self._resolve(self.domain, 'NAPTR')
        except Exception as e:
            naptrs = []
            fail(f'NAPTR lookup failed: {e}')
        sip_naptrs = [record for record in naptrs if text(record.service).upper() in NAPTR_SERVICES]
        if sip_naptrs:
            out(f'  NAPTR {self.domain}')
            for record in sorted(sip_naptrs, key=lambda r: (r.order, r.preference)):
                service = text(record.service).upper()
                replacement = str(record.replacement).rstrip('.')
                out(f'    order {record.order:<3} pref {record.preference:<3} {service:<9} ({NAPTR_SERVICES[service]})  -> {replacement}')
                srv_names.append((replacement, NAPTR_SERVICES[service]))
        else:
            note('No SIP NAPTR records: trying the SRV records directly')
            srv_names = [(f'{prefix}.{self.domain}', transport) for prefix, transport in SRV_SERVICES]

        hosts = {}              # hostname: [addresses]
        found_srv = False
        for name, transport in srv_names:
            try:
                srvs = self._resolve(name, 'SRV')
            except Exception as e:
                fail(f'SRV lookup of {name} failed: {e}')
                continue
            if not srvs:
                note(f'No SRV records for {name}')
                continue
            found_srv = True
            out(f'  SRV {name} ({transport})')
            for record in sorted(srvs, key=lambda r: (r.priority, -r.weight)):
                target = str(record.target).rstrip('.')
                out(f'    priority {record.priority:<3} weight {record.weight:<4} port {record.port:<5} -> {target}')
                hosts.setdefault(target, None)
                if transport == 'tls' and (target, record.port) not in self.tls_targets:
                    self.tls_targets.append((target, record.port))
        if not found_srv:
            note(f'No SRV records: {self.domain} itself, on the default ports')
            hosts[self.domain] = None
            if not sip_naptrs:
                self.tls_targets.append((self.domain, DEFAULT_PORTS['tls']))

        for host in hosts:
            try:
                addresses = [record.address for record in self._resolve(host, 'A')]
            except Exception as e:
                addresses = []
                fail(f'A lookup of {host} failed: {e}')
            hosts[host] = addresses
            if addresses:
                out(f'  A {host}: {", ".join(addresses)}')
            else:
                self.failures += 1
                fail(f'{host} has no A records')
        self.addresses = hosts
        if not any(hosts.values()):
            self.failures += 1
            fail(f'No SIP server address found for {self.domain}')

    # 1b. what the SDK does with them

    def check_routes(self):
        from sipsimple.lookup import DNSLookup
        section('Routes the SDK would try, in order (RFC 3263)')
        settings = SIPSimpleSettings()
        try:
            routes = DNSLookup().lookup_sip_proxy(SIPURI(host=self.domain), settings.sip.transport_list, tls_name=self.tls_name or self.domain).wait()
        except Exception as e:
            self.failures += 1
            fail(f'Lookup failed: {e}')
            return
        for number, route in enumerate(routes, 1):
            out(f'  {number:>2}. {route.address}:{route.port};transport={route.transport}')

    # 2. TLS

    def check_tls(self):
        if not self.tls_targets:
            section('TLS')
            note(f'{self.domain} offers no TLS transport')
            return
        section('TLS servers')
        ca_file = SIPSimpleSettings().tls.ca_list
        if self.tls_name:
            note(f'Certificates are verified for {self.tls_name}')
        for hostname, port in self.tls_targets:
            for address in self.addresses.get(hostname) or ():
                out(f'  {hostname} ({address}) port {port}')
                self._check_tls_server(hostname, address, port, ca_file)

    def _check_tls_server(self, hostname, address, port, ca_file):
        # the chain is verified by openssl, the name here: which name to expect depends on the certificate
        command = ['openssl', 's_client', '-connect', f'{address}:{port}', '-servername', self.tls_name or hostname,
                   '-verify_return_error', '-showcerts']
        if ca_file:
            command += ['-CAfile', str(ca_file)]
        try:
            result = subprocess.run(command, input=b'', capture_output=True, timeout=15)
        except FileNotFoundError:
            self.failures += 1
            fail('openssl is not installed')
            return
        except subprocess.TimeoutExpired:
            self.failures += 1
            fail('No TLS answer in 15 seconds')
            return
        output = text(result.stdout) + text(result.stderr)
        verify = re.search(r'Verify return code: (\d+) \(([^)]*)\)', output)
        certificate = re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', output, re.S)
        if certificate is None:
            self.failures += 1
            reason = next((line.strip() for line in output.splitlines() if 'error' in line.lower()), 'no certificate received')
            fail(f'TLS connection failed: {reason}')
            return
        details = subprocess.run(['openssl', 'x509', '-noout', '-subject', '-issuer', '-enddate', '-ext', 'subjectAltName'],
                                 input=certificate.group(0).encode(), capture_output=True, timeout=10)
        names = []
        for line in text(details.stdout).splitlines():
            line = line.strip()
            if line.startswith('subject='):
                out(f'         Subject: {line[8:].strip()}')
                common_name = re.search(r'CN\s*=\s*([^,/]+)', line)
                if common_name and common_name.group(1).strip() not in names:
                    names.append(common_name.group(1).strip())
            elif line.startswith('issuer='):
                out(f'         Issuer:  {line[7:].strip()}')
            elif line.startswith('notAfter='):
                out(f'         Expires: {line[9:].strip()}')
            elif 'DNS:' in line:
                names += [name.strip()[4:] for name in line.split(',') if name.strip().startswith('DNS:') and name.strip()[4:] not in names]
                out(f"         Names:   {', '.join(name.strip()[4:] for name in line.split(',') if name.strip().startswith('DNS:'))}")

        def matches(name):
            name = name.lower()
            return any(name == pattern.lower() or (pattern.startswith('*.') and name.endswith(pattern[1:].lower()) and name.count('.') == pattern.count('.'))
                       for pattern in names)

        # the name to verify: given, else sip2sip.info when the server presents it (sylk.link and
        # other domains served by the sip2sip.info servers), else the server's own name
        if self.tls_name:
            expected = self.tls_name
        elif matches(SHARED_TLS_NAME):
            expected = SHARED_TLS_NAME
            self.presented_tls_name = SHARED_TLS_NAME
        else:
            expected = hostname
        chain_ok = verify is not None and verify.group(1) == '0'
        if chain_ok and matches(expected):
            ok(f'Certificate verified for {expected}')
        elif not chain_ok:
            self.failures += 1
            fail(f"Certificate not verified: {verify.group(2) if verify else 'unknown error'}")
        else:
            self.failures += 1
            fail(f'Certificate valid but not for {expected}')
        if expected == hostname and hostname != self.domain:
            if matches(self.domain):
                ok(f'The SIP domain {self.domain} is in the certificate')
            else:
                note(f'The SIP domain {self.domain} is not in the certificate (RFC 5922 clients verify it; this one verifies {hostname})')

    # 3. Registration

    def register(self):
        section('Registration')
        if self.credentials is None:
            note(f'No account of {self.domain} found in Blink (use -a and -p to register one)')
            self.application.stop()
            return
        account_id, username, password = self.credentials
        out(f'  Registering {account_id} once...')
        account = Account(account_id)
        account.auth.password = password
        if username:
            account.auth.username = username
        account.sip.register = True
        if self.tls_name or self.presented_tls_name:
            account.sip.tls_name = self.tls_name or self.presented_tls_name
        account.sip.register_interval = 60
        account.message_summary.enabled = False
        account.presence.enabled = False
        account.xcap.enabled = False
        account.enabled = True
        AccountManager().default_account = account
        self.account = account
        account.save()
        from twisted.internet import reactor
        self._register_timer = reactor.callLater(self.options.timeout, self._registration_timeout)

    def _registration_timeout(self):
        self.failures += 1
        fail(f'No registration answer in {self.options.timeout} seconds')
        self.application.stop()

    def _cancel_timer(self):
        timer = getattr(self, '_register_timer', None)
        if timer is not None and timer.active():
            timer.cancel()

    def _NH_SIPAccountRegistrationGotAnswer(self, notification):
        data = notification.data
        if data.code >= 300:
            registrar = data.registrar
            fail(f'{registrar.address}:{registrar.port};transport={registrar.transport} answered {data.code} {text(data.reason)}')

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        data = notification.data
        registrar = data.registrar
        ok(f'Registered contact {data.contact_header.uri} at {registrar.address}:{registrar.port};transport={registrar.transport} for {data.expires} seconds')
        self._cancel_timer()
        self.application.stop()

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        self.failures += 1
        fail(f'Registration failed: {text(notification.data.error)}')
        self._cancel_timer()
        self.application.stop()

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        note('Unregistered')

    def _NH_SIPEngineTransportGotCertificateError(self, notification):
        data = notification.data
        get = (lambda key: data.get(key)) if isinstance(data, dict) else (lambda key: getattr(data, key, None))
        fail(f"SIP TLS certificate error from {get('remote_address')} (expected name {get('remote_hostname')}): {get('reason')}")


def main():
    description = 'Checks the SIP servers of a domain: DNS (NAPTR, SRV, A), the TLS certificate of every server, then one registration.'
    parser = OptionParser(usage='%prog [options] domain', description=description)
    parser.add_option('-a', '--account', dest='account', help='the account to register (default: the first of the domain in Blink)', metavar='USER@DOMAIN')
    parser.add_option('-p', '--password', dest='password', help='its password (default: from Blink, else asked)')
    parser.add_option('-c', '--config-directory', dest='config_directory', default='~/.blink', help="Blink's data directory (default ~/.blink)")
    parser.add_option('--tls-name', dest='tls_name', help='the name the TLS certificates must carry (default: sip2sip.info when presented, else each server\'s name)')
    parser.add_option('-t', '--timeout', type='int', dest='timeout', default=30, help='seconds to wait for the registration (default 30)')
    parser.add_option('-n', '--no-register', action='store_true', dest='no_register', default=False, help='only check DNS and TLS')
    options, args = parser.parse_args()
    if len(args) != 1:
        parser.print_help()
        return 2
    domain = args[0].strip()
    if '@' in domain:
        options.account = options.account or domain
        domain = domain.partition('@')[2]
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    failures = DomainCheck(options, domain).run()
    section('Result')
    out(f'  {"All checks passed" if not failures else f"{failures} check(s) failed"}')
    return 0 if not failures else 1


if __name__ == '__main__':
    sys.exit(main())
