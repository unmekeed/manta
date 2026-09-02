"""Набор коллекторов на VPS совпадает с домашним (спринт 143).

ЗАЧЕМ. С этого спринта VPS — единственный сборщик, а дома сбор
остановлен. Значит всё, что раньше поднимал dev-recover.sh семью
процессами, обязано существовать в docker-compose.yml. Забытый источник
не падает и не жалуется: просто один из семи потоков данных перестаёт
приходить, и заметить это можно лишь по остановившемуся счётчику
конкретной витрины — недели спустя.

Список источников ВЫВОДИТСЯ из dev-recover.sh, а не пишется здесь
руками: записанный, он отстал бы ровно тогда, когда добавляют новый
источник, — и новый оказался бы единственным непроверенным.
"""
import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
RECOVER = ROOT / "scripts" / "dev-recover.sh"
COMPOSE = ROOT / "deployments" / "docker-compose.yml"
BOOTSTRAP = ROOT / "scripts" / "vps-bootstrap.sh"


def home_sources() -> set[str]:
    """Источники, которые поднимает домашний recover."""
    text = RECOVER.read_text(encoding="utf-8")
    return set(re.findall(r"-m collector --source ([a-z-]+)", text))


def compose_collectors() -> dict[str, dict]:
    """{сервис: окружение} для всех сервисов-коллекторов."""
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--profile", "apps",
         "--profile", "stratz", "--profile", "candidates", "config"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("docker compose недоступен")
    services = yaml.safe_load(proc.stdout)["services"]
    return {n: s.get("environment", {}) for n, s in services.items()
            if s.get("environment", {}).get("COLLECTOR_SOURCE")}


def test_home_launches_several_sources():
    """Страховка от сверки пустого множества."""
    assert len(home_sources()) >= 6, f"разбор dev-recover сломан: {home_sources()}"


def test_every_home_source_exists_in_compose():
    """ГЛАВНЫЙ тест: ни один источник не потерян при переезде на VPS.

    Потерянный источник — это не отказ, а тихо иссякший поток данных.
    """
    in_compose = {e["COLLECTOR_SOURCE"] for e in compose_collectors().values()}
    missing = sorted(home_sources() - in_compose)
    assert not missing, f"дома собираются, на VPS — нет: {missing}"


def test_metrics_ports_are_unique():
    """У каждого коллектора СВОЙ порт метрик.

    Общее значение занял бы первый стартовавший, остальные упали бы с
    «Address already in use» (инцидент 2026-07-27).
    """
    ports = [e.get("METRICS_PORT") for e in compose_collectors().values()]
    assert all(ports), f"у кого-то нет METRICS_PORT: {ports}"
    assert len(ports) == len(set(ports)), f"порты метрик повторяются: {ports}"


def test_pro_and_league_limits_are_not_the_public_one():
    """Про и лиги берут СВОИ лимиты, а не общий TIMELINE_LIMIT.

    Дома это делается подстановкой PRO_TIMELINE_LIMIT и LEAGUE_LIMIT в
    переменную TIMELINE_LIMIT. Подставь сюда обычный TIMELINE_LIMIT — и
    про-поток сожжёт квоту, которой хватало на всех.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert 'TIMELINE_LIMIT: "${PRO_TIMELINE_LIMIT' in text
    assert 'TIMELINE_LIMIT: "${LEAGUE_LIMIT' in text


def test_conditional_collectors_are_gated_like_at_home():
    """stratz и candidates включаются по тем же признакам, что дома.

    STRATZ без токена падает на старте; с restart: unless-stopped это
    превратилось бы в ровный шум в логах — поломка, неотличимая от
    работы. Поэтому они в отдельных профилях, а bootstrap решает.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert '"apps", "stratz"' in text
    assert '"apps", "candidates"' in text
    boot = BOOTSTRAP.read_text(encoding="utf-8")
    assert "STRATZ_API_TOKEN" in boot and "profile stratz" in boot
    assert "CANDIDATES_ENABLED" in boot and "profile candidates" in boot

    # И профили ПОДСТАВЛЯЮТСЯ в подъём, а не просто вычисляются. Функция
    # может быть безупречной и не использоваться: тогда stratz с
    # candidates просто не поднимутся, молча и без единой жалобы.
    up = [l for l in boot.splitlines() if "up -d" in l and "COMPOSE" in l]
    assert up, "строка подъёма стека не найдена"
    assert any("$profiles" in l for l in up), (
        f"профили вычисляются, но не подставляются в up: {up}")


# -- источник по солям (спринт 181) --------------------------------------------

def test_the_salt_collector_is_a_service():
    """Скачивание по солям поставлено на поток, а не запускается руками.

    Соли добываются по cron ежечасно и накапливаются. Источник, который
    надо запускать вручную, превращает накопление в долг: очередь растёт,
    а реплеи — файлы с двухнедельным сроком жизни — успевают протухнуть
    раньше, чем до них дойдут руки.
    """
    # Читаем файл напрямую, а не через `docker compose config`: docker
    # есть не на всякой машине, где гоняют тесты, и проверка «сервис
    # объявлен» не должна от него зависеть.
    svc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert "salt-collector" in svc
    assert svc["salt-collector"]["environment"]["COLLECTOR_SOURCE"] == "salts"


def test_the_salt_collector_keeps_pace_with_the_miner():
    """Темп скачивания согласован с темпом добычи.

    Наполнитель берёт 8 солей в час (GC_SALTS_PER_RUN), значит и качать
    надо 8 в час. Быстрее — очередь пустует и часть времени источник
    работает вхолостую; медленнее — соли копятся, а реплей у Valve живёт
    около двух недель, и разница уходит в протухшие ссылки.
    """
    filler = (Path(__file__).resolve().parents[1]
              / "gc-node" / "gc-salts.mjs").read_text(encoding="utf-8")
    mined = int(re.search(r"GC_SALTS_PER_RUN \|\| (\d+)", filler).group(1))

    compose_text = COMPOSE.read_text(encoding="utf-8")
    downloaded = int(re.search(r'SALTS_LIMIT: "\$\{SALTS_LIMIT:-(\d+)\}"',
                               compose_text).group(1))
    assert downloaded == mined, (
        f"добываем {mined} солей за прогон, качаем {downloaded} реплеев — "
        "очередь будет расти или простаивать")


def test_the_salt_collector_needs_no_opendota_quota():
    """У источника нет ключа OpenDota — и это его смысл.

    Он работает ровно тогда, когда суточная квота выбрана и остальные
    реплейные источники стоят. Появись здесь обращение к OpenDota — это
    свойство пропало бы, а заметить пропажу было бы нечем: источник
    продолжал бы качать, просто отказывая в самый нужный момент.
    """
    src = (ROOT / "apps" / "data-collector" / "src" / "collector"
           / "sources" / "salts.py").read_text(encoding="utf-8")
    # Загрузчик у всех источников общий (распаковка и проверка
    # целостности написаны один раз), но ЗАПРОСОВ к API тут быть не
    # должно: адрес уже готов.
    assert "_match_detail" not in src
    assert "proMatches" not in src and "publicMatches" not in src
