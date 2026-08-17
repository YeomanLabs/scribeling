<#
Packages skill/scribeling-guides into dist/scribeling-guides.skill.

A .skill file is a zip whose root is the skill folder itself, so the archive must
contain scribeling-guides/SKILL.md rather than SKILL.md at the top level.
#>
$ErrorActionPreference = "Stop"

$root  = Split-Path -Parent $PSScriptRoot
$skill = Join-Path $root "skill\scribeling-guides"
$dist  = Join-Path $root "dist"
$out   = Join-Path $dist "scribeling-guides.skill"

if (-not (Test-Path (Join-Path $skill "SKILL.md"))) {
    throw "No SKILL.md under $skill"
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null

# Compress-Archive refuses any extension but .zip, so build it as a zip and
# rename afterwards.
$zip = Join-Path $dist "scribeling-guides.zip"
foreach ($stale in @($zip, $out)) {
    if (Test-Path $stale) { Remove-Item $stale -Force }
}

# Exclude bytecode so the archive is reproducible between machines.
$staging = Join-Path $env:TEMP ("scribeling-pack-{0}" -f $PID)
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging | Out-Null
Copy-Item $skill -Destination $staging -Recurse
Get-ChildItem $staging -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Compress-Archive -Path (Join-Path $staging "scribeling-guides") `
                 -DestinationPath $zip -CompressionLevel Optimal
Move-Item $zip $out
Remove-Item $staging -Recurse -Force

$size = [math]::Round((Get-Item $out).Length / 1KB, 1)
Write-Host "Packaged: $out  ($size KB)" -ForegroundColor Green
