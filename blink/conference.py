"""What a conference is, in one place.

One to one communication uses SIP MESSAGE and the message pane; a conference uses
sessions (MSRP chat, RTP audio) and the MSRP chat window. An address is a conference
room when:

  - its domain is the Conference server of an account (server.conference_server: set
    from conference.sipBridge by SylkServer discovery, or by hand), which covers
    nearly every case, or
  - its domain once answered a session as a focus (isfocus in the Contact header)
    without being one of those: a foreign conference server. Such domains are
    remembered across restarts.
"""

import json
import os
import threading

from sipsimple.account import AccountManager

from blink.logging import ActivityLog
from blink.resources import ApplicationData


__all__ = ['conference_domains', 'split_uri', 'is_conference_uri', 'remember_focus']


_lock = threading.Lock()
_focus_domains = None   # loaded on first use


def _focus_file():
    return os.path.join(ApplicationData.directory, 'conference_focus_domains.json')


def _load():
    global _focus_domains
    if _focus_domains is None:
        try:
            with open(_focus_file()) as f:
                _focus_domains = {str(domain).lower() for domain in json.load(f)}
        except (OSError, ValueError, TypeError):
            _focus_domains = set()
    return _focus_domains


def split_uri(uri):
    """(user, host) of a URI given as a string, bytes or SIPURI; host lowercased, without port or parameters."""
    if uri is None:
        return None, None
    if isinstance(uri, bytes):
        uri = uri.decode(errors='replace')
    if not isinstance(uri, str):
        user, host = getattr(uri, 'user', None), getattr(uri, 'host', None)
        user = user.decode(errors='replace') if isinstance(user, bytes) else user
        host = host.decode(errors='replace') if isinstance(host, bytes) else host
        return user, (host or '').lower()
    uri = uri.strip()
    if uri.lower().startswith(('sip:', 'sips:')):
        uri = uri.partition(':')[2]
    user, _, host = uri.partition('@')
    return user, host.partition(';')[0].partition(':')[0].lower()


def conference_domains(*accounts):
    """The Conference server domains of the given accounts and of all accounts."""
    domains = set()
    try:
        all_accounts = list(AccountManager().iter_accounts())
    except Exception:
        all_accounts = []
    for account in list(accounts) + all_accounts:
        server = getattr(getattr(account, 'server', None), 'conference_server', None)
        if not server:
            continue
        domains.add(split_uri(str(server) if '@' in str(server) else '@' + str(server))[1])
    domains.discard('')
    return domains


def is_conference_uri(uri, account=None):
    """Whether this address is a conference room (see the module docstring)."""
    user, host = split_uri(uri)
    if not host:
        return False
    if host.startswith('videoconference.'):
        host = host[len('video'):]     # Sylk Mobile's client-local spelling of the same bridge
    if host in conference_domains(*([account] if account is not None else [])):
        return True
    with _lock:
        return host in _load()


def remember_focus(uri, account=None):
    """A session to uri found a focus: remember its domain if no account setting covers it."""
    user, host = split_uri(uri)
    if not host or host in conference_domains(*([account] if account is not None else [])):
        return
    with _lock:
        domains = _load()
        if host in domains:
            return
        domains.add(host)
        try:
            with open(_focus_file(), 'w') as f:
                json.dump(sorted(domains), f)
        except OSError as e:
            ActivityLog().warning(f'[conference] Cannot save the conference domains seen as a focus: {e}')
    ActivityLog().info(f'[conference] {host} is a foreign conference server (a focus outside the Conference server setting): remembered')
