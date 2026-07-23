param(
    [string]$TaskName = 'ExchangeMarketMakerDaily',
    [string]$RunAt = '09:00'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Runner = Join-Path $ProjectRoot 'run.ps1'
if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Runner not found: $Runner"
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" run"
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Daily SSE/SZSE ETF market-making announcement report' `
    -Force | Out-Null
Write-Host "Scheduled task '$TaskName' created for $RunAt daily." -ForegroundColor Green
