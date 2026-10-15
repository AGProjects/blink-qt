#!/usr/bin/env python3
"""Check the SIP infrastructure of a domain, the way a client finds and uses it.

    check_sip_domain.py                 (the domain of Blink's default account)
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
import shutil
import signal
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
from sipsimple.storage import FileStorage
from sipsimple.threading.green import run_in_green_thread

# blink.sip_domain_check stands alone; it is loaded by path because importing it as part of the
# blink package would run blink/__init__.py, which imports the whole GUI and its .ui resources
import importlib.util   # noqa: E402
_spec = importlib.util.spec_from_file_location('sip_domain_check', os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'blink', 'sip_domain_check.py'))
_sip_domain_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sip_domain_check)
DomainInspector, TextReport, system_ca_file, text = _sip_domain_check.DomainInspector, _sip_domain_check.TextReport, _sip_domain_check.system_ca_file, _sip_domain_check.text


# Output

def out(line=''):
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


REPORT = TextReport(out)
section, ok, fail, note = REPORT.section, REPORT.ok, REPORT.fail, REPORT.note


# Blink's accounts

def _blink_config(config_directory):
    path = os.path.join(os.path.expanduser(config_directory), 'config')
    if not os.path.exists(path):
        return {}
    try:
        return FileBackend(path).load() or {}
    except Exception as e:
        out(f'Cannot read {path}: {e}')
        return {}


def blink_accounts(config_directory):
    accounts = _blink_config(config_directory).get('Accounts') or {}
    return {key: value for key, value in accounts.items() if '@' in key and isinstance(value, dict)}


def blink_default_account(config_directory):
    """The id of Blink's default SIP account: the one set as default when it is a SIP account,
    else the first enabled one. None when Blink has no SIP account."""
    config = _blink_config(config_directory)
    accounts = blink_accounts(config_directory)
    default = config.get('default_account')
    for value in config.values():
        if default is None and isinstance(value, dict):
            default = value.get('default_account')
    if isinstance(default, str) and default in accounts:
        return default
    enabled = [key for key, value in sorted(accounts.items()) if str(value.get('enabled', 'true')).lower() not in ('false', '0', 'no') and not key.endswith('@local')]
    return enabled[0] if enabled else None


# The checks

@implementer(IObserver)
class DomainCheck(object):

    def __init__(self, options, domain):
        self.options = options
        self.domain = domain.lower()
        self.application = SIPApplication()
        self.directory = tempfile.mkdtemp(prefix='check_sip_domain-')
        self.done = Event()
        self.account = None
        self.credentials = None         # (account id, username, password)
        self.tls_name = options.tls_name            # None: sip2sip.info when presented, else each server's own name
        self.inspector = None

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
        return REPORT.failures

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
        settings.tls.ca_list = system_ca_file()
        certificate = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'resources', 'tls', 'default.crt')
        settings.tls.certificate = os.path.normpath(certificate) if os.path.exists(certificate) else None
        settings.audio.input_device = None
        settings.audio.output_device = None
        settings.audio.alert_device = None
        settings.save()

    def _NH_SIPApplicationDidStart(self, notification):
        self.check()

    def check(self):
        # DNS, routes and TLS (blink.sip_domain_check), in this green thread
        self.inspector = DomainInspector(self.domain, REPORT, tls_name=self.tls_name, ca_file=SIPSimpleSettings().tls.ca_list)
        self.inspector.check()
        if self.options.no_register:
            self.application.stop()
        else:
            self.register()

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
        presented_tls_name = self.inspector.presented_tls_name if self.inspector is not None else None
        if self.tls_name or presented_tls_name:
            account.sip.tls_name = self.tls_name or presented_tls_name
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
            fail(f'{registrar.address}:{registrar.port};transport={registrar.transport} answered {data.code} {text(data.reason)}', count=False)

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        data = notification.data
        registrar = data.registrar
        ok(f'Registered contact {data.contact_header.uri} at {registrar.address}:{registrar.port};transport={registrar.transport} for {data.expires} seconds')
        self._cancel_timer()
        self.application.stop()

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        fail(f'Registration failed: {text(notification.data.error)}')
        self._cancel_timer()
        self.application.stop()

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        note('Unregistered')

    def _NH_SIPEngineTransportGotCertificateError(self, notification):
        data = notification.data
        get = (lambda key: data.get(key)) if isinstance(data, dict) else (lambda key: getattr(data, key, None))
        fail(f"SIP TLS certificate error from {get('remote_address')} (expected name {get('remote_hostname')}): {get('reason')}", count=False)


def main():
    description = 'Checks the SIP servers of a domain: DNS (NAPTR, SRV, A), the TLS certificate of every server, then one registration.'
    parser = OptionParser(usage='%prog [options] [domain]', description=description + " Without a domain, the domain of Blink's default account.")
    parser.add_option('-a', '--account', dest='account', help='the account to register (default: the first of the domain in Blink)', metavar='USER@DOMAIN')
    parser.add_option('-p', '--password', dest='password', help='its password (default: from Blink, else asked)')
    parser.add_option('-c', '--config-directory', dest='config_directory', default='~/.blink', help="Blink's data directory (default ~/.blink)")
    parser.add_option('--tls-name', dest='tls_name', help='the name the TLS certificates must carry (default: sip2sip.info when presented, else each server\'s name)')
    parser.add_option('-t', '--timeout', type='int', dest='timeout', default=30, help='seconds to wait for the registration (default 30)')
    parser.add_option('-n', '--no-register', action='store_true', dest='no_register', default=False, help='only check DNS and TLS')
    options, args = parser.parse_args()
    if not args:
        account_id = options.account or blink_default_account(options.config_directory)
        if account_id is None:
            out(f'No domain given and no SIP account in {os.path.join(options.config_directory, "config")}')
            parser.print_help()
            return 2
        out(f'Using account {account_id}')
        args = [account_id]
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
