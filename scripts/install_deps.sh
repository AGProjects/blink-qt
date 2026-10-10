#!/bin/sh
# Install what Blink needs to be built and run from this folder (Debian / Ubuntu),
# as listed in debian/control:
#
#   - the build dependencies (Build-Depends), as the blink-build-deps package made
#     by mk-build-deps (remove them with: sudo apt purge blink-build-deps)
#   - the runtime dependencies (Depends, and Recommends unless --no-recommends)
#
# Then: ./build_inplace && ./run
#
# python3-sipsimple, python3-application, python3-eventlib and python3-otr come from
# the AG Projects repository: add it first (python3-sipsimple docs/Install.linux).
#
# --simulate installs nothing (and needs no root): every dependency is resolved on
# its own against this system's apt sources and reported as OK or MISSING, to check
# that debian/control works on a release (run it in each build chroot). The exit
# status is 1 when something is missing.
#
#   scripts/install_deps.sh [--no-recommends] [--build-only | --runtime-only] [-y] [--simulate]

set -e

cd "$(dirname "$0")/.."
control=debian/control
[ -f "$control" ] || { echo "No $control here" >&2; exit 1; }

recommends=yes
simulate=no
build=yes
runtime=yes
assume_yes=
for argument in "$@"; do
    case "$argument" in
        --no-recommends) recommends=no ;;
        --build-only)    runtime=no ;;
        --runtime-only)  build=no ;;
        -y|--yes)        assume_yes=-y ;;
        -s|--simulate)   simulate=yes ;;
        -h|--help)       sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $argument" >&2; exit 2 ;;
    esac
done

sudo=
[ "$(id -u)" -eq 0 ] || sudo=sudo

# dependencies from debian/control, without ${...} substitution variables:
#   deps build     Build-Depends
#   deps runtime   Depends, and Recommends unless --no-recommends
deps() {
    python3 - "$control" "$1" "$recommends" <<'PYTHON'
import re, sys
control, which, recommends = sys.argv[1:]
source, _, binary = open(control).read().partition('\nPackage: blink')
text = source if which == 'build' else binary

def field(name):
    match = re.search(r'(?:^|\n)%s:(.*?)(?=\n\S|\Z)' % name, text, re.S)
    return match.group(1) if match else ''

if which == 'build':
    fields = field('Build-Depends')
else:
    fields = field('Depends') + (',' + field('Recommends') if recommends == 'yes' else '')
items = [' '.join(item.split()) for item in fields.split(',')]
print(', '.join(item for item in items if item and not item.startswith('${')))
PYTHON
}

# --simulate: each dependency resolved on its own, OK or MISSING (with what apt has)
check() {
    echo "== $1"
    printf '%s\n' "$2" | tr ',' '\n' | sed 's/^ *//; /^$/d' | while IFS= read -r item; do
        if apt-get -s -qq satisfy "$item" >/dev/null 2>&1; then
            echo "   OK       $item"
        else
            available=$(printf '%s\n' "$item" | tr '|' '\n' | sed 's/(.*//; s/ //g' | while read -r name; do
                version=$(apt-cache policy "$name" 2>/dev/null | sed -n 's/^ *Candidate: //p')
                printf '%s ' "$name=${version:-none}"
            done)
            echo "   MISSING  $item    [available: $available]"
            echo "$item" >> "$missing_file"
        fi
    done
}

if [ "$simulate" = yes ]; then
    release=$( (. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") || uname -sr)
    echo "Checking $control on $release (nothing is installed)"
    missing_file=$(mktemp)
    trap 'rm -f "$missing_file"' EXIT
    [ "$build" = yes ] && check 'Build dependencies' "$(deps build)"
    [ "$runtime" = yes ] && check 'Runtime dependencies' "$(deps runtime)"
    if [ -s "$missing_file" ]; then
        echo "== $(wc -l < "$missing_file") dependencies cannot be satisfied on $release"
        exit 1
    fi
    echo "== All dependencies can be satisfied on $release"
    exit 0
fi

if [ "$build" = yes ]; then
    echo "== Build dependencies"
    if ! command -v mk-build-deps >/dev/null 2>&1; then
        $sudo apt-get install $assume_yes devscripts equivs
    fi
    $sudo mk-build-deps -i -r -t "apt-get $assume_yes --no-install-recommends" "$control"
    $sudo rm -f blink-build-deps_*.buildinfo blink-build-deps_*.changes      # left in this folder by mk-build-deps
fi

if [ "$runtime" = yes ]; then
    echo "== Runtime dependencies"
    deps=$(deps runtime)
    echo "$deps" | tr ',' '\n' | sed 's/^ */   /'
    $sudo apt-get satisfy $assume_yes "$deps"
fi

echo "== Done. Build and run from this folder with: ./build_inplace && ./run"
