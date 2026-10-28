<#
Starts the bot hidden in the background (run_bot.cmd, which restarts it if it crashes),
unless it's already running. Safe to run any number of times.
    powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File start_bot.ps1
#>
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" |
    Where-Object { $_.CommandLine -match 'bot\.py|run_bot\.cmd' }
if ($running) { exit 0 }
Start-Process cmd.exe -ArgumentList '/c', "`"$PSScriptRoot\run_bot.cmd`"" `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
