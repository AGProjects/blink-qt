"""The devices behind a conference-info <user>.

One account can be in a room from several devices at once (a Mac and a Linux box on the same
account). The room tells each device as its own <endpoint>s of the one <user>, and a device in the
room's video room twice over: its own SIP leg (the MSRP chat, participant_id = the device's
+sip.instance) and the leg SylkServer makes for its WebRTC session (entity sylkserver@..., user
agent SylkServer-..., a participant_id of its own). This groups the endpoints of a user into
devices, so each device is a participant of its own:

  - the WebRTC leg names its device by <agp-conf:instance_id> (the +sip.instance the device gave
    the video room on join), the participant_id of the device's chat leg
  - without it (a room that does not tell it yet), by the display-text both legs share, when it is
    one chat leg's only; else, one WebRTC leg and one chat leg left over are the same device
  - a leg that pairs with nothing is a device of its own (a WebRTC-only client, a SIP phone)
"""

import re

from sipsimple.payloads import XMLStringElement
from sipsimple.payloads.conference import ConferenceDocument, Endpoint, EndpointExtension, agp_conf_namespace


__all__ = ('Device', 'devices', 'normalize_id')


# the AG Projects endpoint elements read here, registered with the parser unless something else did
# (or they are silently dropped); should that fail, the devices are told apart by display-text alone
for _attribute in ('participant_id', 'user_agent', 'instance_id'):
    if not hasattr(Endpoint, _attribute):
        try:
            _element = type(''.join(part.capitalize() for part in _attribute.split('_')), (XMLStringElement, EndpointExtension),
                            dict(_xml_tag=_attribute, _xml_namespace=agp_conf_namespace, _xml_document=ConferenceDocument))
            Endpoint.register_extension(_attribute, _element)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('cannot register <agp-conf:%s> with the conference-info parser: %s', _attribute, e)
del _attribute


_gateway_re = re.compile(r'^(sips?:)?sylkserver@', re.I)


def normalize_id(value):
    """A device or participant id as compared: no <>, quotes or urn:uuid:, lower case; None for none."""
    if value is None:
        return None
    value = str(value).strip().strip('"').strip('<>')
    if value.lower().startswith('urn:uuid:'):
        value = value[9:]
    return value.lower() or None


def _text(endpoint, attribute):
    value = getattr(endpoint, attribute, None)
    if value is None:
        return None
    value = getattr(value, 'value', value)
    return str(value).strip() or None


def _display_text(endpoint):
    return _text(endpoint, 'display_text')


def is_gateway(endpoint):
    """The leg SylkServer makes for a WebRTC participant of the room's video room."""
    if _gateway_re.match(str(endpoint.entity or '')):
        return True
    agent = _text(endpoint, 'user_agent') or ''
    return agent.startswith('SylkServer')


class Device(object):
    """The endpoints of one device of a conference-info <user>; iterates them, as a <user> does."""

    def __init__(self, user, endpoints):
        self.user = user
        self.entity = user.entity
        self.endpoints = list(endpoints)
        direct = [endpoint for endpoint in self.endpoints if not is_gateway(endpoint)]
        named = [endpoint for endpoint in direct + self.endpoints if endpoint.display_text is not None]
        self.display_text = named[0].display_text if named else user.display_text
        self.ids = {normalize_id(_text(endpoint, 'participant_id') or endpoint.entity) for endpoint in self.endpoints} - {None}
        self.instance_ids = {normalize_id(_text(endpoint, 'participant_id')) for endpoint in direct} | \
                            {normalize_id(_text(endpoint, 'instance_id')) for endpoint in self.endpoints if is_gateway(endpoint)}
        self.instance_ids.discard(None)
        self.webrtc = any(is_gateway(endpoint) for endpoint in self.endpoints)
        # the participant_ids of its own SIP legs (the MSRP chat): what a REFER ;method=BYE names to end just them
        self.direct_ids = [_text(endpoint, 'participant_id') for endpoint in direct if _text(endpoint, 'participant_id')]
        agents = [_text(endpoint, 'user_agent') for endpoint in direct]
        agents = [agent for agent in agents if agent]
        self.user_agent = agents[0] if agents else None
        self.webrtc_display_name = next((_display_text(endpoint) for endpoint in self.endpoints if is_gateway(endpoint) and _display_text(endpoint)), None)

    def __iter__(self):
        return iter(self.endpoints)

    def __len__(self):
        return len(self.endpoints)

    def __repr__(self):
        return '%s(%s, %r)' % (self.__class__.__name__, self.entity, [str(endpoint.entity) for endpoint in self.endpoints])

    @property
    def label(self):
        """What tells this device from the other ones of its account."""
        if self.user_agent:
            return self.user_agent
        name = _display_text(self.endpoints[0]) if self.endpoints else None
        return name or ('WebRTC' if self.webrtc else None)


def devices(user):
    """The devices of a conference-info <user>, in document order; one (without endpoints) when it tells none."""
    endpoints = [endpoint for endpoint in user if endpoint.entity is not None]
    if not endpoints:
        return [Device(user, [])]
    groups = [[endpoint] for endpoint in endpoints if not is_gateway(endpoint)]
    by_instance = {normalize_id(_text(group[0], 'participant_id')): group for group in groups}
    by_instance.pop(None, None)
    unpaired = []
    for endpoint in (endpoint for endpoint in endpoints if is_gateway(endpoint)):
        group = by_instance.get(normalize_id(_text(endpoint, 'instance_id')))
        if group is not None:
            group.append(endpoint)
        else:
            unpaired.append(endpoint)
    free = [group for group in groups if len(group) == 1]
    left = []
    for endpoint in unpaired:
        name = _display_text(endpoint)
        candidates = [group for group in free if name is not None and _display_text(group[0]) == name]
        if len(candidates) == 1 and sum(1 for other in unpaired if _display_text(other) == name) == 1:
            candidates[0].append(endpoint)
            free.remove(candidates[0])
        else:
            left.append(endpoint)
    if len(left) == 1 and len(free) == 1:
        free[0].append(left.pop())
    groups.extend([endpoint] for endpoint in left)
    return [Device(user, group) for group in groups]
