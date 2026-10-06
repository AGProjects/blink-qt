"""Phone number normalisation (E.164) shared with Blink for macOS and Sylk Mobile.

Ported from Blink for macOS util.py; keep the function names and the rules the
same so fixes can be carried between the two clients by diffing. The rules
mirror sylk-mobile (utils.js stripTrunkZeroAfterCountryCode,
normalizeAnonymousUri, Call.js replaceLeadingZero) except where noted.

Pure functions, no Qt and no settings access; an account is only read
duck-typed (account.pstn.idd_prefix / prefix / replace_leading_zero /
strip_digits, account.id.domain, account.conference.server_address).
"""

import re


__all__ = ['sip_prefix_pattern', 'strip_addressbook_special_characters', 'check_valid_phone_number',
           'pstn_apply_leading_zero_rule', 'pstn_home_country_code', 'pstn_strip_trunk_zero', 'pstn_dial_username',
           'pstn_e164', 'pstn_uri_spellings', 'pstn_uri_spellings_for_accounts', 'canonical_pstn_uri',
           'same_phone_number', 'normalize_anonymous_uri', 'is_conference_uri', 'ANONYMOUS_URI']


sip_prefix_pattern = re.compile(r"^(sip:|sips:)")

_pstn_addressbook_chars = r"(\(\s?0\s?\)|[-() \/\.])"
_pstn_addressbook_chars_substract_regexp = re.compile(_pstn_addressbook_chars)
_pstn_match_regexp = re.compile(r"^\+?([0-9,\#\*]|%s)+$" % _pstn_addressbook_chars)
_pstn_plus_regexp = re.compile(r"^\+")


def strip_addressbook_special_characters(contact):
    return _pstn_addressbook_chars_substract_regexp.sub("", contact)


def check_valid_phone_number(number):
    number = number.decode() if isinstance(number, bytes) else number
    return bool(_pstn_match_regexp.match(number))


_pstn_national_regexp = re.compile(r'^0\d+$')
_pstn_country_code_regexp = re.compile(r'^\d{1,3}$')

# Country codes whose national numbers KEEP their leading 0 in the
# international form, because there the 0 is part of the number rather than a
# trunk prefix to be dropped:
#
#   39   Italy, and Vatican City which uses Italian numbering
#   378  San Marino, which follows the same convention
#
# Checked against Wikipedia's "Trunk prefix" article rather than assumed --
# Greece is sometimes named alongside these but is a different case: its
# leading 0 was REPLACED by 2 or 6, so a Greek national number does not start
# with 0 at all and none of this applies to it.
#
# Both rules below need this. Dropping the 0 for these countries produces a
# number that is not dialable: +39 06 6982 is the Vatican switchboard, not a
# typo.
_PSTN_TRUNK_ZERO_KEPT = ('39', '378')


def _pstn_username(uri):
    """The local part of a URI/number, with sip: and visual separators gone."""
    if uri is None:
        return ''
    uri = uri.decode() if isinstance(uri, bytes) else str(uri)
    uri = sip_prefix_pattern.sub("", uri.strip())
    if '@' in uri:
        uri = uri.partition('@')[0]
    return uri.strip()


def pstn_apply_leading_zero_rule(username, replace_leading_zero, idd_prefix=None):
    """The "Replace Leading 0" dial rule.

    A numeric local part starting with a SINGLE 0 (06..., not 00...) has that
    0 replaced by the configured prefix: 0612345678 -> 0031612345678.
    Everything else is returned unchanged.

    Mirrors sylk-mobile Call.js, the rules.replaceLeadingZero branch, with one
    correction. Sylk tests the number against a hardcoded '00', which is right
    only for accounts whose international access code IS '00'. On an account
    using, say, '011', a number already in international form ('01131...')
    still starts with a single 0 and Sylk's rule would rewrite it into
    nonsense. Test against the account's own access code as well, so the rule
    fires on national numbers only. Identical behaviour when idd_prefix is
    '00' or unset, which is the normal case.
    """
    if not replace_leading_zero or not username:
        return username
    if not _pstn_national_regexp.match(username):
        return username
    for access_code in ('00', str(idd_prefix).strip() if idd_prefix else ''):
        if access_code and username.startswith(access_code):
            return username

    # Italy and San Marino keep the 0: the rule is "put the country code in
    # front", not "swap the 0 for it". sylk-mobile does not make this
    # distinction and turns 0212345678 into +39212345678 on an Italian
    # account, dropping a digit that belongs to the number -- its trunk-zero
    # repair has the exception and its dial rule does not. Deliberate
    # divergence; mobile needs the same fix.
    country_code = pstn_home_country_code(replace_leading_zero, idd_prefix)
    if country_code in _PSTN_TRUNK_ZERO_KEPT:
        return str(replace_leading_zero) + username

    return str(replace_leading_zero) + username[1:]


