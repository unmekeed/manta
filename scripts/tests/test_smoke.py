"""Сквозной прогон конвейера (спринт 203; проводка проверена в 206).

Часть проверок статическая: сам прогон требует живого стека. Стережётся
то, что легко решить наоборот и что определяет, будет ли от проверки
польза.

ГЛАВНОЕ РЕШЕНИЕ — НЕ СИНТЕТИЧЕСКИЙ МАТЧ. Выдуманный match_id проще, но
его строки витрины попадают в обучающую выборку. Уборка после себя
работает лишь пока уборка срабатывает: прерванный прогон оставил бы мусор
в датасете МОЛЧА — то есть завёл бы ровно ту беду, от которой этот проект
лечится весь сентябрь. Настоящий матч ничем не рискует: отчёты
перегенерируемы по построению (спринт 195).

ЧТО ЗДЕСЬ ЕЩЁ И ПОЧЕМУ ОНО НЕ СТАТИЧЕСКОЕ. Первая редакция прогона звала
продюсера через `docker exec` БЕЗ `-i`, то есть не отдавала контейнеру
стандартный ввод: продюсер читал EOF сразу, публиковал пустоту и выходил
с нулём. Проверка печатала «событие опубликовано», ждала таймаут и
сообщала «отчёт не появился» — при полностью исправном конвейере.

Ни один статический тест этого не ловил и поймать не мог: в тексте были
и продюсер, и топик, и сравнение отметок. Ловится оно только запуском —
поэтому ниже прогон исполняется целиком на подставных `docker` и `curl`,
где подставной `docker` ВОСПРОИЗВОДИТ настоящее поведение: без `-i`
стандартный ввод процессу не достаётся.
"""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts" / "smoke.sh"


def code() -> str:
    """Текст без комментариев: проверяем КОД, а не объяснения рядом."""
    return "\n".join(ln for ln in SMOKE.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))


def test_the_script_exists_and_parses():
    """Страховка от проверки пустоты."""
    assert SMOKE.exists()
    assert len(code()) > 500, "разбор дал пустой текст — проверки ниже слепы"


def test_no_synthetic_match_is_inserted():
    """ГЛАВНОЕ: прогон не пишет в витрину.

    Вставка выдуманного матча — самый очевидный способ сделать сквозной
    тест, и самый опасный: строки с несуществующим match_id уедут в
    обучающую выборку, а прерванный прогон не уберёт их за собой и не
    скажет об этом.

    Проверяется отсутствие ЗАПИСИ, а не отсутствие слова «синтетика»:
    INSERT в витрину — это то, чего быть не должно, как бы он ни
    назывался.
    """
    src = code().upper()
    for danger in ("INSERT INTO MATCHTIMELINEFEATURES", "INSERT INTO MATCHDRAFT",
                   "INSERT INTO REPLAYEVENTS"):
        assert danger not in src, (
            f"прогон пишет в витрину ({danger}) — синтетический матч уедет "
            f"в обучающую выборку")


def test_a_real_recent_match_is_used():
    """Матч берётся из витрины, и именно свежий.

    У старого реплей мог уехать по ретеншену, часть разбора собралась бы
    не полностью, и прогон упал бы отказом настоящим, но НЕ ТЕМ, о
    котором эта проверка. Ложная тревога в сторожe дороже его отсутствия:
    на неё ходят.
    """
    src = code()
    assert "FROM MatchTimelineFeatures" in src, "матч не берётся из витрины"
    assert "ORDER BY computed_at DESC" in src, "берётся не свежий матч"


def test_the_missing_topic_is_diagnosed_before_the_wait():
    """Отсутствие топика проверяется ДО публикации.

    При выключенном автосоздании продюсер молчит, и без этой проверки
    прогон свалился бы в общий таймаут с диагнозом «отчёт не появился» —
    верным по форме и бесполезным по содержанию. Именно так три дня
    выглядела авария 3–6 сентября.
    """
    src = code()
    head = src.split("kafka-console-producer", 1)[0]
    assert "kafka-topics.sh" in head, (
        "топик не проверяется до публикации — потеря сообщения выльется "
        "в таймаут вместо диагноза")
    assert "make topics" in src, "не сказано, чем лечить отсутствующий топик"


