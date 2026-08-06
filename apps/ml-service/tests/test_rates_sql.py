"""Поведение оконных функций G1 на живом ClickHouse (спринт 131).

Почему отдельный файл с настоящим сервером. Смысл производной целиком
задан кадром окна: `RANGE BETWEEN 300 PRECEDING AND CURRENT ROW`. Три
разные ошибки в этой строке дают три разных вида порчи, и ни одну из них
не видно ни в питоне, ни в юнит-тестах на строках:

  FOLLOWING вместо PRECEDING — утечка целевой переменной: модель узнаёт
      будущее, показывает прекрасный Brier на валидации и мусор в проде;
  ROWS вместо RANGE — кадр меряется строками, и пропущенная минута
      молча превращает трёхминутное окно в четырёхминутное;
  потерянный PARTITION BY — окно перетекает из матча в матч, и первые
      минуты каждого матча считаются от чужого финала.

Запуск:
    make wp-rates-sql-test          # локальный ClickHouse из docker-compose
или вручную:
    MANTA_TEST_CH=1 pytest tests/test_rates_sql.py

Без MANTA_TEST_CH файл пропускается: в CI постоянного ClickHouse нет, и
это честно означает «здесь не проверялось», а не «зелено».

Рабочую витрину тест не трогает: он создаёт собственную базу, копируя
структуру таблиц из manta (CREATE TABLE ... AS), и удаляет её за собой.
Схема берётся из настоящей витрины, а не пишется руками, — иначе тест
проверял бы свою копию и не заметил бы, что метрика из RATE_METRICS в
витрине называется иначе.
"""
import json
import os
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wp_rates import rate_name  # noqa: E402

from training.dataset import (FEATURES, match_rows_sql,  # noqa: E402
                              row_to_features)

CH_URL = os.getenv("CLICKHOUSE_URL", "http://localhost:8123")
CH_USER = os.getenv("CLICKHOUSE_USER", "dota")
CH_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password")
SRC_DB = os.getenv("CLICKHOUSE_DB", "manta")
TEST_DB = "manta_rates_test"

pytestmark = pytest.mark.skipif(
    not os.getenv("MANTA_TEST_CH"),
    reason="нужен MANTA_TEST_CH=1 и живой ClickHouse")


def _ch(query: str, database: str = TEST_DB) -> str:
    resp = requests.post(CH_URL, params={"database": database},
                         data=query.encode("utf-8"),
                         headers={"X-ClickHouse-User": CH_USER,
                                  "X-ClickHouse-Key": CH_PASSWORD},
                         timeout=30)
    if resp.status_code != 200:
        raise AssertionError(resp.text)
    return resp.text


@pytest.fixture()
def ch():
    _ch(f"DROP DATABASE IF EXISTS {TEST_DB}", database="system")
    _ch(f"CREATE DATABASE {TEST_DB}", database="system")
    for table in ("MatchTimelineFeatures", "MatchDraft"):
        _ch(f"CREATE TABLE {TEST_DB}.{table} AS {SRC_DB}.{table}")
    yield _ch
    _ch(f"DROP DATABASE IF EXISTS {TEST_DB}", database="system")


def _insert(match_id: int, points: list[tuple[int, int]]) -> None:
    """points — пары (game_time, networth_diff)."""
    values = ", ".join(f"({match_id}, {t}, {nw}, {nw}, 0, 0, 1)"
                       for t, nw in points)
    _ch("INSERT INTO MatchTimelineFeatures (match_id, game_time,"
        " networth_diff, xp_diff, kills_radiant, kills_dire, radiant_win)"
        f" VALUES {values}")


def _rates(match_id: int, window_s: int) -> dict[int, float]:
    """{game_time: производная networth_diff за окно}.

    Запрос БЕЗ WHERE — ровно как в load_from_clickhouse, где обучение
    читает все матчи разом. Фильтровать по матчу в SQL здесь нельзя:
    отбор одного матча выполняется ДО оконных функций, и тогда любое
    окно оказывается запертым внутри матча само собой — тест на
    протечку между матчами стал бы тавтологией.
    """
    sql = match_rows_sql() + " ORDER BY match_id, game_time"
    rows = [json.loads(line)
            for line in _ch(sql + " FORMAT JSONEachRow").splitlines() if line]
    i = FEATURES.index(rate_name("networth_diff", window_s))
    return {int(r["game_time"]): row_to_features(r)[i]
            for r in rows if int(r["match_id"]) == match_id}


# Матч с разворотом: преимущество растёт до 6-й минуты, потом схлопывается.
# Прошлое и будущее в точке разворота отличаются ЗНАКОМ — это и позволяет
# отличить окно назад от окна вперёд.
REVERSAL = [(60, 0), (120, 1000), (180, 2000), (240, 3000), (300, 4000),
            (360, 5000), (420, 4000), (480, 3000), (540, 2000), (600, 1000)]


def test_window_looks_strictly_backwards(ch):
    """Окно вперёд — утечка целевой переменной.

    На пятой минуте разрыв рос (+1000/мин), на седьмой уже падал
    (−1000/мин). Окно назад обязано показать рост в точке, где будущее
    отрицательно, и падение там, где прошлое положительно.
    """
    _insert(1, REVERSAL)
    r = _rates(1, 60)
    assert r[360] == 1000.0, "в точке разворота видно будущее"
    assert r[420] == -1000.0


def test_window_span_is_measured_in_seconds_not_rows(ch):
    """Кадр в секундах, а не в строках.

    Здесь пропущена четвёртая минута — дыры в поминутных данных
    встречаются на JSON-пути. С ROWS BETWEEN 3 PRECEDING окно уехало бы
    к первой минуте и дало бы другой темп, никак себя не проявив.
    """
    _insert(2, [(60, 0), (120, 3000), (180, 6000), (300, 7000), (360, 8000)])
    # Окно 180 с в точке 360 покрывает [180, 360]: 8000−6000 за 3 минуты.
    got = _rates(2, 180)[360]
    assert abs(got - 2000.0 / 3.0) < 1e-6, got


def test_rates_do_not_leak_between_matches(ch):
    """Без PARTITION BY окно перетекает из матча в матч.

    Второй матч намеренно начинается позже, чем кончился первый: без
    разделения по match_id его первые минуты попали бы в один кадр с
    чужим финалом и считались бы от него. Одинаковое игровое время у
    разных матчей такую ошибку маскирует — поэтому времена разведены.
    """
    _insert(3, REVERSAL)                       # 60..600
    _insert(4, [(660, 5000), (720, 5100)])
    r = _rates(4, 300)
    assert r[660] != r[660], "первая строка матча получила чужую историю"
    assert r[720] == 100.0, "темп посчитан от чужого матча"


def test_first_minute_has_no_rate(ch):
    """У первой строки матча истории нет — обязателен NaN, а не 0."""
    _insert(5, REVERSAL)
    first = _rates(5, 60)[60]
    assert first != first, "первая минута получила осмысленное значение"


def test_short_window_is_not_wider_than_the_long_one(ch):
    """Санитарная проверка семейств: на устойчивом тренде все три окна
    дают один темп, а сразу после разворота минутное окно реагирует
    резче пятиминутного — иначе окна не различаются и мерить их
    по отдельности бессмысленно."""
    _insert(6, REVERSAL)
    assert _rates(6, 60)[300] == _rates(6, 300)[300] == 1000.0
    assert _rates(6, 60)[420] < _rates(6, 300)[420]
