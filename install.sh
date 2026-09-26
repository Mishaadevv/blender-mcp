#!/usr/bin/env bash
# One-line installer: curl -sSL https://raw.githubusercontent.com/Mishaadevv/blender-mcp/main/install.sh | bash -s -- --all
set -e
REPO="${BLENDER_MCP_REPO:-Mishaadevv/blender-mcp}"
URL="https://raw.githubusercontent.com/${REPO}/main/install.py"
TMP="$(mktemp /tmp/blender-mcp-install.XXXXXX.py)"
echo "[blender-mcp] downloading ${URL}"
if command -v curl >/dev/null; then curl -sSL "$URL" -o "$TMP";
elif command -v wget >/dev/null; then wget -qO "$TMP" "$URL";
else echo "need curl or wget"; exit 1; fi
exec python3 "$TMP" "$@"
