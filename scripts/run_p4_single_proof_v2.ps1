$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# HUMAN_SECRET_EXECUTION: presence only; never print or persist the value.
$credentialName = "ODYS_CHEAP_BENCHMARK_API_KEY"
$credential = (Get-Item -Path "Env:$credentialName" -ErrorAction SilentlyContinue).Value
if ([string]::IsNullOrWhiteSpace($credential)) {
    Write-Output "CHEAP_CREDENTIAL=MISSING"
    throw "CREDENTIAL_REQUIRED_BEFORE_RUN"
}
Write-Output "CHEAP_CREDENTIAL=SET"

$repoRoot = "D:\projects\odys-p411-official"
$pythonExe = "D:\projects\odys\.venv\Scripts\python.exe"
$outputPath = "D:\projects\odys-p411-official\results\official_phase4\cheap_model\single_proof_v2"
$runner = "$repoRoot\scripts\run_p4_single_proof_v2.py"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "PYTHON_RUNTIME_MISSING: $pythonExe"
}
if (Test-Path -LiteralPath $outputPath) {
    $existing = @(Get-ChildItem -LiteralPath $outputPath -Force)
    if ($existing.Count -gt 0) {
        throw "SINGLE_PROOF_OUTPUT_EXISTS: $outputPath"
    }
}

Set-Location -LiteralPath $repoRoot
$env:PYTHONPATH = "$repoRoot;$repoRoot\src"

& $pythonExe -B $runner `
    --repo-root $repoRoot `
    --output $outputPath
if ($LASTEXITCODE -ne 0) {
    throw "SINGLE_PROOF_V2_EXECUTION_FAILED:$LASTEXITCODE"
}

Write-Output "SINGLE_PROOF_V2_EXECUTION_COMPLETE"
Write-Output "RESULT_PATH=$outputPath"
