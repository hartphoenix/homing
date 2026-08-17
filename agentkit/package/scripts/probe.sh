#!/bin/sh
# probe.sh — homing-agent-kit environment probe.
#
# Read-only. Mutates nothing it does not remove again. Never prints a secret value.
# Emits exactly one JSON object on stdout; diagnostics go to stderr.
#
# Read references/probe.md before interpreting the output. Every capability is
# have | denied | absent, never a boolean, and every failure carries a `signature`
# naming which of the four refusal layers produced it.

set -eu

SCHEMA=1
CONTROL_URL="https://example.com"
HOMING_ORIGIN="__HOMING_ORIGIN__"
EGRESS_URL="https://ifconfig.co/json"
NET_TIMEOUT=4
DO_NETWORK=1
EXTRA_TARGETS=""
PROBE_TAG=".homing-wprobe-$$"

usage() {
    cat <<'EOF'
probe.sh — one read-only look at this machine, as one JSON object on stdout.

Usage:
  sh probe.sh [--no-network] [--target URL]... [--timeout SECONDS] [--help]

Options:
  --no-network      Skip every HTTP request. The network and homing blocks then
                    report verdict "skipped". Use only when egress is known-absent.
  --target URL      Probe one extra https URL and add it to network.targets.
                    Repeatable. https only.
  --timeout N       Per-request timeout in seconds (default 4).
  -h, --help        This text.

Guarantees:
  * HTTP only. No DNS, no ping, no raw sockets, and the ambient proxy is never
    stripped -- stripping it manufactures "Could not resolve host".
  * No secret value is ever printed. Sensitive variables report presence only,
    and URL userinfo is stripped before anything else.
  * Writability is measured by touch-probe and the probe file is removed again.
  * Nothing aborts the run: a step that fails records an entry in `errors` and
    the probe continues.

Typical runtime is 3-10 seconds. Exit status is 0 whenever JSON was written.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --no-network) DO_NETWORK=0 ;;
        --target)
            shift
            if [ $# -eq 0 ]; then echo "probe.sh: --target needs a URL" >&2; exit 2; fi
            EXTRA_TARGETS="$EXTRA_TARGETS $1" ;;
        --timeout)
            shift
            if [ $# -eq 0 ]; then echo "probe.sh: --timeout needs a number" >&2; exit 2; fi
            NET_TIMEOUT="$1" ;;
        *) echo "probe.sh: unknown option $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

# --------------------------------------------------------------------------
# JSON helpers. No jq dependency: jq is one of the things we are looking for.
# --------------------------------------------------------------------------

# jesc <string> -> the same string, safe between double quotes.
jesc() {
    printf '%s' "${1-}" \
        | tr -d '\001-\011\013-\037\177' \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
        | tr '\n' ' '
}

# js <string> -> a JSON string literal.
js() { printf '"%s"' "$(jesc "${1-}")"; }

# jadd <buffer-var-name> <fragment> — append one comma-separated fragment.
jadd() {
    _jb_cur=""
    eval "_jb_cur=\${$1-}"
    if [ -z "$_jb_cur" ]; then eval "$1=\$2"; else eval "$1=\"\$_jb_cur,\$2\""; fi
}

jkv()   { jadd "$1" "$(js "$2"):$(js "$3")"; }   # string value
jkraw() { jadd "$1" "$(js "$2"):$3"; }           # already-JSON value

ERRORS=""
note_error() { # <step> <signature> <detail>
    jadd ERRORS "{$(js step):$(js "$1"),$(js signature):$(js "$2"),$(js detail):$(js "$3")}"
}

# classify_err <stderr-text> -> one of the four refusal signatures.
#   harness_deny  the agent harness refused before the OS saw the command
#   sandbox_deny  a sandbox blocked a binary that exists and works outside it
#   os_eperm      the OS refused this path or operation
#   (http status is classified by the caller, from the code itself)
classify_err() {
    case "${1-}" in
        *"Permission to use"*|*"has been denied"*|*"requires approval"*) printf 'harness_deny' ;;
        *": operation not permitted"*)                                   printf 'sandbox_deny' ;;
        *"Operation not permitted"*|*"Permission denied"*|*EACCES*|*EPERM*) printf 'os_eperm' ;;
        # A sandboxed keychain read still exits 44 ("item not found") while
        # printing this. Absent and denied are different facts; do not collapse.
        *"Module Directory Service error"*|*"Unable to obtain authorization"*) printf 'os_eperm' ;;
        *"not found"*|*"No such file"*)                                   printf 'not_found' ;;
        "")                                                               printf 'none' ;;
        *)                                                                printf 'unknown' ;;
    esac
}

TMPERR="${TMPDIR:-/tmp}/homing-probe-$$.err"
if ! : >"$TMPERR" 2>/dev/null; then TMPERR=/dev/null; fi
cleanup() { rm -f "$TMPERR" 2>/dev/null || true; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM HUP

RUN_STATUS=0
RUN_ERR=""
RUN_OUT=""

# run_quiet <cmd...> — discard stdout, keep a little stderr, never abort.
run_quiet() {
    RUN_STATUS=0; RUN_ERR=""; RUN_OUT=""
    : >"$TMPERR" 2>/dev/null || true
    "$@" >/dev/null 2>"$TMPERR" || RUN_STATUS=$?
    RUN_ERR="$(head -n 5 "$TMPERR" 2>/dev/null || true)"
    return 0
}

# run_capture <cmd...> — like run_quiet but stdout lands in RUN_OUT.
run_capture() {
    RUN_STATUS=0; RUN_ERR=""; RUN_OUT=""
    : >"$TMPERR" 2>/dev/null || true
    RUN_OUT="$("$@" 2>"$TMPERR")" || RUN_STATUS=$?
    RUN_ERR="$(head -n 5 "$TMPERR" 2>/dev/null || true)"
    return 0
}

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
START_S="$(date +%s 2>/dev/null || echo 0)"

# --------------------------------------------------------------------------
# host
# --------------------------------------------------------------------------

UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
UNAME_M="$(uname -m 2>/dev/null || echo unknown)"
OS_ID=unknown
OS_VERSION=""

case "$UNAME_S" in
    Darwin)
        OS_ID=macos
        run_capture sw_vers -productVersion
        if [ "$RUN_STATUS" -eq 0 ]; then OS_VERSION="$RUN_OUT"; fi
        ;;
    Linux)
        OS_ID=linux
        if [ -r /etc/os-release ]; then
            OS_VERSION="$(sed -n 's/^PRETTY_NAME="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' /etc/os-release 2>/dev/null | head -1)"
        fi
        ;;
    CYGWIN*|MINGW*|MSYS*) OS_ID=windows ;;
