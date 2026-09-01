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
