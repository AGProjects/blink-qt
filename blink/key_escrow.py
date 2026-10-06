# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The PGP key escrow carried on our own XCAP contact.

Ported from Blink for macOS (KeyEscrow.py); the rules are the same, so either
client opens the other's escrow and refuses the same writes. What differs is
where Qt keeps its keys (<keys_directory>/private/<account>.{privkey,pubkey})
and that account.sms.{private,public}_key are path settings here. The
scheduling (restore on reload, repair once per session, the generate prompt
waiting for the answer) is blink.messages.KeyEscrowManager.

Sylk mobile escrows the account keypair on the user's own ("self") contact in
the XCAP addressbook, as a JSON ``keys`` attribute::

    {"private_key": <symmetrically encrypted with the ACCOUNT PASSWORD>,
     "public_key":  <armored, in clear>,
     "device":      "motorola razr 60 ultra",
     "timestamp":   "2026-07-10T12:44:19.049Z"}

so a device that has no key of its own can adopt the account's existing one
instead of generating a fresh keypair and orphaning every message ever
encrypted to the old one.

Which attribute bag does the escrow live in? Read below.

Every contact in the resource-lists document can carry TWO independent
attribute containers, and each client reads only its own:

    urn:ag-projects:sipsimple:xml:ns:addressbook   written by sylk mobile via
                                                   SylkServer, and now by Blink
                                                   too -- see the namespace note
                                                   in configuration/contact.py
    urn:ag-projects:blink:xml:ns:addressbook       written by Blink until that
                                                   switch; still present in
                                                   every existing document

They are not in sync: `organization` exists in both bags on the same contacts,
holding independent values, and documents will carry the older bag for as long
as those contacts live. Rather than assume, read_self_keys() looks in both and
reports which one answered.