esac
if [ -z "$OS_VERSION" ]; then OS_VERSION="$(uname -r 2>/dev/null || echo unknown)"; fi

CONTAINER=none
if [ -f /.dockerenv ]; then CONTAINER=docker; fi
if [ -f /run/.containerenv ]; then CONTAINER=podman; fi
if [ "$CONTAINER" = none ] && [ -r /proc/1/cgroup ]; then
    case "$(cat /proc/1/cgroup 2>/dev/null || true)" in
        *docker*)   CONTAINER=docker ;;
        *kubepods*) CONTAINER=kubernetes ;;
        *lxc*)      CONTAINER=lxc ;;
    esac
fi
if [ -r /proc/version ]; then
    case "$(cat /proc/version 2>/dev/null || true)" in
        *Microsoft*|*microsoft*) CONTAINER=wsl ;;
    esac
fi

HOME_DIR="${HOME:-}"
if [ -z "$HOME_DIR" ]; then HOME_DIR="$(cd ~ 2>/dev/null && pwd -P 2>/dev/null || echo /)"; fi

USER_NAME="${USER:-${LOGNAME:-}}"
if [ -z "$USER_NAME" ]; then
    run_capture id -un
    if [ "$RUN_STATUS" -eq 0 ]; then USER_NAME="$RUN_OUT"; fi
fi
if [ -z "$USER_NAME" ]; then USER_NAME=unknown; fi

run_capture id -u
UID_NUM="$RUN_OUT"
case "$UID_NUM" in ''|*[!0-9]*) UID_NUM=0 ;; esac

if [ -t 1 ]; then TTY_STATE=have; else TTY_STATE=absent; fi

# GUI login session. Presence only; never a login attempt.
GUI_SESSION=unknown
if [ "$OS_ID" = macos ]; then
    if command -v launchctl >/dev/null 2>&1; then
        run_quiet launchctl print-disabled "gui/$UID_NUM"
        if [ "$RUN_STATUS" -eq 0 ]; then
            GUI_SESSION=have
        else
            case "$(classify_err "$RUN_ERR")" in
                harness_deny|sandbox_deny) GUI_SESSION=denied ;;
                *)                         GUI_SESSION=absent ;;
            esac
        fi
    fi
elif [ "$OS_ID" = linux ]; then
    if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
        GUI_SESSION=have
    elif [ -n "${XDG_SESSION_TYPE:-}" ]; then
        case "$XDG_SESSION_TYPE" in
            x11|wayland) GUI_SESSION=have ;;
            *)           GUI_SESSION=absent ;;
        esac
    else
        GUI_SESSION=absent
    fi
fi

HOSTNAME_CLASS=unknown
if [ "$CONTAINER" != none ]; then
    HOSTNAME_CLASS=container
elif [ -n "${CI:-}${GITHUB_ACTIONS:-}${GITLAB_CI:-}${BUILDKITE:-}" ]; then
    HOSTNAME_CLASS=ci
elif [ "$GUI_SESSION" = have ]; then
    HOSTNAME_CLASS=personal
elif [ "$OS_ID" = linux ] && [ "$GUI_SESSION" = absent ]; then
    HOSTNAME_CLASS=server
fi

HOST_BUF=""
jkv HOST_BUF os "$OS_ID"
jkv HOST_BUF os_raw "$UNAME_S"
jkv HOST_BUF version "$OS_VERSION"
jkv HOST_BUF arch "$UNAME_M"
jkv HOST_BUF container "$CONTAINER"
jkv HOST_BUF hostname_class "$HOSTNAME_CLASS"
jkv HOST_BUF gui_session "$GUI_SESSION"
jkv HOST_BUF home "$HOME_DIR"
jkv HOST_BUF user "$USER_NAME"
jkv HOST_BUF shell "${SHELL:-unknown}"

# --------------------------------------------------------------------------
# runtime identity — environment names first, then config dirs, then binaries.
# Never process ancestry: `ps` is blocked in exactly the sandboxes where runtime
# detection matters, and it cannot see past a PATH shim anyway.
# --------------------------------------------------------------------------

# Enumerate variable NAMES only. Values are then read one at a time, by name,
# from a fixed list -- so no value can leak by accident.
ENVNAMES=""
if [ -x /usr/bin/env ]; then
    ENVNAMES="$(/usr/bin/env 2>/dev/null | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*$/\1/p' | sort -u)" || ENVNAMES=""
fi
if [ -z "$ENVNAMES" ]; then
    ENVNAMES="$(env 2>/dev/null | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*$/\1/p' | sort -u)" || ENVNAMES=""
fi
if [ -z "$ENVNAMES" ]; then
    note_error env_enumerate unknown "could not list environment variable names; runtime identity falls back to config dirs"
fi

env_has() { printf '%s\n' "$ENVNAMES" | grep -qx -- "$1" 2>/dev/null; }
env_val() { eval "printf '%s' \"\${$1-}\"" 2>/dev/null || printf ''; }

# Values are emitted only for names on this list, and only after redaction.
SAFE_ENV="CLAUDECODE CLAUDE_CODE_ENTRYPOINT AI_AGENT SANDBOX_RUNTIME CODEX_SANDBOX \
XDG_SESSION_TYPE CI GITHUB_ACTIONS CONTAINER"

