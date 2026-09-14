$ErrorActionPreference = 'Stop'

$repoRoot = 'D:\projects\odys-p411-official'
$pythonExe = 'D:\projects\odys\.venv\Scripts\python.exe'
$runner = Join-Path $repoRoot 'scripts\run_job_ready_recovery_single.py'
$outputPath = Join-Path $repoRoot 'results\job_ready_recovery_v1\single_odys_r1'

$credential = [Environment]::GetEnvironmentVariable('ODYS_CHEAP_BENCHMARK_API_KEY')
if ([string]::IsNullOrWhiteSpace($credential)) {
    Write-Output 'CHEAP_CREDENTIAL=MISSING'
    exit 2
}
Write-Output 'CHEAP_CREDENTIAL=SET'

if (Test-Path -LiteralPath $outputPath) {
    $entries = @(Get-ChildItem -LiteralPath $outputPath -Force)
    if ($entries.Count -gt 0) {
        throw "OUTPUT_COLLISION: $outputPath"
    }
}

$env:PYTHONPATH = $repoRoot
& $pythonExe -B $runner --repo-root $repoRoot --output $outputPath
$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
    Write-Output 'JOB_READY_SINGLE_EXECUTION_COMPLETE'
    Write-Output "RESULT_PATH=$outputPath"
}
exit $exitCode
