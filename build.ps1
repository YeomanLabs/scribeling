<#
Builds Scribeling.exe.

    .\build.ps1            -> requires administrator (recommended)
    .\build.ps1 -NoAdmin   -> runs at normal integrity
    .\build.ps1 -Python <path\to\python.exe>  -> use a specific interpreter

If PowerShell warns that the script came from the internet:
    Unblock-File .\build.ps1

Dependencies install into a .venv beside this script, so they belong to whoever
runs the build rather than to a particular user profile.

Why admin by default: UIPI stops a medium-integrity process from reading the
UI Automation tree of an elevated window, so a non-elevated recorder produces
screenshots with no element names for anything run as admin. A high-integrity
process can read both high and medium, so it covers everything.
#>
param(
    [switch] $NoAdmin,
    # Explicit interpreter path. Useful from an elevated shell running under a
    # different account, where Python lives in someone else's profile.
    [string] $Python
)

$ErrorActionPreference = "Stop"

# PowerShell 5.1 turns ANY native-command stderr output into a terminating error
# when ErrorActionPreference is Stop. pip warns about PATH on stderr, which is
# harmless, so native calls get their own relaxed scope and are judged on their
# exit code instead.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string] $Exe,
        [string[]] $Arguments = @(),
        [string]   $What = "command"
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE."
    }
}

function Resolve-Python {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $candidates = New-Object System.Collections.Generic.List[string]

        foreach ($name in 'py', 'python', 'python3') {
            Get-Command $name -All -ErrorAction SilentlyContinue |
                ForEach-Object { $candidates.Add($_.Source) }
        }

        $globs = @(
            "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
            "$env:ProgramFiles\Python3*\python.exe",
            "${env:ProgramFiles(x86)}\Python3*\python.exe",
            # Last resort: an elevated shell under an admin account cannot see
            # the interactive user's LOCALAPPDATA, so sweep every profile.
            "$env:SystemDrive\Users\*\AppData\Local\Programs\Python\Python3*\python.exe"
        )
        Get-ChildItem $globs -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $candidates.Add($_.FullName) }

        foreach ($path in $candidates) {
            if ([string]::IsNullOrWhiteSpace($path)) { continue }
            # Skip the Microsoft Store app execution alias stub - not Python.
            if ((Split-Path (Split-Path $path -Parent) -Leaf) -eq 'WindowsApps') { continue }
            $major = & $path -c "import sys; print(sys.version_info.major)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $major -eq '3') { return $path }
        }
        return $null
    } finally {
        $ErrorActionPreference = $previous
    }
}

# Resolve everything against the script's own folder, not the caller's cwd.
$script = Join-Path $PSScriptRoot "skill\scribeling-guides\scripts\scribeling.py"
$reqs   = Join-Path $PSScriptRoot "requirements.txt"

if (-not (Test-Path $script)) {
    Write-Host "scribeling.py is not in $PSScriptRoot" -ForegroundColor Red
    Write-Host ""
    Write-Host "Expected skill\scribeling-guides\scripts\scribeling.py - run this from the repo root."
    exit 1
}

if ($Python) {
    if (-not (Test-Path $Python)) { throw "No interpreter at $Python" }
    $python = $Python
} else {
    $python = Resolve-Python
}
if (-not $python) {
    Write-Host "No Python 3 found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it, then close and reopen PowerShell so PATH refreshes:"
    Write-Host "  winget install --id Python.Python.3.13 -e --source winget --scope user" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Already installed under another account? Pass it directly:"
    Write-Host '  .\build.ps1 -Python "C:\Users\<user>\AppData\Local\Programs\Python\Python313\python.exe"' -ForegroundColor Cyan
    exit 1
}

Write-Host "Base interpreter: $python" -ForegroundColor Gray
Invoke-Native $python @("--version") "python --version"

# Everything goes in a venv beside the script. Without it, pip installs land in
# the site-packages of whichever account ran it - so packages installed as one
# user are invisible when you build as another, which is easy to hit on a machine
# with a separate admin account.
$venv    = Join-Path $PSScriptRoot ".venv"
$venvPy  = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host ""
    Write-Host "Creating virtual environment in .venv ..." -ForegroundColor Gray
    Invoke-Native $python @("-m", "venv", $venv) "venv creation"
}
if (-not (Test-Path $venvPy)) { throw "venv was created but $venvPy is missing." }

Write-Host ""
Write-Host "Installing dependencies into .venv ..." -ForegroundColor Gray
Invoke-Native $venvPy @("-m", "pip", "install", "--upgrade", "pip") "pip upgrade"
if (Test-Path $reqs) {
    Invoke-Native $venvPy @("-m", "pip", "install", "-r", $reqs, "pyinstaller") "pip install"
} else {
    # requirements.txt is convenience only - the list is short enough to inline.
    Invoke-Native $venvPy @("-m", "pip", "install", "pynput", "mss", "pillow",
                            "uiautomation", "pyinstaller") "pip install"
}

# Build with the venv interpreter, not the base one.
$python = $venvPy

# NOTE: do not name this $args - that is a PowerShell automatic variable.
$pyArgs = [System.Collections.Generic.List[string]]@(
    "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile",
    "--windowed",
    "--name", "Scribeling",
    # uiautomation generates COM interface wrappers at runtime; PyInstaller
    # cannot find them by static analysis.
    "--collect-all", "uiautomation",
    "--collect-all", "comtypes",
    "--collect-submodules", "pynput",
    "--exclude-module", "matplotlib",
    "--exclude-module", "numpy"
)
if (-not $NoAdmin) { $pyArgs.Add("--uac-admin") }
$pyArgs.Add($script)

Write-Host ""
Write-Host "Building..." -ForegroundColor Gray
Push-Location $PSScriptRoot
try {
    Invoke-Native $python $pyArgs "PyInstaller"
} finally {
    Pop-Location
}

$exe = Join-Path $PSScriptRoot "dist\Scribeling.exe"
Write-Host ""
if (Test-Path $exe) {
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "Built: $exe  ($size MB)" -ForegroundColor Green
    if (-not $NoAdmin) {
        Write-Host "Manifested to require administrator." -ForegroundColor Gray
    }
} else {
    Write-Host "PyInstaller reported success but $exe is missing." -ForegroundColor Red
    exit 1
}
