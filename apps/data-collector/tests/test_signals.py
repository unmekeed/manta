"""Тесты извлечения игровых сигналов из JSON матча (трек F)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.signals import (KEY_ITEMS, RADIANT, DIRE, all_minute_features,
                               draft_row, event_rows, item_features,
                               neutral_level_features, objective_features,
                               team_of, vision_coverage,
                               vision_features)

MINUTES = [60, 120, 180, 240, 300, 360, 420, 480]


def _match(**over) -> dict:
    """Матч-фикстура: 10 игроков, слоты 0-4 Radiant, 128-132 Dire."""
    players = []
    for i in range(10):
        slot = i if i < 5 else 128 + (i - 5)
        players.append({"player_slot": slot, "hero_id": 1 + i,
                        "purchase_log": [], "runes_log": [], "obs_log": [],
                        "sen_log": [], "buyback_log": [], "xp_t": []})
    m = {"match_id": 900, "radiant_win": True, "patch": 60,
         "players": players, "objectives": []}
    m.update(over)
    return m


def test_team_of_slots():
    assert team_of(0) == RADIANT and team_of(4) == RADIANT
    assert team_of(128) == DIRE and team_of(132) == DIRE


def test_draft_row_compositions_and_bans():
    m = _match(picks_bans=[
        {"is_pick": True, "hero_id": 1, "team": 0},     # первый пик — Radiant
        {"is_pick": False, "hero_id": 14, "team": 1},   # бан
    ])
    d = draft_row(m)
    assert d is not None
    assert len(d["radiant_heroes"]) == 5 and len(d["dire_heroes"]) == 5
    assert d["radiant_heroes"][0] == "npc_dota_hero_antimage"  # hero_id 1
    assert d["first_pick_team"] == RADIANT
    assert d["bans"] == ["npc_dota_hero_pudge"]            # hero_id 14
    assert d["radiant_win"] == 1 and d["patch"] == 60


def test_draft_row_without_picks_bans_still_has_compositions():
    d = draft_row(_match())
    assert d is not None and d["bans"] == [] and d["first_pick_team"] == 0
    assert len(d["radiant_heroes"] + d["dire_heroes"]) == 10


def test_draft_row_rejects_incomplete_match():
    m = _match()
    m["players"] = m["players"][:9]
    assert draft_row(m) is None


def test_event_rows_objectives_and_logs():
    m = _match(objectives=[
        {"type": "CHAT_MESSAGE_ROSHAN_KILL", "time": 900, "team": 2},
        {"type": "CHAT_MESSAGE_AEGIS", "time": 905, "player_slot": 0},
        {"type": "CHAT_MESSAGE_FIRSTBLOOD", "time": 120, "player_slot": 128},
    ])
    m["players"][0]["buyback_log"] = [{"time": 1500}]
    m["players"][5]["runes_log"] = [{"time": 240, "key": 2}]
    m["players"][1]["obs_log"] = [{"time": 300, "x": 120, "y": 130}]

    evs = event_rows(m)
    kinds = {e["kind"] for e in evs}
    assert {"roshan", "aegis", "firstblood", "buyback", "rune",
            "ward_obs"} <= kinds
    rosh = next(e for e in evs if e["kind"] == "roshan")
    assert rosh["team"] == RADIANT and rosh["game_time"] == 900
    fb = next(e for e in evs if e["kind"] == "firstblood")
    assert fb["team"] == DIRE                      # slot 128 → Dire
    ward = next(e for e in evs if e["kind"] == "ward_obs")
    assert ward["x"] == 120 and ward["team"] == RADIANT


def test_objective_features_signs_and_accumulation():
    m = _match(objectives=[
        {"type": "CHAT_MESSAGE_ROSHAN_KILL", "time": 200, "team": 2},
        {"type": "CHAT_MESSAGE_ROSHAN_KILL", "time": 400, "team": 3},
        {"type": "CHAT_MESSAGE_FIRSTBLOOD", "time": 150, "player_slot": 0},
    ])
    f = objective_features(m, MINUTES)
    # roshan_diff: +1 после 200с, обратно в 0 после 400с (Dire забрал)
    assert f["roshan_diff"][MINUTES.index(180)] == 0.0
    assert f["roshan_diff"][MINUTES.index(240)] == 1.0
    assert f["roshan_diff"][MINUTES.index(420)] == 0.0
    # first_blood: 0 до события, +1 (Radiant) после и до конца
    assert f["first_blood"][MINUTES.index(120)] == 0.0
    assert f["first_blood"][MINUTES.index(180)] == 1.0
    assert f["first_blood"][-1] == 1.0


def test_aegis_expires_after_five_minutes():
    m = _match(objectives=[
        {"type": "CHAT_MESSAGE_AEGIS", "time": 120, "player_slot": 0}])
    f = objective_features(m, MINUTES)
    assert f["aegis_alive"][MINUTES.index(180)] == 1.0    # держит
    assert f["aegis_alive"][MINUTES.index(480)] == 0.0    # истёк (>300с)


def test_buybacks_diff_counts_both_sides():
    m = _match()
    m["players"][0]["buyback_log"] = [{"time": 100}]      # Radiant
    m["players"][5]["buyback_log"] = [{"time": 100}, {"time": 200}]  # Dire
    f = objective_features(m, MINUTES)
    assert f["buybacks_diff"][MINUTES.index(120)] == 0.0   # 1 и 1
    assert f["buybacks_diff"][MINUTES.index(240)] == -1.0  # Dire потратил 2


def test_vision_wards_active_not_cumulative():
    m = _match()
    # Обс-вард Radiant в 60с, снят в 180с; сентри Dire в 120с.
    m["players"][0]["obs_log"] = [{"time": 60}]
    m["players"][0]["obs_left_log"] = [{"time": 180}]
    m["players"][5]["sen_log"] = [{"time": 120}]
    f = vision_features(m, MINUTES)
    assert f["obs_wards_diff"][MINUTES.index(120)] == 1.0   # активен
    assert f["obs_wards_diff"][MINUTES.index(240)] == 0.0   # снят
    assert f["sen_wards_diff"][MINUTES.index(240)] == -1.0  # накопительно


def test_obs_ward_expires_after_six_minutes():
    m = _match()
    m["players"][0]["obs_log"] = [{"time": 60}]            # без obs_left_log
    f = vision_features(m, MINUTES)
    assert f["obs_wards_diff"][MINUTES.index(300)] == 1.0
    assert f["obs_wards_diff"][MINUTES.index(480)] == 0.0  # 60+360 = 420


def test_item_features_cost_and_key_items():
    m = _match()
    m["players"][0]["purchase_log"] = [
        {"time": 100, "key": "blink"},               # 2250, ключевой
        {"time": 200, "key": "tango"},               # дешёвый, не ключевой
    ]
    m["players"][5]["purchase_log"] = [{"time": 150, "key": "black_king_bar"}]
    f = item_features(m, MINUTES)
    # После 150с: Radiant 2250 − Dire 4050 < 0
    assert f["item_value_diff"][MINUTES.index(180)] < 0
    # key_items: blink(+1) и bkb(−1) → 0
    assert f["key_items_diff"][MINUTES.index(180)] == 0.0
    assert f["key_items_diff"][MINUTES.index(120)] == 1.0   # только blink
    assert "blink" in KEY_ITEMS and "tango" not in KEY_ITEMS


def test_levels_diff_from_xp():
    m = _match()
    # Radiant-игрок с большим опытом, Dire — с нулевым.
    m["players"][0]["xp_t"] = [0, 700, 1200, 2000, 3000, 4000, 5000, 6000, 7000]
    m["players"][5]["xp_t"] = [0, 0, 0, 0, 0, 0, 0, 0, 0]
    f = neutral_level_features(m, MINUTES)
    assert f["levels_diff"][MINUTES.index(60)] > 0     # Radiant впереди
    assert f["levels_diff"][-1] > f["levels_diff"][0]  # разрыв растёт


def test_all_minute_features_returns_every_key():
    f = all_minute_features(_match(), MINUTES)
    for key in ("roshan_diff", "aegis_alive", "buybacks_diff", "first_blood",
                "item_value_diff", "key_items_diff", "obs_wards_diff",
                "sen_wards_diff", "runes_diff", "neutral_tier_diff",
                "levels_diff"):
        assert key in f, f"нет фичи {key}"
        assert len(f[key]) == len(MINUTES)


def test_broken_match_does_not_crash():
    """Битый JSON не должен ронять сбор — группа фич просто пропускается."""
    f = all_minute_features({"match_id": 1, "players": "не список"}, MINUTES)
    assert isinstance(f, dict)


# -- Площадь под обзором (волна 1, спринт 90) ---------------------------------

def _ward_match(radiant_wards, dire_wards, mid=555):
    """Матч с заданными обс-вардами: списки (time, x, y)."""
    def player(slot, wards):
        return {"player_slot": slot, "hero_id": 1,
                "obs_log": [{"time": t, "x": x, "y": y}
                            for t, x, y in wards],
                "obs_left_log": [], "sen_log": [], "runes_log": [],
                "purchase_log": []}
    return {"match_id": mid, "radiant_win": True,
            "players": [player(0, radiant_wards), player(128, dire_wards)]}


def test_spread_wards_beat_clustered_at_equal_count():
    """Ради чего фича заводится: счётчик obs_wards_diff не различает
    три варда в одном лесу и три варда по карте, площадь — различает."""
    spread = _ward_match([(60, 80, 80), (60, 128, 128), (60, 170, 170)], [])
    clustered = _ward_match([(60, 80, 80), (60, 82, 81), (60, 81, 83)], [])
    a = vision_coverage(spread, [60])["vision_coverage_diff"][0]
    b = vision_coverage(clustered, [60])["vision_coverage_diff"][0]
    assert a > b, "разнесённые варды обязаны давать больше площади"


def test_overlap_is_union_not_sum():
    """Два варда в одной точке накрывают ровно столько же, сколько один."""
    one = _ward_match([(60, 128, 128)], [])
    two = _ward_match([(60, 128, 128), (60, 128, 128)], [])
    assert (vision_coverage(one, [60])["vision_coverage_diff"][0]
            == vision_coverage(two, [60])["vision_coverage_diff"][0])


def test_symmetric_wards_give_zero():
    """Одинаковое покрытие сторон — ноль, как у любой diff-фичи."""
    m = _ward_match([(60, 100, 100)], [(60, 150, 150)])
    assert vision_coverage(m, [60])["vision_coverage_diff"][0] == 0.0


def test_sign_follows_radiant():
    """Знак согласован с networth_diff: плюс — в пользу Radiant."""
    m = _ward_match([(60, 100, 100)], [])
    assert vision_coverage(m, [60])["vision_coverage_diff"][0] > 0
    m = _ward_match([], [(60, 100, 100)])
    assert vision_coverage(m, [60])["vision_coverage_diff"][0] < 0


def test_ward_expires_after_lifetime():
    """Вард живёт 360 секунд: на 8-й минуте его уже нет."""
    m = _ward_match([(60, 100, 100)], [])
    got = vision_coverage(m, [120, 480])["vision_coverage_diff"]
    assert got[0] > 0 and got[1] == 0.0


def test_ward_removed_early_stops_counting():
    """Снятый вард перестаёт давать обзор с момента снятия, а не через
    свои 6 минут — иначе сентри-война не отражалась бы в фиче вовсе."""
    m = _ward_match([(60, 100, 100)], [])
    m["players"][0]["obs_left_log"] = [{"time": 130}]
    got = vision_coverage(m, [120, 180])["vision_coverage_diff"]
    assert got[0] > 0 and got[1] == 0.0


def test_no_ward_coordinates_leaves_nan():
    """У матчей STRATZ координат нет вовсе. Ноль означал бы «видят
    одинаково» — ложный сигнал; колонка обязана остаться пустой."""
    m = _ward_match([], [])
    assert vision_coverage(m, [60, 120]) == {}
    m2 = _ward_match([(60, None, None)], [])
    assert vision_coverage(m2, [60]) == {}


def test_one_sided_wards_are_not_nan():
    """Обратная сторона: если варды есть хоть у кого-то, ноль у второй
    стороны честен и колонку прятать нельзя."""
    m = _ward_match([(60, 100, 100)], [])
    assert "vision_coverage_diff" in vision_coverage(m, [60])


def test_value_stays_in_unit_range():
    m = _ward_match([(60, 70 + 5 * i, 70 + 5 * i) for i in range(20)], [])
    v = vision_coverage(m, [60])["vision_coverage_diff"][0]
    assert 0.0 < v <= 1.0


def test_included_in_all_minute_features():
    m = _ward_match([(60, 100, 100)], [(60, 150, 150)])
    assert "vision_coverage_diff" in all_minute_features(m, [60, 120])


def test_length_matches_minutes():
    """Бэкфилл пропускает колонку при несовпадении длины — ряд обязан
    быть ровно по сетке минут."""
    m = _ward_match([(60, 100, 100)], [])
    minutes = [60, 120, 180, 240]
    assert len(vision_coverage(m, minutes)["vision_coverage_diff"]) == 4
