"""In-memory индекс матчей: витрины ClickHouse → эмбеддинги + документы.

Точный поиск (numpy dot) — на текущем масштабе (10^3–10^4 матчей)
миллисекунды. Интерфейс search/context сознательно узкий: замена на
FAISS/ANN при 10^5+ не тронет gRPC-слой.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import numpy as np
import requests

from .embed import cosine_top_k, embed_match, match_document

logger = logging.getLogger("similarity.index")


class MatchIndex:
    def __init__(self, ch_url: str, ch_db: str, ch_user: str, ch_password: str):
        self._ch = (ch_url, ch_db, ch_user, ch_password)
        self._lock = threading.Lock()
        self._ids: list[int] = []
        self._pos: dict[int, int] = {}
        self._matrix = np.zeros((0, 1))
        self._docs: list[str] = []

    # -- загрузка -------------------------------------------------------------

    def _select(self, query: str) -> list[dict]:
        url, db, user, pwd = self._ch
        resp = requests.post(
            url, params={"database": db, "default_format": "JSONEachRow"},
            data=query,
            headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": pwd},
            timeout=180)
        resp.raise_for_status()
        return [json.loads(line) for line in resp.text.splitlines() if line]

    def refresh(self) -> int:
        """Перечитать витрины и перестроить индекс; вернуть число матчей."""
        t0 = time.time()
        timeline_rows = self._select(
            "SELECT match_id, game_time, networth_diff, networth_total,"
            "       xp_diff, kills_radiant, kills_dire, towers_diff,"
            "       radiant_win"
            "  FROM MatchTimelineFeatures FINAL"
            " ORDER BY match_id, game_time")
        player_rows = self._select(
            "SELECT match_id, team, hero FROM PlayerMatchFeatures"
            " FINAL ORDER BY match_id, player_id")

        by_match: dict[int, list[dict]] = {}
        for r in timeline_rows:
            by_match.setdefault(int(r["match_id"]), []).append(r)
        # Словарь героев строится из самой витрины: сортированный список
        # имён → индексы слотов. Детерминирован в рамках одной перестройки,
        # а эмбеддинги сравниваются только внутри неё — этого достаточно.
        all_heroes = sorted({str(p.get("hero") or "") for p in player_rows
                             if p.get("hero")})
        hero_idx = {h: i for i, h in enumerate(all_heroes)}
        heroes: dict[int, dict[int, list]] = {}
        names: dict[int, dict[int, list]] = {}
        for p in player_rows:
            mid, team = int(p["match_id"]), int(p["team"])
            name = str(p.get("hero") or "")
            heroes.setdefault(mid, {2: [], 3: []}).setdefault(team, []).append(
                hero_idx.get(name, 0))
            names.setdefault(mid, {2: [], 3: []}).setdefault(team, []).append(name)

        ids, vecs, docs = [], [], []
        for mid, rows in by_match.items():
            h = heroes.get(mid, {2: [], 3: []})
            n = names.get(mid, {2: [], 3: []})
            ids.append(mid)
            vecs.append(embed_match(rows, h.get(2, []), h.get(3, [])))
            docs.append(match_document(mid, rows, n.get(2, []), n.get(3, [])))

        with self._lock:
            self._ids = ids
            self._pos = {mid: i for i, mid in enumerate(ids)}
            self._matrix = (np.vstack(vecs) if vecs else np.zeros((0, 1)))
            self._docs = docs
        logger.info("индекс перестроен: %d матчей за %.1fs",
                    len(ids), time.time() - t0)
        return len(ids)

    # -- поиск ----------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._ids)

    def find_similar(self, match_id: int, top_k: int) -> list[tuple[int, float]]:
        """Топ-k похожих матчей (id, score); KeyError, если матч не в индексе."""
        with self._lock:
            if match_id not in self._pos:
                raise KeyError(match_id)
            pos = self._pos[match_id]
            hits = cosine_top_k(self._matrix[pos], self._matrix, top_k,
                                exclude=pos)
            return [(self._ids[i], score) for i, score in hits]

    def retrieve_context(self, query: np.ndarray, top_k: int
                         ) -> list[tuple[str, float]]:
        """Топ-k документов по эмбеддингу запроса (для RAG)."""
        with self._lock:
            if self._matrix.shape[0] == 0:
                return []
            if len(query) != self._matrix.shape[1]:
                raise ValueError(
                    f"размер эмбеддинга {len(query)} != {self._matrix.shape[1]}")
            hits = cosine_top_k(np.asarray(query, dtype=float),
                                self._matrix, top_k)
            return [(self._docs[i], score) for i, score in hits]

    def embedding_of(self, match_id: int) -> np.ndarray:
        """Эмбеддинг матча из индекса (для склейки запросов RAG)."""
        with self._lock:
            if match_id not in self._pos:
                raise KeyError(match_id)
            return self._matrix[self._pos[match_id]].copy()