# These are reported as presence only. Their values never appear anywhere.
SIGNAL_ENV="CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_EXECPATH CLAUDE_CODE_CHILD_SESSION \
AI_AGENT SANDBOX_RUNTIME ANTHROPIC_API_KEY CODEX_HOME CODEX_SANDBOX OPENAI_API_KEY \
GEMINI_API_KEY GOOGLE_API_KEY CURSOR_TRACE_ID CURSOR_AGENT WINDSURF_SESSION AIDER_MODEL \
GH_TOKEN GITHUB_TOKEN OLLAMA_HOST CI GITHUB_ACTIONS \
HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy"

is_safe_env() {
    for _s in $SAFE_ENV; do
        if [ "$_s" = "$1" ]; then return 0; fi
    done
    return 1
}

# URL userinfo first -- live proxy credentials hide there and no KEY|TOKEN regex
# catches them -- then truncate.
redact_value() {
    printf '%s' "${1-}" \
        | sed -e 's#://[^:/@]*:[^@]*@#://<redacted>@#g' \
        | cut -c1-120
}

SIGNALS_BUF=""
for _n in $SIGNAL_ENV; do
    if env_has "$_n"; then
        if is_safe_env "$_n"; then
            jadd SIGNALS_BUF "{$(js name):$(js "$_n"),$(js state):$(js set),$(js value):$(js "$(redact_value "$(env_val "$_n")")")}"
        else
            jadd SIGNALS_BUF "{$(js name):$(js "$_n"),$(js state):$(js set)}"
        fi
    fi
done

# Config-directory markers. These answer "what else is on this machine" and only
# weakly "who am I". A `for` loop, never a multi-path glob: zsh aborts the whole
# command when any glob misses and a careless reader concludes "nothing exists".
DIR_MARKERS=".claude .codex .gemini .cursor .windsurf .aider.conf.yml .continue \
.copilot .ollama .lmstudio .config/goose .config/opencode .crush .roo .cline .zed"
DIRS_BUF=""
DIRS_FOUND=""
for _d in $DIR_MARKERS; do
    if [ -e "$HOME_DIR/$_d" ]; then
        jadd DIRS_BUF "$(js "$_d")"
        DIRS_FOUND="$DIRS_FOUND $_d"
    fi
done

RT_ID=unknown
RT_CONF=none
RT_VERSION=""
RT_CANDIDATES=""

if env_has CLAUDECODE || env_has CLAUDE_CODE_ENTRYPOINT; then
    RT_ID=claude-code; RT_CONF=high
elif env_has CODEX_HOME || env_has CODEX_SANDBOX; then
    RT_ID=codex; RT_CONF=high
elif env_has CURSOR_AGENT || env_has CURSOR_TRACE_ID; then
    RT_ID=cursor; RT_CONF=high
elif env_has GEMINI_API_KEY && [ -e "$HOME_DIR/.gemini" ]; then
    RT_ID=gemini-cli; RT_CONF=low
fi

if env_has AI_AGENT; then
    RT_VERSION="$(redact_value "$(env_val AI_AGENT)")"
    if [ "$RT_CONF" = none ]; then RT_CONF=low; fi
fi

for _d in $DIRS_FOUND; do
    case "$_d" in
        .claude)          jadd RT_CANDIDATES "$(js claude-code)" ;;
        .codex)           jadd RT_CANDIDATES "$(js codex)" ;;
        .gemini)          jadd RT_CANDIDATES "$(js gemini-cli)" ;;
        .cursor)          jadd RT_CANDIDATES "$(js cursor)" ;;
        .windsurf)        jadd RT_CANDIDATES "$(js windsurf)" ;;
        .copilot)         jadd RT_CANDIDATES "$(js copilot-cli)" ;;
        .config/opencode) jadd RT_CANDIDATES "$(js opencode)" ;;
        .config/goose)    jadd RT_CANDIDATES "$(js goose)" ;;
        .cline)           jadd RT_CANDIDATES "$(js cline)" ;;
        .roo)             jadd RT_CANDIDATES "$(js roo)" ;;
    esac
done

if [ "$RT_ID" = unknown ]; then
    if [ -n "$RT_CANDIDATES" ]; then RT_CONF=low; fi
    RT_ID=generic
fi

SANDBOX_SIGNALS=""
SANDBOX_DETECTED=false
if env_has SANDBOX_RUNTIME; then
    SANDBOX_DETECTED=true; jadd SANDBOX_SIGNALS "$(js "env:SANDBOX_RUNTIME")"
fi
if env_has CODEX_SANDBOX; then
    SANDBOX_DETECTED=true; jadd SANDBOX_SIGNALS "$(js "env:CODEX_SANDBOX")"
fi
if [ "$CONTAINER" != none ]; then
    SANDBOX_DETECTED=true; jadd SANDBOX_SIGNALS "$(js "container:$CONTAINER")"
fi

RUNTIME_BUF=""
jkv RUNTIME_BUF id "$RT_ID"
jkv RUNTIME_BUF version "$RT_VERSION"
jkv RUNTIME_BUF confidence "$RT_CONF"
jkraw RUNTIME_BUF candidates "[$RT_CANDIDATES]"
jkraw RUNTIME_BUF signals "[$SIGNALS_BUF]"
jkraw RUNTIME_BUF config_dirs "[$DIRS_BUF]"
jkv RUNTIME_BUF tty "$TTY_STATE"
jkraw RUNTIME_BUF sandbox "{$(js detected):$SANDBOX_DETECTED,$(js signals):[$SANDBOX_SIGNALS]}"

# --------------------------------------------------------------------------
# tools — have / denied / absent, with the path and the refusal signature.
# --------------------------------------------------------------------------

TOOLS="python3 python curl wget git jq node npm npx bun deno uv uvx pipx pip3 \
brew apt-get dnf pacman apk winget perl flock timeout gtimeout unzip zip \
shasum sha256sum openssl base64 sqlite3 \
security secret-tool systemd-creds cmdkey pass op \
launchctl systemctl loginctl crontab schtasks at \
docker gh osascript notify-send plutil \
claude codex gemini cursor ollama"

