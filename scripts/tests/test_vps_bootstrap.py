"""Тесты развёртывания на VPS (спринт 143).

Функции извлекаются из vps-bootstrap.sh и исполняются в bash со стабами —
так проверяется настоящий код скрипта, а не его пересказ. Docker, ufw и
сама VPS для этого не нужны: вся логика, где ошибка стоит дорого, — это
работа с паролями и порядок команд фаервола.

Дороже всего здесь две ошибки, и обе тихие.

Перегенерация пароля на повторном прогоне не падает: тома баз останутся
со старым паролем, конфиг получит новый, и доступ к УЖЕ СОБРАННЫМ данным
закроется. Выглядит как «база сломалась».

Расхождение двух файлов секретов тоже не падает: контейнеры поднимутся с
одним паролем, а скрипты на хосте пойдут в них с другим — бэкап и
миграции начнут отвечать «пароль неверен» при живой базе.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "vps-bootstrap.sh"


def _functions(*names: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    out = []
    for name in names:
        m = re.search(rf"^{name}\(\) \{{.*?^\}}", src, re.M | re.S)
        assert m, f"функция {name} не найдена в vps-bootstrap.sh"
        out.append(m.group(0))
    return "\n\n".join(out)


def run_secrets(tmp_path, check_only="0", env_compose_body=None,
                env_host_body=None):
    """Прогнать setup_secrets в песочнице. Возвращает (rc, вывод, файлы)."""
    env_compose = tmp_path / "compose.env"
    env_host = tmp_path / "host.env"
    if env_compose_body is not None:
        env_compose.write_text(env_compose_body, encoding="utf-8")
    if env_host_body is not None:
        env_host.write_text(env_host_body, encoding="utf-8")

    harness = f"""
