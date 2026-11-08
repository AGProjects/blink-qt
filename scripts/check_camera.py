#!/usr/bin/env python3
"""Why is the camera in use? Who has it, directly or through PipeWire.

    check_camera.py            # what this user can see
    sudo check_camera.py       # every process, of every user

1. The cameras: /dev/video* with their names (and the media devices they belong to).
   For each capture node: its USB port and speed, its formats (YUYV, MJPG, ...)
   with their sizes and highest frame rates, and which of the sizes Blink sends
   (480p, 720p, 1080p) it delivers. Nodes that deliver no picture (metadata) are
   marked as such.
   pjmedia (the SDK's V4L2 backend) probes /dev/video0 to /dev/video3 only: a
   camera on a higher node is not seen by Blink (SDK_SCANNED_NODES=64 in the
   environment once the SDK has 60_v4l2_scan_all_nodes.patch).
2. Blink's camera setting (~/.blink/config): the device and resolution, and
   whether that camera delivers that resolution. When it does not, the SDK cannot
   open it and silently falls back to the system default camera.
3. The processes that have a camera device open (from /proc/*/fd). Without root
   only this user's processes can be seen. A process holding a camera its parent
   also holds most likely inherited the descriptor (opened without O_CLOEXEC):
   it keeps the camera busy for as long as it runs.
4. PipeWire (pw-dump): the camera nodes and their state (running: something is
   capturing; idle or suspended: nothing), and the programs whose streams are
   linked to them. PipeWire itself (or WirePlumber) keeping /dev/video open is
   normal while one of them captures, and for a moment after.
5. The camera portal (xdg-desktop-portal) and the processes known to use it.

The exit status is 1 when a camera is in use, 0 when none is.
"""

import fcntl
import json
import os
import re
import struct
import pwd
import shutil
import subprocess
import sys


def out(text=''):
    print(text, flush=True)


def section(title):
    out()
    out(title)
    out('=' * len(title))


def read(path, default=''):
    try:
        with open(path, 'rb') as file:
            return file.read().decode(errors='replace').strip()
    except OSError:
        return default


def process(pid):
    """(user, command name, command line) of a process."""
    try:
        user = pwd.getpwuid(os.stat(f'/proc/{pid}').st_uid).pw_name
    except (OSError, KeyError):
        user = '?'
    command = read(f'/proc/{pid}/comm', '?')
    arguments = read(f'/proc/{pid}/cmdline').replace('\0', ' ').strip()
    return user, command, arguments


def parent(pid):
    """The parent's pid of a process, or None."""
    stat = read(f'/proc/{pid}/stat')
    try:
        return int(stat.rpartition(')')[2].split()[1])
    except (IndexError, ValueError):
        return None


def describe(pid):
    user, command, arguments = process(pid)
    if len(arguments) > 110:
        arguments = arguments[:109] + '…'
    return f'{command} (pid {pid}, user {user}): {arguments or "-"}'


# 1. Devices

def cameras():
    """{'/dev/videoN': name}"""
    found = {}
    base = '/sys/class/video4linux'
    for entry in sorted(os.listdir(base)) if os.path.isdir(base) else ():
        if entry.startswith('video'):
            found[f'/dev/{entry}'] = read(os.path.join(base, entry, 'name'), '?')
    return found


def media_devices():
    return sorted(f'/dev/{entry}' for entry in os.listdir('/dev') if entry.startswith('media'))


# Formats (V4L2 ioctls, no v4l2-ctl needed)

def _iowr(nr, size, direction=3):
    return (direction << 30) | (size << 16) | (ord('V') << 8) | nr


VIDIOC_QUERYCAP = _iowr(0, 104, direction=2)
VIDIOC_ENUM_FMT = _iowr(2, 64)
VIDIOC_ENUM_FRAMESIZES = _iowr(74, 44)
VIDIOC_ENUM_FRAMEINTERVALS = _iowr(75, 52)
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1

