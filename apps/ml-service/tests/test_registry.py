"""Тесты реестра моделей на in-memory backend (без MinIO)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from registry.store import ModelRegistry


class FakeBackend:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key, data):
        self.objects[key] = data

    def get_bytes(self, key):
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def list_keys(self, prefix):
        return [k for k in self.objects if k.startswith(prefix)]


META = {"model_version": "0.1.0", "metrics": {"brier_calibrated": 0.10}}


def test_push_and_resolve_by_version():
    reg = ModelRegistry(FakeBackend())
    v = reg.push("wp", b"artifact-1", META, run_id="r1")
    assert v == "0.1.0-r1"
    data, meta = reg.resolve("wp", v)
    assert data == b"artifact-1"
    assert meta["registry_version"] == v
    assert meta["metrics"]["brier_calibrated"] == 0.10


def test_promote_and_resolve_stage():
    reg = ModelRegistry(FakeBackend())
    v1 = reg.push("wp", b"a1", META, run_id="r1")
    v2 = reg.push("wp", b"a2", META, run_id="r2")
    reg.promote("wp", v1)
    assert reg.resolve("wp")[0] == b"a1"
    reg.promote("wp", v2)  # перевод стейджа = перезапись указателя
    assert reg.resolve("wp")[0] == b"a2"
    # Откат — повторный promote старой версии.
    reg.promote("wp", v1)
    assert reg.stage_metadata("wp")["registry_version"] == v1


def test_promote_requires_existing_version():
    reg = ModelRegistry(FakeBackend())
    with pytest.raises(KeyError):
        reg.promote("wp", "0.1.0-nope")


def test_resolve_missing_raises_and_stage_metadata_none():
    reg = ModelRegistry(FakeBackend())
    with pytest.raises(KeyError):
        reg.resolve("wp", "production")
    assert reg.stage_metadata("wp") is None


def test_list_versions():
    reg = ModelRegistry(FakeBackend())
    reg.push("wp", b"x", META, run_id="r1")
    reg.push("wp", b"y", META, run_id="r2")
    assert reg.list_versions("wp") == ["0.1.0-r1", "0.1.0-r2"]
