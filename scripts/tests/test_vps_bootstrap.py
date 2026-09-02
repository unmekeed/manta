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
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "vps-bootstrap.sh"


def _var(name: str) -> str:
    """Присваивание верхнего уровня из скрипта — целиком, вместе с именем.

    Записать значение в тест копией значило бы завести вторую точку
    правды: маркер cron сменится в скрипте, тест останется зелёным на
    старом, а на машине рядом с новым блоком будет жить старый.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{name}=.*$", src, re.M)
    assert m, f"переменная {name} не найдена в vps-bootstrap.sh"
    return m.group(0)


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


SERVICE_PASSWORDS = (
    "MANTA_DB_PASS_COLLECTOR", "MANTA_DB_PASS_REPORTS",
    "MANTA_DB_PASS_GATEWAY", "MANTA_DB_PASS_RO",
    "MANTA_CH_PASS_READER", "MANTA_CH_PASS_WRITER",
    "MANTA_CH_PASS_TRAINER", "MANTA_REDIS_PASSWORD",
)


def test_service_passwords_are_generated_and_distinct(tmp_path):
    rc, out, compose, _ = run_secrets(tmp_path)
    assert rc == 0, out
    values = [password_of(compose, name) for name in SERVICE_PASSWORDS]
    assert all(len(value) >= 16 for value in values)
    assert len(set(values)) == len(values)
    assert password_of(compose) not in values


def test_service_passwords_survive_rerun(tmp_path):
    rc, _, first, _ = run_secrets(tmp_path)
    assert rc == 0
    rc, out, second, _ = run_secrets(tmp_path, env_compose_body=first)
    assert rc == 0, out
    for name in SERVICE_PASSWORDS:
        assert password_of(first, name) == password_of(second, name)


def test_old_vps_gets_only_missing_service_passwords(tmp_path):
    body = "MANTA_DB_PASSWORD=старый-root\nMANTA_DB_PASS_COLLECTOR=старый-collector\n"
    rc, out, compose, _ = run_secrets(tmp_path, env_compose_body=body)
    assert rc == 0, out
    assert password_of(compose) == "старый-root"
    assert password_of(compose, "MANTA_DB_PASS_COLLECTOR") == "старый-collector"
    assert all(password_of(compose, name) for name in SERVICE_PASSWORDS)


def test_gateway_keys_dir_is_added_once_and_preserved(tmp_path):
    rc, out, first, _ = run_secrets(tmp_path)
    assert rc == 0, out
    path = password_of(first, "MANTA_KEYS_DIR")
    assert path.endswith("/manta-keys")
    rc, out, second, _ = run_secrets(tmp_path, env_compose_body=first)
    assert rc == 0, out
    assert password_of(second, "MANTA_KEYS_DIR") == path
    assert second.count("MANTA_KEYS_DIR=") == 1


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
    for name in SERVICE_PASSWORDS:
        assert password_of(compose, name) not in out


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


# -- каждый раздел проверки говорит ----------------------------------------------

def run_check_section(tmp_path, func, with_docker: bool, running=0):
    """Прогнать раздел проверки со стабом docker (или без него)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # PATH состоит ТОЛЬКО из песочницы: иначе «docker не установлен»
    # проверялось бы на машине, где docker установлен, и тест смотрел бы
    # не на ту ветку. Нужные утилиты добавляем ссылками поимённо.
    for tool in ("grep", "bash"):
        link = bin_dir / tool
        if not link.exists():
            link.symlink_to(shutil.which(tool))
    if with_docker:
        names = "\n".join(f"manta-{i}" for i in range(running))
        (bin_dir / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'if [ "$1" = "compose" ]; then printf "%s" "{names}"; fi\n'
            "exit 0\n", encoding="utf-8")
        (bin_dir / "docker").chmod(0o755)
    harness = f"""
set -uo pipefail
export PATH="{bin_dir}"
CHECK_ONLY=1
COMPOSE="docker compose"
say()  {{ printf '\\n>> %s\\n' "$1"; }}
ok()   {{ printf '   OK   %s\\n' "$1"; }}
warn() {{ printf '   ВНИМАНИЕ %s\\n' "$1"; }}
die()  {{ printf 'ОСТАНОВ: %s\\n' "$1" >&2; exit 1; }}
{_functions(func)}
{func}
"""
    proc = subprocess.run([shutil.which("bash"), "-c", harness],
                          capture_output=True, text=True,
                          env={"PATH": str(bin_dir)})
    return proc.stdout + proc.stderr


def test_stack_check_speaks_when_docker_is_absent(tmp_path):
    """Раздел «стек» обязан сказать, что проверить нечем.

    Молчащий раздел на чистой машине выглядит единственным пунктом БЕЗ
    замечаний — то есть исправным, хотя не проверено вообще ничего. В
    этом проекте неотличимая от успеха тишина уже стоила тринадцати дней
    без бэкапов.
    """
    out = run_check_section(tmp_path, "start_stack", with_docker=False)
    assert out.strip(), "раздел промолчал"
    assert "ВНИМАНИЕ" in out and "docker" in out


def test_stack_check_reports_absent_containers(tmp_path):
    """Docker есть, контейнеров нет — это тоже замечание, а не тишина."""
    out = run_check_section(tmp_path, "start_stack", with_docker=True, running=0)
    assert "ВНИМАНИЕ" in out


def test_stack_check_reports_running_containers(tmp_path):
    out = run_check_section(tmp_path, "start_stack", with_docker=True, running=7)
    assert "OK" in out and "7" in out


REPO_FAKE = "/tmp/manta"
CRON_BACKUP = f"30 3 * * * cd {REPO_FAKE} && ./scripts/backup.sh"
CRON_PEER = f"0 4 * * * cd {REPO_FAKE} && ./scripts/peer-sync.sh"
CRON_BEAT = f"0 9 * * * cd {REPO_FAKE} && ./scripts/heartbeat.sh"
CRON_SALTS = f"17 * * * * cd {REPO_FAKE} && ./scripts/gc-salts.sh"


