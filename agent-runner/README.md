# Homing local Claude runner (superseded)

This hand-rolled runner — a local launchd job that started Claude Code non-interactively against
a hardened StreetEasy MCP, with the Homing bearer token loaded from a private environment file —
is superseded by the **agent kit**.

`run-claude-search.sh`, `search-prompt.md`, and `com.homing.local-search.plist.example` are
removed. Use the kit instead: copy the setup instruction from Homing's UI to your assistant, or
fetch `https://<your-homing-origin>/agent/` directly. It installs itself, pairs to your account
through an approval code instead of a token you have to create and store by hand, and schedules a
lean recurring check on its own.

See [docs/agent-api.md](../docs/agent-api.md) and [docs/architecture.md](../docs/architecture.md)
for how the kit works.
