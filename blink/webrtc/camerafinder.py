"""Finding network cameras on the local network, and testing their RTSP streams.

  - ONVIF WS-Discovery: a Probe for NetworkVideoTransmitter sent to 239.255.255.250:3702;
    each ProbeMatch gives the camera's device service address (XAddrs) and its scopes (name,
    hardware). Its RTSP address is then asked with the ONVIF SOAP calls GetCapabilities,
    GetProfiles and GetStreamUri, authenticated with a WS-Security UsernameToken (digest) dated in the
    camera's clock (GetSystemDateAndTime), or with HTTP Digest when the camera refuses that.
  - mDNS: the _rtsp._tcp services (python3-zeroconf, when installed).
  - A test: rtspsrc ─► decodebin ─► appsink until the first whole picture (a key frame), or a timeout.
  - Whether a saved camera answers on its RTSP port (not all cameras answer discovery), its MAC
    address (the neighbour table), and whether a host is on one of this computer's networks.

Everything here blocks: it is called from threads (run_in_thread) and answers in the GUI thread.
"""

import base64
import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree

from blink.logging import ActivityLog


__all__ = ['FoundCamera', 'check_online', 'classify_local', 'discover', 'onvif_stream_uri', 'test_stream']


WS_DISCOVERY = ('239.255.255.250', 3702)
DISCOVERY_TIME = 3.0
SOAP_TIMEOUT = 6
TEST_TIMEOUT = 12
ONLINE_TIMEOUT = 2.0
KEY_FRAME_WAIT = 3.0        # seconds of pictures without a key frame after which one is shown anyway


class FoundCamera(object):
    def __init__(self, name, host, how, xaddr=None, url=None, hardware=None):
        self.name = name
        self.host = host
        self.how = how              # 'onvif' or 'mdns'
        self.xaddr = xaddr          # ONVIF device service URL
        self.url = url              # an RTSP address when known (mDNS)
        self.hardware = hardware

    def __repr__(self):
        return f'<FoundCamera {self.name} {self.host} {self.how}>'

    @property
    def title(self):
        parts = [self.name or self.host]
        if self.hardware and self.hardware != self.name:
            parts.append(self.hardware)
        parts.append(f'{self.host}, {"ONVIF" if self.how == "onvif" else "Bonjour"}')
        return ' · '.join(parts)


# Discovery

_PROBE = '''<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{id}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>'''


def _local_name(tag):
    return tag.rsplit('}', 1)[-1]


def _onvif_discover(timeout=DISCOVERY_TIME):
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        sock.sendto(_PROBE.format(id=uuid.uuid4()).encode(), WS_DISCOVERY)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (address, port) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ElementTree.fromstring(data)
            except ElementTree.ParseError:
                continue
            xaddrs, scopes = '', ''
            for element in root.iter():
                if _local_name(element.tag) == 'XAddrs' and element.text:
                    xaddrs = element.text
                elif _local_name(element.tag) == 'Scopes' and element.text:
                    scopes = element.text
            xaddr = next((url for url in xaddrs.split() if address in url), xaddrs.split()[0] if xaddrs.split() else None)
            name = hardware = None
            for scope in scopes.split():
                match = re.match(r'onvif://www\.onvif\.org/(name|hardware)/(.+)', scope)
                if match:
                    value = urllib.request.unquote(match.group(2)).replace('_', ' ')
                    if match.group(1) == 'name':
                        name = value
                    else:
                        hardware = value
            found[address] = FoundCamera(name or hardware or address, address, 'onvif', xaddr=xaddr, hardware=hardware)
    except OSError as e:
        ActivityLog().warning(f'[cameras] ONVIF discovery failed: {e}')
    finally:
        sock.close()
    return list(found.values())