def run_cron(tmp_path, *, existing="", peers="", check_only="0"):
    """Прогнать setup_cron со стабом crontab. Возвращает (вывод, расписание).

    Стаб хранит расписание в файле: `crontab -l` читает, `crontab -`
    перезаписывает. Так проверяется не намерение скрипта, а то, что в
    итоге оказалось у машины, — разница, на которой этот проект уже
    обжигался (файл `sshd_config.d` был записан, а настройка не
    применилась).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("grep", "bash", "cat"):
        link = bin_dir / tool
        if not link.exists():
            link.symlink_to(shutil.which(tool))
    cronfile = tmp_path / "crontab.txt"
    cronfile.write_text(existing, encoding="utf-8")
    (bin_dir / "crontab").write_text(
        "#!/usr/bin/env bash\n"
        f'F="{cronfile}"\n'
        'if [ "$1" = "-l" ]; then cat "$F"; exit 0; fi\n'
        'cat > "$F"\n', encoding="utf-8")
    (bin_dir / "crontab").chmod(0o755)

    harness = f"""
set -uo pipefail
export PATH="{bin_dir}"
CHECK_ONLY={check_only}
REPO={REPO_FAKE}
MANTA_PEER_HOSTS={shlex.quote(peers)}
say()  {{ printf '\\n>> %s\\n' "$1"; }}
ok()   {{ printf '   OK   %s\\n' "$1"; }}
warn() {{ printf '   ВНИМАНИЕ %s\\n' "$1"; }}
load_host_settings() {{ :; }}
{_var("CRON_MARKER")}
{_functions("cron_wanted", "cron_mine", "cron_foreign", "setup_cron")}
setup_cron
"""
    proc = subprocess.run([shutil.which("bash"), "-c", harness],
                          capture_output=True, text=True,
                          env={"PATH": str(bin_dir)})
    return proc.stdout + proc.stderr, cronfile.read_text(encoding="utf-8")


def test_cron_check_speaks_when_already_configured(tmp_path):
    """Настроенный cron — тоже повод сказать, а не промолчать.

    Ветка «уже настроено» самая частая: её видят на каждом повторном
    прогоне. Промолчи она — раздел выглядел бы непроверенным.
    """
    marker = _var("CRON_MARKER").split("=", 1)[1].strip('"')
    out, _ = run_cron(
        tmp_path, check_only="1",
        existing=f"{marker}\n{CRON_BACKUP}\n{CRON_BEAT}\n{CRON_SALTS}\n")
    assert "OK" in out, out


def test_single_machine_gets_no_exchange_job(tmp_path):
    """ГЛАВНОЕ утверждение спринта.

    Обмен нужен двум собирающим машинам. На одиночной он падал бы каждую
    ночь в 04:00 и слал бы красный алерт в Telegram — ложную тревогу по
    расписанию, которая учит не читать канал.
    """
    out, cron = run_cron(tmp_path, peers="")
    assert CRON_BACKUP in cron and CRON_BEAT in cron
    assert "peer-sync" not in cron, cron
    assert "обмена нет" in out


def test_peers_bring_the_exchange_job_back(tmp_path):
    """Обратная сторона: с соседями обмен обязан появиться.

    Без этой проверки мутация «никогда не ставить обмен» прошла бы мимо, и
    вторая машина молча не получала бы чужие слепки.
    """
    out, cron = run_cron(tmp_path, peers="pc2")
    assert CRON_PEER in cron, cron


def test_existing_schedule_is_repaired_not_left_alone(tmp_path):
    """Правка обязана доехать до УЖЕ настроенной машины.

    Прежняя версия выходила по первому совпадению с маркером: расписание,
    заведённое старым скриптом, оставалось прежним навсегда. Правки
    доезжали до чистых машин и не доезжали до работающих — ровно та беда,
    которую в спринтах 159–161 чинили тремя отдельными коммитами уже на
    живой VPS.

    Здесь на машине лежит старое расписание С обменом, а соседей больше
    нет. Строка обязана исчезнуть.
    """
    marker = _var("CRON_MARKER").split("=", 1)[1].strip('"')
    old = f"{marker}\n{CRON_BACKUP}\n{CRON_PEER}\n{CRON_BEAT}\n"
    out, cron = run_cron(tmp_path, existing=old, peers="")
    assert "peer-sync" not in cron, cron
    assert CRON_BACKUP in cron and CRON_BEAT in cron


def test_foreign_cron_lines_survive(tmp_path):
    """Чужие задачи в crontab не наши, и трогать их нельзя.

    Скрипт переписывает crontab целиком — значит обязан вернуть на место
    всё, что писал не он. Потерять чужую строку молча легко, а заметить
    пропажу можно и через месяц.
    """
    foreign = "0 5 * * * /usr/local/bin/чужая-задача"
    out, cron = run_cron(tmp_path, existing=f"{foreign}\n", peers="")
    assert foreign in cron, cron


def test_cron_setup_is_idempotent(tmp_path):
    """Второй прогон ничего не меняет и говорит «уже настроено».

    Скрипт задуман идемпотентным, и расписание — то место, где нарушение
    заметно не сразу: дубли строк множат ночные задачи.
    """
    out1, cron1 = run_cron(tmp_path, peers="pc2")
    cronfile = tmp_path / "crontab.txt"
    out2, cron2 = run_cron(tmp_path, existing=cron1, peers="pc2")
    assert cron1 == cron2, f"{cron1!r} != {cron2!r}"
    assert "уже настроен" in out2
    assert cron2.count("peer-sync") == 1


def test_no_check_section_is_completely_mute(tmp_path):
    """Пол, а не потолок: раздел обязан УМЕТЬ говорить.

    Тест намеренно слабый, и важно понимать, чего он НЕ проверяет: что
    говорит каждая ветка. Раздел с тремя путями, из которых молчит один,
    он пропустит — такие ветки закрываются по отдельности, прогоном со
    стабами. Здесь ловится другое: раздел, добавленный без единого
    сообщения вообще.

    Список разделов берётся из самого скрипта: записанный руками, он
    разошёлся бы при добавлении нового, и новый раздел оказался бы
    единственным непроверенным.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    sections = re.findall(r"^(setup_\w+|start_stack)\(\) \{", src, re.M)
    assert len(sections) >= 5, f"разделов подозрительно мало: {sections}"
    for name in sections:
        body = _functions(name)
        assert re.search(r'\b(ok|warn)\s+"', body), \
            f"раздел {name} может завершиться, ничего не сказав"


# -- сборка образов --------------------------------------------------------------

def test_build_list_is_derived_not_hardcoded():
    """Список собираемых сервисов НЕ записан в скрипте.

    Записанный руками, он разошёлся бы с compose при добавлении сервиса,
    и новый молча не собирался бы: `up` подтянул бы для него старый образ
    или упал бы на отсутствующем — но уже в другом месте и с другой
    ошибкой.
    """
    src = _functions("build_images")
    for name in ("api-gateway", "data-collector", "feature-extractor",
                 "parser-svc", "ml-service", "frontend"):
        assert name not in src, f"имя сервиса {name} зашито в скрипт"
    assert "config --format json" in src, "список не берётся из compose"


