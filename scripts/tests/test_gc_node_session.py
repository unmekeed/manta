"""Правила подъёма сессии Game Coordinator — общие для ВСЕХ скриптов
(спринт 178).

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ. Скриптов, разговаривающих с GC, уже два (замер и
наполнитель), и второй писался переносом кода из первого. При переносе
потерялась одна строка — `client.gamesPlayed([APPID])`, — и наполнитель
на живом VPS молча ждал сорок пять секунд, а потом объявлял «GC не
прислал ClientWelcome». Со стороны это читалось как «Valve не пускает
аккаунт», хотя аккаунт входил и в замере работал.

Проверять надо не конкретный файл, а ПРАВИЛО, и на всех, кто ему
подчиняется. Список скриптов собирается сам — по наличию `sendToGC`:
третий скрипт подхватится без правки теста, а иначе он унаследовал бы ту
же дыру, что и второй.
"""
import re
from pathlib import Path

import pytest

GC_DIR = Path(__file__).resolve().parents[1] / "gc-node"

# Скрипты, которые разговаривают с GC. Не список имён: имена разъезжаются
# молча, а признак «шлёт сообщения в GC» — нет.
TALKERS = sorted(p for p in GC_DIR.glob("*.mjs")
                 if "sendToGC" in p.read_text(encoding="utf-8"))


def src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_there_is_something_to_check():
    """Страховка от проверки пустоты.

    Сломайся сбор списка — параметризованные проверки ниже прошли бы на
    пустом множестве, и файл выглядел бы зелёным, ничего не проверяя.
    """
    names = {p.name for p in TALKERS}
    assert {"gc-probe.mjs", "gc-salts.mjs"} <= names, names


@pytest.mark.parametrize("script", TALKERS, ids=lambda p: p.name)
def test_the_script_announces_it_is_playing_dota(script):
    """ГЛАВНОЕ: перед разговором с GC объявляется запуск игры.

    Game Coordinator — часть игры, а не Steam. Клиенту, который не
    «играет», он сессию не поднимает и ClientWelcome не шлёт, сколько ему
    ни здоровайся. Вход в Steam при этом проходит успешно, поэтому отказ
    выглядит как проблема на стороне Valve.
    """
    assert "client.gamesPlayed([APPID]);" in src(script), (
        "не объявлен запуск Dota 2 — GC не поднимет сессию")


@pytest.mark.parametrize("script", TALKERS, ids=lambda p: p.name)
def test_the_announcement_comes_before_waiting_for_the_welcome(script):
    """И объявляется ДО ожидания, а не после.

    Порядок здесь не косметика: `waitForGC` ждёт до сорока пяти секунд,
    и объявление после него пришлось бы на уже истёкшее ожидание — то
    есть строка была бы на месте, а толку от неё никакого.
    """
    body = src(script)
    assert body.index("client.gamesPlayed([APPID]);") < body.index("waitForGC()")


@pytest.mark.parametrize("script", TALKERS, ids=lambda p: p.name)
def test_a_silent_gc_explains_where_to_look(script):
    """Отказ называет, что проверять у аккаунта.

    «ClientWelcome не пришёл» само по себе не отличает нашу ошибку от
    ограничений аккаунта. Живой случай был нашей ошибкой, и без подсказки
    искали бы её у Valve.
    """
    # Смотрим на то, что ПЕЧАТАЕТСЯ, а не на первое упоминание слова в
    # файле: им оказался комментарий, объясняющий эту же беду, и проверка
    # проходила на скрипте, который в отказе молчал.
    printed = " ".join(re.findall(r"console\.error\((.*?)\);", src(script),
                                  re.S))
    assert "ClientWelcome" in printed, "отказ не называет, чего не дождались"
    assert "библиотеке" in printed, "не сказано, что проверять у аккаунта"


@pytest.mark.parametrize("script", TALKERS, ids=lambda p: p.name)
def test_the_hello_is_repeated_while_waiting(script):
    """Привет повторяется, а не шлётся один раз.

    GC часто игнорирует первый ClientHello, пока сессия приложения не
    поднялась целиком. Одиночный привет давал бы ложное «GC молчит» на
    ровном месте — и отличить его от настоящего было бы нечем.
    """
    body = src(script)
    assert re.search(r"setInterval\(", body), "привет не повторяется"
    assert body.count("MSG_CLIENT_HELLO") >= 3, (
        "привет шлётся один раз: объявление, отправка и повтор — это три "
        "упоминания")
