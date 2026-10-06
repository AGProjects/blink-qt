
"""Version-aware reader for Sylk location-sharing payloads.

Ported from Blink for macOS SylkLocation.py; keep the function names and the
rules the same so fixes can be carried between the two clients by diffing.
The only divergence is maps_url, which links to OpenStreetMap instead of
Apple Maps.

This is the Blink counterpart of ``app/locationEnvelope.js`` in
sylk-mobile and of ``sylk/applications/webrtcgateway/location.py`` on the
server; it implements payload **version 2** of
``application/sylk-location-sharing`` as described in
``docs/messages/sylk-location-sharing-v2.md``.

The one rule the three implementations must agree on:

    Metadata is authoritative from version 2 on. Below that, the content is.

  * **v2** — ``content`` is nothing but the PGP-armoured coordinates (or
    the *empty string* for a coordinate-free signal) and the cleartext
    lifecycle envelope travels in the message ``metadata`` (a CPIM body
    header on the wire, a column in the server journal).
  * **v1** — the whole envelope is a JSON body and the ciphertext is
    nested in it under ``value``.

``location_envelope()`` folds both shapes back into the single v1-shaped
object, splicing the coordinates in right after ``action``, so every
reader above it sees one shape and never learns a version exists.

Everything here is pure Python — no Qt, no sipsimple, no PGP — so it
can be exercised standalone:

    python3 -m unittest tests.test_location
"""

import datetime
import json

__all__ = [
    'LOCATION_CONTENT_TYPE', 'LEGACY_LOCATION_CONTENT_TYPE',
    'NOTABLE_ACTIONS', 'is_notable_action',
    'COORDINATE_ACTIONS', 'SIGNAL_ACTIONS', 'UPDATE_ACTIONS', 'LOCATION_ACTIONS',
    'is_armoured', 'envelope_version', 'location_metadata', 'location_envelope',
    'envelope_session_id', 'envelope_session_and_source',
    'location_payload', 'maps_url', 'storable_envelope',
    'bubble_id', 'session_bubble_ids',
    'system_note', 'ended_label',
    'track_points', 'append_track_point', 'merge_location_bodies',
    'MAX_TRACK_POINTS',
    'one_shot_envelope', 'location_request_envelope', 'REQUEST_LIFETIME_HOURS',
    'envelope_summary', 'row_metadata', 'storage_fields', 'TEARDOWN_ACTIONS', 'ORIGIN_ACTIONS',
]


LOCATION_CONTENT_TYPE = 'application/sylk-location-sharing'
# The pre-location-sharing wire format: a generic metadata envelope whose
# action == 'location'. Still read, never written.
LEGACY_LOCATION_CONTENT_TYPE = 'application/sylk-message-metadata'

PGP_HEADER = '-----BEGIN PGP MESSAGE-----'
PGP_FOOTER = '-----END PGP MESSAGE-----'

# Ticks that carry (encrypted) coordinates and therefore render a map.
COORDINATE_ACTIONS = frozenset((
    'location_once',
    'location_start',
    'location_update',
    'meeting_request',      # value-bearing invite — the meet starts on request
    'meeting_start',
    'meeting_update',
))

# Ticks that carry no coordinates at all. In v2 their content is the empty
# string, which is a perfectly valid message — never a malformed one.
SIGNAL_ACTIONS = frozenset((
    'location_request',
    'meeting_accept',
    'meeting_reject',
    'location_stop',
    'meeting_end',
))

# Ticks that merely move an existing map pin rather than opening a bubble.
UPDATE_ACTIONS = frozenset(('location_update', 'meeting_update'))

# Ticks that open a bubble.
ORIGIN_ACTIONS = COORDINATE_ACTIONS - UPDATE_ACTIONS

# Ticks that are a NEW MESSAGE: they raise the unread badge and move the
# conversation up the contact list by stamping its last-message time.
#
# Narrower than ORIGIN_ACTIONS, and deliberately so. A share is one event
# no matter how long it runs: the start is the message, and the hundreds
# of update ticks that follow are that same message still being true.
# Counting them turned a single share into "26 unread" and kept shoving
# the contact back to the top of the list every few seconds.
#
# meeting_start is excluded for the same reason -- a meet already
# announced itself with meeting_request, and both legs share that id (see
# bubble_id), so meeting_start is a second tick of an event the user has
# already been told about. Teardown signals (location_stop, meeting_end)
# leave a breadcrumb; nothing new was said.
NOTABLE_ACTIONS = frozenset((
    'location_once',
    'location_start',
    'meeting_request',
))


