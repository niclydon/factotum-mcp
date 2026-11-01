#!/bin/bash
# install.sh <tailnet-bind-ip> [host-label]
#
# First install of factotum-gateway on one Mac. Run ON the host.
# - Creates ~/services/factotum-gateway/ and copies server.py there.
# - Writes the host's ONE shared gateway token (D2), supplied via
#   the FACTOTUM_GATEWAY_TOKEN environment variable, into
#   ~/services/factotum-gateway/.env (0600) if no .env exists; an
#   existing .env is left untouched (re-runs never re-key silently).
#   The .env contains ONLY gateway vars: GATEWAY_TOKEN, GATEWAY_BIND,
#   GATEWAY_PORT, GATEWAY_HOST.
# - Creates the audit dir ~/Library/Application Support/FactotumGateway/.
# - Renders the LaunchAgent plist and bootstraps it in the caller's
#   GUI domain.
# First install has nothing to back up; say so rather than inventing
# a backup. Re-runs keep the previous server.py as
# server.py.bak-<date>-pre<version> before overwriting.
set -euo pipefail
BIND="${1:?usage: install.sh <tailnet-bind-ip> [host-label]}"
HOST_LABEL="${2:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/services/factotum-gateway"
PYTHON_BIN="$HOME/services/factotum-mcp/.venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3)"

mkdir -p "$DEST"
if [ -f "$DEST/server.py" ]; then
  cp "$DEST/server.py" "$DEST/server.py.bak-$(date +%Y%m%d)-pre-install"
  echo "backed up previous server.py"
else
  echo "first install: no previous server.py to back up"
fi
cp "$SRC_DIR/server.py" "$DEST/server.py"

if [ -f "$DEST/.env" ]; then
  echo ".env exists; token left untouched"
else
  TOKEN="${FACTOTUM_GATEWAY_TOKEN:?set FACTOTUM_GATEWAY_TOKEN to the gateway token for this host}"
  umask 077
  printf 'GATEWAY_TOKEN=%s\nGATEWAY_BIND=%s\nGATEWAY_PORT=7902\nGATEWAY_HOST=%s\n' \
    "$TOKEN" "$BIND" "$HOST_LABEL" > "$DEST/.env"
  chmod 600 "$DEST/.env"
  echo ".env created (0600) from FACTOTUM_GATEWAY_TOKEN"
fi

mkdir -p "$HOME/Library/Application Support/FactotumGateway"

sed -e "s|__PYTHON__|$PYTHON_BIN|g" -e "s|__HOME__|$HOME|g" \
  "$SRC_DIR/install/io.example.factotum-gateway.plist" \
  > "$HOME/Library/LaunchAgents/io.example.factotum-gateway.plist"

UID_NOW="$(id -u)"
launchctl bootout "gui/$UID_NOW/io.example.factotum-gateway" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NOW" \
  "$HOME/Library/LaunchAgents/io.example.factotum-gateway.plist"
launchctl kickstart -k "gui/$UID_NOW/io.example.factotum-gateway"
echo "installed and kickstarted io.example.factotum-gateway (bind $BIND:7902)"
