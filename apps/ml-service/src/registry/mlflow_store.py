"""Реестр моделей поверх MLflow Model Registry (Гл. 10.6, Фаза 4).

Интерфейс повторяет registry.store.ModelRegistry (push/promote/resolve/
stage_metadata/list_versions) — вызывающий код (auto-train, gRPC-сервер,
уведомления) не меняется, бэкенд выбирается переменной окружения:

    REGISTRY_BACKEND=mlflow  MLFLOW_TRACKING_URI=http://localhost:9600

Маппинг понятий:
- версия      → mlflow ModelVersion (номера «1», «2», …); наш semver-run
                идентификатор хранится тегом версии `manta_version`;
- push        → run в эксперименте manta-models + артефакты model.pkl и
                metadata.json + create_model_version;
- стейдж      → alias реестра (актуальный API MLflow: стейджи объявлены
                устаревшими в пользу alias'ов);
- promote     → set_registered_model_alias(name, stage, version);
- resolve     → get_model_version_by_alias | точный номер версии; артефакт
                скачивается из run'а версии.

Клиент — mlflow-skinny (лёгкая клиентская сборка); артефакты ходят через
сам tracking-сервер (--serve-artifacts), S3-креды клиенту не нужны.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class MlflowRegistry:
    EXPERIMENT = "manta-models"

    def __init__(self, tracking_uri: str):
        import mlflow
        from mlflow.tracking import MlflowClient

        self._mlflow = mlflow
        mlflow.set_tracking_uri(tracking_uri)
        self._client = MlflowClient(tracking_uri)
        exp = self._client.get_experiment_by_name(self.EXPERIMENT)
        self._exp_id = (exp.experiment_id if exp
                        else self._client.create_experiment(self.EXPERIMENT))

    # -- запись ---------------------------------------------------------------

    def push(self, name: str, artifact: bytes, metadata: dict,
             run_id: str | None = None) -> str:
        run_tag = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manta_version = f"{metadata.get('model_version', '0.0.0')}-{run_tag}"

        run = self._client.create_run(
            self._exp_id, run_name=f"{name}-{manta_version}")
        rid = run.info.run_id
        # Числовые метрики — в run (сравнимость в UI MLflow).
        for k, v in (metadata.get("metrics") or {}).items():
            if isinstance(v, (int, float)):
                self._client.log_metric(rid, k, float(v))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "model.pkl").write_bytes(artifact)
            (Path(tmp) / "metadata.json").write_text(
                json.dumps({**metadata, "registry_version": manta_version},
                           ensure_ascii=False))
            self._client.log_artifacts(rid, tmp)
        self._client.set_terminated(rid)

        try:
            self._client.create_registered_model(name)
        except Exception:  # noqa: BLE001 — уже существует
            pass
        source = self._client.get_run(rid).info.artifact_uri
        mv = self._client.create_model_version(name, source=source, run_id=rid)
        self._client.set_model_version_tag(name, mv.version,
                                           "manta_version", manta_version)
        return str(mv.version)

    def promote(self, name: str, version: str, stage: str = "production") -> None:
        self._client.set_registered_model_alias(name, stage, version)

    # -- чтение ---------------------------------------------------------------

    def _version_of(self, name: str, ref: str):
        """ref — alias (production/staging) или номер версии MLflow."""
        try:
            return self._client.get_model_version_by_alias(name, ref)
        except Exception:  # noqa: BLE001 — не alias; пробуем как номер
            try:
                return self._client.get_model_version(name, ref)
            except Exception as e:  # noqa: BLE001
                raise KeyError(f"{name}:{ref}") from e

    def resolve(self, name: str, ref: str = "production") -> tuple[bytes, dict]:
        mv = self._version_of(name, ref)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._client.download_artifacts(mv.run_id, "", tmp)
            artifact = (Path(root) / "model.pkl").read_bytes()
            metadata = json.loads((Path(root) / "metadata.json").read_text())
        return artifact, metadata

    def stage_metadata(self, name: str, stage: str = "production") -> dict | None:
        try:
            _, meta = self.resolve(name, stage)
            return meta
        except KeyError:
            return None

    def list_versions(self, name: str) -> list[str]:
        try:
            versions = self._client.search_model_versions(f"name='{name}'")
        except Exception:  # noqa: BLE001 — модели ещё нет
            return []
        return sorted((str(v.version) for v in versions), key=int)


def mlflow_registry_from_env() -> MlflowRegistry:
    return MlflowRegistry(os.getenv("MLFLOW_TRACKING_URI",
                                    "http://localhost:9600"))