def pstn_home_country_code(replace_leading_zero, idd_prefix=None):
    """The account's own country code, derived from "Replace Leading 0".

    The setting is by construction <international access code><country code>
    ('0031'), so peeling the access code off the front leaves '31'. The '+31'
    and bare '0031' spellings are accepted too. None when it cannot be read.
    """
    if not replace_leading_zero:
        return None
    value = str(replace_leading_zero).strip()
    access = str(idd_prefix).strip() if idd_prefix else '00'
    country_code = None
    if access and value.startswith(access):
        country_code = value[len(access):]
    elif value.startswith('+'):
        country_code = value[1:]
    elif value.startswith('00'):
        country_code = value[2:]
    if not country_code or not _pstn_country_code_regexp.match(country_code):
        return None
    return country_code


def pstn_strip_trunk_zero(username, replace_leading_zero, idd_prefix=None):
    """Remove a national trunk 0 left in front of the HOME country code.

    Click-to-dial links get this wrong constantly: a number printed in
    national form with its trunk prefix, given a country code but not stripped
    of the 0 -- tel:+31-023-7993800 -> +310237993800, which is not dialable.

    Deliberately narrow, and every bound is load-bearing:
      - home country code only; there is no country-code table here and
        guessing where a foreign one ends would mangle good numbers
      - only when "Replace Leading 0" is configured
      - never for +39: Italy (and San Marino / Vatican, which share the code)
        keep their leading 0 in E.164 -- +39 06 6982 is the Vatican
      - exactly one 0 is removed, and only from an all-digit local part

    Mirrors sylk-mobile utils.js stripTrunkZeroAfterCountryCode.
    """
    country_code = pstn_home_country_code(replace_leading_zero, idd_prefix)
    if not country_code or country_code in _PSTN_TRUNK_ZERO_KEPT or not username:
        return username
    access = str(idd_prefix).strip() if idd_prefix else '00'
    prefixes = ['+' + country_code]
    for candidate in (access + country_code, '00' + country_code):
        if candidate not in prefixes:
            prefixes.append(candidate)
    for prefix in prefixes:
        if username.startswith(prefix + '0'):
            tail = username[len(prefix) + 1:]
            if tail.isdigit():
                return prefix + tail
    return username


def _pstn_to_e164(username, idd_prefix, replace_leading_zero):
    """One pass of the pipeline, no external-line-prefix handling."""
    username = pstn_strip_trunk_zero(username, replace_leading_zero, idd_prefix)
    username = pstn_apply_leading_zero_rule(username, replace_leading_zero, idd_prefix)

    if username.startswith('+'):
        digits = username[1:]
    else:
        access_codes = []
        if idd_prefix:
            access_codes.append(str(idd_prefix).strip())
        if '00' not in access_codes:
            access_codes.append('00')
        digits = None
        for code in access_codes:
            if code and username.startswith(code) and len(username) > len(code):
                digits = username[len(code):]
                break
        if digits is None:
            return None

    if not digits.isdigit():
        return None
    # Same length floor matchesURI uses for its phone tail match, so we never
    # mint a contact the matcher would not find again.
    if len(digits) < 8:
        return None
    return '+' + digits


