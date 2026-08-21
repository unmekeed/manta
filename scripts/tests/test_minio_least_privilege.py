"""Статические границы MinIO для VPS (спринт 160)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICIES = ROOT / "infra" / "minio" / "policies"
USERS = ROOT / "scripts" / "create-minio-users.sh"


def policy(name: str) -> dict:
    return json.loads((POLICIES / f"{name}.json").read_text(encoding="utf-8"))


def resources(name: str) -> set[str]:
    return {item for statement in policy(name)["Statement"]
            for item in statement["Resource"]}


def actions(name: str) -> set[str]:
    return {item for statement in policy(name)["Statement"]
            for item in statement["Action"]}


def test_ingest_policy_cannot_access_model_registry():
    assert all("models" not in resource for resource in resources("manta-ingest"))
    assert resources("manta-ingest") == {
        "arn:aws:s3:::replays", "arn:aws:s3:::replays/*",
        "arn:aws:s3:::matches-json", "arn:aws:s3:::matches-json/*",
    }


def test_runtime_model_reader_cannot_publish_or_delete():
    reader = actions("manta-model-reader")
    assert "s3:GetObject" in reader
    assert "s3:PutObject" not in reader
    assert "s3:DeleteObject" not in reader


def test_only_model_writer_policy_can_write_models():
    assert "s3:PutObject" in actions("manta-model-writer")
    for name in ("manta-ingest", "manta-parser", "manta-model-reader"):
        model_resources = [r for r in resources(name) if "models" in r]
        if model_resources:
            assert "s3:PutObject" not in actions(name)


def test_provisioner_maps_each_user_to_one_policy():
    text = USERS.read_text(encoding="utf-8")
    for user, policy_name in (
        ("manta_ingest", "manta-ingest"),
        ("manta_parser", "manta-parser"),
        ("manta_model_reader", "manta-model-reader"),
        ("manta_model_writer", "manta-model-writer"),
    ):
        assert f"create_user {user}" in text
        assert policy_name in text


def test_provisioner_is_idempotent_for_existing_users_and_policies():
    text = USERS.read_text(encoding="utf-8")
    assert "admin user info" in text
    assert "admin policy create" in text
