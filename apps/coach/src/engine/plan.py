"""LLM-Coach: план тренировки из отчётов + RAG-прецеденты (Гл. 3, 6.3).

Каркас двухслойный:

1. **Аналитический слой (этот модуль, без LLM)** — детерминированная
   агрегация MatchReports игрока: частоты классов ошибок (critical_death
   с SHAP-вкладами и Safety Index), слабые фазы, лейнинг/импакт. Выход —
   структурированные наблюдения + текстовый план по шаблонам. Это
   работает всегда и тестируется без внешних зависимостей.
2. **LLM-слой (llm.py)** — опциональная генерация связного текста поверх
   тех же наблюдений и RAG-контекста (похожие матчи из Similarity
   RetrieveContext). Подключается env-ключом; без ключа каркас честно
   отдаёт шаблонный план — деградация мягкая.

Наблюдение = (skill, priority, evidence): skill — что тренировать,
priority — насколько горит (1 — самое важное), evidence — почему
(частота, средние ΔWP, примеры матчей).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Observation:
    skill: str
    priority: int
    evidence: str
    resource_url: str = ""


# Классы навыков, выводимые из отчётов. resource_url — заглушки под
# будущие обучающие материалы (Гл. 6.3: контент-база).
SKILL_SAFETY = "позиционирование и map awareness"
SKILL_DEATHS = "контроль смертей в ключевые моменты"
SKILL_LANING = "стадия лейнинга"
SKILL_IMPACT = "реализация преимущества в командных боях"


def analyze_player(reports: list[dict], player_id: int) -> list[Observation]:
    """Наблюдения по игроку из его MatchReports (analysis-JSON отчётов).

    reports: [{"match_id", "analysis": {...}}] — analysis в формате
    report-generator (players[] с errors/impact_score/laning_score).
    """
    deaths = []          # (match_id, delta_wp, safety_index, drivers)
    laning_scores = []
    impact_scores = []
    n_matches = 0

    for rep in reports:
        players = (rep.get("analysis") or {}).get("players") or []
        me = next((p for p in players
                   if int(p.get("player_id", -1)) == int(player_id)), None)
        if me is None:
            continue
        n_matches += 1
        for err in me.get("errors") or []:
            if err.get("type") == "critical_death":
                deaths.append((int(rep.get("match_id", 0)),
                               float(err.get("delta_wp", 0.0)),
                               float(err.get("safety_index", 0.0)),
                               err.get("top_contributions") or []))
        if me.get("laning_score") is not None:
            laning_scores.append(float(me["laning_score"]))
        if me.get("impact_score") is not None:
            impact_scores.append(float(me["impact_score"]))

    if n_matches == 0:
        return []

    obs: list[Observation] = []

    deaths_per_match = len(deaths) / n_matches
    risky = [d for d in deaths if d[2] >= 0.6]
    total_wp_lost = -sum(d[1] for d in deaths)
    if deaths_per_match >= 1.0:
        examples = ", ".join(str(d[0]) for d in deaths[:3])
        obs.append(Observation(
            skill=SKILL_DEATHS, priority=1,
            evidence=(f"{deaths_per_match:.1f} критических смертей за матч "
                      f"(потеряно суммарно {total_wp_lost * 100:.0f}% WP за "
                      f"{n_matches} матчей); примеры: {examples}")))
    if risky and len(risky) / max(len(deaths), 1) >= 0.4:
        obs.append(Observation(
            skill=SKILL_SAFETY, priority=1 if deaths_per_match >= 1.5 else 2,
            evidence=(f"{len(risky)} из {len(deaths)} критических смертей — "
                      f"в рискованной позиции (Safety Index ≥ 0.6): глубоко "
                      f"без обзора/союзников")))

    if laning_scores:
        avg_lane = sum(laning_scores) / len(laning_scores)
        if avg_lane < 0.45:
            obs.append(Observation(
                skill=SKILL_LANING, priority=2,
                evidence=(f"средний laning score {avg_lane:.2f} < 0.45: "
                          f"линия проигрывается чаще, чем выигрывается "
                          f"({len(laning_scores)} матчей)")))

    if impact_scores:
        avg_imp = sum(impact_scores) / len(impact_scores)
        if avg_imp < 0.45:
            obs.append(Observation(
                skill=SKILL_IMPACT, priority=3,
                evidence=(f"средний impact score {avg_imp:.2f}: вклад в "
                          f"командные сдвиги WP ниже нейтрального")))

    obs.sort(key=lambda o: o.priority)
    return obs


def render_plan(obs: list[Observation], context_docs: list[str],
                player_id: int) -> str:
    """Шаблонный текстовый план (fallback без LLM и вход для LLM-слоя)."""
    if not obs:
        return (f"Игрок {player_id}: недостаточно данных для плана — "
                f"нет отчётов с этим игроком.")
    lines = [f"План тренировки для игрока {player_id}:"]
    for i, o in enumerate(obs, 1):
        lines.append(f"{i}. [{o.priority}] {o.skill} — {o.evidence}")
    if context_docs:
        lines.append("Похожие матчи для разбора:")
        lines.extend(f"  • {d}" for d in context_docs[:3])
    return "\n".join(lines)
