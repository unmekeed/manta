"""Инструменты бегут там, где стоят их зависимости (спринт 208).

ЧТО СЛУЧИЛОСЬ. На VPS:

    $ make ml-status
    File "src/registry/store.py", line 59, in __init__
        from minio import Minio
    ModuleNotFoundError: No module named 'minio'

Цель гоняла ХОСТОВЫЙ python3 против кода приложения, чьи зависимости
стоят только в образе. На машине владельца (Windows/WSL) окружение с
ними существовало, поэтому за сотню прогонов это ни разу не всплыло.
Вскрылось там, где дороже всего: на единственной оставшейся машине, в
момент, когда инструментом надо было ВОСПОЛЬЗОВАТЬСЯ.

ТА ЖЕ БЕДА, ЧТО В СПРИНТЕ 143. Тогда `pg-migrate.sh` звал хостовый psql,
а соседний `ch-migrate.sh` с самого начала ходил через `docker exec`.
Здесь правильный образец тоже лежал рядом: сами эти модули крутятся в
контейнерах круглосуточно.

ПОЧЕМУ ОТДЕЛЬНЫМ ПРАВИЛОМ. Зависимость от хоста не видна ни в одном
прогоне на машине, где зависимости есть. Проверить её выполнением
нельзя — тут и сейчас докера с поднятым стеком нет, — но можно проверить
ФОРМУ вызова: инструмент, ходящий в боевые хранилища, обязан звать код
приложения внутри образа.

ГРАНИЦА ПРАВИЛА. Не всякая цель с хостовым python3 сломана. Различаются
три рода:

  * ИНСТРУМЕНТ — сходить в боевую базу, реестр, витрину (`ml-status`,
    `tier-audit`, `backfill`). Обязан бежать в образе;
  * ЗАПУСК СЕРВИСА РУКАМИ вместо докера (`ml-serve`, `report-gen`) —
    в том и смысл, чтобы мимо докера; в контейнере он и так крутится;
  * ТЕСТЫ И ПЕРЕСБОРКА ЭТАЛОНОВ (`golden-test`) — работа на машине
    разработчика, боевых хранилищ не касается.

Список исключений ведётся ПОИМЁННО и с причиной. Список «всё, что не
подошло» превратился бы в свалку, куда попадает и настоящий разъезд.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"

# Цели, которым хостовый python3 положен, и ПОЧЕМУ.
#
# «Запустить сервис руками» — тот же процесс, что крутится в контейнере;
# смысл цели именно в обходе докера при отладке. Пустить её через exec
# значило бы запускать сервис внутри контейнера, где он уже запущен.
ALLOWED = {
    "ml-serve":     "запуск сервиса руками вместо докера",
    "report-gen":   "запуск сервиса руками вместо докера",
    "sim-serve":    "запуск сервиса руками вместо докера",
    "draft-serve":  "запуск сервиса руками вместо докера",
    "coach-serve":  "запуск сервиса руками вместо докера",
    "fs-serve":     "запуск сервиса руками вместо докера",
    "pytest":       "тесты: машина разработчика, боевых хранилищ нет",
    "golden-test":  "тесты: машина разработчика, боевых хранилищ нет",
    "signals-golden-update": "пересборка эталонов на машине разработчика",
    # Порождение кода, а не работа с данными: protoc пишет заглушки В
    # РЕПОЗИТОРИЙ, и делать это внутри контейнера бессмысленно — результат
    # остался бы там. Каталог apps/ здесь лишь в путях вывода.
    "proto-gen": "кодогенерация: пишет в репозиторий, хранилищ не касается",
    "wp-rates-sql-test": "тест против живого ClickHouse, запускается руками",
    "candidates-sql-test": "тест против живого Postgres, запускается руками",
    "dedup-sql-test": "тест против живого Postgres, запускается руками",
    "sql-test": "тест против живого Postgres, запускается руками",
}


def recipes() -> dict[str, str]:
    """цель → её рецепт (строки, начинающиеся с табуляции).

    Разбор идёт по самому Makefile, а не по списку рядом: список «для
    теста» проверял бы список, а вызывали бы рецепт.
    """
    out, target, buf = {}, None, []
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            if target:
                buf.append(line)
            continue
        if target:
            out[target] = "\n".join(buf)
        m = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
        target, buf = (m.group(1) if m else None), []
    if target:
        out[target] = "\n".join(buf)
    return out


def host_python_targets() -> dict[str, str]:
    """Цели, зовущие хостовый python3 против кода приложения.

    `python3 -m pytest` внутри такой цели тоже считается: важно не имя
    модуля, а то, чьим интерпретатором он запускается.
    """
    bad = {}
    for target, body in recipes().items():
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("#"))
        if re.search(r"\bpython3\b", code) and "apps/" in code:
            bad[target] = code
    return bad


def test_the_makefile_is_parsed():
    """Страховка от проверки пустоты.

    Сломайся разбор — правило ниже прошло бы на пустом словаре. Этот
    проект уже ловил такое на себе не раз.
    """
    r = recipes()
    assert len(r) > 40, f"разобрано целей: {len(r)}"
    assert "ml-status" in r and "doctor" in r, sorted(r)[:20]


def test_the_parser_sees_a_host_python_call():
    """Разбор действительно узнаёт хостовый вызов.

    Без этой проверки правило ниже было бы зелёным и при поиске, который
    ничего не находит, — то есть при полностью снятом стороже.
    """
    assert host_python_targets(), (
        "не найдено ни одной цели с хостовым python3 — разбор сломан "
        "(в Makefile они есть: сервисы запускаются руками)")


def test_every_tool_that_touches_live_storage_runs_inside_the_image():
    """ГЛАВНОЕ: инструмент зовёт код приложения внутри образа.

    Хостовый python3 против кода приложения падает на первом же импорте
    там, где зависимостей нет, — и узнаётся это в момент, когда
    инструментом надо воспользоваться.
    """
    offenders = {t: c for t, c in host_python_targets().items()
                 if t not in ALLOWED}
    assert not offenders, (
        "цели гоняют хостовый python3 против кода приложения:\n" +
        "\n".join(f"  {t}: {c.strip()[:90]}" for t, c in offenders.items()) +
        "\n\nЛечение: ./scripts/in-image.sh <роль> …  Если цель "
        "ЗАПУСКАЕТ СЕРВИС РУКАМИ или это тест — вписать её в ALLOWED "
        "С ПРИЧИНОЙ, а не расширять правило")


def test_the_exception_list_has_no_dead_entries():
    """Исключение, потерявшее предмет, снимается.

    Список исключений — единственная лазейка в правиле, и потому сам
    обязан быть под присмотром: строка, пережившая цель, к которой
    относилась, однажды прикроет настоящий разъезд.
    """
    known = set(recipes())
    stale = {t for t in ALLOWED if t not in known}
    assert not stale, f"исключения для несуществующих целей: {sorted(stale)}"


def test_the_tools_a_target_runs_are_inside_the_image():
    """Скрипт из tools/ обязан быть В ОБРАЗЕ, раз его зовут через exec.

    Перевести цель на `docker exec` и не положить сам файл внутрь —
    значит поменять один отказ на другой, столь же внезапный. Раскладка
    берётся из Dockerfile: перепишут его — проверка пойдёт следом, а не
    начнёт врать.
    """
    image_of = {"collect": ROOT / "apps/data-collector/Dockerfile",
                "features": ROOT / "apps/feature-extractor/Dockerfile",
                "ml-read": ROOT / "apps/ml-service/Dockerfile",
                "ml-write": ROOT / "apps/ml-service/Dockerfile"}
    missing = []
    for target, body in recipes().items():
        for role, path in re.findall(
                r"in-image\.sh\s+(\S+)\s+(tools/\S+)", body):
            dockerfile = image_of[role].read_text(encoding="utf-8")
            top = path.split("/")[0] + "/"
            if not re.search(rf"^COPY\s+\S*{re.escape(top)}\s", dockerfile,
                             re.M):
                missing.append(f"{target}: {path} не копируется в "
                               f"{image_of[role].relative_to(ROOT)}")
    assert not missing, "\n".join(missing)
