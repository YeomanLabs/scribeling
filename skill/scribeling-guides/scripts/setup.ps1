<#
Prepares the recorder's Python environment. Idempotent - safe to run every time.

Prints the path of the interpreter to use on the last line, prefixed with
PYTHON=, so the caller can capture it without parsing the whole output.
#>
$ErrorActionPreference = "Stop"

# PowerShell 5.1 promotes ANY native stderr output to a terminating error when
# ErrorActionPreference is Stop. pip warns about PATH on stderr, which is
# harmless, so native calls are judged on their exit code instead.
function Invoke-Native {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @(),
          [string]$What = "command")
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host $_ } }
    finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE." }
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
        # Sweep every profile: an elevated shell under a separate admin account
        # cannot see the interactive user's LOCALAPPDATA.
        $globs = @(
            "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
            "$env:ProgramFiles\Python3*\python.exe",
            "$env:SystemDrive\Users\*\AppData\Local\Programs\Python\Python3*\python.exe"
        )
        Get-ChildItem $globs -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $candidates.Add($_.FullName) }

        foreach ($path in $candidates) {
            if ([string]::IsNullOrWhiteSpace($path)) { continue }
            # The Microsoft Store app execution alias stub is not Python.
            if ((Split-Path (Split-Path $path -Parent) -Leaf) -eq 'WindowsApps') { continue }
            $major = & $path -c "import sys; print(sys.version_info.major)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $major -eq '3') { return $path }
        }
        return $null
    } finally { $ErrorActionPreference = $previous }
}

# The venv lives outside the skill folder, which may be read-only, and is keyed
# to the running user so a separate admin account gets its own.
$home_dir = Join-Path $env:LOCALAPPDATA "scribeling"
$venv     = Join-Path $home_dir "venv"
$venvPy   = Join-Path $venv "Scripts\python.exe"

$marker = Join-Path $home_dir "python.txt"

if (Test-Path $venvPy) {
    $check = & $venvPy -c "import pynput, mss, PIL, uiautomation" 2>&1
    if ($LASTEXITCODE -eq 0) {
        New-Item -ItemType Directory -Force -Path $home_dir | Out-Null
        Set-Content -Path $marker -Value $venvPy -Encoding ASCII
        Write-Host "Environment already good." -ForegroundColor Green
        Write-Host "PYTHON=$venvPy"
        exit 0
    }
    Write-Host "Environment incomplete, reinstalling dependencies." -ForegroundColor Gray
}

$python = Resolve-Python
if (-not $python) {
    Write-Host "No Python 3 found." -ForegroundColor Red
    Write-Host "Install it, then reopen the terminal so PATH refreshes:"
    Write-Host "  winget install --id Python.Python.3.13 -e --source winget --scope user" -ForegroundColor Cyan
    exit 1
}

Write-Host "Base interpreter: $python" -ForegroundColor Gray
New-Item -ItemType Directory -Force -Path $home_dir | Out-Null

if (-not (Test-Path $venvPy)) {
    Write-Host "Creating venv at $venv" -ForegroundColor Gray
    Invoke-Native $python @("-m", "venv", $venv) "venv creation"
}
if (-not (Test-Path $venvPy)) { throw "venv created but $venvPy is missing." }

Invoke-Native $venvPy @("-m", "pip", "install", "--upgrade", "pip") "pip upgrade"
Invoke-Native $venvPy @("-m", "pip", "install", "pynput", "mss", "pillow",
                        "uiautomation") "dependency install"

# Later steps read this instead of parsing output or guessing at 'python'.
Set-Content -Path $marker -Value $venvPy -Encoding ASCII

Write-Host ""
Write-Host "Ready." -ForegroundColor Green
Write-Host "PYTHON=$venvPy"
