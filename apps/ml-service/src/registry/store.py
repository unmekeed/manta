"""Реестр моделей поверх S3/MinIO (Гл. 10.6: semver + run_id, стейджи).

Раскладка бакета `models`:

    {name}/versions/{version}/model.pkl        # артефакт joblib
    {name}/versions/{version}/metadata.json    # метрики, датасет, фичи
    {name}/stages/{stage}.json                 # указатель {"version": ...}

Версия = semver артефакта + run_id по UTC-времени запуска
(`0.1.0-20260715T043000Z`) — монотонна и человекочитаема. Продвижение
в стейдж (`production`) — атомарная перезапись одного JSON-указателя;
откат = повторный promote старой версии.

Полноценный MLflow Registry (Гл. 10) станет заменой этого модуля;
интерфейс push/promote/resolve сознательно повторяет его семантику.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from typing import Protocol


class Backend(Protocol):
    """Минимальный S3-контракт: реализуется MinIO и in-memory фейком."""

    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...          # KeyError если нет
    def list_keys(self, prefix: str) -> list[str]: ...
    def delete_bytes(self, key: str) -> None: ...        # идемпотентно


class MinioBackend:
    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str = "models", secure: bool = False):
        from minio import Minio  # импорт по месту: тесты живут на фейке
        from minio.error import S3Error
        self._s3err = S3Error
        self._client = Minio(endpoint, access_key=access_key,
                             secret_key=secret_key, secure=secure)
        self._bucket = bucket
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)

    def put_bytes(self, key: str, data: bytes) -> None:
        self._client.put_object(self._bucket, key, io.BytesIO(data), len(data))

    def get_bytes(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(self._bucket, key)
        except self._s3err as e:
            if e.code == "NoSuchKey":
                raise KeyError(key) from e
            raise
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def list_keys(self, prefix: str) -> list[str]:
        return [o.object_name for o in
                self._client.list_objects(self._bucket, prefix=prefix,
                                          recursive=True)]

    def delete_bytes(self, key: str) -> None:
        self._client.remove_object(self._bucket, key)


class ModelRegistry:
    def __init__(self, backend: Backend):
        self._b = backend

    # -- запись ---------------------------------------------------------------

    def push(self, name: str, artifact: bytes, metadata: dict,
             run_id: str | None = None) -> str:
        """Загрузить версию; вернуть её идентификатор."""
        base = str(metadata.get("model_version", "0.0.0"))
        run = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        version = f"{base}-{run}"
        prefix = f"{name}/versions/{version}"
        self._b.put_bytes(f"{prefix}/model.pkl", artifact)
        self._b.put_bytes(f"{prefix}/metadata.json",
                          json.dumps({**metadata, "registry_version": version},
                                     ensure_ascii=False).encode())
        return version

    def promote(self, name: str, version: str, stage: str = "production") -> None:
        # Указатель пишется только на существующую версию.
        self._b.get_bytes(f"{name}/versions/{version}/metadata.json")
        self._b.put_bytes(f"{name}/stages/{stage}.json",
                          json.dumps({"version": version}).encode())
        # История промоушенов: продвигавшиеся версии защищены от cleanup()
        # навсегда — по ним восстанавливается любой прод прошлого.
        hist_key = f"{name}/stages/{stage}_history.json"
        try:
            hist = json.loads(self._b.get_bytes(hist_key))
        except KeyError:
            hist = []
        if version not in hist:
            hist.append(version)
            self._b.put_bytes(hist_key, json.dumps(hist).encode())

    # -- чтение ---------------------------------------------------------------

    def resolve(self, name: str, ref: str = "production") -> tuple[bytes, dict]:
        """ref — стейдж (production/staging) или точная версия."""
        version = ref
        try:
            ptr = json.loads(self._b.get_bytes(f"{name}/stages/{ref}.json"))
            version = ptr["version"]
        except KeyError:
            pass  # ref — не стейдж; пробуем как версию
        prefix = f"{name}/versions/{version}"
        artifact = self._b.get_bytes(f"{prefix}/model.pkl")
        metadata = json.loads(self._b.get_bytes(f"{prefix}/metadata.json"))
        return artifact, metadata

    def stage_metadata(self, name: str, stage: str = "production") -> dict | None:
        """Метаданные текущей версии стейджа; None, если стейдж пуст."""
        try:
            _, meta = self.resolve(name, stage)
            return meta
        except KeyError:
            return None

    def list_versions(self, name: str) -> list[str]:
        prefix = f"{name}/versions/"
        seen = sorted({k[len(prefix):].split("/")[0]
                       for k in self._b.list_keys(prefix)})
        return seen

    # -- обслуживание ---------------------------------------------------------

    def cleanup(self, name: str, keep_last: int = 10) -> list[str]:
        """Удалить старые версии; вернуть список удалённых (D2 роадмапа).

        Защищены: keep_last последних по run_id (хронология, а не semver:
        «0.10.0-...» лексически младше «0.9.0-...») и все версии, когда-либо
        продвигавшиеся в любой стейдж (история из promote()).
        """
        protected: set[str] = set()
        for key in self._b.list_keys(f"{name}/stages/"):
            payload = json.loads(self._b.get_bytes(key))
            if key.endswith("_history.json"):
                protected.update(payload)
            else:
                protected.add(payload["version"])

        by_age = sorted(self.list_versions(name),
                        key=lambda v: v.rsplit("-", 1)[-1])
        protected.update(by_age[-keep_last:] if keep_last else [])

        deleted = []
        for version in by_age:
            if version in protected:
                continue
            for key in self._b.list_keys(f"{name}/versions/{version}/"):
                self._b.delete_bytes(key)
            deleted.append(version)
        return deleted


def registry_from_env() -> ModelRegistry:
    return ModelRegistry(MinioBackend(
        endpoint=os.getenv("S3_ENDPOINT", "localhost:9500"),
        access_key=os.getenv("S3_ACCESS_KEY", "dota"),
        secret_key=os.getenv("S3_SECRET_KEY", "dota_dev_password"),
        bucket=os.getenv("MODELS_BUCKET", "models"),
        secure=os.getenv("S3_USE_SSL", "") == "true",
    ))
