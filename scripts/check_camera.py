#!/usr/bin/env python3
"""Why is the camera in use? Who has it, directly or through PipeWire.

    check_camera.py            # what this user can see
    sudo check_camera.py       # every process, of every user

1. The cameras: /dev/video* with their names (and the media devices they belong to).
2. The processes that have a camera device open (from /proc/*/fd). Without root
   only this user's processes can be seen.
3. PipeWire (pw-dump): the camera nodes and their state (running: something is
   capturing; idle or suspended: nothing), and the programs whose streams are
   linked to them. PipeWire itself (or WirePlumber) keeping /dev/video open is
   normal while one of them captures, and for a moment after.
4. The camera portal (xdg-desktop-portal) and the processes known to use it.

The exit status is 1 when a camera is in use, 0 when none is.
"""

import json
import os
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


# 2. Open files

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
    if os.geteuid() != 0:
        out('Not root: only the processes of this user can be seen (run with sudo to see all).')

    section('Cameras')
    devices = cameras()
    if not devices:
        out('  No /dev/video devices: no camera, or its driver is not loaded')
    for path, name in devices.items():
        out(f'  {path:<14} {name}')
    media = media_devices()
    if media:
        out(f"  media devices: {', '.join(media)}")

    section('Processes with a camera device open')
    open_by, hidden = holders(list(devices) + media)
    pipewire_pids = set()
    anything = False
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
    if in_use:
        out('  The camera is in use (see above).')
    elif pipewire_pids:
        out('  Only PipeWire has the camera open, with no stream: it lets it go when it suspends the node')
        out('  (a few seconds). If it never does: systemctl --user restart wireplumber')
    else:
        out('  Nothing is using the camera.')
    return 1 if in_use else 0


if __name__ == '__main__':
    sys.exit(main())
