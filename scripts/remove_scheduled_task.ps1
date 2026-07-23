param([string]$TaskName = 'ExchangeMarketMakerDaily')

$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
