<#
Runs the bot 24/7 as a Windows scheduled task: starts at boot (no login needed),
runs hidden in the background, and restarts itself if it crashes.

Run from an ADMIN PowerShell in this folder:
    .\bot_task.ps1 install     # register and start it
    .\bot_task.ps1 status      # is it running? + last lines of bot.log
    .\bot_task.ps1 restart     # e.g. after editing bot.py
    .\bot_task.ps1 stop
    .\bot_task.ps1 start
    .\bot_task.ps1 uninstall
If script execution is blocked:  powershell -ExecutionPolicy Bypass -File .\bot_task.ps1 install
#>
param([ValidateSet("install", "uninstall", "start", "stop", "restart", "status")]
      [string]$Action = "status")

$TaskName = "DiscordClaudeBot"
$Here = $PSScriptRoot
$Runner = Join-Path $Here "run_bot.cmd"
$Log = Join-Path $Here "bot.log"

function Stop-Bot {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    # Make sure no bot.py from this folder is left running (the task may not kill children).
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" |
        Where-Object { $_.CommandLine -match [regex]::Escape("bot.py") -or
                       $_.CommandLine -match [regex]::Escape($Runner) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

switch ($Action) {
    "install" {
        $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Runner`"" -WorkingDirectory $Here
        $trigger = New-ScheduledTaskTrigger -AtStartup
        # S4U = run as you, whether or not you're logged in, with no window and no stored password.
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Installed and started '$TaskName'. Log: $Log"
    }
    "uninstall" { Stop-Bot; Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Write-Host "Removed." }
    "start"     { Start-ScheduledTask -TaskName $TaskName; Write-Host "Started." }
    "stop"      { Stop-Bot; Write-Host "Stopped." }
    "restart"   { Stop-Bot; Start-ScheduledTask -TaskName $TaskName; Write-Host "Restarted." }
    "status" {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $t) { Write-Host "Not installed. Run: .\bot_task.ps1 install"; break }
        $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -match "bot\.py" }
        Write-Host "Task state: $($t.State)   bot.py running: $([bool]$running)"
        if (Test-Path $Log) { Write-Host "`n--- last 15 lines of bot.log ---"; Get-Content $Log -Tail 15 }
    }
}
