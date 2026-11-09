# Move DAISY from this PC to the VPS where VERA runs - every step in one go (docs/20.4).
#
#   cd $HOME\Documents\DAISY
#   powershell -ExecutionPolicy Bypass -File deploy\move-to-vps.ps1 -Server ubuntu@your-vps-address
#
# What it does:
#   1 checks it can reach the server over SSH (your own key; type the passphrase/password yourself if asked)
#   2 waits until after the US close (16:25 ET on trading days) unless -Now
#   3 pushes DAISY to GitHub (the server installs from there)
#   4 stops DAISY's monitor on this PC and keeps the "ABG Monitor" startup task disabled (one Discord poster)
#   5 packs data + settings, copies them and VERA's DAISY settings to the server, deletes the local copy
#   6 runs deploy/vps-finish.sh there: installs DAISY as the "abg" service with the nightly tested auto-update,
#     checks its health (sudo password asked on the server).  VERA's own config lives in tolu-adeb/vera and is
#     not touched here.
param(
  [string]$Server = "ubuntu@135.148.166.233",     # the OVH VPS-2 VERA runs on (vps-29a39fe0)
  [switch]$Now
)
$ErrorActionPreference = "Stop"
$Daisy = Split-Path -Parent $PSScriptRoot
Set-Location $Daisy
function Say($m) { Write-Host "`n== $m" -ForegroundColor Cyan }

if (-not $Server) {
  $cfg = Join-Path $HOME ".ssh\config"
  if (Test-Path $cfg) {
    $hosts = Select-String -Path $cfg -Pattern '^\s*Host\s+(.+)$' | ForEach-Object { $_.Matches[0].Groups[1].Value } |
             Where-Object { $_ -notmatch '[\*\?]' }
    if ($hosts) { Write-Host "Hosts in your SSH config: $($hosts -join ', ')" }
  }
  $Server = Read-Host "Server to move DAISY to (user@address, or a Host name from your SSH config)"
}
if (-not $Server) { throw "no server given" }

Say "1/6 checking SSH to $Server"
& ssh -o ConnectTimeout=15 $Server "echo connected to `$(hostname); command -v sudo curl tar >/dev/null && echo tools-ok"
if ($LASTEXITCODE -ne 0) { throw "can't reach $Server over SSH - check the address and that your key is on the server" }

Say "2/6 timing"
$ny = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "Eastern Standard Time")
$after = $ny.Date.AddHours(16).AddMinutes(25)
if (-not $Now -and $ny.DayOfWeek -ne 'Saturday' -and $ny.DayOfWeek -ne 'Sunday' -and $ny -lt $after -and $ny.Hour -ge 7) {
  $wait = [int]($after - $ny).TotalSeconds
  Write-Host ("Market day: waiting until 16:25 ET ({0:N0} min). Ctrl+C to stop; -Now moves straight away (the server then runs today's stock-desk pass)." -f ($wait / 60))
  Start-Sleep -Seconds $wait
} else { Write-Host "ok to move now ($($ny.ToString('ddd HH:mm')) ET)" }

Say "3/6 pushing DAISY to GitHub"
git push origin main
if ($LASTEXITCODE -ne 0) { throw "git push failed - fix that first (the server installs from GitHub)" }

Say "4/6 stopping DAISY's monitor on this PC"
try { Disable-ScheduledTask -TaskName "ABG Monitor" -ErrorAction Stop | Out-Null; Write-Host "startup task 'ABG Monitor' disabled" }
catch { Write-Host "no 'ABG Monitor' startup task (fine)" }
# every DAISY process on this PC (the monitor window and any dashboard, which would otherwise take over as monitor)
$venv = Join-Path $Daisy ".venv"
# (a venv's python.exe hands off to the base interpreter, so match on the command line and the lock's pid too)
$lockPid = $null
$lock = Join-Path $Daisy "data\monitor.lock"
if (Test-Path $lock) { try { $lockPid = (Get-Content $lock -Raw | ConvertFrom-Json).pid } catch {} }
$procs = Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $PID -and $_.Name -notmatch '^(powershell|pwsh|conhost|WindowsTerminal|OpenConsole)' -and (
    $_.ProcessId -eq $lockPid -or
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.CommandLine.IndexOf($venv, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
    ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'Start (Monitor|Dashboard)\.bat') ) }
if ($procs) {
  $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped $($_.Name) (pid $($_.ProcessId))" }
  Start-Sleep -Seconds 3
} else { Write-Host "DAISY isn't running on this PC" }

Say "5/6 packing and copying"
python scripts\pack_for_server.py
if ($LASTEXITCODE -ne 0) { throw "packing failed" }
$pack = Join-Path (Split-Path -Parent $Daisy) "daisy-server.tgz"
$stage = Join-Path $env:TEMP "daisy-move"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $stage | Out-Null
Move-Item $pack "$stage\daisy-server.tgz"
Copy-Item "$Daisy\deploy\vps-finish.sh" "$stage\vps-finish.sh"
& ssh $Server "rm -rf ~/daisy-move"
& scp -r $stage "${Server}:~/daisy-move"
$scpOk = $LASTEXITCODE
Remove-Item $stage -Recurse -Force               # the archive holds your keys: never leave a copy here
if ($scpOk -ne 0) { throw "copy to the server failed (local copy deleted; re-run the script)" }

Say "6/6 installing on the server (it asks for your sudo password there)"
& ssh -t $Server "sed -i 's/\r$//' ~/daisy-move/vps-finish.sh && bash ~/daisy-move/vps-finish.sh"
if ($LASTEXITCODE -ne 0) { throw "the server step stopped early - scroll up for the reason, fix it, and re-run (data on the server is kept)" }

python scripts\pack_for_server.py --hold-local
Write-Host "`nDAISY now runs on $Server. Keep this PC's monitor off (the startup task stays disabled)." -ForegroundColor Green
Write-Host "This PC's Discord lines are held and its dashboard won't start a monitor, so it can never post a second copy."