The read is deliberately done against the raw lxml tree rather than through
sipsimple's Contact settings, because XCAPContact.normalize() drops every
attribute that is not a registered SharedSetting -- which is exactly what
hides the escrow from Blink today. Going to the tree also means this module
needs no change to the addressbook schema to see what is there, so it stays
honest about the document as the server actually returns it.
"""

__all__ = ['ADDRESSBOOK_NS', 'SIPSIMPLE_ATTRIBUTES_NS', 'BLINK_ATTRIBUTES_NS', 'ESCROW_NAMESPACE',
           'self_contact_element', 'self_contact_elements', 'self_escrow_records',
           'read_self_keys', 'escrow_write_targets', 'escrow_write_blockers', 'escrow_write_action',
           'escrow_is_missing', 'escrow_record',
           'write_self_keys', 'install_keypair', 'restore_from_own_contact',
           'self_contact_report', 'log_self_contact']

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
BLINK_ATTRIBUTES_NS = 'urn:ag-projects:blink:xml:ns:addressbook'

# Searched in this order; the first bag carrying a `keys` attribute wins.
# sipsimple first, because that is where the mobile's attributes land.
ATTRIBUTE_NAMESPACES = (('sipsimple', SIPSIMPLE_ATTRIBUTES_NS),
                        ('blink', BLINK_ATTRIBUTES_NS))

# Where an escrow written by Blink has to go. Not a preference: the mobile
# reads only this bag, so an escrow written anywhere else is invisible to the
# device that needs it, which is the entire point of writing one.
ESCROW_NAMESPACE = SIPSIMPLE_ATTRIBUTES_NS


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


def _resource_lists_element(account):
    """The lxml root of this account's resource-lists document, or None.

    None covers every "not yet" there is: an account with XCAP disabled, one
    whose root has not been discovered, one whose first fetch has not landed.
    """
    try:
        content = account.xcap_manager.resource_lists.content
    except (AttributeError, ReferenceError):
        return None
    return getattr(content, 'element', None)


def _contact_elements(root):
    return root.iter('{%s}contact' % ADDRESSBOOK_NS)


def _contact_uris(contact_element):
    """Every URI on a contact, percent-decoded.

    The document stores them encoded (``ag%40ag-projects.com``), so comparing
    a raw attribute against account.id never matches.
    """
    for uri_element in contact_element.iter('{%s}uri' % ADDRESSBOOK_NS):
        value = uri_element.get('uri')
        if value:
            yield unquote(value)


def _attributes(contact_element, namespace):
    """The {name: value} of one attribute bag on this contact.

    findall rather than iter on purpose: each of a contact's URIs carries an
    attribute bag of its own, and iter() would fold those into the contact's,
    which is how you end up reporting a `position` attribute on a contact that
    has no such thing.
    """
    attributes = {}
    for container in contact_element.findall('{%s}attributes' % namespace):
        for child in container.findall('{%s}attribute' % namespace):
            name = child.get('name')
            if name is None:
                continue
            attributes[name] = None if child.get('nil') == 'true' else (child.text or '')
    return attributes


def self_contact_elements(account):
    """Every contact element carrying our own account URI.

    Plural on purpose. Nothing stops the document holding our URI on more
    than one contact -- the two clients already add a `uri` entry each for the
    same address -- and an escrow written to one of them is invisible to a
    reader that picks the other. A caller that silently took the first would
    never notice.
    """
    root = _resource_lists_element(account)
    if root is None:
        return []
    account_id = str(account.id).lower()
    return [contact_element for contact_element in _contact_elements(root)
            if any(uri.lower() == account_id for uri in _contact_uris(contact_element))]


def self_contact_element(account):
    """The contact element whose URI is our own account, or None."""
    elements = self_contact_elements(account)
    return elements[0] if elements else None


def shared_accounts(contact_element, account, known_accounts):
    """Other configured accounts whose URI sits on this same contact.

    A contact carries ONE `keys` attribute. If it also carries the URI of
    another of our accounts, both accounts resolve to it as their own
    contact -- and they have different keypairs, so whichever escrows last
    overwrites the other. Detecting that is the difference between a backup
    and a lost key.
    """
    account_id = str(account.id).lower()
    uris = {uri.lower() for uri in _contact_uris(contact_element)}
    return sorted(known for known in known_accounts
                  if known.lower() in uris and known.lower() != account_id)


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


def self_escrow_records(account):
    """[(contact id, namespace label, record)] for every escrow on a contact of ours.

    Several contacts can legitimately carry our URI -- they are all us, so
    they all deserve the same escrow. They can nonetheless disagree, if a
    write reached one and not another, so the records are gathered rather
    than the first one taken.
    """
    found = []
    for contact_element in self_contact_elements(account):
        for label, namespace in ATTRIBUTE_NAMESPACES:
            raw = _attributes(contact_element, namespace).get('keys')
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (TypeError, ValueError) as e:
                BlinkLogger().log_info('Key escrow: the %s `keys` attribute on contact %s of %s is not '
                                       'valid JSON: %s' % (label, contact_element.get('id'), account.id, e))
                continue
            if not isinstance(record, dict):
                BlinkLogger().log_info('Key escrow: the %s `keys` attribute on contact %s of %s is not '
                                       'an object' % (label, contact_element.get('id'), account.id))
                continue
            found.append((contact_element.get('id'), label, record))
    return found


def read_self_keys(account):
    """The newest escrow record on any contact of ours, or None.

    Returns the parsed JSON with two added keys: 'namespace', naming the bag
    it came from, and 'contact_id', the contact it was found on.

    EVERY contact carrying our URI is searched, and the newest record wins.
    Duplicates genuinely occur -- one account here sits on two "AG Projects"
    contacts -- so a first-match reader would report an escrow as absent
    merely because it sat on the second one, which is the worst possible
    wrong answer: it reads as "no backup exists" exactly when somebody needs
    one. Between two escrows for the same account the newest is the right
    one, since a re-escrow only ever happens to supersede.

    Never raises: no document, no self contact, no attribute and unparseable
    JSON all come back as None, because every caller is on a path that must
    survive not finding anything.
    """
    records = self_escrow_records(account)
    if not records:
        return None
    contact_id, label, record = max(records, key=lambda item: item[2].get('timestamp') or '')
    record = dict(record)
    record['namespace'] = label
    record['contact_id'] = contact_id
    return record


def escrow_write_targets(account):
    """Every contact an escrow for this account should be written to.

    All of them, not one. Several contacts carrying the same account URI is a
    valid, non-conflictual configuration: they are all us, so they all take
    the same key. Writing every one of them keeps the document consistent and
    makes a read correct whichever contact it happens to pick first.
    """
    return [element.get('id') for element in self_contact_elements(account)]


def escrow_write_blockers(account, accounts=None):
    """Why an escrow must not be written for this account right now.

    A list of reasons; empty means a write is safe to attempt. This exists
    before write_self_keys does, on purpose -- the guard is the part that
    decides whether the feature backs a key up or destroys one, so it is
    written and proven while everything is still read-only.

    The one genuine conflict is a contact carrying the URI of a DIFFERENT
    account of ours: one contact holds one `keys` attribute, and two accounts
    have two keypairs, so the second writer would destroy the first's backup.
    The resolution is to split that contact, not to pick a surviving key --
    choosing a key only chooses which account is left without a backup.
    """
    accounts = known_accounts() if accounts is None else accounts
    blockers = []

    if not _has_file(account.sms.private_key):
        blockers.append('this device holds no private key for %s, so there is nothing to escrow'
                        % account.id)

    password = getattr(getattr(account, 'auth', None), 'password', None)
    if not (password or '').strip():
        blockers.append('no account password available to encrypt the escrow with')

    if _resource_lists_element(account) is None:
        blockers.append('the addressbook has not been fetched from the server yet')
        return blockers

    elements = self_contact_elements(account)
    if not elements:
        blockers.append('no contact carries the URI %s, so there is nowhere to write' % account.id)
        return blockers

    for contact_element in elements:
        shared = shared_accounts(contact_element, account, accounts)
        if shared:
            blockers.append('contact %s also carries %s; one contact holds one `keys` attribute, so '
                            'split it -- move %s onto its own contact -- rather than choosing which '
                            'key survives'
                            % (contact_element.get('id'), ' and '.join(shared), ' and '.join(shared)))

    for contact_id, _label, record in self_escrow_records(account):
        owner = _escrow_owner(record.get('public_key'), accounts)
        if owner and owner.lower() != str(account.id).lower():
            blockers.append('the escrow already on contact %s is the key of %s; overwriting it '
                            'would destroy that account\'s only backup' % (contact_id, owner))
    return blockers


def escrow_write_action(account, force=False, accounts=None):
    """(would it do anything, why not) for a write right now.

    Two kinds of no. escrow_write_blockers holds the refusals that force can
    never override, because they mean the write would destroy somebody's key.
    The two decided here are softer and overridden by force:

      * the server already carries this key, so a write is pointless;
      * the server carries a key this device does NOT hold. That is what a
        fresh profile looks like after Blink has been talked into generating
        a new keypair: the escrow is the account's real, older key, and
        quietly replacing it with a key minted five minutes ago would burn
        the only copy of the one every existing message was encrypted to.
        Replacing it has to be a deliberate act, not the default.
    """
    blockers = escrow_write_blockers(account, accounts)
    if blockers:
        return False, blockers[0]
    if force:
        return True, None

    try:
        with open(_path(account.sms.public_key), 'rb') as fd:
            ours = fd.read().decode('utf-8', 'replace').replace('\r', '').strip()
    except (IOError, OSError, TypeError) as e:
        return False, 'this device\'s public key could not be read (%s).' % e

    records = self_escrow_records(account)
    foreign = [(contact_id, record) for contact_id, _label, record in records
               if (record.get('public_key') or '').replace('\r', '').strip() != ours]
    if foreign:
        contact_id, record = foreign[0]
        return False, ('contact %s carries an escrow for a key this device does not hold, written '
                       '%s by "%s". Replacing it would leave that key with no backup anywhere, and '
                       'every message encrypted to it unreadable.'
                       % (contact_id, record.get('timestamp', '?'), record.get('device', '?')))

    current = set(contact_id for contact_id, _label, record in records
                  if (record.get('public_key') or '').replace('\r', '').strip() == ours)
    if all(target in current for target in escrow_write_targets(account)):
        return False, 'the key of this account is already saved on the server.'
    return True, None


def escrow_is_missing(account):
    """True when this account has a key but nothing on the server carries it.

    The one state safe to repair without asking: there is no escrow at all,
    so writing one cannot displace anybody's key. Every other difference
    between the local key and an escrow is a decision, not a repair.
    """
    if not _has_file(account.sms.private_key):
        return False
    return not self_escrow_records(account)


def escrow_record(account):
    """Build the escrow record for this device's keypair, or raise.

    The private key is encrypted symmetrically with the ACCOUNT PASSWORD --
    the same secret sylk mobile uses, which is what makes the two clients able
    to open each other's escrows. The public key, the device label and the
    timestamp travel in clear so any client can see whose key it is, which
    device wrote it and when, without holding the password.

    The timestamp is deliberately shaped like JavaScript's toISOString(), so
    it sorts lexicographically against the ones mobile writes -- read_self_keys
    picks the newest by string comparison.
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


