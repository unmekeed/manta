"""Живые negative/positive checks; MANTA_RUN_STORAGE_INTEGRATION=1."""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run(*args, check=True, env=None):
    return subprocess.run(args, check=check, capture_output=True, text=True,
                          env=env)


def wait_for(container, *command):
    for _ in range(90):
        if run("docker", "exec", container, *command, check=False).returncode == 0:
            return
        time.sleep(1)
    pytest.fail(f"{container} did not become ready")


@pytest.mark.skipif(os.getenv("MANTA_RUN_STORAGE_INTEGRATION") != "1",
                    reason="set MANTA_RUN_STORAGE_INTEGRATION=1")
def test_allowed_and_forbidden_clickhouse_and_redis_operations():
    if not shutil.which("docker") or run("docker", "info", check=False).returncode:
        pytest.skip("Docker daemon unavailable")
    suffix = uuid.uuid4().hex[:8]
    ch = f"manta-clickhouse-policy-test-{suffix}"
    redis = f"manta-redis-auth-test-{suffix}"
    admin = "admin-password-for-policy-test"
    reader = "reader-password-for-policy-test"
    writer = "writer-password-for-policy-test"
    trainer = "trainer-password-for-policy-test"
    redis_password = "redis-password-for-policy-test"
    try:
        run("docker", "run", "-d", "--name", ch,
            "-e", "CLICKHOUSE_USER=dota", "-e", f"CLICKHOUSE_PASSWORD={admin}",
            "-e", "CLICKHOUSE_DB=manta",
            "-e", "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            "clickhouse/clickhouse-server:24.8")
        wait_for(ch, "clickhouse-client", "--user", "dota", "--password", admin,
                 "-q", "SELECT 1")
        env = {**os.environ, "CLICKHOUSE_CONTAINER": ch,
               "CLICKHOUSE_ADMIN_PASSWORD": admin,
               "MANTA_CH_PASS_READER": reader,
               "MANTA_CH_PASS_WRITER": writer,
               "MANTA_CH_PASS_TRAINER": trainer}
        run("bash", str(ROOT / "scripts" / "create-clickhouse-users.sh"), env=env)
        run("docker", "exec", ch, "clickhouse-client", "--user", "dota",
            "--password", admin, "-q",
            "CREATE TABLE manta.permission_probe (n UInt8) ENGINE=Memory")

        def ch_query(user, password, query, allowed=True):
            result = run("docker", "exec", ch, "clickhouse-client",
                         "--user", user, "--password", password,
                         "-q", query, check=False)
            assert (result.returncode == 0) is allowed, result.stderr

        ch_query("manta_ch_reader", reader, "SELECT count() FROM manta.permission_probe")
        ch_query("manta_ch_reader", reader, "INSERT INTO manta.permission_probe VALUES (1)", False)
        ch_query("manta_ch_writer", writer, "INSERT INTO manta.permission_probe VALUES (1)")
        ch_query("manta_ch_writer", writer, "DROP TABLE manta.permission_probe", False)
        ch_query("manta_ch_trainer", trainer, "SELECT count() FROM manta.permission_probe")
        ch_query("manta_ch_trainer", trainer, "INSERT INTO manta.permission_probe VALUES (2)", False)

        run("docker", "run", "-d", "--name", redis, "redis:7",
            "redis-server", "--requirepass", redis_password)
        wait_for(redis, "redis-cli", "-a", redis_password, "ping")
        unauth = run("docker", "exec", redis, "redis-cli", "ping", check=False)
        assert "NOAUTH" in unauth.stdout + unauth.stderr
        auth = run("docker", "exec", "-e", f"REDISCLI_AUTH={redis_password}",
                   redis, "redis-cli", "ping")
        assert auth.stdout.strip() == "PONG"
    finally:
        run("docker", "rm", "-f", ch, check=False)
        run("docker", "rm", "-f", redis, check=False)
