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
from collector.parked import UnreachableHosts  # noqa: E402
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

    # Заглушки повторяют НАСТОЯЩИЙ интерфейс, а не удобный (спринт 194).
    # Заглушка, которая проще боевого клиента, проверяет несуществующую
    # систему: ровно так тест сторожа в 193-м был зелёным три месяца,
    # потому что его заглушка не красила вывод.
    class FakeS3:
        """MinIO: put_object кладёт, stat_object бросает, если ключа нет."""

        def __init__(self):
            self.objects = set()

        def put_object(self, bucket, key, *a, **kw):
            self.objects.add(key)

        def stat_object(self, bucket, key):
            if key not in self.objects:
                raise RuntimeError("NoSuchKey")
            return object()

    c._s3 = FakeS3()

    class FakeProducer:
        """confluent_kafka.Producer: on_delivery обязателен, flush → int.

        `flush` возвращает число сообщений, ОСТАВШИХСЯ в очереди. Ноль
        значит «всё доставлено»; заглушка, возвращавшая None, делала
        проверку доставки бессмысленной.
        """

        def __init__(self):
            self.fail = None       # текст ошибки доставки или None
            self.stuck = 0         # сколько сообщений «зависнет» в очереди

        def produce(self, topic, key, value, on_delivery=None):
            c.published.append(key)
            if on_delivery is not None:
                on_delivery(self.fail, None)

        def flush(self, timeout=None):
            return self.stuck

    c._producer = FakeProducer()

    class Cfg:
        s3_bucket = "replays"
    c._cfg = Cfg()

    # Парковка (спринт 153). Настоящая — в Postgres; здесь список, чтобы
    # проверять ЧТО и КОГДА туда уходит, не поднимая базы. Семантику
    # самих запросов проверяет tests/test_dedup_sql.py на живой базе:
    # фейк на её месте проверял бы сам себя.
    c.parked = []
    c._park = lambda ref, reason: c.parked.append((ref.match_id, reason))
    c._unreachable = UnreachableHosts(threshold=2, ttl_s=3600,
                                      clock=lambda: 0.0)
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


# -- подтверждённая публикация (спринт 194) ------------------------------------
#
# ЖИВОЙ ОТКАЗ 3-6 сентября 2026. Топики Kafka исчезли, и коллектор трое
# суток качал реплеи, клал их в S3, ПОМЕЧАЛ МАТЧИ СОБРАННЫМИ — а события
# пропадали. Ни ошибки, ни строчки в логе: `flush()` возвращает число
# оставшихся в очереди сообщений, и этот возврат никто не смотрел.
#
# Матч, помеченный собранным, больше не берётся никогда, поэтому трое
# суток реплеев выпали из обработки насовсем. Здесь проверяется, что
# недоставка — это НЕуспех.


def test_undelivered_event_does_not_mark_the_match_collected():
    """ГЛАВНОЕ: не доставили — значит не собрали.

    Пометка и сдвиг курсора живут в одной транзакции, поэтому непомеченный
    матч вернётся следующим циклом сам. Обратное поведение (пометить и
    идти дальше) теряет матч навсегда и выглядит при этом как успех.
    """
    src = FakeSource([100])
    c = make_collector(src)
    c._producer.stuck = 1          # брокер не подтвердил запись

    assert c.collect_once() == 0, "матч засчитан при недоставленном событии"
    assert 100 not in c._collected, "матч помечен собранным без публикации"
    assert c.cursor is None, "курсор сдвинут мимо непубликованного матча"


def test_delivery_error_is_also_a_failure():
    """Ошибка доставки от брокера — не успех, даже если очередь пуста.

    Два разных признака: `flush` говорит «сколько осталось», колбэк —
    «что пошло не так с отправленным». Проверять только первый значит
    пропустить сообщение, которое брокер ОТВЕРГ.
    """
    src = FakeSource([100])
    c = make_collector(src)
    c._producer.fail = "UNKNOWN_TOPIC_OR_PART"

    assert c.collect_once() == 0
    assert 100 not in c._collected


def test_the_match_returns_on_the_next_cycle():
    """Непомеченный матч берётся снова, когда брокер ожил.

    Без этого «не помечать» означало бы просто терять матч тише.
    """
    src = FakeSource([100])
    c = make_collector(src)
    c._producer.stuck = 1
    assert c.collect_once() == 0

    c._producer.stuck = 0          # Kafka вернулась
    assert c.collect_once() == 1
    assert 100 in c._collected


def test_a_stored_replay_is_not_downloaded_twice():
    """Повторный заход не качает файл заново — он уже в хранилище.

    Реплей весит 60-90 МиБ. Без этой ветки каждая неудача доставки
    стоила бы ещё одной такой закачки, и починка Kafka на час означала бы
    гигабайты лишнего трафика.
    """
    src = FakeSource([100])
    c = make_collector(src)
    c._producer.stuck = 1
    c.collect_once()
    assert src.downloads == [100], "первый заход обязан скачать"

    c._producer.stuck = 0
    assert c.collect_once() == 1
    assert src.downloads == [100], "реплей скачан повторно, хотя лежал в S3"
    assert 100 in c._collected


def test_successful_delivery_still_marks_the_match():
    """Обратная сторона: исправная доставка ничего не ломает."""
    src = FakeSource([100, 101])
    c = make_collector(src)
    assert c.collect_once() == 2
    assert c._collected == {100, 101}
