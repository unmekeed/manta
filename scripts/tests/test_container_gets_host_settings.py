"""Настройки машины доезжают до контейнеров (спринт 143).

ЧТО СЛУЧИЛОСЬ. На VPS сервисы живут в Docker, а ~/manta-train.env читают
только скрипты на хосте. Шард и ключи API туда вписаны — и не видны
никому: контейнерный коллектор стартовал с шардом по умолчанию (0 из 1,
то есть «беру всё») и собирал ТЕ ЖЕ матчи, что домашние машины.

Отказа при этом нет. Нет и ошибки в логах. Есть удвоенная работа и
сожжённая квота OpenDota, заметные только по счётчикам витрины — и то
если сравнивать машины между собой.

Дома проблема не проявляется в принципе: там сервисы поднимает
dev-recover.sh процессами, и файл они читают сами. То есть неверная
конфигурация возможна ровно на той машине, где её труднее всего заметить.
"""
import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deployments" / "docker-compose.yml"
BOOTSTRAP = ROOT / "scripts" / "vps-bootstrap.sh"
MAKEFILE = ROOT / "Makefile"

# Переменные, которые задают ПОВЕДЕНИЕ МАШИНЫ, а не стенда. Их разъезд
# между хостом и контейнером не ломает ничего видимого.
MACHINE_VARS = ("COLLECTOR_SHARD_ID", "COLLECTOR_SHARD_COUNT",
                "OPENDOTA_API_KEY", "STRATZ_API_TOKEN",
                # Спринт 191: без токена сторожа молчат, а молчание
                # неотличимо от благополучия — то же свойство, что у
                # шарда, ради которого этот файл и заведён.
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def merged(env: dict) -> dict:
    if not shutil.which("docker"):
        pytest.skip("docker недоступен")
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--profile", "apps",
         "config", "--format", "json"],
        capture_output=True, text=True, env={**os.environ, **env})
    if proc.returncode != 0:
        pytest.fail(proc.stderr[:400])
    return json.loads(proc.stdout)


def test_collector_receives_the_shard_from_the_environment():
    """Шард доезжает до контейнера коллектора.

    Это главный тест файла: именно из-за шарда машины и не дублируют
    работу друг друга.
    """
    cfg = merged({"COLLECTOR_SHARD_ID": "2", "COLLECTOR_SHARD_COUNT": "3"})
    env = cfg["services"]["data-collector"]["environment"]
    assert env.get("COLLECTOR_SHARD_ID") == "2"
    assert env.get("COLLECTOR_SHARD_COUNT") == "3"


def test_collector_receives_api_keys():
    cfg = merged({"OPENDOTA_API_KEY": "ключ-од", "STRATZ_API_TOKEN": "ключ-стратц"})
    env = cfg["services"]["data-collector"]["environment"]
    assert env.get("OPENDOTA_API_KEY") == "ключ-од"
    assert env.get("STRATZ_API_TOKEN") == "ключ-стратц"


def test_defaults_keep_a_single_machine_working():
    """Без переменных — прежнее поведение: один шард, ключей нет.

    Домашняя машина живёт без deployments/.env, и правка ради VPS не
    должна была ничего ей менять.
    """
    cfg = merged({k: "" for k in MACHINE_VARS})
    env = cfg["services"]["data-collector"]["environment"]
    assert env.get("COLLECTOR_SHARD_COUNT") in ("1", "")


def test_bootstrap_loads_the_settings_before_starting_the_stack():
    """bootstrap подхватывает файл ДО подъёма стека.

    compose подставляет переменные из своего окружения. Не загрузи их
    заранее — контейнеры получат умолчания, и всё будет выглядеть
    исправным.
    """
    src = BOOTSTRAP.read_text(encoding="utf-8")
    m = re.search(r"^start_stack\(\) \{.*?^\}", src, re.M | re.S)
    assert m, "start_stack не найдена"
    body = m.group(0)
    assert "load_host_settings" in body, "настройки машины не загружаются"
    assert body.index("load_host_settings") < body.index("up -d"), \
        "настройки загружаются после подъёма стека"


