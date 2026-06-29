param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$component = Join-Path (Join-Path $Root "custom_components") "tuya_recordings"
$pythonFiles = Get-ChildItem -LiteralPath $component -Recurse -Filter "*.py" |
    Select-Object -ExpandProperty FullName

$compileArgs = @("-m", "py_compile") + $pythonFiles
& python @compileArgs
if ($LASTEXITCODE -ne 0) {
    throw "Python compile failed."
}
$translationCheckArgs = @(
    "-c",
    "import json, pathlib, sys; root=pathlib.Path(sys.argv[1]); strings=json.loads((root/'custom_components'/'tuya_recordings'/'strings.json').read_text()); en=json.loads((root/'custom_components'/'tuya_recordings'/'translations'/'en.json').read_text()); assert strings == en, 'strings.json and translations/en.json differ'",
    $Root
)
& python @translationCheckArgs
if ($LASTEXITCODE -ne 0) {
    throw "Translation validation failed."
}

$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:PYTHONPATH = $Root
Push-Location $Root
try {
    & python -m pytest -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) {
        throw "Pytest failed."
    }
}
finally {
    Pop-Location
}

$oldNames = rg "tuya_protect_recordings|Tuya Protect Recordings|ProtectRecordings|ProtectAuth|ProtectApi|TuyaProtect|Camera Bridge|protect_recordings|tuya-protect|tuya_protect|aiortc" $Root -g "!**/__pycache__/**" -g "!**/validate.ps1" -g "!**/build_helpers.ps1" 2>$null
if ($LASTEXITCODE -eq 0) {
    $oldNames
    throw "Old integration naming or stale WebRTC references remain."
}
if ($LASTEXITCODE -ne 1) {
    throw "Name scan failed."
}

$helpers = @(
    "pion_offer_linux_amd64",
    "pion_offer_linux_arm64",
    "pion_offer_linux_armv7"
) | ForEach-Object { Join-Path $component $_ }

foreach ($helper in $helpers) {
    if (-not (Test-Path -LiteralPath $helper)) {
        throw "Missing bundled Pion helper: $helper"
    }
}

Get-ChildItem -LiteralPath $Root -Recurse -Force -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $Root -Recurse -Force -Directory -Filter ".pytest_cache" |
    Remove-Item -Recurse -Force

Write-Host "Tuya Recordings validation passed."
