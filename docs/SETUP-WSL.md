# Развёртывание Manta на чистой Windows (WSL2)

Пошагово: от «WSL не имеет установленных дистрибутивов» до работающего
сбора и обучения. Проверено на переустановке 2026-07-31.

Все команды после шага 1 выполняются **внутри WSL** (Ubuntu), не в
PowerShell.

---

## 1. WSL2 и Ubuntu

В PowerShell **от администратора**:

```powershell
wsl --install -d Ubuntu-24.04
```

Команда ставит подсистему и дистрибутив, после чего требует
**перезагрузку**. После неё откроется окно Ubuntu — задайте имя
пользователя и пароль (они не связаны с учёткой Windows).

Проверка, что встала именно вторая версия:

```powershell
wsl -l -v          # VERSION должен быть 2
```

Если `VERSION 1` — исправить:

```powershell
wsl --set-version Ubuntu-24.04 2
```

---

## 2. Базовые пакеты

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    build-essential cmake pkg-config git curl wget unzip \
    python3 python3-pip python3-venv \
    postgresql-client
```

`unzip` нужен установщику rclone (без него он падает), `cmake` и
`build-essential` — сборке C++ ядра парсера, `postgresql-client` — для
`psql` из инструкций по диагностике.

---

## 3. Docker внутри WSL

Docker Desktop не обязателен — `make recover` поднимает демон сам:

```bash
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker "$USER"
```

Группа применится после перезахода:

```bash
exit
```

Снова откройте Ubuntu и проверьте:

```bash
sudo dockerd >/tmp/dockerd.log 2>&1 &
sleep 5 && docker info >/dev/null && echo "docker работает"
```

Дальше демон будет стартовать автоматически — это первый шаг
`dev-recover.sh`.

---

## 4. Go 1.25.12

Версия зафиксирована в `apps/api-gateway/go.mod`; более старая не
соберёт проект, а `apt` даёт устаревшую.

```bash
cd /tmp
wget https://go.dev/dl/go1.25.12.linux-amd64.tar.gz
sudo rm -rf /usr/local/go
sudo tar -C /usr/local -xzf go1.25.12.linux-amd64.tar.gz
echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
source ~/.bashrc
go version          # go1.25.12
```

---

## 5. Node.js (веб-интерфейс)

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node -v
```

---

## 6. Репозиторий

```bash
cd ~
git clone https://github.com/unmekeed/manta.git
cd manta
```

Если репозиторий приватный, git попросит логин и пароль — вместо пароля
нужен **personal access token** GitHub (Settings → Developer settings →
Personal access tokens). Чтобы не вводить каждый раз:

```bash
git config --global credential.helper store
```

---

## 7. Файл секретов

Живёт **вне** репозитория и в git не попадает никогда.

```bash
nano ~/manta-train.env
```

Содержимое для ПК №1 (на ПК №2 отличается только `COLLECTOR_SHARD_ID=1`):

```
# --- шардирование между машинами ---
COLLECTOR_SHARD_COUNT=2
COLLECTOR_SHARD_ID=0

# --- темп сбора под квоту OpenDota ---
TIMELINE_LIMIT=14
PRO_TIMELINE_LIMIT=4
OPENDOTA_LIMIT=2

# --- STRATZ (необязательно; лимит 10000/сутки делится на все машины) ---
STRATZ_API_TOKEN=
STRATZ_LIMIT=40
STRATZ_PRO_LIMIT=10

# --- ключи внешних сервисов ---
OPENDOTA_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# --- оффсайт-бэкап (необязательно) ---
MANTA_CLOUD_REMOTE=
```

Чего в этом файле быть НЕ должно: строки `METRICS_PORT=...`. Она
читается через `set -a` и раздаётся всем сервисам сразу — первый
стартовавший занимает порт, остальные падают с «Address already in use»
(инцидент 2026-07-27).

---

## 8. Данные

Стек поднимется и с пустой витриной — сбор наполнит её сам, но модель
без данных не обучится. Три источника слепка, по убыванию удобства.

**Локальный диск Windows.** Если раздел с бэкапами не форматировали, он
на месте:

```bash
ls -la /mnt/d/manta-backup/ 2>/dev/null || ls -la /mnt/c/manta-backups/
mkdir -p ~/manta-backups
cp /mnt/d/manta-backup/manta-dataset*.tar ~/manta-backups/
```

**Облако через rclone**, если настраивался `MANTA_CLOUD_REMOTE`:

```bash
sudo apt install -y rclone
rclone config                     # завести ремоут заново
rclone copy <remote>:manta ~/manta-backups --include 'manta-dataset-*.tar'
```

**Со второй машины**, если жива:

```bash
# на ПК №2
cd ~/manta && make dataset-export OUT=/mnt/d/manta-backup/manta-dataset.tar
# перенести файл на ПК №1 в ~/manta-backups/
```

Класть в `~/manta-backups` — этот каталог `manta up` проверяет сам.

---

## 9. Первый запуск

```bash
cd ~/manta
sudo ln -sf "$HOME/manta/scripts/manta" /usr/local/bin/manta
manta up
```

`manta up` делает всё: `git pull`, поднимает инфраструктуру, применяет
миграции, ставит python-зависимости, запускает сервисы и коллекторы,
а затем **восстанавливает бэкап, если витрина почти пуста** (порог
`MANTA_RESTORE_THRESHOLD`, по умолчанию 100 матчей). На машине с данными
шаг восстановления пропускается, так что команду безопасно повторять.

Первый запуск дольше обычного: скачиваются образы, ставятся зависимости,
собирается C++ ядро.

---

## 10. Проверка

```bash
manta doctor
```

Ожидается `ЗДОРОВ (warn: 0)`. Что смотреть, если нет:

| строка | значит |
|---|---|
| контейнер `missing` | инфраструктура не поднялась — повторить `manta up` |
| топик ОТСУТСТВУЕТ | `make topics` |
| `ReplayEvents ПУСТА` | нормально на свежей машине, наполнится сбором |
| `витрина ... пуста` | бэкап не восстановился — см. шаг 8 |
| процессы DOWN | смотреть `~/manta-logs/<сервис>.log` |

Адреса после запуска:

```
http://localhost:5173    веб-интерфейс
http://localhost:9107    дашборд обучения
http://localhost:8080    REST API
```

---

## 11. Автозапуск при входе в Windows

Чтобы стек поднимался сам после перезагрузки — в PowerShell **от
администратора**:

```powershell
cd \\wsl$\Ubuntu-24.04\home\<пользователь>\manta\scripts
.\autostart-install.ps1
```

Скрипт заводит две задачи планировщика: `Manta-Recover` (при входе) и
`Manta-Backup` (ежедневный слепок с ротацией).

---

## 12. Чего НЕ делать

Список выстрадан — каждая строка стоила данных:

- `docker compose down -v` — удаляет volume вместе с датасетом;
- `docker system prune --volumes` — то же самое;
- в Docker Desktop: «Clean / Purge data», «Factory reset»;
- `wsl --unregister Ubuntu-24.04` — сносит всю файловую систему WSL,
  включая базы.

Бэкап на `/mnt/c` или `/mnt/d` переживает переустановку WSL — в отличие
от всего, что лежит внутри дистрибутива. Держите слепки там.

---

## 13. Дальше

- как устроена модель и что означают метрики — `docs/MODEL-GUIDE.md`;
- что делать при инцидентах — `docs/runbooks.md`;
- история решений и почему сделано именно так — `docs/HANDOFF.md`.