def pstn_dial_username(username, idd_prefix=None, prefix=None, strip_digits=None,
                       replace_leading_zero=None):
    """The local part to put on the wire for a dialled phone number.

    The account's dial plan, in the order sylk-mobile applies it at the SIP
    boundary (Call.js), with Blink's two extra steps on the end:

        strip the address book's visual separators
          -> trunk-zero repair          (utils.js stripTrunkZeroAfterCountryCode)
          -> "Replace Leading 0"        (Call.js rules.replaceLeadingZero)
          -> '+' -> idd_prefix          (Call.js rules.replacePlus)
          -> strip_digits               (Blink only)
          -> external line prefix       (Blink only)

    The first three steps are shared with pstn_e164, which runs the same
    pipeline but stops at the canonical '+...' form -- so the number stored on
    a contact and the number put on the wire are derived from one another and
    cannot drift.

    Note the leading-zero rule already yields the international WIRE form
    ('0612345678' -> '0031612345678'), because the setting holds
    <access code><country code>. The '+' rewrite that follows is then a no-op.
    A rule written in the '+31' spelling instead produces '+31612345678', which
    that same rewrite converts -- so both spellings of the setting work.
    """
    username = strip_addressbook_special_characters(username)
    username = pstn_strip_trunk_zero(username, replace_leading_zero, idd_prefix)
    username = pstn_apply_leading_zero_rule(username, replace_leading_zero, idd_prefix)
    if idd_prefix:
        username = _pstn_plus_regexp.sub(str(idd_prefix), username)
    if strip_digits and len(username) > strip_digits:
        username = username[strip_digits:]
    if prefix:
        username = str(prefix) + username
    return username


def pstn_e164(number, account=None, idd_prefix=None, prefix=None, replace_leading_zero=None):
    """Canonical bare +E.164 for a PSTN number, or None if it is not one.

    Bare on purpose: no domain. The whole PSTN rewrite block in format_uri is
    gated on '@' not being in the URI, so a domain-qualified number stored on a
    contact would bypass the account dial plan when dialled from the contact
    list. Blink's own Address Book import stores numbers bare for that reason.

    Pass either an account (settings are read off account.pstn) or the three
    rule values directly.
    """
    if account is not None:
        # Duck-typed rather than an isinstance check against BonjourAccount:
        # it keeps this module free of sipsimple, and the link-local account
        # has no pstn section anyway.
        pstn = getattr(account, 'pstn', None)
        if pstn is not None:
            idd_prefix = getattr(pstn, 'idd_prefix', None) if idd_prefix is None else idd_prefix
            prefix = getattr(pstn, 'prefix', None) if prefix is None else prefix
            if replace_leading_zero is None:
                replace_leading_zero = getattr(pstn, 'replace_leading_zero', None)

    username = _pstn_username(number)
    if not username:
        return None
    if not _pstn_match_regexp.match(username):
        return None

    username = strip_addressbook_special_characters(username)
    if not username:
        return None

    result = _pstn_to_e164(username, idd_prefix, replace_leading_zero)
    if result is not None:
        return result

    # Only now consider the external line prefix ('9' to reach an outside
    # line). Trying the unstripped form first means a real number that merely
    # starts with the same digit is never mangled.
    if prefix:
        prefix = str(prefix).strip()
        if prefix and username.startswith(prefix) and len(username) > len(prefix):
            return _pstn_to_e164(username[len(prefix):], idd_prefix, replace_leading_zero)

    return None


