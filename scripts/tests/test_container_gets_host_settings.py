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
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deployments" / "docker-compose.yml"
BOOTSTRAP = ROOT / "scripts" / "vps-bootstrap.sh"

# Переменные, которые задают ПОВЕДЕНИЕ МАШИНЫ, а не стенда. Их разъезд
# между хостом и контейнером не ломает ничего видимого.
MACHINE_VARS = ("COLLECTOR_SHARD_ID", "COLLECTOR_SHARD_COUNT",
                "OPENDOTA_API_KEY", "STRATZ_API_TOKEN")


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

COLLECTOR_MAIN = ROOT / "apps/data-collector/src/collector/__main__.py"
# Читаются коллектором, но задаются не окружением машины: инфраструктура
# приходит из app-env, а METRICS_PORT у каждого сервиса СВОЙ — общее
# значение занял бы первый стартовавший (инцидент 2026-07-27).
INFRA_VARS = {"KAFKA_BROKERS", "POSTGRES_DSN", "METRICS_PORT",
              "COLLECTOR_SOURCE", "COLLECTOR_INTERVAL_SECONDS"}


def collector_env_vars() -> tuple[dict, set]:
    """Что коллектор читает: {имя: умолчание} и множество без умолчания.

    Форм три, и вторая чуть не проскочила мимо: умолчанием переменной
    может быть ДРУГАЯ переменная —

        int(os.getenv("STRATZ_MIN_RANK",
                      os.getenv("OPENDOTA_MIN_RANK", "80")))

    Разбор, знающий только про строковое умолчание, такую переменную не
    видит вовсе: она не попадает ни в один список, не пробрасывается в
    контейнер и молча теряется, если её задать в файле машины.
    """
    src = COLLECTOR_MAIN.read_text(encoding="utf-8")
    chained = {m[0]: m[2] for m in re.findall(
        r'os\.getenv\("([A-Z_]+)",\s*\n?\s*os\.getenv\("([A-Z_]+)",\s*"([^"]*)"\)\)',
        src)}
    plain = dict(re.findall(r'os\.getenv\("([A-Z_]+)",\s*"([^"]*)"\)', src))
    bare = set(re.findall(r'os\.getenv\("([A-Z_]+)"\)', src))
    with_def = {**plain, **chained}
    return ({k: v for k, v in with_def.items() if k not in INFRA_VARS},
            bare - INFRA_VARS - set(with_def))


def test_the_parser_finds_variables():
    """Страховка от проверки пустого множества."""
    with_def, no_def = collector_env_vars()
    assert len(with_def) >= 15, f"разбор сломан: {with_def}"
    assert "COLLECTOR_SHARD_COUNT" in with_def


def test_every_variable_the_collector_reads_is_passed_through():
    """Список выводится из КОДА, а не пишется в compose руками.

    Записанный руками, он отстал бы при добавлении новой переменной, и
    та молча превратилась бы в умолчание — ровно так на VPS оказался
    шард 0 из 1 при вписанном в файл шарде 2 из 3.
    """
    with_def, no_def = collector_env_vars()
    text = COMPOSE.read_text(encoding="utf-8")
    missing = [v for v in (set(with_def) | no_def) if f"${{{v}:-" not in text]
    assert not missing, f"коллектор читает, а compose не пробрасывает: {sorted(missing)}"


def test_compose_defaults_match_the_collector_defaults():
    """Умолчание в compose совпадает с умолчанием в коде.

    Два умолчания одной переменной — это два разных ответа на вопрос
    «сколько по умолчанию». Разойдясь, они дают не отказ, а другое
    поведение на VPS и дома при одинаковых настройках.
    """
    with_def, _ = collector_env_vars()
    text = COMPOSE.read_text(encoding="utf-8")
    bad = []
    for var, default in sorted(with_def.items()):
        m = re.search(rf"\$\{{{var}:-(.*?)\}}*\"", text)
        if not m:
            bad.append(f"{var}: нет в compose")
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
    src = COLLECTOR_MAIN.read_text(encoding="utf-8")
    numeric = set(re.findall(r'int\(os\.getenv\("([A-Z_]+)"', src)) - INFRA_VARS
    assert numeric, "числовых переменных не найдено — разбор сломан"
    for var in sorted(numeric):
        value = env.get(var)
        assert value not in ("", None), f"{var} приезжает пустым — int() упадёт"
        int(value)


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
    names = sorted(set(with_def) | no_def)
    # Значение заведомо непохожее на любое умолчание.
    probe = {v: f"проба-{i}" for i, v in enumerate(names)}
    env = merged(probe)["services"]["data-collector"]["environment"]
    bad = [f"{v}: ждали {probe[v]!r}, получили {env.get(v)!r}"
           for v in names if env.get(v) != probe[v]]
    assert not bad, "значение из окружения не доехало:\n" + "\n".join(bad)
