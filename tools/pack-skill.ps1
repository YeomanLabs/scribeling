<#
Packages skill/scribeling-guides into dist/scribeling-guides.skill.

Does NOT use Compress-Archive. PowerShell 5.1 writes path separators inside the
archive as backslashes, which violates the zip spec (APPNOTE 4.4.17.1 requires
forward slashes) and makes Claude's skill uploader reject the file with
"Zip file contains path with invalid characters". Entries are written by hand so
the separator is correct regardless of PowerShell version.

The archive root must be the skill folder itself: scribeling-guides/SKILL.md,
not SKILL.md at the top level.
#>
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root  = Split-Path -Parent $PSScriptRoot
$skill = Join-Path $root "skill\scribeling-guides"
$dist  = Join-Path $root "dist"
$out   = Join-Path $dist "scribeling-guides.skill"

if (-not (Test-Path (Join-Path $skill "SKILL.md"))) {
    throw "No SKILL.md under $skill"
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null
if (Test-Path $out) { Remove-Item $out -Force }

$parent = Split-Path -Parent $skill   # so entries start with scribeling-guides/
$files = Get-ChildItem $skill -Recurse -File |
         Where-Object { $_.FullName -notmatch '__pycache__' -and $_.Extension -ne '.pyc' }

$stream  = [System.IO.File]::Open($out, [System.IO.FileMode]::Create)
$archive = New-Object System.IO.Compression.ZipArchive(
    $stream, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in $files) {
        # Relative path, forward slashes, no leading separator.
        $name = $file.FullName.Substring($parent.Length).TrimStart('\', '/').Replace('\', '/')
        $entry = $archive.CreateEntry($name,
                     [System.IO.Compression.CompressionLevel]::Optimal)
        $entryStream = $entry.Open()
        try {
            $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
            $entryStream.Write($bytes, 0, $bytes.Length)
        } finally {
            $entryStream.Dispose()
        }
        Write-Host "  $name"
    }
} finally {
    $archive.Dispose()
    $stream.Dispose()
}

# Verify before anyone tries to upload it.
$check = [System.IO.Compression.ZipFile]::OpenRead($out)
try {
    $bad = $check.Entries | Where-Object { $_.FullName -match '\\' }
    if ($bad) { throw "Backslash in entry names: $($bad.FullName -join ', ')" }
    if (-not ($check.Entries |
              Where-Object { $_.FullName -eq 'scribeling-guides/SKILL.md' })) {
        throw "scribeling-guides/SKILL.md is not at the archive root."
    }
} finally {
    $check.Dispose()
}

$size = [math]::Round((Get-Item $out).Length / 1KB, 1)
Write-Host ""
Write-Host "Packaged: $out  ($size KB)" -ForegroundColor Green
