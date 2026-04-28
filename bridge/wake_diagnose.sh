#!/usr/bin/env bash
# wake_diagnose.sh — interactive Mac diagnostic for the wake feature.
#
# What it does:
#   1. Reports Cursor process state (PID, bundle path).
#   2. Reports current foreground app.
#   3. Probes Accessibility permission and tells you exactly which dialog to
#      open if it's missing.
#   4. Activates Cursor and runs wake_cursor.sh in dry-run mode.
#   5. Optionally walks you through finding the correct command-palette
#      string for "Focus Chat" by trying each candidate from a fallback
#      list and asking you which one focused the chat input.
#
# Usage:
#   bash bridge/wake_diagnose.sh                 # quick read-only diagnose
#   bash bridge/wake_diagnose.sh --interactive   # also walk command palette
#   WAKE_FOCUS_CMDS="A:B:C" bash bridge/wake_diagnose.sh --interactive
#
# Designed to be runnable on its own without depending on any other repo
# state (config.toml, bridge daemon, etc.).

set -uo pipefail

if grep -q $'\r' "$0"; then
    echo "ERR: $0 has CRLF line endings. Run: sed -i '' 's/\\r//g' $0" >&2
    exit 1
fi

INTERACTIVE=0
case "${1:-}" in
    --interactive|-i) INTERACTIVE=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
esac

cyan()   { printf '\033[36m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WAKE_SCRIPT="$REPO_ROOT/bridge/wake_cursor.sh"

cyan "== wake_diagnose =="
echo ""

# 1. OS check
case "$(uname -s)" in
    Darwin) green "  OS:             macOS $(sw_vers -productVersion 2>/dev/null || echo '?')" ;;
    *)      red   "  OS:             $(uname -s) — this script is macOS-only"; exit 1 ;;
esac
echo "  arch:           $(uname -m)"

# 2. Required tools
for cmd in osascript pbcopy pbpaste pgrep open; do
    if command -v "$cmd" >/dev/null 2>&1; then
        green "  $cmd:           $(command -v "$cmd")"
    else
        red "  $cmd:           NOT FOUND"
    fi
done

# 3. Cursor process
PID="$(pgrep -x Cursor 2>/dev/null | head -n 1)"
if [ -z "$PID" ]; then
    PID="$(pgrep -if 'Cursor.app/Contents/MacOS/Cursor' 2>/dev/null | head -n 1)"
fi
if [ -n "$PID" ]; then
    BUNDLE="$(ps -p "$PID" -o comm= 2>/dev/null || echo '?')"
    green "  Cursor running: yes (pid $PID)"
    echo "  Cursor bundle:  $BUNDLE"
else
    red   "  Cursor running: NO — open Cursor and re-run this script"
    exit 2
fi

# 4. Foreground app right now
FRONT="$(osascript -e 'tell application "System Events" to name of first application process whose frontmost is true' 2>/dev/null || echo '?')"
echo "  frontmost app:  $FRONT"

# 5. Accessibility probe
echo ""
cyan "Probing Accessibility permission (sends a no-op Shift keystroke)..."
TMPERR="$(mktemp -t wake_diag.XXXXXX)"
trap 'rm -f "$TMPERR"' EXIT
if osascript -e 'tell application "System Events" to key down shift' 2>"$TMPERR"; then
    osascript -e 'tell application "System Events" to key up shift' >/dev/null 2>&1 || true
    green "  Accessibility:  GRANTED"
else
    osascript -e 'tell application "System Events" to key up shift' >/dev/null 2>&1 || true
    red   "  Accessibility:  DENIED"
    echo "      stderr: $(cat "$TMPERR")"
    yellow ""
    yellow "  Open: System Settings > Privacy & Security > Accessibility"
    yellow "  Add the app that ran this script (Terminal / iTerm / launchd)"
    yellow "  AND Cursor itself. Toggle off+on if the change doesn't apply."
    exit 3
fi

# 6. wake_cursor.sh dry-run
echo ""
cyan "Running wake_cursor.sh dry-run..."
if [ ! -x "$WAKE_SCRIPT" ]; then
    yellow "  wake_cursor.sh not executable — fixing..."
    chmod +x "$WAKE_SCRIPT" 2>/dev/null || true
fi
DRY_OUT="$(WAKE_DRY_RUN=1 bash "$WAKE_SCRIPT" 2>&1 || true)"
if echo "$DRY_OUT" | grep -q "DRYRUN OK"; then
    green "  dry-run:        $DRY_OUT"
else
    red   "  dry-run FAILED: $DRY_OUT"
    exit 4
fi

# 7. Interactive command-palette walk
if [ "$INTERACTIVE" = "1" ]; then
    echo ""
    cyan "== interactive command-palette walk =="
    yellow "We'll send each candidate in turn. After each one, look at Cursor."
    yellow "If your cursor (caret) is BLINKING IN THE CHAT INPUT BOX, that"
    yellow "candidate is the right one — press Y. Otherwise press N to try the"
    yellow "next. Press Esc in Cursor between attempts if a palette is stuck."
    echo ""

    CANDIDATES="${WAKE_FOCUS_CMDS:-Focus Chat:Focus on Chat View:Workbench Chat: Focus on Chat Input:Cursor: Focus Chat:Chat: Focus Chat View}"
    IFS=":"
    # shellcheck disable=SC2086
    set -- $CANDIDATES
    unset IFS

    WINNER=""
    for cand in "$@"; do
        [ -z "$cand" ] && continue
        echo ""
        cyan "  -> trying: $cand"
        # ensure clean state
        osascript -e 'tell application "Cursor" to activate' >/dev/null 2>&1 || true
        sleep 0.4
        osascript -e 'tell application "System Events" to key code 53' >/dev/null 2>&1 || true
        sleep 0.15
        # open palette + paste candidate + Enter
        osascript -e 'tell application "System Events" to keystroke "p" using {command down, shift down}' >/dev/null 2>&1
        sleep 0.35
        printf '%s' "$cand" | pbcopy
        sleep 0.08
        osascript -e 'tell application "System Events" to keystroke "v" using {command down}' >/dev/null 2>&1
        sleep 0.25
        osascript -e 'tell application "System Events" to key code 36' >/dev/null 2>&1
        sleep 0.7
        printf "Did focus land in the Cursor chat input? [y/N/q] "
        read -r ans
        case "$ans" in
            y|Y) WINNER="$cand"; break ;;
            q|Q) break ;;
            *)   ;;
        esac
    done

    echo ""
    if [ -n "$WINNER" ]; then
        green "FOUND: '$WINNER' focuses the chat input."
        yellow "Set this in bridge/config.toml:"
        echo  "    wake_focus_command = \"$WINNER\""
        yellow "and restart the bridge."
    else
        red "No candidate worked. Open Cursor's command palette manually"
        red "(Cmd+Shift+P), look for a command containing 'Focus' and 'Chat',"
        red "and set that exact name as wake_focus_command in config.toml."
    fi
fi

echo ""
cyan "== done =="