def _mdns_discover(timeout=DISCOVERY_TIME):
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    found = []

    class Listener(object):
        def add_service(self, zeroconf, kind, name):
            info = zeroconf.get_service_info(kind, name, timeout=1500)
            if info is None or not info.addresses:
                return
            host = socket.inet_ntoa(info.addresses[0])
            path = (info.properties or {}).get(b'path', b'').decode(errors='replace')
            path = path if path.startswith('/') else '/' + path if path else '/'
            found.append(FoundCamera(name.split('._rtsp')[0], host, 'mdns', url=f'rtsp://{host}:{info.port}{path}'))

        def remove_service(self, *args):
            pass

        def update_service(self, *args):
            pass

    zeroconf = Zeroconf()
    try:
        ServiceBrowser(zeroconf, '_rtsp._tcp.local.', Listener())
        time.sleep(timeout)
    finally:
        zeroconf.close()
    return found


def discover():
    """The cameras answering ONVIF WS-Discovery or announcing _rtsp._tcp over mDNS (blocks a few seconds)."""
    cameras = _onvif_discover()
    known = {camera.host for camera in cameras}
    cameras += [camera for camera in _mdns_discover() if camera.host not in known]
    ActivityLog().info(f'[cameras] Found on the network: {", ".join(camera.title for camera in cameras) or "none"}')
    return cameras


# Whether a camera is on this computer's networks

def _ip_command():
    return shutil.which('ip') or next((path for path in ('/usr/sbin/ip', '/sbin/ip', '/usr/bin/ip', '/bin/ip') if os.path.exists(path)), None)


def _local_networks():
    """The networks of this computer's interfaces (ip -o addr: inet and inet6 with their prefix)."""
    import ipaddress
    command = _ip_command()
    if command is None:
        return []
    try:
        output = subprocess.run([command, '-o', 'addr', 'show'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    networks = []
    for line in output.splitlines():
        words = line.split()
        if len(words) >= 4 and words[2] in ('inet', 'inet6'):
            try:
                networks.append(ipaddress.ip_interface(words[3].split('%')[0]).network)
            except ValueError:
                continue
    return networks


def classify_local(hosts):
    """{host: whether it is on one of this computer's networks} (blocks: name lookups)."""
    import ipaddress
    networks = _local_networks()
    answer = {}
    for host in hosts:
        if host.lower().rstrip('.').endswith('.local'):
            answer[host] = True         # mDNS: on the link by definition
            continue
        try:
            addresses = [ipaddress.ip_address(host.strip('[]').split('%')[0])]
        except ValueError:
            try:
                addresses = [ipaddress.ip_address(info[4][0].split('%')[0]) for info in socket.getaddrinfo(host, None)]
            except (OSError, ValueError):
                addresses = []
        answer[host] = any(address in network for address in addresses for network in networks if address.version == network.version)
    return answer


# Whether a camera answers

def _rtsp_port(url):
    try:
        parts = urlsplit(url)
        return parts.hostname, parts.port or (322 if parts.scheme.lower() == 'rtsps' else 554)
    except ValueError:
        return None, None


def _mac_address(address):
    """The MAC address of a neighbour (ip neigh, else /proc/net/arp), or None."""
    command = _ip_command()
    if command is not None:
        try:
            output = subprocess.run([command, 'neigh', 'show', 'to', address], capture_output=True, text=True, timeout=3).stdout
            match = re.search(r'lladdr ((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})', output)
            if match:
                return match.group(1).lower()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        with open('/proc/net/arp') as arp:
            for line in arp.read().splitlines()[1:]:
                words = line.split()
                if len(words) >= 4 and words[0] == address and words[3] != '00:00:00:00:00:00':
                    return words[3].lower()
    except OSError:
        pass
    return None


def check_online(url):
    """(online, mac): whether the camera takes a TCP connection on its RTSP port (cameras that discovery does not
    list), and its MAC address (the neighbour entry the connection made), None when not known (blocks)."""
    host, port = _rtsp_port(url)
    if not host:
        return False, None
    try:
        connection = socket.create_connection((host, port), timeout=ONLINE_TIMEOUT)
    except OSError:
        return False, None
    try:
        address = connection.getpeername()[0]
    finally:
        connection.close()
    return True, _mac_address(address)


# ONVIF: the RTSP address of a camera

def _security_header(user, password, offset=0.0):
    """A WS-Security UsernameToken (digest); Created in the camera's time (offset: its clock minus ours), as a
    camera refuses a token whose time is too far from its own."""
    if not user:
        return ''
    nonce = os.urandom(16)
    created = datetime.fromtimestamp(time.time() + offset, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + (password or '').encode()).digest()).decode()
    return f'''<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{user}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
</UsernameToken></Security></s:Header>'''


class _Refused(RuntimeError):
    pass


def _post(url, envelope, user, password):
    """POST to the camera, answering an HTTP Digest (or Basic) challenge with the user and password."""
    handlers = []
    if user:
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, url, user, password or '')
        handlers = [urllib.request.HTTPDigestAuthHandler(passwords), urllib.request.HTTPBasicAuthHandler(passwords)]
    request = urllib.request.Request(url, data=envelope.encode(), headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=SOAP_TIMEOUT) as response:
            return ElementTree.fromstring(response.read())
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors='replace')
        if e.code == 401 or (e.code in (400, 500) and re.search(r'NotAuthorized|not authorized|Sender', text, re.I)):
            raise _Refused(f'the camera refused the user or password (ONVIF, {e.code})')
        raise RuntimeError(f'the camera answered {e.code} {e.reason}')


