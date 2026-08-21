"""Живая проверка MinIO policies; включается MANTA_RUN_MINIO_INTEGRATION=1."""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICIES = ROOT / "infra" / "minio" / "policies"


def run(*args, check=True, env=None):
    return subprocess.run(args, check=check, capture_output=True, text=True,
                          env=env)


@pytest.mark.skipif(os.getenv("MANTA_RUN_MINIO_INTEGRATION") != "1",
                    reason="set MANTA_RUN_MINIO_INTEGRATION=1")
def test_allowed_and_forbidden_minio_operations():
    if not shutil.which("docker") or run("docker", "info", check=False).returncode:
        pytest.skip("Docker daemon unavailable")
    name = f"manta-minio-policy-test-{uuid.uuid4().hex[:8]}"
    root_password = "root-password-for-policy-test"
    try:
        run("docker", "run", "-d", "--name", name,
            "-e", "MINIO_ROOT_USER=dota",
            "-e", f"MINIO_ROOT_PASSWORD={root_password}",
            "-v", f"{POLICIES.resolve()}:/policies:ro",
            "minio/minio:latest", "server", "/data")
        for _ in range(60):
            if run("docker", "exec", name, "mc", "ready", "local",
                   check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("MinIO did not become ready")

        env = {**os.environ,
               "MINIO_CONTAINER": name,
               "MINIO_ROOT_PASSWORD": root_password,
               "MANTA_S3_PASS_INGEST": "ingest-password-test",
               "MANTA_S3_PASS_PARSER": "parser-password-test",
               "MANTA_S3_PASS_MODEL_READER": "reader-password-test",
               "MANTA_S3_PASS_MODEL_WRITER": "writer-password-test"}
        run("bash", str(ROOT / "scripts" / "create-minio-users.sh"), env=env)

        def mc(alias, user, password, *args, allowed=True):
            run("docker", "exec", name, "mc", "alias", "set", alias,
                "http://127.0.0.1:9000", user, password)
            result = run("docker", "exec", name, "mc", *args,
                         check=False)
            assert (result.returncode == 0) is allowed, result.stderr

        run("docker", "exec", name, "sh", "-c",
            "printf replay >/tmp/replay; printf model >/tmp/model")
        mc("ingest", "manta_ingest", "ingest-password-test",
           "cp", "/tmp/replay", "ingest/replays/ok")
        mc("ingest", "manta_ingest", "ingest-password-test",
           "cp", "/tmp/model", "ingest/models/denied", allowed=False)
        mc("writer", "manta_model_writer", "writer-password-test",
           "cp", "/tmp/model", "writer/models/model.pkl")
        mc("reader", "manta_model_reader", "reader-password-test",
           "cat", "reader/models/model.pkl")
        mc("reader", "manta_model_reader", "reader-password-test",
           "cp", "/tmp/model", "reader/models/denied", allowed=False)
    finally:
        run("docker", "rm", "-f", name, check=False)