def pstn_uri_spellings(uri, account=None, domain=None):
    """Every spelling a phone number's history could be filed under.

    History is never rewritten -- a stored remote_uri is the URI that was on
    the INVITE, which is a fact -- so a contact holding one spelling of a
    number cannot find rows written under another. That is not a property of
    the database: the panel already queries with every URI a contact has
    (SMSViewController.history_remote_uris), and get_recordings filters the
    same way. It is a property of a contact that knows only one spelling of
    itself. This is what tells it the others.

    Returns the input plus, for a phone number, its canonical E.164 and the
    wire forms this account's dial plan produces -- with and without a domain,
    because a recording filename carries a host and a bare contact URI does
    not. Order is stable and the input always comes first.

    Deliberately includes the form the CURRENT rules produce and the plain
    number as typed: turning "Replace Leading 0" on changes the wire form, so
    calls made before and after the change are filed differently. Both have to
    be found, or enabling a dial rule quietly hides a conversation's past.

    Anything that is not a phone number is returned unchanged: one spelling,
    no guessing.
    """
    if uri is None:
        return []
    text = uri.decode() if isinstance(uri, bytes) else str(uri)
    text = sip_prefix_pattern.sub("", text.strip())
    if not text:
        return []

    spellings = [text]

    def add(value):
        if value and value not in spellings:
            spellings.append(value)

    e164 = pstn_e164(text, account)
    if e164 is None:
        return spellings

    idd_prefix = prefix = strip_digits = replace_leading_zero = None
    if account is not None:
        pstn = getattr(account, 'pstn', None)
        if pstn is not None:
            idd_prefix = getattr(pstn, 'idd_prefix', None)
            prefix = getattr(pstn, 'prefix', None)
            strip_digits = getattr(pstn, 'strip_digits', None)
            replace_leading_zero = getattr(pstn, 'replace_leading_zero', None)
    if domain is None and account is not None:
        try:
            domain = account.id.domain
        except Exception:
            domain = None

    bare = _pstn_username(text)
    forms = [e164, bare]
    forms.append(pstn_dial_username(bare, idd_prefix, prefix, strip_digits,
                                    replace_leading_zero))
    # What the dial plan produced BEFORE "Replace Leading 0" was configured.
    forms.append(pstn_dial_username(bare, idd_prefix, prefix, strip_digits, None))

    # Running the dial plan forwards is not enough when the input is already
    # canonical: pstn_dial_username leaves '+31...' alone on an account with no
    # idd_prefix, so a contact stored in E.164 would never reach the rows
    # written under the national or 00 form. Derive those directly.
    digits = e164[1:]
    access_codes = ['00']
    if idd_prefix and str(idd_prefix).strip() not in access_codes:
        access_codes.insert(0, str(idd_prefix).strip())
    for code in access_codes:
        forms.append(code + digits)

    # The national form, which is what was dialled and logged before the rule
    # existed. Only when the home country is known -- and it being known is
    # exactly what says the trunk prefix is a 0, since "Replace Leading 0" is
    # the rule that replaces one.
    country_code = pstn_home_country_code(replace_leading_zero, idd_prefix)
    if country_code and digits.startswith(country_code) and len(digits) > len(country_code):
        forms.append('0' + digits[len(country_code):])

    for form in forms:
        add(form)
        if domain:
            add('%s@%s' % (form, domain))

    return spellings


ANONYMOUS_URI = 'anonymous@anonymous.invalid'


# Conference bridges seen in the wild, plus whatever the account names.
_CONFERENCE_DOMAIN_PREFIXES = ('conference.', 'videoconference.')
_DEFAULT_CONFERENCE_DOMAIN = 'conference.sip2sip.info'


def is_conference_uri(uri, account=None):
    """Whether this address is a conference room rather than a person.

    A room is not somebody to file in a call log: it is a place several people
    were, its name is a number nobody dials twice, and a contact made from one
    is junk that then replicates to every device.

    Recognised by domain, the way sylk-mobile does it (_abIsConferenceUri):
    the account's own conference server, the default bridge, and anything on a
    'conference.' or 'videoconference.' domain.
    """
    if not uri:
        return False
    text = uri.decode() if isinstance(uri, bytes) else str(uri)
    text = sip_prefix_pattern.sub("", text.strip()).lower()
    if '@' not in text:
        return False
    domain = text.partition('@')[2].partition(':')[0]
    if not domain:
        return False

    if domain == _DEFAULT_CONFERENCE_DOMAIN:
        return True
    if any(domain.startswith(prefix) for prefix in _CONFERENCE_DOMAIN_PREFIXES):
        return True
    try:
        server = str(getattr(account.conference, 'server_address', '') or '').strip().lower()
        if server and domain == server:
            return True
    except Exception:
        pass
    return False


