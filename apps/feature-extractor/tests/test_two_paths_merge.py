"""Две половины витрины не затирают друг друга (спринт 182).

ЧТО СЛУЧИЛОСЬ 2026-09-02. `MatchTimelineFeatures` наполняют ДВА пути, и
они считают разное:

  JSON OpenDota  — трек F (Roshan, аегис, байбэки, предметы, варды, руны,
                   нейтралки, уровни) плюс tier, avg_rank, patch;
  реплей         — настоящие позиции: position_advance, alive_diff,
                   local_manpower_diff, spread_diff.

Таблица — ReplacingMergeTree(computed_at) с ключом (match_id, game_time),
то есть побеждает последняя строка ЦЕЛИКОМ. Реплей, пришедший вторым,
обнулял всё, чего не считает, — молча: ни ошибки, ни лога. Вместе с
сигналами пропадали `patch` (он управляет весами строк, A9) и `tier` (по
нему гейт отбирает про-эталон).

Дефект спал, пока пути почти не пересекались. Разбудил его spринт 181:
`salt-collector` начал качать реплеи матчей, УЖЕ собранных JSON-путём, —
то есть ровно тех, у которых сигналы есть.

ЧТО ПРОВЕРЯЕТСЯ. Что переносится ВСЁ чужое, а не список, записанный
руками: рукописный перечень разъехался бы при первой же новой колонке, и
разъехался бы молча — новая фича просто перестала бы переживать разбор
реплея.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from extractor.runner import Extractor  # noqa: E402

# Схема витрины: своё (считает реплей), чужое (считает JSON-путь).
OWN = ["match_id", "game_time", "networth_diff", "xp_diff", "radiant_win",
       "feature_version"]
FOREIGN = ["roshan_diff", "obs_wards_diff", "patch", "tier", "avg_rank"]


class FakeCH:
    database = "manta"

    def __init__(self, existing=None, schema=None, fail_schema=False):
        self.existing = existing if existing is not None else []
        self.schema = schema if schema is not None else OWN + FOREIGN
        self.fail_schema = fail_schema
        self.queries = []

    def select(self, query, params=None):
        self.queries.append(query)
        if "system.columns" in query:
            if self.fail_schema:
                raise RuntimeError("ClickHouse недоступен")
            return [{"name": n} for n in self.schema]
        return self.existing


def runner_with(ch):
    r = Extractor.__new__(Extractor)
    r.ch = ch
    return r


def trows(times=(60, 120)):
    return [{"match_id": 1, "game_time": t, "networth_diff": t,
             "xp_diff": 0, "radiant_win": 1, "feature_version": "v1"}
            for t in times]


def existing(times=(60, 120), roshan=3.0, patch=57):
    return [{"game_time": t, "roshan_diff": roshan, "obs_wards_diff": 2.0,
             "patch": patch, "tier": "Pub", "avg_rank": 80} for t in times]


# -- главное --------------------------------------------------------------------

def test_foreign_columns_survive_the_replay(caplog):
    """ГЛАВНОЕ: сигналы JSON-пути переживают разбор реплея.

    Без переноса они молча обнулялись бы — а это ровно те двенадцать
    фич, ради измерения которых копится датасет (ML-PLAN §8).
    """
    ch = FakeCH(existing=existing())
    rows = trows()
    with caplog.at_level("INFO"):
        runner_with(ch)._carry_over_foreign_columns(1, rows)
    assert rows[0]["roshan_diff"] == 3.0
    assert rows[0]["obs_wards_diff"] == 2.0


def test_the_patch_and_tier_survive_too():
    """patch и tier — не сигналы, но теряются так же и стоят дорого.

    patch управляет весом строки после баланс-патча (A9): потеряв его,
    матч получает вес неизвестного патча. tier отделяет про-эталон, на
    котором работает гейт продвижения.
    """
    ch = FakeCH(existing=existing(patch=57))
    rows = trows()
    runner_with(ch)._carry_over_foreign_columns(1, rows)
    assert rows[0]["patch"] == 57
    assert rows[0]["tier"] == "Pub"


def test_our_own_columns_are_not_overwritten():
    """А своё реплей не отдаёт.

    Позиции он считает по настоящим снапшотам, JSON-путь — приближением.
    Взять оттуда значило бы обменять точные данные на приблизительные.
    """
    ch = FakeCH(existing=[dict(r, networth_diff=-999) for r in existing()])
    rows = trows()
    runner_with(ch)._carry_over_foreign_columns(1, rows)
    assert rows[0]["networth_diff"] == 60, "реплей отдал своё чужому"


def test_the_carried_list_comes_from_the_schema():
    """Список переносимого берётся из СХЕМЫ, а не пишется руками.

    Новая колонка, добавленная миграцией, обязана переноситься сама.
    Рукописный перечень разъехался бы при первой же — и молча: фича
    просто перестала бы переживать разбор реплея.
    """
    ch = FakeCH(schema=OWN + FOREIGN + ["brand_new_diff"],
                existing=[dict(r, brand_new_diff=42.0) for r in existing()])
    rows = trows()
    runner_with(ch)._carry_over_foreign_columns(1, rows)
    assert rows[0]["brand_new_diff"] == 42.0, "новая колонка не перенеслась"


# -- границы --------------------------------------------------------------------

def test_a_match_seen_for_the_first_time_needs_no_carry():
    """Матча ещё не было — переносить нечего, и это не ошибка.

    Реплейный путь часто приходит первым (про-реплеи, кандидаты).
    """
    ch = FakeCH(existing=[])
    rows = trows()
    runner_with(ch)._carry_over_foreign_columns(1, rows)
    assert "roshan_diff" not in rows[0]


def test_rows_without_a_counterpart_are_left_alone():
    """Строка, которой не было в витрине, остаётся своей.

    Реплей может дать больше минут, чем JSON (обрезанный таймлайн).
    Проставить ей чужие значения соседней минуты значило бы придумать
    данные.
    """
    ch = FakeCH(existing=existing(times=(60,)))
    rows = trows(times=(60, 120))
    runner_with(ch)._carry_over_foreign_columns(1, rows)
    assert rows[0]["roshan_diff"] == 3.0
    assert "roshan_diff" not in rows[1]


def test_an_unreachable_schema_does_not_lose_the_match():
    """ClickHouse не ответил про схему — вставляем что посчитали.

    Потерять чужие колонки плохо, но не вставить свои хуже: матч остался
    бы вовсе без строк, то есть разбор реплея пропал бы целиком.
    """
    ch = FakeCH(fail_schema=True)
    rows = trows()
    runner_with(ch)._carry_over_foreign_columns(1, rows)
    assert rows == trows(), "строки испорчены при недоступной схеме"


def test_computed_at_is_not_carried_over():
    """Версия строки не переносится.

    computed_at — версия ReplacingMergeTree. Перенеси старую, и новая
    строка проиграла бы слияние собственной предшественнице: разбор
    реплея не применился бы вовсе.
    """
    ch = FakeCH(schema=OWN + FOREIGN + ["computed_at"],
                existing=[dict(r, computed_at="2020-01-01 00:00:00")
                          for r in existing()])
    rows = trows()
    runner_with(ch)._carry_over_foreign_columns(1, rows)
    assert "computed_at" not in rows[0]


def test_nothing_is_asked_for_an_empty_batch():
    """Пустая пачка в базу не ходит."""
    ch = FakeCH()
    runner_with(ch)._carry_over_foreign_columns(1, [])
    assert ch.queries == []


def test_the_carry_actually_runs_before_the_insert():
    """Перенос ВЫЗЫВАЕТСЯ, и раньше вставки.

    Поймано мутацией: проверки выше дёргают метод напрямую и потому
    прошли бы на коде, где вызов из обработчика убран вовсе. Ошибка живёт
    в МЕСТЕ ВЫЗОВА — та же беда, что в спринте 174 с возрастом
    production.

    Порядок здесь тоже существенный: перенос ПОСЛЕ вставки не спас бы
    ничего — затирание уже случилось бы.
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "extractor"
           / "runner.py").read_text(encoding="utf-8")
    carry = src.index("self._carry_over_foreign_columns(match_id, trows)")
    insert = src.index('self.ch.insert_rows("MatchTimelineFeatures", trows)')
    assert carry < insert, "перенос идёт после вставки — затирание уже случилось"
