#!/bin/bash
set -euo pipefail

if [[ -z "${SUB2_TOKEN:-}" ]]; then
    echo "SUB2_TOKEN is required" >&2
    exit 2
fi

APP=/mnt/user/appdata/FunASR-2pass/gateway
TEMPLATE=/boot/config/plugins/dockerMan/templates-user/my-Vibe-ASR-Gateway.xml
WORK=/www/wwwroot/work
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$WORK/backups/vibestick-sub2/$TIMESTAMP"
RELEASE="$WORK/vibestick-sub2-release"
ARCHIVE="$WORK/vibestick-sub2-release.tar.gz"

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
backup_file "$APP/src/vibe_stick/protocol/state.py"
backup_file "$APP/src/vibe_stick/providers/base.py"
backup_file "$APP/src/vibe_stick/server/app.py"
backup_file "$APP/start.sh"
backup_file "$TEMPLATE"

rm -rf "$RELEASE"
mkdir -p "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"

install -d "$APP/src/vibe_stick/usage"
install -m 0644 "$RELEASE/bridge/src/vibe_stick/__init__.py" "$APP/src/vibe_stick/__init__.py"
install -m 0644 "$RELEASE/bridge/src/vibe_stick/protocol/state.py" "$APP/src/vibe_stick/protocol/state.py"
install -m 0644 "$RELEASE/bridge/src/vibe_stick/providers/base.py" "$APP/src/vibe_stick/providers/base.py"
install -m 0644 "$RELEASE/bridge/src/vibe_stick/server/app.py" "$APP/src/vibe_stick/server/app.py"
install -m 0644 "$RELEASE/bridge/src/vibe_stick/usage/__init__.py" "$APP/src/vibe_stick/usage/__init__.py"
install -m 0644 "$RELEASE/bridge/src/vibe_stick/usage/sub2.py" "$APP/src/vibe_stick/usage/sub2.py"
install -m 0755 "$RELEASE/bridge/deploy/unraid/start.sh" "$APP/start.sh"

umask 077
printf '%s\n' \
    "VIBE_STICK_SUB2_USAGE_TOKEN=$SUB2_TOKEN" \
    'VIBE_STICK_SUB2_USAGE_BASE_URL=https://88api.ai/sub2-usage' \
    'VIBE_STICK_SUB2_CODEX_ACCOUNT_ID=52' \
    'VIBE_STICK_SUB2_CLAUDE_ACCOUNT_ID=29' \
    'VIBE_STICK_SUB2_USAGE_INTERVAL_SECONDS=60' \
    > "$APP/sub2-usage.env"

if ! grep -q 'VIBE_STICK_SUB2_USAGE_TOKEN' "$TEMPLATE"; then
    awk -v token="$SUB2_TOKEN" '
        { print }
        /Target="VIBE_STICK_BRIDGE_TOKEN"/ {
            print "  <Config Name=\"Sub2 Usage Token\" Target=\"VIBE_STICK_SUB2_USAGE_TOKEN\" Default=\"\" Mode=\"\" Description=\"Server-side Codex and Claude quota token\" Type=\"Variable\" Display=\"always\" Required=\"false\" Mask=\"true\">" token "</Config>"
            print "  <Config Name=\"Sub2 Usage Base URL\" Target=\"VIBE_STICK_SUB2_USAGE_BASE_URL\" Default=\"https://88api.ai/sub2-usage\" Mode=\"\" Description=\"Sub2-Usage API base URL\" Type=\"Variable\" Display=\"advanced\" Required=\"true\" Mask=\"false\">https://88api.ai/sub2-usage</Config>"
            print "  <Config Name=\"Codex Usage Account\" Target=\"VIBE_STICK_SUB2_CODEX_ACCOUNT_ID\" Default=\"52\" Mode=\"\" Description=\"Sub2 gpt-plus account id\" Type=\"Variable\" Display=\"advanced\" Required=\"true\" Mask=\"false\">52</Config>"
            print "  <Config Name=\"Claude Usage Account\" Target=\"VIBE_STICK_SUB2_CLAUDE_ACCOUNT_ID\" Default=\"29\" Mode=\"\" Description=\"Sub2 Claude account id\" Type=\"Variable\" Display=\"advanced\" Required=\"true\" Mask=\"false\">29</Config>"
            print "  <Config Name=\"Usage Refresh Seconds\" Target=\"VIBE_STICK_SUB2_USAGE_INTERVAL_SECONDS\" Default=\"60\" Mode=\"\" Description=\"Background quota refresh interval\" Type=\"Variable\" Display=\"advanced\" Required=\"true\" Mask=\"false\">60</Config>"
        }
    ' "$TEMPLATE" > "$WORK/my-Vibe-ASR-Gateway.xml.new"
    install -m 0600 "$WORK/my-Vibe-ASR-Gateway.xml.new" "$TEMPLATE"
    rm -f "$WORK/my-Vibe-ASR-Gateway.xml.new"
else
    sed -i -E "s#^(.*Target=\"VIBE_STICK_SUB2_USAGE_TOKEN\"[^>]*>)[^<]*(</Config>)#\1${SUB2_TOKEN}\2#" "$TEMPLATE"
fi

docker restart Vibe-ASR-Gateway >/dev/null
sleep 6
echo "backup=$BACKUP"
printf 'health='
curl -fsS http://127.0.0.1:8765/health
printf '\n'

rm -rf "$RELEASE"
rm -f "$ARCHIVE" "$WORK/apply-sub2-update.sh"