def test_the_verdict_comes_from_the_database_not_the_log():
    """Успех определяется появлением ОТЧЁТА, а не строкой в логе.

    Лог говорит, что генератор ПЫТАЛСЯ. Вопрос же в том, появился ли
    отчёт: ровно то различие, из-за которого «конвейер жив» и «данные
    свежие» — разные утверждения, и первое неоднократно было истинным при
    ложном втором.
    """
    src = code()
    assert "FROM MatchReports" in src, "вердикт выносится не по отчёту"
    assert "generated_at" in src, "не проверяется, что отметка СДВИНУЛАСЬ"


def test_an_unchanged_report_is_a_failure():
    """Неизменившийся отчёт — отказ, а не «и так сойдёт».

    Отчёт у свежего матча обычно УЖЕ ЕСТЬ. Проверь мы лишь его наличие —
    прогон был бы зелёным при полностью мёртвом конвейере: строка-то на
    месте со вчера. Значение имеет только сдвиг отметки времени.
    """
    src = code()
    assert '"$now" != "$before"' in src, (
        "сравнение с состоянием ДО прогона отсутствует — проверка пройдёт "
        "на старом отчёте при мёртвом конвейере")


def test_the_available_block_is_checked():
    """Проверяется и блок available — метка свежести кода.

    Он появился в спринте 195. Его отсутствие означает, что генератор
    крутит старый образ, — тот самый отказ, который дважды за неделю
    выглядел как успешная раскатка (`up -d` показал Running, а код был
    прежний).
    """
    assert "available" in code(), "свежесть кода генератора не проверяется"


def test_the_failure_names_where_to_look():
    """Отказ сужает диагноз, а не сообщает о себе.

    Читатель приходит сюда, когда у него меньше всего времени. Различие
    «событие не доехало» и «доехало и упало» видно по лагу
    консьюмер-группы, и сказать об этом надо в самом отказе.
    """
    src = code()
    assert "kafka-consumer-groups.sh" in src, "не сказано, как проверить лаг"
    assert "manta-report-generator-1" in src, "не назван лог генератора"
    assert "manta-ml-service-1" in src, (
        "не названа частая причина — модель, а не конвейер")


# -- прогон целиком на подставном окружении ------------------------------------
#
# Подставной `docker` повторяет ЕДИНСТВЕННОЕ поведение настоящего, которое
# здесь имеет значение: без `-i` контейнеру не отдаётся стандартный ввод.
# Заглушка, устроенная удобнее оригинала, проверяет несуществующую систему
# — правило, которое этот проект уже оплатил трижды.

FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
STATE="$MANTA_FAKE_STATE"
args=("$@")

# Настоящий docker отдаёт процессу stdin ТОЛЬКО при -i. Без него продюсер
# читает EOF сразу и публикует пустоту, возвращая ноль.
interactive=0
for a in "${args[@]}"; do [ "$a" = "-i" ] && interactive=1; done

tool=""
for a in "${args[@]}"; do
    case "$a" in */kafka-*.sh) tool="${a##*/}";; esac
done

produced() { cat "$STATE/produced" 2>/dev/null || echo 0; }

case "$tool" in
kafka-topics.sh)
    echo features.calculated; echo match.downloaded; exit 0;;
kafka-get-offsets.sh)
    # Конец топика растёт ровно на число опубликованных сообщений.
    echo "features.calculated:0:$((10 + $(produced)))"
    echo "features.calculated:1:7"
    exit 0;;
kafka-console-producer.sh)
    got=""
    [ "$interactive" = 1 ] && got="$(cat)"
    if [ -n "$got" ] && [ -z "${MANTA_FAKE_SWALLOW:-}" ]; then
        printf '%s\n' "$got" >> "$STATE/messages"
        echo $(( $(produced) + 1 )) > "$STATE/produced"
    fi
    exit 0;;
