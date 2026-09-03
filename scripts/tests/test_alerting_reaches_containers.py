"""Сервис, умеющий кричать, обязан получить чем (спринт 191).

ЧТО СЛУЧИЛОСЬ. Генерация отчётов встала 2 сентября в 07:00 и простояла
29 часов. Никто не узнал. Разбирая это, мы завели сторожа — и он бы
промолчал ровно так же, потому что `TELEGRAM_BOT_TOKEN` в compose
получал только ml-autotrain. Код кричать умеет, окружение крикнуть не
даёт, и отличить это от «всё хорошо» нельзя ничем.

ФОРМА ДЕФЕКТА тут не новая: верное решение, применённое к части своих
случаев. Токен добавили тому сервису, ради которого его заводили, и не
добавили соседнему, когда кричать понадобилось и ему.

Поэтому проверка идёт ОТ КОДА, а не от списка сервисов: кто в исходниках
берёт TelegramNotifier, тому и нужен токен. Рукописный перечень
разъехался бы при первом же новом сторожевом сервисе — то есть в точности
тогда, когда проверка нужнее всего.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML нужен для разбора compose")

ROOT = Path(__file__).resolve().parents[2]
APPS = ROOT / "apps"
COMPOSE = ROOT / "deployments" / "docker-compose.yml"

NEEDED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

# Соответствие «каталог приложения → сервисы compose». У ml-service их
# два: сервер предсказаний и автообучение из того же образа, и кричит
# именно второй.
SERVICES_OF = {
    "ml-service": ("ml-autotrain",),
    "report-generator": ("report-generator",),
    "data-collector": ("data-collector", "timeline-collector",
                       "pro-timeline-collector", "league-collector",
                       "pro-replay-collector", "stratz-collector",
                       "salt-collector", "candidates-collector"),
}


def _load_compose() -> dict:
    """Разбор базового файла с игнорированием тега `!override`."""
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor(
        "!override",
        lambda ldr, node: ldr.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode) else ldr.construct_object(node))
    return yaml.load(COMPOSE.read_text(encoding="utf-8"), Loader=Loader)


def apps_that_alert() -> set[str]:
    """Каталоги приложений, чей код создаёт TelegramNotifier."""
    found = set()
    for path in APPS.rglob("*.py"):
        if "/tests/" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Именно ВЫЗОВ, а не импорт: модуль может переэкспортировать класс,
        # никого при этом не оповещая.
        if re.search(r"TelegramNotifier\s*\(", text):
            found.add(path.relative_to(APPS).parts[0])
    return found


def test_the_scan_finds_someone():
    """Страховка от тихой поломки самой проверки.

    Если regexp перестанет находить (класс переименовали, обёртку
    завели), список окажется пуст, и следующий тест станет тождественно
    зелёным — то есть исчезнет, не покраснев ни разу.
    """
    assert apps_that_alert(), "ни один сервис не создаёт TelegramNotifier"


def test_every_alerting_service_gets_the_token():
    """У кого в коде есть сторож, у того в compose есть токен."""
    services = _load_compose()["services"]
    missing = []
    for app in sorted(apps_that_alert()):
        for name in SERVICES_OF.get(app, (app,)):
            env = (services.get(name) or {}).get("environment") or {}
            for var in NEEDED:
                if var not in env:
                    missing.append(f"{name}: нет {var}")
    assert not missing, (
        "сервис умеет слать уведомления, но окружения для этого не "
        "получает — сторож будет молчать:\n  " + "\n  ".join(missing))


def test_the_service_map_names_only_real_services():
    """Опечатка в карте сервисов не должна давать зелёный тест.

    `SERVICES_OF` — рукописная часть, и она обязана быть сверена: имя с
    опечаткой просто не нашлось бы в compose, проверка выше не нашла бы
    окружения... и упала бы по верной причине. А вот ЛИШНЕЕ имя молча
    расширило бы проверку на несуществующий сервис.
    """
    services = set(_load_compose()["services"])
    unknown = sorted({name for names in SERVICES_OF.values()
                      for name in names} - services)
    assert not unknown, f"в карте есть сервисы, которых нет в compose: {unknown}"
