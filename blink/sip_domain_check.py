"""The SIP infrastructure of a domain as a client finds it: DNS as RFC 3263 has it (NAPTR, SRV, A),
the routes the SIP SIMPLE SDK would try, and the TLS certificate of every TLS server.

Shared by scripts/check_sip_domain.py (which also registers) and the DNS Information dialog
of the preferences (blink.preferences). The DNS checks use the SDK's resolver, whose sockets are
eventlib's: they run in a green thread of the reactor (an OS thread has no hub). The TLS check
runs openssl and waits for it, which a green thread must not: in a running application it goes
to a thread of its own. What they find is given, as it is found, to a Report (text or HTML).
"""

import os
import re
import subprocess


__all__ = 'Report', 'TextReport', 'HtmlReport', 'DomainInspector', 'RelayInspector', 'SYSTEM_CA_FILES', 'SHARED_TLS_NAME', 'system_ca_file', 'lookup_routes'


SYSTEM_CA_FILES = ('/etc/ssl/certs/ca-certificates.crt', '/etc/pki/tls/certs/ca-bundle.crt', '/etc/ssl/cert.pem')
NAPTR_SERVICES = {'SIPS+D2T': 'tls', 'SIP+D2T': 'tcp', 'SIP+D2U': 'udp'}
SRV_SERVICES = (('_sips._tcp', 'tls'), ('_sip._tcp', 'tcp'), ('_sip._udp', 'udp'))
DEFAULT_PORTS = {'tls': 5061, 'tcp': 5060, 'udp': 5060}
# the name the servers of sylk.link (and other domains they serve) carry in their certificate:
# when a server presents it, it is the name verified (as Blink's tls_name_exceptions)
SHARED_TLS_NAME = 'sip2sip.info'


def system_ca_file():
    return next((path for path in SYSTEM_CA_FILES if os.path.exists(path)), None)


def lookup_routes(domain, tls_name):
    """The routes the SDK would try for a domain, in order. In a green thread."""
    from sipsimple.configuration.settings import SIPSimpleSettings
    from sipsimple.core import SIPURI
    from sipsimple.lookup import DNSLookup
    return DNSLookup().lookup_sip_proxy(SIPURI(host=domain), SIPSimpleSettings().sip.transport_list, tls_name=tls_name).wait()


def text(value):
    return value.decode(errors='replace') if isinstance(value, bytes) else str(value)


class Report(object):
    """Where the findings go, as they are found: sections of rows (a label and its value, which may
    have several lines), subsections (one per TLS server) and verdicts (ok, fail, note).
    Drawn as text by TextReport (the script) and as HTML by HtmlReport (the DNS Information dialog)."""

    def __init__(self):
        self.failures = 0

    def section(self, title):
        raise NotImplementedError

    def subsection(self, title):
        raise NotImplementedError

    def row(self, label, value):
        raise NotImplementedError

    def verdict(self, kind, line):
        raise NotImplementedError

    def ok(self, line):
        self.verdict('ok', line)

    def fail(self, line, count=True):
        """A failure; count=False for one shown but not failing the check (a lookup error the next step survives)."""
        if count:
            self.failures += 1
        self.verdict('fail', line)

    def note(self, line):
        self.verdict('note', line)


class TextReport(Report):
    """The findings as lines of text, given to write(line) one by one."""

    marks = {'ok': '[ OK ]', 'fail': '[FAIL]', 'note': '[ -- ]'}

    def __init__(self, write):
        super().__init__()
        self.write = write

    def out(self, line=''):
        self.write(line)

    def section(self, title):
        self.write('')
        self.write(title)
        self.write('=' * len(title))

    def subsection(self, title):
        self.write(f'  {title}')

    def row(self, label, value):
        lines = str(value).split('\n')
        self.write(f'    {label}: {lines[0]}')
        for line in lines[1:]:
            self.write(f'    {" " * len(label)}  {line}')

    def verdict(self, kind, line):
        self.write(f'  {self.marks[kind]} {line}')


