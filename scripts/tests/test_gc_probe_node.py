"""Схема GC-замера кодирует то, что мы думаем (спринт 167).

ЧТО ЗДЕСЬ ВАЖНО. Замер разговаривает с Game Coordinator четырьмя
protobuf-сообщениями, выписанными вручную: полная схема Valve тянется
пакетом `steam-resources`, который на современном protobufjs просто не
грузится (`ProtoBuf.newBuilder is not a function`).

Ручная схема даёт ровно один способ ошибиться молча — и он сработал ещё
до первого живого запуска. protobufjs по умолчанию переименовывает поля в
camelCase (`match_id` → `matchId`), а `fromObject` выбрасывает ключи,
которых не знает, БЕЗ ошибки. Запрос уезжал пустым, ответ разбирался без
соли, и выглядело бы это как «Valve ничего не отдаёт»: беда своего кода
читалась бы как отказ чужого сервиса. Лечится `keepCase: true`.

Поэтому проверяется не «схема разбирается», а «в проводе оказалось то,
что мы положили»: запрос с match_id непустой и содержит номер, ответ с
солью разбирается обратно в ту же соль.

Номера полей и сообщений сверяются с настоящими .proto Valve — они
выписаны в комментариях скрипта, и разойтись им нельзя.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gc-node" / "gc-probe.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node не установлен")

MATCH_ID = 8123456789          # больше 2^32: проверяем и 64-битность
SALT = 3735928559              # больше 2^31: fixed32 беззнаковый


def _schema() -> str:
    """Схема из самого скрипта — не копия.

    Копия проверяла бы саму себя: разъедься она со скриптом, тест
    остался бы зелёным на несуществующей схеме.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    return src.split("const SCHEMA = `")[1].split("`;")[0]


