#!/usr/bin/env python3
"""
Post-process a PyInstaller .app bundle for distribution (macOS only).

Subcommands
-----------
  relink   Rewrite absolute, non-system dylib load commands (e.g. /opt/local,
           /usr/local, MacPorts, Homebrew, /Library/Frameworks) to
           @executable_path/../Frameworks/<name>, copying any missing library
           into Contents/Frameworks recursively so the bundle is self-contained.

  strip    Remove loose *.dylib in Contents/Frameworks that nothing in the
           bundle references (orphans and redundant duplicates, e.g. a second
           OpenSSL major). Removed files are MOVED out of the .app (not
           deleted) so they can be restored. Iterated to a fixpoint.

  slim     Remove named *.framework bundles (e.g. QtPdf, QtQuick3D*,
           QtShaderTools) that the app does not use. A framework still
           referenced by a surviving binary is kept unless --force is given.

  verify   Assert no residual absolute non-system load commands remain and
           report duplicate library majors.

This is the PyInstaller-layout adaptation of the Cocoa Blink build_scripts
(get_deps_recurrent.py + change_lib_paths.sh). It shells out to `otool` and
`install_name_tool`; run codesigning separately (03-codesign.sh) afterwards,
since editing load commands invalidates any existing signature.
"""

import argparse
import fnmatch
import os
import shutil
import struct
import subprocess
import sys
from collections import defaultdict

# Load commands pointing here are OS-provided; never bundle or rewrite them.
SYSTEM_PREFIXES = ("/usr/lib/", "/System/")
# Already bundle-relative; leave as-is.
RELATIVE_PREFIXES = ("@executable_path", "@loader_path", "@rpath")
# Everything the app's own libraries are relinked to point at.
NEW_PREFIX = "@executable_path/../Frameworks/"

MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",  # 64-bit little-endian
    b"\xce\xfa\xed\xfe",  # 32-bit little-endian
    b"\xfe\xed\xfa\xcf",  # 64-bit big-endian
    b"\xfe\xed\xfa\xce",  # 32-bit big-endian
    b"\xca\xfe\xba\xbe",  # fat/universal
    b"\xbe\xba\xfe\xca",  # fat, swapped
}


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def is_macho(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) in MACHO_MAGICS
    except (OSError, IOError):
        return False


def list_macho(app):
    """Every Mach-O file inside the .app (skipping symlinks)."""
    out = []
    for root, _dirs, files in os.walk(app):
        for name in files:
            p = os.path.join(root, name)
            if os.path.islink(p):
                continue
            if is_macho(p):
                out.append(p)
    return out


def otool_deps(path):
    """LC_LOAD_DYLIB paths (dependencies) declared by a Mach-O file."""
    res = run(["otool", "-L", path])
    if res.returncode != 0:
        return []
    deps = []
    for line in res.stdout.splitlines():
        if not line.startswith("\t"):
            continue  # first line is the file name, not a dependency
        dep = line.strip().split(" ", 1)[0]
        if dep:
            deps.append(dep)
    return deps


def needs_bundling(dep):
    """True for an absolute path that is neither an OS library nor already
    bundle-relative — i.e. something we must copy in and relink."""
    if dep.startswith(RELATIVE_PREFIXES):
        return False
    if dep.startswith(SYSTEM_PREFIXES):
        return False
    return dep.startswith("/")


def make_writable(path):
    try:
        os.chmod(path, os.stat(path).st_mode | 0o200)
    except OSError:
        pass


def current_id(path):
    """The LC_ID_DYLIB install name of a dylib (via `otool -D`), or None."""
    res = run(["otool", "-D", path])
    if res.returncode != 0:
        return None
    lines = res.stdout.splitlines()
    return lines[1].strip() if len(lines) > 1 else None


def adhoc_sign(path):
    """Ad-hoc re-sign a modified Mach-O so it still loads on Apple Silicon.
    (The real Developer ID signing happens later in 03-codesign.sh.)"""
    run(["codesign", "--force", "--sign", "-", path])


