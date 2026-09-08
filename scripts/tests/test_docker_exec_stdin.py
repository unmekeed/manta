"""Труба в `docker exec` требует `-i` (спринт 206).

ЧТО ЛОВИТСЯ. `docker exec` БЕЗ `-i` не отдаёт процессу внутри контейнера
стандартный ввод. Команда, читающая stdin, получает EOF сразу, НИЧЕГО не
делает и выходит с НУЛЁМ. Труба снаружи тоже не жалуется: маленькая
запись помещается в буфер и уходит в никуда.

Отказ поэтому невидим по всем обычным признакам: код возврата нулевой,
stderr пуст, лог молчит. Видно только следствие — работа не сделана.

ЭТО НЕ УМОЗРИТЕЛЬНО. Ровно так `make smoke` (спринт 203) две попытки
подряд сообщал «отчёт не появился за 300с» при полностью исправном
конвейере: событие не публиковалось вовсе, лаг консьюмер-группы был
нулевым (у непроизведённого сообщения он такой же), а генератор в те же
минуты производил отчёты по другим матчам. Диагноз уводил в сторону тем
вернее, чем внимательнее его читали.

ПОЧЕМУ ОТДЕЛЬНЫМ ПРАВИЛОМ, А НЕ ПРАВКОЙ ОДНОГО МЕСТА. Забыть `-i`
ничего не стоит: 34 из 36 обращений к `docker exec` в scripts/ ввод не
читают, и `-i` им не нужен, — то есть форма без него привычна глазу и
переносится копированием в то место, где ввод читают.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def shell_files() -> list[Path]:
    return sorted(p for p in SCRIPTS.rglob("*.sh") if p.is_file())


def docker_vars(src: str) -> dict[str, bool]:
    """NAME → отдаётся ли stdin, для переменных вида NAME="docker exec …".

    Обращение через переменную (`$KAFKA_BIN/kafka-topics.sh`) — обычный
    здесь способ записи, и правило, смотрящее только на дословное
    `docker exec`, прошло бы мимо как раз того случая, который случился.
    """
    out = {}
    for name, body in re.findall(r'^\s*([A-Z_][A-Z0-9_]*)="(docker exec [^"]*)"',
                                 src, re.M):
        out[name] = " -i" in f" {body} " or " -it" in f" {body} "
    return out


def offenders() -> list[str]:
    """Строки, где в docker exec без -i уходит труба или heredoc."""
    bad = []
    for path in shell_files():
        src = path.read_text(encoding="utf-8")
        known = docker_vars(src)
        for n, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            # Кого кормят: дословный docker exec справа от трубы либо
            # переменная, в которой он записан.
            fed = re.findall(r"\|\s*(?:\$\{?([A-Z_][A-Z0-9_]*)\}?|(docker exec\b))",
                             code)
            for var, literal in fed:
                if literal:
                    interactive = " -i " in code.split("docker exec", 1)[1] + " "
                elif var in known:
                    interactive = known[var]
                else:
                    continue
                if not interactive:
                    bad.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    return bad


def test_the_scan_sees_the_scripts():
    """Страховка от проверки пустоты.

    Сломайся сбор файлов — правило ниже прошло бы на пустом списке. Этот
    проект уже ловил такое на себе не раз.
    """
    assert len(shell_files()) >= 10, shell_files()
    joined = "\n".join(p.read_text(encoding="utf-8") for p in shell_files())
    assert "docker exec" in joined, "разбор не нашёл ни одного docker exec"


def test_a_variable_holding_docker_exec_is_recognised():
    """Разбор понимает обращение через переменную, а не только дословное.

    Именно в такой форме отказ и жил: `KAFKA_BIN="docker exec …"`, а на
    месте использования слова `docker` не видно вовсе.
    """
    got = docker_vars('KAFKA_BIN="docker exec c /opt/kafka/bin"\n'
                      'KAFKA_IN="docker exec -i c /opt/kafka/bin"\n')
    assert got == {"KAFKA_BIN": False, "KAFKA_IN": True}, got


def test_nothing_is_piped_into_a_docker_exec_without_stdin():
    """ГЛАВНОЕ: команда, которую кормят с трубы, получает стандартный ввод.

    Без `-i` она читает EOF, не делает ничего и возвращает ноль — то есть
    отказывает МОЛЧА и выглядит успехом.
    """
    bad = offenders()
    assert not bad, (
        "в docker exec без -i уходит труба: команда прочитает EOF, ничего "
        "не сделает и вернёт ноль.\n" + "\n".join(bad))
