"""Тесты учений по восстановлению (scripts/backup-drill.sh, спринт 138).

Сам прогон требует Docker и живых баз — он и есть учения. Здесь
проверяется то, что можно проверить без них и что дороже всего стоит
ошибиться: чтобы учения ни при каких обстоятельствах не записали чужой
слепок в БОЕВУЮ базу.

Цена ошибки несимметрична. Провалившиеся учения — это плохая новость.
Учения, восстановившие старый слепок поверх рабочих данных, — это
затёртые курсоры коллекторов и отчёты, то есть потеря, ради защиты от
которой бэкапы и заводились.
"""
import re
import subprocess
from pathlib import Path

import pytest

DRILL = Path(__file__).resolve().parents[1] / "backup-drill.sh"
SRC = DRILL.read_text(encoding="utf-8")

PROD_CH = "manta-clickhouse-1"
PROD_PG = "manta-postgres-1"


def _run(env: dict, args: tuple = (), timeout: int = 30):
    import os
    return subprocess.run(["bash", str(DRILL), *args],
                          env={**os.environ, **env},
                          capture_output=True, text=True, timeout=timeout)


# -- защита от записи в боевую базу ------------------------------------------------

@pytest.mark.parametrize("var,value", [
    ("CH_CONTAINER", PROD_CH),
    ("PG_CONTAINER", PROD_PG),
    ("CH_CONTAINER", "какой-то-чужой"),
])
def test_refuses_to_target_anything_but_drill_containers(var, value):
    """Переменная из чужого шелла не должна увести учения в production."""
    proc = _run({var: value})
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "ОТКАЗ" in proc.stderr


@pytest.mark.parametrize("var", ["CH_CONTAINER", "PG_CONTAINER"])
def test_allows_drill_containers(var):
    """Собственные временные имена проходят проверку.

    Иначе защита была бы бесполезной: она обязана пропускать ровно то,
    ради чего написана, — иначе её просто снимут.
    """
    proc = _run({var: "manta-drill-clickhouse",
                 "MANTA_BACKUP_DIR": "/nonexistent-on-purpose"})
    # Дальше упрётся в отсутствие слепка, но НЕ в отказ по контейнеру.
    assert "ОТКАЗ" not in proc.stderr


def test_import_is_invoked_only_against_drill_containers():
    """Статическая проверка: импорт и миграции нацелены на временные базы.

    Проверка защиты в начале скрипта охраняет ВХОДНЫЕ переменные, но не
    то, что написано ниже по тексту. Опечатка в имени контейнера у самого
    вызова import прошла бы мимо неё — и записала бы слепок в боевую
    базу. Здесь читается текст скрипта.
    """
    for call in re.finditer(r"dataset-sync\.sh import|ch-migrate\.sh|"
                            r"pg-migrate\.sh", SRC):
        # Берём строки перед вызовом: переменные окружения задаются там же.
        start = SRC.rfind("\n", 0, max(0, call.start() - 200))
        context = SRC[start:call.end()]
        if "migrate" in call.group() or "import" in call.group():
            assert PROD_CH not in context, f"боевой ClickHouse рядом с {call.group()}"
            assert PROD_PG not in context, f"боевой Postgres рядом с {call.group()}"


def test_production_names_appear_only_as_read_sources():
    """Боевые имена в скрипте есть, но только для ЧТЕНИЯ счётчиков.

    Сверять восстановленное не с чем, кроме источника, поэтому читать из
    production необходимо. Но каждое такое место обязано быть чтением:
    ни INSERT, ни import, ни миграции.
    """
    for line in SRC.splitlines():
        if PROD_CH in line or PROD_PG in line:
            assert "SELECT" in line or "src_" in line or "#" in line, (
                f"боевой контейнер вне чтения: {line.strip()}")


# -- выбор архива ------------------------------------------------------------------

def test_missing_archive_fails_clearly(tmp_path):
    proc = _run({"MANTA_BACKUP_DIR": str(tmp_path)})
    assert proc.returncode == 2
    assert "нет слепков" in proc.stderr


def test_empty_archive_is_rejected(tmp_path):
    """Пустой файл — не слепок.

    Без этой проверки учения бы «прошли» на нулевом архиве: восстановить
    из ничего нечего, счётчики сошлись бы на нулях, и мы получили бы
    зелёный отчёт о неработающем бэкапе.
    """
    empty = tmp_path / "manta-dataset-20260101T0000.tar"
    empty.touch()
    proc = _run({"MANTA_BACKUP_DIR": str(tmp_path)})
    assert proc.returncode == 2
    assert "пуст" in proc.stderr


def test_newest_archive_is_picked(tmp_path):
    """Без аргумента берётся САМЫЙ СВЕЖИЙ слепок.

    Проверять восстановление на позавчерашнем архиве значит проверять не
    то, что будешь восстанавливать.
    """
    import os
    import time
    old = tmp_path / "manta-dataset-20260101T0000.tar"
    new = tmp_path / "manta-dataset-20260817T0000.tar"
    for f in (old, new):
        f.write_bytes(b"x" * 100)
    os.utime(old, (time.time() - 86400, time.time() - 86400))

    proc = _run({"MANTA_BACKUP_DIR": str(tmp_path)}, timeout=60)
    assert str(new) in proc.stdout, proc.stdout