def test_build_is_sequential_not_parallel():
    """Сборка идёт ПО ОДНОМУ сервису.

    `compose up --build` собирает восемь образов разом: три pip install,
    npm ci, две сборки Go и компиляция C++-ядра одновременно. На
    двухъядерном VPS это не только медленно — при падении одной сборки
    остальные получают CANCELED, и настоящая ошибка тонет в двухстах
    строках отменённых шагов. Первый живой прогон закончился именно так.
    """
    src = _functions("build_images", "start_stack")
    assert "up -d --build" not in src, "сборка снова идёт вместе с up"
    assert 'build "$svc"' in src, "сборка не разбита по сервисам"


def test_failed_build_names_the_service_and_keeps_the_log():
    """При сбое названо, ЧТО не собралось, и сказано, где полный вывод.

    Иначе повторяется первый прогон: код возврата 1 и ни слова о том,
    какой сервис и почему.
    """
    src = _functions("build_images")
    assert 'failed="$failed $svc"' in src
    # Проверяем именно СООБЩЕНИЕ об остановке, а не наличие $log где-то в
    # функции: путь к логу упоминается в ней трижды по другим поводам, и
    # проверка «$log встречается» проходила бы даже с обезличенным
    # «сборка не удалась».
    die_lines = [l for l in src.splitlines() if "die " in l and "failed" in l]
    assert die_lines, "нет строки остановки при сбое сборки"
    assert any("$log" in l for l in die_lines), \
        f"в сообщении о сбое не сказано, где полный вывод: {die_lines}"


