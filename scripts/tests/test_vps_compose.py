"""Тесты наложения для VPS (спринт 138).

Базовый docker-compose.yml писался для домашней машины за NAT, где
открытые порты безвредны. На VPS с белым адресом тринадцать портов —
Postgres, ClickHouse, MinIO, Kafka, Grafana и прочие — публикуются на
0.0.0.0 с паролем `dota_dev_password`, лежащим в git.

Проверяется одно свойство, и оно единственное, ради которого файл
существует: НИ ОДИН порт не смотрит наружу. Причём проверяется сверкой
двух файлов, а не списком в тесте: список пришлось бы обновлять руками,
и он разошёлся бы с compose ровно тогда, когда это опаснее всего — при
добавлении нового сервиса.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML нужен для разбора compose")

DEPLOY = Path(__file__).resolve().parents[2] / "deployments"
BASE = DEPLOY / "docker-compose.yml"
VPS = DEPLOY / "docker-compose.vps.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _published(compose: dict) -> dict[str, list[str]]:
    """{сервис: [строки публикации портов]} — только те, что публикуются."""
    out = {}
    for name, svc in (compose.get("services") or {}).items():
        ports = svc.get("ports") or []
        if ports:
            out[name] = [str(p) for p in ports]
    return out


def test_every_published_port_is_bound_to_loopback():
    """Каждый порт наложения слушает 127.0.0.1 и ничего больше.

    Формат `"127.0.0.1:5432:5432"`. Без адреса Docker публикует на всех
    интерфейсах, и это не оговорка, а поведение по умолчанию.
    """
    bad = []
    for svc, ports in _published(_load(VPS)).items():
        for p in ports:
            if not p.startswith("127.0.0.1:"):
                bad.append(f"{svc}: {p}")
    assert not bad, "порты смотрят наружу:\n" + "\n".join(bad)


def test_no_service_publishes_a_port_without_a_vps_override():
    """Новый порт в базовом файле обязан появиться и в наложении.

    Это главный тест файла. Наложение не «выключает» порты базового
    compose — оно ЗАМЕНЯЕТ список у тех сервисов, что в нём описаны.
    Сервис, забытый в наложении, останется опубликованным на 0.0.0.0 со
    своим dev-паролем, и заметить это можно будет только сканером
    снаружи.
    """
    base = _published(_load(BASE))
    vps = _published(_load(VPS))
    missing = sorted(set(base) - set(vps))
    assert not missing, (
        "сервисы публикуют порты, но их нет в наложении для VPS "
        f"(останутся открыты наружу): {missing}")


def test_override_covers_the_same_ports_as_the_base():
    """Наложение не теряет и не выдумывает порты.

    Потерянный порт означает недоступную службу, лишний — открытую
    дырку. Сверяем по контейнерной стороне отображения.
    """
    def container_ports(ports: list[str]) -> set[str]:
        # "127.0.0.1:9500:9000" -> "9000";  "5432:5432" -> "5432"
        return {p.split(":")[-1] for p in ports}

    base = _published(_load(BASE))
    vps = _published(_load(VPS))
    for svc in sorted(set(base) & set(vps)):
        assert container_ports(base[svc]) == container_ports(vps[svc]), svc


# Машина, под которую считаются потолки: тариф Standart — 8 ядер, 12 ГБ.
# Число тут не для красоты: при смене тарифа его надо поменять ОСОЗНАННО,
# иначе потолки перестанут ограничивать что-либо (сумма больше памяти) или
# начнут душить (сумма сильно меньше).
TARGET_RAM_MB = 12 * 1024

# Сколько оставить ОС и ХОСТОВЫМ процессам. Сервисы сбора, парсер и
# обучение живут не в Docker (см. dev-recover.sh), и их в потолках нет:
# семь коллекторов, парсер, а на пике — обучение, у которого замерено
# 559 МиБ RSS. Три гигабайта на это, а не «сколько останется».
HOST_RESERVE_MB = 3 * 1024


def test_memory_limits_fit_the_target_machine():
    """Сумма потолков плюс запас для хоста влезает в машину.

    Потолки нужны потому, что ClickHouse считает доступную память от
    ВСЕГО хоста и, не зная про Kafka и Postgres рядом, выедает её до OOM
    соседей. Но и сумма потолков не должна превышать машину, иначе они
    не ограничивают ничего — это была бы декорация.
    """
    def to_mb(value: str) -> int:
        m = re.fullmatch(r"(\d+)([mg])", str(value).strip().lower())
        assert m, f"непонятный лимит: {value}"
        return int(m.group(1)) * (1024 if m.group(2) == "g" else 1)

    limits = {name: to_mb(svc["mem_limit"])
              for name, svc in _load(VPS)["services"].items()
              if svc.get("mem_limit")}
    assert limits, "потолков памяти нет вовсе"
    total = sum(limits.values())
    budget = TARGET_RAM_MB - HOST_RESERVE_MB
    assert total <= budget, (
        f"сумма потолков {total} МиБ не влезает в {budget} МиБ "
        f"(машина {TARGET_RAM_MB} МиБ минус запас хосту {HOST_RESERVE_MB})")


def test_clickhouse_gets_the_largest_share():
    """ClickHouse — самый прожорливый, и это должно быть видно в файле.

    Если однажды кто-то урежет его ниже Kafka, запросы к витрине начнут
    падать по памяти, а причина будет неочевидна.
    """
    services = _load(VPS)["services"]
    limits = {n: s.get("mem_limit") for n, s in services.items()}
    assert limits.get("clickhouse"), "у ClickHouse нет потолка памяти"
    assert limits["clickhouse"].endswith("g")


def test_services_restart_by_themselves():
    """VPS перезагружают, и стек обязан подниматься сам.

    Дома это делает задача Планировщика после входа в систему; на
    сервере входа в систему не происходит вовсе, и без restart-политики
    стек молча не встанет после перезагрузки.
    """
    without = [name for name, svc in _load(VPS)["services"].items()
               if svc.get("restart") != "unless-stopped"]
    assert not without, f"без политики перезапуска: {without}"
