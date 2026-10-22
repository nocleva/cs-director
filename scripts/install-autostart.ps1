# SquadMind'ı oturum açılınca otomatik başlatan Görev Zamanlayıcı görevini kurar (yönetici gerekmez).
# Kaldırmak için: Unregister-ScheduledTask -TaskName 'SquadMind' -Confirm:$false

$script = Join-Path $PSScriptRoot 'start-squadmind.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Süre sınırı yok; pilde de çalışsın; başarısız olursa 1 dk sonra 3 kez yeniden dene
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3
Register-ScheduledTask -TaskName 'SquadMind' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'SquadMind web sunucusu + app.squadmind.info tüneli (oturum açılınca)' -Force | Out-Null
Write-Output "Görev kuruldu: SquadMind (oturum açılınca başlar)"
