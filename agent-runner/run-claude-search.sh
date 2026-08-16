#!/bin/zsh
set -euo pipefail

ROOT="/Users/rhhart/Documents/GitHub"
MCP_ROOT="$ROOT/streeteasy-mcp"
MCP_ENTRY="$MCP_ROOT/dist/stdio.js"
PROMPT="$ROOT/homing/agent-runner/search-prompt.md"

: "${HOMING_API_TOKEN:?Set HOMING_API_TOKEN from Homing's Equip an agent page}"
[[ -x "$HOME/.local/bin/claude" ]] && CLAUDE="$HOME/.local/bin/claude" || CLAUDE="$(command -v claude)"
[[ -x "$MCP_ENTRY" ]] || { print -u2 "StreetEasy MCP is not built: $MCP_ENTRY"; exit 1; }

config="$(mktemp -t homing-claude-mcp.XXXXXX.json)"
log_dir="${HOMING_RUN_LOG_DIR:-$HOME/Library/Logs/Homing}"
mkdir -p -m 700 "$log_dir"
trap 'rm -f "$config"' EXIT

cat >"$config" <<EOF
{
  "mcpServers": {
    "streeteasy-local": {
      "type": "stdio",
      "command": "node",
      "args": ["$MCP_ENTRY"]
    }
  }
}
EOF

cd "$ROOT/homing"
exec "$CLAUDE" -p "$(<"$PROMPT")" \
  --strict-mcp-config \
  --mcp-config "$config" \
  --permission-mode dontAsk \
  --tools Bash \
  --output-format text \
  --no-session-persistence \
  >>"$log_dir/latest.log" 2>&1
