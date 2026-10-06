#!/bin/sh
#
# Reset Blink Qt to a "first start" state for message and contact import tests,
# keeping the account configuration (and by default the PGP keys).
#
# Usage: scripts/reset_state.sh [--keys] [--logs] [account]
#
#   account   only clear the XCAP cache and journal cursor of this account
#             (default: all accounts); message history is always cleared
#   --keys    also remove PGP keys (to test key escrow restore)
#   --logs    also remove the logs
#
# BLINK_DIR overrides the data directory (default ~/.blink).

set -e

BLINK_DIR=${BLINK_DIR:-$HOME/.blink}
KEYS=0
LOGS=0
ACCOUNT=""

for arg in "$@"; do
    case "$arg" in
        --keys) KEYS=1 ;;
        --logs) LOGS=1 ;;
        -h|--help) sed -n '3,14p' "$0"; exit 0 ;;
        -*) echo "Unknown option $arg" >&2; exit 1 ;;
        *) ACCOUNT="$arg" ;;
    esac
done

if [ ! -d "$BLINK_DIR" ]; then
    echo "$BLINK_DIR does not exist" >&2
    exit 1
fi

if pgrep -f 'bin/blink|blink-run.py' >/dev/null 2>&1; then
    echo "Blink is running, quit it first" >&2
    exit 1
fi

remove() {
    for path in "$@"; do
        if [ -e "$path" ]; then
            rm -rf "$path"
            echo "Removed $path"
        fi
    done
}

# message, call and transfer history, downloaded and cached files
remove "$BLINK_DIR/message_history.db" "$BLINK_DIR/calls_history" "$BLINK_DIR/transfer_history" \
       "$BLINK_DIR/history" "$BLINK_DIR/downloads" "$BLINK_DIR/file_transfers" "$BLINK_DIR/journal" \
       "$BLINK_DIR/addressbook_origins"

# XCAP document cache
if [ -n "$ACCOUNT" ]; then
    remove "$BLINK_DIR/xcap/$ACCOUNT" "$BLINK_DIR/journal/$ACCOUNT" "$BLINK_DIR/addressbook_origins/$ACCOUNT.json"
else
    remove "$BLINK_DIR/xcap"
fi

[ "$KEYS" = 1 ] && remove "$BLINK_DIR/keys"
[ "$LOGS" = 1 ] && remove "$BLINK_DIR/logs"

# forget the journal cursor and API token so the next start syncs from scratch
if [ -f "$BLINK_DIR/config" ]; then
    python3 - "$BLINK_DIR/config" "$ACCOUNT" <<'EOF'
import sys
from sipsimple.configuration.backend.file import FileBackend

filename, account = sys.argv[1], sys.argv[2] or None
keys = ('history_synchronization_id', 'history_synchronization_token', 'history_synchronization_url', 'history_synchronization_timestamp')

backend = FileBackend(filename)
data = backend.load()
changed = False
for account_id, settings in (data.get('Accounts') or {}).items():
    if account is not None and account_id != account:
        continue
    sms = settings.get('sms') if isinstance(settings, dict) else None
    if not isinstance(sms, dict):
        continue
    for key in keys:
        if key in sms:
            del sms[key]
            changed = True
            print('Cleared %s.sms.%s' % (account_id, key))
if changed:
    backend.save(data)
EOF
fi

echo "Done. Account settings kept in $BLINK_DIR/config"