def write_self_keys(account, force=False, accounts=None):
    """Escrow this device's keypair onto every contact carrying our URI.

    Returns (contact ids written, reason); an empty list and a reason means
    nothing was written and says why.

    Writes to every target rather than one, because several contacts carrying
    the same account URI is a valid configuration and they are all us.
    Whether a write should happen at all is escrow_write_action's decision,
    so the menu item and this cannot drift into disagreeing.
    """
    logger = BlinkLogger()

    ok, reason = escrow_write_action(account, force=force, accounts=accounts)
    if not ok:
        logger.log_info('Key escrow: not writing for %s -- %s' % (account.id, reason))
        return [], reason

    targets = escrow_write_targets(account)

    try:
        record = escrow_record(account)
    except Exception as e:
        logger.log_error('Key escrow: could not build an escrow for %s: %s' % (account.id, e))
        return [], str(e)

    payload = json.dumps(record)
    logger.log_info('Key escrow: writing an escrow for %s to %s -- device="%s" timestamp=%s blob=%d chars'
                    % (account.id, ', '.join(targets), record['device'], record['timestamp'],
                       len(record['private_key'])))

    from sipsimple.addressbook import AddressbookManager

    written = []
    manager = account.xcap_manager
    manager.start_transaction()
    try:
        for contact_id in targets:
            try:
                contact = AddressbookManager().get_contact(contact_id)
            except KeyError:
                logger.log_info('Key escrow: contact %s of %s is in the document but not loaded yet, skipped'
                                % (contact_id, account.id))
                continue
            manager.update_contact(contact.__toxcap__(), {'keys': payload})
            written.append(contact_id)
    finally:
        # Commit whatever was queued even if one target could not be resolved:
        # leaving a transaction open wedges every later addressbook change.
        manager.commit_transaction()

    logger.log_info('Key escrow: queued an escrow for %s on %d contact(s): %s'
                    % (account.id, len(written), ', '.join(written)))
    return written, None


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


