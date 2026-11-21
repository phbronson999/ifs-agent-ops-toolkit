# Build the Rust/WASM capture core into extension/pkg/ (then load extension/ unpacked in Edge or Chrome).
#
# Prerequisites (once):
#   rustup target add wasm32-unknown-unknown
#   cargo install wasm-bindgen-cli --version 0.2.100 --locked   # must equal the wasm-bindgen crate version
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$core = Join-Path $root "core"
$out = Join-Path $root "extension\pkg"

$expected = "0.2.100"
$cli = (& wasm-bindgen --version) 2>$null
if (-not $cli -or -not $cli.Contains($expected)) {
    throw "wasm-bindgen CLI $expected is required (found: '$cli'). Run: cargo install wasm-bindgen-cli --version $expected --locked"
}

Push-Location $core
try {
    cargo test --quiet
    if ($LASTEXITCODE -ne 0) { throw "capture core tests failed" }
    cargo build --release --target wasm32-unknown-unknown --features wasm
    if ($LASTEXITCODE -ne 0) { throw "wasm build failed" }
} finally {
    Pop-Location
}

$wasm = Join-Path $core "target\wasm32-unknown-unknown\release\ifs_capture_core.wasm"
wasm-bindgen $wasm --target web --no-typescript --out-dir $out
if ($LASTEXITCODE -ne 0) { throw "wasm-bindgen failed" }
if (Get-Command node -ErrorAction SilentlyContinue) {
    node (Join-Path $root "tests\wasm_smoke.mjs")
    if ($LASTEXITCODE -ne 0) { throw "wasm smoke test failed" }
}
Write-Host "Built $out. Load $(Join-Path $root 'extension') as an unpacked extension (edge://extensions or chrome://extensions, Developer mode)."
