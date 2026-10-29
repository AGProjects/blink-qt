# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The PGP key escrow of an account, kept in its SylkServer account storage.

The rules are the ones of Blink for macOS (KeyEscrow.py), so either client opens
the other's escrow and refuses the same writes. What differs is where Qt keeps
its keys (<keys_directory>/private/<account>.{privkey,pubkey}) and that
account.sms.{private,public}_key are path settings here. The scheduling (the
escrow pass, the generate prompt waiting for the answer) is
blink.messages.KeyEscrowManager.

The escrow lets a device that has no key of its own adopt the account's existing
keypair instead of generating a fresh one and orphaning every message ever
encrypted to the old one. It is one object per account, keys/pgp-private in that
account's own storage (blink.key_storage; SylkServer docs/PRIVATE-KEY-STORAGE.md):

    {"private_key": <symmetrically encrypted with the ACCOUNT PASSWORD>,
     "public_key":  <armored, in clear>,
     "device":      "motorola razr 60 ultra",
     "timestamp":   "2026-07-10T12:44:19.049Z"}

The escrow used to be a `keys` attribute on our own XCAP contact. That copy is no
longer read or written: storage is the only escrow. Where the storage keeps an
escrow and our own contact still carries the old attribute, the attribute is
cleared (strip_contact_escrow), so the key stops riding along with every copy of
the addressbook.
"""

__all__ = ['SIPSIMPLE_ATTRIBUTES_NS',
           'read_escrow', 'escrow_write_blockers', 'escrow_write_action', 'escrow_is_missing',
           'escrow_record', 'write_escrow', 'foreign_escrow', 'escrow_password_check',
           'reencrypt_escrow', 'install_keypair', 'restore_from_storage', 'strip_contact_escrow',
           'record_key_provenance', 'key_provenance', 'key_created', 'reconcile_decision']

import datetime
import json
import os

from urllib.parse import unquote


class _Logger(object):
    """ActivityLog under '[pgp]', or nothing where blink is not importable (tests)."""

    @staticmethod
    def _log():
        try:
            from blink.logging import ActivityLog
        except Exception:
            return None
        return ActivityLog()

    def _say(self, level, message):
        log = self._log()
        if log is not None:
            getattr(log, level)('[pgp] %s' % message)

    def log_info(self, message):
        self._say('info', message)

    def log_debug(self, message):
        self._say('debug', message)

    def log_error(self, message):
        self._say('error', message)


def BlinkLogger():
    return _Logger()


def _path(value):
    """A key path setting as a string (an ApplicationDataPath in Qt), or None."""
    if not value:
        return None
    return getattr(value, 'normalized', None) or str(value)


def _has_file(value):
    path = _path(value)
    return bool(path) and os.path.exists(path)


ADDRESSBOOK_NS = 'urn:ag-projects:xml:ns:addressbook'
SIPSIMPLE_ATTRIBUTES_NS = 'urn:ag-projects:sipsimple:xml:ns:addressbook'


def _key_storage():
    """The KeyStorage singleton, or None where blink is not importable (tests)."""
    try:
        from blink.key_storage import KeyStorage
        return KeyStorage()
    except Exception:
        return None


def read_escrow(account):
    """The escrow record the account storage keeps for the account, or None."""
    storage = _key_storage()
    return storage.record(account) if storage is not None else None


def _storage_state(account):
    """What KeyStorage knows of the account storage (None where it is not importable)."""
    storage = _key_storage()
    if storage is None:
        return None
    return storage.state(account) if storage._wanted(account) else 'unsupported'


def _local_public_key(account):
    """This device's armored public key for the account, normalized, or None."""
    try:
        with open(_path(account.sms.public_key), 'rb') as fd:
            return fd.read().decode('utf-8', 'replace').replace('\r', '').strip()
    except (IOError, OSError, TypeError):
        return None


def _normalized(public_key):
    return (public_key or '').replace('\r', '').strip()


