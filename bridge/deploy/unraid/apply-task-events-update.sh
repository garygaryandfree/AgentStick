#!/bin/bash
set -euo pipefail

APP=/mnt/user/appdata/FunASR-2pass/gateway
WORK=/www/wwwroot/work
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$WORK/backups/vibestick-task-events/$TIMESTAMP"
RELEASE="$WORK/vibestick-task-events-release"
ARCHIVE="$WORK/vibestick-task-events-release.tar.gz"

backup_file() {
    local source=$1
    if [[ -f "$source" ]]; then
        local destination="$BACKUP$source"
        mkdir -p "$(dirname "$destination")"
        cp -a "$source" "$destination"
    fi
}

mkdir -p "$BACKUP"
backup_file "$APP/src/vibe_stick/__init__.py"
backup_file "$APP/src/vibe_stick/paste/input_relay.py"
backup_file "$APP/src/vibe_stick/server/app.py"

rm -rf "$RELEASE"
mkdir -p "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"

install -m 0644 "$RELEASE/bridge/src/vibe_stick/__init__.py" \
    "$APP/src/vibe_stick/__init__.py"
install -m 0644 "$RELEASE/bridge/src/vibe_stick/paste/input_relay.py" \
    "$APP/src/vibe_stick/paste/input_relay.py"
install -m 0644 "$RELEASE/bridge/src/vibe_stick/server/app.py" \
    "$APP/src/vibe_stick/server/app.py"

docker restart Vibe-ASR-Gateway >/dev/null
sleep 6
echo "backup=$BACKUP"
printf 'health='
curl -fsS http://127.0.0.1:8765/health
printf '\n'

rm -rf "$RELEASE"
rm -f "$ARCHIVE" "$WORK/apply-task-events-update.sh"
