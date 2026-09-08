#!/usr/bin/env bash
# Запустить модуль приложения ТАМ, ГДЕ У НЕГО ЕСТЬ ЗАВИСИМОСТИ.
#
#     ./scripts/in-image.sh ml-read  -m training.status
#     ./scripts/in-image.sh ml-write -m training.train_winprob --min-matches 50
#     ./scripts/in-image.sh collect  tools/tier_audit.py --days 3
#
# ЧТО СЛУЧИЛОСЬ. `make ml-status` на VPS упал так:
#
#     File "src/registry/store.py", line 59, in __init__
#         from minio import Minio
#     ModuleNotFoundError: No module named 'minio'
#
# Цель гоняла ХОСТОВЫЙ python3 против кода приложения, чьи зависимости
# стоят только в образе. На машине владельца (Windows/WSL) окружение с
# ними существовало, поэтому за сотню прогонов это ни разу не всплыло.
# Вскрылось там, где дороже всего: на единственной оставшейся машине, в
# момент, когда инструментом надо было ВОСПОЛЬЗОВАТЬСЯ.
#
# Это ровно та беда, что и в спринте 143, когда `pg-migrate.sh` звал
# хостовый psql, а соседний `ch-migrate.sh` с самого начала ходил через
# `docker exec` — правильный образец лежал рядом. Здесь он тоже лежал
# рядом: сами эти модули КРУТЯТСЯ в контейнерах круглосуточно.
#
# ПОЧЕМУ EXEC, А НЕ `compose run`. Запущенный контейнер уже держит
# разрешённое окружение — адреса сервисов внутри сети и пароли, которые
# compose подставил из env-файла при подъёме. `compose run` перечитывал
# бы `${MANTA_S3_PASS_MODEL_WRITER:?…}` и падал бы у того, кто не
# подставил env-файл в текущую оболочку, — то есть у всех, кто пришёл
# чинить, а не разворачивать.
#
# ПОЧЕМУ РАЗНЫЕ КОНТЕЙНЕРЫ ДЛЯ ЧТЕНИЯ И ЗАПИСИ. У ml-service ключ
# реестра только на чтение (manta_model_reader) и лимит памяти 768 МиБ;
# у ml-autotrain — ключ на запись и лимита нет. Обучение, запущенное
# через exec в ml-service, считалось бы В ЕГО cgroup: OOM убил бы не
# обучение, а сам сервис инференса, и вместе с ним генерацию отчётов.
# Лёгкое чтение идёт в reader, всё, что обучает, — в writer.
#
# ПОДМЕНА ХОСТОМ НЕ ПРЕДУСМОТРЕНА НАМЕРЕННО. Откат на `python3` при
# отсутствующем контейнере вернул бы ровно тот отказ, ради которого
# скрипт написан, только уже молча и в неожиданном месте.
set -uo pipefail
cd "$(dirname "$0")/.."

usage() {
    echo "usage: $0 <роль> <аргументы python>" >&2
    echo "  роли: ml-read | ml-write | collect | features" >&2
    exit 2
}

[ $# -ge 2 ] || usage
role="$1"; shift

case "$role" in
ml-read)   container="${ML_READ_CONTAINER:-manta-ml-service-1}";     up="make vps-up";;
ml-write)  container="${ML_WRITE_CONTAINER:-manta-ml-autotrain-1}";  up="make vps-up";;
collect)   container="${COLLECT_CONTAINER:-manta-timeline-collector-1}"; up="make vps-up";;
features)  container="${FEATURES_CONTAINER:-manta-feature-extractor-1}"; up="make vps-up";;
*)         usage;;
esac

# Именно «бежит», а не «существует»: остановленный контейнер даёт
# `docker exec` с невнятной ошибкой, а причина у него совсем другая.
state=$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)
if [ "$state" != "true" ]; then
    echo "контейнер $container не запущен — выполнять команду негде." >&2
    echo "Поднять стек: $up" >&2
    echo "Другой контейнер: ML_READ_CONTAINER/ML_WRITE_CONTAINER/" >&2
    echo "COLLECT_CONTAINER/FEATURES_CONTAINER" >&2
    exit 1
fi

# ENTRYPOINT образов — python, но exec его не наследует: имя зовём явно.
# PYTHONPATH внутри образа уже выставлен (ENV в Dockerfile).
exec docker exec "$container" python "$@"