class HtmlReport(Report):
    """The findings kept, to be drawn as HTML (html()) as the message info panel draws a message:
    a heading per section, gray labels next to their values; changed() is called after each one."""

    # mid tones, readable on a light and on a dark background alike
    colors = {'ok': '#3fa34d', 'fail': '#e0443e', 'note': '#8a8a8a'}
    marks = {'ok': '\u2714', 'fail': '\u2718', 'note': '\u2013'}

    def __init__(self, changed=None):
        super().__init__()
        self.changed = changed or (lambda: None)
        self.sections = []          # [(title, [(kind, label, value)])]

    def _add(self, kind, label, value):
        if not self.sections:
            self.sections.append(('', []))
        self.sections[-1][1].append((kind, label, value))
        self.changed()

    def section(self, title):
        self.sections.append((title, []))
        self.changed()

    def subsection(self, title):
        self._add('subsection', title, None)

    def row(self, label, value):
        self._add('row', label, value)

    def verdict(self, kind, line):
        self._add(kind, None, line)

    def html(self):
        import html
        parts = []
        for title, rows in self.sections:
            cells = []
            for kind, label, value in rows:
                if kind == 'subsection':
                    cells.append(f'<tr><td colspan="2" style="padding-top:8px"><b>{html.escape(str(label))}</b></td></tr>')
                elif kind == 'row':
                    cells.append(f'<tr><td style="color:gray;padding-right:12px;vertical-align:top;white-space:nowrap">{html.escape(str(label))}</td>'
                                 f'<td style="white-space:pre-wrap">{html.escape(str(value))}</td></tr>')
                else:
                    color = self.colors[kind]
                    # a note's text in the palette's own color (no color given): only its mark is gray
                    text_style = f'color:{color};' if kind != 'note' else ''
                    cells.append(f'<tr><td style="color:{color};padding-right:12px;vertical-align:top;text-align:right">{self.marks[kind]}</td>'
                                 f'<td style="{text_style}white-space:pre-wrap">{html.escape(str(value))}</td></tr>')
            heading = f'<h3 style="margin-top:14px">{html.escape(title)}</h3>' if title else ''
            parts.append(f'{heading}<table cellspacing="2">{"".join(cells)}</table>')
        return ''.join(parts)


