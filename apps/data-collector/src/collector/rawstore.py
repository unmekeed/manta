"""Хранилище сырого JSON матчей в S3/MinIO (трек F, спринт 60).

Зачем. Квота OpenDota (~3000 запросов/сутки на IP) — главный дефицит
проекта: датасет копится неделями. При этом матч скачивается ЦЕЛИКОМ, а
извлекалась малая часть полей. Каждая новая фича означала бы «собрать всё
заново» — то есть недели ожидания.

Сохраняя исходный JSON один раз, мы получаем право пересчитать ЛЮБУЮ
будущую фичу по всей истории оффлайн, не потратив ни одного вызова API.
Объём умеренный: gzip-JSON распаршенного матча — обычно 30–150 КиБ, то
есть ~10 ГиБ на 100 тысяч матчей.

Хранилище опционально и никогда не должно ронять сбор: витрина первична.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import os

logger = logging.getLogger("collector.rawstore")


class RawMatchStore:
    def __init__(self, client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls) -> "RawMatchStore | None":
        """Собрать хранилище из окружения; None — выключено или S3 не
        поднялся (сбор продолжится, в лог уйдёт предупреждение)."""
        if os.getenv("RAW_MATCH_STORE", "1") == "0":
            return None
        try:
            from minio import Minio
            client = Minio(
                os.getenv("S3_ENDPOINT", "localhost:9500"),
                access_key=os.getenv("S3_ACCESS_KEY", "dota"),
                secret_key=os.getenv("S3_SECRET_KEY", "dota_dev_password"),
                secure=os.getenv("S3_USE_SSL", "false") == "true")
            bucket = os.getenv("RAW_MATCH_BUCKET", "matches-json")
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
            return cls(client, bucket)
        except Exception as e:  # noqa: BLE001 — хранилище опционально
            logger.warning("сырой JSON не сохраняется (S3 недоступен): %s", e)
            return None

    @staticmethod
    def key_for(match_id: int) -> str:
        # Префикс по миллионам — как партиционирование в ClickHouse: листинг
        # бакета на сотнях тысяч объектов иначе неудобен.
        return f"{match_id // 1_000_000}/{match_id}.json.gz"

    def put(self, match_id: int, raw: dict) -> None:
        body = gzip.compress(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode())
        self._client.put_object(
            self._bucket, self.key_for(match_id), io.BytesIO(body), len(body),
            content_type="application/gzip")

    def get(self, match_id: int) -> dict | None:
        """Прочитать сохранённый матч (бэкфилл новых фич)."""
        try:
            resp = self._client.get_object(self._bucket, self.key_for(match_id))
            try:
                return json.loads(gzip.decompress(resp.read()))
            finally:
                resp.close()
                resp.release_conn()
        except Exception:  # noqa: BLE001 — матча в хранилище нет
            return None

    def iter_match_ids(self):
        """match_id всех сохранённых матчей (для бэкфилла)."""
        for obj in self._client.list_objects(self._bucket, recursive=True):
            name = obj.object_name.rsplit("/", 1)[-1]
            if name.endswith(".json.gz"):
                try:
                    yield int(name[: -len(".json.gz")])
                except ValueError:
                    continue