def _pgpy():
    """pgpy, or None where it is not installed.

    Imported lazily because this module is also driven by the offline
    harnesses in scripts/, which run on a plain python that need not have it;
    only the paths that actually touch key material require it.
    """
    try:
        import warnings
        with warnings.catch_warnings():
            # pgpy's constants module raises a pile of
            # CryptographyDeprecationWarnings on import (IDEA, CAST5, ...).
            warnings.simplefilter('ignore')
            import pgpy
    except ImportError:
        return None
    return pgpy


def known_accounts():
    """{account id: public key path} for every configured SIP account.

    Imported lazily and degrading to {} so the offline harnesses, which have
    no sipsimple, keep working; they pass their own mapping instead.
    """
    try:
        from sipsimple.account import Account, AccountManager
    except ImportError:
        return {}
    try:
        return dict((str(account.id), _path(account.sms.public_key))
                    for account in AccountManager().get_accounts()
                    if isinstance(account, Account))
    except Exception:
        return {}


def _escrow_owner(public_key, accounts):
    """Which configured account the escrowed public key belongs to, if any."""
    normalized = (public_key or '').replace('\r', '').strip()
    if not normalized:
        return None
    for account_id, path in sorted(accounts.items()):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, 'rb') as fd:
                if fd.read().decode('utf-8', 'replace').replace('\r', '').strip() == normalized:
                    return account_id
        except (IOError, OSError):
            continue
    return None


def _storage_blockers(account, accounts):
    """Why the escrow cannot be written to the account storage now ([] when it can)."""
    state = _storage_state(account)
    if state not in ('present', 'absent'):
        return ['the account storage cannot be written: %s' % {
            None: 'not asked yet', 'pending': 'it has not answered yet', 'failed': 'it could not be read',
            'invalid': 'it keeps something that is not a key escrow', 'unsupported': 'the account has none'}.get(state, state)]
    record = read_escrow(account)
    owner = _escrow_owner((record or {}).get('public_key'), accounts)
    if owner and owner.lower() != str(account.id).lower():
        return ['the escrow in the account storage is the key of %s; overwriting it '
                'would destroy that account\'s only backup' % owner]
    return []


def escrow_write_blockers(account, accounts=None):
    """Why an escrow must not be written for this account right now: a list of reasons,
    empty when a write is safe to attempt. force never overrides these."""
    accounts = known_accounts() if accounts is None else accounts
    blockers = []

    if not _has_file(account.sms.private_key):
        blockers.append('this device holds no private key for %s, so there is nothing to escrow'
                        % account.id)

    password = getattr(getattr(account, 'auth', None), 'password', None)
    if not (password or '').strip():
        blockers.append('no account password available to encrypt the escrow with')

    blockers.extend(_storage_blockers(account, accounts))
    return blockers


def escrow_write_action(account, force=False, accounts=None):
    """(would it do anything, why not) for a write right now.

    Two kinds of no. escrow_write_blockers holds the refusals that force can
    never override. The two decided here are softer and Option-overridable:

      * the storage already keeps this key, so a write is pointless;
      * the storage keeps a key this device does NOT hold. That is what a
        fresh profile looks like after Blink has been talked into generating
        a new keypair: the escrow is the account's real, older key, and
        quietly replacing it would burn the only copy of the one every
        existing message was encrypted to.
    """
    blockers = escrow_write_blockers(account, accounts)
    if blockers:
        return False, blockers[0]
    if force:
        return True, None

    ours = _local_public_key(account)
    if ours is None:
        return False, 'this device\'s public key could not be read.'

    record = read_escrow(account)
    if record is None:
        return True, None
    if _normalized(record.get('public_key')) != ours:
        return False, ('the account storage keeps an escrow for a key this device does not hold, written '
                       '%s by "%s". Replacing it would leave that key with no backup anywhere, and '
                       'every message encrypted to it unreadable. Hold Option to replace it anyway.'
                       % (record.get('timestamp', '?'), record.get('device', '?')))
    return False, 'the key of this account is already saved on the server.'


