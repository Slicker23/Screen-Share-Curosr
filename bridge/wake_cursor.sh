#!/usr/bin/env bash
# wake_cursor.sh - macOS counterpart of wake_cursor.ps1.
#
# Hardened behaviour:
#   - Activates Cursor via `open -a Cursor` (more reliable than osascript-only;
#     does not require Accessibility permission for activation).
#   - Probes Accessibility permission BEFORE typing anything by sending a
#     no-op modifier key. If the permission is missing, exits with a clear
#     error so we don't paste into nowhere.
#   - Sends Esc once before opening the command palette to dismiss any
#     stuck modal/menu so we always start in a known state.
#   - Tries each name in WAKE_FOCUS_CMDS (colon-separated) in order so a
#     mis-named primary doesn't silently drop the prompt; gives up if all fail.
#   - Captures osascript stderr to a per-invocation tmpfile and surfaces it
#     on any failure (you'll see the AppleScript error in the bridge log).
#
# Args (env vars to avoid argv escaping pain):
#   WAKE_MESSAGE      Prompt text to inject (required unless WAKE_DRY_RUN=1).
#   WAKE_FOCUS_CMD    Single command-palette string. Default "Focus Chat".
#                     Used only if WAKE_FOCUS_CMDS is unset.
#   WAKE_FOCUS_CMDS   Colon-separated fallback list, tried in order. e.g.
#                     "Focus Chat:Workbench: Focus on Chat View:Cursor: Focus Chat"
#                     If unset, we synthesise a sensible default chain that
#                     starts with WAKE_FOCUS_CMD.
#   WAKE_WORKSPACE    Substring of the Cursor window title to prefer.
#   WAKE_DRY_RUN      If "1", just probe (activate + accessibility) and exit.
#
# Exit codes:
#   0 = OK (or DRYRUN OK).
#   2 = bad input / missing dependency.
#   3 = Cursor process not running.
#   4 = activation failed.
#   5 = Accessibility permission missing.
#
# Output: prints "OK ..." on success or "ERR: ..." on failure.

set -uo pipefail

# CRLF self-check.
if grep -q $'\r' "$0"; then
    echo "ERR: $0 has CRLF line endings. Run: sed -i '' 's/\\r//g' $0" >&2
    exit 2
fi

MESSAGE="${WAKE_MESSAGE:-}"
FOCUS_CMD="${WAKE_FOCUS_CMD:-Focus Chat}"
FOCUS_CMDS_RAW="${WAKE_FOCUS_CMDS:-}"
WORKSPACE_MATCH="${WAKE_WORKSPACE:-}"
DRY_RUN="${WAKE_DRY_RUN:-0}"

if [ -z "$MESSAGE" ] && [ "$DRY_RUN" != "1" ]; then
    echo "ERR: WAKE_MESSAGE env var required (or WAKE_DRY_RUN=1)" >&2
    exit 2
fi

if ! command -v osascript >/dev/null 2>&1; then
    echo "ERR: osascript not found (this script is macOS-only)" >&2
    exit 2
fi

# Tempfile for capturing osascript stderr across calls. Cleaned on exit.
OSASCRIPT_ERR_LOG="$(mktemp -t wake_cursor.XXXXXX)"
trap 'rm -f "$OSASCRIPT_ERR_LOG"' EXIT

# Run osascript and write its stderr to OSASCRIPT_ERR_LOG (overwriting each
# call). Returns osascript's own exit code. Stdout is passed through.
osa() {
    osascript "$@" 2>"$OSASCRIPT_ERR_LOG"
}

# --- Find Cursor ------------------------------------------------------------
# Try a chain of process detectors so we don't bail just because pgrep -x
# can't see the helper process layout on whatever macOS version this is.
find_cursor_pid() {
    local pid=""
    pid="$(pgrep -x "Cursor" 2>/dev/null | head -n 1 || true)"
    if [ -n "$pid" ]; then echo "$pid"; return 0; fi
    pid="$(pgrep -if "Cursor.app/Contents/MacOS/Cursor" 2>/dev/null | head -n 1 || true)"
    if [ -n "$pid" ]; then echo "$pid"; return 0; fi
    pid="$(ps -ax -o pid=,comm= 2>/dev/null | awk '$2 ~ /Cursor.app\/Contents\/MacOS\/Cursor$/ {print $1; exit}')"
    if [ -n "$pid" ]; then echo "$pid"; return 0; fi
    return 1
}

