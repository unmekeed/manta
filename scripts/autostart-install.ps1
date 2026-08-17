# ВНИМАНИЕ: файл обязан храниться в UTF-8 С BOM. Windows PowerShell
# 5.1 без BOM читает .ps1 в ANSI-кодировке системы (для русской
# локали — Windows-1251), кириллица в строках разваливается вплоть до
# поломки кавычек, и скрипт падает на РАЗБОРЕ, не дойдя до первой
# команды. Проверяется тестом scripts/tests/test_daily_report.py.
# Автозапуск Manta на Windows (E1 роадмапа) — задачи Планировщика.
#
# Создаёт четыре задачи:
#   Manta-Anchor   — при входе в систему и далее каждые 10 минут: держать
#                    WSL живым, чтобы окно терминала можно было закрыть на
#                    крестик, не погасив стек (scripts/wsl-anchor.sh);
#   Manta-Recover  — при входе в систему: поднять Docker Desktop, дождаться
#                    демона и выполнить `make recover` в WSL (идемпотентно);
#   Manta-Backup   — ежедневно: слепок датасета с ротацией (scripts/backup.sh);
#   Manta-Report   — ежедневно: снимок диагностики за день, хранится 30 дней
#                    (scripts/daily-report.sh).
#
# Запускать в PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА, из Windows (не из WSL):
#   powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\home\<user>\manta\scripts\autostart-install.ps1
#
# Параметры (значения по умолчанию подходят для типовой установки):
#   -Distro     имя дистрибутива WSL (wsl -l -q покажет список)
#   -WslUser    пользователь внутри WSL (whoami в WSL)
#   -BackupAt   время ежедневного бэкапа, ЧЧ:ММ
#   -ReportAt   время ежедневного отчёта, ЧЧ:ММ
#   -Uninstall  удалить обе задачи
#
# Проверить после установки:  Get-ScheduledTask -TaskName 'Manta-*'
# Запустить вручную:          Start-ScheduledTask -TaskName 'Manta-Recover'

param(
    [string]$Distro   = 'Ubuntu',
    [string]$WslUser  = $env:USERNAME,
    [string]$BackupAt = '05:30',
    # Отчёт снимается ПОСЛЕ бэкапа: если бэкап сломается, это попадёт
    # в отчёт того же дня, а не следующего.
    [string]$ReportAt = '06:00',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    foreach ($name in 'Manta-Anchor', 'Manta-Recover', 'Manta-Backup', 'Manta-Report') {
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
$reportCmd = "wsl -d $Distro -u $WslUser -- bash -lc 'cd ~/manta && ./scripts/daily-report.sh'"

function New-MantaTask($name, $command, $trigger, $description, $settings) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`""
    if (-not $settings) {
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    }
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Description $description -RunLevel Highest | Out-Null
    Write-Host "создана задача $name"
}

# Якорь WSL. Отдельными настройками, потому что общие ему не подходят:
#
#   ExecutionTimeLimit 0  — «без ограничения». С общими двумя часами
#     Планировщик убивал бы якорь дважды в рабочий день, и стек падал бы
#     при закрытии окна — ровно то, от чего якорь и ставится.
#   MultipleInstances IgnoreNew — повторное срабатывание не поднимает
#     второй экземпляр. Сам скрипт тоже это проверяет (pidfile + flock),
#     но полагаться на одну защиту там, где их можно поставить две,
#     незачем.
#
# Повтор каждые 10 минут — это самолечение. `wsl --shutdown`, перезапуск
# Docker Desktop и выход из спящего режима гасят якорь, и без повтора он
# вернулся бы только при следующем входе в систему.
$anchorSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

$anchorTrigger = New-ScheduledTaskTrigger -AtLogOn
$anchorTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition

$anchorCmd = "wsl -d $Distro -u $WslUser -- bash -lc 'cd ~/manta && ./scripts/wsl-anchor.sh run'"

New-MantaTask 'Manta-Anchor' $anchorCmd $anchorTrigger `
    'Manta: держать WSL живым, чтобы окно можно было закрыть на крестик' `
    $anchorSettings

New-MantaTask 'Manta-Recover' $recoverCmd `
    (New-ScheduledTaskTrigger -AtLogOn) `
    'Manta: поднять стек после входа в систему (идемпотентно)'

New-MantaTask 'Manta-Backup' $backupCmd `
    (New-ScheduledTaskTrigger -Daily -At $BackupAt) `
    'Manta: ежедневный слепок датасета с ротацией'

New-MantaTask 'Manta-Report' $reportCmd `
    (New-ScheduledTaskTrigger -Daily -At $ReportAt) `
    'Manta: ежедневный снимок диагностики (хранится 30 дней)'

Write-Host ''
Write-Host 'Готово. Проверка:'
Write-Host '  Get-ScheduledTask -TaskName ''Manta-*'''
Write-Host '  Start-ScheduledTask -TaskName ''Manta-Recover''   # прогнать сейчас'