# pjmedia's V4L2 backend probes /dev/video0 .. /dev/video3 (V4L2_MAX_DEVS 4 in pjproject 2.17),
# unless the SDK carries 60_v4l2_scan_all_nodes.patch
SDK_SCANNED_NODES = int(os.environ.get('SDK_SCANNED_NODES', '4'))

# the sizes Blink sends (blink.screen_share.CAMERA_RESOLUTIONS)
BLINK_RESOLUTIONS = [((640, 480), '480p'), ((1280, 720), '720p'), ((1920, 1080), '1080p')]


def _ioctl(fd, request, buffer):
    fcntl.ioctl(fd, request, buffer, True)
    return buffer


def _fourcc(value):
    return struct.pack('<I', value).decode('ascii', errors='replace').strip()


def _intervals(fd, pixelformat, width, height):
    """The highest frame rate of a size, None when the driver does not say."""
    rates = []
    for index in range(64):
        buffer = bytearray(struct.pack('<5I', index, pixelformat, width, height, 0) + bytes(32))
        try:
            _ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, buffer)
        except OSError:
            break
        kind = struct.unpack_from('<I', buffer, 16)[0]
        if kind == 1:      # discrete
            numerator, denominator = struct.unpack_from('<2I', buffer, 20)
            if numerator:
                rates.append(denominator / numerator)
        else:              # continuous or stepwise: the minimum interval is the highest rate
            numerator, denominator = struct.unpack_from('<2I', buffer, 20)
            if numerator:
                rates.append(denominator / numerator)
            break
    return max(rates) if rates else None


