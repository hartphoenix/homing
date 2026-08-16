# Homing local Claude runner

This runner is intended for a Mac with a residential network connection. It
starts Claude Code non-interactively and gives it the hardened local
StreetEasy MCP over stdio. Homing writes go through the HTTPS API.

The runner deliberately keeps the Homing bearer token in the process
environment and never puts it in a command line, prompt file, plist, or log.
Create the token in Homing's **Equip an agent** page, then load it from a
private local secrets file or your password manager before invoking the
runner. Do not commit that file.

## One-time setup

Build the MCP once:

```sh
cd /Users/rhhart/Documents/GitHub/streeteasy-mcp
npm ci --ignore-scripts
npm run build
```

Make a private environment file (mode 600), for example
`~/.config/homing/runner.env`:

```sh
mkdir -p ~/.config/homing
chmod 700 ~/.config/homing
printf 'HOMING_API_TOKEN=replace-with-the-token-from-homing\\n' > ~/.config/homing/runner.env
chmod 600 ~/.config/homing/runner.env
```

Load it only in the invoking shell:

```sh
set -a; . ~/.config/homing/runner.env; set +a
/Users/rhhart/Documents/GitHub/homing/agent-runner/run-claude-search.sh
```

## Dry run

Set `HOMING_DRY_RUN=1` to let Claude inspect projects and StreetEasy without
writing leads or changing runs. The prompt also requires the agent to report a
bounded summary rather than dumping listing contents.

## Scheduling

`com.homing.local-search.plist.example` is a launchd template. Copy it to
`~/Library/LaunchAgents/com.homing.local-search.plist`, replace the two paths,
and use a wrapper or secret-manager command that exports
`HOMING_API_TOKEN` before running this script. launchd does not expand `~` in
paths and should not contain the token itself.