esac

# psql: SQL — последний аргумент.
sql="${args[$(( ${#args[@]} - 1 ))]}"
case "$sql" in
*available*) echo true;;
*)  # Отметка отчёта сдвигается ровно тогда, когда событие опубликовано:
    # конвейер здесь исправен, и вся разница — в том, доехало ли событие.
    printf '2026-09-08T05:00:%02d.000000\n' "$(produced)";;
esac
"""

FAKE_CURL = """#!/usr/bin/env bash
cat >/dev/null
echo 8888888888
"""


def run_smoke(tmp_path, swallow=False, timeout_s=60):
    """Прогнать scripts/smoke.sh на подставных docker и curl."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        p = bin_dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MANTA_FAKE_STATE"] = str(state)
    # Файла нет — прогон не подхватит боевых настроек машины.
    env["MANTA_TRAIN_ENV"] = str(tmp_path / "нет-такого.env")
    if swallow:
        env["MANTA_FAKE_SWALLOW"] = "1"

    started = time.monotonic()
    r = subprocess.run(["bash", str(SMOKE), "--timeout", str(timeout_s)],
                       capture_output=True, text=True, env=env,
                       timeout=timeout_s + 60)
    return r, time.monotonic() - started, state


def test_the_run_passes_when_the_pipeline_works(tmp_path):
    """Страховка от «зелено, потому что ничего не проверяется».

    Тест ниже требует ОТКАЗА, и он проходил бы при вечно падающем
    прогоне. Значит, сперва надо убедиться, что на исправном конвейере
    прогон доходит до конца.
    """
    r, _, state = run_smoke(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "КОНВЕЙЕР ЖИВ" in r.stdout, r.stdout
    assert (state / "messages").exists(), (
        "продюсер не получил стандартный ввод — сообщение не опубликовано")
    assert '"match_id":8888888888' in (state / "messages").read_text(
        encoding="utf-8")


def test_an_unpublished_event_is_named_at_once_not_waited_out(tmp_path):
    """ГЛАВНОЕ: непопадание в топик — отдельный диагноз, а не таймаут.

    Ровно этот отказ случился на живой машине: продюсер звался без `-i`,
    не публиковал ничего и выходил с нулём. Прогон печатал «событие
    опубликовано», ждал пять минут и сообщал «отчёт не появился» — при
    исправном конвейере, нулевом лаге и генераторе, производившем в те же
    минуты отчёты по другим матчам. Диагноз уводил в сторону тем вернее,
    чем внимательнее его читали.

    Проверяется и ЧТО сказано, и КОГДА: вердикт, выданный по истечении
    таймаута, стоит читателю тех же минут ожидания и той же неверной
    догадки.
    """
    r, elapsed, state = run_smoke(tmp_path, swallow=True, timeout_s=60)
    assert r.returncode != 0, r.stdout
    assert "конец топика" in r.stdout, (
        f"отказ не назвал непопадание в топик:\n{r.stdout}")
    assert "отчёт не появился" not in r.stdout, (
        "прогон свалился в общий таймаут вместо диагноза о публикации")
    assert elapsed < 30, (
        f"диагноз выдан через {elapsed:.0f}с — прогон досидел до таймаута "
        f"вместо того, чтобы остановиться сразу")
    assert not (state / "messages").exists()


def test_the_producer_is_the_only_call_given_stdin(tmp_path):
    """`-i` стоит там, где читают ввод, и не стоит там, где не читают.

    Проверка не текстовая: подставной docker сообщает, чем его звали, и
    утверждение читается с той стороны, с какой его увидит Kafka.
    """
    src = code()
    piped = [ln for ln in src.splitlines() if "| $KAFKA" in ln]
    assert piped, "в прогоне не нашлось ни одной команды, читающей stdin"
    for ln in piped:
        assert "$KAFKA_IN/" in ln, (
            f"команде отдают ввод через форму без -i: {ln.strip()}")
    assert "KAFKA_IN=\"docker exec -i " in src, (
        "форма для читающих stdin команд определена без -i")
