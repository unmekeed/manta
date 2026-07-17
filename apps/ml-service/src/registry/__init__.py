"""Реестр моделей: собственный S3-бэкенд (по умолчанию) или MLflow.

Выбор — переменной окружения REGISTRY_BACKEND: "s3" (дефолт) | "mlflow".
Оба бэкенда реализуют один интерфейс push/promote/resolve/stage_metadata/
list_versions — вызывающий код от выбора не зависит.
"""
import os

from .store import MinioBackend, ModelRegistry
from .store import registry_from_env as _s3_registry_from_env


def registry_from_env():
    if os.getenv("REGISTRY_BACKEND", "s3").lower() == "mlflow":
        from .mlflow_store import mlflow_registry_from_env
        return mlflow_registry_from_env()
    return _s3_registry_from_env()


__all__ = ["ModelRegistry", "MinioBackend", "registry_from_env"]
