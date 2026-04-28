# wake_cursor.ps1 - Inject a prompt into the Cursor agent chat.
#
# Usage:
#   pwsh -File wake_cursor.ps1 -Message "your prompt" `
#                              [-WorkspaceMatch "folder-name"] `
#                              [-FocusCommand "Focus Chat"] `
#                              [-DryRun]
#
# How it works:
#   1. Find the Cursor process whose window title mentions WorkspaceMatch.
#   2. Bring that window to the foreground (Win32 SetForegroundWindow with the
#      AttachThreadInput trick to bypass focus-steal protection).
#   3. Open Cursor's command palette (Ctrl+Shift+P).
#   4. Paste a focus command string and press Enter — this executes the top
#      matching command, which should put keyboard focus into the chat input.
#   5. Paste the user's message and press Enter.
#
# Why command palette instead of just Ctrl+L?
#   Ctrl+L is a TOGGLE in Cursor: pressing it when the chat panel is already
#   open CLOSES it. So sending Ctrl+L from a wake script is unreliable —
#   the first wake closes the chat, the second one reopens it. The command
#   palette approach is non-toggling: invoking "Focus Chat" always lands
#   focus in the chat input regardless of the current panel state.
#
# Why not Ctrl+A + Del to clear any draft?
#   That was the original implementation and it caused source files to be
#   overwritten when focus failed to land in the chat input on time. We
#   removed it. Worst case now: your message is appended to whatever draft
#   you had in the chat box. Files are never touched.
#
# Output: prints "OK pid=<pid> ..." on success, errors to stderr with exit !=0.

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Message,
    [string]$WorkspaceMatch = "",
    [string]$FocusCommand = "Focus Chat",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$sig = @"
using System;
using System.Text;
using System.Runtime.InteropServices;

public class Win {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet=CharSet.Auto)]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);
}
"@
if (-not ([System.Management.Automation.PSTypeName]'Win').Type) {
    Add-Type -TypeDefinition $sig
}

function Get-FgClass {
    $h = [Win]::GetForegroundWindow()
    $sb = New-Object System.Text.StringBuilder 256
    [Win]::GetClassName($h, $sb, 256) | Out-Null
    return @{ hwnd = $h; class = $sb.ToString() }
}

# Paste arbitrary text via clipboard. Faster + safer than SendKeys for unicode
# and special characters. Caller is responsible for any pre/post sleep.
function Send-Paste([string]$text) {
    [System.Windows.Forms.Clipboard]::SetText($text)
    Start-Sleep -Milliseconds 80
    [System.Windows.Forms.SendKeys]::SendWait("^v")
}

$procs = Get-Process -Name "Cursor" -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle }

if (-not $procs) {
    Write-Error "no Cursor window found"
    exit 2
}

$target = $null
if ($WorkspaceMatch) {
    $target = $procs | Where-Object { $_.MainWindowTitle -like "*$WorkspaceMatch*" } | Select-Object -First 1
}
if (-not $target) {
    $target = $procs | Sort-Object -Property StartTime -Descending | Select-Object -First 1
}

$hwnd = $target.MainWindowHandle

if ([Win]::IsIconic($hwnd)) {
    [Win]::ShowWindow($hwnd, 9) | Out-Null
    Start-Sleep -Milliseconds 250
}

# Bring to foreground. SetForegroundWindow has anti-focus-steal protection;
# the AttachThreadInput trick lets us bypass it from a background process.
$fgInfo = Get-FgClass
$fgHwnd = $fgInfo.hwnd
$fgPid = 0
$fgThread = [Win]::GetWindowThreadProcessId($fgHwnd, [ref]$fgPid)
$myThread = [Win]::GetCurrentThreadId()

[Win]::AttachThreadInput($fgThread, $myThread, $true) | Out-Null
$ok = [Win]::SetForegroundWindow($hwnd)
[Win]::AttachThreadInput($fgThread, $myThread, $false) | Out-Null

if (-not $ok) {
    Write-Error "SetForegroundWindow failed (focus-steal protection?)"
    exit 3
}

Start-Sleep -Milliseconds 400

$afterFocus = Get-FgClass
if ($DryRun) {
    Write-Output "DRYRUN pid=$($target.Id) title=$($target.MainWindowTitle) fg_class_before=$($fgInfo.class) fg_class_after=$($afterFocus.class) focus_command='$FocusCommand'"
    exit 0
}

# Step 1: open command palette. Ctrl+Shift+P is Cursor's default — it always
# opens the palette and immediately focuses the search input. Unlike Ctrl+L
# this is NOT a toggle; pressing it when palette is already open is harmless
# (it just keeps it open).
[System.Windows.Forms.SendKeys]::SendWait("^+p")
Start-Sleep -Milliseconds 350

# Step 2: paste the focus command name. Using paste (not typed SendKeys)
# because SendKeys treats { } + ^ % ~ as special characters and would
# mis-encode anything user-supplied.
Send-Paste $FocusCommand
Start-Sleep -Milliseconds 250

# Step 3: Enter — runs the highlighted (top-matching) command, which focuses
# the chat input.
[System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
Start-Sleep -Milliseconds 600

# Step 4: paste and submit the user's actual message.
Send-Paste $Message
Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait("{ENTER}")

Write-Output "OK pid=$($target.Id) title=$($target.MainWindowTitle) fg_class=$($afterFocus.class) focus_command='$FocusCommand'"
exit 0