TOOLS_BUF=""
tool_entry() { # <name> <state> <path> <signature>
    _t="$(js name):$(js "$1"),$(js state):$(js "$2")"
    if [ -n "${3-}" ]; then _t="$_t,$(js path):$(js "$3")"; fi
    if [ -n "${4-}" ]; then _t="$_t,$(js signature):$(js "$4")"; fi
    jadd TOOLS_BUF "{$_t}"
}

for _t in $TOOLS; do
    _p=""
    _p="$(command -v "$_t" 2>/dev/null)" || _p=""
    if [ -n "$_p" ]; then
        case "$_p" in
            # `command -v` can return a bare name for a shell function or alias.
            # Say so rather than handing the caller something it cannot exec.
            /*) tool_entry "$_t" have "$_p" "" ;;
            *)  tool_entry "$_t" have "$_p" shell_builtin_or_alias ;;
        esac
    else
        tool_entry "$_t" absent "" not_found
    fi
done

# A binary on PATH can still be refused, and "refused" is not "missing".
# Probe only the ones whose refusal changes a decision.
probe_denial() { # <name> <cmd...>
    _pd_name="$1"; shift
    if ! command -v "$_pd_name" >/dev/null 2>&1; then return 0; fi
    run_quiet "$@"
    if [ "$RUN_STATUS" -ne 0 ]; then
        _pd_sig="$(classify_err "$RUN_ERR")"
        case "$_pd_sig" in
            harness_deny|sandbox_deny|os_eperm)
                jadd TOOLS_BUF "{$(js name):$(js "$_pd_name"),$(js state):$(js denied),$(js signature):$(js "$_pd_sig"),$(js note):$(js "on PATH but refused for this session; the binary exists")}"
                ;;
        esac
    fi
    return 0
}

probe_denial crontab crontab -l
if [ "$OS_ID" = macos ]; then probe_denial launchctl launchctl print-disabled "gui/$UID_NUM"; fi
if [ "$OS_ID" = linux ]; then probe_denial systemctl systemctl --user is-system-running; fi

# --------------------------------------------------------------------------
# paths — touch-probe every candidate, then clean up after itself.
# A path that does not exist is probed through its nearest existing ancestor:
# the temporary entry is created and immediately removed.
# --------------------------------------------------------------------------

is_synced() {
    case "$1" in
        *"/Library/Mobile Documents"*|*"/Library/CloudStorage"*|*Dropbox*|*OneDrive*|*"Google Drive"*|*Syncthing*|*"/iCloud"*|*pCloud*)
            return 0 ;;
    esac
    return 1
}

PATHS_BUF=""
ANY_WRITABLE=no

probe_path() { # <class> <path>
    _pp_class="$1"; _pp_path="$2"
    _pp_exists=false; _pp_writable=false; _pp_state=absent; _pp_sig=none
    _pp_real="$_pp_path"

    if [ -d "$_pp_path" ]; then
        _pp_exists=true
        _pp_real="$(cd "$_pp_path" 2>/dev/null && pwd -P 2>/dev/null)" || _pp_real="$_pp_path"
        if ( : >"$_pp_path/$PROBE_TAG" ) 2>"$TMPERR"; then
            _pp_writable=true; _pp_state=writable
            rm -f "$_pp_path/$PROBE_TAG" 2>/dev/null || true
        else
            _pp_state=denied
            _pp_sig="$(classify_err "$(head -n 3 "$TMPERR" 2>/dev/null || true)")"
        fi
    elif [ -e "$_pp_path" ]; then
        _pp_exists=true; _pp_state=denied; _pp_sig=not_a_directory
    else
        _pp_anc="$_pp_path"
        while [ -n "$_pp_anc" ] && [ "$_pp_anc" != "/" ] && [ ! -d "$_pp_anc" ]; do
            _pp_anc="$(dirname "$_pp_anc")"
        done
        if [ -d "$_pp_anc" ]; then
            if ( mkdir "$_pp_anc/$PROBE_TAG" ) 2>"$TMPERR"; then
                _pp_writable=true; _pp_state=creatable
                rmdir "$_pp_anc/$PROBE_TAG" 2>/dev/null || true
            else
                _pp_state=denied
                _pp_sig="$(classify_err "$(head -n 3 "$TMPERR" 2>/dev/null || true)")"
            fi
        fi
    fi

    if [ "$_pp_writable" = true ]; then ANY_WRITABLE=yes; fi
    if is_synced "$_pp_real"; then _pp_synced=true; else _pp_synced=false; fi

    jadd PATHS_BUF "{$(js class):$(js "$_pp_class"),$(js path):$(js "$_pp_path"),$(js exists):$_pp_exists,$(js writable):$_pp_writable,$(js state):$(js "$_pp_state"),$(js signature):$(js "$_pp_sig"),$(js synced):$_pp_synced}"
    return 0
}

if [ "$OS_ID" = macos ]; then
    CFG_DEFAULT="$HOME_DIR/Library/Application Support/Homing"
    STATE_DEFAULT="$HOME_DIR/Library/Application Support/Homing/state"
    LOG_DEFAULT="$HOME_DIR/Library/Logs/Homing"
    SCHED_DIR="$HOME_DIR/Library/LaunchAgents"
else
    CFG_DEFAULT="${XDG_CONFIG_HOME:-$HOME_DIR/.config}/homing"
    STATE_DEFAULT="${XDG_STATE_HOME:-$HOME_DIR/.local/state}/homing"
    LOG_DEFAULT="${XDG_STATE_HOME:-$HOME_DIR/.local/state}/homing/logs"
    SCHED_DIR="${XDG_CONFIG_HOME:-$HOME_DIR/.config}/systemd/user"
fi

probe_path skill     "$HOME_DIR/.agents/skills"
probe_path skill     "$HOME_DIR/.claude/skills"
probe_path skill     "$HOME_DIR/.codex/skills"
probe_path skill     "$HOME_DIR/.gemini/skills"
probe_path skill     "$PWD/.claude/skills"
probe_path scheduler "$SCHED_DIR"
probe_path config    "$CFG_DEFAULT"
probe_path state     "$STATE_DEFAULT"
probe_path logs      "$LOG_DEFAULT"

# --------------------------------------------------------------------------
# capabilities
# --------------------------------------------------------------------------

CAP_SHELL=have
CAP_SUBPROCESS=have
CAP_BACKGROUND=have
CAP_WEB_FETCH=absent
HTTP_CLIENT=none

if command -v curl >/dev/null 2>&1; then
    HTTP_CLIENT=curl; CAP_WEB_FETCH=have
elif command -v wget >/dev/null 2>&1; then
    HTTP_CLIENT=wget; CAP_WEB_FETCH=have
elif command -v python3 >/dev/null 2>&1; then
    HTTP_CLIENT=python3; CAP_WEB_FETCH=have
fi

if [ "$ANY_WRITABLE" = yes ]; then CAP_FILE_WRITE=have; else CAP_FILE_WRITE=denied; fi

# --------------------------------------------------------------------------
# scheduler — durability is the only property that matters. A session-scoped
# cron tool is not a scheduler.
# --------------------------------------------------------------------------

SCHED_BUF=""
sched_entry() { # <kind> <state> <dir> <dir_writable-json> <durable-json> <note>
    jadd SCHED_BUF "{$(js kind):$(js "$1"),$(js state):$(js "$2"),$(js dir):$(js "$3"),$(js dir_writable):$4,$(js durable):$5,$(js note):$(js "${6-}")}"
}

sched_dir_writable() {
    if [ -d "$1" ] && ( : >"$1/$PROBE_TAG" ) 2>/dev/null; then
        rm -f "$1/$PROBE_TAG" 2>/dev/null || true
        printf 'true'
    else
        printf 'false'
    fi
}

if [ "$OS_ID" = macos ]; then
    if command -v launchctl >/dev/null 2>&1; then
        sched_entry launchd have "$SCHED_DIR" "$(sched_dir_writable "$SCHED_DIR")" true \
            "LaunchAgent plus launchctl bootstrap gui/UID; StartCalendarInterval only"
    else
        sched_entry launchd absent "$SCHED_DIR" false true ""
    fi
    # crontab exists on macOS and is deliberately never used: the setuid binary
    # blocks on a TCC dialog that an unattended installer can never answer, so
    # the install hangs forever rather than failing.
    sched_entry cron absent "" false false "never used on macOS; the installer must not touch crontab here"
elif [ "$OS_ID" = linux ]; then
    if command -v systemctl >/dev/null 2>&1; then
        run_quiet systemctl --user is-system-running
        _ss=have
        if [ "$RUN_STATUS" -ne 0 ]; then
            case "$(classify_err "$RUN_ERR")" in
                harness_deny|sandbox_deny|os_eperm) _ss=denied ;;
                *) _ss=have ;;   # "degraded"/"starting" exit non-zero and are fine
            esac
        fi
        sched_entry systemd-user "$_ss" "$SCHED_DIR" "$(sched_dir_writable "$SCHED_DIR")" true \
            "user timer plus loginctl enable-linger; overlap prevention is free"
    else
        sched_entry systemd-user absent "$SCHED_DIR" false true ""
    fi
    if command -v crontab >/dev/null 2>&1; then
        sched_entry cron have "" false true "only when systemd is absent; no catch-up, minimal environment"
    else
        sched_entry cron absent "" false true ""
    fi
elif [ "$OS_ID" = windows ]; then
    if command -v schtasks >/dev/null 2>&1; then
        sched_entry schtasks have "" true true "S4U, RunLevel Limited, MultipleInstances IgnoreNew"
    else
        sched_entry schtasks absent "" false true ""
    fi
fi

# --------------------------------------------------------------------------
# secret_store — presence of the binary is the test. A failed read under a
# sandbox is not evidence that the store is missing.
# --------------------------------------------------------------------------

SECRET_BUF=""
secret_entry() { # <kind> <binary> <state> <note>
    jadd SECRET_BUF "{$(js kind):$(js "$1"),$(js binary):$(js "$2"),$(js state):$(js "$3"),$(js note):$(js "${4-}")}"
}

if [ "$OS_ID" = macos ]; then
    if [ -x /usr/bin/security ]; then
        secret_entry macos-keychain /usr/bin/security have \
            "write and read with /usr/bin/security only; never a language keyring library"
    else
        secret_entry macos-keychain "" absent ""
    fi
fi
if command -v secret-tool >/dev/null 2>&1; then
    if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
        _st_note="session bus present"
    else
        _st_note="no session bus; unusable from a scheduled job"
    fi
    secret_entry libsecret "$(command -v secret-tool 2>/dev/null || true)" have "$_st_note"
fi
if command -v systemd-creds >/dev/null 2>&1; then
    secret_entry systemd-creds "$(command -v systemd-creds 2>/dev/null || true)" have \
        "best Linux answer for a scheduled job"
fi
if command -v cmdkey >/dev/null 2>&1; then
    secret_entry windows-dpapi "$(command -v cmdkey 2>/dev/null || true)" have ""
fi
if command -v op >/dev/null 2>&1; then
    secret_entry 1password "$(command -v op 2>/dev/null || true)" have \
        "only when already signed in and unattended access is already configured"
fi
secret_entry file-0600 "" have \
    "always available; a 0600 file in a 0700 dir outside every synced folder"

# --------------------------------------------------------------------------
# mcp — every runtime's config found, not just this one. A sibling runtime's
# server is often the most useful capability on the box.
# --------------------------------------------------------------------------

MCP_BUF=""
mcp_entry() {
    jadd MCP_BUF "{$(js runtime):$(js "$1"),$(js server):$(js "$2"),$(js transport):$(js "${3:-unknown}"),$(js config):$(js "$4")}"
}

_cc_json="$HOME_DIR/.claude.json"
if [ -r "$_cc_json" ] && command -v python3 >/dev/null 2>&1; then
    run_capture python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
names = set(d.get("mcpServers") or {})
for p in (d.get("projects") or {}).values():
    if isinstance(p, dict):
        names |= set(p.get("mcpServers") or {})
print("\n".join(sorted(n for n in names if n)))' "$_cc_json"
    if [ "$RUN_STATUS" -eq 0 ] && [ -n "$RUN_OUT" ]; then
        while IFS= read -r _s; do
            if [ -n "$_s" ]; then mcp_entry claude-code "$_s" unknown "$_cc_json"; fi
        done <<MCPEOF
$RUN_OUT
MCPEOF
    fi
fi

_cx_toml="$HOME_DIR/.codex/config.toml"
if [ -r "$_cx_toml" ]; then
    _servers="$(sed -n 's/^\[mcp_servers\.\([^]]*\)\].*$/\1/p' "$_cx_toml" 2>/dev/null || true)"
    for _s in $_servers; do mcp_entry codex "$_s" stdio "$_cx_toml"; done
fi

for _mcpcfg in "$HOME_DIR/.cursor/mcp.json" "$HOME_DIR/.gemini/settings.json"; do
    if [ -r "$_mcpcfg" ] && grep -q '"mcpServers"' "$_mcpcfg" 2>/dev/null; then
        mcp_entry "$(basename "$(dirname "$_mcpcfg")")" "(configured)" unknown "$_mcpcfg"
    fi
done

# --------------------------------------------------------------------------
# network — HTTP only, always through the ambient proxy, always paired with the
# control URL. Control ok + target fails means the site. Control fails too means
# you. Never interpret a target without the control.
# --------------------------------------------------------------------------

PROXY_STATE=absent
PROXY_VARS=""
PROXY_ENDPOINT=""
for _pv in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy; do
    if env_has "$_pv"; then
        PROXY_STATE=have
        jadd PROXY_VARS "$(js "$_pv")"
        if [ -z "$PROXY_ENDPOINT" ]; then PROXY_ENDPOINT="$(redact_value "$(env_val "$_pv")")"; fi
    fi
done
NO_PROXY_SET=false
if env_has NO_PROXY || env_has no_proxy; then NO_PROXY_SET=true; fi

HTTP_CODE=000
HTTP_SIG=none
HONEST_UA="HomingAgent/1.0 (+$HOMING_ORIGIN/agent/; user-directed housing search for one person)"

http_status() { # <url> -> sets HTTP_CODE and HTTP_SIG
    HTTP_CODE=000
    HTTP_SIG=none
    case "$HTTP_CLIENT" in
        curl)
            run_capture curl -sS -o /dev/null -w '%{http_code}' \
                --max-time "$NET_TIMEOUT" --connect-timeout "$NET_TIMEOUT" \
                --proto '=https' --max-redirs 3 -A "$HONEST_UA" "$1"
            HTTP_CODE="$RUN_OUT"
            if [ "$RUN_STATUS" -ne 0 ]; then HTTP_SIG="$(classify_err "$RUN_ERR")"; fi
            ;;
        wget)
            run_quiet wget -q -S -O /dev/null --timeout="$NET_TIMEOUT" --tries=1 \
                -U "$HONEST_UA" "$1"
            HTTP_CODE="$(printf '%s\n' "$RUN_ERR" | sed -n 's#^[[:space:]]*HTTP/[0-9.]* \([0-9][0-9][0-9]\).*#\1#p' | tail -1)"
            if [ "$RUN_STATUS" -ne 0 ] && [ -z "$HTTP_CODE" ]; then HTTP_SIG="$(classify_err "$RUN_ERR")"; fi
            ;;
        python3)
            run_capture python3 -c 'import sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1], headers={"User-Agent": sys.argv[3]})
try:
    with urllib.request.urlopen(req, timeout=float(sys.argv[2])) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")' "$1" "$NET_TIMEOUT" "$HONEST_UA"
            HTTP_CODE="$RUN_OUT"
            ;;
        *)
            HTTP_SIG=no_http_client ;;
    esac
    case "$HTTP_CODE" in ''|*[!0-9]*) HTTP_CODE=000 ;; esac
    return 0
}

TARGETS_BUF=""
CONTROL_CODE=000
CONTROL_SIG=skipped
EGRESS_CLASS=unknown
EGRESS_ORG=""
EGRESS_CITY=""
EGRESS_COUNTRY=""
HOMING_CODE=000
HOMING_SIG=skipped
HOMING_REACHABLE=false
NET_VERDICT=skipped

if [ "$HTTP_CLIENT" = none ]; then
    NET_VERDICT=no_http_client
    note_error network no_http_client "no curl, wget, or python3 on PATH; nothing was probed"
elif [ "$DO_NETWORK" -eq 1 ]; then
    http_status "$CONTROL_URL"
    CONTROL_CODE="$HTTP_CODE"; CONTROL_SIG="$HTTP_SIG"

    http_status "$HOMING_ORIGIN/api/v1/me/projects"
    HOMING_CODE="$HTTP_CODE"; HOMING_SIG="$HTTP_SIG"
    case "$HOMING_CODE" in
        2*|3*|401|403) HOMING_REACHABLE=true ;;
    esac
    jadd TARGETS_BUF "{$(js url):$(js "$HOMING_ORIGIN/api/v1/me/projects"),$(js http):$(js "$HOMING_CODE"),$(js signature):$(js "$HOMING_SIG"),$(js role):$(js homing)}"

    for _u in $EXTRA_TARGETS; do
        http_status "$_u"
        jadd TARGETS_BUF "{$(js url):$(js "$_u"),$(js http):$(js "$HTTP_CODE"),$(js signature):$(js "$HTTP_SIG"),$(js role):$(js extra)}"
    done

    # Egress class is a politeness input, never a permission slip.
    if [ "$HTTP_CLIENT" = curl ]; then
        run_capture curl -sS --max-time "$NET_TIMEOUT" --connect-timeout "$NET_TIMEOUT" "$EGRESS_URL"
        if [ "$RUN_STATUS" -eq 0 ] && [ -n "$RUN_OUT" ]; then
            EGRESS_ORG="$(printf '%s' "$RUN_OUT" | sed -n 's/.*"asn_org"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
            EGRESS_CITY="$(printf '%s' "$RUN_OUT" | sed -n 's/.*"city"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
            EGRESS_COUNTRY="$(printf '%s' "$RUN_OUT" | sed -n 's/.*"country"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
        fi
    fi
    case "$EGRESS_ORG" in
        '') EGRESS_CLASS=unknown ;;
        *Amazon*|*AWS*|*Google*|*Microsoft*|*Azure*|*Hetzner*|*DigitalOcean*|*OVH*|*Linode*|*Akamai*|*Cloudflare*|*Oracle*|*Vultr*|*Scaleway*|*Contabo*|*Alibaba*|*Tencent*|*Equinix*|*Leaseweb*|*M247*|*Choopa*|*Datacamp*|*Hosting*|*Datacenter*)
            EGRESS_CLASS=datacenter ;;
        *)  if [ -n "$EGRESS_CITY" ]; then EGRESS_CLASS=residential; else EGRESS_CLASS=unknown; fi ;;
    esac

    # Name the layer. This is the single most misread signal in the whole probe.
    if [ "$CONTROL_SIG" = harness_deny ] || [ "$CONTROL_SIG" = sandbox_deny ]; then
        NET_VERDICT=denied
    elif [ "$CONTROL_CODE" = 000 ] && [ "$HOMING_CODE" = 000 ]; then
        NET_VERDICT=no_egress
    elif [ "$CONTROL_CODE" != 000 ] && [ "$HOMING_CODE" = 000 ]; then
        NET_VERDICT=homing_unreachable
    elif [ "$HOMING_REACHABLE" = true ]; then
        NET_VERDICT=ok
    else
        NET_VERDICT=homing_error
    fi
fi

NET_BUF=""
jkraw NET_BUF control "{$(js url):$(js "$CONTROL_URL"),$(js http):$(js "$CONTROL_CODE"),$(js signature):$(js "$CONTROL_SIG")}"
jkraw NET_BUF targets "[$TARGETS_BUF]"
jkraw NET_BUF egress "{$(js class):$(js "$EGRESS_CLASS"),$(js asn_org):$(js "$EGRESS_ORG"),$(js city):$(js "$EGRESS_CITY"),$(js country):$(js "$EGRESS_COUNTRY")}"
jkraw NET_BUF proxy "{$(js state):$(js "$PROXY_STATE"),$(js vars):[$PROXY_VARS],$(js endpoint):$(js "$PROXY_ENDPOINT"),$(js no_proxy_set):$NO_PROXY_SET}"
jkv   NET_BUF client "$HTTP_CLIENT"
jkv   NET_BUF verdict "$NET_VERDICT"

HOMING_BUF=""
jkv   HOMING_BUF origin "$HOMING_ORIGIN"
jkv   HOMING_BUF http "$HOMING_CODE"
jkraw HOMING_BUF reachable "$HOMING_REACHABLE"
jkv   HOMING_BUF signature "$HOMING_SIG"

# --------------------------------------------------------------------------
# prior_install — the phase that collapses the question count.
# --------------------------------------------------------------------------

PI_FOUND=false
PI_SCHED=""
PI_STATE=""
PI_SECRET=unknown
PI_LAST_RUN=""
PI_LOCK=""
PI_MANIFEST=""

for _sd in "$CFG_DEFAULT" "$STATE_DEFAULT" "$LOG_DEFAULT" "$HOME_DIR/.homing"; do
    if [ -d "$_sd" ]; then
        PI_FOUND=true
        jadd PI_STATE "$(js "$_sd")"
    fi
done

for _mf in "$STATE_DEFAULT/install-manifest.json" "$CFG_DEFAULT/install-manifest.json"; do
    if [ -r "$_mf" ] && [ -z "$PI_MANIFEST" ]; then
        PI_FOUND=true
        PI_MANIFEST="$_mf"
    fi
done

if [ -r "$STATE_DEFAULT/last-run.json" ]; then
    PI_LAST_RUN="$(sed -n 's/.*"at"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$STATE_DEFAULT/last-run.json" 2>/dev/null | head -1)"
fi

# A lock older than twice the expected run time is stale. A job that exits 0 on
# a stale lock looks healthy while doing nothing at all -- observed in the wild,
# silently disabling a real daily job for a day.
for _lk in "$STATE_DEFAULT/run.lock" "$CFG_DEFAULT/run.lock"; do
    if [ -e "$_lk" ]; then
        PI_FOUND=true
        _stale=false
        run_capture find "$_lk" -maxdepth 0 -mmin +120
        if [ "$RUN_STATUS" -eq 0 ] && [ -n "$RUN_OUT" ]; then _stale=true; fi
        jadd PI_LOCK "{$(js path):$(js "$_lk"),$(js stale_suspect):$_stale}"
    fi
done

if [ "$OS_ID" = macos ]; then
    for _pl in "$HOME_DIR/Library/LaunchAgents"/*homing*.plist "$HOME_DIR/Library/LaunchAgents"/*Homing*.plist; do
        if [ -e "$_pl" ]; then
            PI_FOUND=true
            _lbl="$(sed -n 's#.*<string>\([A-Za-z0-9._-]*homing[A-Za-z0-9._-]*\)</string>.*#\1#p' "$_pl" 2>/dev/null | head -1)"
            if [ -z "$_lbl" ]; then _lbl="$(basename "$_pl" .plist)"; fi
            jadd PI_SCHED "{$(js kind):$(js launchd),$(js label):$(js "$_lbl"),$(js path):$(js "$_pl"),$(js source):$(js file)}"
        fi
    done
elif [ "$OS_ID" = linux ]; then
    for _un in "$SCHED_DIR"/*homing*; do
        if [ -e "$_un" ]; then
            PI_FOUND=true
            jadd PI_SCHED "{$(js kind):$(js systemd-user),$(js label):$(js "$(basename "$_un")"),$(js path):$(js "$_un"),$(js source):$(js file)}"
        fi
    done
fi

# Secret-store item presence, never its value. `find-generic-password` without
# -w prints metadata only, and a sandbox refusal is recorded as denied, never as
# absent: the scheduled job runs outside the sandbox and reads the store fine.
if [ "$OS_ID" = macos ] && [ -x /usr/bin/security ]; then
    PI_SECRET=absent
    for _svc in homing-api-token com.homing.agent-token; do
        if [ "$PI_SECRET" = present ]; then continue; fi
        run_quiet /usr/bin/security find-generic-password -a "$USER_NAME" -s "$_svc"
        _sec_sig="$(classify_err "$RUN_ERR")"
        if [ "$RUN_STATUS" -eq 0 ]; then
            PI_SECRET=present
        elif [ "$_sec_sig" != none ] && [ "$_sec_sig" != unknown ]; then
            # A sandboxed read exits 44 -- the same code as a genuine miss --
            # while printing a Module Directory Service error. The signature is
            # the only honest discriminator, so trust it over the exit code.
            PI_SECRET=denied
        elif [ "$RUN_STATUS" -ne 44 ]; then
            PI_SECRET=denied
        fi
    done
elif command -v secret-tool >/dev/null 2>&1; then
    run_quiet secret-tool search service homing account api-token
    if [ "$RUN_STATUS" -eq 0 ]; then PI_SECRET=present; else PI_SECRET=absent; fi
fi

PI_BUF=""
jkraw PI_BUF found "$PI_FOUND"
jkraw PI_BUF scheduler_records "[$PI_SCHED]"
jkraw PI_BUF state_dirs "[$PI_STATE]"
jkv   PI_BUF secret_item "$PI_SECRET"
jkv   PI_BUF last_run_at "$PI_LAST_RUN"
jkraw PI_BUF lock "[$PI_LOCK]"
jkv   PI_BUF manifest "$PI_MANIFEST"

# --------------------------------------------------------------------------
# isolation — evidence for the rung, never a claim beyond the evidence.
# --------------------------------------------------------------------------

ISO_RUNG=0
ISO_EV=""
if [ "$SANDBOX_DETECTED" = true ]; then
    ISO_RUNG=1
    jadd ISO_EV "$(js "sandbox signals present")"
fi
_cc_settings="$HOME_DIR/.claude/settings.json"
if [ -r "$_cc_settings" ]; then
    if grep -q '"allowedDomains"' "$_cc_settings" 2>/dev/null; then
        if [ "$ISO_RUNG" -lt 3 ]; then ISO_RUNG=3; fi
        jadd ISO_EV "$(js "an egress allowlist is configured outside the model")"
    fi
    if grep -q '"allowUnsandboxedCommands"[[:space:]]*:[[:space:]]*false' "$_cc_settings" 2>/dev/null; then
        jadd ISO_EV "$(js "unsandboxed commands are disabled")"
    fi
fi
if [ "$CONTAINER" != none ]; then
    if [ "$ISO_RUNG" -lt 5 ]; then ISO_RUNG=5; fi
    jadd ISO_EV "$(js "container: $CONTAINER")"
fi
if [ "$PROXY_STATE" = have ]; then
    jadd ISO_EV "$(js "egress is proxied; a proxy is an enforcement point only when the user controls it")"
fi

ISO_BUF=""
jkraw ISO_BUF rung "$ISO_RUNG"
jkraw ISO_BUF evidence "[$ISO_EV]"

# --------------------------------------------------------------------------
# browser — presence only, never launched.
# --------------------------------------------------------------------------

BROWSER_BUF=""
if [ "$OS_ID" = macos ]; then
    for _b in "Google Chrome" "Safari" "Firefox" "Arc" "Microsoft Edge" "Brave Browser"; do
        if [ -d "/Applications/$_b.app" ]; then jadd BROWSER_BUF "$(js "$_b")"; fi
    done
else
    for _b in google-chrome chromium chromium-browser firefox microsoft-edge brave-browser; do
        if command -v "$_b" >/dev/null 2>&1; then jadd BROWSER_BUF "$(js "$_b")"; fi
    done
fi

# --------------------------------------------------------------------------
# emit
# --------------------------------------------------------------------------

END_S="$(date +%s 2>/dev/null || echo 0)"
DUR=$(( END_S - START_S ))
if [ "$DUR" -lt 0 ]; then DUR=0; fi

printf '{'
printf '%s:%s' "$(js schema)" "$SCHEMA"
printf ',%s:%s' "$(js generated_at)" "$(js "$STARTED_AT")"
printf ',%s:%s' "$(js duration_seconds)" "$DUR"
printf ',%s:{%s}' "$(js host)" "$HOST_BUF"
printf ',%s:{%s}' "$(js runtime)" "$RUNTIME_BUF"
printf ',%s:{%s:%s,%s:%s,%s:%s,%s:%s,%s:%s}' \
    "$(js capabilities)" \
    "$(js shell)"      "$(js "$CAP_SHELL")" \
    "$(js file_write)" "$(js "$CAP_FILE_WRITE")" \
    "$(js web_fetch)"  "$(js "$CAP_WEB_FETCH")" \
    "$(js subprocess)" "$(js "$CAP_SUBPROCESS")" \
    "$(js background)" "$(js "$CAP_BACKGROUND")"
printf ',%s:[%s]' "$(js tools)" "$TOOLS_BUF"
printf ',%s:[%s]' "$(js paths)" "$PATHS_BUF"
printf ',%s:[%s]' "$(js scheduler)" "$SCHED_BUF"
printf ',%s:[%s]' "$(js secret_store)" "$SECRET_BUF"
printf ',%s:[%s]' "$(js mcp)" "$MCP_BUF"
printf ',%s:{%s}' "$(js network)" "$NET_BUF"
printf ',%s:{%s}' "$(js homing)" "$HOMING_BUF"
printf ',%s:{%s}' "$(js prior_install)" "$PI_BUF"
printf ',%s:{%s}' "$(js isolation)" "$ISO_BUF"
printf ',%s:[%s]' "$(js browser)" "$BROWSER_BUF"
printf ',%s:[%s]' "$(js errors)" "$ERRORS"
printf '}\n'

exit 0
