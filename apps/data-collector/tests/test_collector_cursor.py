"""Курсор коллектора не должен вставать на несобираемом матче.

Инцидент 2026-07-31: реплейный путь стоял 82ч при живых процессах и
нулевом лаге Kafka. Причина — курсор источника двигался ТОЛЬКО после
успешной публикации, а fetch_new отдаёт кандидатов старыми вперёд,
отбрасывая всё <= курсора. Матч, который нельзя собрать (битый bz2,
снятый с серверов Valve реплей, дубликат из общей CollectedMatches),
возвращался первым в каждом следующем цикле — очередь не двигалась.

Здесь проверяется, что каждая ветка пропуска сдвигает курсор.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.runner import MAX_TRANSIENT_RETRIES, Collector  # noqa: E402
from collector.sources import (MatchRef,  # noqa: E402
                               PermanentDownloadError)


class FakeSource:
    """Источник, повторяющий контракт OpenDotaSource: отдаёт кандидатов
    строго выше курсора, старые вперёд."""

    name = "fake"

    def __init__(self, match_ids, fail=None):
        self._ids = sorted(match_ids)
        self._fail = fail or {}
        self.downloads = []

    def fetch_new(self, after_cursor):
        floor = int(after_cursor) if after_cursor else 0
        for mid in self._ids:
            if mid > floor:
                yield MatchRef(match_id=mid, replay_url=f"http://x/{mid}.bz2",
                               tier="Professional", source_cursor=str(mid))

    def download_replay(self, ref):
        self.downloads.append(ref.match_id)
        exc = self._fail.get(ref.match_id)
        if exc:
            raise exc
        return b"PBDEMS2" + b"\0" * 64


def make_collector(source, collected=()):
    """Collector без реальных PG/Kafka/S3 — интересна только логика курсора."""
    c = Collector.__new__(Collector)
    c._source = source
    c._transient_fails = {}
    c._collected = set(collected)
    c.cursor = None
    c.published = []

    c._ensure_db = lambda: None
    c._get_cursor = lambda: c.cursor
    c._is_collected = lambda mid: mid in c._collected

    def advance(ref):
        c.cursor = ref.source_cursor
    c._advance_cursor = advance

    def mark(ref, url):
        c._collected.add(ref.match_id)
        c.cursor = ref.source_cursor
    c._mark_collected = mark

    class FakeS3:
        def put_object(self, *a, **kw): pass
    c._s3 = FakeS3()

    class FakeProducer:
        def produce(self, topic, key, value): c.published.append(key)
        def flush(self, t): pass
    c._producer = FakeProducer()

    class Cfg:
        s3_bucket = "replays"
    c._cfg = Cfg()
    return c


def test_permanent_failure_does_not_block_queue():
    """Битый реплей пропускается навсегда, следующий матч собирается."""
    src = FakeSource([100, 101], fail={
        100: PermanentDownloadError("битый bz2")})
    c = make_collector(src)

    assert c.collect_once() == 1          # 101 собран, 100 пропущен
    assert c.cursor == "101"
    # Второй цикл не должен снова упираться в 100.
    assert c.collect_once() == 0
    assert src.downloads == [100, 101]    # 100 не перекачивался


def test_duplicate_advances_cursor():
    """Матч, уже собранный ДРУГИМ источником (CollectedMatches общая),
    не должен держать курсор реплейного источника."""
    src = FakeSource([200, 201])
    c = make_collector(src, collected={200})

    assert c.collect_once() == 1
    assert c.cursor == "201"
    assert src.downloads == [201]         # дубликат не качался


def test_duplicate_only_cycle_still_advances():
    """Цикл целиком из дубликатов — курсор всё равно уходит вперёд,
    иначе следующий цикл получит ровно тот же список."""
    src = FakeSource([300, 301])
    c = make_collector(src, collected={300, 301})

    assert c.collect_once() == 0
    assert c.cursor == "301"
    assert list(src.fetch_new(c.cursor)) == []


def test_transient_failure_retries_then_gives_up():
    """Временный сбой повторяется, но не бесконечно: после
    MAX_TRANSIENT_RETRIES курсор уходит через матч."""
    src = FakeSource([400], fail={400: TimeoutError("сеть")})
    c = make_collector(src)

    for _ in range(MAX_TRANSIENT_RETRIES - 1):
        assert c.collect_once() == 0
        assert c.cursor is None           # ещё повторяем — курсор на месте
    assert c.collect_once() == 0
    assert c.cursor == "400"              # затор разомкнут
    assert len(src.downloads) == MAX_TRANSIENT_RETRIES


def test_transient_counter_resets_after_success():
    """Успех обнуляет счётчик: разовые сбои не накапливаются до сдвига."""
    src = FakeSource([500])
    c = make_collector(src)
    c._transient_fails[500] = MAX_TRANSIENT_RETRIES - 1

    assert c.collect_once() == 1
    assert 500 not in c._transient_fails


@pytest.mark.parametrize("exc", [
    PermanentDownloadError("нет реплея"),
    TimeoutError("сеть"),
])
def test_failed_match_never_marked_collected(exc):
    """Несобранный матч не попадает в CollectedMatches ни по одной ветке —
    иначе мы бы соврали, что он собран."""
    src = FakeSource([600], fail={600: exc})
    c = make_collector(src)
    for _ in range(MAX_TRANSIENT_RETRIES):
        c.collect_once()
    assert 600 not in c._collected
    assert c.published == []