def escrow_is_missing(account):
    """True when this account has a key and the account storage said it keeps no escrow.

    The one state safe to repair without asking: there is no escrow at all, so
    writing one cannot displace anybody's key. Every other difference between the
    local key and an escrow is a decision, not a repair. A storage that has not
    answered (or failed) is not "missing".
    """
    if not _has_file(account.sms.private_key):
        return False
    return _storage_state(account) == 'absent'


def escrow_record(account):
    """Build the escrow record for this device's keypair, or raise.

    The private key is encrypted symmetrically with the ACCOUNT PASSWORD --
    the same secret sylk mobile uses, which is what makes the two clients able
    to open each other's escrows. The public key, the device label and the
    timestamp travel in clear so any client can see whose key it is, which
    device wrote it and when, without holding the password.

    The timestamp is deliberately shaped like JavaScript's toISOString(), so
    it sorts lexicographically against the ones mobile writes.
    """
    import socket

    pgpy = _pgpy()
    if pgpy is None:
        raise RuntimeError('pgpy is not importable, so no escrow can be encrypted')

    with open(_path(account.sms.private_key), 'rb') as fd:
        private_key = fd.read().decode('utf-8', 'replace').replace('\r', '').strip()
    with open(_path(account.sms.public_key), 'rb') as fd:
        public_key = fd.read().decode('utf-8', 'replace').replace('\r', '').strip()

    password = account.auth.password.strip()
    encrypted = str(pgpy.PGPMessage.new(private_key).encrypt(password))

    # SELF-CHECK before publishing, with the same call the restore path will
    # use. An escrow is written once and then not looked at again for months,
    # so a blob that cannot be decrypted is discovered on the new device that
    # needed it -- at which point nothing can distinguish a bad blob from a
    # changed password. Prove the round trip here instead, where the failure
    # is merely a refusal to upload.
    roundtrip = pgpy.PGPMessage.from_blob(encrypted.encode()).decrypt(password).message
    if isinstance(roundtrip, (bytes, bytearray)):
        roundtrip = roundtrip.decode('utf-8', 'replace')
    if roundtrip.replace('\r', '').strip() != private_key:
        raise ValueError('escrow self-check failed: the encrypted blob did not decrypt back to the '
                         'private key, so it was NOT uploaded')

    return {'private_key': encrypted,
            'public_key': public_key,
            'device': '%s (Blink Qt)' % socket.gethostname().split('.')[0],
            'timestamp': datetime.datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'}


def write_escrow(account, force=False, accounts=None):
    """Escrow this device's keypair into the account storage. Returns (sent, reason);
    sent False and a reason means nothing was written and says why.

    Whether a write should happen at all is escrow_write_action's decision, so the
    menu item and this cannot drift into disagreeing.
    """
    logger = BlinkLogger()

    ok, reason = escrow_write_action(account, force=force, accounts=accounts)
    if not ok:
        logger.log_info('Key escrow: not writing for %s -- %s' % (account.id, reason))
        return False, reason

    try:
        record = escrow_record(account)
    except Exception as e:
        logger.log_error('Key escrow: could not build an escrow for %s: %s' % (account.id, e))
        return False, str(e)

    logger.log_info('Key escrow: writing an escrow for %s to the account storage -- device="%s" timestamp=%s blob=%d chars'
                    % (account.id, record['device'], record['timestamp'], len(record['private_key'])))
    if not _key_storage().store(account, json.dumps(record), 'escrow written by this device'):
        return False, 'the account storage cannot be written now'
    return True, None


def foreign_escrow(account):
    """The escrow record the account storage keeps of a key this device does NOT hold,
    or None (also when this device holds no key or its public key cannot be read)."""
    ours = _local_public_key(account)
    record = read_escrow(account)
    if ours is None or record is None:
        return None
    return record if _normalized(record.get('public_key')) != ours else None


def escrow_password_check(account):
    """(stale, reason): stale is True when the escrow of OUR key does not open with the
    current account password; reason says why nothing can be said (stale False then).

    Only an escrow of the key this device holds is judged. An escrow of another key is
    somebody else's backup, and not being able to open it says nothing about our password.
    """
    pgpy = _pgpy()
    if pgpy is None:
        return False, 'pgpy is not importable'
    password = (getattr(getattr(account, 'auth', None), 'password', None) or '').strip()
    if not password:
        return False, 'no account password available'
    if not _has_file(account.sms.private_key):
        return False, 'this device holds no private key for %s' % account.id
    ours = _local_public_key(account)
    if ours is None:
        return False, 'this device\'s public key could not be read'
    record = read_escrow(account)
    if record is None:
        return False, None
    if _normalized(record.get('public_key')) != ours:
        return False, 'the account storage keeps an escrow for a key this device does not hold'

    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            opened = pgpy.PGPMessage.from_blob((record.get('private_key') or '').encode()).decrypt(password).message
    except Exception:
        opened = None
    return not opened, None


def reencrypt_escrow(account, accounts=None):
    """Write the escrow again, encrypted with the current account password, when the escrow
    of our key does not open with it. The caller decides that the password is good enough to
    encrypt with: the server has accepted it (a registration succeeded with it).

    Returns (sent, reason); nothing to do is (False, None).
    """
    logger = BlinkLogger()
    stale, reason = escrow_password_check(account)
    if reason:
        logger.log_info('Key escrow: not re-encrypting for %s -- %s' % (account.id, reason))
        return False, reason
    if not stale:
        return False, None
    logger.log_info('Key escrow: the escrow of %s does not open with the current account password, '
                    're-encrypting it' % account.id)
    # force only overrides "already saved": the blockers still refuse, and an escrow of a key we
    # do not hold was ruled out above
    sent, reason = write_escrow(account, force=True, accounts=accounts)
    if sent:
        logger.log_info('Key escrow: re-encrypted the escrow of %s with the current account password' % account.id)
    else:
        logger.log_info('Key escrow: could not re-encrypt the escrow of %s -- %s' % (account.id, reason))
    return sent, reason


def keys_directory():
    """Where Blink Qt keeps <account>.privkey / .pubkey, creating it if needed."""
    from application.system import makedirs
    from sipsimple.configuration.settings import SIPSimpleSettings

    path = os.path.join(SIPSimpleSettings().chat.keys_directory.normalized, 'private')
    makedirs(path)
    return path


def _archive_existing_key(path):
    """Move a private key aside instead of overwriting it. Returns the new path.

    Nothing in this feature may destroy a private key. A key that is replaced
    is still the only thing that can read every message encrypted to it, so a
    replacement renames rather than overwrites, and the old file stays next to
    the new one under the fingerprint it belongs to.
    """
    if not os.path.exists(path):
        return None
    suffix = None
    pgpy = _pgpy()
    if pgpy is not None:
        try:
            with open(path, 'rb') as fd:
                key, _ = pgpy.PGPKey.from_blob(fd.read())
            suffix = key.fingerprint.replace(' ', '')
        except Exception:
            suffix = None
    if suffix is None:
        suffix = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    archived = '%s.%s' % (path, suffix)
    if not os.path.exists(archived):
        os.rename(path, archived)
    return archived


def install_keypair(account, private_key, public_key):
    """Adopt a keypair as this account's, keeping any key it replaces.

    Shared by the pincode import panel and by the escrow restore so both write
    the same files and set the same settings; they differ only in where the
    key came from and which secret opened it.
    """
    private_key = (private_key or '').replace('\r', '').strip()
    public_key = (public_key or '').replace('\r', '').strip()
    if 'BEGIN PGP PRIVATE KEY' not in private_key:
        raise ValueError('that is not a PGP private key')
    if 'BEGIN PGP PUBLIC KEY' not in public_key:
        raise ValueError('that is not a PGP public key')

    logger = BlinkLogger()
    path = keys_directory()
    private_key_path = '%s/%s.privkey' % (path, account.id)
    public_key_path = '%s/%s.pubkey' % (path, account.id)

    try:
        with open(private_key_path, 'rb') as fd:
            existing = fd.read().decode('utf-8', 'replace').replace('\r', '').strip()
    except (IOError, OSError):
        existing = None

    if existing and existing != private_key:
        archived = _archive_existing_key(private_key_path)
        logger.log_info('Key escrow: the previous private key of %s was kept as %s'
                        % (account.id, archived))

    with open(private_key_path, 'wb+') as fd:
        fd.write(private_key.encode())
    with open(public_key_path, 'wb+') as fd:
        fd.write(public_key.encode())

    account.sms.private_key = private_key_path
    account.sms.public_key = public_key_path
    account.save()

    logger.log_info('Key escrow: installed the keypair of %s (%s)'
                    % (account.id, _describe_key_file(private_key_path)))
    return private_key_path


def restore_from_storage(account, replace=False):
    """Adopt the keypair escrowed in the account storage. Returns (ok, reason).

    Only where there is nothing to lose: an account that already holds a private
    key is left alone, because choosing between a local key and a different
    escrowed one decides which messages stay readable, and that is the user's call.
    replace=True is that call made (reconcile_decision, or the user): the local key
    is moved aside by install_keypair, never deleted.

    Every failure is a reason string, never an exception -- the caller is a
    notification handler and a sign-in must not fail because a backup did.
    """
    logger = BlinkLogger()

    if not replace and _has_file(account.sms.private_key):
        return False, 'this device already holds a private key for %s' % account.id

    record = read_escrow(account)
    if record is None:
        return False, 'no escrow in the account storage of %s' % account.id

    private_key_blob = record.get('private_key') or ''
    public_key = record.get('public_key') or ''
    if not private_key_blob or not public_key:
        return False, 'the escrow in the account storage is incomplete'

    password = (getattr(getattr(account, 'auth', None), 'password', None) or '').strip()
    if not password:
        return False, 'no account password available to open the escrow with'

    pgpy = _pgpy()
    if pgpy is None:
        return False, 'pgpy is not importable, so the escrow cannot be opened'

    try:
        decrypted = pgpy.PGPMessage.from_blob(private_key_blob.encode()).decrypt(password)
    except Exception as e:
        # Not necessarily a wrong password: signing in proved the password is right
        # for the ACCOUNT, not that it is the one the escrow was encrypted WITH.
        return False, ('the escrow in the account storage (written %s by "%s") would not decrypt. Either the '
                       'account password changed after that date, or the stored blob is damaged. '
                       'openpgp said: %s' % (record.get('timestamp', '?'), record.get('device', '?'), e))

    private_key = decrypted.message
    if isinstance(private_key, (bytes, bytearray)):
        private_key = private_key.decode('utf-8', 'replace')

    try:
        install_keypair(account, private_key, public_key)
    except Exception as e:
        return False, 'the escrow decrypted but could not be installed: %s' % e
    record_key_provenance(account, 'restored')

    logger.log_info('Key escrow: restored the keypair of %s from the account storage, written %s by "%s"'
                    % (account.id, record.get('timestamp', '?'), record.get('device', '?')))
    return True, None


# The escrow formerly kept on our own XCAP contact

def _own_contacts_with_keys(account):
    """The ids of the contacts carrying our own URI that still have a `keys` attribute
    with a value in the sipsimple bag (where every client wrote the escrow)."""
    try:
        root = account.xcap_manager.resource_lists.content.element
    except (AttributeError, ReferenceError):
        return []
    if root is None:
        return []
    account_id = str(account.id).lower()
    found = []
    for contact_element in root.iter('{%s}contact' % ADDRESSBOOK_NS):
        uris = (unquote(element.get('uri') or '') for element in contact_element.iter('{%s}uri' % ADDRESSBOOK_NS))
        if not any(uri.lower() == account_id for uri in uris):
            continue
        for container in contact_element.findall('{%s}attributes' % SIPSIMPLE_ATTRIBUTES_NS):
            if any(child.get('name') == 'keys' and child.get('nil') != 'true' and (child.text or '').strip()
                   for child in container.findall('{%s}attribute' % SIPSIMPLE_ATTRIBUTES_NS)):
                found.append(contact_element.get('id'))
                break
    return found


def strip_contact_escrow(account):
    """Clear the old `keys` attribute on our own contacts, once the account storage keeps an
    escrow. Never while it keeps none: then the old attribute may be the only copy left of a
    key, and it costs nothing to leave it. Returns the contact ids cleared."""
    if _storage_state(account) != 'present' or read_escrow(account) is None:
        return []
    contact_ids = _own_contacts_with_keys(account)
    if not contact_ids:
        return []
    from sipsimple.addressbook import AddressbookManager
    cleared = []
    manager = account.xcap_manager
    manager.start_transaction()
    try:
        for contact_id in contact_ids:
            try:
                contact = AddressbookManager().get_contact(contact_id)
            except KeyError:
                continue
            manager.update_contact(contact.__toxcap__(), {'keys': None})
            cleared.append(contact_id)
    finally:
        # leaving a transaction open wedges every later addressbook change
        manager.commit_transaction()
    if cleared:
        BlinkLogger().log_info('Key escrow: cleared the old escrow on contact(s) %s of %s, the account storage keeps it'
                               % (', '.join(cleared), account.id))
    return cleared


def _describe_key_file(path):
    """"fingerprint X uid Y" for a PGP key file, or why that could not be said.

    pgpy is imported lazily: this module is also driven by the offline
    harnesses, which must keep working on a python that has no pgpy, and a
    missing fingerprint is worth degrading over rather than failing over.
    """
    pgpy = _pgpy()
    if pgpy is None:
        return 'fingerprint unavailable (pgpy not importable)'
    try:
        with open(path, 'rb') as fd:
            key, _ = pgpy.PGPKey.from_blob(fd.read())
    except Exception as e:
        return 'unreadable as a PGP key (%s)' % e
    uids = ', '.join(str(uid.name or uid.email or '') for uid in key.userids) or '-'
    return 'fingerprint %s uid "%s"' % (key.fingerprint, uids)


# Where the local key came from, and what the account storage held when it did
#
# <keys directory>/<account>.keyinfo, written whenever this device installs a key:
#
#   {"fingerprint": ..., "source": "generated" | "restored" | "imported" | "kept",
#    "escrow_known": true | false, "escrow_fingerprint": <fp> | null, "at": <iso>}
#
# escrow_known says whether the storage had answered when the key was installed;
# escrow_fingerprint is the key it kept then (null: none). A key generated while
# the storage had not answered was made blind: a different escrow found later is
# the account's real key, not an older one to be replaced. "kept" records that
# the user chose this device's key over an escrow, so the question is not asked
# again for the same pair.

def _provenance_path(account):
    return '%s/%s.keyinfo' % (keys_directory(), account.id)


def _fingerprint(armored):
    pgpy = _pgpy()
    if pgpy is None or not armored:
        return None
    try:
        key, _ = pgpy.PGPKey.from_blob(armored.encode() if isinstance(armored, str) else armored)
        return str(key.fingerprint).replace(' ', '').upper()
    except Exception:
        return None


def _local_fingerprint(account):
    return _fingerprint(_local_public_key(account))


def record_key_provenance(account, source):
    """Note where the key this device now holds came from, and what the storage held then."""
    fingerprint = _local_fingerprint(account)
    if not fingerprint:
        return
    state = _storage_state(account)
    known = state in ('present', 'absent')
    record = read_escrow(account) if state == 'present' else None
    info = {'fingerprint': fingerprint,
            'source': source,
            'escrow_known': known,
            'escrow_fingerprint': _fingerprint((record or {}).get('public_key')) if known else None,
            'at': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'}
    path = _provenance_path(account)
    try:
        with open(path + '.tmp', 'w') as fd:
            json.dump(info, fd)
        os.replace(path + '.tmp', path)
    except OSError as e:
        BlinkLogger().log_error('Cannot write %s: %s' % (path, e))
        return
    BlinkLogger().log_info('%s: key %s %s, the account storage %s'
                           % (account.id, fingerprint[-16:], source,
                              ('kept key %s' % info['escrow_fingerprint'][-16:]) if info['escrow_fingerprint']
                              else ('kept no key' if known else 'had not answered (%s)' % (state or 'not asked'))))


def key_provenance(account):
    """The provenance of the key this device holds now, or None (unknown, or of another key)."""
    try:
        with open(_provenance_path(account)) as fd:
            info = json.load(fd)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get('fingerprint') != _local_fingerprint(account):
        return None
    return info


def key_created(public_key):
    """When an armored key was created (aware UTC datetime), or None."""
    pgpy = _pgpy()
    if pgpy is None or not public_key:
        return None
    try:
        key, _ = pgpy.PGPKey.from_blob(public_key.encode() if isinstance(public_key, str) else public_key)
        created = key.created
    except Exception:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    return created


def reconcile_decision(account):
    """What to do when this device holds a key and the account storage keeps an escrow of
    another one: (action, reason, details), action one of

      None      nothing to reconcile (same key, no key, no escrow), or already decided
      'adopt'   install the escrowed key, the local one moved aside
      'ask'     the user decides

    The local key is kept only by the user ('ask'). In order:
      1. a key this device generated before the storage answered was made blind:
         the escrow is the account's key -> adopt
      2. a key installed knowing that very escrow (the user kept it) -> already decided
      3. otherwise (a key without provenance, or an escrow written after ours):
         the key created later wins if it is the escrow's -> adopt, else ask
    """
    ours = _local_public_key(account)
    record = read_escrow(account)
    if not _has_file(account.sms.private_key) or ours is None or record is None or not record.get('public_key'):
        return None, None, None
    local_fp = _fingerprint(ours)
    escrow_fp = _fingerprint(record.get('public_key'))
    if not local_fp or not escrow_fp or local_fp == escrow_fp:
        return None, None, None

    local_created = key_created(ours)
    escrow_created = key_created(record.get('public_key'))
    info = key_provenance(account)
    details = {'local_fingerprint': local_fp, 'escrow_fingerprint': escrow_fp,
               'local_created': local_created, 'escrow_created': escrow_created,
               'escrow_timestamp': record.get('timestamp'), 'escrow_device': record.get('device'),
               'provenance': info}

    if info and info.get('source') == 'generated' and not info.get('escrow_known'):
        return 'adopt', 'the key of this device was generated before the account storage had answered', details
    if info and info.get('escrow_known') and info.get('escrow_fingerprint') == escrow_fp:
        return None, 'the key of this device was installed knowing the escrow of key %s' % escrow_fp[-16:], details
    if local_created and escrow_created:
        if escrow_created > local_created:
            return 'adopt', 'the escrowed key is newer (created %s, the key of this device %s)' % (
                escrow_created.strftime('%Y-%m-%d'), local_created.strftime('%Y-%m-%d')), details
        return 'ask', 'the key of this device is newer (created %s, the escrowed key %s)' % (
            local_created.strftime('%Y-%m-%d'), escrow_created.strftime('%Y-%m-%d')), details
    return 'ask', 'the creation date of a key could not be read', details