def restore_from_own_contact(account):
    """Adopt the keypair escrowed on our own contact. Returns (ok, reason).

    Only where there is nothing to lose: an account that already holds a
    private key is left alone, because choosing between a local key and a
    different escrowed one decides which messages stay readable, and that is
    the user's call rather than a side effect of an addressbook reload.

    Every failure is a reason string, never an exception -- the caller is a
    notification handler and a sign-in must not fail because a backup did.
    """
    logger = BlinkLogger()

    if _has_file(account.sms.private_key):
        return False, 'this device already holds a private key for %s' % account.id

    record = read_self_keys(account)
    if record is None:
        return False, 'no escrow on any contact of %s' % account.id

    private_key_blob = record.get('private_key') or ''
    public_key = record.get('public_key') or ''
    if not private_key_blob or not public_key:
        return False, 'the escrow on contact %s is incomplete' % record.get('contact_id')

    password = (getattr(getattr(account, 'auth', None), 'password', None) or '').strip()
    if not password:
        return False, 'no account password available to open the escrow with'

    pgpy = _pgpy()
    if pgpy is None:
        return False, 'pgpy is not importable, so the escrow cannot be opened'

    try:
        decrypted = pgpy.PGPMessage.from_blob(private_key_blob.encode()).decrypt(password)
    except Exception as e:
        # Do NOT call this a wrong password. Signing in proved the password is
        # right for the ACCOUNT; what it cannot prove is that it is the one
        # the escrow was encrypted WITH. The escrow is written once and never
        # refreshed, so a password changed afterwards -- on the web, on another
        # client -- leaves a blob that fails exactly like a corrupt one.
        return False, ('the escrow on contact %s (written %s by "%s") would not decrypt. Either the '
                       'account password changed after that date, or the stored blob is damaged. '
                       'openpgp said: %s'
                       % (record.get('contact_id'), record.get('timestamp', '?'),
                          record.get('device', '?'), e))

    private_key = decrypted.message
    if isinstance(private_key, (bytes, bytearray)):
        private_key = private_key.decode('utf-8', 'replace')

    try:
        install_keypair(account, private_key, public_key)
    except Exception as e:
        return False, 'the escrow decrypted but could not be installed: %s' % e

    logger.log_info('Key escrow: restored the keypair of %s from the escrow on contact %s, '
                    'written %s by "%s"'
                    % (account.id, record.get('contact_id'), record.get('timestamp', '?'),
                       record.get('device', '?')))
    return True, None


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


