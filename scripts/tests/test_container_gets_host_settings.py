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