def _node(tmp_path: Path, body: str):
    """Выполнить кусок JS с protobufjs. Возвращает разобранный stdout."""
    node_modules = Path(__file__).resolve().parents[1] / "gc-node" / "node_modules"
    if not (node_modules / "protobufjs").is_dir():
        pytest.skip("protobufjs не установлен — сначала make gc-node")
    (tmp_path / "schema.txt").write_text(_schema(), encoding="utf-8")
    script = tmp_path / "run.cjs"
    script.write_text(textwrap.dedent(f"""
        const protobuf = require({json.dumps(str(node_modules / 'protobufjs'))});
        const fs = require('fs');
        const schema = fs.readFileSync({json.dumps(str(tmp_path / 'schema.txt'))}, 'utf8');
        const root = protobuf.parse(schema, {{ keepCase: true }}).root;
        {body}
    """), encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


def test_request_carries_the_match_id(tmp_path):
    """ГЛАВНОЕ: запрос не уезжает пустым.

    Пустой запрос GC отвергает молча, и замер отчитался бы «ноль солей» —
    вывод про Valve, сделанный из собственной ошибки.
    """
    out = _node(tmp_path, f"""
        const Req = root.lookupType('CMsgGCMatchDetailsRequest');
        const buf = Req.encode(Req.fromObject({{ match_id: {MATCH_ID} }})).finish();
        console.log(buf.toString('hex'));
    """)
    assert out, "запрос закодировался в пустоту"
    # Поле 1, varint: тег 0x08, дальше сам номер матча.
    assert out.startswith("08")
    back = _node(tmp_path, f"""
        const Req = root.lookupType('CMsgGCMatchDetailsRequest');
        const buf = Buffer.from('{out}', 'hex');
        console.log(JSON.stringify(Req.toObject(Req.decode(buf), {{longs: String}})));
    """)
    assert json.loads(back)["match_id"] == str(MATCH_ID)


def test_response_gives_back_the_salt(tmp_path):
    """Соль разбирается обратно в ту же соль.

    replay_salt объявлена fixed32, а не uint32. Перепутай тип — число
    разобралось бы в мусор, и это выглядело бы как «Valve отдала
    ерунду», а не как наша ошибка.
    """
    out = _node(tmp_path, f"""
        const M = root.lookupType('CMsgDOTAMatch');
        const Res = root.lookupType('CMsgGCMatchDetailsResponse');
        const inner = M.encode(M.fromObject({{
            match_id: {MATCH_ID}, cluster: 413, replay_salt: {SALT},
            duration: 2100 }})).finish();
        const out = Res.encode(Res.fromObject({{
            result: 1,
            match: M.toObject(M.decode(inner), {{longs: String}}) }})).finish();
        console.log(JSON.stringify(
            Res.toObject(Res.decode(out), {{longs: String}})));
    """)
    got = json.loads(out)
    assert got["result"] == 1
    assert got["match"]["replay_salt"] == SALT
    assert got["match"]["cluster"] == 413
    assert got["match"]["match_id"] == str(MATCH_ID)


def test_keep_case_is_not_optional():
    """`keepCase: true` обязателен, и это надо сторожить прямо.

    Проверки выше поймали бы его пропажу — но только пока они сами
    задают keepCase. Убери его из скрипта, оставив в тесте, и тест
    остался бы зелёным на сломанном коде.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "keepCase: true" in src, (
        "без keepCase protobufjs переименует поля, и fromObject молча "
        "выбросит наши ключи")


def test_field_numbers_match_valve():
    """Номера полей и сообщений — те, что в .proto Valve.

    Схема выписана вручную; ошибка в номере не падает, а даёт пустой или
    чужой разбор. Числа зафиксированы здесь, чтобы правка «на глаз» не
    прошла незамеченной:
        dota_gcmessages_common.proto — CMsgDOTAMatch
        dota_gcmessages_client.proto — CMsgGCMatchDetails{Request,Response}
        dota_gcmessages_msgid.proto  — 7095/7096
        gcsystemmsgs.proto           — 4004/4006
    """
    src = SCRIPT.read_text(encoding="utf-8")
    for line in ("optional uint64  match_id    = 6;",
                 "optional uint32  cluster     = 10;",
                 "optional fixed32 replay_salt = 13;",
                 "optional uint64 match_id = 1;"):
        assert line in src, f"поле изменилось: {line}"
    for const, value in (("MSG_CLIENT_WELCOME", 4004),
                         ("MSG_CLIENT_HELLO", 4006),
                         ("MSG_MATCH_DETAILS_REQUEST", 7095),
                         ("MSG_MATCH_DETAILS_RESPONSE", 7096)):
        assert f"const {const} = {value};" in src, f"{const} должен быть {value}"


def test_ids_file_is_required():
    """Без списка матчей замер объясняет, где его взять.

    Замер без источника id — это либо трассировка, либо тихий ноль.
    Подсказка тут стоит одной строки и экономит round-trip.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "--ids-file" in src
    # Именно В ВЕТКЕ ОТКАЗА, а не где-нибудь в файле: то же слово стоит в
    # заголовочном комментарии, и проверка «есть в тексте» оставалась
    # зелёной, когда подсказку из сообщения убирали. Поймано мутацией.
    branch = src.split("if (!args.idsFile) {")[1].split("}")[0]
    assert "collectedmatches" in branch, (
        "отказ не показывает, откуда брать список матчей")

# -- проверка соли на CDN ------------------------------------------------------

def test_replay_url_format_is_pinned():
    """Формат ссылки на реплей записан прямо и сторожится.

    Соль, из которой не скачивается файл, — не соль. Но «соль неверна» и
    «мы неверно собрали адрес» дают ОДИН И ТОТ ЖЕ 404, и перепутать их
    значит объявить рабочий механизм нерабочим. Поэтому формат
    зафиксирован здесь, а не проверяется «на глаз».
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert ("`http://replay${cluster}.valve.net/570/"
            "${matchId}_${salt}.dem.bz2`") in src


def test_verification_asks_for_one_byte_not_the_file():
    """Проверяется заголовком, а не скачиванием.

    Реплей весит 58 МиБ. Скачивать их ради вопроса «правильная ли соль»
    значит тратить канал на то, что решается одним запросом диапазона, —
    и на VPS с ограниченным трафиком это заметно.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "Range: 'bytes=0-0'" in src
    assert "resp.status === 206" in src, "206 — нормальный ответ на диапазон"


def test_unreachable_clusters_are_not_verified():
    """Китайские кластеры в проверку не берутся.

    До них нет маршрута, и каждая проверка стоила бы полного таймаута.
    Проверка, которая честно ждёт пятнадцать секунд ради заведомо
    известного ответа, — это не строгость, а трата времени владельца.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "String(m.cluster).startsWith('4')" in src


def test_absence_of_refusals_is_not_called_a_ceiling():
    """Ноль отказов — это «потолок не найден», а не «потолка нет».

    Первый живой прогон дал 200 из 200 без единого отказа при нашей же
    паузе в секунду. Прочитать это как «Valve не ограничивает» — значит
    выдать отсутствие измерения за измерение.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "потолок Valve не найден" in src

# -- передышка отличает темп от бюджета ----------------------------------------

def test_cooldown_retries_instead_of_giving_up():
    """Серия молчаний — развилка замера, а не конец прогона.

    GC не отказывает словами: он замолкает. Молчание одинаково выглядит и
    когда мы спрашиваем слишком часто, и когда выбрали суточный бюджет —
    а лечатся эти два случая по-разному (пауза против второго аккаунта).
    Различает их только время: подождать и спросить снова.

    До этого замер обрывался на первой же серии и отвечал «потолок такой-то»,
    не различив две разные причины.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "--cooldown" in src
    assert "await sleep(args.cooldown * 1000);" in src
    assert "continue;" in src.split("stats.cooled++;")[1][:400], (
        "после передышки прогон должен продолжаться, а не обрываться")


def test_cooldown_is_bounded():
    """Передышек конечное число.

    GC, замолчавший навсегда, иначе держал бы замер вечно — и это была бы
    та же беда, что у питоновского входа: процесс жив, ответа нет, никто
    не знает почему.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "stats.cooled < args.cooldowns" in src


def test_verdict_names_the_two_causes():
    """Вывод называет ПРИЧИНУ, а не только числа.

    «Ожил после паузы» и «не ожил» — это разные решения: держать
    задержку или заводить второй аккаунт. Оставить читателя вычислять
    это из счётчиков значит сделать замер, требующий пересказа.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "упирались в ТЕМП" in src
    assert "исчерпанный БЮДЖЕТ" in src


def test_rate_is_not_computed_from_timeouts():
    """Темп считается по ответам, а не по всем запросам.

    Неотвеченный запрос стоит пятнадцати секунд таймаута. Живой прогон
    показал «8.5 запросов/мин» — число, описывавшее не Valve, а нашу
    собственную арифметику ожидания, и по нему легко было сделать вывод
    о жёстком лимите там, где его нет.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "солей/мин" in src
    assert "stats.asked / (seconds / 60)" not in src, (
        "темп по всем запросам вводит в заблуждение при таймаутах")

# -- вердикт не должен быть поспешным ------------------------------------------

def test_verdict_needs_more_than_a_single_revival():
    """«Ожил после паузы» само по себе ничего не доказывает.

    ЖИВАЯ ОШИБКА, а не гипотеза. Замер напечатал «упирались в ТЕМП,
    лечится задержкой» — на прогоне, где пауза была ТА ЖЕ секундная, что
    в прошлый раз прошла 200 запросов из 200 без потерь, а сломалось на
    пятом. Темп одинаковый, поведение противоположное: дело не в темпе.

    Различает случаи второй признак — СКОЛЬКО пришло после паузы. Много и
    ровно — темп. Пара штук и снова тишина — накопитель, который копится
    в простое и отдаёт по капле.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "perRevival" in src, "вердикт по одному признаку уже ошибся"
    assert "НАКОПИТЕЛЬ" in src
    assert "args.stopAfter * 2" in src, "нужен порог «идёт ровно»"


def test_steady_rate_excludes_the_prefilled_bucket():
    """Скорость считается ПОСЛЕ первого молчания, а не за весь прогон.

    До первого молчания расходуется запас, накопленный простоем: первый
    живой прогон дал 200 подряд и вывод «ограничений нет», а следующий
    сломался на пятом запросе. Счёт по всему прогону меряет запас, а
    нужна скорость его восполнения — именно она отвечает на вопрос
    «сколько в сутки».
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "firstFailureTime" in src
    assert "установившийся темп" in src
    assert "в сутки" in src, "оценку надо приводить к суткам, а не к минуте"


def test_the_daily_estimate_comes_from_the_steady_rate():
    """Суточная оценка выводится из установившегося темпа.

    Взять её из общего счёта значило бы умножить запас на сутки и
    получить число, которого не будет никогда.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    block = src.split("установившийся темп")[1][:200]
    assert "perMin * 60 * 24" in block