CURSOR_PID="$(find_cursor_pid || true)"
if [ -z "$CURSOR_PID" ]; then
    echo "ERR: Cursor process not running (tried pgrep -x, pgrep -if, ps -ax)" >&2
    exit 3
fi

# --- Activate ---------------------------------------------------------------
# `open -a` is more reliable than `osascript -e 'tell application "Cursor" to activate'`
# because it does not require Accessibility permission. AppleScript activation
# may silently no-op if the user hasn't granted the calling app permission to
# control Cursor.
if ! open -a "Cursor" 2>/dev/null; then
    if ! osa -e 'tell application "Cursor" to activate' >/dev/null; then
        ERR_DETAIL="$(cat "$OSASCRIPT_ERR_LOG" 2>/dev/null || true)"
        echo "ERR: failed to activate Cursor (open -a + osascript both failed)" >&2
        [ -n "$ERR_DETAIL" ] && echo "ERR: osascript: $ERR_DETAIL" >&2
        exit 4
    fi
fi

# Window-server lag + animation; needs real time on macOS.
sleep 0.4

# Best-effort: focus a window whose title matches WORKSPACE_MATCH. Silent
# failure is fine; we still typed into the front-most Cursor window if not.
if [ -n "$WORKSPACE_MATCH" ]; then
    osa <<APPLESCRIPT >/dev/null || true
tell application "System Events"
    tell process "Cursor"
        set winList to windows whose title contains "$WORKSPACE_MATCH"
        if (count of winList) > 0 then
            perform action "AXRaise" of (item 1 of winList)
        end if
    end tell
end tell
APPLESCRIPT
fi

# --- Accessibility probe ----------------------------------------------------
# Send a benign keystroke (just the Shift modifier with no main key — yields
# a no-op key event) and inspect osascript exit code + stderr. If the calling
# process lacks Accessibility permission, macOS returns error code -1719
# ("System Events got an error: ... is not allowed assistive access") or
# exit status 1 with that text. We bail BEFORE typing the prompt so we don't
# paste into a focused editor pane.
if ! osa -e 'tell application "System Events" to key down shift' >/dev/null; then
    ERR_DETAIL="$(cat "$OSASCRIPT_ERR_LOG" 2>/dev/null || true)"
    if echo "$ERR_DETAIL" | grep -qE "(-1719|not allowed|assistive access|accessibility)"; then
        echo "ERR: Accessibility permission missing for the calling process." >&2
        echo "     Open: System Settings > Privacy & Security > Accessibility" >&2
        echo "     and enable the parent app (Terminal / iTerm / launchd / Cursor)." >&2
        echo "ERR: osascript: $ERR_DETAIL" >&2
        # try to release the modifier we may have set down
        osascript -e 'tell application "System Events" to key up shift' >/dev/null 2>&1 || true
        exit 5
    fi
    echo "ERR: accessibility probe failed unexpectedly" >&2
    [ -n "$ERR_DETAIL" ] && echo "ERR: osascript: $ERR_DETAIL" >&2
    osascript -e 'tell application "System Events" to key up shift' >/dev/null 2>&1 || true
    exit 5
fi
osa -e 'tell application "System Events" to key up shift' >/dev/null || true

if [ "$DRY_RUN" = "1" ]; then
    FRONT_APP="$(osa -e 'tell application "System Events" to name of first application process whose frontmost is true' 2>/dev/null || echo "?")"
    echo "DRYRUN OK pid=$CURSOR_PID frontmost=$FRONT_APP focus_command='$FOCUS_CMD'"
    exit 0
fi

# --- Build the focus-command fallback chain --------------------------------
# Use WAKE_FOCUS_CMDS if provided; otherwise synthesise a chain that starts
# with WAKE_FOCUS_CMD and tries known-likely Cursor variants.
if [ -n "$FOCUS_CMDS_RAW" ]; then
    FOCUS_CHAIN="$FOCUS_CMDS_RAW"
