"""Unit-тесты Data Collector: конверт, курсорная логика источников."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.runner import build_envelope  # noqa: E402
from collector.sources import MatchRef  # noqa: E402
from collector.sources.fixture import FixtureSource  # noqa: E402

SCHEMA_PATH = (pathlib.Path(__file__).resolve().parents[3]
               / "libs" / "schemas" / "event-envelope.schema.json")


def _ref(match_id: int = 8000000001) -> MatchRef:
    return MatchRef(match_id=match_id,
                    replay_url=f"fixture://replays/{match_id}.dem",
                    tier="Pub", source_cursor=str(match_id))


def test_envelope_matches_schema_required_fields():
    schema = json.loads(SCHEMA_PATH.read_text())
    env = build_envelope(_ref(), "s3://replays/fixture/x.dem", "fixture")
    missing = set(schema["required"]) - set(env)
    assert not missing, f"missing required fields: {missing}"
    assert env["event_type"] in schema["properties"]["event_type"]["enum"]
    assert len(env["trace_id"]) == 32
    assert env["partition_key"] == "match_id:8000000001"
    assert env["payload"]["source"] == "fixture"


def test_envelope_ids_are_unique():
    a = build_envelope(_ref(), "s3://x", "fixture")
    b = build_envelope(_ref(), "s3://x", "fixture")
    assert a["event_id"] != b["event_id"]


def test_fixture_source_respects_cursor():
    src = FixtureSource()
    all_matches = list(src.fetch_new(None))
    assert len(all_matches) == 3

    after_first = list(src.fetch_new(all_matches[0].source_cursor))
    assert len(after_first) == 2
    assert all(m.match_id > all_matches[0].match_id for m in after_first)

    after_last = list(src.fetch_new(all_matches[-1].source_cursor))
    assert after_last == []


def test_fixture_replay_has_source2_magic():
    src = FixtureSource()
    ref = next(iter(src.fetch_new(None)))
    data = src.download_replay(ref)
    assert data.startswith(b"PBDEMS2")
    assert len(data) > 1024
