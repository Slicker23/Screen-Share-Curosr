#!/usr/bin/env bash
# install.sh — Mac (Apple Silicon) installer for Cursor Phone Bridge.
#
# Mirrors install.ps1 step-for-step but produces Mac-native paths and uses
# launchd for auto-start instead of Windows Scheduled Tasks.
#
# Usage:
#   ./install.sh                          # interactive
#   ./install.sh --install-launchagent --skip-pip
#   ./install.sh --uninstall-launchagent
#   ./install.sh --diagnose               # check everything, change nothing
#   ./install.sh --use-venv               # force a project-local venv
#
# After install, you MUST grant Accessibility permission so the wake script
# can synthesize keystrokes:
#   System Settings > Privacy & Security > Accessibility
#   add the Terminal (or whatever app launches the bridge) and Cursor itself.

set -uo pipefail

# Self-check: if this script was checked out with CRLF endings (Windows
# default), bash will fail with confusing "$'\r': command not found" cascades.
if grep -q $'\r' "$0"; then
    echo "ERR: this script has Windows (CRLF) line endings. Convert to LF:" >&2
    echo "       sed -i '' 's/\\r//g' $0 bridge/wake_cursor.sh" >&2
    echo "  or:  dos2unix $0 bridge/wake_cursor.sh" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_DIR="$REPO_ROOT/bridge"
HOOKS_DIR="$REPO_ROOT/hooks"
CURSOR_DIR="$HOME/.cursor"
HOOKS_JSON_OUT="$CURSOR_DIR/hooks.json"
BRIDGE_INFO_OUT="$CURSOR_DIR/cursor-phone-bridge.json"
LAUNCHAGENT_LABEL="com.user.cursorphonebridge"
LAUNCHAGENT_PLIST="$HOME/Library/LaunchAgents/${LAUNCHAGENT_LABEL}.plist"
LAUNCHAGENT_LOG_DIR="$HOME/Library/Logs/CursorPhoneBridge"
VENV_DIR="$REPO_ROOT/.venv"

SKIP_PIP=0
INSTALL_LA=""        # "" = ask, "1" = yes, "0" = no
UNINSTALL_LA=0
DIAGNOSE_ONLY=0
USE_VENV=0

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-pip) SKIP_PIP=1 ;;
        --install-launchagent) INSTALL_LA=1 ;;
        --no-launchagent) INSTALL_LA=0 ;;
        --uninstall-launchagent) UNINSTALL_LA=1 ;;
        --diagnose) DIAGNOSE_ONLY=1 ;;
        --use-venv) USE_VENV=1 ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