# -- изоляция ----------------------------------------------------------------------

def test_drill_ports_do_not_clash_with_production():
    """Учения не занимают боевые порты.

    Иначе их нельзя было бы гонять на работающей машине — а именно там
    они и нужны, потому что проверяют боевой слепок.
    """
    assert "55432" in SRC and "58123" in SRC
    for prod_port in (":5432:", ":8123:", ":9000:"):
        assert prod_port not in SRC, f"боевой порт {prod_port} в учениях"


def test_containers_are_removed_even_on_failure():
    """trap на EXIT: упавший прогон не оставляет контейнеры висеть."""
    assert re.search(r"trap\s+cleanup\s+EXIT", SRC), "нет trap на выход"
    assert "docker rm -f" in SRC


# -- вердикт не должен врать ------------------------------------------------------

def _extract_bash_func(name: str) -> str:
    """Вырезать ровно ОДНУ функцию из скрипта, чтобы проверить её код.

    Наивная регулярка «до `}` в начале строки» здесь не годится: часть
    функций однострочные и закрываются `; }` в конце строки. Она
    захватывала кусок до следующей многострочной функции — и проверка
    «есть ли --database в dst_ch» оказывалась довольна тем, что флаг
    нашёлся в соседней src_ch. Мутация, снимавшая флаг именно с dst_ch,
    из-за этого выживала.
    """
    start = SRC.index(f"{name}() {{")
    tail = SRC[start:]
    # Конец — первое из: `; }` в конце строки либо `}` в начале строки.
    ends = [m.start() for m in re.finditer(r";\s*\}\s*$", tail, re.M)]
    ends += [m.start() for m in re.finditer(r"^\}", tail, re.M)]
    assert ends, f"не найден конец функции {name}()"
    return tail[:min(ends) + 2]


def _run_check(cases: str) -> tuple[int, int, str]:
    """Прогнать настоящую check() из скрипта. Возвращает (CHECKED, FAILED, вывод)."""
    prelude = (
        "FAILED=0\nCHECKED=0\n"
        "ok() { echo \"OK $1\"; }\n"
        "bad() { echo \"FAIL $1\"; FAILED=$((FAILED + 1)); }\n"
    )
    script = prelude + _extract_bash_func("check") + "\n" + cases + \
        '\necho "CHECKED=$CHECKED FAILED=$FAILED"\n'
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    out = proc.stdout
    m = re.search(r"CHECKED=(\d+) FAILED=(\d+)", out)
    return int(m.group(1)), int(m.group(2)), out


def test_check_counts_only_real_comparisons():
    """«Источник недоступен» не считается выполненной сверкой.

    Ровно этим учения и соврали на живой машине: одиннадцать раз подряд
    сказали «сравнить не с чем» и объявили себя пройденными.
    """
    checked, failed, _ = _run_check('check "таблица" "?" "100"')
    assert checked == 0, "недоступный источник зачтён за сверку"
    assert failed == 0, "недоступный источник не должен считаться провалом"


def test_check_counts_matches_and_mismatches():
    checked, failed, out = _run_check(
        'check "совпало" "100" "100"\ncheck "разошлось" "100" "99"')
    assert checked == 2
    assert failed == 1
    assert "OK совпало" in out and "FAIL разошлось" in out


def test_empty_expected_is_not_a_comparison():
    """Пустая строка от упавшего запроса — тоже «не сравнили»."""
    checked, _, _ = _run_check('check "таблица" "" "0"')
    assert checked == 0


def test_zero_checks_cannot_be_reported_as_success():
    """Главная защита: ноль сверок — это провал, а не «пройдено».

    Проверяется текстом скрипта: у ветки успеха обязано стоять условие на
    CHECKED, иначе учения снова смогут отчитаться, ничего не проверив.
    """
    assert re.search(r'if\s+\[\s+"\$CHECKED"\s+-eq\s+0\s+\]', SRC), (
        "нет проверки «ни одной сверки не выполнено»")
    # Успех печатается ПОСЛЕ этой проверки, то есть только при CHECKED > 0.
    zero = SRC.index('"$CHECKED" -eq 0')
    success = SRC.index("УЧЕНИЯ ПРОЙДЕНЫ")
    assert zero < success, "успех печатается раньше проверки числа сверок"


def test_manifest_fields_are_actually_compared_not_just_mentioned():
    """Каждое поле манифеста уходит именно в check(), а не просто есть в файле.

    Первая версия этого теста искала названия полей в тексте скрипта — и
    пропускала мутацию, где вызов check() заменён на двоеточие: строка с
    полем оставалась на месте, тест был доволен, а сверка не выполнялась.
    Проверять надо употребление, а не присутствие.
    """
    assert "meta.json" in SRC, "манифест архива не читается вовсе"
    for field in ("matches_in_mart", "collected", "reports"):
        pattern = rf'check\s+"[^"]+"\s+"\$\(want {field}\)"'
        assert re.search(pattern, SRC), (
            f"поле манифеста {field} не передаётся в check()")


