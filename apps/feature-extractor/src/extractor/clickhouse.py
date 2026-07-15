"""Мини-клиент ClickHouse поверх HTTP (SELECT JSONEachRow / INSERT)."""
from __future__ import annotations

import json

import requests


class ClickHouse:
    def __init__(self, url: str, database: str, user: str, password: str,
                 timeout_s: float = 60.0):
        self.url = url
        self.database = database
        self.timeout_s = timeout_s
        self._headers = {
            "X-ClickHouse-User": user,
            "X-ClickHouse-Key": password,
        }

    def select(self, query: str, params: dict | None = None) -> list[dict]:
        """SELECT с серверными подстановками {name:Type} → список dict."""
        q = {"database": self.database, "default_format": "JSONEachRow"}
        for k, v in (params or {}).items():
            q[f"param_{k}"] = str(v)
        resp = requests.post(self.url, params=q, data=query,
                             headers=self._headers, timeout=self.timeout_s)
        resp.raise_for_status()
        return [json.loads(line) for line in resp.text.splitlines() if line]

    def insert_rows(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        resp = requests.post(
            self.url,
            params={"database": self.database,
                    "query": f"INSERT INTO {table} FORMAT JSONEachRow"},
            data=body.encode("utf-8"),
            headers=self._headers, timeout=self.timeout_s)
        resp.raise_for_status()
