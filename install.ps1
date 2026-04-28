<#
.SYNOPSIS
  Installer for Cursor Phone Bridge.

.DESCRIPTION
  - Checks Python.
  - Installs Python deps.
  - Copies config.toml.example -> bridge/config.toml (if missing).
  - Renders hooks.json into %USERPROFILE%\.cursor\hooks.json with absolute paths.
  - Writes %USERPROFILE%\.cursor\cursor-phone-bridge.json so the runner finds the bridge.
  - Optionally registers a Scheduled Task to launch the bridge at user logon.

.PARAMETER RegisterTask
  Create the "CursorPhoneBridge" scheduled task. Default: prompted.

.PARAMETER SkipPip
  Skip 'pip install'.

.EXAMPLE
  .\install.ps1
  .\install.ps1 -RegisterTask -SkipPip
#>

[CmdletBinding()]
param(
    [switch]$RegisterTask,
    [switch]$SkipPip
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$BridgeDir = Join-Path $Root "bridge"
$HooksDir = Join-Path $Root "hooks"
$CursorDir = Join-Path $env:USERPROFILE ".cursor"
$HooksJsonOut = Join-Path $CursorDir "hooks.json"
$BridgeInfoOut = Join-Path $CursorDir "cursor-phone-bridge.json"

function Find-Python {
    # Use python.exe (NOT pythonw.exe) for hooks: pythonw.exe detaches stdout
    # which makes Cursor see "no output" and trigger failClosed denials.
    # Skip the WindowsApps shim (zero-byte App Execution Alias) which can
    # silently break stdin piping when Cursor spawns the child.
    $candidates = @("python.exe", "python3.exe")
    foreach ($c in $candidates) {
        $found = Get-Command $c -All -ErrorAction SilentlyContinue
        foreach ($cmd in $found) {
            if ($cmd.Source -notlike "*WindowsApps*") { return $cmd.Source }
        }
    }
    # last resort: WindowsApps alias
    foreach ($c in $candidates) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "Python not found in PATH. Install Python 3.11+ first."
}

function Find-PythonForeground {
    # used for foreground (logged) execution; pip uses this too
    $cmd = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command "python3.exe" -ErrorAction SilentlyContinue }
    if (-not $cmd) { throw "python.exe not found" }
    return $cmd.Source
}

Write-Host "== Cursor Phone Bridge installer ==" -ForegroundColor Cyan

$pythonW = Find-Python
$pythonFg = Find-PythonForeground
Write-Host "python (background): $pythonW"
Write-Host "python (foreground): $pythonFg"

if (-not $SkipPip) {
    Write-Host "`nInstalling Python deps..." -ForegroundColor Cyan
    & $pythonFg -m pip install -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

if (-not (Test-Path $CursorDir)) {
    New-Item -ItemType Directory -Path $CursorDir | Out-Null
}

$ConfigPath = Join-Path $BridgeDir "config.toml"
$ConfigExample = Join-Path $BridgeDir "config.toml.example"
if (-not (Test-Path $ConfigPath)) {
    Copy-Item $ConfigExample $ConfigPath
    Write-Host "`nCreated $ConfigPath from example." -ForegroundColor Yellow
    Write-Host "EDIT IT NOW. You need:" -ForegroundColor Yellow
    Write-Host "  1. telegram.bot_token   (get from @BotFather, /newbot)" -ForegroundColor Yellow
    Write-Host "  2. telegram.allowed_user_ids = [<your numeric Telegram ID from @userinfobot>]" -ForegroundColor Yellow
    Write-Host "  3. bridge.secret        (generate one below)" -ForegroundColor Yellow

    $secret = & $pythonFg -c "import secrets;print(secrets.token_urlsafe(32))"
    Write-Host "`nGenerated secret for you:" -ForegroundColor Green
    Write-Host "  $secret"
    Write-Host "`nPaste it as bridge.secret in $ConfigPath, then re-run this installer." -ForegroundColor Yellow
    exit 0
}

# parse config to extract secret
$configRaw = Get-Content $ConfigPath -Raw
$secretMatch = [regex]::Match($configRaw, '(?m)^\s*secret\s*=\s*"([^"]+)"')
if (-not $secretMatch.Success) { throw "could not find bridge.secret in $ConfigPath" }
$secret = $secretMatch.Groups[1].Value
if ($secret -match "REPLACE") { throw "edit bridge.secret in $ConfigPath (still placeholder)" }

$tokenMatch = [regex]::Match($configRaw, '(?m)^\s*bot_token\s*=\s*"([^"]+)"')
if ($tokenMatch.Success -and $tokenMatch.Groups[1].Value -match "REPLACE") {
    throw "edit telegram.bot_token in $ConfigPath (still placeholder)"
}

$portMatch = [regex]::Match($configRaw, '(?m)^\s*port\s*=\s*(\d+)')
$port = if ($portMatch.Success) { [int]$portMatch.Groups[1].Value } else { 8765 }

$bridgeUrl = "http://127.0.0.1:$port"

# write bridge info JSON for the runner (NO BOM - PowerShell's Set-Content adds one and it breaks json.loads)
$bridgeInfoJson = @{ url = $bridgeUrl; secret = $secret } | ConvertTo-Json
[System.IO.File]::WriteAllText($BridgeInfoOut, $bridgeInfoJson, [System.Text.UTF8Encoding]::new($false))
Write-Host "Wrote $BridgeInfoOut" -ForegroundColor Green

# render hooks.json
$RunnerPath = (Join-Path $HooksDir "runner.py").Replace("\", "/")
$PythonForHooks = $pythonW.Replace("\", "/")
$template = Get-Content (Join-Path $HooksDir "hooks.json.template") -Raw
$rendered = $template.Replace("{{PYTHON}}", $PythonForHooks).Replace("{{RUNNER}}", $RunnerPath)

if (Test-Path $HooksJsonOut) {
    $backup = "$HooksJsonOut.bak.$(Get-Date -Format yyyyMMddHHmmss)"
    Copy-Item $HooksJsonOut $backup
    Write-Host "Backed up existing hooks.json -> $backup" -ForegroundColor Yellow
}
[System.IO.File]::WriteAllText($HooksJsonOut, $rendered, [System.Text.UTF8Encoding]::new($false))
Write-Host "Wrote $HooksJsonOut" -ForegroundColor Green

# scheduled task
if (-not $RegisterTask) {
    $ans = Read-Host "`nRegister Scheduled Task to start bridge at logon? [y/N]"
    if ($ans -match '^[Yy]') { $RegisterTask = $true }
}

if ($RegisterTask) {
    $TaskName = "CursorPhoneBridge"
    $bridgePy = (Join-Path $BridgeDir "bridge.py")
    $action = New-ScheduledTaskAction -Execute $pythonW -Argument "`"$bridgePy`"" -WorkingDirectory $BridgeDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 5
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Scheduled Task '$TaskName' registered + started." -ForegroundColor Green
} else {
    Write-Host "`nTo run the bridge manually:" -ForegroundColor Cyan
    Write-Host "  $pythonFg `"$($BridgeDir)\bridge.py`""
}

Write-Host "`nInstall done. Restart Cursor so it picks up the new hooks.json." -ForegroundColor Cyan