def test_missing_manifest_is_a_failure_not_a_skip():
    """Архив без манифеста — это провал сверки, а не повод её пропустить."""
    m = re.search(r'if \[ -z "\$meta" \]; then\s*\n\s*(\w+)', SRC)
    assert m, "нет ветки на отсутствующий манифест"
    assert m.group(1) == "bad", f"отсутствие манифеста обрабатывается как {m.group(1)}"


def test_failed_query_is_distinguished_from_wrong_data():
    """«Запрос не выполнился» и «пришло другое число» — разные беды.

    Первое означает сломанный запрос САМИХ учений, второе — испорченный
    бэкап. На первом живом прогоне это была именно первая беда (забытый
    --database у clickhouse-client), и сообщение «получено ?» увело бы
    искать проблему в данных, которых оно не касается.
    """
    _, failed, out = _run_check('check "витрина" "2070" "?"')
    assert failed == 1, "неудавшийся запрос обязан считаться провалом"
    assert "ЗАПРОС НЕ ВЫПОЛНИЛСЯ" in out
    assert "не бэкап виноват" in out


def test_wrong_number_says_what_came_instead():
    _, failed, out = _run_check('check "витрина" "2070" "2069"')
    assert failed == 1
    assert "получено 2069" in out
    assert "ЗАПРОС НЕ ВЫПОЛНИЛСЯ" not in out


def test_clickhouse_queries_name_the_database():
    """Таблицы живут в `manta`, а не в базе по умолчанию.

    dataset-sync.sh пишет всюду полное имя `manta.MatchTimelineFeatures`
    именно поэтому. Учения обращались коротким именем и получали пустой
    ответ на верно восстановленных данных.
    """
    for fn in ("dst_ch", "src_ch"):
        body = _extract_bash_func(fn)
        assert "--database" in body, f"{fn}() не указывает базу"


# -- живая база против слепка ------------------------------------------------------

def _run_grew(cases: str) -> tuple[int, str]:
    """Прогнать настоящую grew() из скрипта. Возвращает (LOST, вывод)."""
    prelude = "LOST=0\nRED=''\nOFF=''\n"
    script = prelude + _extract_bash_func("grew") + "\n" + cases + \
        '\necho "LOST=$LOST"\n'
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    m = re.search(r"LOST=(\d+)", proc.stdout)
    return int(m.group(1)), proc.stdout


def test_live_database_ahead_of_snapshot_is_normal():
    """Слепок старый, база с тех пор росла — это НЕ расхождение.

    Первый живой прогон сравнивал двухнедельный архив с сегодняшней базой
    как «должно совпасть» и выдал девять провалов с вердиктом
    «восстановить ровно то же нельзя» — при полностью сошедшемся
    манифесте. Вывод был ложный: база законно ушла вперёд на две недели
    сбора.
    """
    lost, out = _run_grew('grew "collectedmatches" "4407" "9312"')
    assert lost == 0, "рост живой базы зачтён за потерю"
    assert "ОТСТАЁТ" not in out


def test_live_database_behind_snapshot_is_an_alarm():
    """Обратное направление — настоящий признак потери данных.

    Живая база, где строк МЕНЬШЕ, чем в старой копии, означает, что
    что-то пропало уже после снятия слепка. Ради этого сравнение с
    источником и остаётся.
    """
    lost, out = _run_grew('grew "collectedmatches" "9312" "4407"')
    assert lost == 1
    assert "ОТСТАЁТ" in out


def test_equal_counts_are_fine():
    lost, _ = _run_grew('grew "таблица" "100" "100"')
    assert lost == 0


def test_unavailable_source_prints_nothing_at_all():
    """Недоступный источник — молчание, а не строка «ок … → ?».

    Без охраны сравнение «?» с числом просто ложно, и ветка уходит в
    «ок», печатая «слепок 100 → живая база ?». Счётчики при этом целы,
    поэтому проверять надо именно ВЫВОД: строка «ок» про несостоявшееся
    сравнение — это отчёт об успехе там, где ничего не сравнивали.
    """
    lost, out = _run_grew('grew "таблица" "100" "?"')
    assert lost == 0
    assert "ОТСТАЁТ" not in out
    assert out.strip() == "LOST=0", f"напечатано лишнее: {out!r}"


def test_source_comparison_never_fails_the_drill():
    """Сравнение с источником справочное и на вердикт не влияет.

    Вердикт выносит манифест: он отвечает на вопрос «восстанавливается ли
    архив в то, что в нём записано». Состояние живой базы к этому вопросу
    отношения не имеет.
    """
    grew_body = _extract_bash_func("grew")
    assert "FAILED" not in grew_body, "grew() трогает счётчик вердикта"
    assert "CHECKED" not in grew_body, "grew() зачитывается за сверку"
