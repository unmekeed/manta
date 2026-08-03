"""Бэкфилл фич по сохранённому JSON (спринт 88).

Проверяется главное свойство инструмента: он ДОПИСЫВАЕТ, а не
переписывает. Витрина — общая для двух путей сбора, и колонки реплея
(position_advance, alive_diff) восстановить из JSON нельзя в принципе.
Бэкфилл, затирающий их нулями или NaN, уничтожил бы единственные
матчи с полным набором фич, и заметили бы это только на переобучении.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.backfill import Backfiller, fmt  # noqa: E402
from collector.timeline_runner import (MTF_COLUMNS,  # noqa: E402
                                       TimelineConfig)


class FakeStore:
    def __init__(self, matches):
        self._m = matches

    def get(self, mid):
        return self._m.get(mid)

    def iter_match_ids(self):
        return iter(self._m)


def _raw(mid=901, minutes=3):
    """Сырой JSON матча в форме, которую отдаёт OpenDota."""
    return {
        "match_id": mid,
        "radiant_win": True,
        "duration": minutes * 60,
        "objectives": [
            {"type": "CHAT_MESSAGE_FIRSTBLOOD", "time": 65, "player_slot": 0},
            {"type": "CHAT_MESSAGE_ROSHAN_KILL", "time": 125, "team": 2},
        ],
        "players": [
            {"player_slot": 0, "hero_id": 1, "level": 6,
             "obs_log": [{"time": 60, "x": 120, "y": 130}],
             "sen_log": [{"time": 70, "x": 121, "y": 131}],
             "purchase_log": [{"time": 90, "key": "blink"}],
             "runes_log": [{"time": 100, "key": "2"}]},
            {"player_slot": 128, "hero_id": 2, "level": 5,
             "obs_log": [], "sen_log": [], "purchase_log": [],
             "runes_log": []},
        ],
    }


def _row(mid, game_time, **over):
    """Строка витрины как TSV-поля, в порядке MTF_COLUMNS."""
    base = {c: "nan" for c in MTF_COLUMNS}
    base.update({
        "match_id": str(mid), "game_time": str(game_time),
        "networth_diff": "100", "xp_diff": "80",
        "kills_radiant": "1", "kills_dire": "0", "radiant_win": "1",
        "tier": "Premium", "patch": "60",
        "feature_version": "opendota-json@3",
    })
    base.update({k: str(v) for k, v in over.items()})
    return [base[c] for c in MTF_COLUMNS]


class FakeBackfiller(Backfiller):
    """Backfiller с подменённым ClickHouse: запоминает вставки."""

    def __init__(self, rows_by_match, matches):
        super().__init__(TimelineConfig(), FakeStore(matches))
        self._rows_by_match = rows_by_match
        self.inserted = []

    def _rows_of(self, match_id):
        return [list(r) for r in self._rows_by_match.get(match_id, [])]

    def _insert(self, table, columns, rows):
        # Проверку --dry-run повторяем ровно там же, где она стоит в
        # бою — в единственной точке записи. Если фейк её проглотит,
        # тест на dry-run будет проверять сам себя, а не инструмент.
        if not rows or self._dry:
            return
        self.inserted.append((table, columns, rows))


def _mtf(bf):
    for table, _cols, rows in bf.inserted:
        if table == "MatchTimelineFeatures":
            return rows
    return None


def test_replay_columns_survive_backfill():
    """Главная проверка: колонки реплея возвращаются байт в байт."""
    rows = [_row(901, 60, position_advance="0.25", alive_diff="2"),
            _row(901, 120, position_advance="-0.1", alive_diff="-1"),
            _row(901, 180, position_advance="0.5", alive_diff="0")]
    bf = FakeBackfiller({901: rows}, {901: _raw(901)})
    assert bf.process(901)
    out = _mtf(bf)
    pa = MTF_COLUMNS.index("position_advance")
    ad = MTF_COLUMNS.index("alive_diff")
    assert [r[pa] for r in out] == ["0.25", "-0.1", "0.5"]
    assert [r[ad] for r in out] == ["2", "-1", "0"]


def test_track_f_columns_are_filled():
    """Ради чего всё: пустые колонки трека F получают значения."""
    rows = [_row(901, 60), _row(901, 120), _row(901, 180)]
    bf = FakeBackfiller({901: rows}, {901: _raw(901)})
    assert bf.process(901)
    out = _mtf(bf)
    fb = MTF_COLUMNS.index("first_blood")
    ros = MTF_COLUMNS.index("roshan_diff")
    assert all(r[fb] != "nan" for r in out)
    assert all(r[ros] != "nan" for r in out)


def test_identity_columns_are_untouched():
    """match_id/game_time/исход/тир/патч не пересчитываются: в сыром JSON
    тира нет вовсе, а исход бэкфилл не имеет права переписывать."""
    rows = [_row(901, 60), _row(901, 120), _row(901, 180)]
    bf = FakeBackfiller({901: rows}, {901: _raw(901)})
    bf.process(901)
    out = _mtf(bf)
    for name, want in (("match_id", "901"), ("radiant_win", "1"),
                       ("tier", "Premium"), ("patch", "60"),
                       ("feature_version", "opendota-json@3")):
        i = MTF_COLUMNS.index(name)
        assert {r[i] for r in out} == {want}, name
    gt = MTF_COLUMNS.index("game_time")
    assert [r[gt] for r in out] == ["60", "120", "180"]


def test_match_absent_from_showcase_is_skipped():
    """Матч собран другой машиной кластера — вставлять его строки с нуля
    нельзя: неоткуда взять исход, тир и патч."""
    bf = FakeBackfiller({}, {901: _raw(901)})
    assert not bf.process(901)
    assert bf.inserted == []
    assert bf.stats["нет в витрине"] == 1


def test_match_absent_from_store_is_skipped():
    bf = FakeBackfiller({901: [_row(901, 60)]}, {})
    assert not bf.process(901)
    assert bf.inserted == []
    assert bf.stats["нет в хранилище"] == 1


def test_only_missing_skips_filled_matches():
    """Повторный прогон по всему хранилищу не должен переписывать то,
    что уже посчитано: 1100 лишних вставок ради нуля изменений."""
    rows = [_row(901, 60, roshan_diff="1"), _row(901, 120, roshan_diff="1")]
    bf = FakeBackfiller({901: rows}, {901: _raw(901)})
    assert not bf.process(901, only_missing=True)
    assert bf.stats["уже заполнены"] == 1
    assert bf.inserted == []


def test_only_missing_processes_partially_filled():
    """Хотя бы одна дырка — матч считается заново целиком: половинчатая
    колонка хуже пустой, её нечем отличить от честного значения."""
    rows = [_row(901, 60, roshan_diff="1"), _row(901, 120)]
    bf = FakeBackfiller({901: rows}, {901: _raw(901)})
    assert bf.process(901, only_missing=True)


def test_length_mismatch_skips_column_not_truncates(monkeypatch, caplog):
    """Расчёт вернул не то число минут — колонка пропускается целиком.

    Молчаливое усечение по zip() дало бы матч, у которого начало
    пересчитано, а хвост нет, и различить это в витрине нечем.
    """
    import collector.backfill as bfmod
    monkeypatch.setattr(bfmod, "all_minute_features",
                        lambda m, minutes: {"roshan_diff": [1.0],
                                            "first_blood": [1.0] * len(minutes)})
    rows = [_row(901, 60), _row(901, 120), _row(901, 180)]
    bf = FakeBackfiller({901: rows}, {901: _raw(901)})
    with caplog.at_level("WARNING"):
        assert bf.process(901)
    out = _mtf(bf)
    ros = MTF_COLUMNS.index("roshan_diff")
    fb = MTF_COLUMNS.index("first_blood")
    assert [r[ros] for r in out] == ["nan", "nan", "nan"]   # не тронута
    assert all(r[fb] == "1.0" for r in out)                 # посчитана
    assert "пропущена" in caplog.text


def test_dry_run_writes_nothing():
    rows = [_row(901, 60), _row(901, 120)]
    bf = FakeBackfiller({901: rows}, {901: _raw(901)})
    bf._dry = True
    bf.process(901)
    assert bf.inserted == []


def test_run_survives_broken_match():
    """Один битый матч не имеет права оборвать прогон по хранилищу."""
    rows = [_row(902, 60), _row(902, 120)]
    bf = FakeBackfiller({902: rows}, {901: None, 902: _raw(902)})

    orig = bf._rows_of

    def boom(mid):
        if mid == 901:
            raise RuntimeError("ClickHouse моргнул")
        return orig(mid)

    bf._rows_of = boom
    bf.run([901, 902])
    assert bf.stats["ошибок"] == 1
    assert bf.stats["обновлено"] == 1


def test_limit_stops_early():
    rows = {i: [_row(i, 60)] for i in range(910, 920)}
    bf = FakeBackfiller(rows, {i: _raw(i) for i in rows})
    bf.run(list(rows), limit=3)
    assert bf.stats["матчей"] == 3


@pytest.mark.parametrize("value,want", [
    (float("nan"), "nan"), (1.5, "1.5"), (3, "3"),
    (["a", "b"], "['a','b']"),
])
def test_fmt_matches_runner(value, want):
    """Формат обязан совпадать с тем, чем пишет коллектор: одна и та же
    таблица, и расхождение вылезло бы как порча типа при вставке."""
    assert fmt(value) == want
