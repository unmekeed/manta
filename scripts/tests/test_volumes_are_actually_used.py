"""Смонтированный том обязан быть тем, куда сервис пишет (спринт 193).

ЖИВОЙ ОТКАЗ 3 сентября 2026. Переход на journald пересоздал контейнеры,
Kafka поднялась с пустым каталогом данных, и ВСЕ СЕМЬ прикладных топиков
исчезли. Автосоздание топиков выключено намеренно, поэтому продюсеры
трое суток писали в никуда — без ошибки, без строчки в логе. Реплейный
путь и генерация отчётов стояли, и заметили это по свежести данных, а не
по отказу.

Причина оказалась старше отказа: в compose том был смонтирован в
`/var/lib/kafka/data`, а образ `apache/kafka` пишет в `/tmp/kafka-logs`,
потому что `KAFKA_LOG_DIRS` никто не задал. На живой машине
`meta.properties` лежал в /tmp, а том стоял пустым С ДАТЫ СБОРКИ ОБРАЗА.
То есть сохранности не было никогда — просто до сентября контейнер не
пересоздавали, и это выглядело как работающая система.

ЧЕМУ ЭТО УЧИТ. Смонтированный том — это НАМЕРЕНИЕ, а не факт. Docker с
радостью примонтирует его куда угодно и ничего не скажет, если туда
никто не пишет. Проверка «том объявлен» отвечает на вопрос, которого
никто не задавал.

ЧЕГО ЭТА ПРОВЕРКА НЕ УМЕЕТ. Она статическая и знает только про те
сервисы, чей каталог данных задаётся переменной окружения. Общего способа
узнать, куда пишет произвольный образ, из compose нет — для остальных
работает проверка «том не пуст» в doctor.sh, но она требует живой машины.
Две проверки дополняют друг друга: эта ловит ошибку до деплоя, та — на
любом образе.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML нужен для разбора compose")

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deployments" / "docker-compose.yml"

# Сервис → (переменная с каталогом данных, имя тома).
#
# Список рукописный, и другим он быть не может: связь «эта переменная
# задаёт каталог данных» знает только человек, читавший документацию
# образа. Зато каждая строка здесь — проверяемое утверждение, а не
# надежда.
DATA_DIR_ENV = {
    "kafka": ("KAFKA_LOG_DIRS", "kafka_data"),
}


def compose() -> dict:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor(
        "!override",
        lambda ldr, node: ldr.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode) else ldr.construct_object(node))
    return yaml.load(COMPOSE.read_text(encoding="utf-8"), Loader=Loader)


def mount_target(service: dict, volume: str) -> str | None:
    """Куда в контейнере смонтирован именованный том."""
    for entry in service.get("volumes") or []:
        if isinstance(entry, str) and entry.split(":")[0] == volume:
            return entry.split(":")[1]
    return None


@pytest.mark.parametrize("name", sorted(DATA_DIR_ENV))
def test_service_writes_into_its_volume(name):
    """Каталог данных сервиса совпадает с точкой монтирования тома."""
    env_key, volume = DATA_DIR_ENV[name]
    svc = compose()["services"][name]
    target = mount_target(svc, volume)
    assert target, f"{name}: том {volume} не смонтирован"

    env = svc.get("environment") or {}
    configured = env.get(env_key)
    assert configured, (
        f"{name}: {env_key} не задан — образ будет писать в свой каталог по "
        f"умолчанию, а том {volume} останется пустым. Ровно так 3 сентября "
        f"пропали все топики Kafka.")
    assert configured == target, (
        f"{name}: пишет в {configured}, а том смонтирован в {target} — "
        f"данные не переживут пересоздания контейнера")


def test_every_service_with_a_named_volume_is_accounted_for():
    """Сервис с именованным томом либо проверяется, либо назван осознанно.

    Та же форма, что у списка таблиц в спринте 156: незнакомый случай
    обязан ронять тест, иначе «мы про это не подумали» неотличимо от «мы
    сознательно не проверяем».

    Здесь в исключениях — образы, которые пишут в свой каталог по
    умолчанию, и он же смонтирован. Проверить это статически нельзя: путь
    зашит внутри образа. Их страхует проверка «том не пуст» в doctor.sh.
    """
    known_default_dir = {
        "postgres",    # /var/lib/postgresql/data — умолчание образа
        "clickhouse",  # /var/lib/clickhouse
        "minio",       # каталог задан в command: server /data
        "mlflow",      # /mlflow
        "prometheus",  # /prometheus
        "grafana",     # /var/lib/grafana
    }
    cfg = compose()
    declared = set(cfg.get("volumes") or {})
    unchecked = []
    for name, svc in cfg["services"].items():
        used = {e.split(":")[0] for e in (svc.get("volumes") or [])
                if isinstance(e, str) and e.split(":")[0] in declared}
        if used and name not in DATA_DIR_ENV and name not in known_default_dir:
            unchecked.append(name)
    assert not unchecked, (
        "сервисы с именованным томом, про которые никто не решил, куда они "
        f"пишут: {unchecked}")