def normalize_anonymous_uri(uri):
    """Collapse a withheld caller onto the one anonymous address.

    A gateway hands out a fresh <random>@guest.<host> for every withheld call,
    so filing them as they arrive breeds a junk contact per call. Mobile
    rewrites them to a single address before anything sees them
    (utils.js normalizeAnonymousUri) and this is the same rule, deliberately
    to the character: '@guest.' or '@anonymous.' anywhere in the address ->
    'anonymous@anonymous.invalid'.

    NOT util.is_anonymous(), which is a wider test -- it also answers True for
    users named 'asterisk' and 'unknown'. Matching mobile matters more than
    matching Blink's own test here: the two clients have to agree on which row
    a withheld call belongs to, and mobile's is the narrower rule.
    """
    if not uri or not isinstance(uri, str):
        return uri
    lowered = uri.lower()
    if '@guest.' in lowered or '@anonymous.' in lowered:
        return ANONYMOUS_URI
    return uri


def pstn_uri_spellings_for_accounts(uri):
    """Every spelling of a number, across every account's dial plan and domain.

    pstn_uri_spellings needs an account, because the wire form depends on that
    account's rules and the stored URI carries that account's domain. But a
    number is not tied to an account: it was dialled over whichever provider
    was selected at the time, and the conversation may later be opened on a
    different one. A contact stored '+31235244040' has to find the rows written
    as '+31235244040@sip1.budgetphone.nl' even when the conversation is sitting
    on the sylk.link account -- which it will be, since a bare number has no
    account of its own.

    So the union over all enabled accounts, each contributing its own rules and
    its own domain. Cheap: a handful of accounts and pure string work.
    """
    spellings = []

    def add(values):
        for value in values:
            if value and value not in spellings:
                spellings.append(value)

    add(pstn_uri_spellings(uri, None))
    try:
        from sipsimple.account import AccountManager, BonjourAccount
        for account in AccountManager().get_accounts():
            if account is BonjourAccount() or not getattr(account, 'enabled', False):
                continue
            add(pstn_uri_spellings(uri, account))
    except Exception:
        pass
    return spellings


def canonical_pstn_uri(uri, account=None):
    """E.164 for a PSTN URI, otherwise the URI unchanged (lowercased aor).

    Use this wherever a remote party is written to history, so that the same
    call recorded live and replayed from the server history produces the same
    remote_uri -- which is what makes the unique index over
    (msgid, local_uri, remote_uri) actually deduplicate.
    """
    if uri is None:
        return ''
    uri = uri.decode() if isinstance(uri, bytes) else str(uri)
    uri = sip_prefix_pattern.sub("", uri.strip())

    # Before anything else: a withheld caller is one party, however many
    # addresses the gateway invents for them.
    anonymous = normalize_anonymous_uri(uri)
    if anonymous != uri:
        return anonymous

    e164 = pstn_e164(uri, account)
    if e164:
        return e164
    return uri.lower()


def same_phone_number(a, b):
    """Whether two strings denote the same phone number.

    The comparison BlinkContact.matchesURI does inline: strip the address book
    separators, drop a leading + on both sides and leading 0s on one, and
    accept a tail match once the number is long enough. Needed wherever
    numbers are compared as strings -- SIPManager.get_recordings filters
    recordings with an exact 'in' test, which a bare E.164 contact URI can
    never satisfy against a user@host recording filename.
    """
    left = strip_addressbook_special_characters(_pstn_username(a)).lstrip('+')
    right = strip_addressbook_special_characters(_pstn_username(b)).lstrip('+')
    if not left or not right:
        return False
    if not left.isdigit() or not right.isdigit():
        return False
    if left == right:
        return True
    left_trimmed = left.lstrip('0')
    right_trimmed = right.lstrip('0')
    if not left_trimmed or not right_trimmed:
        return False
    if len(right_trimmed) > 7 and left.endswith(right_trimmed):
        return True
    if len(left_trimmed) > 7 and right.endswith(left_trimmed):
        return True
    return False
