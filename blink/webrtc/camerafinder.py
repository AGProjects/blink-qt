"""Finding network cameras on the local network, and testing their RTSP streams.

  - ONVIF WS-Discovery: a Probe for NetworkVideoTransmitter sent to 239.255.255.250:3702;
    each ProbeMatch gives the camera's device service address (XAddrs) and its scopes (name,
    hardware). Its RTSP address is then asked with the ONVIF SOAP calls GetCapabilities,
    GetProfiles and GetStreamUri, authenticated with a WS-Security UsernameToken (digest).
  - mDNS: the _rtsp._tcp services (python3-zeroconf, when installed).
  - A test: rtspsrc ─► decodebin ─► appsink until the first picture, or a timeout.

Everything here blocks: it is called from threads (run_in_thread) and answers in the GUI thread.
"""

import base64
import hashlib
import os
import re
import socket
import time
import uuid
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree

from blink.logging import ActivityLog


__all__ = ['FoundCamera', 'discover', 'onvif_stream_uri', 'test_stream']


WS_DISCOVERY = ('239.255.255.250', 3702)
DISCOVERY_TIME = 3.0
SOAP_TIMEOUT = 6
TEST_TIMEOUT = 12


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


# ONVIF: the RTSP address of a camera

def _security_header(user, password):
    if not user:
        return ''
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + (password or '').encode()).digest()).decode()
    return f'''<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{user}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
</UsernameToken></Security></s:Header>'''


def _soap(url, body, user, password):
    envelope = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                f'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
                f'xmlns:tt="http://www.onvif.org/ver10/schema">{_security_header(user, password)}<s:Body>{body}</s:Body></s:Envelope>')
    request = urllib.request.Request(url, data=envelope.encode(), headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
    try:
        with urllib.request.urlopen(request, timeout=SOAP_TIMEOUT) as response:
            return ElementTree.fromstring(response.read())
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors='replace')
        if e.code in (400, 401, 500) and re.search(r'NotAuthorized|not authorized|Sender', text, re.I):
            raise RuntimeError('the camera refused the user or password')
        raise RuntimeError(f'the camera answered {e.code} {e.reason}')


def _find(root, name):
    return next((element for element in root.iter() if _local_name(element.tag) == name), None)


def onvif_stream_uri(xaddr, user, password):
    """The RTSP address of the camera's first media profile (blocks; raises RuntimeError with the reason)."""
    try:
        capabilities = _soap(xaddr, '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>', user, password)
        media = _find(capabilities, 'Media')
        media_url = _find(media, 'XAddr').text.strip() if media is not None and _find(media, 'XAddr') is not None else xaddr
        profiles = _soap(media_url, '<trt:GetProfiles/>', user, password)
        profile = next((element for element in profiles.iter() if _local_name(element.tag) == 'Profiles'), None)
        if profile is None or not profile.get('token'):
            raise RuntimeError('the camera has no media profile')
        body = (f'<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>'
                f'</tt:Transport></trt:StreamSetup><trt:ProfileToken>{profile.get("token")}</trt:ProfileToken></trt:GetStreamUri>')
        answer = _soap(media_url, body, user, password)
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
    """Pull the first picture of an RTSP stream: (QImage, width, height); raises RuntimeError with the reason."""
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