def _describe_local_keypair(account):
    """What this device holds for the account: the thing an escrow would carry."""
    private_path = _path(account.sms.private_key)
    if not private_path:
        return 'no private key configured'
    if not os.path.exists(private_path):
        return 'private key configured at %s, but that file is missing' % private_path

    public_path = _path(account.sms.public_key)
    if not public_path:
        public = 'no public key configured'
    elif not os.path.exists(public_path):
        public = 'public key missing at %s' % public_path
    else:
        public = 'public key present'
    return '%s, %s (%s)' % (_describe_key_file(private_path), public, private_path)


def _describe_public_key(account, public_key):
    """How the escrowed public key relates to the one this device holds."""
    normalized = (public_key or '').replace('\r', '').strip()
    if not normalized:
        return 'MISSING'
    if 'BEGIN PGP PUBLIC KEY' not in normalized:
        return 'not-a-public-key'
    path = _path(account.sms.public_key)
    if not path or not os.path.exists(path):
        return 'present, no local key to compare against'
    try:
        with open(path, 'rb') as fd:
            local = fd.read().decode().replace('\r', '').strip()
    except (IOError, OSError, UnicodeDecodeError) as e:
        return 'present, local key unreadable (%s)' % e
    return 'matches this device' if local == normalized else 'DIFFERENT from this device'