# --------------------------------------------------------------------------- #
# relink
# --------------------------------------------------------------------------- #
def cmd_relink(app):
    frameworks = os.path.join(app, "Contents", "Frameworks")
    os.makedirs(frameworks, exist_ok=True)

    worklist = list_macho(app)
    processed = set()
    copied = 0
    rewritten = 0
    missing = set()
    modified = set()

    while worklist:
        m = worklist.pop()
        if m in processed:
            continue
        processed.add(m)

        for dep in otool_deps(m):
            if not needs_bundling(dep):
                continue
            base = os.path.basename(dep)
            target = os.path.join(frameworks, base)

            if not os.path.exists(target):
                if os.path.exists(dep):
                    shutil.copy(dep, target)
                    make_writable(target)
                    # A freshly copied lib gets a sane bundle-relative id.
                    run(["install_name_tool", "-id", NEW_PREFIX + base, target])
                    modified.add(target)
                    copied += 1
                    worklist.append(target)  # relink the newly added lib too
                else:
                    missing.add(dep)
                    continue

            make_writable(m)
            run(["install_name_tool", "-change", dep, NEW_PREFIX + base, m])
            rewritten += 1
            modified.add(m)

    # Only files we actually changed need re-signing. Anything we did not touch
    # keeps its original (valid) signature. This is critical on Apple Silicon:
    # a modified-but-unsigned Mach-O is SIGKILLed at dlopen ("Code Signature
    # Invalid"). The real Developer ID signing still happens in 03-codesign.sh.
    for f in modified:
        adhoc_sign(f)

    print(f"relink: copied {copied} libraries, rewrote {rewritten} load "
          f"commands, re-signed {len(modified)} file(s)")
    if missing:
        print("relink: WARNING — referenced libraries not found on disk "
              "(cannot bundle):", file=sys.stderr)
        for dep in sorted(missing):
            print(f"    {dep}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# strip
# --------------------------------------------------------------------------- #
def _referenced_basenames(machos):
    """Basenames named as a dependency by any Mach-O in the set."""
    refs = set()
    for m in machos:
        for dep in otool_deps(m):
            refs.add(os.path.basename(dep))
    return refs


def cmd_strip(app, dry_run, dest):
    frameworks = os.path.join(app, "Contents", "Frameworks")
    exe_dir = os.path.join(app, "Contents", "MacOS")

    # Candidates: loose *.dylib directly inside Contents/Frameworks. We never
    # touch python extensions (*.so), framework bundles, or the executables —
    # they are entry points loaded by name.
    def candidates():
        return [os.path.join(frameworks, f)
                for f in os.listdir(frameworks)
                if f.endswith(".dylib")
                and os.path.isfile(os.path.join(frameworks, f))
                and not os.path.islink(os.path.join(frameworks, f))]

    # Always-keep basenames: everything that is an entry point.
    keep_always = set()
    for m in list_macho(app):
        base = os.path.basename(m)
        if m.endswith(".so") or "/Contents/MacOS/" in m or ".framework/" in m:
            keep_always.add(base)

    moved = []
    total_bytes = 0
    changed = True
    while changed:
        changed = False
        machos = list_macho(app)
        referenced = _referenced_basenames(machos)
        keep = referenced | keep_always
        for lib in candidates():
            base = os.path.basename(lib)
            if base in keep:
                continue
            # Orphan: nothing references it and it is not an entry point.
            size = os.path.getsize(lib)
            total_bytes += size
            if dry_run:
                print(f"  ORPHAN  {base}  ({size/1024/1024:.1f} MB)")
                keep_always.add(base)  # avoid re-reporting in the next pass
            else:
                rel = os.path.relpath(lib, app)
                out = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.move(lib, out)
                print(f"  pruned  {base}  ({size/1024/1024:.1f} MB) -> {out}")
                changed = True
            moved.append(base)

    verb = "would free" if dry_run else "freed"
    print(f"strip: {len(moved)} unreferenced dylib(s), {verb} "
          f"{total_bytes/1024/1024:.1f} MB")
    if dry_run and moved:
        print("strip: dry-run only — nothing was moved. Re-run without "
              "--dry-run to prune.")
    return 0


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _version_stem(base):
    # libfoo.1.2.dylib -> libfoo ; QtCore -> QtCore
    name = base
    for suffix in (".dylib", ".so"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    parts = name.split(".")
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    return ".".join(parts)


def cmd_verify(app):
    residual = defaultdict(list)
    for m in list_macho(app):
        for dep in otool_deps(m):
            if needs_bundling(dep):
                residual[dep].append(os.path.relpath(m, app))

    frameworks = os.path.join(app, "Contents", "Frameworks")
    stems = defaultdict(list)
    if os.path.isdir(frameworks):
        for f in os.listdir(frameworks):
            if f.endswith(".dylib") and os.path.isfile(os.path.join(frameworks, f)):
                stems[_version_stem(f)].append(f)
    dups = {stem: libs for stem, libs in stems.items() if len(libs) > 1}

    if dups:
        print("verify: duplicate library majors still present:")
        for stem, libs in sorted(dups.items()):
            print(f"    {stem}: {', '.join(sorted(libs))}")

    if residual:
        print("verify: FAILED — residual absolute non-system load commands:",
              file=sys.stderr)
        for dep, referrers in sorted(residual.items()):
            print(f"    {dep}", file=sys.stderr)
            for r in referrers:
                print(f"        <- {r}", file=sys.stderr)
        return 1

    print("verify: OK — no /opt/local, /usr/local or other non-system "
          "absolute load commands remain.")
    return 0


# --------------------------------------------------------------------------- #
# slim
# --------------------------------------------------------------------------- #
def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    return total


def _all_frameworks(app):
    out = []
    for root, dirs, _files in os.walk(app):
        for d in dirs:
            if d.endswith(".framework"):
                out.append(os.path.join(root, d))
    return out


def cmd_slim(app, patterns, force):
    candidates = []
    for fw in _all_frameworks(app):
        name = os.path.basename(fw)[: -len(".framework")]
        if any(fnmatch.fnmatch(name, p) for p in patterns):
            candidates.append(fw)

    if not candidates:
        print("slim: no matching frameworks found")
        return 0

    cand_names = {os.path.basename(c)[: -len(".framework")] for c in candidates}

    def inside_candidate(path):
        return any(path == c or path.startswith(c + os.sep) for c in candidates)

    # Which surviving binaries still load each candidate framework?
    referrers = defaultdict(list)
    for m in list_macho(app):
        if inside_candidate(m):
            continue
        for dep in otool_deps(m):
            for name in cand_names:
                if "/%s.framework/" % name in dep:
                    referrers[name].append(os.path.relpath(m, app))

    removed_bytes = 0
    removed = 0
    for fw in sorted(candidates):
        name = os.path.basename(fw)[: -len(".framework")]
        refs = referrers.get(name)
        if refs and not force:
            print(f"  KEEP    {name}.framework — still referenced by "
                  f"{len(refs)} binary(ies); skipping (use --force to override)")
            for r in sorted(set(refs))[:3]:
                print(f"            <- {r}")
            continue
        size = _dir_size(fw)
        shutil.rmtree(fw)
        removed_bytes += size
        removed += 1
        print(f"  removed  {name}.framework  ({size/1024/1024:.1f} MB)")

    print(f"slim: removed {removed} framework(s), freed "
          f"{removed_bytes/1024/1024:.1f} MB")
    return 0


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("relink", help="rewrite system-lib load commands to the bundle")
    p.add_argument("app")

    p = sub.add_parser("strip", help="remove unreferenced/duplicate dylibs")
    p.add_argument("app")
    p.add_argument("--dry-run", action="store_true", help="report only; move nothing")
    p.add_argument("--into", default=None,
                   help="destination for pruned libs (default: <app-dir>/pruned-libs)")

    p = sub.add_parser("slim", help="remove named unused *.framework bundles")
    p.add_argument("app")
    p.add_argument("--framework", action="append", default=[], metavar="PATTERN",
                   help="framework name (glob) to remove; repeatable, e.g. "
                        "--framework QtPdf --framework 'QtQuick3D*'")
    p.add_argument("--force", action="store_true",
                   help="remove even if a surviving binary still references it")

    p = sub.add_parser("verify", help="assert no residual system-lib links")
    p.add_argument("app")

    args = ap.parse_args()

    app = os.path.abspath(args.app)
    if not app.endswith(".app") or not os.path.isdir(app):
        print(f"error: {args.app} is not a .app bundle", file=sys.stderr)
        return 2

    if sys.platform != "darwin":
        print("error: this tool must run on macOS (needs otool/install_name_tool).",
              file=sys.stderr)
        return 2

    if args.command == "relink":
        return cmd_relink(app)
    if args.command == "strip":
        dest = args.into or os.path.join(os.path.dirname(app), "pruned-libs")
        return cmd_strip(app, args.dry_run, dest)
    if args.command == "slim":
        return cmd_slim(app, args.framework, args.force)
    if args.command == "verify":
        return cmd_verify(app)
    return 2


if __name__ == "__main__":
    sys.exit(main())
