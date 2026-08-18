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
    libsnappy-dev libgomp1 \
    python3 python3-pip python3-venv \
    postgresql-client
```

`libsnappy-dev` обязателен: реплеи Valve сжаты snappy покадрово, и
`CMakeLists.txt` ищет библиотеку через `find_library(... REQUIRED)` —
без неё сборка ядра падает на `Could not find SNAPPY_LIB`. `libgomp1`
нужен LightGBM (OpenMP), `unzip` — установщику rclone, `cmake` и
`build-essential` — сборке C++ ядра, `postgresql-client` даёт `psql`
для диагностических запросов из runbooks.

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

Отличий между машинами ровно три: `COLLECTOR_SHARD_ID`,
`MANTA_HOST_LABEL` и наличие ключей Steam GC.

**ПК №1** — сбор как обычно:

```
# --- кто я (спринт 142) ---
# Метка идёт в имя слепка: manta-dataset-<метка>-<UTC>.tar. Без неё обе
# машины кладут в облако неразличимые файлы, и обмен невозможен —
# нельзя отличить свой слепок от чужого. Метки ОБЯЗАНЫ различаться.
MANTA_HOST_LABEL=pc1

# --- шардирование между машинами ---
# Матчи делятся по остатку match_id: ПК №1 берёт чётные, ПК №2 нечётные.
# Одинаковый SHARD_ID на обеих машинах = обе качают одно и то же.
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

# --- обмен датасетом через облако ---
# Один и тот же ремоут на обеих машинах: туда backup.sh кладёт свой
# слепок, оттуда peer-sync.sh забирает чужой.
MANTA_CLOUD_REMOTE=gdrive-crypt:manta-backups
```

**ПК №2** — то же самое, но с другой меткой, другим шардом и ключами GC:

```
MANTA_HOST_LABEL=pc2
COLLECTOR_SHARD_COUNT=2
COLLECTOR_SHARD_ID=1

TIMELINE_LIMIT=14
PRO_TIMELINE_LIMIT=4
OPENDOTA_LIMIT=2

STRATZ_API_TOKEN=
STRATZ_LIMIT=40
STRATZ_PRO_LIMIT=10

OPENDOTA_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

MANTA_CLOUD_REMOTE=gdrive-crypt:manta-backups

# --- Steam GC (только ПК №2) ---
# Логин и пароль нужны РОВНО ОДИН РАЗ, для `make gc-token`: он меняет их
# на refresh-токен и кладёт его 0600 в ~/.manta-gc. Дальше вход идёт
# токеном, а эти две строки можно стереть.
STEAM_BOT_LOGIN=
STEAM_BOT_PASSWORD=
STEAM_API_KEY=
```

### 7а. Ежедневный обмен между машинами (спринт 142)

Машины собирают РАЗНЫЕ матчи (шардирование по остатку `match_id`), поэтому
у каждой ровно половина датасета. Сводит их пара скриптов:

| Кто | Что делает |
|---|---|
| `make backup` | снимает свой слепок и кладёт в облако как `manta-dataset-<метка>-<UTC>.tar` |
| `make peer-sync` | забирает из облака САМЫЙ СВЕЖИЙ слепок каждого соседа и вливает |

Порядок в сутках: сначала бэкап, потом обмен. В cron на обеих машинах:

```
30 3 * * * cd ~/manta && ./scripts/backup.sh
0  4 * * * cd ~/manta && ./scripts/peer-sync.sh
0  9 * * * cd ~/manta && ./scripts/heartbeat.sh
```

Первый прогон стоит посмотреть глазами:

```bash
make peer-sync ARGS=--dry-run
```

Что важно знать про обмен:

* **Свой слепок не вливается.** Машина узнаёт себя по `MANTA_HOST_LABEL`;
  если метки на обеих машинах совпадут, каждая будет считать чужой файл
  своим и обмен встанет МОЛЧА — скрипт скажет «в облаке только мои» и
  выйдет с нулём.
* **Повторный прогон ничего не качает заново**: влитые имена лежат в
  `~/manta-backups/peers/.imported`, а сам архив после успешного импорта
  удаляется — всё его содержимое уже в базе.
* **Чужие слепки лежат в отдельном подкаталоге** `peers/`, а не рядом со
  своими. Иначе `heartbeat.sh` считал бы свежий чужой слепок своим и
  перестал бы замечать, что собственный бэкап сломался.
* **Обмен ничего не удаляет в облаке.** Ротацию ведёт `backup.sh`, и
  только по своим файлам — с меткой в маске.

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

Скрипт заводит четыре задачи планировщика: `Manta-Anchor` (якорь WSL, см.
ниже), `Manta-Recover` (при входе), `Manta-Backup` (ежедневный слепок с
ротацией) и `Manta-Report` (ежедневная диагностика).

Все задачи запускаются со скрытым окном: на экране от них ничего не
появляется.

---

## 11a. Окно WSL можно закрыть на крестик

**Раньше было нельзя.** Все процессы стека запускаются через `nohup`,
поэтому сигнал о закрытии терминала им не страшен — но убивал их не
сигнал, а сам WSL. Когда закрывается ПОСЛЕДНИЙ сеанс дистрибутива, WSL
гасит виртуальную машину целиком, вместе со всем, что внутри неё
работало. Поэтому «закрыл окно» и «остановил сбор» были одним и тем же
действием, и окно приходилось держать открытым.

Задача `Manta-Anchor` держит один невидимый сеанс, который никто не
закрывает. Пока он жив, WSL дистрибутив не гасит, и окно терминала можно
закрывать когда угодно. Якорь не делает ничего, кроме этого: у него нет
доступа ни к базам, ни к сети.

Проверить, что стек переживёт закрытие окна:

```bash
make wsl-anchor-status
```

```
якорь WSL: работает (pid 812)
окно терминала можно закрывать на крестик
```

Если ответ «НЕ работает» — поднять вручную (без прав администратора и без
перезагрузки):

```bash
make wsl-anchor
```

Задача Планировщика повторяется каждые 10 минут, поэтому после
`wsl --shutdown`, перезапуска Docker Desktop или выхода из спящего режима
якорь возвращается сам — ждать входа в систему не нужно.

Снять якорь (WSL снова начнёт гаснуть с последним закрытым окном):

```bash
make wsl-anchor-stop
```

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