def self_contact_report(account, accounts=None):
    """The lines describing what our own contact carries. Pure, never raises.

    Building the report before logging it is what makes the caller able to
    tell whether anything actually changed: XCAPManagerDidReloadData fires
    several times per sign-in, and six identical lines per account per reload
    buries the one reload where something moved.
    """
    accounts = known_accounts() if accounts is None else accounts
    lines = []

    root = _resource_lists_element(account)
    if root is None:
        return lines                        # nothing fetched yet; not worth a line

    elements = self_contact_elements(account)
    if not elements:
        total = sum(1 for _ in _contact_elements(root))
        lines.append('no contact matching our own URI %s among %d XCAP contacts'
                     % (account.id, total))
        return lines

    contact_element = elements[0]
    if len(elements) > 1:
        lines.append('%s is on %d contacts (%s) -- all of them are us, so an escrow goes to every '
                     'one; a read takes the newest'
                     % (account.id, len(elements),
                        ', '.join(element.get('id') for element in elements)))

    bags = ' '.join('%s=[%s]' % (label, ', '.join(sorted(_attributes(contact_element, namespace))) or '-')
                    for label, namespace in ATTRIBUTE_NAMESPACES)
    lines.append('own contact of %s is id=%s name="%s" uris=[%s] attributes %s'
                 % (account.id, contact_element.get('id'),
                    contact_element.findtext('{%s}name' % ADDRESSBOOK_NS) or '',
                    ', '.join(_contact_uris(contact_element)), bags))

    shared = shared_accounts(contact_element, account, accounts)
    if shared:
        lines.append('WARNING contact %s also carries the URI of %s. One contact holds ONE `keys` '
                     'attribute but these accounts have different keypairs, so an escrow written '
                     'for either would overwrite the other. Split the contact -- move %s onto its '
                     'own -- rather than choosing which key survives.'
                     % (contact_element.get('id'), ' and '.join(shared), ' and '.join(shared)))

    lines.append('this device holds for %s: %s' % (account.id, _describe_local_keypair(account)))

    record = read_self_keys(account)
    if record is None:
        lines.append('%s has no key escrow on any of its contacts -- one would be written to %s, %s'
                     % (account.id, ', '.join(escrow_write_targets(account)), ESCROW_NAMESPACE))
    else:
        private_key = record.get('private_key') or ''
        found_on = record.get('contact_id')
        lines.append('contact %s of %s carries an escrow in the %s bag -- device="%s" written=%s '
                     'blob=%d chars armor=%s public_key %s'
                     % (found_on, account.id, record['namespace'], record.get('device', '?'),
                        record.get('timestamp', '?'), len(private_key),
                        'ok' if 'BEGIN PGP MESSAGE' in private_key else 'MISSING',
                        _describe_public_key(account, record.get('public_key'))))
        stale = [contact_id for contact_id, _label, other in self_escrow_records(account)
                 if (other.get('timestamp') or '') != (record.get('timestamp') or '')]
        if stale:
            lines.append('NOTE contacts %s carry an older escrow for %s; a write would bring them '
                         'back into step' % (', '.join(stale), account.id))
        missing = [target for target in escrow_write_targets(account)
                   if target not in [contact_id for contact_id, _l, _r in self_escrow_records(account)]]
        if missing:
            lines.append('NOTE contacts %s carry no escrow at all for %s' % (', '.join(missing), account.id))
        owner = _escrow_owner(record.get('public_key'), accounts)
        if owner and owner.lower() != str(account.id).lower():
            lines.append('WARNING the escrow on contact %s is the key of %s, not of %s -- this '
                         'contact is being shared by two accounts and only one escrow survives'
                         % (found_on, owner, account.id))

    # Ask the same question the menu item asks, through the same function.
    # Reporting only the blockers made this line claim a write "would be
    # permitted" on an account that is already fully escrowed -- disagreeing
    # with the greyed-out menu item about the very same account.
    blockers = escrow_write_blockers(account, accounts)
    if blockers:
        lines.append('an escrow write for %s would be REFUSED -- %d reason(s):'
                     % (account.id, len(blockers)))
        lines.extend('  %s' % blocker for blocker in blockers)
    else:
        would_write, reason = escrow_write_action(account, accounts=accounts)
        if would_write:
            lines.append('an escrow write for %s would be permitted' % account.id)
        else:
            lines.append('an escrow write for %s would do nothing -- %s' % (account.id, reason))
    return lines


# Last report logged per account, so an unchanged one is not repeated.
_last_report = {}


def log_self_contact(account, accounts=None, force=False):
    """Log what our own XCAP contact carries, if it differs from last time."""
    logger = BlinkLogger()
    lines = self_contact_report(account, accounts)
    if not lines:
        return
    key = str(account.id)
    if not force and _last_report.get(key) == lines:
        logger.log_debug('Key escrow: unchanged for %s since the last addressbook reload' % key)
        return
    _last_report[key] = lines
    # A state report, not an event: debug only. A line that says something
    # is wrong -- a shared contact, a refused write -- still reaches the log.
    alarming = any(line.startswith('WARNING') or 'REFUSED' in line for line in lines)
    log = logger.log_info if alarming else logger.log_debug
    for line in lines:
        log('Key escrow: %s' % line)