def test_make_vps_up_loads_the_settings_too():
    """И КОРОТКИЙ путь подъёма — тоже (спринт 191).

    Тест выше проверял ровно одну дорогу к `up -d` — ту, что через
    bootstrap. Рядом всё это время жила вторая, `make vps-up`, которой
    пользуются каждый день, потому что она короче. Она файл настроек не
    загружала, и `${TELEGRAM_BOT_TOKEN:-}` подставлялся пустым: сторожа
    в контейнерах были выключены, а выглядело это как тишина в канале.

    Проверять надо КАЖДЫЙ путь к `up -d`, а не тот, который вспомнили
    при написании проверки: пользоваться будут самым коротким, а
    проверен окажется самый заметный.
    """
    src = MAKEFILE.read_text(encoding="utf-8")
    m = re.search(r"^vps-up:.*?(?=^\S|\Z)", src, re.M | re.S)
    assert m, "цель vps-up не найдена"
    body = m.group(0)
    assert "MANTA_TRAIN_ENV" in body, (
        "make vps-up поднимает стек, не загрузив настройки машины: "
        "контейнеры получат умолчания, и это не будет видно нигде")
    assert body.index("MANTA_TRAIN_ENV") < body.index("up -d"), \
        "настройки загружаются после подъёма стека"


