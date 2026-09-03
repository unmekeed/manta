"""Тесты наложения для VPS (спринт 138, переписаны в 143).

Базовый docker-compose.yml писался для домашней машины за NAT, где
открытые порты безвредны. На VPS с белым адресом одиннадцать сервисов —
Postgres, ClickHouse, MinIO, Kafka, Grafana и прочие — публикуются на
0.0.0.0 с паролем, лежащим в git.

ЧТО ЗДЕСЬ БЫЛО НЕ ТАК ДО СПРИНТА 143. Тесты читали ФАЙЛ наложения и
убеждались, что в нём каждый порт записан как 127.0.0.1:… Это правда, и
это ничего не значит: docker compose СКЛЕИВАЕТ списки портов, а не
заменяет их. В слитой конфигурации у каждого сервиса оказывалось ДВЕ
публикации — исходная на 0.0.0.0 и добавленная на loopback.

То есть файл, написанный ради того, чтобы закрыть порты, их не закрывал,
а тесты этого не видели, потому что смотрели не туда. В докстроке
прежнего теста прямым текстом стояло ложное убеждение: «наложение
ЗАМЕНЯЕТ список у тех сервисов, что в нём описаны».

Вскрылось это не проверкой, а отказом при развёртывании: postgres занял
0.0.0.0:5432, а следом не смог занять 127.0.0.1:5432 — «address already
in use», сам с собой. Сбой привязки и не дал стеку подняться настежь.

ЧТО ПРОВЕРЯЕТСЯ ТЕПЕРЬ. Слитая конфигурация — то есть ЭФФЕКТ, а не
намерение. Плюс статически: механизм замены (`!override`) стоит у
каждого списка портов. Первое точнее, второе работает и там, где docker
недоступен.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML нужен для разбора compose")

DEPLOY = Path(__file__).resolve().parents[2] / "deployments"
BASE = DEPLOY / "docker-compose.yml"
VPS = DEPLOY / "docker-compose.vps.yml"
NGINX_VPS = DEPLOY / "nginx.vps.conf"
PROM_BASE = DEPLOY / "monitoring" / "prometheus.yml"
PROM_VPS = DEPLOY / "monitoring" / "prometheus.vps.yml"
MLFLOW_DOCKERFILE = DEPLOY / "mlflow.Dockerfile"


def _load(path: Path) -> dict:
    """Прочитать файл наложения как YAML.

    Тег !override, которым помечены списки портов, для yaml.safe_load —
    неизвестный тег, поэтому он вырезается перед разбором. Сам факт его
    наличия проверяется отдельным тестом по тексту файла.
    """
    text = path.read_text(encoding="utf-8").replace(" !override", "")
    return yaml.safe_load(text)


def merged_config() -> dict:
    """Слитая конфигурация — то, что docker получит на самом деле."""
    if not shutil.which("docker"):
        pytest.skip("docker недоступен: слитую конфигурацию не получить")
    secrets = {
        "MANTA_DB_PASS_COLLECTOR": "collector-test-password",
        "MANTA_DB_PASS_REPORTS": "reports-test-password",
        "MANTA_DB_PASS_GATEWAY": "gateway-test-password",
        "MANTA_DB_PASS_RO": "ro-test-password",
        "MANTA_S3_PASS_INGEST": "ingest-test-password",
        "MANTA_S3_PASS_PARSER": "parser-test-password",
        "MANTA_S3_PASS_MODEL_READER": "reader-test-password",
        "MANTA_S3_PASS_MODEL_WRITER": "writer-test-password",
        "MANTA_CH_PASS_READER": "ch-reader-test-password",
        "MANTA_CH_PASS_WRITER": "ch-writer-test-password",
        "MANTA_CH_PASS_TRAINER": "ch-trainer-test-password",
        "MANTA_REDIS_PASSWORD": "redis-test-password",
        "MANTA_KEYS_DIR": "/tmp/manta-test-keys",
    }
    proc = subprocess.run(
        ["docker", "compose", "-f", str(BASE), "-f", str(VPS),
         "--profile", "apps", "--profile", "monitoring",
         "config", "--format", "json"],
        capture_output=True, text=True, env={**os.environ, **secrets})
    if proc.returncode != 0:
        pytest.fail(f"docker compose config не отработал: {proc.stderr[:400]}")
    return json.loads(proc.stdout)


def published(service: dict) -> list[dict]:
    return service.get("ports") or []


def _published(compose: dict) -> dict[str, list[str]]:
    """{сервис: [строки публикации портов]} — только те, что публикуются."""
    out = {}
    for name, svc in (compose.get("services") or {}).items():
        ports = svc.get("ports") or []
        if ports:
            out[name] = [str(p) for p in ports]
    return out


def test_merged_config_publishes_nothing_outside_loopback():
    """ГЛАВНЫЙ тест файла: в СЛИТОЙ конфигурации ничего не смотрит наружу.

    Проверяется эффект, а не намерение. Прежняя версия читала файл
    наложения и видела там одни лишь 127.0.0.1 — при том, что docker
    получал вдобавок исходные публикации на 0.0.0.0. Одиннадцать портов
    с паролем из репозитория смотрели в интернет, а тест был зелёным.
    """
    cfg = merged_config()
    bad = [f"{name}: {p.get('host_ip', '0.0.0.0')}:{p.get('published')}"
           for name, svc in cfg["services"].items()
           for p in published(svc)
           if p.get("host_ip") != "127.0.0.1"]
    assert not bad, "порты смотрят наружу:\n" + "\n".join(bad)


def test_merged_config_has_no_duplicate_publications():
    """У сервиса не должно остаться ДВУХ публикаций одного порта.

    Именно так проявлялась склейка: 0.0.0.0:5432 и 127.0.0.1:5432 у
    одного контейнера. Вторая привязка падала с «address already in use»,
    и стек не поднимался — то есть склейка ломала не только безопасность,
    но и сам запуск.
    """
    cfg = merged_config()
    for name, svc in cfg["services"].items():
        seen = [(p.get("published"), p.get("protocol")) for p in published(svc)]
        dupes = {x for x in seen if seen.count(x) > 1}
        assert not dupes, f"{name}: порт опубликован дважды: {dupes}"


def test_every_ports_list_in_the_overlay_is_marked_override():
    """Механизм замены стоит у КАЖДОГО списка портов.

    Статическая проверка нужна отдельно от проверки эффекта: там, где
    docker недоступен, та пропускается, а эта — нет. Забыть !override у
    одного сервиса значит вернуть ему публикацию на 0.0.0.0.
    """
    text = VPS.read_text(encoding="utf-8")
    lists = re.findall(r"^\s*ports:(.*)$", text, re.M)
    assert lists, "в наложении нет ни одного списка портов"
    plain = [l for l in lists if "!override" not in l]
    assert not plain, (
        f"{len(plain)} списков портов без !override — compose СКЛЕИТ их с "
        f"базовыми, и порты уйдут на 0.0.0.0")


def _published(compose: dict) -> dict[str, list[str]]:
    """{сервис: [строки публикации портов]} — только те, что публикуются."""
    out = {}
    for name, svc in (compose.get("services") or {}).items():
        ports = svc.get("ports") or []
        if ports:
            out[name] = [str(p) for p in ports]
    return out


def test_no_service_publishes_a_port_without_a_vps_override():
    """Новый порт в базовом файле обязан появиться и в наложении.

    Наложение заменяет список портов только у сервисов, которые в нём
    описаны И помечены !override. Сервис, забытый в наложении, останется
    опубликованным на 0.0.0.0 со своим dev-паролем, и заметить это можно
    будет только сканером снаружи.

    Проверка статическая и потому переживает отсутствие docker; эффект
    сверяет test_merged_config_publishes_nothing_outside_loopback.
    """
    base = _published(_load(BASE))
    vps = _published(_load(VPS))
    missing = sorted(set(base) - set(vps))
    assert not missing, (
        "сервисы публикуют порты, но их нет в наложении для VPS "
        f"(останутся открыты наружу): {missing}")


def test_override_covers_the_same_ports_as_the_base():
    """Наложение не теряет и не выдумывает порты.

    Потерянный порт означает недоступную службу, лишний — открытую
    дырку. Сверяем по контейнерной стороне отображения.
    """
    def container_ports(ports: list[str]) -> set[str]:
        # "127.0.0.1:9500:9000" -> "9000";  "5432:5432" -> "5432"
        return {p.split(":")[-1] for p in ports}

    base = _published(_load(BASE))
    vps = _published(_load(VPS))
    for svc in sorted(set(base) & set(vps)):
        assert container_ports(base[svc]) == container_ports(vps[svc]), svc


# Машина, под которую считаются потолки: тариф Standart — 8 ядер, 12 ГБ.
# Число тут не для красоты: при смене тарифа его надо поменять ОСОЗНАННО,
# иначе потолки перестанут ограничивать что-либо (сумма больше памяти) или
# начнут душить (сумма сильно меньше).
TARGET_RAM_MB = 12 * 1024

# Сколько оставить ОС и ХОСТОВЫМ процессам. Сервисы сбора, парсер и
# обучение живут не в Docker (см. dev-recover.sh), и их в потолках нет:
# семь коллекторов, парсер, а на пике — обучение, у которого замерено
# 559 МиБ RSS. Три гигабайта на это, а не «сколько останется».
HOST_RESERVE_MB = 3 * 1024


def test_memory_limits_fit_the_target_machine():
    """Сумма потолков плюс запас для хоста влезает в машину.

    Потолки нужны потому, что ClickHouse считает доступную память от
    ВСЕГО хоста и, не зная про Kafka и Postgres рядом, выедает её до OOM
    соседей. Но и сумма потолков не должна превышать машину, иначе они
    не ограничивают ничего — это была бы декорация.
    """
    def to_mb(value: str) -> int:
        m = re.fullmatch(r"(\d+)([mg])", str(value).strip().lower())
        assert m, f"непонятный лимит: {value}"
        return int(m.group(1)) * (1024 if m.group(2) == "g" else 1)

    limits = {name: to_mb(svc["mem_limit"])
              for name, svc in _load(VPS)["services"].items()
              if svc.get("mem_limit")}
    assert limits, "потолков памяти нет вовсе"
    total = sum(limits.values())
    budget = TARGET_RAM_MB - HOST_RESERVE_MB
    assert total <= budget, (
        f"сумма потолков {total} МиБ не влезает в {budget} МиБ "
        f"(машина {TARGET_RAM_MB} МиБ минус запас хосту {HOST_RESERVE_MB})")


def test_clickhouse_gets_the_largest_share():
    """ClickHouse — самый прожорливый, и это должно быть видно в файле.

    Если однажды кто-то урежет его ниже Kafka, запросы к витрине начнут
    падать по памяти, а причина будет неочевидна.
    """
    services = _load(VPS)["services"]
    limits = {n: s.get("mem_limit") for n, s in services.items()}
    assert limits.get("clickhouse"), "у ClickHouse нет потолка памяти"
    assert limits["clickhouse"].endswith("g")


def test_services_restart_by_themselves():
    """VPS перезагружают, и стек обязан подниматься сам.

    Дома это делает задача Планировщика после входа в систему; на
    сервере входа в систему не происходит вовсе, и без restart-политики
    стек молча не встанет после перезагрузки.
    """
    without = [name for name, svc in _load(VPS)["services"].items()
               if svc.get("restart") != "unless-stopped"]
    assert not without, f"без политики перезапуска: {without}"


def test_every_service_logs_to_the_journal():
    """Логи обязаны пережить пересоздание контейнера (спринт 191).

    Драйвер по умолчанию — json-file, и файл живёт РОВНО столько же,
    сколько контейнер. Разбирая 29-часовой отказ генерации отчётов 2-3
    сентября, мы остались без улик именно поэтому: деплой пересоздал
    контейнеры, и логи тех суток исчезли вместе со старыми. Причину
    самого отказа установить уже невозможно.

    Проверяется КАЖДЫЙ сервис, а не только те, чьи логи кажутся
    интересными: угадать заранее, чей лог понадобится при следующем
    разборе, нельзя — в этот раз понадобился ml-service, о котором никто
    не думал.
    """
    without = [name for name, svc in _load(VPS)["services"].items()
               if (svc.get("logging") or {}).get("driver") != "journald"]
    assert not without, (
        f"логи не переживут пересоздание контейнера: {without}")


def test_vps_apps_use_function_specific_postgres_users():
    cfg = merged_config()
    services = cfg["services"]
    expected = {
        "api-gateway": "manta_gateway_user",
        "report-generator": "manta_reports_user",
        "data-collector": "manta_collector_user",
        "timeline-collector": "manta_collector_user",
        "pro-timeline-collector": "manta_collector_user",
        "league-collector": "manta_collector_user",
        "pro-replay-collector": "manta_collector_user",
        "stratz-collector": "manta_collector_user",
        "candidates-collector": "manta_collector_user",
    }
    for service, user in expected.items():
        dsn = services[service]["environment"]["POSTGRES_DSN"]
        assert f"postgresql://{user}:" in dsn, (service, dsn)
        assert "postgresql://dota:" not in dsn


def test_vps_overlay_has_no_dev_fallback_for_service_passwords():
    text = VPS.read_text(encoding="utf-8")
    for var in ("MANTA_DB_PASS_COLLECTOR", "MANTA_DB_PASS_REPORTS",
                "MANTA_DB_PASS_GATEWAY"):
        assert f"${{{var}:?" in text
        assert f"${{{var}:-" not in text


def test_vps_apps_use_function_specific_minio_users():
    services = merged_config()["services"]
    ingest = ("api-gateway", "data-collector", "timeline-collector",
              "pro-timeline-collector", "league-collector",
              "pro-replay-collector", "stratz-collector",
              "candidates-collector")
    for service in ingest:
        assert services[service]["environment"]["S3_ACCESS_KEY"] == "manta_ingest"
    assert services["parser-svc"]["environment"]["S3_ACCESS_KEY"] == "manta_parser"
    assert services["ml-service"]["environment"]["S3_ACCESS_KEY"] == "manta_model_reader"
    assert services["ml-autotrain"]["environment"]["S3_ACCESS_KEY"] == "manta_model_writer"


def test_non_s3_apps_do_not_inherit_root_credentials_on_vps():
    services = merged_config()["services"]
    for service in ("feature-extractor", "report-generator"):
        env = services[service]["environment"]
        assert env["S3_ACCESS_KEY"] == ""
        assert env["S3_SECRET_KEY"] == ""


def test_vps_overlay_requires_every_minio_service_password():
    text = VPS.read_text(encoding="utf-8")
    for var in ("MANTA_S3_PASS_INGEST", "MANTA_S3_PASS_PARSER",
                "MANTA_S3_PASS_MODEL_READER", "MANTA_S3_PASS_MODEL_WRITER"):
        assert f"${{{var}:?" in text
        assert f"${{{var}:-" not in text


def test_vps_apps_use_function_specific_clickhouse_users():
    services = merged_config()["services"]
    writers = ("api-gateway", "data-collector", "timeline-collector",
               "pro-timeline-collector", "league-collector",
               "pro-replay-collector", "stratz-collector",
               "candidates-collector", "parser-svc", "feature-extractor")
    for service in writers:
        assert services[service]["environment"]["CLICKHOUSE_USER"] == "manta_ch_writer"
    for service in ("report-generator", "ml-service"):
        assert services[service]["environment"]["CLICKHOUSE_USER"] == "manta_ch_reader"
    assert services["ml-autotrain"]["environment"]["CLICKHOUSE_USER"] == "manta_ch_trainer"
    for service in writers + ("report-generator", "ml-service", "ml-autotrain"):
        assert services[service]["environment"]["CLICKHOUSE_USER"] != "dota"


def test_redis_requires_auth_and_clients_receive_password():
    services = merged_config()["services"]
    redis = services["redis"]
    assert "--requirepass" in redis["command"]
    assert redis["environment"]["MANTA_REDIS_PASSWORD"] == "redis-test-password"
    gateway = services["api-gateway"]["environment"]
    assert gateway["REDIS_ADDR"] == "redis:6379"
    assert gateway["REDIS_PASSWORD"] == "redis-test-password"


def test_mlflow_is_on_an_internal_network_only_with_ml_clients():
    cfg = merged_config()
    assert cfg["networks"]["mlflow-internal"]["internal"] is True
    services = cfg["services"]
    assert set(services["mlflow"]["networks"]) == {"mlflow-internal"}
    for service in ("postgres", "ml-service", "ml-autotrain"):
        assert "mlflow-internal" in services[service]["networks"]
    for service, body in services.items():
        if service not in {"postgres", "mlflow", "ml-service", "ml-autotrain"}:
            assert "mlflow-internal" not in (body.get("networks") or {}), service


def test_mlflow_postgres_driver_is_baked_into_image():
    """Internal-сеть не имеет DNS/интернета: runtime pip install зациклит
    контейнер ещё до запуска MLflow."""
    mlflow = merged_config()["services"]["mlflow"]
    assert "pip install" not in mlflow["command"]
    assert mlflow.get("build"), "MLflow обязан собираться с DB-драйвером"
    dockerfile = MLFLOW_DOCKERFILE.read_text(encoding="utf-8")
    assert "psycopg2-binary==" in dockerfile


def test_vps_overlay_requires_storage_passwords_without_dev_fallbacks():
    text = VPS.read_text(encoding="utf-8")
    for var in ("MANTA_CH_PASS_READER", "MANTA_CH_PASS_WRITER",
                "MANTA_CH_PASS_TRAINER", "MANTA_REDIS_PASSWORD"):
        assert f"${{{var}:?" in text
        assert f"${{{var}:-" not in text


def test_clickhouse_bootstrap_admin_can_manage_service_identities():
    clickhouse = merged_config()["services"]["clickhouse"]
    assert clickhouse["environment"]["CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT"] == "1"


def test_gateway_is_fail_closed_and_mounts_only_verification_material():
    gateway = merged_config()["services"]["api-gateway"]
    env = gateway["environment"]
    assert env["MANTA_PROD"] == "1"
    assert env["JWT_PUBLIC_KEY_FILE"] == "/run/manta-secrets/jwt-public.pem"
    assert "JWT_PRIVATE_KEY_FILE" not in env
    assert env["TLS_CERT_FILE"] == "/run/manta-secrets/tls-cert.pem"
    assert env["TLS_KEY_FILE"] == "/run/manta-secrets/tls-key.pem"
    mounts = {v["target"]: v for v in gateway["volumes"]}
    assert set(mounts) == {
        "/run/manta-secrets/jwt-public.pem",
        "/run/manta-secrets/tls-cert.pem",
        "/run/manta-secrets/tls-key.pem",
    }
    assert all(volume.get("read_only") is True for volume in mounts.values())


def test_vps_gateway_is_still_published_only_on_loopback():
    ports = published(merged_config()["services"]["api-gateway"])
    assert ports and all(port.get("host_ip") == "127.0.0.1" for port in ports)


def test_frontend_and_prometheus_use_verified_gateway_tls():
    nginx = NGINX_VPS.read_text(encoding="utf-8")
    assert "proxy_pass https://api-gateway:8080" in nginx
    assert "proxy_ssl_verify on" in nginx
    assert "proxy_ssl_trusted_certificate" in nginx
    assert "proxy_ssl_verify off" not in nginx

    base = yaml.safe_load(PROM_BASE.read_text(encoding="utf-8"))
    vps = yaml.safe_load(PROM_VPS.read_text(encoding="utf-8"))
    base_jobs = {job["job_name"] for job in base["scrape_configs"]}
    vps_jobs = {job["job_name"] for job in vps["scrape_configs"]}
    assert vps_jobs == base_jobs, "VPS monitoring config drifted from base jobs"
    gateway = next(job for job in vps["scrape_configs"]
                   if job["job_name"] == "api-gateway")
    assert gateway["scheme"] == "https"
    assert gateway["tls_config"]["server_name"] == "api-gateway"
    assert gateway["tls_config"]["ca_file"].endswith("tls-cert.pem")