else
    FOCUS_CHAIN="${FOCUS_CMD}:Focus on Chat View:Workbench Chat: Focus on Chat Input:Cursor: Focus Chat"
fi

# Save user's clipboard so we can restore it after we steal it for paste.
PREV_CLIP="$(pbpaste 2>/dev/null || true)"

# Pre-step: dismiss any open modal / palette / menu so we start in a known
# state. Esc is harmless if nothing is open.
osa -e 'tell application "System Events" to key code 53' >/dev/null || true
sleep 0.10

# Helper: run one focus attempt. Opens command palette, pastes the candidate
# command name, presses Return.
attempt_focus() {
    local cmd_name="$1"
    osa -e 'tell application "System Events" to keystroke "p" using {command down, shift down}' >/dev/null || return 1
    sleep 0.35
    printf '%s' "$cmd_name" | pbcopy
    sleep 0.08
    osa -e 'tell application "System Events" to keystroke "v" using {command down}' >/dev/null || return 1
    sleep 0.25
    osa -e 'tell application "System Events" to key code 36' >/dev/null || return 1
    sleep 0.55
    return 0
}

# Try each focus-command candidate. We can't truly verify success (Cursor
# is Electron, so we can't query the focused subwindow easily), but if the
# osascript calls themselves error we move on.
ATTEMPTED=""
SUCCESS_CMD=""
IFS=":"
# shellcheck disable=SC2086
set -- $FOCUS_CHAIN
unset IFS
for cand in "$@"; do
    [ -z "$cand" ] && continue
    ATTEMPTED="${ATTEMPTED}|${cand}"
    if attempt_focus "$cand"; then
        SUCCESS_CMD="$cand"
        break
    fi
    # If a candidate fails, press Esc to clean up before trying the next one.
    osa -e 'tell application "System Events" to key code 53' >/dev/null || true
    sleep 0.10
done

if [ -z "$SUCCESS_CMD" ]; then
    ERR_DETAIL="$(cat "$OSASCRIPT_ERR_LOG" 2>/dev/null || true)"
    echo "ERR: all focus-command candidates failed (tried${ATTEMPTED})" >&2
    [ -n "$ERR_DETAIL" ] && echo "ERR: last osascript: $ERR_DETAIL" >&2
    # Restore clipboard before bailing.
    [ -n "$PREV_CLIP" ] && printf '%s' "$PREV_CLIP" | pbcopy 2>/dev/null || true
    exit 6
fi

# --- Paste + submit the actual message --------------------------------------
printf '%s' "$MESSAGE" | pbcopy
sleep 0.10
if ! osa -e 'tell application "System Events" to keystroke "v" using {command down}' >/dev/null; then
    ERR_DETAIL="$(cat "$OSASCRIPT_ERR_LOG" 2>/dev/null || true)"
    echo "ERR: paste keystroke failed" >&2
    [ -n "$ERR_DETAIL" ] && echo "ERR: osascript: $ERR_DETAIL" >&2
    [ -n "$PREV_CLIP" ] && printf '%s' "$PREV_CLIP" | pbcopy 2>/dev/null || true
    exit 7
fi
sleep 0.20
if ! osa -e 'tell application "System Events" to key code 36' >/dev/null; then
    ERR_DETAIL="$(cat "$OSASCRIPT_ERR_LOG" 2>/dev/null || true)"
    echo "ERR: submit keystroke failed" >&2
    [ -n "$ERR_DETAIL" ] && echo "ERR: osascript: $ERR_DETAIL" >&2
    [ -n "$PREV_CLIP" ] && printf '%s' "$PREV_CLIP" | pbcopy 2>/dev/null || true
    exit 8
fi

# Restore the user's prior clipboard. Best-effort.
sleep 0.20
if [ -n "$PREV_CLIP" ]; then
    printf '%s' "$PREV_CLIP" | pbcopy 2>/dev/null || true
fi

echo "OK pid=$CURSOR_PID message_chars=${#MESSAGE} focus_used='$SUCCESS_CMD'"
exit 0
