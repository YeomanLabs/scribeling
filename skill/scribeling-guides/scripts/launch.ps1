<#
Launches the recorder GUI detached, then proves the window actually exists.

Why this script exists instead of a Start-Process line in the instructions:
PowerShell's -ArgumentList joins array elements with spaces WITHOUT re-quoting
them, so @('gui','--title','Send an email') arrives at Python as five arguments
and argparse rejects the tail. Detached processes throw stderr away, so the
failure is completely silent - no window, no error. This script builds one
correctly quoted command line, captures stderr to a file regardless, and reports
what went wrong if no window appears.

    .\launch.ps1 -Title "Create an Autopilot profile" -Description "For deskside"
#>
param(
    [string] $Title = "",
    [string] $Description = "",
    [string] $OutDir = "",
    [int]    $TimeoutSeconds = 20
)

$ErrorActionPreference = "Stop"

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$script  = Join-Path $here "scribeling.py"
$homeDir = Join-Path $env:LOCALAPPDATA "scribeling"
$marker  = Join-Path $homeDir "python.txt"

if (-not (Test-Path $script)) { throw "scribeling.py is missing from $here" }

# Bootstrap on first run so this works out of the box from a single command.
if (-not (Test-Path $marker)) {
    Write-Host "First run - preparing the environment." -ForegroundColor Gray
    & powershell -ExecutionPolicy Bypass -File (Join-Path $here "setup.ps1")
    if ($LASTEXITCODE -ne 0) { throw "setup.ps1 failed; cannot launch." }
}

$python = (Get-Content $marker -Raw).Trim()
if (-not (Test-Path $python)) {
    Write-Host "Recorded interpreter is gone; re-running setup." -ForegroundColor Gray
    & powershell -ExecutionPolicy Bypass -File (Join-Path $here "setup.ps1")
    if ($LASTEXITCODE -ne 0) { throw "setup.ps1 failed; cannot launch." }
    $python = (Get-Content $marker -Raw).Trim()
}

# Embedded double quotes would terminate the argument early. Titles do not need
# them, so fold them to single quotes rather than fighting CommandLineToArgvW.
function Clean([string]$text) { return ($text -replace '"', "'") }

$parts = @('"{0}" gui' -f $script)
if ($Title)       { $parts += '--title "{0}"'       -f (Clean $Title) }
if ($Description) { $parts += '--description "{0}"' -f (Clean $Description) }
if ($OutDir)      { $parts += '--outdir "{0}"'      -f (Clean $OutDir) }
# ONE string, passed through verbatim. Never an array.
$argLine = $parts -join ' '

$errFile = Join-Path $env:TEMP ("scribeling-launch-{0}.err" -f $PID)
if (Test-Path $errFile) { Remove-Item $errFile -Force }

Write-Host "Launching: $python $argLine" -ForegroundColor Gray
$proc = Start-Process -FilePath $python -ArgumentList $argLine `
                      -RedirectStandardError $errFile -PassThru

# A GUI that fails on an argument error exits in well under a second, so waiting
# for a window title is the only honest confirmation that it came up.
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$title = ""
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 300
    if ($proc.HasExited) { break }
    $proc.Refresh()
    if ($proc.MainWindowTitle) { $title = $proc.MainWindowTitle; break }
}

if ($title) {
    Write-Host ""
    Write-Host "Recorder is up. PID $($proc.Id), window '$title'." -ForegroundColor Green
    Write-Host "WINDOW=OK"
    exit 0
}

Write-Host ""
if ($proc.HasExited) {
    Write-Host "The recorder exited immediately (code $($proc.ExitCode))." -ForegroundColor Red
} else {
    Write-Host "No window appeared within $TimeoutSeconds seconds." -ForegroundColor Red
}
if ((Test-Path $errFile) -and (Get-Item $errFile).Length -gt 0) {
    Write-Host "--- stderr ---" -ForegroundColor Yellow
    Get-Content $errFile | ForEach-Object { Write-Host $_ }
} else {
    Write-Host "Nothing on stderr. Reproduce in the foreground to see the error:"
    Write-Host "  & `"$python`" `"$script`" gui" -ForegroundColor Cyan
}
Write-Host "WINDOW=FAILED"
exit 1
