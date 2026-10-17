"""The last good DNS answers, kept on disk, used when the name servers do not answer.

Every answer the SIP SIMPLE SDK's resolver (sipsimple.lookup.DNSResolver) gets is saved,
the whole response in wire format, so a saved answer carries its additional records too.
When a question then gets no answer (timeout, or no name server reachable) and an answer
to it was saved before, that answer is returned instead, whatever its age: an old
address of the registrar beats no registration at all. The SDK and the TXT discovery of
SylkServer both resolve through DNSResolver, so both get the fallback.

With a saved answer to fall back on, a question is given at most fallback_wait seconds
before the saved answer is used, instead of the whole lifetime of the lookup: the SDK
gives all the questions of one lookup (NAPTR, SRV, A) 30 seconds together.

Only answers are saved: a name that does not exist (NXDOMAIN) or has no records of the
type asked is not, and a saved answer is never used for those, only when nobody answered.
"""

import base64
import json
import os
import threading
import time

from datetime import datetime, timezone

import dns.exception
import dns.message
import dns.name
import dns.rdataclass
import dns.rdatatype
import dns.resolver

from blink.logging import ActivityLog
from blink.resources import ApplicationData


__all__ = ['install', 'DNSAnswerCache']


class DNSAnswerCache(object):

    fallback_wait = 4.0         # seconds a question gets before a saved answer is used
    save_delay = 2.0            # seconds to coalesce writes of the file
    min_ttl = 60                # seconds a saved answer is valid once used (the SDK caches it that long)

    def __init__(self, path=None):
        self.path = path or os.path.join(ApplicationData.directory, 'dns_cache.json')
        self._lock = threading.Lock()
        self._entries = {}      # 'name|TYPE' -> {'wire': base64, 'saved': ISO time}
        self._dirty = False
        self._timer = None
        self._used = {}         # key -> monotonic time the fallback was last logged
        self._load()

    @staticmethod
    def key(qname, rdtype):
        name = qname.to_text() if isinstance(qname, dns.name.Name) else str(qname)
        name = name.rstrip('.').lower()
        if isinstance(rdtype, str):
            rdtype = dns.rdatatype.from_text(rdtype)
        return f'{name}|{dns.rdatatype.to_text(rdtype)}'

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            ActivityLog().warning(f'[dns] Saved DNS answers in {self.path} cannot be read, starting without them: {e}')
            return
        if isinstance(data, dict):
            self._entries = {key: value for key, value in data.items() if isinstance(value, dict) and 'wire' in value}
        ActivityLog().info(f'[dns] {len(self._entries)} saved DNS answers loaded, used when the name servers do not answer')

    def save(self, key, answer):
        response = getattr(answer, 'response', None)
        if response is None:
            return
        try:
            wire = base64.b64encode(response.to_wire()).decode('ascii')
        except Exception:
            return
        with self._lock:
            current = self._entries.get(key)
            if current is not None and current['wire'] == wire:
                return          # unchanged: no write
            self._entries[key] = {'wire': wire, 'saved': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            self._dirty = True
            if self._timer is None:
                self._timer = threading.Timer(self.save_delay, self._write)
                self._timer.daemon = True
                self._timer.start()

    def _write(self):
        with self._lock:
            self._timer = None
            if not self._dirty:
                return
            data = dict(self._entries)
            self._dirty = False
        temporary = self.path + '.tmp'
        try:
            with open(temporary, 'w') as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(temporary, self.path)
        except Exception as e:
            ActivityLog().warning(f'[dns] Saving DNS answers to {self.path} failed: {e}')

    def has(self, key):
        with self._lock:
            return key in self._entries

    def answer(self, key, qname, rdtype, rdclass):
        """The saved answer to a question as a dns.resolver.Answer, or None."""
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        try:
            response = dns.message.from_wire(base64.b64decode(entry['wire']))
            # a saved answer is used once nobody answers, however old: its TTLs are what it
            # had when saved, raised to min_ttl so the SDK's own cache keeps it a while
            for rrset in response.answer + response.additional:
                rrset.ttl = max(rrset.ttl, self.min_ttl)
            name = response.question[0].name if response.question else (qname if isinstance(qname, dns.name.Name) else dns.name.from_text(str(qname)))
            if isinstance(rdtype, str):
                rdtype = dns.rdatatype.from_text(rdtype)
            if isinstance(rdclass, str):
                rdclass = dns.rdataclass.from_text(rdclass)
            answer = dns.resolver.Answer(name, rdtype, rdclass, response)
        except Exception as e:
            ActivityLog().warning(f'[dns] Saved DNS answer for {key} cannot be used: {e!r}')
            return None
        return answer, entry.get('saved', 'an earlier run')

    def note_used(self, key, saved, why):
        """Log a fallback, once a minute per question (lookups ask the same thing many times)."""
        now = time.monotonic()
        with self._lock:
            if now - self._used.get(key, -3600) < 60:
                return
            self._used[key] = now
        name, _, kind = key.rpartition('|')
        message = f'[dns] DNS lookup {kind} {name}: {why}, using the answer saved at {saved}'
        ActivityLog().warning(message)
        # and in the SIP tab of the logs window, next to the lookups themselves
        from application.notification import NotificationCenter, NotificationData
        NotificationCenter().post_notification('UILogMessage', data=NotificationData(message=f'{datetime.now()}: {message}\n', section='sip'))


_cache = None


def install():
    """Wrap sipsimple.lookup.DNSResolver.resolve with the saved answers. Once, before the SDK starts."""
    global _cache
    if _cache is not None:
        return _cache
    from sipsimple.lookup import DNSResolver
    _cache = cache = DNSAnswerCache()
    original = DNSResolver.resolve
    timeouts = tuple(cls for cls in (dns.resolver.Timeout, getattr(dns.resolver, 'LifetimeTimeout', None), dns.exception.Timeout) if cls is not None)

    def resolve(self, qname, rdtype=dns.rdatatype.A, rdclass=dns.rdataclass.IN, *args, **kw):
        key = DNSAnswerCache.key(qname, rdtype)
        fallback = cache.has(key)
        lifetime = self.lifetime
        if fallback and lifetime is not None and lifetime > cache.fallback_wait:
            # only a short wait when there is something to fall back on; the rest of the
            # lookup's lifetime stays for its other questions
            self.lifetime = cache.fallback_wait
        try:
            answer = original(self, qname, rdtype, rdclass, *args, **kw)
        except timeouts as e:
            why, error = 'no answer in time', e
        except dns.resolver.NoNameservers as e:
            why, error = 'no name server answered', e
        else:
            cache.save(key, answer)
            return answer
        finally:
            if fallback and lifetime is not None and lifetime > cache.fallback_wait:
                # what this question used comes off the lookup's own lifetime
                used = cache.fallback_wait - self.lifetime
                self.lifetime = max(lifetime - used, 0)
        saved = cache.answer(key, qname, rdtype, rdclass)
        if saved is None:
            raise error
        answer, when = saved
        cache.note_used(key, when, why)
        return answer

    DNSResolver.resolve = resolve
    return cache