class DomainInspector(object):
    """DNS, routes and TLS of a domain. check_dns and check_routes in a green thread, check_tls
    anywhere (it blocks on openssl)."""

    def __init__(self, domain, report, tls_name=None, ca_file=None):
        self.domain = domain.lower()
        self.report = report
        self.tls_name = tls_name                    # None: sip2sip.info when presented, else each server's own name
        self.ca_file = ca_file
        self.tls_targets = []                       # [(hostname, port)]
        self.addresses = {}                         # hostname: [addresses]
        self.presented_tls_name = None              # sip2sip.info when a server presented it

    def check(self):
        """All of them, in a green thread (blocking it while openssl runs: for scripts)."""
        self.check_lookups()
        self.check_certificates()

    def check_lookups(self):
        """DNS and routes. In a green thread."""
        try:
            self.check_dns()
            self.check_routes()
        except Exception as e:
            self.report.fail(f'Unexpected error: {e!r}')

    def check_certificates(self):
        """TLS. In any thread: it waits for openssl."""
        try:
            self.check_tls()
        except Exception as e:
            self.report.fail(f'Unexpected error: {e!r}')

    # DNS

    @staticmethod
    def _resolve(name, kind):
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
        report = self.report
        report.section(f'DNS records of {self.domain}')
        srv_names = []          # [(srv name, transport)]
        try:
            naptrs = self._resolve(self.domain, 'NAPTR')
        except Exception as e:
            naptrs = []
            report.fail(f'NAPTR lookup failed: {e}', count=False)
        sip_naptrs = [record for record in naptrs if text(record.service).upper() in NAPTR_SERVICES]
        if sip_naptrs:
            lines = []
            for record in sorted(sip_naptrs, key=lambda r: (r.order, r.preference)):
                service = text(record.service).upper()
                replacement = str(record.replacement).rstrip('.')
                lines.append(f'{NAPTR_SERVICES[service].upper()} \u2192 {replacement}   (order {record.order}, preference {record.preference})')
                srv_names.append((replacement, NAPTR_SERVICES[service]))
            report.row('NAPTR', '\n'.join(lines))
        else:
            report.note('No SIP NAPTR records: trying the SRV records directly')
            srv_names = [(f'{prefix}.{self.domain}', transport) for prefix, transport in SRV_SERVICES]

        hosts = {}              # hostname: [addresses]
        found_srv = False
        for name, transport in srv_names:
            try:
                srvs = self._resolve(name, 'SRV')
            except Exception as e:
                report.fail(f'SRV lookup of {name} failed: {e}', count=False)
                continue
            if not srvs:
                report.note(f'No SRV records for {name}')
                continue
            found_srv = True
            lines = []
            for record in sorted(srvs, key=lambda r: (r.priority, -r.weight)):
                target = str(record.target).rstrip('.')
                lines.append(f'{target}:{record.port}   (priority {record.priority}, weight {record.weight})')
                hosts.setdefault(target, None)
                if transport == 'tls' and (target, record.port) not in self.tls_targets:
                    self.tls_targets.append((target, record.port))
            report.row(f'SRV {name}', '\n'.join(lines))
        if not found_srv:
            report.note(f'No SRV records: {self.domain} itself, on the default ports')
            hosts[self.domain] = None
            if not sip_naptrs:
                self.tls_targets.append((self.domain, DEFAULT_PORTS['tls']))

        for host in hosts:
            try:
                addresses = [record.address for record in self._resolve(host, 'A')]
            except Exception as e:
                addresses = []
                report.fail(f'A lookup of {host} failed: {e}', count=False)
            hosts[host] = addresses
            if addresses:
                report.row(f'A {host}', ', '.join(addresses))
            else:
                report.fail(f'{host} has no A records')
        self.addresses = hosts
        if not any(hosts.values()):
            report.fail(f'No SIP server address found for {self.domain}')

    # what the SDK does with them

    def check_routes(self):
        report = self.report
        report.section('Routes, in the order they are tried (RFC 3263)')
        try:
            routes = lookup_routes(self.domain, self.tls_name or self.domain)
        except Exception as e:
            report.fail(f'Lookup failed: {e}')
            return
        if not routes:
            report.fail('No routes')
        for number, route in enumerate(routes or (), 1):
            report.row(f'{number}.', f'{route.address}:{route.port}   {route.transport.upper()}')

    # TLS

    tls_title = 'TLS servers'
    # RFC 5922: a SIP client verifies the server's certificate against the SIP domain; the SDK
    # verifies it against the server's name (or the TLS name). Not a question for an MSRP relay.
    check_sip_domain_in_certificate = True

    def check_tls(self):
        report = self.report
        if not self.tls_targets:
            report.section('TLS')
            report.note(f'{self.domain} offers no TLS transport')
            return
        report.section(self.tls_title)
        if self.tls_name:
            report.note(f'Certificates are verified for {self.tls_name}')
        for hostname, port in self.tls_targets:
            for address in self.addresses.get(hostname) or ():
                report.subsection(f'{hostname}   {address}:{port}')
                self._check_tls_server(hostname, address, port)

    def _check_tls_server(self, hostname, address, port):
        report = self.report
        # the chain is verified by openssl, the name here: which name to expect depends on the certificate
        command = ['openssl', 's_client', '-connect', f'{address}:{port}', '-servername', self.tls_name or hostname,
                   '-verify_return_error', '-showcerts']
        if self.ca_file:
            command += ['-CAfile', str(self.ca_file)]
        try:
            result = subprocess.run(command, input=b'', capture_output=True, timeout=15)
        except FileNotFoundError:
            report.fail('openssl is not installed')
            return
        except subprocess.TimeoutExpired:
            report.fail('No TLS answer in 15 seconds')
            return
        output = text(result.stdout) + text(result.stderr)
        verify = re.search(r'Verify return code: (\d+) \(([^)]*)\)', output)
        certificate = re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', output, re.S)
        if certificate is None:
            reason = next((line.strip() for line in output.splitlines() if 'error' in line.lower()), 'no certificate received')
            report.fail(f'TLS connection failed: {reason}')
            return
        details = subprocess.run(['openssl', 'x509', '-noout', '-subject', '-issuer', '-enddate', '-ext', 'subjectAltName'],
                                 input=certificate.group(0).encode(), capture_output=True, timeout=10)
        names = []
        for line in text(details.stdout).splitlines():
            line = line.strip()
            if line.startswith('subject='):
                report.row('Subject', line[8:].strip())
                common_name = re.search(r'CN\s*=\s*([^,/]+)', line)
                if common_name and common_name.group(1).strip() not in names:
                    names.append(common_name.group(1).strip())
            elif line.startswith('issuer='):
                report.row('Issuer', line[7:].strip())
            elif line.startswith('notAfter='):
                report.row('Expires', line[9:].strip())
            elif 'DNS:' in line:
                names += [name.strip()[4:] for name in line.split(',') if name.strip().startswith('DNS:') and name.strip()[4:] not in names]
                report.row('Names', ', '.join(name.strip()[4:] for name in line.split(',') if name.strip().startswith('DNS:')))

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
            report.ok(f'Certificate verified for {expected}')
        elif not chain_ok:
            report.fail(f"Certificate not verified: {verify.group(2) if verify else 'unknown error'}")
        else:
            report.fail(f'Certificate valid but not for {expected}')
        if self.check_sip_domain_in_certificate and expected == hostname and hostname != self.domain:
            if matches(self.domain):
                report.ok(f'The certificate also carries the SIP domain {self.domain}')
            else:
                report.note(f'The certificate does not carry the SIP domain {self.domain}: fine for Blink, which verifies the server name {hostname}, '
                            f'but clients that verify the domain itself (RFC 5922) would refuse this server')


