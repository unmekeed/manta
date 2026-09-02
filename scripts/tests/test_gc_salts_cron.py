"""Обёртка расписания вокруг наполнителя солей (спринт 173).

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ. Ровно одно решение, но самое дорогое: КОГДА
молчать, а когда звать.

GC не отказывает словами — он перестаёт отвечать. На исчерпанном бюджете
это штатное состояние, наступающее КАЖДУЮ НОЧЬ. Ошибись в эту сторону — и
получишь ежесуточную ложную тревогу; а ложная тревога по расписанию не
просто бесполезна, она обучает не читать канал, и следом мимо проходит
настоящая беда. Ровно так в этом проекте тринадцать дней никто не
замечал, что бэкап не снимается (спринты 142, 163).

Ошибись в другую сторону — и сломанная добыча (нет токена, не поднялась
сессия, лежит база) будет молчать так же, как удачная ночь: реплеи
перестанут доезжать, а всё будет выглядеть зелёным.

Обёртка исполняется НАСТОЯЩАЯ, со стабами вместо node и curl: проверять
пересказ скрипта смысла нет — cron запускает его, а не пересказ.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gc-salts.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("node") is None,
    reason="нужны bash и node: проверка модулей исполняется настоящая")

REAL_NODE = shutil.which("node") or "node"


def run(tmp_path, *, node_rc=0, node_out="соли: спрошено 8", token=True,
        node_installed=True, deps=True, modules=("steam-user", "protobufjs", "pg")):
    """Прогнать обёртку в поддельном репозитории. -> (rc, вывод, телеграм)."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "gc-node").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "gc-salts.sh")
    (repo / "scripts" / "gc-node" / "gc-salts.mjs").write_text("//", "utf-8")
    # package.json — источник списка зависимостей и для скрипта, и отсюда.
    (repo / "scripts" / "gc-node" / "package.json").write_text(
        '{"dependencies": {"steam-user": "^5", "protobufjs": "^7", "pg": "^8"}}',
        "utf-8")
    if deps:
        mods = repo / "scripts" / "gc-node" / "node_modules"
        mods.mkdir()
        for name in modules:
            (mods / name).mkdir()
            (mods / name / "package.json").write_text(
                '{"name": "%s", "version": "1.0.0", "main": "i.js"}' % name,
                "utf-8")
            (mods / name / "i.js").write_text("", "utf-8")

    state = tmp_path / "state"
    state.mkdir()
    if token:
        (state / "refresh-token").write_text("не-настоящий-токен", "utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    tg_log = tmp_path / "telegram.log"
    # curl подменяется целиком: настоящая отправка в Telegram из теста —
    # это сообщение живому владельцу.
    (bindir / "curl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tg_log}\n', "utf-8")
    if node_installed:
        # Стаб подменяет только ЗАПУСК наполнителя. Вызов `node -e`, которым
        # обёртка проверяет установленные модули, делегируется настоящему
        # node: иначе проверка модулей исполнялась бы стабом, то есть не
        # проверялась бы вовсе, а параметр `modules` ничего не значил бы.
        (bindir / "node").write_text(
            f'#!/bin/sh\n'
            f'if [ "$1" = "-e" ]; then exec {REAL_NODE} "$@"; fi\n'
            f'echo "{node_out}"\nexit {node_rc}\n', "utf-8")
    for name in ("curl", "node"):
        p = bindir / name
        if p.exists():
            p.chmod(0o755)

    env = dict(os.environ)
    env.update({
        # PATH подменяется целиком, чтобы «node не установлен» проверялось
        # так же, как на живой машине: command -v ищет по PATH.
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GC_STATE_DIR": str(state),
        "MANTA_TRAIN_ENV": str(tmp_path / "нет-такого.env"),
        "TELEGRAM_BOT_TOKEN": "стаб",
        "TELEGRAM_CHAT_ID": "стаб",
    })
    proc = subprocess.run(["bash", str(repo / "scripts" / "gc-salts.sh")],
                          capture_output=True, text=True, env=env, timeout=30)
    tg = tg_log.read_text("utf-8") if tg_log.exists() else ""
    return proc.returncode, proc.stdout + proc.stderr, tg


# -- молчание не будит владельца ------------------------------------------------

def test_a_normal_portion_is_quiet(tmp_path):
    """Удачный прогон не пишет в Telegram.

    Ежечасное «всё хорошо» — это шестнадцать сообщений в сутки, после
    которых канал перестают открывать.
    """
    rc, out, tg = run(tmp_path)
    assert rc == 0, out
    assert tg == "", f"обёртка написала в Telegram: {tg!r}"


def test_exhausted_budget_is_not_an_alert(tmp_path):
    """ГЛАВНОЕ утверждение спринта.

    «GC замолчал — бюджет на сегодня» наступает каждую ночь. Красный
    алерт на это был бы ежесуточной ложной тревогой.
    """
    rc, out, tg = run(tmp_path, node_rc=0,
                      node_out="GC замолчал после 3 солей — бюджет на сегодня")
    assert rc == 0, out
    assert tg == "", f"исчерпанный бюджет позвал владельца: {tg!r}"


def test_a_machine_without_a_token_stays_silent(tmp_path):
    """Добыча не включена — это конфигурация, а не поломка.

    Токен заводится вручную (нужен пароль бота). На машине, где его не
    заводили, ежечасный красный алерт сообщал бы о нашем же решении.
    """
    rc, out, tg = run(tmp_path, token=False)
    assert rc == 0, out
    assert tg == ""
    assert "не настроена" in out


def test_without_a_token_node_is_not_even_started(tmp_path):
    """И вход в Steam при этом не делается.

    Каждая попытка входа — это разговор с Valve. Ходить туда ежечасно на
    машине, где добыча выключена, незачем.
    """
    rc, out, _ = run(tmp_path, token=False)
    assert "соли: спрошено" not in out, "наполнитель всё-таки запустился"


# -- а вот это разбудить обязано ------------------------------------------------

def test_a_dead_steam_session_alerts(tmp_path):
    """Сессия не поднялась — соли не будет ни сегодня, ни завтра.

    Это чинится только руками (токен, сеть, CM), и промолчать здесь
    значит остановить добычу навсегда с зелёным видом.
    """
    rc, out, tg = run(tmp_path, node_rc=1)
    assert rc == 1
    assert "Manta" in tg and "соли" in tg


def test_a_missing_dependency_alerts(tmp_path):
    """Нет токена у наполнителя или лежит база — тоже поломка."""
    rc, out, tg = run(tmp_path, node_rc=2)
    assert rc == 1
    assert tg, "молчаливый отказ"


def test_an_unknown_exit_code_is_not_treated_as_success(tmp_path):
    """Неизвестный код — тревога, а не «наверное, обошлось».

    Считать успехом всё, что не перечислено, значит превратить любой
    будущий код возврата в тишину.
    """
    rc, out, tg = run(tmp_path, node_rc=42)
    assert rc == 1
    assert "42" in tg or "42" in out


def test_missing_node_alerts_because_the_schedule_promised_it(tmp_path):
    """Расписание стоит, а node нет — это запланированный отказ.

    Установка ставит node именно потому, что расписание на него
    рассчитывает; пропажа — повод сказать вслух.
    """
    rc, out, tg = run(tmp_path, node_installed=False)
    assert rc == 1
    assert tg
    # Именно НАЗВАННАЯ причина, а не «код 127» от bash. Мутация «убрать
    # проверку вовсе» проходила проверку `"node" in out`: bash сам пишет
    # «node: command not found», и отказ выглядел похоже — но в Telegram
    # уезжало бы «наполнитель завершился с кодом 127», из которого ничего
    # не следует. Поймано мутацией.
    assert "не установлен" in out, out


def test_missing_node_modules_alerts(tmp_path):
    """То же для зависимостей Node: их ставит make gc-node."""
    rc, out, tg = run(tmp_path, deps=False)
    assert rc == 1
    assert "gc-node" in out
    assert tg


# -- порядок проверок -----------------------------------------------------------

def test_the_token_is_checked_before_anything_that_can_alert(tmp_path):
    """Выключенная добыча молчит, даже если машина не готова.

    Иначе машина без токена И без node слала бы алерт про node — то есть
    ругалась бы на отсутствие инструмента для работы, которую делать не
    собиралась.
    """
    rc, out, tg = run(tmp_path, token=False, node_installed=False, deps=False)
    assert rc == 0, out
    assert tg == ""


def test_the_token_value_never_reaches_the_output(tmp_path):
    """Токен равносилен паролю и в вывод не попадает.

    Вывод cron уезжает в лог, а при отказе — в Telegram.
    """
    rc, out, tg = run(tmp_path, node_rc=1)
    assert "не-настоящий-токен" not in out
    assert "не-настоящий-токен" not in tg


def test_a_present_directory_with_a_missing_module_is_named_correctly(tmp_path):
    """ГЛАВНОЕ, ради чего проверка стала проверять модули.

    ЖИВОЙ СЛУЧАЙ, пойманный ПЕРЕД деплоем спринта 173. Наполнителю
    понадобился `pg`, а на машине каталог node_modules существовал с
    прошлого `make gc-node`. Проверка «каталог на месте» проходила, node
    падал на require кодом 1 — тем же, каким мы обозначаем «сессия Steam
    не поднялась», — и в Telegram уезжало бы про Steam о беде, до Steam
    не дошедшей. Чинить пошли бы токен и сеть.

    Проверять надо ЭФФЕКТ (модули на месте), а не АРТЕФАКТ (каталог
    создан) — та же форма ошибки, что и «файл записан» вместо «настройка
    подействовала» в спринте 169.
    """
    rc, out, tg = run(tmp_path, modules=("steam-user", "protobufjs"))  # без pg
    assert rc == 1
    assert "pg" in out, out
    assert "gc-node" in out, "не сказано, чем чинить"
    # И ни слова про Steam: беда до него не дошла.
    assert "Steam" not in out and "сессия" not in out, out


def test_the_dependency_list_is_taken_from_package_json(tmp_path):
    """Список зависимостей не переписан в скрипт руками.

    Рукописный перечень разъедется при первой же новой зависимости — и
    разъедется молча, ровно как это и случилось с `pg`.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "package.json" in src
    for name in ("steam-user", "protobufjs", "pg"):
        assert f'"{name}"' not in src, (
            f"{name} вписан в обёртку руками — список обязан быть один")
