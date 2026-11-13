# Export and publish this machine's usage dailies (Windows port of phoebus's
# ~/.nougen/bin/tracker_daily_publish.sh).
#
# blade had NO scheduler for this: every blade daily up to 2026-09-11 was
# exported by hand from a Claude session, so nothing was published after the
# last one ran. Scheduled every 4h by the NouGen-TrackerDaily task.
#
# Order matters: rebase BEFORE exporting so the export lands on current
# origin/main. Only yesterday..today are exported -- archived days are never
# regenerated (an aged day re-reads a pruned corpus and undercounts).
$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'

$Log = Join-Path $env:USERPROFILE '.nougen\logs\tracker_daily.log'
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
function Say($m) { Add-Content -Path $Log -Encoding utf8 -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $m) }

# Resolve the clone instead of hardcoding one box's layout.
$Candidates = @(
  "$env:USERPROFILE\Watchtower\NouGen\NouGenTracker",
  "$env:USERPROFILE\Outpost\NouGenTracker",
  "$env:USERPROFILE\NouGenTools\NouGenTracker",
  "$env:USERPROFILE\.nougen\nougentracker-live-safe",
  "$env:USERPROFILE\.nougen\tracker")
$Repo = $env:NOUGENTRACKER_DIR
if (-not ($Repo -and (Test-Path "$Repo\.git"))) { $Repo = $Candidates | Where-Object { Test-Path "$_\.git" } | Select-Object -First 1 }
if (-not $Repo) { Say 'FATAL no tracker clone found'; exit 1 }
Set-Location $Repo

$Py = @("$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "C:\Python311\python.exe", "C:\Python312\python.exe", "C:\Python313\python.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Py) { Say 'FATAL no real python.exe (WindowsApps alias does not run under Task Scheduler)'; exit 1 }

$Machine = (& $Py -c "import fleet_dailies as f; print(f.resolve_machine())" 2>$null | Select-Object -Last 1)
if (-not $Machine) { Say 'FATAL could not resolve machine name'; exit 1 }

git fetch -q origin main 2>$null
if ($LASTEXITCODE -ne 0) { Say 'SKIP: fetch failed (offline?)'; exit 0 }

# Refuse only on dirt that COLLIDES with incoming commits, not on any dirt.
$Dirty = @(git status --porcelain -- . | Where-Object { $_ -notmatch '^\?\? ' } | ForEach-Object { ($_.Substring(3) -split ' -> ')[-1] })
if ($Dirty.Count) {
  $Incoming = @(git diff --name-only HEAD origin/main)
  $Collide = @($Dirty | Where-Object { $Incoming -contains $_ })
  if ($Collide.Count) { Say ("SKIP: uncommitted work COLLIDES with incoming commits: " + ($Collide -join ' ')); exit 0 }
}

$Behind = [int](git rev-list --count HEAD..origin/main)
if ($Behind -gt 0) {
  git rebase -q --autostash origin/main 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { git rebase --abort 2>$null | Out-Null; Say "SKIP: rebase onto origin/main failed ($Behind behind) -- needs a human"; exit 0 }
  Say "rebased $Behind commit(s) onto origin/main"
}

$Yday  = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
$Today = (Get-Date).ToString('yyyy-MM-dd')
& $Py token_tracker.py --start $Yday --end $Today --export *> $null
if ($LASTEXITCODE -ne 0) { Say "FAIL: export errored for $Yday..$Today"; exit 1 }

if (-not (git status --porcelain -- "dailies/$Machine")) { Say "no change ($Yday..$Today)"; exit 0 }

git add -- "dailies/$Machine" 2>$null
git -c user.name="$Machine" -c user.email="whoentertains@gmail.com" commit -q -m "dailies($Machine): scheduled export $Yday..$Today" -m "Machine: $Machine`nAgent: tracker-daily-publish" 2>$null | Out-Null

foreach ($attempt in 1, 2) {
  git push -q origin HEAD:main 2>$null
  if ($LASTEXITCODE -eq 0) { Say "published $Yday..$Today (attempt $attempt) from $Repo"; exit 0 }
  git fetch -q origin main 2>$null
  git rebase -q --autostash origin/main 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { git rebase --abort 2>$null | Out-Null; break }
}
Say 'FAIL: push rejected twice -- commit is local, needs a human'
exit 1
