param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$source = Join-Path (Join-Path $Root "tools") "pion_offer_probe"
$target = Join-Path (Join-Path $Root "custom_components") "tuya_recordings"

$builds = @(
    @{ GOARCH = "amd64"; GOARM = ""; Suffix = "linux_amd64" },
    @{ GOARCH = "arm64"; GOARM = ""; Suffix = "linux_arm64" },
    @{ GOARCH = "arm"; GOARM = "7"; Suffix = "linux_armv7" }
)

foreach ($build in $builds) {
    $env:GOOS = "linux"
    $env:GOARCH = $build.GOARCH
    $env:GOARM = $build.GOARM
    $env:CGO_ENABLED = "0"

    $output = Join-Path $target "pion_offer_$($build.Suffix)"
    Push-Location $source
    try {
        go build -buildvcs=false -trimpath -ldflags="-s -w" -o $output .
    }
    finally {
        Pop-Location
    }
}

Copy-Item -LiteralPath (Join-Path $target "pion_offer_linux_amd64") -Destination (Join-Path $target "pion_offer") -Force
Write-Host "Built Pion helpers."
