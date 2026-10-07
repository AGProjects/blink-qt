"""Which addressbook group is which, the way Blink for macOS and Sylk Mobile agree on it.

A group's id belongs to whichever client created it (Sylk Mobile mints its
own) and its name is a label the user may change, so neither identifies the
software's groups across clients. The identity is `kind`, a shared attribute
in the group's XCAP attribute bag (lowercase machine words: 'calls', 'tel',
...). Groups made before the attribute existed are found by name, and a group
Blink made before any sync by its reserved id. Same order as macOS
(HistoryManager._find_group, ContactListModel.is_favorites_group).

No Qt and no sipsimple: a group is anything with id, name and kind.
"""

__all__ = ['CALLS', 'TEL', 'BLOCKED', 'CONFERENCE', 'FAVORITES', 'STAMPED_KINDS',
           'group_kind', 'find_group', 'is_group', 'stamp_plan']


class GroupIdentity(object):
    def __init__(self, kind, name, reserved_ids):
        self.kind = kind
        self.name = name
        self.reserved_ids = tuple(reserved_ids)

    def __repr__(self):
        return f'GroupIdentity({self.kind!r}, {self.name!r}, {self.reserved_ids!r})'


CALLS = GroupIdentity('calls', 'Calls', ('_calls',))
TEL = GroupIdentity('tel', 'Tel', ('_tel',))
BLOCKED = GroupIdentity('blocked', 'Blocked', ('_blocked',))
CONFERENCE = GroupIdentity('conference', 'Conference', ('_conference',))
# '_favorites' is what a new one gets, 'favorites' what every account made before the convention carries
FAVORITES = GroupIdentity('favorites', 'Favorites', ('_favorites', 'favorites'))

# the groups whose kind is written onto an existing group (macOS stamp_group_kinds)
STAMPED_KINDS = (CALLS, TEL, BLOCKED, CONFERENCE, FAVORITES)


def _text(value):
    return str(value or '').strip()


def group_kind(group):
    return _text(getattr(group, 'kind', '')).lower()


def find_group(groups, identity):
    """The group for `identity`: by kind first, then by name, then by a reserved id; or None."""
    by_name = by_id = None
    wanted = identity.name.lower()
    for group in groups:
        if group_kind(group) == identity.kind:
            return group
        if by_name is None and _text(getattr(group, 'name', '')).lower() == wanted:
            by_name = group
        if by_id is None and getattr(group, 'id', None) in identity.reserved_ids:
            by_id = group
    return by_name or by_id


def is_group(group, identity):
    """Whether this one group is `identity`, by the same three tests."""
    if group is None:
        return False
    if group_kind(group) == identity.kind:
        return True
    if _text(getattr(group, 'name', '')).lower() == identity.name.lower():
        return True
    return getattr(group, 'id', None) in identity.reserved_ids


def stamp_plan(groups, identities=STAMPED_KINDS):
    """What stamping would do, as [(identity, group, action)]:

    'stamp'    the group has no kind: write identity.kind
    'stamped'  it already carries it
    'foreign'  it carries another kind, written by someone else: never overwritten
    'missing'  there is no such group: nothing is created here
    """
    plan = []
    for identity in identities:
        group = find_group(groups, identity)
        if group is None:
            plan.append((identity, None, 'missing'))
            continue
        current = group_kind(group)
        if not current:
            plan.append((identity, group, 'stamp'))
        elif current == identity.kind:
            plan.append((identity, group, 'stamped'))
        else:
            plan.append((identity, group, 'foreign'))
    return plan