set -uo pipefail
ENV_COMPOSE="{env_compose}"
ENV_HOST="{env_host}"
CHECK_ONLY="{check_only}"
say()  {{ printf '\\n>> %s\\n' "$1"; }}
ok()   {{ printf '   OK   %s\\n' "$1"; }}
warn() {{ printf '   ВНИМАНИЕ %s\\n' "$1"; }}
die()  {{ printf 'ОСТАНОВ: %s\\n' "$1" >&2; exit 1; }}
{_functions("gen_password", "setup_secrets")}
setup_secrets
"""
    proc = subprocess.run(["bash", "-c", harness], capture_output=True,
                          text=True)
    return (proc.returncode, proc.stdout + proc.stderr,
            env_compose.read_text(encoding="utf-8") if env_compose.exists() else "",
            env_host.read_text(encoding="utf-8") if env_host.exists() else "")


def password_of(body: str, var: str = "MANTA_DB_PASSWORD") -> str:
    m = re.search(rf"^{var}=(.+)$", body, re.M)
    return m.group(1) if m else ""


# -- пароль генерируется один раз ------------------------------------------------

def test_password_is_generated_on_first_run(tmp_path):
    rc, out, compose, host = run_secrets(tmp_path)
    assert rc == 0, out
    assert len(password_of(compose)) >= 16, "пароль слишком короткий"


def test_second_run_does_not_regenerate_the_password(tmp_path):
    """САМОЕ важное свойство скрипта.

    Тома Postgres и ClickHouse созданы с первым паролем. Сгенерируй
    второй — контейнеры перестанут пускать к уже собранным данным, и
    выглядеть это будет как «база сломалась», а не как «скрипт
    перезаписал секрет».
    """
    rc, _, first, _ = run_secrets(tmp_path)
    assert rc == 0
    rc, out, second, _ = run_secrets(tmp_path)
    assert rc == 0, out
    assert password_of(first) == password_of(second)
    assert "не перегенерирую" in out


def test_existing_password_survives_even_if_file_is_odd(tmp_path):
    """Чужой формат файла — не повод считать пароль отсутствующим.

    Файл мог быть отредактирован руками. Пока в нём есть строка с
    паролем, скрипт обязан оставить её в покое.
    """
    body = "# мои заметки\nMANTA_DB_PASSWORD=руками-вписанный-пароль\nПРОЧЕЕ=1\n"
    rc, out, compose, _ = run_secrets(tmp_path, env_compose_body=body)
    assert rc == 0
    assert password_of(compose) == "руками-вписанный-пароль"


# -- два файла, один пароль ------------------------------------------------------

def test_both_files_get_the_same_password(tmp_path):
    """compose и скрипты на хосте обязаны знать один пароль.

    Разъехавшись, они дают не отказ, а «пароль неверен» из бэкапа при
    живом контейнере — диагноз, на который уходит вечер.
    """
    rc, out, compose, host = run_secrets(tmp_path)
    assert rc == 0
    pw = password_of(compose)
    assert password_of(host, "CLICKHOUSE_PASSWORD") == pw
    assert password_of(host, "POSTGRES_PASSWORD") == pw


def test_mismatch_in_host_file_is_reported_not_silently_fixed(tmp_path):
    """Уже заданный чужой пароль не переписывается молча.

    Значение могло быть выставлено осознанно. Молча заменить его — значит
    сломать то, что работало; поэтому скрипт предупреждает и оставляет
    решение человеку.
    """
    rc, out, compose, host = run_secrets(
        tmp_path, env_host_body="CLICKHOUSE_PASSWORD=свой-пароль\n")
    assert rc == 0
    assert "не совпадает" in out
    assert "CLICKHOUSE_PASSWORD=свой-пароль" in host


def test_host_file_is_not_duplicated_on_rerun(tmp_path):
    """Повторный прогон не плодит строки.

    Файл читается через `set -a; .` — дубли не сломают его, но растущий
    на каждый прогон файл секретов рано или поздно скроет опечатку.
    """
    run_secrets(tmp_path)
    rc, out, _, host = run_secrets(tmp_path)
    assert rc == 0
    assert host.count("CLICKHOUSE_PASSWORD=") == 1


# -- секреты не печатаются -------------------------------------------------------

def test_password_never_appears_in_output(tmp_path):
    """Вывод скрипта уходит в терминал, логи cron и скриншоты.

    В этом проекте секреты не печатаются нигде и никогда — правило
    старше этого скрипта.
    """
    rc, out, compose, _ = run_secrets(tmp_path)
    pw = password_of(compose)
    assert pw and pw not in out


def test_check_mode_writes_nothing(tmp_path):
    """--check только смотрит.

    Иначе «просто проверю» на работающей машине сгенерировало бы новый
    пароль и закрыло бы доступ к данным.
    """
    rc, out, compose, host = run_secrets(tmp_path, check_only="1")
    assert rc == 0
    assert compose == "" and host == ""


# -- права -----------------------------------------------------------------------

def test_secrets_file_is_not_world_readable(tmp_path):
    run_secrets(tmp_path)
    mode = (tmp_path / "compose.env").stat().st_mode & 0o077
    assert mode == 0, "файл секретов доступен посторонним"


# -- фаервол ---------------------------------------------------------------------

def test_ssh_is_allowed_before_the_firewall_is_enabled():
    """Порядок команд ufw — вопрос доступа к машине.

    Включить фаервол раньше, чем разрешён SSH, значит отрезать себя от
    VPS: чинить придётся через консоль хостера, и заметить это можно
    только по оборвавшейся сессии.
    """
    src = _functions("setup_firewall")
    allow = src.index("ufw allow OpenSSH")
    enable = src.index("ufw --force enable")
    assert allow < enable, "ufw включается раньше, чем разрешён SSH"


# -- согласованность с compose ---------------------------------------------------

def test_compose_reads_the_same_variable_the_script_writes():
    """Имя переменной — контракт между скриптом и compose-файлом.

    Скрипт пишет MANTA_DB_PASSWORD, compose его подставляет. Переименуй
    одно — и стек молча поднимется с дефолтным паролем из репозитория,
    выглядя при этом совершенно исправным.
    """
    compose = (SCRIPT.parents[1] / "deployments" / "docker-compose.yml"
               ).read_text(encoding="utf-8")
    assert "${MANTA_DB_PASSWORD:-" in compose
    assert "MANTA_DB_PASSWORD=" in SCRIPT.read_text(encoding="utf-8")


def test_dev_default_is_preserved_for_the_home_machine():
    """Дома ничего не меняется: без переменной подставляется дев-пароль.

    Иначе правка ради VPS сломала бы `make up` на домашней машине, где
    никакого deployments/.env нет.
    """
    compose = (SCRIPT.parents[1] / "deployments" / "docker-compose.yml"
               ).read_text(encoding="utf-8")
    for m in re.finditer(r"\$\{MANTA_DB_PASSWORD(:-)?([^}]*)\}", compose):
        assert m.group(1) == ":-", "подстановка без умолчания сломает домашний стек"
        assert m.group(2) == "dota_dev_password"