cyan()   { printf '\033[36m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }

# ----------------------------------------------------------------------------
# Cursor.app detection. mdfind -name finds installed apps in any standard
# location (/Applications, ~/Applications, etc.) without us hard-coding paths.
# ----------------------------------------------------------------------------
find_cursor_app() {
    for p in "/Applications/Cursor.app" "$HOME/Applications/Cursor.app"; do
        [ -d "$p" ] && { echo "$p"; return 0; }
    done
    if command -v mdfind >/dev/null 2>&1; then
        local found
        found="$(mdfind 'kMDItemContentType=="com.apple.application-bundle" && kMDItemFSName=="Cursor.app"' 2>/dev/null | head -n 1)"
        if [ -n "$found" ] && [ -d "$found" ]; then
            echo "$found"
            return 0
        fi
    fi
    return 1
}

# ----------------------------------------------------------------------------
# Pick the Python interpreter we'll use for the bridge AND for hooks.
# Resolves to an absolute path via `command -v` so hooks.json doesn't rely on
# whatever PATH Cursor's hook subprocess inherits.
# ----------------------------------------------------------------------------
find_python() {
    local p
    p="$(command -v python3 || true)"
    if [ -z "$p" ]; then
        red "python3 not found in PATH. Install Python 3.11+ (https://python.org or 'brew install python@3.12')."
        return 1
    fi
    # Resolve to absolute path (command -v already returns absolute on Mac for
    # binaries on PATH, but be defensive against shell built-ins).
    case "$p" in
        /*) ;;
        *) p="$(cd "$(dirname "$p")" && pwd)/$(basename "$p")" ;;
    esac
    echo "$p"
}

py_version() {
    "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])'
}

py_version_ok() {
    local v="$1"
    case "$v" in
        3.1[1-9]|3.[2-9][0-9]) return 0 ;;
        *) return 1 ;;
    esac
}

# ----------------------------------------------------------------------------
# Pip install with PEP 668 handling. Newer Pythons (Homebrew, Apple-bundled)
# refuse `pip install --user` with "externally-managed-environment". We catch
# that, retry inside a project-local venv, and switch our PYTHON pointer to
# the venv interpreter.
# ----------------------------------------------------------------------------
do_pip_install() {
    local py="$1"
    local out
    if [ "$USE_VENV" = "1" ]; then
        cyan "Forcing venv at $VENV_DIR (--use-venv)"
    else
        cyan "pip install --user ..."
        if out="$("$py" -m pip install --user -r "$REPO_ROOT/requirements.txt" 2>&1)"; then
            echo "$out" | tail -n 5
            return 0
        fi
        if echo "$out" | grep -qiE "externally-managed-environment|PEP 668"; then
            yellow ""
            yellow "Pip refused --user install (PEP 668). Falling back to a project-local"
            yellow "venv at $VENV_DIR. The bridge daemon and hooks will be wired to use it."
        else
            echo "$out"
            return 1
        fi
    fi

    if [ ! -d "$VENV_DIR" ]; then
        cyan "Creating venv: $py -m venv $VENV_DIR"
        if ! "$py" -m venv "$VENV_DIR"; then
            red "venv creation failed. Try installing python3-venv (Linux) or use Homebrew Python on Mac."
            return 1
        fi
    fi
    local venv_py="$VENV_DIR/bin/python3"
    if [ ! -x "$venv_py" ]; then
        red "venv python missing at $venv_py"
        return 1
    fi
    cyan "Installing deps into venv..."
    "$venv_py" -m pip install --upgrade pip >/dev/null 2>&1 || true
    if ! "$venv_py" -m pip install -r "$REPO_ROOT/requirements.txt"; then
        red "pip install in venv failed"
        return 1
    fi
    # Re-point everything at the venv python so hooks.json + the LaunchAgent
    # both resolve to the interpreter that actually has our deps installed.
    PYTHON3="$venv_py"
    green "Switched PYTHON3 -> $PYTHON3"
    return 0
}

# ----------------------------------------------------------------------------
# Diagnose mode: read-only sweep of the install. Run with --diagnose.
# ----------------------------------------------------------------------------
do_diagnose() {
    cyan "== Cursor Phone Bridge diagnostics =="
    echo ""

    # Python
    if PY="$(find_python)"; then
        VER="$(py_version "$PY" 2>/dev/null || echo "?")"
        if py_version_ok "$VER"; then
            green "  python3:        $PY (v$VER) OK"
        else
            yellow "  python3:        $PY (v$VER) too old (need 3.11+)"
        fi
    else
        red    "  python3:        NOT FOUND"
    fi

    # venv presence
    if [ -x "$VENV_DIR/bin/python3" ]; then
        VENV_VER="$("$VENV_DIR/bin/python3" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "?")"
        green "  venv:           $VENV_DIR (python $VENV_VER)"
    else
        echo "  venv:           none (using system python --user packages if any)"
    fi

    # Cursor.app
    if APP="$(find_cursor_app)"; then
        green "  Cursor.app:     $APP"
    else
        red   "  Cursor.app:     NOT FOUND in /Applications, ~/Applications, or Spotlight"
    fi

    # Cursor process
    PID="$(pgrep -x Cursor 2>/dev/null | head -n 1)"
    if [ -z "$PID" ]; then
        PID="$(pgrep -if 'Cursor.app/Contents/MacOS/Cursor' 2>/dev/null | head -n 1)"
    fi
    if [ -n "$PID" ]; then
        green "  Cursor running: yes (pid $PID)"
    else
        yellow "  Cursor running: NO (open Cursor before the wake script can target it)"
    fi

    # Config
    if [ -f "$BRIDGE_DIR/config.toml" ]; then
        green "  config.toml:    $BRIDGE_DIR/config.toml"
    else
        yellow "  config.toml:    missing (will be created from example on first install)"
    fi

    # Hooks
    if [ -f "$HOOKS_JSON_OUT" ]; then
        if grep -q "{{PYTHON}}" "$HOOKS_JSON_OUT"; then
            red "  hooks.json:     $HOOKS_JSON_OUT exists but contains unrendered {{PYTHON}} placeholder!"
        else
            green "  hooks.json:    $HOOKS_JSON_OUT (rendered)"
        fi
    else
        yellow "  hooks.json:     not yet rendered at $HOOKS_JSON_OUT"
    fi

    # Bridge info
    if [ -f "$BRIDGE_INFO_OUT" ]; then
        green "  bridge info:    $BRIDGE_INFO_OUT"
    else
        yellow "  bridge info:    not yet written at $BRIDGE_INFO_OUT"
    fi

    # Bridge daemon health
    if command -v curl >/dev/null 2>&1; then
        if curl -sf --max-time 2 "http://127.0.0.1:8765/health" >/dev/null 2>&1; then
            green "  bridge daemon:  http://127.0.0.1:8765/health OK"
        else
            yellow "  bridge daemon:  not responding on :8765 (start it: python3 bridge/bridge.py)"
        fi
    fi

    # LaunchAgent
    if [ -f "$LAUNCHAGENT_PLIST" ]; then
        if launchctl print "gui/$(id -u)/${LAUNCHAGENT_LABEL}" >/dev/null 2>&1; then
            green "  LaunchAgent:    loaded ($LAUNCHAGENT_PLIST)"
        else
            yellow "  LaunchAgent:    plist exists but not loaded; run launchctl bootstrap"
        fi
    else
        echo "  LaunchAgent:    none (manual start)"
    fi

    # Wake script
    if [ -x "$BRIDGE_DIR/wake_cursor.sh" ]; then
        green "  wake script:    $BRIDGE_DIR/wake_cursor.sh (executable)"
    else
        yellow "  wake script:    $BRIDGE_DIR/wake_cursor.sh exists but not executable; run chmod +x"
    fi

    # Accessibility (best-effort)
    if [ -n "$PID" ]; then
        ACCESS_TEST="$(WAKE_DRY_RUN=1 bash "$BRIDGE_DIR/wake_cursor.sh" 2>&1 || true)"
        if echo "$ACCESS_TEST" | grep -q "DRYRUN OK"; then
            green "  accessibility:  granted (wake_cursor.sh dry-run OK)"
        elif echo "$ACCESS_TEST" | grep -q "Accessibility permission missing"; then
            red "  accessibility:  NOT GRANTED — open System Settings > Privacy & Security > Accessibility"
        else
            yellow "  accessibility:  could not determine ($ACCESS_TEST)"
        fi
    fi

    echo ""
    cyan "== End of diagnostics =="
}

# ----------------------------------------------------------------------------
# Uninstall LaunchAgent path.
# ----------------------------------------------------------------------------
if [ "$UNINSTALL_LA" = "1" ]; then
    if [ -f "$LAUNCHAGENT_PLIST" ]; then
        launchctl bootout "gui/$(id -u)" "$LAUNCHAGENT_PLIST" 2>/dev/null || true
        rm -f "$LAUNCHAGENT_PLIST"
        green "Removed $LAUNCHAGENT_PLIST"
    else
        yellow "No LaunchAgent installed."
    fi
    exit 0
fi

if [ "$DIAGNOSE_ONLY" = "1" ]; then
    do_diagnose
    exit 0
fi

# ----------------------------------------------------------------------------
# Install path proper.
# ----------------------------------------------------------------------------
cyan "== Cursor Phone Bridge installer (macOS) =="

PYTHON3="$(find_python)" || exit 1
PY_VER="$(py_version "$PYTHON3")"
echo "python3:  $PYTHON3 (v$PY_VER)"
if ! py_version_ok "$PY_VER"; then
    red "Python $PY_VER too old. Need 3.11+."
    exit 1
fi

if [ -x "$VENV_DIR/bin/python3" ] && [ "$USE_VENV" != "1" ] && [ "$SKIP_PIP" = "1" ]; then
    # If a venv already exists from a previous --use-venv run, prefer it so
    # hooks.json points at the interpreter that actually has our deps.
    PYTHON3="$VENV_DIR/bin/python3"
    yellow "Using existing venv at $VENV_DIR (skip-pip mode)"
fi

if APP="$(find_cursor_app)"; then
    echo "Cursor.app: $APP"
else
    yellow "WARNING: Cursor.app not found in /Applications or ~/Applications."
    yellow "         Install Cursor (https://cursor.com) before running the bridge."
fi

if [ "$SKIP_PIP" = "0" ]; then
    cyan ""
    cyan "Installing Python deps..."
    if ! do_pip_install "$PYTHON3"; then
        red "Dep install failed. See output above."
        exit 1
    fi
fi

mkdir -p "$CURSOR_DIR"

CONFIG_PATH="$BRIDGE_DIR/config.toml"
CONFIG_EXAMPLE="$BRIDGE_DIR/config.toml.example"
if [ ! -f "$CONFIG_PATH" ]; then
    cp "$CONFIG_EXAMPLE" "$CONFIG_PATH"
    yellow ""
    yellow "Created $CONFIG_PATH from example."
    yellow "EDIT IT NOW. You need:"
    yellow "  1. telegram.bot_token  (from @BotFather, /newbot)"
    yellow "  2. telegram.allowed_user_ids = [<your numeric Telegram ID from @userinfobot>]"
    yellow "  3. bridge.secret       (use the one below)"
    SECRET="$("$PYTHON3" -c 'import secrets; print(secrets.token_urlsafe(32))')"
    green ""
    green "Generated secret for you:"
    echo  "  $SECRET"
    yellow ""
    yellow "Paste it as bridge.secret in $CONFIG_PATH, then re-run this installer."
    exit 0
fi

# Extract secret from config.toml. tomllib parsing > regex grepping.
SECRET="$("$PYTHON3" - "$CONFIG_PATH" <<'PY'
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
secret = (cfg.get("bridge", {}) or {}).get("secret", "")
if not secret or "REPLACE" in secret.upper():
    sys.exit("ERR: bridge.secret missing or still placeholder in " + sys.argv[1])
print(secret)
PY
)" || { echo "$SECRET" >&2; exit 1; }

PORT="$("$PYTHON3" - "$CONFIG_PATH" <<'PY'
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
print(int((cfg.get("bridge", {}) or {}).get("port", 8765)))
PY
)"

BRIDGE_URL="http://127.0.0.1:${PORT}"

"$PYTHON3" - "$BRIDGE_INFO_OUT" "$BRIDGE_URL" "$SECRET" <<'PY'
import json, sys
path, url, secret = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"url": url, "secret": secret}, f)
PY
green "Wrote $BRIDGE_INFO_OUT"

RUNNER_PATH="$HOOKS_DIR/runner.py"
TEMPLATE="$HOOKS_DIR/hooks.json.template"
if [ ! -f "$TEMPLATE" ]; then
    red "Missing hooks template at $TEMPLATE"
    exit 1
fi

if [ -f "$HOOKS_JSON_OUT" ]; then
    BACKUP="${HOOKS_JSON_OUT}.bak.$(date +%Y%m%d%H%M%S)"
    cp "$HOOKS_JSON_OUT" "$BACKUP"
    yellow "Backed up existing hooks.json -> $BACKUP"
fi

"$PYTHON3" - "$TEMPLATE" "$HOOKS_JSON_OUT" "$PYTHON3" "$RUNNER_PATH" <<'PY'
import sys
template_path, out_path, py, runner = sys.argv[1:5]
with open(template_path, "r", encoding="utf-8") as f:
    text = f.read()
text = text.replace("{{PYTHON}}", py).replace("{{RUNNER}}", runner)
with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)
PY
green "Wrote $HOOKS_JSON_OUT"

chmod +x "$BRIDGE_DIR/wake_cursor.sh"
green "Marked $BRIDGE_DIR/wake_cursor.sh executable"

if [ -z "$INSTALL_LA" ]; then
    printf "\nInstall LaunchAgent so the bridge starts at login? [y/N] "
    read -r ans
    case "$ans" in y|Y|yes|YES) INSTALL_LA=1 ;; *) INSTALL_LA=0 ;; esac
fi

if [ "$INSTALL_LA" = "1" ]; then
    mkdir -p "$LAUNCHAGENT_LOG_DIR"
    BRIDGE_PY="$BRIDGE_DIR/bridge.py"
    cat > "$LAUNCHAGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LAUNCHAGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON3}</string>
        <string>${BRIDGE_PY}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${BRIDGE_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LAUNCHAGENT_LOG_DIR}/bridge.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LAUNCHAGENT_LOG_DIR}/bridge.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)" "$LAUNCHAGENT_PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$LAUNCHAGENT_PLIST"
    launchctl enable "gui/$(id -u)/${LAUNCHAGENT_LABEL}"
    launchctl kickstart -k "gui/$(id -u)/${LAUNCHAGENT_LABEL}" || true
    green "Installed + started LaunchAgent at $LAUNCHAGENT_PLIST"
    yellow "Logs:  $LAUNCHAGENT_LOG_DIR/bridge.{out,err}.log"
else
    cyan ""
    cyan "To run the bridge manually:"
    echo "  $PYTHON3 $BRIDGE_DIR/bridge.py"
fi

cyan ""
cyan "Install done."
yellow "ACCESSIBILITY: open System Settings > Privacy & Security > Accessibility"
yellow "and add the app that launches the bridge (Terminal / iTerm / launchd /"
yellow "Cursor) so wake_cursor.sh can synthesize keystrokes. Without it, the"
yellow "wake feature will silently no-op."
echo ""
yellow "  Verify with:  ./install.sh --diagnose"
yellow "  Test wake:    WAKE_DRY_RUN=1 bash $BRIDGE_DIR/wake_cursor.sh"
echo ""
cyan "Restart Cursor so it picks up the new hooks.json."