@pytest.mark.parametrize("var", MACHINE_VARS)
def test_every_machine_variable_is_passed_through(var):
    """Каждая переменная машины пробрасывается в compose.

    Список записан здесь руками СОЗНАТЕЛЬНО: вывести его неоткуда —
    какие переменные задают поведение машины, а какие стенда, знает
    только человек. Зато забытая переменная ловится сразу.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert f"${{{var}" in text, f"{var} не пробрасывается в контейнеры"


# -- умолчания не расходятся с кодом ---------------------------------------------

# Весь пакет коллектора, а не одна точка входа (спринт 154).
#
# ПОЧЕМУ ЭТО ВАЖНО. Прежняя версия читала только __main__.py, и на этом
# сторож, заведённый ради «настройки не доезжают до контейнера», пропустил
# ровно тот случай, ради которого писался: STEAM_API_KEY читается в
# ranks.py, в compose его не было, и своя разбивка молча стояла пустой на
# VPS. Ключ в ~/manta-train.env был вписан — и не значил ничего.
#
# Второй промах той же версии — разбор регулярками. Вызов, перенесённый
# на другую строку:
#
#     sleep_s = max(sleep_s, int(os.getenv(
#         "STRATZ_RATE_SLEEP_S", "3600")))
#
# под шаблон не подходил, и переменная не находилась даже в том файле,
# который сторож читал. Теперь разбор через ast: форматирование до дерева
# не доходит.
COLLECTOR_SRC = ROOT / "apps/data-collector/src/collector"

# Читаются коллектором, но задаются не окружением машины.
INFRA_VARS = {
    # Адреса стенда: приходят из app-env литералами.
    "KAFKA_BROKERS", "POSTGRES_DSN", "COLLECTOR_SOURCE",
    "COLLECTOR_INTERVAL_SECONDS",
    "S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET",
    "S3_USE_SSL", "RAW_MATCH_STORE", "RAW_MATCH_BUCKET",
    "CLICKHOUSE_URL", "CLICKHOUSE_DB", "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    # METRICS_PORT у каждого сервиса СВОЙ — общее значение занял бы
    # первый стартовавший (инцидент 2026-07-27).
    "METRICS_PORT",
}

# Переменные, которые НЕЛЬЗЯ объявить в compose, и почему. Список именно
# с причинами: без них он через полгода превратится в свалку исключений,
# куда дописывают всё, что мешает тесту пройти.
CANNOT_DECLARE = {
    "OPENDOTA_BUDGET":
        "умолчание вычисляется как доля общего лимита на источник; "
        "объявить его в compose значило бы продублировать расчёт, а "
        "`${VAR:-}` дал бы пустую строку и int() упал бы на старте",
    "RANKS_BATCH_SIZE":
        "умолчание вычисляется от размера пачки резолвера",
    "RANKS_RESOLVER":
        "читается без умолчания: None и пустая строка означают разное",
}


def collector_env_vars() -> tuple[dict, set]:
    """Что коллектор читает: {имя: литеральное умолчание} и остальные.

    Разбор через ast — форматирование вызова роли не играет. Регулярка в
    прежней версии не видела вызовов, перенесённых на другую строку.
    """
    with_def: dict[str, str] = {}
    no_def: set[str] = set()
    for py in sorted(COLLECTOR_SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "getenv"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                continue
            name = node.args[0].value
            if name in INFRA_VARS:
                continue
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                with_def[name] = str(node.args[1].value)
            else:
                # Цепочка (os.getenv("A", os.getenv("B", "80"))) или
                # вычисляемое умолчание: литерала нет, но переменная есть.
                no_def.add(name)
    return with_def, no_def - set(with_def)


def numeric_vars() -> dict[str, str]:
    """{имя: "int"|"float"} — переменные, уходящие в число.

    Разбор по всему пакету и через ast: регулярка прежней версии искала
    только `int(os.getenv("X"` в одном файле и не видела ни float, ни
    переноса строки.

    Тип запоминается, а не сводится к «числу»: проверять float-переменную
    через int() значит объявить поломкой верное значение 1.1 — первая
    версия этой правки так и сделала.
    """
    out: dict[str, str] = {}
    for py in sorted(COLLECTOR_SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("int", "float")
                    and node.args):
                continue
            inner = node.args[0]
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "getenv"
                    and inner.args
                    and isinstance(inner.args[0], ast.Constant)):
                out[inner.args[0].value] = node.func.id
    return out


def collector_environment() -> dict:
    """Окружение, которое получает контейнер коллектора.

    Якоря YAML разрешает сам, поэтому здесь ровно то, что увидит docker, —
    и слитые app-env с collector-env, и литералы стенда. Поиск по тексту
    файла, как в прежней версии, не отличал объявленную переменную от
    упомянутой в комментарии.
    """
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))[
        "services"]["data-collector"]["environment"]


def test_the_parser_finds_variables():
    """Страховка от проверки пустого множества."""
    with_def, no_def = collector_env_vars()
    assert len(with_def) >= 15, f"разбор сломан: {with_def}"
    assert "COLLECTOR_SHARD_COUNT" in with_def


def test_the_parser_reads_the_whole_package():
    """Разбор идёт по ВСЕМУ пакету, а не по точке входа.

    Это не придирка к реализации, а единственный способ поймать сужение
    обратно. Мутация «читать только __main__.py» пережила первый заход:
    после того как все переменные добавлены в compose, узкий разбор
    находит меньше — и все найденные на месте. Сторож проходит, ничего
    не проверив, ровно как проходил до спринта 154.

    STEAM_API_KEY читается в ranks.py и больше нигде. Его отсутствие в
    разборе означает, что сторож снова слеп к тому, из-за чего своя
    разбивка простояла на VPS с пустой очередью.
    """
    with_def, no_def = collector_env_vars()
    names = set(with_def) | no_def
    assert "STEAM_API_KEY" in names, (
        "разбор не видит ranks.py — сторож сузился обратно до __main__.py")
    assert "PATCH_LAG" in names, "разбор не видит sources/"
    assert "REPLAY_RETENTION_DAYS" in names, "разбор не видит runner.py"


def test_every_variable_the_collector_reads_is_passed_through():
    """Список выводится из КОДА, а не пишется в compose руками.

    Записанный руками, он отстал бы при добавлении новой переменной, и
    та молча превратилась бы в умолчание — ровно так на VPS оказался
    шард 0 из 1 при вписанном в файл шарде 2 из 3.
    """
    with_def, no_def = collector_env_vars()
    env = collector_environment()
    missing = [v for v in sorted(set(with_def) | no_def)
               if v not in env and v not in CANNOT_DECLARE]
    assert not missing, (
        "коллектор читает, а контейнер не получает: " + ", ".join(missing)
        + "\n(если объявить нельзя — впишите в CANNOT_DECLARE С ПРИЧИНОЙ)")


def test_compose_defaults_match_the_collector_defaults():
    """Умолчание в compose совпадает с умолчанием в коде.

    Два умолчания одной переменной — это два разных ответа на вопрос
    «сколько по умолчанию». Разойдясь, они дают не отказ, а другое
    поведение на VPS и дома при одинаковых настройках.
    """
    with_def, _ = collector_env_vars()
    env = collector_environment()
    bad = []
    for var, default in sorted(with_def.items()):
        if var in CANNOT_DECLARE:
            continue
        raw = env.get(var)
        if raw is None:
            bad.append(f"{var}: нет в окружении контейнера")
            continue
        m = re.fullmatch(rf"\$\{{{var}:-(.*)\}}", str(raw))
        if not m:
            # Литерал стенда, а не настройка машины — сверять не с чем.
            continue
        # У цепочки умолчаний сверяем ПОСЛЕДНЕЕ звено: именно оно
        # применится, если не задана ни одна переменная цепочки.
        got = m.group(1).split(":-")[-1].rstrip("}")
        if got != default:
            bad.append(f"{var}: в коде {default!r}, в compose {got!r}")
    assert not bad, "\n".join(bad)


def test_numeric_variables_never_arrive_empty():
    """Числовые переменные не приезжают пустой строкой.

    `${VAR:-}` задаёт переменную ПУСТОЙ, а это не «не задана»:
    int("") падает с ValueError, и коллектор не стартует вовсе. Ошибка
    выглядела бы как «контейнер перезапускается», без единой подсказки о
    причине.
    """
    cfg = merged({})
    env = cfg["services"]["data-collector"]["environment"]
    numeric = {k: v for k, v in numeric_vars().items()
               if k not in INFRA_VARS and k not in CANNOT_DECLARE}
    assert numeric, "числовых переменных не найдено — разбор сломан"
    assert "float" in numeric.values(), "разбор не видит float — половина слепа"
    for var, kind in sorted(numeric.items()):
        value = env.get(var)
        assert value not in ("", None), (
            f"{var} приезжает пустым — {kind}() упадёт")
        (int if kind == "int" else float)(value)


def test_metrics_port_is_not_shared():
    """METRICS_PORT не попадает в общий блок.

    Он у каждого сервиса свой; общее значение занял бы первый
    стартовавший, остальные упали бы с «Address already in use».
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${METRICS_PORT" not in text


def test_no_hardcoded_value_overrides_the_environment():
    """Ни одна переменная не перебивается литералом в сервисе.

    Проверка ЭФФЕКТА, а не текста. Якорь может быть безупречен, а строка
    `OPENDOTA_LIMIT: "3"` ниже по сервису молча победит его при слиянии —
    и вписанное в файл машины значение не доедет, не оставив следа.

    Так и было до спринта 143: зашитая тройка перебивала OPENDOTA_LIMIT=2
    из окружения. Текстовые проверки этого не видят, потому что якорь при
    этом на месте и умолчание в нём верное.
    """
    with_def, no_def = collector_env_vars()
    names = sorted((set(with_def) | no_def) - set(CANNOT_DECLARE))
    # Значение заведомо непохожее на любое умолчание.
    probe = {v: f"проба-{i}" for i, v in enumerate(names)}
    env = merged(probe)["services"]["data-collector"]["environment"]
    bad = [f"{v}: ждали {probe[v]!r}, получили {env.get(v)!r}"
           for v in names if env.get(v) != probe[v]]
    assert not bad, "значение из окружения не доехало:\n" + "\n".join(bad)