MSRP_RELAY_PORT = 2855      # the relay's port when neither the account nor SRV gives one


class RelayInspector(DomainInspector):
    """The NAT traversal servers of a domain, as the SDK finds them: the MSRP relay (chat and file
    transfers: the account's own, else the SRV records _msrps._tcp of the domain, else the domain
    itself on port 2855) with the TLS certificate of every relay, and the STUN servers (audio and
    video: the account's own, else the SRV records _stun._udp of the domain).

    msrp_relay: (host, port, transport) set in the account, or None; stun_servers: [(host, port)];
    parts: which of them to check, 'msrp' and/or 'stun'."""

    tls_title = 'TLS of the MSRP relays'
    check_sip_domain_in_certificate = False     # a relay is verified by its own name

    def __init__(self, domain, report, msrp_relay=None, stun_servers=(), tls_name=None, ca_file=None, parts=('msrp', 'stun')):
        super().__init__(domain, report, tls_name=tls_name, ca_file=ca_file)
        self.msrp_relay = msrp_relay
        self.stun_servers = list(stun_servers or ())
        self.parts = parts

    def check_lookups(self):
        """DNS of the relay and/or the STUN servers. In a green thread."""
        try:
            if 'msrp' in self.parts:
                self.check_msrp_relay()
            if 'stun' in self.parts:
                self.check_stun()
        except Exception as e:
            self.report.fail(f'Unexpected error: {e!r}')

    def check_certificates(self):
        """TLS of the relays (STUN has none)."""
        if 'msrp' in self.parts:
            super().check_certificates()

    def _addresses(self, hosts):
        for host in hosts:
            if host in self.addresses:
                continue
            try:
                addresses = [record.address for record in self._resolve(host, 'A')]
            except Exception as e:
                addresses = []
                self.report.fail(f'A lookup of {host} failed: {e}', count=False)
            self.addresses[host] = addresses
            if addresses:
                self.report.row(f'A {host}', ', '.join(addresses))
            else:
                self.report.fail(f'{host} has no A records')

    def check_msrp_relay(self):
        report = self.report
        report.section('MSRP relay (chat and file transfers)')
        if self.msrp_relay is not None:
            host, port, transport = self.msrp_relay
            port = port or MSRP_RELAY_PORT
            report.row('Set in the account', f'{host}:{port}   {transport.upper()}')
            targets = [(host, port, transport.lower())]
        else:
            name = f'_msrps._tcp.{self.domain}'
            try:
                srvs = self._resolve(name, 'SRV')
            except Exception as e:
                srvs = []
                report.fail(f'SRV lookup of {name} failed: {e}', count=False)
            if srvs:
                targets = []
                lines = []
                for record in sorted(srvs, key=lambda r: (r.priority, -r.weight)):
                    target = str(record.target).rstrip('.')
                    lines.append(f'{target}:{record.port}   (priority {record.priority}, weight {record.weight})')
                    targets.append((target, record.port, 'tls'))
                report.row(f'SRV {name}', '\n'.join(lines))
            else:
                report.note(f'No SRV records for {name}: {self.domain} itself, on port {MSRP_RELAY_PORT}')
                targets = [(self.domain, MSRP_RELAY_PORT, 'tls')]
        self._addresses(host for host, port, transport in targets)
        if not any(self.addresses.get(host) for host, port, transport in targets):
            report.fail('No MSRP relay address found')
        for host, port, transport in targets:
            if transport == 'tls' and (host, port) not in self.tls_targets:
                self.tls_targets.append((host, port))

    def check_stun(self):
        report = self.report
        report.section('STUN servers (audio and video)')
        if self.stun_servers:
            report.row('Set in the account', '\n'.join(f'{host}:{port}' for host, port in self.stun_servers))
            hosts = [host for host, port in self.stun_servers]
        else:
            name = f'_stun._udp.{self.domain}'
            try:
                srvs = self._resolve(name, 'SRV')
            except Exception as e:
                srvs = []
                report.fail(f'SRV lookup of {name} failed: {e}', count=False)
            if not srvs:
                report.note(f'No SRV records for {name}: no STUN servers, ICE has only the local candidates')
                return
            hosts = []
            lines = []
            for record in sorted(srvs, key=lambda r: (r.priority, -r.weight)):
                target = str(record.target).rstrip('.')
                lines.append(f'{target}:{record.port}   (priority {record.priority}, weight {record.weight})')
                if target not in hosts:
                    hosts.append(target)
            report.row(f'SRV {name}', '\n'.join(lines))
        self._addresses(hosts)