def test_every_buildable_service_of_compose_is_covered():
    """Проверка контракта: то, что compose помечает build, скрипт собирает.

    Тест читает compose так же, как это делает скрипт, — если однажды
    формат вывода `config --format json` изменится, упадёт и тест, а не
    только установка на чужой машине.
    """
    import json
    compose = SCRIPT.parents[1] / "deployments" / "docker-compose.yml"
    proc = subprocess.run(
        ["docker", "compose", "-f", str(compose), "--profile", "apps",
         "config", "--format", "json"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("docker compose недоступен")
    services = json.loads(proc.stdout).get("services", {})
    buildable = [n for n, s in services.items() if "build" in s]
    assert len(buildable) >= 6, f"собираемых сервисов подозрительно мало: {buildable}"


# -- предполётная проверка портов ------------------------------------------------

def run_check_ports(tmp_path, listening_lines, overlay=None):
    """Прогнать check_ports со стабом ss. Возвращает (rc, вывод)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("grep", "bash", "sort", "tr", "sed", "head", "printf", "echo"):
        w = shutil.which(tool)
        link = bin_dir / tool
        if w and not link.exists():
            link.symlink_to(w)
    # printf, а не cat: PATH в песочнице состоит из считанных ссылок, и
    # внешний cat туда не входит. Молча пустой вывод стаба выглядел бы
    # как «все порты свободны» — то есть тест проверял бы не ту ветку.
    body = "".join(f"printf '%s\\n' {shlex.quote(l)}\n" for l in listening_lines)
    (bin_dir / "ss").write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    (bin_dir / "ss").chmod(0o755)

    # Наложение копируем: функция читает его по относительному пути.
    deploy = tmp_path / "deployments"
    deploy.mkdir(exist_ok=True)
    src = overlay if overlay is not None else (
        SCRIPT.parents[1] / "deployments" / "docker-compose.vps.yml"
    ).read_text(encoding="utf-8")
    (deploy / "docker-compose.vps.yml").write_text(src, encoding="utf-8")

    harness = f"""
set -uo pipefail
export PATH="{bin_dir}"
say()  {{ printf '\\n>> %s\\n' "$1"; }}
ok()   {{ printf '   OK   %s\\n' "$1"; }}
warn() {{ printf '   ВНИМАНИЕ %s\\n' "$1"; }}
die()  {{ printf 'ОСТАНОВ: %s\\n' "$1" >&2; exit 1; }}
{_functions("check_ports")}
check_ports
"""
    proc = subprocess.run([shutil.which("bash"), "-c", harness],
                          capture_output=True, text=True, cwd=tmp_path,
                          env={"PATH": str(bin_dir)})
    return proc.returncode, proc.stdout + proc.stderr


FREE = ["LISTEN 0 128 127.0.0.1:22 0.0.0.0:* users:((\"sshd\",pid=1,fd=3))"]
PG_BUSY = FREE + [
    'LISTEN 0 244 127.0.0.1:5432 0.0.0.0:* users:(("postgres",pid=900,fd=6))']


def test_free_ports_pass(tmp_path):
    rc, out = run_check_ports(tmp_path, FREE)
    assert rc == 0, out
    assert "13" in out, out


def test_busy_port_stops_before_anything_starts(tmp_path):
    """Конфликт портов — остановка ДО запуска, а не посреди него.

    Первое живое развёртывание встало на `failed to bind host port
    127.0.0.1:5432` уже после того, как половина контейнеров была
    создана. Из такой ошибки видно один порт из тринадцати и неизвестно,
    кто его занял; после исправления всё повторяется на следующем.
    """
    rc, out = run_check_ports(tmp_path, PG_BUSY)
    assert rc != 0, out
    assert "5432" in out
    assert "ничего не сломано" in out


def test_busy_port_names_the_process(tmp_path):
    """Названо не только «занят», но и КЕМ.

    Без имени процесса диагноз упирается в отдельный поход за ss, а на
    чужой машине это лишний круг.

    Проверяется ИМЕННО строка предупреждения. Первая версия искала
    «postgres» во всём выводе и проходила даже без определения процесса:
    слово встречалось в тексте подсказки («systemctl disable --now
    postgresql»). Тест зеленел, ничего не проверяя.
    """
    rc, out = run_check_ports(tmp_path, PG_BUSY)
    warns = [l for l in out.splitlines() if "занят" in l]
    assert warns, out
    assert any("postgres" in l for l in warns), warns


@pytest.mark.parametrize("line,why", [
    ('LISTEN 0 244 127.0.0.1:15432 0.0.0.0:* users:(("нечто",pid=1,fd=6))',
     "похожий номер порта"),
    ('LISTEN 0 244 10.0.0.5:5432 0.0.0.0:* users:(("postgres",pid=9,fd=6))',
     "тот же порт на ДРУГОМ интерфейсе"),
])
def test_not_a_false_positive(tmp_path, line, why):
    """Ложная тревога хуже отсутствия проверки: та хотя бы не врёт.

    Два случая, и второй важнее. Служба, слушающая 10.0.0.5:5432, НЕ
    мешает докеру занять 127.0.0.1:5432 — адреса разные. Наивный поиск
    подстроки «:5432» запретил бы установку на ровном месте, и человек
    пошёл бы выключать нужную ему службу.
    """
    rc, out = run_check_ports(tmp_path, FREE + [line])
    assert rc == 0, f"{why}: ложное срабатывание\n{out}"


def test_port_list_comes_from_the_overlay_not_the_script(tmp_path):
    """Список портов читается из наложения.

    Записанный в скрипте, он разошёлся бы при добавлении сервиса, и новый
    порт проверялся бы «на живую», то есть никак.
    """
    src = _functions("check_ports")
    assert "docker-compose.vps.yml" in src
    for hard in ("5432", "8123", "9092"):
        assert f'"{hard}"' not in src, f"порт {hard} зашит в скрипт"

    # И проверка на деле: наложение с одним портом даёт один порт.
    rc, out = run_check_ports(
        tmp_path, FREE,
        overlay='services:\n  x:\n    ports:\n      - "127.0.0.1:7777:7777"\n')
    assert rc == 0
    assert " 1 " in out or "все 1 " in out, out


def test_port_check_runs_before_the_stack_starts():
    """check_ports вызывается, и вызывается ДО compose up.

    Функция может быть безупречной и не вызываться ни разу — тогда
    конфликт снова вскроется посреди запуска, когда половина контейнеров
    уже создана.
    """
    src = _functions("start_stack")
    assert "check_ports" in src, "предполётная проверка портов не вызывается"
    assert src.index("check_ports") < src.index("up -d"), \
        "проверка портов идёт после подъёма стека"


# -- свои порты не считаются конфликтом ------------------------------------------

def run_check_ports_with_compose(tmp_path, listening_lines, our_ports):
    """Прогнать check_ports со стабами ss И docker compose ps."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("grep", "bash", "sort", "tr", "sed", "head", "python3"):
        w = shutil.which(tool)
        link = bin_dir / tool
        if w and not link.exists():
            link.symlink_to(w)
    body = "".join(f"printf '%s\\n' {shlex.quote(l)}\n" for l in listening_lines)
    (bin_dir / "ss").write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    (bin_dir / "ss").chmod(0o755)

    pubs = ",".join('{"PublishedPort":%s,"URL":"127.0.0.1"}' % p for p in our_ports)
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f"""printf '%s\\n' '{{"Name":"manta-x","Publishers":[{pubs}]}}'\n""",
        encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)

    deploy = tmp_path / "deployments"
    deploy.mkdir(exist_ok=True)
    (deploy / "docker-compose.vps.yml").write_text(
        (SCRIPT.parents[1] / "deployments" / "docker-compose.vps.yml"
         ).read_text(encoding="utf-8"), encoding="utf-8")

    harness = f"""
set -uo pipefail
export PATH="{bin_dir}"
COMPOSE="docker compose"
say()  {{ printf '\\n>> %s\\n' "$1"; }}
ok()   {{ printf '   OK   %s\\n' "$1"; }}
warn() {{ printf '   ВНИМАНИЕ %s\\n' "$1"; }}
die()  {{ printf 'ОСТАНОВ: %s\\n' "$1" >&2; exit 1; }}
{_functions("check_ports")}
check_ports
"""
    proc = subprocess.run([shutil.which("bash"), "-c", harness],
                          capture_output=True, text=True, cwd=tmp_path,
                          env={"PATH": str(bin_dir)})
    return proc.returncode, proc.stdout + proc.stderr


DOCKER_PROXY = [
    'LISTEN 0 4096 127.0.0.1:%d 0.0.0.0:* users:(("docker-proxy",pid=1,fd=4))' % p
    for p in (3000, 5432, 6379, 8080, 8123, 9000, 9092, 9500, 9501, 9600)]


def test_own_running_stack_is_not_a_conflict(tmp_path):
    """Порты, которые держит НАШ же стек, конфликтом не считаются.

    Скрипт идемпотентен: его гоняют повторно после сбоя и после git pull.
    На машине с уже поднятым стеком docker-proxy честно держит все порты
    — это желаемое состояние, а не помеха. Первая версия проверки об этом
    не знала и отказывалась работать на успешно развёрнутой машине.

    Ложная тревога хуже отсутствия проверки: она учит игнорировать себя.
    """
    ours = [3000, 5432, 6379, 8080, 8123, 9000, 9092, 9500, 9501, 9600]
    rc, out = run_check_ports_with_compose(tmp_path, DOCKER_PROXY, ours)
    assert rc == 0, out
    assert "ОСТАНОВ" not in out


def test_foreign_process_is_still_a_conflict(tmp_path):
    """Чужой процесс на нашем порту остаётся конфликтом.

    Послабление ради идемпотентности не должно превратить проверку в
    декорацию: порт, которого нет среди опубликованных нашим стеком,
    по-прежнему обязан останавливать установку.
    """
    rc, out = run_check_ports_with_compose(
        tmp_path,
        ['LISTEN 0 244 127.0.0.1:5432 0.0.0.0:* users:(("postgres",pid=9,fd=6))'],
        our_ports=[3000])          # 5432 наш стек НЕ публикует
    assert rc != 0, out
    assert "5432" in out


def test_own_port_match_is_exact_not_substring(tmp_path):
    """«Свой» порт сравнивается ЦЕЛИКОМ, а не как подстрока.

    Сегодня среди тринадцати наших портов ни один не является подстрокой
    другого, поэтому неточное сравнение ничего не ломает — и мутация,
    заменяющая `grep -qx` на `grep -q`, проходит незамеченной. Это
    свойство СПИСКА, а не кода: добавь порт 500 рядом с 9500 или 3 рядом
    с 3000 — и чужой процесс начнёт молча считаться своим.

    Здесь проверяется само сравнение: стек «публикует» 15432, а чужой
    процесс сидит на 5432. При поиске подстроки 5432 нашлось бы внутри
    15432, и конфликт был бы пропущен.
    """
    rc, out = run_check_ports_with_compose(
        tmp_path,
        ['LISTEN 0 244 127.0.0.1:5432 0.0.0.0:* users:(("чужой",pid=9,fd=6))'],
        our_ports=[15432])
    assert rc != 0, f"чужой порт принят за свой по подстроке:\n{out}"
    assert "5432" in out


# -- контейнеры действительно работают -------------------------------------------

def run_verify(tmp_path, containers):
    """Прогнать verify_running со стабом docker.

    containers: [(имя, состояние, число перезапусков)].
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("grep", "bash", "sed", "sleep"):
        w = shutil.which(tool)
        link = bin_dir / tool
        if w and not link.exists():
            link.symlink_to(w)

    names = "\n".join(n for n, _, _ in containers)
    # docker inspect -f ФОРМАТ ИМЯ  ->  $1=inspect $2=-f $3=формат $4=имя.
    # Первая версия стаба искала имя в $3 и формат в $2 — и отвечала
    # пустотой на всё, отчего проверка считала контейнеры живыми.
    cases = "".join(
        f'''  if [ "$4" = "{n}" ]; then
    case "$3" in
      *Status*) printf '%s\\n' "{st}" ;;
      *RestartCount*) printf '%s\\n' "{rc}" ;;
    esac
    exit 0
  fi\n''' for n, st, rc in containers)
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "ps" ]; then\n'
        f'  printf "%s\\n" "{names}"\n  exit 0\nfi\n'
        'if [ "$1" = "inspect" ]; then\n'
        f"{cases}"
        '  exit 1\nfi\n'
        'if [ "$1" = "logs" ]; then printf "строка лога\\n"; exit 0; fi\n'
        "exit 0\n", encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)

    harness = f"""
set -uo pipefail
export PATH="{bin_dir}"
VERIFY_GRACE_S=0
say()  {{ printf '\\n>> %s\\n' "$1"; }}
ok()   {{ printf '   OK   %s\\n' "$1"; }}
warn() {{ printf '   ВНИМАНИЕ %s\\n' "$1"; }}
die()  {{ printf 'ОСТАНОВ: %s\\n' "$1" >&2; exit 1; }}
{_functions("verify_running")}
verify_running
"""
    proc = subprocess.run([shutil.which("bash"), "-c", harness],
                          capture_output=True, text=True,
                          env={"PATH": str(bin_dir)})
    return proc.returncode, proc.stdout + proc.stderr


def test_all_running_passes(tmp_path):
    rc, out = run_verify(tmp_path, [("manta-a", "running", "0"),
                                    ("manta-b", "running", "1")])
    assert rc == 0, out
    assert "все контейнеры работают" in out


def test_restarting_container_fails_the_install(tmp_path):
    """Перезапускающийся контейнер — это НЕ «Готово».

    `compose up -d` возвращает успех, как только контейнеры созданы.
    Упавший через секунду сервис под restart: unless-stopped уходит в
    бесконечный цикл, а установка рапортует об успехе. Ровно так и вышло
    на живой машине: семь коллекторов подняты, один крутится по кругу,
    скрипт вышел с нулём.
    """
    rc, out = run_verify(tmp_path, [("manta-a", "running", "0"),
                                    ("manta-timeline-collector-1", "restarting", "9")])
    assert rc != 0, out
    assert "manta-timeline-collector-1" in out
    assert "НЕ готов" in out


def test_failed_container_logs_are_shown(tmp_path):
    """Показаны последние строки лога упавшего.

    Иначе диагностика начинается с отдельного похода за `docker logs`, а
    имя контейнера ещё надо угадать среди четырнадцати.
    """
    rc, out = run_verify(tmp_path, [("manta-x", "exited", "0")])
    assert "строка лога" in out


def test_exited_container_is_not_ignored(tmp_path):
    """Молча вышедший контейнер — тоже отказ.

    Он не перезапускается и потому не бросается в глаза, но сервиса нет.
    """
    rc, out = run_verify(tmp_path, [("manta-x", "exited", "0")])
    assert rc != 0


def test_survivor_with_many_restarts_is_reported_but_not_fatal(tmp_path):
    """Пережил перезапуски, но сейчас жив — предупреждение, не отказ.

    Kafka и ClickHouse честно перезапускаются на старте, пока ждут
    соседей. Считать это провалом значило бы ронять установку на
    нормальном поведении.
    """
    rc, out = run_verify(tmp_path, [("manta-kafka-1", "running", "5")])
    assert rc == 0, out
    assert "перезапускался" in out


def test_verification_actually_runs_at_the_end_of_the_stack_step():
    """verify_running ВЫЗЫВАЕТСЯ, и после миграций.

    Безупречная функция, которую никто не зовёт, — это отсутствующая
    функция. А до миграций звать её нельзя: сервисы, которым нужна
    схема, честно перезапускаются, пока её нет, и проверка объявила бы
    отказом нормальный порядок вещей.
    """
    src = _functions("start_stack")
    assert "verify_running" in src, "проверка живости не вызывается"
    assert src.index("migrate") < src.index("verify_running"), \
        "проверка живости идёт до миграций"


def test_postgres_users_exist_before_application_containers_start():
    """Login-роли появляются между infra-up и apps-up."""
    src = _functions("start_stack")
    assert (src.index('$COMPOSE up -d') < src.rindex("pg-migrate.sh") <
            src.rindex("create-db-users.sh") <
            src.rindex("$COMPOSE --profile apps"))


def test_clickhouse_users_exist_before_application_containers_start():
    src = _functions("start_stack")
    assert (src.index('$COMPOSE up -d') < src.rindex("ch-migrate.sh") <
            src.rindex("create-clickhouse-users.sh") <
            src.rindex("$COMPOSE --profile apps"))


def test_minio_secrets_are_distinct_and_idempotent(tmp_path):
    names = ("MANTA_S3_PASS_INGEST", "MANTA_S3_PASS_PARSER",
             "MANTA_S3_PASS_MODEL_READER", "MANTA_S3_PASS_MODEL_WRITER")
    rc, out, first, _ = run_secrets(tmp_path)
    assert rc == 0, out
    values = [password_of(first, name) for name in names]
    assert all(len(value) >= 16 for value in values)
    assert len(set(values)) == len(values)
    rc, out, second, _ = run_secrets(tmp_path)
    assert rc == 0, out
    assert [password_of(second, name) for name in names] == values


def test_minio_users_exist_before_application_containers_start():
    src = _functions("start_stack")
    assert (src.index('$COMPOSE up -d') <
            src.rindex("create-minio-users.sh") <
            src.rindex("$COMPOSE --profile apps"))


# =============================================================================
# Инструменты хоста (спринт 157)
#
# ЧТО СЛУЧИЛОСЬ. Установка закончилась словами «залить датасет: make
# peer-sync», пользователь их скопировал и получил «Command 'make' not
# found». Ubuntu-сервер make не несёт, а bootstrap его не ставил.
#
# Дыра глубже опечатки. Тот же bootstrap ЗАВОДИТ РАСПИСАНИЕ: обмен в
# 04:00 и выгрузка бэкапа в облако в 03:30 — оба через rclone, которого
# на машине тоже нет. Оба скрипта наличие проверяют и говорят внятно, но
# говорят они в cron, то есть в никуда: каждую ночь тихий отказ, и
# заметить его можно только по тому, что данных не прибавляется.
#
# Форма знакомая: правильное решение применено к части своих случаев.
# Расписание написано ПРАВИЛЬНО — оно зовёт ./scripts/x.sh напрямую, без
# make. Человеку же адресовали команду, которой на машине нет.
# =============================================================================

MAKEFILE = SCRIPT.parent.parent / "Makefile"
SETUP_VPS = SCRIPT.parent.parent / "docs" / "SETUP-VPS.md"


def _assignment(name: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{name}=\(([^)]*)\)", src, re.M)
    assert m, f"массив {name} не найден в vps-bootstrap.sh"
    return m.group(0)


def host_tools() -> set[str]:
    """Инструменты, которые установка ставит на хост.

    Массивом, а не строкой, и это не вкусовщина: строка
    `HOST_TOOLS="make rclone"` попадала в проверку предписанных команд
    как «make rclone» — то есть объявление списка читалось как приказ
    выполнить несуществующую цель. Ложная тревога на верной настройке.

    Со спринта 173 записаны они парами «команда:пакет»: apt ставит
    `nodejs`, а в PATH появляется `node`, и путать эти имена нельзя.
    Здесь нужна КОМАНДА — её ищут скрипты расписания строкой
    `command -v X || die`.
    """
    return {e.split(":", 1)[0] for e in _host_entries()}


def _host_entries() -> list[str]:
    return _host_entries_of("HOST_TOOLS")


def _host_entries_of(name: str) -> list[str]:
    """Элементы bash-массива верхнего уровня, объявленного в скрипте.

    Массивы в скрипте бывают многострочными (STEPS), поэтому берётся всё
    от «(» до «)», а не одна строка.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    body = src.split(f"{name}=(", 1)[1].split(")", 1)[0]
    return body.split()


def host_packages() -> set[str]:
    """Имена пакетов apt — правая половина тех же пар."""
    return {e.split(":")[-1] for e in _host_entries()}


def run_host_tools(tmp_path, installed=(), check_only="0"):
    """Прогнать setup_host_tools в песочнице с заданным набором «уже есть».

    PATH подменяется целиком: command -v — встроенная команда bash и
    стабу не поддаётся, зато честно ищет по PATH. Значит «инструмент
    установлен» проверяется ровно так же, как на живой машине.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "apt.log"
    (bindir / "apt-get").write_text(
        f'#!/bin/sh\necho "$@" >>{log}\n', encoding="utf-8")
    (bindir / "apt-get").chmod(0o755)
    for tool in installed:
        (bindir / tool).write_text("#!/bin/sh\n", encoding="utf-8")
        (bindir / tool).chmod(0o755)

    harness = f"""
set -uo pipefail
PATH="{bindir}"
say()  {{ printf '>> %s\\n' "$1"; }}
ok()   {{ printf 'OK %s\\n' "$1"; }}
warn() {{ printf 'WARN %s\\n' "$1"; }}
die()  {{ printf 'DIE %s\\n' "$1"; exit 1; }}
need_root() {{ :; }}
SUDO=""
CHECK_ONLY={check_only}
{_assignment("HOST_TOOLS")}
{_functions("setup_host_tools")}
setup_host_tools
"""
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    apt = log.read_text(encoding="utf-8") if log.exists() else ""
    return r.returncode, r.stdout + r.stderr, apt


def test_missing_tools_are_installed(tmp_path):
    rc, out, apt = run_host_tools(tmp_path)
    assert rc == 0, out
    assert "make" in apt and "rclone" in apt, f"apt не звали: {apt!r}"


def test_only_the_missing_ones_are_installed(tmp_path):
    """Ставится недостающее, а не весь список заново.

    Не косметика: apt-get install на уже стоящем пакете безобиден, но
    прогон установки — операция, которую гоняют после каждого git pull, и
    лишние минуты на apt делают её той, которую пропускают.
    """
    rc, out, apt = run_host_tools(tmp_path, installed=("make",))
    assert rc == 0, out
    assert "rclone" in apt
    assert "make" not in apt


def test_nothing_is_installed_when_everything_is_there(tmp_path):
    rc, out, apt = run_host_tools(
        tmp_path, installed=("make", "rclone", "node", "npm"))
    assert rc == 0, out
    assert apt == "", f"apt звали впустую: {apt!r}"
    assert "OK" in out


def test_check_mode_installs_nothing(tmp_path):
    """--check обязан только смотреть.

    Прогон с --check делают ИМЕННО чтобы ничего не менять — на живой
    машине, чтобы свериться. Установка пакетов оттуда была бы худшим
    видом сюрприза: тихим и от лица проверки.
    """
    rc, out, apt = run_host_tools(tmp_path, check_only="1")
    assert rc == 0, out
    assert apt == "", f"проверка что-то поставила: {apt!r}"
    assert "WARN" in out and "make" in out and "rclone" in out


def test_check_mode_is_not_mute_about_what_is_missing(tmp_path):
    """И называет ровно то, чего нет.

    «Что-то не так» без имени — это приглашение перечитать скрипт.
    """
    rc, out, apt = run_host_tools(tmp_path, installed=("make",), check_only="1")
    assert "rclone" in out
    assert re.search(r"WARN[^\n]*rclone", out), out


# -- правила, из-за которых спринт и случился ---------------------------------

def scheduled_scripts() -> list[Path]:
    """Скрипты, которые bootstrap ставит в cron.

    Со спринта 163 само расписание объявляет `cron_wanted`, а `setup_cron`
    его лишь примиряет с тем, что уже стоит на машине. Смотреть надо в
    обе: разбор одной только `setup_cron` перестал бы находить хоть
    что-нибудь — а проверки ниже параметризуются найденным, то есть
    молча превратились бы в пустое множество.
    """
    src = _functions("setup_cron", "cron_wanted")
    found = re.findall(r"\./scripts/([a-z0-9-]+\.sh)", src)
    assert found, "в расписании не нашлось ни одного скрипта"
    return [SCRIPT.parent / name for name in sorted(set(found))]


def test_the_schedule_and_the_scripts_are_actually_found():
    """Страховка от проверки пустоты: разборы ниже что-то находят."""
    names = {p.name for p in scheduled_scripts()}
    assert {"backup.sh", "peer-sync.sh", "heartbeat.sh"} <= names, names
    assert all(p.exists() for p in scheduled_scripts())
    assert "gc-salts.sh" in names, "добыча солей выпала из расписания"
    assert host_tools() == {"make", "rclone", "node", "npm"}
    # Пакет apt для node зовётся иначе, чем команда, и это ровно та
    # разница, из-за которой список стал парами.
    assert "nodejs" in host_packages()


@pytest.mark.parametrize("script", [p.name for p in scheduled_scripts()])
def test_every_tool_the_schedule_needs_is_installed(script):
    """Расписание не заводится для инструмента, которого нет.

    Скрипты объявляют свои зависимости сами — строкой
    `command -v X || die`. Значит и сверять надо с ней, а не со списком,
    переписанным руками: перепишешь один раз верно, а разъедется он
    при первой же новой зависимости.

    Цена ошибки — тихий ночной отказ: cron гоняет скрипт, скрипт внятно
    объясняет, чего не хватает, и говорит это в /dev/null.
    """
    path = SCRIPT.parent / script
    needs = set(re.findall(r"command -v (\w+) >/dev/null \|\| ",
                           path.read_text(encoding="utf-8")))
    missing = needs - host_tools() - BASE_TOOLS
    assert not missing, (
        f"{script} стоит в расписании, но требует {sorted(missing)} — "
        f"а установка их не ставит")


# Есть на чистой Ubuntu Server 24.04 без единого apt install.
BASE_TOOLS = {"tar", "gzip", "curl", "python3", "sed", "awk", "grep",
              "find", "crontab", "openssl", "docker"}


def make_targets() -> set[str]:
    return set(re.findall(r"^([a-z][a-z0-9-]*):",
                          MAKEFILE.read_text(encoding="utf-8"), re.M))


# Предписание — это КОМАНДА, а не всякое упоминание слова «make». Отсюда
# позиция в строке: начало строки (блок кода), обратная кавычка (текст) или
# двоеточие с пробелом (напутствие установщика «залить датасет: make
# peer-sync»). Без этого объявление HOST_TOOLS=(make rclone) читалось бы
# как приказ выполнить несуществующую цель «rclone» — ложная тревога на
# верной настройке, а такая тревога учит игнорировать проверку.
PRESCRIBED_RE = re.compile(r"(?:^[ \t]*|`|: )make ([a-z][a-z0-9-]*)", re.M)


def prescribed_make_targets() -> set[str]:
    """Цели make, которые предписаны установщиком и руководством по VPS."""
    text = (SCRIPT.read_text(encoding="utf-8")
            + SETUP_VPS.read_text(encoding="utf-8"))
    return set(PRESCRIBED_RE.findall(text))


def test_a_mention_is_not_a_prescription():
    """Разбор отличает команду от слова в списке."""
    assert PRESCRIBED_RE.findall("HOST_TOOLS=(make rclone)\n") == []
    assert PRESCRIBED_RE.findall("make peer-sync ARGS=--dry-run\n") == ["peer-sync"]
    assert PRESCRIBED_RE.findall("текст `make doctor` текст") == ["doctor"]
    assert PRESCRIBED_RE.findall('echo "  • залить: make peer-sync"') == ["peer-sync"]


def test_prescribed_commands_are_actually_runnable():
    """Команда, напечатанная установщиком, работает сразу после установки.

    Ровно этого и не было: последняя строка напутствия — «залить датасет:
    make peer-sync», а make на Ubuntu-сервере нет. Проверка держит две
    половины вместе: раз мы говорим «make», значит make ставим, и цель
    существует.
    """
    prescribed = prescribed_make_targets()
    assert prescribed, "разбор не нашёл ни одной команды make — проверка пуста"
    assert "peer-sync" in prescribed, "потеряна та самая команда"

    assert "make" in host_tools(), (
        "инструкции написаны через make, а установка его не ставит — "
        f"не сработает ни одна из {sorted(prescribed)}")

    unknown = sorted(prescribed - make_targets())
    assert not unknown, f"предписаны несуществующие цели make: {unknown}"


def test_tools_are_installed_before_the_schedule_is_written():
    """Порядок шагов: сначала инструменты, потом расписание и напутствие.

    Обратный порядок не падает — он просто оставляет окно, в котором
    cron уже заведён, а rclone ещё нет. На идемпотентном скрипте это
    мелочь, но именно из таких окон и складывается «вроде всё прошло».
    """
    # Со спринта 176 порядок задаёт список STEPS, а не последовательность
    # вызовов в хвосте: из него же берётся и разбор --only, чтобы два
    # перечня шагов не разошлись.
    order = [e.split(":", 1)[0] for e in _host_entries_of("STEPS")]
    assert order.index("tools") < order.index("cron")
    src = SCRIPT.read_text(encoding="utf-8")
    tail = src[src.index("# -- поехали"):]
    assert tail.index("STEPS") < tail.index("Готово. Дальше")


# -- частичная установка (спринт 176) ------------------------------------------
#
# ЗАЧЕМ ФЛАГ. Полный прогон пересобирает образы по одному — на двухъядерном
# VPS это десятки минут. Когда меняется одна строка расписания, цена
# «применить правку» несоизмерима с правкой, и правку откладывают. Ровно
# так вышло в спринтах 159–161: три коммита-починки cron подряд не
# доезжали до машины. Установка, которую нельзя применить частично,
# применяется реже, чем нужно.

def run_bootstrap(tmp_path, *args):
    """Прогнать скрипт целиком со стабами вместо всех тяжёлых шагов.

    Подменяются САМИ ФУНКЦИИ шагов: так проверяется настоящий разбор
    аргументов и настоящий диспетчер, а не их пересказ.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    # Тело шагов заменяем на отметку в лог — до строки «поехали» скрипт
    # только объявляет, поэтому подмена делается после его загрузки.
    log = tmp_path / "steps.log"
    stubs = "\n".join(
        f'{fn}() {{ echo {name} >> "{log}"; }}'
        for name, fn in (("secrets", "setup_secrets"), ("docker", "setup_docker"),
                         ("tools", "setup_host_tools"), ("keys", "setup_gateway_keys"),
                         ("firewall", "setup_firewall"), ("stack", "start_stack"),
                         ("cron", "setup_cron")))
    marker = "# -- поехали ---"
    head, tail = src.split(marker, 1)
    patched = head + stubs + "\n" + marker + tail
    script = tmp_path / "bootstrap.sh"
    script.write_text(patched, encoding="utf-8")
    proc = subprocess.run(["bash", str(script), *args], capture_output=True,
                          text=True, cwd=str(SCRIPT.parent.parent), timeout=60)
    done = log.read_text(encoding="utf-8").split() if log.exists() else []
    return proc.returncode, proc.stdout + proc.stderr, done


def test_without_flags_every_step_runs(tmp_path):
    """Страховка от проверки пустоты: без флагов делается всё.

    Проверки ниже смотрят, что шагов стало МЕНЬШЕ; сломайся диспетчер
    целиком — они прошли бы, а установка не делала бы ничего.
    """
    rc, out, done = run_bootstrap(tmp_path)
    assert rc == 0, out
    assert done == ["secrets", "docker", "tools", "keys", "firewall",
                    "stack", "cron"], done


def test_only_runs_a_single_step(tmp_path):
    """ГЛАВНОЕ: --only cron не пересобирает стек.

    Именно сборка образов делает полный прогон дорогим; ради строки в
    расписании её платить не за что.
    """
    rc, out, done = run_bootstrap(tmp_path, "--only", "cron")
    assert rc == 0, out
    assert done == ["cron"], done


def test_only_accepts_several_steps_in_declared_order(tmp_path):
    """Несколько шагов — через запятую, и порядок берётся из списка.

    Порядок шагов не произволен (пароли раньше стека, стек раньше cron).
    Выполнять их в том порядке, в каком их назвали, значит разрешить
    поднять стек до генерации паролей.
    """
    rc, out, done = run_bootstrap(tmp_path, "--only", "cron,secrets")
    assert rc == 0, out
    assert done == ["secrets", "cron"], done


def test_a_misspelled_step_stops_instead_of_doing_nothing(tmp_path):
    """Опечатка — отказ, а не тихое бездействие.

    Пропусти её — прогон закончился бы словами «выполнены только шаги:
    crn», не сделав ничего, и это выглядело бы как успешная установка.
    """
    rc, out, done = run_bootstrap(tmp_path, "--only", "crn")
    assert rc != 0
    assert done == [], done
    assert "crn" in out and "cron" in out, "отказ не показывает доступные шаги"


def test_the_step_names_are_checked_before_anything_happens(tmp_path):
    """И проверяется ДО первого действия.

    Иначе «--only secrets,crn» сгенерировал бы пароли и упал — то есть
    опечатка в конце списка меняла бы состояние машины.
    """
    rc, out, done = run_bootstrap(tmp_path, "--only", "secrets,crn")
    assert rc != 0
    assert done == [], done


def test_only_without_a_name_is_refused(tmp_path):
    """`--only` без шага — не «сделай всё».

    Молчаливое «значит, всё» на оборванной команде запустило бы
    получасовую пересборку там, где просили один шаг.
    """
    rc, out, done = run_bootstrap(tmp_path, "--only")
    assert rc != 0 and done == []


def test_an_unknown_flag_is_refused(tmp_path):
    """Неизвестный флаг не проглатывается.

    Проглоченный `--сheck` с русской «с» выполнил бы полную установку
    там, где просили проверку.
    """
    rc, out, done = run_bootstrap(tmp_path, "--chek")
    assert rc != 0 and done == []
    assert "--check" in out


def test_the_step_list_asked_for_matches_the_dispatcher(tmp_path):
    """`--only ?` печатает те же шаги, которые реально исполняются.

    Два перечня шагов однажды разошлись бы, и `--only` тихо пропускал бы
    существующий шаг.
    """
    rc, out, done = run_bootstrap(tmp_path, "--only", "?")
    assert rc == 0 and done == []
    listed = out.split(":", 1)[1].split()
    _, _, all_done = run_bootstrap(tmp_path)
    assert listed == all_done, (listed, all_done)


def test_partial_run_says_it_was_partial(tmp_path):
    """Итог не выдаёт частичный прогон за полную установку.

    Напутствие «Готово. Дальше — docs/SETUP-VPS.md» после `--only cron`
    сообщало бы, что машина настроена целиком.
    """
    rc, out, _ = run_bootstrap(tmp_path, "--only", "cron")
    assert "только шаги" in out
    assert "Готово. Дальше" not in out


def test_the_cron_banner_names_every_job_it_installs():
    """Баннер расписания перечисляет ровно то, что поставил.

    Баннер, называющий не весь список, читается как полный: работа, о
    которой он молчит, считается неслучившейся, и её идут заводить руками
    — или, хуже, не идут. Поймано при деплое спринта 173: соли уже
    ставились в расписание, а баннер о них молчал.
    """
    src = _functions("cron_wanted", "setup_cron")
    jobs = set(re.findall(r"\./scripts/([a-z0-9-]+)\.sh", src))
    names = {"backup": "бэкап", "peer-sync": "обмен", "heartbeat": "сторож",
             "gc-salts": "соли"}
    for job in jobs:
        assert job in names, f"новая работа {job} — добавьте её в проверку"

    # КАЖДЫЙ баннер отдельно, а не их объединение. Веток две (с соседями и
    # без), и проверка по объединению пропускала мутацию, снявшую работу
    # из одной из них, — та самая форма дефекта, что повторяется в этом
    # проекте чаще прочих: верное решение применено к части своих случаев.
    # Только ПЕРЕЧИСЛЯЮЩИЕ баннеры: в setup_cron есть ещё «cron уже
    # настроен», который списка работ не содержит и содержать не должен.
    banners = [b for b in re.findall(r'ok "([^"]+)"', src) if "UTC" in b]
    assert len(banners) == 2, banners
    # Обмен условен — он и в расписании появляется только при соседях.
    always = {names[j] for j in jobs} - {"обмен"}
    for banner in banners:
        missing = sorted(w for w in always if w not in banner)
        assert not missing, (
            f"баннер «{banner}» молчит о {missing}, хотя расписание это ставит")
