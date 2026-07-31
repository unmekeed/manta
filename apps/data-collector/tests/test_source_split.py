"""Разделение кандидатов между источниками деталей одной машины.

Без него opendota-timeline и stratz-timeline читают ОДИН листинг с
вершины и каждый цикл берут одни и те же матчи. Дедуп по
CollectedMatches срабатывает лишь ПОСЛЕ отметки, а между проверкой и
отметкой лежит запрос деталей — в это окно оба успевают взять матч. Цена
ничьей не «лишний запрос»: витрина ReplacingMergeTree оставляет строку,
вставленную последней, и бедная строка STRATZ затирает строку OpenDota
вместе с фичами трека F.
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.sources import Shard, SourceSplit  # noqa: E402

IDS = list(range(8_921_000_000, 8_921_000_400))


def test_single_source_accepts_everything():
    """STRATZ не настроен — делить не с кем, фильтр прозрачен."""
    s = SourceSplit()
    assert all(s.accepts(m) for m in IDS)


def test_two_sources_never_take_the_same_match():
    a, b = SourceSplit(0, 2), SourceSplit(1, 2)
    both = [m for m in IDS if a.accepts(m) and b.accepts(m)]
    assert both == []


def test_two_sources_cover_everything():
    """Ни один кандидат не теряется: что не взял один, берёт другой."""
    a, b = SourceSplit(0, 2), SourceSplit(1, 2)
    assert all(a.accepts(m) or b.accepts(m) for m in IDS)


def test_split_is_roughly_even():
    a = [m for m in IDS if SourceSplit(0, 2).accepts(m)]
    assert 0.4 < len(a) / len(IDS) < 0.6


def test_split_is_independent_of_machine_shard():
    """Ключевое: деление берёт match_id // 10, а не остаток самого id.
    С общим делителем фильтры сцепились бы — машина №2 (нечётные id) не
    получала бы кандидатов для одного из источников вовсе."""
    shard2 = Shard(shard_id=1, count=2)          # ПК №2: нечётные
    mine = [m for m in IDS if shard2.accepts(m)]
    assert mine, "шард машины не должен быть пустым"
    for split_id in (0, 1):
        share = [m for m in mine if SourceSplit(split_id, 2).accepts(m)]
        assert share, f"источник {split_id} остался без кандидатов"
        assert 0.3 < len(share) / len(mine) < 0.7


def test_invalid_split_rejected():
    with pytest.raises(ValueError):
        SourceSplit(split_id=2, count=2)
    with pytest.raises(ValueError):
        SourceSplit(split_id=0, count=0)


# -- назначение долей в точке входа -------------------------------------------

def _split_for(name, token):
    from collector import __main__ as m
    old = os.environ.get("STRATZ_API_TOKEN")
    if token:
        os.environ["STRATZ_API_TOKEN"] = token
    else:
        os.environ.pop("STRATZ_API_TOKEN", None)
    try:
        return m._detail_split(name)
    finally:
        os.environ.pop("STRATZ_API_TOKEN", None)
        if old is not None:
            os.environ["STRATZ_API_TOKEN"] = old


def test_no_split_until_stratz_configured():
    """Без токена поведение не меняется вовсе — иначе включение STRATZ
    молча урезало бы вдвое поток уже работающего источника."""
    assert _split_for("opendota-timeline", None).count == 1


def test_sources_get_opposite_shares_when_stratz_on():
    od = _split_for("opendota-timeline", "t")
    st = _split_for("stratz-timeline", "t")
    assert (od.count, st.count) == (2, 2)
    assert od.split_id != st.split_id
    assert not any(od.accepts(m) and st.accepts(m) for m in IDS)


def test_pro_sources_split_too():
    """У pro-путей тот же листинг /proMatches и та же гонка."""
    od = _split_for("opendota-timeline-pro", "t")
    st = _split_for("stratz-timeline-pro", "t")
    assert od.split_id != st.split_id
