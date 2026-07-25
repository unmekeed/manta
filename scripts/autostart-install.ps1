# Автозапуск Manta на Windows (E1 роадмапа) — задачи Планировщика.
#
# Создаёт две задачи:
#   Manta-Recover  — при входе в систему: поднять Docker Desktop, дождаться
#                    демона и выполнить `make recover` в WSL (идемпотентно);
#   Manta-Backup   — ежедневно: слепок датасета с ротацией (scripts/backup.sh).
#
# Запускать в PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА, из Windows (не из WSL):
#   powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\home\<user>\manta\scripts\autostart-install.ps1
#
# Параметры (значения по умолчанию подходят для типовой установки):
#   -Distro     имя дистрибутива WSL (wsl -l -q покажет список)
#   -WslUser    пользователь внутри WSL (whoami в WSL)
#   -BackupAt   время ежедневного бэкапа, ЧЧ:ММ
#   -Uninstall  удалить обе задачи
#
# Проверить после установки:  Get-ScheduledTask -TaskName 'Manta-*'
# Запустить вручную:          Start-ScheduledTask -TaskName 'Manta-Recover'

param(
    [string]$Distro   = 'Ubuntu',
    [string]$WslUser  = $env:USERNAME,
    [string]$BackupAt = '05:30',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    foreach ($name in 'Manta-Recover', 'Manta-Backup') {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "удалена задача $name"
        }
    }
    exit 0
}

# Docker Desktop поднимает демон в собственной ВМ, поэтому его надо
# стартовать из Windows; `make recover` внутри WSL сам дождаться его не
# может (там нет sudo dockerd при WSL-интеграции).
$docker = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
if (-not (Test-Path $docker)) {
    Write-Warning "Docker Desktop не найден: $docker — задача Recover будет ждать демон, запущенный вручную"
}

# Ждём демон до 5 минут: после входа в систему Docker Desktop стартует не
# мгновенно, а recover без докера просто упадёт.
$recoverCmd = @"
if (Test-Path '$docker') { Start-Process '$docker' }
for (`$i = 0; `$i -lt 60; `$i++) {
    wsl -d $Distro -u $WslUser -- docker info 2>&1 | Out-Null
    if (`$LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
}
wsl -d $Distro -u $WslUser -- bash -lc 'cd ~/manta && MANTA_TRAIN_ENV=~/manta-train.env make recover'
"@

$backupCmd = "wsl -d $Distro -u $WslUser -- bash -lc 'cd ~/manta && ./scripts/backup.sh'"

function New-MantaTask($name, $command, $trigger, $description) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`""
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Description $description -RunLevel Highest | Out-Null
    Write-Host "создана задача $name"
}

New-MantaTask 'Manta-Recover' $recoverCmd `
    (New-ScheduledTaskTrigger -AtLogOn) `
    'Manta: поднять стек после входа в систему (идемпотентно)'

New-MantaTask 'Manta-Backup' $backupCmd `
    (New-ScheduledTaskTrigger -Daily -At $BackupAt) `
    'Manta: ежедневный слепок датасета с ротацией'

Write-Host ''
Write-Host 'Готово. Проверка:'
Write-Host '  Get-ScheduledTask -TaskName ''Manta-*'''
Write-Host '  Start-ScheduledTask -TaskName ''Manta-Recover''   # прогнать сейчас'
