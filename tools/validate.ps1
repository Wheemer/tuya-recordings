param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [switch]$Browser
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

& python -m ruff check custom_components tests
if ($LASTEXITCODE -ne 0) {
    throw "Ruff validation failed."
}

$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:PYTHONPATH = $Root
Push-Location $Root
try {
    & python -m pytest -p no:cacheprovider -p pytest_asyncio.plugin tests
    if ($LASTEXITCODE -ne 0) {
        throw "Pytest failed."
    }
}
finally {
    Pop-Location
}

if ($Browser) {
    Push-Location $Root
    try {
        & python tests/native_player.browser.py
        if ($LASTEXITCODE -ne 0) {
            throw "Native player browser validation failed."
        }
        & python tests/panel_style.browser.py
        if ($LASTEXITCODE -ne 0) {
            throw "Panel browser validation failed."
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host "Tuya Recordings validation passed."