def capture_info(path):
    """{'capture': bool, 'formats': [(fourcc, description, [(w, h, fps)])]}, or {'error': text}."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        return dict(error=e.strerror)
    try:
        caps = _ioctl(fd, VIDIOC_QUERYCAP, bytearray(104))
        capabilities, device_caps = struct.unpack_from('<2I', caps, 84)
        if capabilities & V4L2_CAP_DEVICE_CAPS:
            capabilities = device_caps
        if not capabilities & V4L2_CAP_VIDEO_CAPTURE:
            return dict(capture=False, formats=[])
        formats = []
        for index in range(32):
            buffer = bytearray(struct.pack('<3I', index, V4L2_BUF_TYPE_VIDEO_CAPTURE, 0) + bytes(52))
            try:
                _ioctl(fd, VIDIOC_ENUM_FMT, buffer)
            except OSError:
                break
            description = bytes(buffer[12:44]).split(b'\0', 1)[0].decode(errors='replace')
            pixelformat = struct.unpack_from('<I', buffer, 44)[0]
            sizes = []
            for size_index in range(128):
                frame = bytearray(struct.pack('<3I', size_index, pixelformat, 0) + bytes(32))
                try:
                    _ioctl(fd, VIDIOC_ENUM_FRAMESIZES, frame)
                except OSError:
                    break
                kind = struct.unpack_from('<I', frame, 8)[0]
                if kind == 1:
                    width, height = struct.unpack_from('<2I', frame, 12)
                    sizes.append((width, height, _intervals(fd, pixelformat, width, height)))
                else:      # continuous or stepwise: the largest size stands for the range
                    min_w, max_w, step_w, min_h, max_h, step_h = struct.unpack_from('<6I', frame, 12)
                    sizes.append((max_w, max_h, _intervals(fd, pixelformat, max_w, max_h)))
                    sizes.append((min_w, min_h, _intervals(fd, pixelformat, min_w, min_h)))
                    break
            formats.append((_fourcc(pixelformat), description, sizes))
        return dict(capture=True, formats=formats)
    except OSError as e:
        return dict(error=e.strerror)
    finally:
        os.close(fd)


def usb_port(path):
    """(bus-port, speed in Mbps, removable) of a camera on USB, or None."""
    directory = os.path.realpath(f'/sys/class/video4linux/{os.path.basename(path)}/device')
    while directory.startswith('/sys/') and directory != '/sys':
        speed = read(os.path.join(directory, 'speed'))
        if speed:
            return os.path.basename(directory), speed, read(os.path.join(directory, 'removable'), 'unknown')
        directory = os.path.dirname(directory)
    return None


def size_support(formats, width, height):
    """[(fourcc, fps)] of the formats that deliver a size."""
    return [(fourcc, fps) for fourcc, _, sizes in formats for w, h, fps in sizes if (w, h) == (width, height)]


def describe_rate(fps):
    return '?' if fps is None else f'{fps:g}'


# Blink's setting

def blink_video_setting():
    """{'device': ..., 'resolution': ..., 'framerate': ...} of SIPSimpleSettings.video in
    Blink's configuration file, or None without one. The file is the SDK's FileBackend
    format: groups are 'name:' lines, settings 'name = value' lines, nested by indentation."""
    sudo_user = os.environ.get('SUDO_USER')
    home = os.path.expanduser(f'~{sudo_user}') if sudo_user else os.path.expanduser('~')
    path = os.path.join(home, '.blink', 'config')
    try:
        with open(path, encoding='utf-8', errors='replace') as file:
            lines = file.read().splitlines()
    except OSError:
        return None
    stack = []      # [(indentation, group name)]
    found = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indentation = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indentation:
            stack.pop()
        text = line.strip()
        if text.endswith(':') and '=' not in text:
            stack.append((indentation, text[:-1].strip().strip('"')))
            continue
        name, separator, value = text.partition('=')
        if not separator:
            continue
        groups = [group.lower() for _, group in stack]
        if not groups or groups[-1] != 'video':
            continue
        # the general settings, not an account's: the shallowest video group wins
        if found is None or len(groups) < found[0]:
            found = (len(groups), {})
        if len(groups) == found[0]:
            found[1][name.strip()] = value.strip().strip('"')
    return dict(path=path, **found[1]) if found else dict(path=path)


def parse_resolution(text):
    match = re.match(r'\s*(\d+)\s*x\s*(\d+)', text or '')
    return (int(match.group(1)), int(match.group(2))) if match else None


# Open files

def holders(paths):
    """{device path: [pid]} for the processes that have one of the paths open."""
    result = {path: [] for path in paths}
    targets = {os.path.realpath(path): path for path in paths}
    hidden = 0
    for pid in sorted((entry for entry in os.listdir('/proc') if entry.isdigit()), key=int):
        directory = f'/proc/{pid}/fd'
        try:
            descriptors = os.listdir(directory)
        except PermissionError:
            hidden += 1
            continue
        except OSError:
            continue
        seen = set()
        for descriptor in descriptors:
            try:
                target = os.readlink(os.path.join(directory, descriptor))
            except OSError:
                continue
            path = targets.get(target)
            if path is not None and path not in seen:
                seen.add(path)
                result[path].append(int(pid))
    return result, hidden


# 3. PipeWire

def pipewire():
    """([camera node], {node id: node}) from pw-dump, or None without PipeWire."""
    if shutil.which('pw-dump') is None:
        return None
    try:
        objects = json.loads(subprocess.run(['pw-dump'], capture_output=True, timeout=10).stdout or b'[]')
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    nodes, links, clients = {}, [], {}
    for item in objects:
        kind = item.get('type', '')
        info = item.get('info') or {}
        props = info.get('props') or {}
        if kind.endswith(':Node'):
            nodes[item['id']] = dict(id=item['id'], state=info.get('state'), props=props)
        elif kind.endswith(':Link'):
            links.append(info)
        elif kind.endswith(':Client'):
            clients[item['id']] = props
    cameras_ = [node for node in nodes.values() if node['props'].get('media.class') == 'Video/Source'
                and (node['props'].get('device.api') in ('v4l2', 'libcamera') or 'api.v4l2.path' in node['props'] or 'api.libcamera.path' in node['props'])]
    for camera in cameras_:
        consumers = []
        for link in links:
            if link.get('output-node-id') != camera['id']:
                continue
            node = nodes.get(link.get('input-node-id'))
            if node is None:
                continue
            props = node['props']
            client = clients.get(props.get('client.id')) or {}
            pid = props.get('application.process.id') or client.get('application.process.id') or client.get('pipewire.sec.pid')
            name = props.get('application.name') or client.get('application.name') or props.get('node.name') or '?'
            consumers.append((name, pid, link.get('state')))
        camera['consumers'] = consumers
    return cameras_, nodes


def main():
    in_use = False
    problems = []
    if os.geteuid() != 0:
        out('Not root: only the processes of this user can be seen (run with sudo to see all).')

    section('Cameras')
    devices = cameras()
    if not devices:
        out('  No /dev/video devices: no camera, or its driver is not loaded')
    infos = {}
    for path, name in devices.items():
        info = infos[path] = capture_info(path)
        if 'error' in info:
            out(f'  {path:<14} {name}   (cannot be opened: {info["error"]})')
            continue
        if not info['capture']:
            out(f'  {path:<14} {name}   (no picture: a metadata node)')
            continue
        out(f'  {path:<14} {name}')
        port = usb_port(path)
        if port is not None:
            usb, speed, removable = port
            speed_text = {'1.5': '1.5 Mbps, USB 1 low speed', '12': '12 Mbps, USB 1', '480': '480 Mbps, USB 2',
                          '5000': '5 Gbps, USB 3', '10000': '10 Gbps, USB 3.1', '20000': '20 Gbps, USB 3.2'}.get(speed, f'{speed} Mbps')
            out(f'      USB {usb}, {speed_text}, {removable}')
        if not info['formats']:
            out('      no formats: it delivers no picture')
        for fourcc, description, sizes in info['formats']:
            largest = sorted(sizes, key=lambda size: size[0] * size[1], reverse=True)
            listed = ', '.join(f'{w}x{h}@{describe_rate(fps)}' for w, h, fps in largest[:8]) + (', ...' if len(largest) > 8 else '')
            out(f'      {fourcc:<5} {description}: {listed or "no sizes"}')
        support = []
        for (width, height), label in BLINK_RESOLUTIONS:
            formats = size_support(info['formats'], width, height)
            if formats:
                support.append(f'{label} ' + '/'.join(f'{fourcc}@{describe_rate(fps)}' for fourcc, fps in formats))
            else:
                support.append(f'{label} no')
        out(f"      Blink's sizes: {', '.join(support)}")
        if info['formats'] and not any(fourcc in ('MJPG', 'H264', 'HEVC') for fourcc, _, _ in info['formats']):
            out('      only raw formats: large sizes are limited by the USB bandwidth')
        number = int(re.sub(r'\D', '', os.path.basename(path)) or 0)
        if number >= SDK_SCANNED_NODES and info['formats']:
            problems.append(f'{name} is {path}: the SDK (pjmedia V4L2) scans /dev/video0 to /dev/video{SDK_SCANNED_NODES - 1} only, '
                            'so Blink does not list it nor can open it. Replug it while lower numbers are free, or build the SDK '
                            'with 60_v4l2_scan_all_nodes.patch')
            out(f'      ! not seen by the SDK: it scans /dev/video0 to /dev/video{SDK_SCANNED_NODES - 1} only')
    media = media_devices()
    if media:
        out(f"  media devices: {', '.join(media)}")

    section("Blink's camera setting")
    first_setting_problem = len(problems)
    setting = blink_video_setting()
    if setting is None:
        out('  No ~/.blink/config: Blink was not run by this user')
    else:
        device = setting.get('device')
        resolution = setting.get('resolution')
        out(f"  device: {device or 'system_default (not set)'}")
        out(f"  resolution: {resolution or '640x480 (not set)'}")
        if setting.get('framerate'):
            out(f"  framerate: {setting['framerate']}")
        size = parse_resolution(resolution) or (640, 480)
        capture = {path: name for path, name in devices.items() if infos.get(path, {}).get('capture')}
        if device in (None, '', 'None'):
            out('  No camera chosen (or none could be opened when Blink last started): video calls send no picture')
        elif device == 'system_default':
            chosen = next(iter(capture), None)
            if chosen is not None:
                out(f'  system_default is the first camera: {chosen} {capture[chosen]}')
        else:
            matches = [path for path, name in capture.items() if name == device or device.startswith(name) or name.startswith(device)]
            if not matches:
                problems.append(f'Blink is set to the camera "{device}", which is not connected')
            for path in matches:
                formats = size_support(infos[path]['formats'], *size)
                if formats:
                    out(f'  {path} delivers {size[0]}x{size[1]}: ' + ', '.join(f'{fourcc}@{describe_rate(fps)}' for fourcc, fps in formats))
                else:
                    problems.append(f'{device} ({path}) does not deliver {size[0]}x{size[1]}: the SDK cannot open it at that size '
                                    f'and falls back to the system default camera. Choose a size it has in Blink\'s camera menu')
        for problem in problems[first_setting_problem:]:
            out(f'  ! {problem}')

    section('Processes with a camera device open')
    open_by, hidden = holders(list(devices) + media)
    pipewire_pids = set()
    anything = False
    inherited = []
    for path, pids in open_by.items():
        for pid in pids:
            anything = True
            user, command, _ = process(pid)
            if command in ('pipewire', 'wireplumber', 'pipewire-media-session'):
                pipewire_pids.add(pid)
                out(f'  {path:<14} {describe(pid)}   <- PipeWire: see below who it captures for')
            else:
                in_use = True
                out(f'  {path:<14} {describe(pid)}')
                ppid = parent(pid)
                if ppid in pids:
                    inherited.append((path, pid, ppid))
                    out(f'  {"":<14} ^ inherited from its parent (pid {ppid}): the descriptor leaked into a child '
                        'process, which keeps the camera busy while it runs')
    if not anything:
        out('  None' + (f' ({hidden} processes of other users not readable)' if hidden else ''))

    section('PipeWire camera nodes')
    result = pipewire()
    if result is None:
        out('  pw-dump not found or PipeWire not running')
    else:
        camera_nodes, _ = result
        if not camera_nodes:
            out('  No camera nodes')
        for node in camera_nodes:
            props = node['props']
            name = props.get('node.description') or props.get('node.nick') or props.get('node.name')
            path = props.get('api.v4l2.path') or props.get('api.libcamera.path') or ''
            state = node.get('state') or '?'
            out(f'  node {node["id"]:<5} {name}  {path}  state: {state}')
            for consumer, pid, link_state in node['consumers']:
                in_use = True
                who = describe(pid) if pid and str(pid).isdigit() and os.path.isdir(f'/proc/{pid}') else f'{consumer} (pid {pid or "?"})'
                out(f'           captured by {who}  [link {link_state}]')
            if state == 'running' and not node['consumers']:
                in_use = True
                out('           running, but no stream linked (a program is starting or stopping)')

    section('Camera portal')
    portal = [pid for pid in (entry for entry in os.listdir('/proc') if entry.isdigit())
              if read(f'/proc/{pid}/comm').startswith('xdg-desktop-por')]
    if portal:
        for pid in portal:
            out(f'  {describe(pid)}')
        out('  Flatpak and Snap programs (and browsers in them) reach the camera through it and PipeWire;')
        out('  they show up above as PipeWire streams, not as /dev/video users.')
    else:
        out('  xdg-desktop-portal is not running')

    section('Result')
    for problem in problems:
        out(f'  ! {problem}')
    for path, pid, ppid in inherited:
        _, command, _ = process(pid)
        _, parent_command, _ = process(ppid)
        out(f'  ! {command} (pid {pid}) holds {path} inherited from {parent_command} (pid {ppid}): reopening the camera '
            'can fail with "device busy" until it exits')
    if in_use:
        busy = sorted(path for path, pids in open_by.items() if any(pid not in pipewire_pids for pid in pids))
        out(f"  In use: {', '.join(busy) or 'through PipeWire'} (see above).")
    elif pipewire_pids:
        out('  Only PipeWire has the camera open, with no stream: it lets it go when it suspends the node')
        out('  (a few seconds). If it never does: systemctl --user restart wireplumber')
    else:
        out('  Nothing is using the camera.')
    return 1 if in_use else 0


if __name__ == '__main__':
    sys.exit(main())
