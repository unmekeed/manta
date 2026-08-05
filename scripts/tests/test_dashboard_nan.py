"""«Не измерено» не должно выглядеть как отличный результат (спринт 92).

Инцидент 2026-08-03: на дашборде висело «Brier на про-эталоне 0.0000» и
рядом зелёная подпись «цель ≤ 0.18 ✓». Brier ноль недостижим в принципе,
а галочка утверждала, что цель проекта перевыполнена.

Причина: prometheus_client создаёт Gauge со значением 0.0, а
BRIER_BENCHMARK выставлялся только внутри переобучения. После каждого
`make recover` процесс auto-train стартовал заново, и до первой
тренировки метрика честно отдавала ноль — который дашборд честно
показывал и честно сравнивал с порогом.

Тот же класс отказа, что «пустой bash-скрипт вернул 0, значит успех»:
отсутствие данных прошло по всем проверкам как хороший результат.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard  # noqa: E402


def test_nan_metric_reads_as_missing():
    metrics = {("wp_brier_benchmark_pro", ()): float("nan")}
    assert dashboard._pick(metrics, "wp_brier_benchmark_pro") is None


def test_absent_metric_reads_as_missing():
    assert dashboard._pick({}, "wp_brier_benchmark_pro") is None


def test_real_zero_is_still_a_value():
    """Обратная сторона: ноль — законное значение для счётчиков вроде
    retrains_total, и глушить его нельзя."""
    metrics = {("retrains_total", (("outcome", "promoted"),)): 0.0}
    assert dashboard._pick(metrics, "retrains_total",
                           outcome="promoted") == 0.0


def test_nan_survives_the_prometheus_parser():
    """Сквозная проверка: NaN обязан доехать от текста экспозиции до
    _pick. Если парсер уронит строку, метрика станет «отсутствующей» по
    другой причине — совпадение, на которое опираться нельзя."""
    text = "# HELP wp_brier_valid x\n# TYPE wp_brier_valid gauge\nwp_brier_valid NaN\n"
    parsed = dashboard._parse_prom(text)
    assert ("wp_brier_valid", ()) in parsed, "парсер потерял строку с NaN"
    assert dashboard._pick(parsed, "wp_brier_valid") is None


@pytest.mark.parametrize("value", [float("nan"), None])
def test_history_never_records_missing(value):
    """Спарклайн живёт весь срок процесса, и масштаб считается по
    min/max: один NaN испортил бы график навсегда."""
    key = "brier_bm_test"
    dashboard._history[key].clear()
    dashboard._record(key, value)
    assert list(dashboard._history[key]) == []


def test_history_records_real_values():
    key = "brier_bm_test2"
    dashboard._history[key].clear()
    dashboard._record(key, 0.1682)
    assert list(dashboard._history[key]) == [0.1682]


# -- дашборд обязан подхватывать правки (спринт 101) --------------------------

def test_recover_restarts_stale_dashboard():
    """Дашборд — единственный процесс, который make stop намеренно не
    трогает (спринт 74: иначе умирает кнопка «Поднять всё»). Из-за этого
    он же оказался единственным, кто НЕ ПОДХВАТЫВАЛ правки: recover
    пропускал его как «уже работает».

    Инцидент 2026-08-04: фиксы спринтов 92 и 96 не доехали до страницы
    вовсе — на экране висела надпись из версии двухдневной давности, и
    оба «исправленных» дефекта выглядели неисправленными.
    """
    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    assert "stat -c %Y scripts/dashboard.py" in src, (
        "recover не сверяет время правки дашборда со временем запуска")
    assert "дашборд старее своего кода" in src


def test_recover_does_not_kill_its_own_parent():
    """Если recover запущен КНОПКОЙ дашборда, убивать дашборд нельзя —
    он родитель этой задачи, и она оборвётся на середине. В этом случае
    ожидается предупреждение, а не kill."""
    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    i = src.index("дашборд старее своего кода")
    guard = src[max(0, i - 600):i]
    assert "MANTA_DASHBOARD_JOB" in guard, (
        "нет защиты от самоубийства при запуске из панели управления")


def test_dashboard_marks_its_own_jobs():
    """Метку ставит сам дашборд — иначе проверка выше опирается на
    переменную, которую никто не выставляет."""
    src = (Path(__file__).resolve().parents[1] / "dashboard.py"
           ).read_text(encoding="utf-8")
    assert 'MANTA_DASHBOARD_JOB": "1"' in src
    assert "env=job_env" in src


def test_dashboard_probe_survives_missing_process():
    """Строка поиска дашборда обязана переживать ОТСУТСТВИЕ процесса.

    dev-recover.sh идёт под `set -euo pipefail`, а pgrep без совпадений
    возвращает 1 — то есть присваивание `pid=$(pgrep ...)` роняет весь
    скрипт. Инцидент 2026-08-04: recover падал ровно в том сценарии, ради
    которого блок и писался, — когда дашборд убит вручную перед
    обновлением.

    Проверка ИСПОЛНЯЕТ строку, а не ищет в ней подстроку: `|| true`
    можно потерять при рефакторинге, и текстовый тест это пропустил бы
    ровно так же, как пропустил в первый раз.
    """
    import subprocess

    import tempfile

    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    line = next(l for l in src.splitlines()
                if l.startswith("dash_pid=$(pgrep"))
    probe = line.replace("scripts/dashboard.py", "процесса-которого-нет-9z9z")
    # Скрипт кладём В ФАЙЛ, а не передаём через `bash -c`. Иначе шаблон
    # попадает в командную строку самого bash, pgrep -f находит её же, и
    # тест зеленеет при любом коде — проверено мутацией: без `|| true`
    # вариант с `-c` тоже «проходил».
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "probe.sh"
        f.write_text(f"set -euo pipefail\n{probe}\necho ДОШЛИ\n",
                     encoding="utf-8")
        r = subprocess.run(["bash", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, f"строка роняет скрипт: {r.stderr}"
    assert "ДОШЛИ" in r.stdout


# -- recover сверяет свежесть кода у ВСЕХ процессов (спринт 106) --------------

def test_recover_restarts_every_stale_service():
    """Спринт 101 закрыл ловушку `if ! pgrep` только для дашборда, а она
    общая: после git pull recover докладывает «уже работает, пропуск», и
    стек крутит код, загруженный ДО правки, пока doctor рапортует ЗДОРОВ.

    2026-08-05 это стоило двух ложных заходов подряд — спринты 104 и 105
    выглядели неработающими, хотя обе правки были верны.
    """
    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    assert "restart_if_stale" in src
    # Сверка обязана охватывать коллекторы и экстрактор — именно на них
    # сегодня и обожглись.
    for pat in ("collector --source opendota-league",
                "python3 -u -m extractor",
                "python3 -u -m training.auto"):
        assert pat in src.split("restart_if_stale()")[1], (
            f"сервис не попал в сверку свежести: {pat}")


def test_stale_check_runs_before_services_start():
    """Порядок решает: если убивать устаревшие ПОСЛЕ блоков запуска, они
    останутся лежать до следующего recover — то есть стек окажется без
    сервисов вместо обновлённых."""
    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    assert (src.index("restart_if_stale \"${svc%%|*}\"")
            < src.index("# 4. Хост-сервисы конвейера"))


def test_stale_probe_survives_missing_process():
    """Та же грабля, что в спринте 102: под `set -euo pipefail` pgrep без
    совпадений возвращает 1 и роняет весь скрипт. Проверка ИСПОЛНЯЕТ
    функцию с заведомо отсутствующим процессом."""
    import subprocess
    import tempfile

    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    body = src.split("restart_if_stale() {", 1)[1].split("\n}", 1)[0]
    fn = "say() { printf '>> %s\\n' \"$*\"; }\nnewest_code=9999999999\n" \
         "restart_if_stale() {" + body + "\n}\n"
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "probe.sh"
        f.write_text("set -euo pipefail\n" + fn
                     + 'restart_if_stale тест "процесса-нет-9z9z"\n'
                       'echo ДОШЛИ\n', encoding="utf-8")
        r = subprocess.run(["bash", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, f"функция роняет скрипт: {r.stderr}"
    assert "ДОШЛИ" in r.stdout


def test_dashboard_kept_out_of_the_generic_loop():
    """У дашборда отдельный блок с защитой от самоубийства: recover может
    быть запущен ЕГО кнопкой. Попади он в общий список — задача обрывалась
    бы на середине."""
    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    loop = src.split("restart_if_stale()")[1].split("# 4. Хост-сервисы")[0]
    assert "dashboard.py" not in loop


def test_recover_defaults_to_the_env_file(tmp_path):
    """`make recover` работал БЕЗ токенов (спринт 107).

    Makefile передаёт `MANTA_TRAIN_ENV=` пустым — переменная в нём не
    определена. scripts/manta дефолт подставляет, Makefile нет, и
    env-файл не читался вовсе: ключ OpenDota, токен STRATZ и Telegram
    выключены. До спринта 106 это сходило с рук, потому что recover
    пропускал живые сервисы; теперь он их ПЕРЕЗАПУСКАЕТ, то есть отобрал
    бы токены у работающих коллекторов — и выглядело бы как обычный
    рестарт.

    Проверка исполняет строку с ПУСТЫМ значением, как его и передаёт
    Makefile: `${VAR:-default}` считает пустое отсутствующим, а
    `${VAR-default}` — нет, и разница здесь решает всё.
    """
    import subprocess

    src = (Path(__file__).resolve().parents[1] / "dev-recover.sh"
           ).read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if l.startswith("TRAIN_ENV="))
    probe = tmp_path / "probe.sh"
    probe.write_text(f'set -euo pipefail\nHOME=/дом\n{line}\n'
                     'printf "%s" "$TRAIN_ENV"\n', encoding="utf-8")
    r = subprocess.run(["bash", str(probe)], capture_output=True, text=True,
                       env={"MANTA_TRAIN_ENV": "", "PATH": "/usr/bin:/bin"})
    assert r.stdout == "/дом/manta-train.env", (
        f"пустое значение не заместилось дефолтом: {r.stdout!r}")
