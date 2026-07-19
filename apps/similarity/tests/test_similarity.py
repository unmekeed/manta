"""Тесты Similarity Engine: эмбеддинги, индекс, gRPC-контракт."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine.embed import (EMBED_DIM, cosine_top_k, embed_match,
                          match_document)
from engine.index import MatchIndex


def _timeline(minutes=30, drift=100, radiant_win=1):
    """Синтетический таймлайн: линейный рост преимущества drift/мин."""
    return [{"game_time": t * 60,
             "networth_diff": drift * t,
             "networth_total": 12000 + t * 1000,
             "xp_diff": int(drift * t * 1.2),
             "kills_radiant": t // 3, "kills_dire": t // 5,
             "towers_diff": min(t // 10, 3),
             "radiant_win": radiant_win}
            for t in range(1, minutes + 1)]


def test_embed_shape_and_determinism():
    v = embed_match(_timeline(), [1, 2, 3, 4, 5], [10, 11, 12, 13, 14])
    assert v.shape == (EMBED_DIM,)
    v2 = embed_match(_timeline(), [1, 2, 3, 4, 5], [10, 11, 12, 13, 14])
    assert np.allclose(v, v2)
    # длина матча не меняет размерность (ресэмплинг)
    v3 = embed_match(_timeline(minutes=55), [1], [2])
    assert v3.shape == (EMBED_DIM,)


def test_similar_trajectories_closer_than_opposite():
    """Матч с тем же сюжетом ближе, чем зеркальный (проигрыш Radiant)."""
    base = embed_match(_timeline(drift=100), [1, 2], [3, 4])
    same = embed_match(_timeline(drift=110), [1, 2], [3, 4])
    opposite = embed_match(_timeline(drift=-100), [1, 2], [3, 4])

    def cos(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    assert cos(base, same) > cos(base, opposite)


def test_hero_overlap_increases_similarity():
    base = embed_match(_timeline(), [1, 2, 3, 4, 5], [10, 11, 12, 13, 14])
    same_draft = embed_match(_timeline(drift=95), [1, 2, 3, 4, 5],
                             [10, 11, 12, 13, 14])
    other_draft = embed_match(_timeline(drift=95), [20, 21, 22, 23, 24],
                              [30, 31, 32, 33, 34])

    def cos(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    assert cos(base, same_draft) > cos(base, other_draft)


def test_cosine_top_k_excludes_self():
    m = np.vstack([embed_match(_timeline(drift=d), [1], [2])
                   for d in (100, 90, -100)])
    hits = cosine_top_k(m[0], m, 2, exclude=0)
    assert [i for i, _ in hits] == [1, 2]      # сам матч исключён
    assert hits[0][1] > hits[1][1]


def test_match_document_plots():
    doc = match_document(42, _timeline(drift=400), ["npc_dota_hero_axe"],
                         ["npc_dota_hero_kez"])
    assert "Матч 42" in doc and "победа Radiant" in doc
    assert "доминация" in doc and "axe" in doc and "kez" in doc
    # камбэк: Radiant был глубоко позади, но victory
    tl = _timeline(drift=-400)
    for r in tl[-3:]:
        r["networth_diff"] = 5000
    doc2 = match_document(43, [{**r, "radiant_win": 1} for r in tl],
                          [], [])
    assert "камбэк" in doc2


def _fake_index():
    idx = MatchIndex("http://x", "db", "u", "p")
    tl = {100: _timeline(drift=100), 101: _timeline(drift=95),
          102: _timeline(drift=-120)}
    ids, vecs, docs = [], [], []
    for mid, rows in tl.items():
        ids.append(mid)
        vecs.append(embed_match(rows, [1, 2], [3, 4]))
        docs.append(match_document(mid, rows, ["npc_dota_hero_axe"], []))
    idx._ids = ids
    idx._pos = {m: i for i, m in enumerate(ids)}
    idx._matrix = np.vstack(vecs)
    idx._docs = docs
    return idx


def test_index_find_similar_and_context():
    idx = _fake_index()
    hits = idx.find_similar(100, 2)
    assert hits[0][0] == 101          # ближайший — тот же сюжет
    assert hits[0][1] > hits[1][1]
    with pytest.raises(KeyError):
        idx.find_similar(999, 2)

    q = idx.embedding_of(100)
    docs = idx.retrieve_context(q, 2)
    assert len(docs) == 2 and "Матч" in docs[0][0]
    with pytest.raises(ValueError):
        idx.retrieve_context(np.zeros(3), 2)


def test_grpc_contract():
    import grpc
    from serve import build_server
    from gen import services_pb2, services_pb2_grpc

    server, port = build_server(_fake_index(), 0)
    server.start()
    try:
        chan = grpc.insecure_channel(f"localhost:{port}")
        stub = services_pb2_grpc.SimilarityServiceStub(chan)

        res = stub.FindSimilar(services_pb2.SimilarityQuery(
            entity="match", reference_id=100, top_k=2))
        assert [h.id for h in res.hits] == [101, 102]
        assert res.hits[0].score > res.hits[1].score

        with pytest.raises(grpc.RpcError) as e:
            stub.FindSimilar(services_pb2.SimilarityQuery(
                entity="match", reference_id=999, top_k=2))
        assert e.value.code() == grpc.StatusCode.NOT_FOUND

        with pytest.raises(grpc.RpcError) as e2:
            stub.FindSimilar(services_pb2.SimilarityQuery(
                entity="player", reference_id=1, top_k=2))
        assert e2.value.code() == grpc.StatusCode.UNIMPLEMENTED

        idx = _fake_index()
        ctx = stub.RetrieveContext(services_pb2.ContextQuery(
            query_embedding=list(idx.embedding_of(100)), top_k=2))
        assert len(ctx.documents) == 2 and len(ctx.scores) == 2
        assert "Матч" in ctx.documents[0]

        with pytest.raises(grpc.RpcError) as e3:
            stub.RetrieveContext(services_pb2.ContextQuery(top_k=2))
        assert e3.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        server.stop(0)
