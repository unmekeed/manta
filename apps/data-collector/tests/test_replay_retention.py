"""Тесты правила удаления реплеев из S3 (спринт 138).

До этого спринта .dem не удалял НИКТО: ни политики на бакете, ни вызова
remove_object во всём монорепо. Дома это терпимо — матчей мало. При цели
2000 матчей в сутки это 113 ГиБ в день и 3.4 ТБ в месяц, то есть
переполнение диска VPS на первой же неделе.

Проверяется не «MinIO умеет lifecycle» (это его забота), а наше
поведение: правило объявляется, повторный запуск его не дублирует, а
отказ S3 не роняет сбор — но и не остаётся незамеченным.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector import runner  # noqa: E402


class FakeS3:
    def __init__(self, existing=None, fail_set=False, fail_get=False):
        self.config = existing
        self.set_calls = []
        self._fail_set = fail_set
        self._fail_get = fail_get

    def get_bucket_lifecycle(self, bucket):
        if self._fail_get:
            raise RuntimeError("не поддерживается")
        return self.config

    def set_bucket_lifecycle(self, bucket, config):
        if self._fail_set:
            raise RuntimeError("отказано")
        self.config = config
        self.set_calls.append(config)


def test_rule_is_declared_when_absent():
    s3 = FakeS3(existing=None)
    runner._ensure_replay_retention(s3, "replays")
    assert len(s3.set_calls) == 1
    rule = s3.set_calls[0].rules[0]
    assert rule.expiration.days == runner.REPLAY_RETENTION_DAYS
    assert rule.status == "Enabled"


def test_rule_covers_the_whole_bucket():
    """Пустой префикс — иначе правило не тронет ничего.

    Ключи складываются как «<источник>/<match_id>.dem», источников семь,
    и правило с конкретным префиксом чистило бы один из них.
    """
    s3 = FakeS3(existing=None)
    runner._ensure_replay_retention(s3, "replays")
    assert s3.set_calls[0].rules[0].rule_filter.prefix == ""


def test_existing_rule_is_not_overwritten():
    """Повторный запуск не трогает уже настроенное.

    Коллекторов семь, каждый вызывает это при старте. Перезапись на
    каждом старте затирала бы правило, поправленное вручную.
    """
    class Existing:
        rules = ["что-то уже есть"]

    s3 = FakeS3(existing=Existing())
    runner._ensure_replay_retention(s3, "replays")
    assert s3.set_calls == []


def test_unsupported_lifecycle_does_not_break_collection(caplog):
    """S3 без поддержки lifecycle — не повод не собирать матчи.

    Но и молчать нельзя: без правила диск кончится, и знать об этом надо
    заранее, а не по «no space left on device».
    """
    s3 = FakeS3(existing=None, fail_set=True)
    with caplog.at_level("WARNING"):
        runner._ensure_replay_retention(s3, "replays")
    assert "следи за диском" in caplog.text


def test_missing_rule_is_read_as_absent_not_fatal():
    """get_bucket_lifecycle падает, когда правила нет — это норма."""
    s3 = FakeS3(existing=None, fail_get=True)
    runner._ensure_replay_retention(s3, "replays")
    assert len(s3.set_calls) == 1


def test_retention_can_be_disabled_but_says_so(monkeypatch, caplog):
    """Ноль суток — осознанное «хранить вечно», и оно громкое.

    Отключать бывает нужно (разовый прогон, отладка парсера), но тихое
    отключение вернуло бы ровно ту дыру, ради которой всё заводилось.
    """
    monkeypatch.setattr(runner, "REPLAY_RETENTION_DAYS", 0)
    s3 = FakeS3(existing=None)
    with caplog.at_level("WARNING"):
        runner._ensure_replay_retention(s3, "replays")
    assert s3.set_calls == []
    assert "НЕ удаляются" in caplog.text


def test_default_retention_is_sane():
    """Неделя: покрывает повторный разбор и не топит диск.

    Ноль означал бы «удалять немедленно» и ломал бы повторный разбор при
    смене версии парсера; девяносто суток из спецификации при 2000
    матчей в сутки — это 10 ТиБ, чего нет ни на одном VPS.
    """
    assert 1 <= runner.REPLAY_RETENTION_DAYS <= 30