def _envelope(body, header=''):
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            f'xmlns:tt="http://www.onvif.org/ver10/schema">{header}<s:Body>{body}</s:Body></s:Envelope>')


def _clock_offset(xaddr):
    """The camera's UTC clock minus ours, in seconds (GetSystemDateAndTime needs no authentication); 0 if unknown."""
    try:
        answer = _post(xaddr, _envelope('<tds:GetSystemDateAndTime/>'), None, None)
        utc = _find(answer, 'UTCDateTime')
        date, clock = _find(utc, 'Date'), _find(utc, 'Time')
        value = lambda element, name: int(_find(element, name).text)
        camera = datetime(value(date, 'Year'), value(date, 'Month'), value(date, 'Day'),
                          value(clock, 'Hour'), value(clock, 'Minute'), value(clock, 'Second'), tzinfo=timezone.utc)
        return camera.timestamp() - time.time()
    except Exception:
        return 0.0


def _soap(url, body, user, password, offset=0.0):
    """The call with a WS-Security token in the camera's time (and HTTP Digest if challenged); refused, once more
    with HTTP Digest only: cameras differ in which they take."""
    try:
        return _post(url, _envelope(body, _security_header(user, password, offset)), user, password)
    except _Refused:
        if not user:
            raise
    ActivityLog().info(f'[cameras] ONVIF {url}: WS-Security refused, trying HTTP Digest only')
    return _post(url, _envelope(body), user, password)


def _find(root, name):
    return next((element for element in root.iter() if _local_name(element.tag) == name), None)


def onvif_stream_uri(xaddr, user, password):
    """The RTSP address of the camera's first media profile (blocks; raises RuntimeError with the reason)."""
    try:
        offset = _clock_offset(xaddr) if user else 0.0
        if abs(offset) > 5:
            ActivityLog().info(f'[cameras] ONVIF {xaddr}: the camera clock is {offset:+.0f} s from ours')
        capabilities = _soap(xaddr, '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>', user, password, offset)
        media = _find(capabilities, 'Media')
        media_url = _find(media, 'XAddr').text.strip() if media is not None and _find(media, 'XAddr') is not None else xaddr
        profiles = _soap(media_url, '<trt:GetProfiles/>', user, password, offset)
        profile = next((element for element in profiles.iter() if _local_name(element.tag) == 'Profiles'), None)
        if profile is None or not profile.get('token'):
            raise RuntimeError('the camera has no media profile')
        body = (f'<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>'
                f'</tt:Transport></trt:StreamSetup><trt:ProfileToken>{profile.get("token")}</trt:ProfileToken></trt:GetStreamUri>')
        answer = _soap(media_url, body, user, password, offset)
        uri = _find(answer, 'Uri')
        if uri is None or not uri.text:
            raise RuntimeError('the camera gave no stream address')
        return uri.text.strip()
    except RuntimeError:
        raise
    except (OSError, ElementTree.ParseError, AttributeError) as e:
        raise RuntimeError(f'ONVIF: {e}')


# Testing a stream

