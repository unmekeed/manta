"""Тесты Coach: анализ отчётов, план, LLM-fallback, gRPC."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine.llm import TemplateLLM, llm_from_env
from engine.plan import (SKILL_DEATHS, SKILL_LANING, SKILL_SAFETY,
                         analyze_player, render_plan)


def _report(mid, player_id=3, errors=None, laning=0.5, impact=0.5):
    return {"match_id": mid, "analysis": {"players": [{
        "player_id": player_id,
        "errors": errors or [],
        "laning_score": laning,
        "impact_score": impact,
    }]}}


def _death(delta=-0.08, si=0.7):
    return {"type": "critical_death", "delta_wp": delta, "safety_index": si,
            "top_contributions": [{"feature": "networth_diff", "value": -0.5}]}


def test_analyze_flags_deaths_and_safety():
    reports = [_report(1, errors=[_death(), _death(si=0.8)]),
               _report(2, errors=[_death(si=0.1)])]
    obs = analyze_player(reports, 3)
    skills = [o.skill for o in obs]
    assert SKILL_DEATHS in skills          # 1.5 смерти/матч
    assert SKILL_SAFETY in skills          # 2 из 3 смертей рискованные
    assert obs[0].priority == 1
    assert "1.5" in next(o for o in obs if o.skill == SKILL_DEATHS).evidence


def test_analyze_flags_weak_laning():
    reports = [_report(i, laning=0.3) for i in range(1, 4)]
    obs = analyze_player(reports, 3)
    assert any(o.skill == SKILL_LANING for o in obs)


def test_analyze_quiet_player_and_missing():
    # хороший игрок: без ошибок, лейнинг/импакт выше порогов → пусто
    assert analyze_player([_report(1, laning=0.6, impact=0.6)], 3) == []
    # игрока нет в отчётах
    assert analyze_player([_report(1, player_id=7)], 3) == []


def test_render_plan_contains_evidence_and_context():
    obs = analyze_player([_report(1, errors=[_death(), _death()])], 3)
    text = render_plan(obs, ["матч 8900 (похожесть 0.91)"], 3)
    assert "План тренировки для игрока 3" in text
    assert "критических смертей" in text
    assert "Похожие матчи" in text and "8900" in text


def test_llm_fallback_without_keys(monkeypatch):
    monkeypatch.delenv("COACH_LLM_PROVIDER", raising=False)
    llm = llm_from_env()
    assert isinstance(llm, TemplateLLM)
    assert llm.generate("план") == "план"


def test_grpc_contract(monkeypatch):
    import grpc
    from serve_coach import build_server
    from gen import services_pb2, services_pb2_grpc

    server, port, svc = build_server("postgresql://fake", "localhost:1", 0)
    reports = [_report(1, errors=[_death(), _death()]),
               _report(2, errors=[_death()])]
    monkeypatch.setattr(svc, "_reports_for_player", lambda pid, limit=50: reports)
    monkeypatch.setattr(svc, "_similar_context", lambda mids: ["матч 42 (0.9)"])
    server.start()
    try:
        chan = grpc.insecure_channel(f"localhost:{port}")
        stub = services_pb2_grpc.RecommendationServiceStub(chan)
        plan = stub.BuildPlan(services_pb2.PlanRequest(player_id=3))
        assert len(plan.items) >= 1
        assert plan.items[0].priority == 1
        assert plan.items[0].resource_url.startswith("text:")
        assert "критических смертей" in plan.items[0].resource_url
        assert "матч 42" in plan.items[0].resource_url

        monkeypatch.setattr(svc, "_reports_for_player",
                            lambda pid, limit=50: [])
        with pytest.raises(grpc.RpcError) as e:
            stub.BuildPlan(services_pb2.PlanRequest(player_id=99))
        assert e.value.code() == grpc.StatusCode.NOT_FOUND
    finally:
        server.stop(0)
