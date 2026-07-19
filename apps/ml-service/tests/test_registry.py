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

    def delete_bytes(self, key):
        self.objects.pop(key, None)


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


def test_cleanup_keeps_last_n_and_promoted():
    reg = ModelRegistry(FakeBackend())
    versions = [reg.push("wp", b"x", META, run_id=f"r{i}") for i in range(6)]
    reg.promote("wp", versions[0])       # исторический production
    reg.promote("wp", versions[5])       # текущий production
    deleted = reg.cleanup("wp", keep_last=2)
    # Удалены r1..r3: r0 защищён историей промоушенов, r4/r5 — последние две.
    assert deleted == [versions[1], versions[2], versions[3]]
    assert reg.list_versions("wp") == [versions[0], versions[4], versions[5]]
    # Защищённые версии остаются читаемыми, включая откат на историческую.
    assert reg.resolve("wp", versions[0])[0] == b"x"


def test_cleanup_sorts_by_run_id_not_semver():
    # «0.10.0» лексически младше «0.9.0» — хронология должна идти по run_id.
    reg = ModelRegistry(FakeBackend())
    old = reg.push("wp", b"o", {"model_version": "0.10.0"}, run_id="20260101T000000Z")
    new = reg.push("wp", b"n", {"model_version": "0.9.0"}, run_id="20260301T000000Z")
    assert reg.cleanup("wp", keep_last=1) == [old]
    assert reg.list_versions("wp") == [new]


def test_cleanup_zero_keep_protects_only_promoted():
    reg = ModelRegistry(FakeBackend())
    v1 = reg.push("wp", b"a", META, run_id="r1")
    v2 = reg.push("wp", b"b", META, run_id="r2")
    reg.promote("wp", v2)
    assert reg.cleanup("wp", keep_last=0) == [v1]
    assert reg.resolve("wp")[0] == b"b"


# -- MLflow-бэкенд (Гл. 10.6, Фаза 4) -----------------------------------------
# Тесты идут по локальному file-store MLflow (без сервера): семантика
# push/promote/resolve та же, что у live-сервера из compose.

def _mlflow_reg(tmp_path):
    import os
    os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"  # тесты; live идёт в сервер
    from registry.mlflow_store import MlflowRegistry
    return MlflowRegistry(f"file://{tmp_path}/mlruns")


def test_mlflow_push_resolve_roundtrip(tmp_path):
    reg = _mlflow_reg(tmp_path)
    v1 = reg.push("wp", b"artifact-1", {"model_version": "0.4.0",
                                        "metrics": {"brier_calibrated": 0.15}})
    assert v1 == "1"
    # стейджа ещё нет
    assert reg.stage_metadata("wp") is None
    reg.promote("wp", v1)
    art, meta = reg.resolve("wp", "production")
    assert art == b"artifact-1"
    assert meta["registry_version"].startswith("0.4.0-")
    assert meta["metrics"]["brier_calibrated"] == 0.15


def test_mlflow_promote_switches_and_rollback(tmp_path):
    reg = _mlflow_reg(tmp_path)
    v1 = reg.push("wp", b"old", {"model_version": "0.4.0"})
    v2 = reg.push("wp", b"new", {"model_version": "0.4.1"})
    assert reg.list_versions("wp") == ["1", "2"]
    reg.promote("wp", v2)
    assert reg.resolve("wp")[0] == b"new"
    # точная версия резолвится напрямую, откат = promote старой
    assert reg.resolve("wp", v1)[0] == b"old"
    reg.promote("wp", v1)
    assert reg.resolve("wp")[0] == b"old"


def test_mlflow_missing_raises_keyerror(tmp_path):
    import pytest
    reg = _mlflow_reg(tmp_path)
    with pytest.raises(KeyError):
        reg.resolve("nope", "production")
    assert reg.list_versions("nope") == []


def test_registry_backend_switch(tmp_path, monkeypatch):
    """registry_from_env выбирает бэкенд по REGISTRY_BACKEND."""
    from registry import registry_from_env
    from registry.mlflow_store import MlflowRegistry

    monkeypatch.setenv("REGISTRY_BACKEND", "mlflow")
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"file://{tmp_path}/mlruns")
    assert isinstance(registry_from_env(), MlflowRegistry)