def test_stream(url, user, password, timeout=TEST_TIMEOUT):
    """Pull the first whole picture (from a key frame on) of an RTSP stream: (QImage, width, height); raises RuntimeError with the reason."""
    import threading
    from PyQt6.QtGui import QImage
    from blink.webrtc.videoroom import Gst, _make
    from blink.webrtc.extravideo import _video_stream_only
    pipeline = Gst.Pipeline.new('camera-test')
    source = _make('rtspsrc', location=url, latency=200, protocols='tcp')
    if user:
        source.set_property('user-id', user)
        source.set_property('user-pw', password or '')
    decodebin = _make('decodebin')
    convert = _make('videoconvert')
    scale = _make('videoscale')
    sink = _make('appsink', emit_signals=True, max_buffers=1, drop=True, sync=False,
                 caps=Gst.Caps.from_string('video/x-raw,format=BGRx,width=480,height=270,pixel-aspect-ratio=1/1'))
    for element in (source, decodebin, convert, scale, sink):
        pipeline.add(element)
    convert.link(scale)
    scale.link(sink)
    result = {}
    done = threading.Event()

    def key_probe(pad, info):
        # the decoder's input: a buffer that is no delta unit is a key frame, the pictures from it on are whole
        buffer = info.get_buffer()
        if buffer is not None and not buffer.has_flags(Gst.BufferFlags.DELTA_UNIT):
            result['key'] = True
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK

    def element_added(bin, sub_bin, element):
        factory = element.get_factory()
        klass = factory.get_metadata('klass') if factory is not None else ''
        if 'Decoder' in klass and 'Video' in klass:
            if element.find_property('output-corrupt') is not None:
                element.set_property('output-corrupt', False)      # no gray pictures before the key frame (libav)
            pad = element.get_static_pad('sink')
            if pad is not None:
                pad.add_probe(Gst.PadProbeType.BUFFER, key_probe)

    def rtsp_pad(element, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is not None and caps.get_size() and caps.get_structure(0).get_string('media') == 'video' and not decodebin.get_static_pad('sink').is_linked():
            pad.link(decodebin.get_static_pad('sink'))
        else:
            fakesink = _make('fakesink', sync=False)
            fakesink.set_property('async', False)
            pipeline.add(fakesink)
            fakesink.sync_state_with_parent()
            pad.link(fakesink.get_static_pad('sink'))

    def decoded(element, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is not None and caps.get_size() and caps.get_structure(0).get_name().startswith('video/') and not convert.get_static_pad('sink').is_linked():
            pad.link(convert.get_static_pad('sink'))
            structure = caps.get_structure(0)
            if structure.has_field('width'):
                result['size'] = (structure.get_value('width'), structure.get_value('height'))

    def sample(appsink):
        if 'image' not in result:
            sample_ = appsink.emit('pull-sample')
            now = time.monotonic()
            result.setdefault('first', now)
            if not result.get('key') and now - result['first'] < KEY_FRAME_WAIT:
                return Gst.FlowReturn.OK    # before the key frame: gray or smeared
            buffer = sample_.get_buffer()
            ok, mapping = buffer.map(Gst.MapFlags.READ)
            if ok:
                try:
                    result['image'] = QImage(bytes(mapping.data), 480, 270, 480 * 4, QImage.Format.Format_RGB32).copy()
                finally:
                    buffer.unmap(mapping)
            done.set()
        return Gst.FlowReturn.OK

    def bus_message(bus, message, data):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            result.setdefault('error', error.message)
            done.set()
        return Gst.BusSyncReply.DROP

    source.connect('select-stream', _video_stream_only)
    source.connect('pad-added', rtsp_pad)
    pipeline.connect('deep-element-added', element_added)
    decodebin.connect('pad-added', decoded)
    sink.connect('new-sample', sample)
    pipeline.get_bus().set_sync_handler(bus_message, None)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        done.wait(timeout)
    finally:
        pipeline.get_bus().set_sync_handler(None, None)
        pipeline.set_state(Gst.State.NULL)
    if 'image' in result:
        width, height = result.get('size', (0, 0))
        return result['image'], width, height
    error = result.get('error')
    if error and re.search(r'unauthori[sz]ed|401', error, re.I):
        raise RuntimeError('the camera refused the user or password')
    raise RuntimeError(error or f'no picture within {timeout} s')
