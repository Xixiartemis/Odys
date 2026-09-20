$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repo = "D:\projects\odys-p411-official"
$python = "D:\projects\odys\.venv\Scripts\python.exe"
$output = Join-Path $repo "results\official_phase4\cheap_model\golden_probe_v1"
$credentialName = "ODYS_CHEAP_BENCHMARK_API_KEY"

# HUMAN_SECRET_EXECUTION: presence gate only; never print or persist the value.
$credential = (Get-Item -Path "Env:$credentialName" -ErrorAction SilentlyContinue).Value
if ([string]::IsNullOrWhiteSpace($credential)) {
    Write-Output "CHEAP_CREDENTIAL=MISSING"
    throw "CREDENTIAL_REQUIRED_BEFORE_RUN"
}
Write-Output "CHEAP_CREDENTIAL=SET"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "PYTHON_RUNTIME_MISSING: $python"
}
if (Test-Path -LiteralPath $output) {
    $existing = @(Get-ChildItem -LiteralPath $output -Force)
    if ($existing.Count -gt 0) {
        throw "GOLDEN_PROBE_OUTPUT_EXISTS: $output"
    }
}

Set-Location -LiteralPath $repo
$env:PYTHONPATH = $repo

& $python -B "$repo\scripts\run_p4_golden_probe.py" `
    --repo-root $repo `
    --output $output
if ($LASTEXITCODE -ne 0) {
    throw "GOLDEN_PROBE_EXECUTION_FAILED:$LASTEXITCODE"
}

Write-Output "RESULT_PATH=$output"