def is_notable_action(payload):
    """Whether a decoded location payload counts as a new message."""
    if not payload:
        return False
    return payload.get('action') in NOTABLE_ACTIONS

# Ticks that end a session.
TEARDOWN_ACTIONS = frozenset(('location_stop', 'meeting_end', 'meeting_reject'))

LOCATION_ACTIONS = COORDINATE_ACTIONS | SIGNAL_ACTIONS

# The legacy action value used by application/sylk-message-metadata.
LEGACY_ACTION = 'location'

# How much of a trail a single share keeps. A share left running for a day
# at one tick every few seconds is thousands of points, and the row is a
# JSON blob in SQLite -- past a certain length the extra points say nothing
# the line already says, so the oldest are dropped.
MAX_TRACK_POINTS = 1000


def _as_text(value):
    """Best-effort decode of a wire body to str. Never raises."""
    if value is None:
        return ''
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode('utf-8')
        except UnicodeDecodeError:
            return bytes(value).decode('utf-8', errors='replace')
    if isinstance(value, str):
        return value
    return ''


def is_armoured(text):
    """True for a PGP-armoured blob (the shape of a v2 content)."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return stripped.startswith(PGP_HEADER) and stripped.endswith(PGP_FOOTER)


def envelope_version(envelope):
    """The envelope's major payload version, parsed leniently.

    ``"2"``, ``"2.0"``, ``"2.1.3"``, ``2`` and ``2.0`` all read as 2. A
    missing, empty or unparseable version reads as None ("no version"),
    which fails the ``>= 2`` test and falls to the v1 path.
    """
    if not isinstance(envelope, dict):
        return None
    raw = envelope.get('version')
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text.split('.')[0].strip())
    except (TypeError, ValueError):
        return None


def location_metadata(raw):
    """Parse a message's metadata side-band into a dict, or None.

    Accepts the JSON string that rides the CPIM ``agp.Metadata`` body
    header / the journal's metadata column, or an already-decoded dict.
    """
    if isinstance(raw, dict):
        return raw
    text = _as_text(raw).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def location_envelope(content, metadata=None, decrypt=None):
    """Return the v1-shaped envelope for a location message, or None.

        if metadata parses as a JSON object AND its version is >= 2:
            envelope    = the metadata
            coordinates = the content        (empty => coordinate-free signal)
        else:
            envelope    = the content parsed as JSON      (v1)
            coordinates = envelope.value

    The ``>= 2`` test, rather than "metadata present", is deliberate: a
    late-v1 sender populated metadata as a *mirror* of the body it was
    also sending, and that mirror must be ignored so a v1 tick behaves
    exactly as it did before v2 existed.
    """
    meta = location_metadata(metadata)
    if meta is not None and (envelope_version(meta) or 0) >= 2:
        envelope = {}
        if 'action' in meta:
            envelope['action'] = meta['action']
        # Splice the coordinates back in immediately after `action`,
        # reproducing the exact object — key order included — a v1 peer
        # would have sent.
        blob = _as_text(content).strip()
        if blob:
            envelope['value'] = blob
        for key, value in meta.items():
            # `value` never travels in metadata: it is cleartext and is
            # relayed as a single-line header. Drop it defensively so a
            # stray key can neither forge nor erase the real coordinates.
            if key in ('action', 'value'):
                continue
            envelope[key] = value
        return envelope or None

    text = _as_text(content).strip()
    if not text:
        return None
    if is_armoured(text):
        # The legacy application/sylk-message-metadata format encrypts the
        # *whole* envelope, not just the coordinates, so it has to be
        # opened before it can be classified. (A v1 location-sharing body
        # is cleartext JSON with the ciphertext nested under `value`, and
        # a v2 body never reaches this branch.)
        if decrypt is None:
            return None
        text = _as_text(decrypt(text)).strip()
        if not text:
            return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def envelope_session_and_source(envelope):
    """The session grouping key of a tick, and which field it came from.

    ``sessionId || messageId`` — that precedence, not the reverse.
    ``messageId`` is no longer sent on ``location_stop``; it survives
    only as a fallback for peers still running the older sender, and a
    reader that checks it first breaks the moment it stops arriving.

    The source is returned alongside because it is the difference between
    a share that groups correctly and one that splits in two: when
    neither field is present the caller falls back to the tick's own SIP
    message id, which silently invents a session per tick. That fallback
    is legitimate for a one-shot (which carries no sessionId, having no
    trail to group) and a bug for anything else, and until this was
    reported nothing said which had happened.
    """
    if not isinstance(envelope, dict):
        return None, 'none'
    for key in ('sessionId', 'messageId'):
        value = envelope.get(key)
        if value:
            return str(value), key
    return None, 'none'


def envelope_session_id(envelope):
    """The session grouping key of a tick, or None."""
    return envelope_session_and_source(envelope)[0]


def maps_url(lat, lng):
    """OpenStreetMap link with a marker at the position (macOS links to Apple Maps)."""
    return 'https://www.openstreetmap.org/?mlat=%.7f&mlon=%.7f#map=16/%.7f/%.7f' % (lat, lng, lat, lng)


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coordinates(envelope, decrypt=None):
    """Decode an envelope's ``value`` into a coordinates dict, or None.

    ``value`` is either an armoured blob (v1/v2 on the wire — decrypted
    through the ``decrypt`` callable), a JSON string, or an already
    decoded dict (a row we persisted in the clear, and the legacy
    metadata format). The plaintext is one of two shapes: bare
    coordinates (top-level ``latitude``) or wrapped with a shared meet
    destination (top-level ``value``).
    """
    value = envelope.get('value')
    if value is None:
        return None

    if isinstance(value, (bytes, bytearray, str)):
        text = _as_text(value).strip()
        if not text:
            return None
        if is_armoured(text):
            if decrypt is None:
                return None
            text = _as_text(decrypt(text)).strip()
            if not text:
                return None
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None

    if not isinstance(value, dict):
        return None

    destination = value.get('destination')
    if value.get('latitude') is not None:
        coords = value                      # bare
    elif isinstance(value.get('value'), dict):
        coords = value['value']             # wrapped: { value, destination }
    else:
        coords = value

    lat = _float_or_none(coords.get('latitude'))
    lng = _float_or_none(coords.get('longitude'))
    if lat is None or lng is None:
        return None

    result = {
        'latitude': lat,
        'longitude': lng,
        'accuracy': _float_or_none(coords.get('accuracy')),
        'timestamp': coords.get('timestamp'),
        'destination': None,
        'maps_url': maps_url(lat, lng),
    }
    if isinstance(destination, dict):
        dest_lat = _float_or_none(destination.get('latitude'))
        dest_lng = _float_or_none(destination.get('longitude'))
        if dest_lat is not None and dest_lng is not None:
            result['destination'] = {'latitude': dest_lat, 'longitude': dest_lng}
    return result


def _legacy_payload(envelope, decrypt):
    """Normalise a pre-v1 application/sylk-message-metadata location tick.

    ``messageId`` is the stable id of the share (our session key) and
    ``metadataId`` is null on the origin tick, set on every follow-up.
    """
    session = envelope.get('messageId') or envelope.get('metadataId')
    if not session:
        return None
    coords = _coordinates(envelope, decrypt)
    if coords is None:
        return None
    is_update = envelope.get('metadataId') is not None
    return {
        'action': 'location_update' if is_update else 'location_start',
        'session_id': str(session),
        'session_source': 'messageId' if envelope.get('messageId') else 'metadataId',
        'is_update': is_update,
        'is_signal': False,
        'is_coordinate': True,
        'one_shot': bool(envelope.get('one_shot')),
        'role': None,
        'reason': None,
        'expires': envelope.get('expires'),
        'request_id': None,
        'device_id': None,
        'version': None,
        'legacy': True,
        'coords': coords,
        'track': track_points(envelope),
        'envelope': envelope,
    }


# Envelope fields that get a column of their own, or are reconstructed on
# read, and so must NOT be duplicated into the stored metadata. Everything
# else in the envelope is kept. See docs/messages/sylk-location-sharing-v2.md,
# "SQL storage": what is stored is a whitelist, not the wire envelope.
METADATA_DERIVABLE_FIELDS = frozenset((
    'action', 'value', 'messageId', 'sessionId', 'timestamp', 'uri',
    'one_shot', 'meeting_request',
))


def row_metadata(stored_metadata, related_action=None, related_msg_id=None):
    """Rebuild a stored row's envelope metadata for reading.

    What is persisted is a whitelist -- only envelope fields with no column
    of their own -- so `action` and `sessionId` are NOT in it. They live in
    the related_action / related_msg_id columns precisely so they can be
    filtered and grouped in SQL without parsing JSON, and they have to be
    put back before the envelope means anything: without `action` a decoder
    sees a version-2 metadata blob describing nothing and returns None.

    A row with no related_action predates these columns (or is not a
    location tick); its metadata is handed back untouched, which is what
    makes an old row decode exactly as it always did.
    """
    if not related_action:
        return stored_metadata

    meta = location_metadata(stored_metadata)
    if meta is None:
        # No stored metadata means the row is v1-shaped: its BODY is the
        # envelope, with only `value` armoured, so it needs no side-band and
        # synthesising one is actively wrong -- a v2-looking metadata blob
        # makes the decoder read the whole v1 envelope as the coordinates.
        # A v2 row always stores at least `version`, which is what keeps the
        # two apart.
        return None

    meta = dict(meta)
    meta['action'] = related_action
    if related_msg_id:
        meta['sessionId'] = related_msg_id
    # The whitelist is only ever written for v2-shaped storage; a row
    # without a version is still one of ours, so say so rather than let it
    # fall through to the v1 branch and be read as a cleartext body.
    meta.setdefault('version', 2)
    return meta


def envelope_summary(content, metadata=None, content_type=None):
    """Classify a location tick WITHOUT opening its coordinates.

    Everything a store path needs -- what the tick is, which share it
    belongs to, whether it carries coordinates at all -- lives in the
    cleartext envelope: v2 sends it as metadata beside an armoured body,
    v1 sends it as a cleartext JSON body with only `value` armoured. So a
    journal replay can file thousands of ticks correctly without a private
    key and without a single decrypt, which is the entire point.

    Returns None when the tick cannot be classified without decrypting --
    the legacy `application/sylk-message-metadata` format armours the whole
    envelope, so it has no cleartext to read. Such a row is still stored
    verbatim; it is simply classified when something renders it.

    `store_metadata` is the whitelist to persist: the envelope minus every
    field that has its own column or is reconstructed on read.
    """
    envelope = location_envelope(content, metadata, decrypt=None)
    if envelope is None:
        return None

    action = envelope.get('action')
    if action == LEGACY_ACTION and content_type != LOCATION_CONTENT_TYPE:
        # A legacy tick that happened to arrive in the clear. Its own
        # decoder reconstructs the rest; there is no session grouping to
        # derive here that _legacy_payload would not contradict.
        return None
    if action not in LOCATION_ACTIONS:
        return None

    session, session_source = envelope_session_and_source(envelope)
    is_coordinate = action in COORDINATE_ACTIONS

    return {
        'action': action,
        'session_id': session,
        'session_source': session_source,
        'is_coordinate': is_coordinate,
        'is_update': action in UPDATE_ACTIONS,
        'is_signal': action in SIGNAL_ACTIONS,
        'is_notable': action in NOTABLE_ACTIONS,
        # 'location' on browsable rows -- origins and one-shots -- so a
        # media browser finds them; None on trail ticks.
        'category': 'location' if (is_coordinate and action not in UPDATE_ACTIONS) else None,
        'store_metadata': dict((key, value) for key, value in envelope.items()
                               if key not in METADATA_DERIVABLE_FIELDS),
        'envelope': envelope,
    }


def storage_fields(content, metadata=None, content_type=LOCATION_CONTENT_TYPE):
    """The columns of a stored location tick, from its cleartext envelope only.

    related_action is the tick's action and related_msg_id its share
    (sessionId, else messageId), so the ticks of one share are filed against
    its origin row and come with it; update ticks are trail rows, left out of
    a page and drawn as the origin's trail. category marks browsable rows
    (origins, one-shots). metadata keeps the version 2 side-band minus what
    has a column (row_metadata puts it back on read); a version 1 tick keeps
    its envelope in the body and stores none. Nothing is decrypted, so the
    journal can file a share without a key. {} when the tick cannot be read
    without decrypting (legacy format) or is not a location message.
    """
    summary = envelope_summary(content, metadata, content_type)
    if summary is None:
        return {}
    fields = {'related_action': summary['action']}
    if summary['session_id']:
        fields['related_msg_id'] = summary['session_id']
    if summary['category']:
        fields['category'] = summary['category']
    side_band = location_metadata(metadata)
    if side_band is not None and (envelope_version(side_band) or 0) >= 2:
        fields['metadata'] = json.dumps(summary['store_metadata'], separators=(',', ':'))
    return fields


def location_payload(content, metadata=None, decrypt=None, content_type=None):
    """Decode a location message into everything the renderer needs.

    Returns None when the message is not a location payload at all (a
    different metadata flavour, an unparseable body, or a coordinate tick
    whose blob we cannot decrypt). A coordinate-free signal — including
    one whose content is the empty string — returns a payload with
    ``is_coordinate`` False and ``coords`` None; that is a valid message,
    not a broken one.
    """
    envelope = location_envelope(content, metadata, decrypt=decrypt)
    if envelope is None:
        return None

    action = envelope.get('action')

    if action == LEGACY_ACTION and content_type != LOCATION_CONTENT_TYPE:
        return _legacy_payload(envelope, decrypt)

    if action not in LOCATION_ACTIONS:
        return None

    is_coordinate = action in COORDINATE_ACTIONS
    coords = _coordinates(envelope, decrypt) if is_coordinate else None
    if is_coordinate and coords is None:
        # A coordinate tick we cannot read is not renderable. Signals, by
        # contrast, legitimately have nothing to decode.
        return None

    session, session_source = envelope_session_and_source(envelope)

    return {
        'action': action,
        'session_id': session,
        'session_source': session_source,
        'is_update': action in UPDATE_ACTIONS,
        'is_signal': action in SIGNAL_ACTIONS,
        'is_coordinate': is_coordinate,
        'one_shot': action == 'location_once',
        'role': envelope.get('role'),
        'reason': envelope.get('reason'),
        'expires': envelope.get('expires'),
        'request_id': envelope.get('requestId'),
        'device_id': envelope.get('deviceId'),
        'version': envelope_version(envelope),
        'legacy': False,
        'coords': coords,
        'track': track_points(envelope),
        'envelope': envelope,
    }


# -- senders ---------------------------------------------------------------
#
# Blink only ever sends the two simplest things in the vocabulary: "here I
# am, once" and "where are you?". Live sharing, meets and their teardown
# ticks are read here but originated on mobile, which has the background
# location machinery to keep a track running.
#
# Both are built as **v1** bodies -- the whole cleartext envelope as the
# JSON content, coordinates nested under `value` -- rather than the v2
# metadata split. v1 is what every reader in the three implementations
# still understands (location_envelope folds it into the same shape), and
# the v2 split exists to keep coordinates encrypted in a header, which is
# not something this path does yet. Emitting the shape we cannot get
# subtly wrong is worth more than emitting the newest one.

# How long an ask stays good for. A day, matching sylk-mobile's
# sendLocationRequest: long enough that someone away from their phone for
# most of a day can still answer it, short enough that a forgotten ask
# does not answer itself a week later.
REQUEST_LIFETIME_HOURS = 24


def _expiry(now, hours=REQUEST_LIFETIME_HOURS):
    """An ISO 8601 instant `hours` from `now`, or None if that cannot be had.

    `now` is passed in rather than read here so this module stays pure --
    it is exercised by the tests with no clock of its own.
    """
    try:
        return (now + datetime.timedelta(hours=hours)).isoformat()
    except Exception:
        return None


def one_shot_envelope(coords, message_id, now=None, hours=REQUEST_LIFETIME_HOURS):
    """The body for "here is where I am", as a JSON string.

    `action` is `location_once` rather than mobile's legacy `location` +
    `one_shot: True` spelling: location_once is the action this reader
    calls a one-shot (see location_payload's `one_shot` key), it is in
    NOTABLE_ACTIONS so the bubble counts as something said, and it needs
    no second field to be understood. A body Blink cannot read back is a
    map bubble that never appears.

    Coordinates go in cleartext. The receiver's `_coordinates` takes an
    already-decoded dict as readily as an armoured blob, and inventing an
    encryption path here that the peer may have no key for would lose the
    message rather than protect it.
    """
    latitude = _float_or_none(coords.get('latitude') if coords else None)
    longitude = _float_or_none(coords.get('longitude') if coords else None)
    if latitude is None or longitude is None:
        return None

    value = {'latitude': latitude, 'longitude': longitude}
    accuracy = _float_or_none(coords.get('accuracy'))
    if accuracy is not None:
        value['accuracy'] = accuracy
    if coords.get('timestamp'):
        value['timestamp'] = str(coords['timestamp'])

    envelope = {
        'action': 'location_once',
        'messageId': str(message_id),
        'value': value,
        'version': '1.0',
    }
    expires = _expiry(now, hours) if now is not None else None
    if expires:
        envelope['expires'] = expires
    return json.dumps(envelope)


def location_request_envelope(message_id, now=None, hours=REQUEST_LIFETIME_HOURS):
    """The body for "please share your location", as a JSON string.

    Coordinate-free by definition -- we are asking, not telling -- so it
    carries no `value` at all. `messageId` doubles as the request key the
    answer points back at with `requestId`, which is how sylk-mobile
    correlates a one-shot answer to the ask that prompted it.
    """
    envelope = {
        'action': 'location_request',
        'messageId': str(message_id),
        'version': '1.0',
    }
    expires = _expiry(now, hours) if now is not None else None
    if expires:
        envelope['expires'] = expires
    return json.dumps(envelope)


MEET_COORDINATE_ACTIONS = frozenset(('meeting_request', 'meeting_start', 'meeting_update'))


def bubble_id(payload, fallback_id=None):
    """The id of the chat bubble (and history row) a tick belongs to.

    A plain live share, a one-shot and every legacy tick map straight
    onto their session id, so the row keeps the same primary key it had
    before v2. A **meet** is the one case where one session carries two
    coordinate tracks — both legs share the `meeting_request` id and are
    told apart by `role` — so the role is appended: Blink's bubble draws
    a single pin, and folding two moving parties onto it would make the
    pin jump between them.
    """
    session = payload.get('session_id') or fallback_id
    if session is None:
        return None
    session = str(session)
    role = payload.get('role')
    if role and payload.get('action') in MEET_COORDINATE_ACTIONS:
        return '%s:%s' % (session, role)
    return session


def session_bubble_ids(payload, fallback_id=None):
    """Every bubble id a teardown signal may have to stamp.

    A `meeting_end` carries no role, so it ends both legs of the meet.
    """
    session = payload.get('session_id') or fallback_id
    if session is None:
        return []
    session = str(session)
    return [session, '%s:inviter' % session, '%s:invited' % session]


def track_points(envelope):
    """The recorded trail carried by a stored location row, oldest first.

    Only rows Blink itself wrote have one: the wire format sends each tick
    on its own and the trail is something the reader accumulates. A row
    written before trails existed simply has no ``track``, and the reader
    starts one from the single position it does have.
    """
    if not isinstance(envelope, dict):
        return []
    raw = envelope.get('track')
    if not isinstance(raw, list):
        return []
    points = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        lat = _float_or_none(item.get('latitude'))
        lng = _float_or_none(item.get('longitude'))
        if lat is None or lng is None:
            continue
        points.append({
            'latitude': lat,
            'longitude': lng,
            'accuracy': _float_or_none(item.get('accuracy')),
            'timestamp': item.get('timestamp'),
        })
    return points[-MAX_TRACK_POINTS:]


def _point_time(value):
    """A sortable value for a trail point's timestamp, or None.

    Tolerant on purpose: points come from the wire, from stored rows and
    from rows written by builds that formatted this differently, and a
    timestamp that will not parse must leave the point where it is rather
    than throw the trail away.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value
    text = _as_text(value).strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    # Naive and aware values cannot be compared, and both shapes occur.
    return parsed.replace(tzinfo=None) if parsed.tzinfo is None else parsed.astimezone(
        datetime.timezone.utc).replace(tzinfo=None)


def append_track_point(track, coords):
    """Add a position to a trail, keeping it in time order.

    A device that has not moved keeps sending its position, and a trail of
    two hundred identical points is a trail of one point drawn two hundred
    times -- it makes the slider useless and the map no more accurate.

    Ordering is by the point's own timestamp rather than by arrival. Two
    callers feed one trail: ticks arriving live, and rows replayed from
    history. While a share is running both happen at once, and a trail that
    trusted arrival order drew a line leaping between the live position and
    each older point as history caught up -- a zigzag across the map. The
    ordered insert costs nothing in the normal case, where each point is
    newer than the last and this appends.
    """
    if not isinstance(track, list) or not isinstance(coords, dict):
        return track or []
    lat = _float_or_none(coords.get('latitude'))
    lng = _float_or_none(coords.get('longitude'))
    if lat is None or lng is None:
        return track
    point = {
        'latitude': lat,
        'longitude': lng,
        'accuracy': _float_or_none(coords.get('accuracy')),
        'timestamp': coords.get('timestamp'),
    }

    when = _point_time(point['timestamp'])
    index = len(track)
    if when is not None:
        # Walk back over anything stamped later. Untimestamped neighbours
        # are left alone: nothing can be said about where they belong.
        while index > 0:
            previous = _point_time(track[index - 1].get('timestamp'))
            if previous is None or previous <= when:
                break
            index -= 1

    neighbour = track[index - 1] if index > 0 else None
    if neighbour is not None and (abs(neighbour['latitude'] - lat) < 1e-7
                                  and abs(neighbour['longitude'] - lng) < 1e-7):
        # same spot: keep the newer timestamp and accuracy, one point
        track[index - 1] = point
        return track
    following = track[index] if index < len(track) else None
    if following is not None and (abs(following['latitude'] - lat) < 1e-7
                                  and abs(following['longitude'] - lng) < 1e-7):
        return track

    track.insert(index, point)
    if len(track) > MAX_TRACK_POINTS:
        del track[:len(track) - MAX_TRACK_POINTS]
    return track


def storable_envelope(payload, track=None):
    """The JSON body Blink persists for a location row.

    A v2 tick is stored the way the server rebuilds it for pushes, as on
    macOS (whose table has no metadata column): the v1-shaped
    envelope with the coordinates spliced back in after ``action``. The
    coordinates are stored **decrypted** — exactly as the pre-existing
    metadata path already did — so a chat reload can rebuild the bubble
    without the private key, and so one parser serves both the wire and
    the row.
    """
    envelope = dict(payload.get('envelope') or {})
    coords = payload.get('coords')
    if coords is None:
        envelope.pop('value', None)
    else:
        value = {
            'latitude': coords['latitude'],
            'longitude': coords['longitude'],
        }
        if coords.get('accuracy') is not None:
            value['accuracy'] = coords['accuracy']
        if coords.get('timestamp') is not None:
            value['timestamp'] = coords['timestamp']
        if coords.get('destination'):
            # Preserve the wrapped shape so the meeting point survives.
            value = {'value': value, 'destination': coords['destination']}
        envelope['value'] = value
    if track:
        # The trail is Blink's own accumulation, not something that came
        # over the wire: storing it is what lets a reload rebuild the whole
        # share instead of just its last known position.
        envelope['track'] = [
            {'latitude': point['latitude'], 'longitude': point['longitude'],
             'accuracy': point.get('accuracy'), 'timestamp': point.get('timestamp')}
            for point in track[-MAX_TRACK_POINTS:]
        ]
    if payload.get('session_id') and not envelope.get('sessionId'):
        # A one-shot has no sessionId on the wire (its session is the
        # envelope id); stamping it keeps the stored rows self-describing.
        envelope['sessionId'] = payload['session_id']
    return json.dumps(envelope)


def _parse_object(body):
    """A stored row's body as a dict, or None if it is not one."""
    text = _as_text(body).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def merge_location_bodies(stored, incoming):
    """Fold a new tick into the row that is already there, keeping the trail.

    Three writers touch a live share's row -- the conversation that is
    rendering it, the copy replicated back from another of our own devices,
    and the journal replay -- and each of them knows only about the tick in
    its own hands. Two of them wrote a single-position envelope, which
    flattened whatever trail the third had accumulated: a share with two
    hundred ticks came back from a restart with one point.

    Merging where the write happens is what fixes that for all three at
    once. The longer of the two trails wins (both writers are accumulating
    the same series, so the longer one has seen more of it) and the
    incoming position is appended to it, which is a no-op when it is
    already the last point.
    """
    incoming_envelope = _parse_object(incoming)
    if incoming_envelope is None:
        return incoming

    stored_envelope = _parse_object(stored)
    stored_track = track_points(stored_envelope) if stored_envelope else []
    if not stored_track and stored_envelope is not None:
        # A row written before trails existed, or the share's own origin:
        # its single position is the beginning of the trail.
        coords = _coordinates(stored_envelope)
        if coords is not None:
            append_track_point(stored_track, coords)

    incoming_track = track_points(incoming_envelope)
    track = incoming_track if len(incoming_track) > len(stored_track) else stored_track

    coords = _coordinates(incoming_envelope)
    if not incoming_track and coords is not None:
        # A writer that knows only its own tick: its position is the next
        # point of the trail. One that brought a trail already has its own
        # position at the end of it, and re-appending the envelope's
        # `value` would put a stale point after the newest one.
        append_track_point(track, coords)

    if not track:
        return incoming

    # The envelope keeps describing one position, and that position is the
    # newest point of the merged trail -- so a reader that knows nothing
    # about trails still sees where the share actually got to.
    newest = track[-1]
    latest = {'latitude': newest['latitude'],
              'longitude': newest['longitude'],
              'accuracy': newest.get('accuracy'),
              'timestamp': newest.get('timestamp'),
              'destination': (coords or {}).get('destination')}
    return storable_envelope({'envelope': incoming_envelope,
                              'coords': latest,
                              'session_id': envelope_session_id(incoming_envelope)},
                             track)


def system_note(payload, sender_name, direction):
    """The chat breadcrumb for a lifecycle event, or None for silent ticks.

    Mirrors the note vocabulary in the spec's *System notes* section.
    Outgoing payloads are this account's own actions replicated from
    another device, so they get the first-person wording.
    """
    action = payload.get('action')
    reason = payload.get('reason') or 'ended'
    incoming = direction != 'outgoing'
    name = sender_name or 'Contact'

    if action == 'location_start':
        return ('\U0001F4CD %s started sharing live location' % name) if incoming \
            else '\U0001F4CD Sharing live location'

    if action == 'location_stop':
        if reason == 'returned':
            return ('\U0001F4CD %s returned' % name) if incoming else '\U0001F4CD You returned'
        if reason == 'expired':
            return ("\U0001F4CD %s's location sharing expired" % name) if incoming \
                else '\U0001F4CD Your location sharing expired'
        return ('\U0001F4CD %s stopped sharing live location' % name) if incoming \
            else '\U0001F4CD You stopped sharing live location'

    if action == 'location_request':
        return ('\U0001F4CD %s asked for your location' % name) if incoming \
            else '\U0001F4CD Location requested'

    if action == 'meeting_request':
        return ('\U0001F4CD Meet-up request by %s' % name) if incoming \
            else '\U0001F4CD Meet-up requested'

    if action == 'meeting_accept':
        return '\U0001F4CD Meet-up started'

    if action == 'meeting_reject':
        return ('\U0001F4CD %s declined your meet-up request' % name) if incoming \
            else '\U0001F4CD Meet-up declined'

    if action == 'meeting_end':
        if reason == 'proximity':
            return '\U0001F389 You met'
        if reason == 'expired':
            return '\U0001F4CD Meet-up expired'
        return '\U0001F4CD Meet-up ended'

    # location_once / meeting_start / the trail ticks: the map bubble is
    # their chat-visible marker, so they get no note of their own.
    return None


def ended_label(payload):
    """The footer text stamped onto a map bubble when its session ends."""
    action = payload.get('action')
    reason = payload.get('reason') or 'ended'

    if action == 'location_stop':
        if reason == 'returned':
            return 'Returned'
        if reason == 'expired':
            return 'Sharing expired'
        return 'Track ended'

    if action == 'meeting_end':
        if reason == 'proximity':
            return 'You met'
        if reason == 'expired':
            return 'Meet-up expired'
        return 'Meet-up ended'

    if action == 'meeting_reject':
        return 'Meet-up declined'

    return None
