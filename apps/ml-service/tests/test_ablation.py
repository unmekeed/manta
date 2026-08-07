"""Тесты ablation-анализа фич (спринт 69).

Главное, что проверяется, — что инструмент НЕ путает два разных вердикта:
«фича бесполезна» (данные есть, эффекта нет) и «фича неизмерима» (данных
нет вовсе). Смешать их — значит выкинуть по правилу ML-PLAN §6 фичу,
которую просто ещё не на чем было проверить: ровно этот риск создаёт
трек F, где все 12 новых колонок NaN у исторических матчей.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.ablation import (GROUPS, MIN_OBSERVED_ROWS, PHASES, coverage,
                               observed_mask, run, verdict, without)
from training.dataset import FEATURES, synth_matches


def test_without_blanks_only_named_columns():
    ds = synth_matches(40)
    out = without(ds, ["towers_diff"])
    i = FEATURES.index("towers_diff")
    assert np.all(np.isnan(out.X[:, i])), "колонка должна быть полностью NaN"
    others = [j for j in range(len(FEATURES)) if j != i]
    assert np.array_equal(np.isnan(ds.X[:, others]), np.isnan(out.X[:, others])), \
        "остальные колонки не должны меняться"
    # Исходный датасет не тронут (иначе прогон по группам портил бы базу).
    assert not np.all(np.isnan(ds.X[:, i]))


def test_without_preserves_dataset_metadata():
    """n_matches/tiers/patches обязаны пережить копирование: без них
    _valid_mask и patch_weights дадут другой сплит, и сравнение перестанет
    быть парным."""
    ds = synth_matches(30)
    out = without(ds, ["rax_diff"])
    assert out.n_matches == ds.n_matches
    assert np.array_equal(out.y, ds.y) and np.array_equal(out.groups, ds.groups)
    a, b = ds._valid_mask(), out._valid_mask()
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def test_coverage_counts_non_nan():
    ds = synth_matches(20)
    i = FEATURES.index("alive_diff")
    ds.X[:, i] = np.nan
    cov = coverage(ds)
    assert cov["alive_diff"] == 0.0
    assert cov["game_time"] == 1.0


def test_verdict_uses_noise_not_sign():
    # Эффект меньше двух сигм — это шум, каким бы ни был знак.
    assert "шум" in verdict(0.001, 0.003)
    assert "шум" in verdict(-0.001, 0.003)
    # Заметно хуже без фичи — фича полезна.
    assert "ПОЛЕЗНА" in verdict(0.010, 0.003)
    # Заметно лучше без фичи — фича вредит.
    assert "ВРЕДИТ" in verdict(-0.010, 0.003)


def test_statistically_significant_but_tiny_effect_is_not_a_win():
    """Найдено на живом прогоне: парное сравнение двух почти одинаковых
    моделей даёт σ ~0.0001, и микроэффект 0.0003 проходит порог 2σ. Без
    порога практической значимости инструмент объявил бы draft_prior
    полезным — вопреки независимому замеру спринта 62."""
    v = verdict(0.00034, 0.00014)
    assert "ПОЛЕЗНА" not in v
    assert "ничтожен" in v


def test_low_coverage_never_yields_removal_verdict():
    """Тоже с живого прогона: у alive/towers/rax покрытие 5.5% (они есть
    только у реплейных матчей). «Эффекта нет» на 5% датасета — это факт про
    выборку, а не про фичу, и удалять по нему нельзя."""
    v = verdict(0.00015, 0.00009, cov=0.055)
    assert "кандидат на удаление" not in v
    assert "преждевременен" in v
    # При полном покрытии тот же результат уже означает «выкидывать».
    assert "кандидат на удаление" in verdict(0.00001, 0.00050, cov=1.0)


def test_empty_feature_is_unmeasurable_not_useless():
    """Ключевая проверка: у пустой фичи вердикт «неизмерима», и модель
    ради неё не переобучается."""
    ds = synth_matches(60)
    i = FEATURES.index("roshan_diff")
    ds.X[:, i] = np.nan

    rows, base = run(ds, {"F2_объективы": ["roshan_diff"]})
    assert len(rows) == 1
    r = rows[0]
    assert r["delta"] is None, "обучение не должно было запускаться"
    assert "НЕИЗМЕРИМА" in r["verdict"]
    assert "кандидат на удаление" not in r["verdict"]
    assert 0.0 <= base <= 1.0


def test_filled_feature_gets_measured():
    """Обратный случай: если данные есть, вердикт выносится по числам."""
    ds = synth_matches(60)
    i = FEATURES.index("towers_diff")
    rng = np.random.default_rng(0)
    ds.X[:, i] = rng.normal(size=len(ds.y))

    rows, _ = run(ds, {"башни": ["towers_diff"]})
    r = rows[0]
    assert r["delta"] is not None and r["sigma"] is not None
    assert r["coverage"] == 1.0
    assert "НЕИЗМЕРИМА" not in r["verdict"]


def test_groups_reference_only_real_features():
    """Опечатка в GROUPS привела бы к тихому пропуску целого блока."""
    for label, names in GROUPS.items():
        unknown = [n for n in names if n not in FEATURES]
        assert not unknown, f"{label}: нет таких фич — {unknown}"


# -- «где есть» против «везде» (спринт 132) ---------------------------------------

def test_observed_mask_needs_only_one_feature_of_the_group():
    """Группа отключается ЦЕЛИКОМ, значит эффект возможен везде, где было
    чему отключаться. Требовать заполненности всех фич сразу — сузить
    подвыборку до пересечения покрытий и мерить на другой популяции."""
    X = np.full((3, len(FEATURES)), np.nan)
    a, b = FEATURES.index("roshan_diff"), FEATURES.index("aegis_alive")
    X[0, a] = 1.0                      # есть одна
    X[1, a] = X[1, b] = 1.0            # есть обе
    obs = observed_mask(X, ["roshan_diff", "aegis_alive"])
    assert list(obs) == [True, True, False]


@pytest.fixture(scope="module")
def sparse_signal():
    """Фича заполнена ровно у половины матчей и там почти решает исход.

    Так выглядит любая фича трека F: она есть только у реплейных матчей,
    а у JSON-матчей её нет вовсе. Половина, а не 5%, — чтобы подвыборка
    осталась достаточной для бутстрапа: на синтетике из четырёх сотен
    матчей более редкий сигнал даёт честную, но слишком широкую σ, и тест
    начал бы мигать от seed к seed.

    Прогон общий на модуль: два полных обучения стоят несколько секунд.
    """
    n_matches, share, seed = 400, 0.5, 5
    ds = synth_matches(n_matches, seed=seed)
    i = FEATURES.index("roshan_diff")
    ds.X[:, i] = np.nan
    rng = np.random.default_rng(seed)
    matches = sorted(set(ds.groups.tolist()))
    rng.shuffle(matches)
    chosen = set(matches[:int(len(matches) * share)])
    filled = np.array([g in chosen for g in ds.groups])
    # Там, где фича есть, она почти детерминирует исход: эффект её
    # отключения обязан быть большим ИМЕННО на этих строках.
    ds.X[filled, i] = ds.y[filled] * 10.0 + rng.normal(0, 0.3,
                                                      size=filled.sum())
    rows, _ = run(ds, {"F2_объективы": ["roshan_diff"]})
    return ds, filled, rows[0]


def test_verdict_is_measured_where_the_feature_exists_not_everywhere(
        sparse_signal):
    """Главная поправка спринта 132.

    Фича, заполненная у половины строк, физически не может двигать метрику
    на остальных: там модель её не видела ни с ней, ни без неё. Считая Δ
    по всей валидации, инструмент делил реальный эффект на долю покрытия —
    и тем вернее рекомендовал удалить фичу, чем она реже. Ревизия трека F
    этим инструментом выкинула бы работающие фичи.
    """
    _, _, r = sparse_signal
    assert r["delta"] is not None and r["delta_all"] is not None
    assert r["delta"] > 1.5 * r["delta_all"], (
        "эффект «где есть» обязан быть заметно больше размазанного по всей "
        f"валидации: {r['delta']} против {r['delta_all']}")
    assert "ПОЛЕЗНА" in r["verdict"], r["verdict"]


def test_report_row_counts_matches_carrying_the_signal(sparse_signal):
    """«Вернуться при росте данных» бесполезно без ответа «насколько
    вырасти». Доля строк на это не отвечает: 5% строк — это и сто матчей,
    и пять."""
    ds, filled, r = sparse_signal
    assert r["matches_observed"] == len(set(ds.groups[filled].tolist()))


def test_phase_deltas_are_reported_separately(sparse_signal):
    """Объективы живут в поздней игре, производные — в ранней. Фича,
    дающая много в одной фазе и ноль в остальных, в среднем выглядит
    шумом; без разбивки этого не увидеть."""
    _, _, r = sparse_signal
    phases = r["phases"]
    assert phases, "фазовая разбивка не посчитана"
    assert set(phases) <= {p[0] for p in PHASES}
    for ph in phases.values():
        assert "delta" in ph and "sigma" in ph


def test_verdict_is_refused_on_a_handful_of_validation_rows():
    """Покрытие по датасету есть, а на валидации фичу видно в двух
    десятках строк: так бывает, когда матчи с полным сигналом осели в
    train. Brier на двадцати строках — это шум сэмплирования, и вердикт
    по нему был бы выдумкой; инструмент обязан ОТКАЗАТЬСЯ его выносить.

    Строк намеренно не ноль: пустая подвыборка отсеклась бы и без порога,
    и тест не отличил бы «есть порог» от «нет порога».
    """
    ds = synth_matches(60)
    i = FEATURES.index("roshan_diff")
    ds.X[:, i] = np.nan
    in_valid, pro = ds._valid_mask()
    ds.X[~in_valid & ~pro, i] = 1.0            # заполнено в обучении
    valid_rows = np.where(in_valid & ~pro)[0][:20]
    ds.X[valid_rows, i] = 1.0                  # и в 20 строках валидации

    rows, _ = run(ds, {"F2_объективы": ["roshan_diff"]})
    r = rows[0]
    assert 0 < r["observed_rows"] < MIN_OBSERVED_ROWS, r["observed_rows"]
    assert r["delta"] is None, "вердикт вынесен по горстке строк"
    assert "НЕИЗМЕРИМА" in r["verdict"]
    assert "кандидат на удаление" not in r["verdict"]


# -- защита от параллельных прогонов (спринт 132.1) -------------------------------

def test_second_run_refuses_to_start(tmp_path):
    """Прогон идёт десятки минут и до конца ничего не пишет в --json.

    На живой машине это привело ровно к тому, к чему и должно было:
    `ls` не находил отчёт, команда запускалась снова — и так восемь раз.
    Восемь прогонов дрались за процессор, обучали по двенадцать моделей
    каждый и писали в один файл.
    """
    from training.ablation import single_run

    lock = tmp_path / "ablation.lock"
    with single_run(lock):
        with pytest.raises(SystemExit) as e:
            with single_run(lock):
                pass
    assert "уже выполняется" in str(e.value)
    assert "pkill" in str(e.value), "сообщение обязано говорить, как прервать"


def test_lock_is_released_after_a_finished_run(tmp_path):
    """Блокировка на flock, а не на «есть ли файл»: иначе первое же
    аварийное завершение запретило бы запуск навсегда."""
    from training.ablation import single_run

    lock = tmp_path / "ablation.lock"
    with single_run(lock):
        pass
    with single_run(lock):        # не должно бросить
        pass


def test_lock_survives_a_killed_run(tmp_path):
    """Файл блокировки остаётся на диске после kill -9, но захват ядро
    снимает вместе с процессом — следующий запуск обязан пройти."""
    from training.ablation import single_run

    lock = tmp_path / "ablation.lock"
    lock.write_text("999999")     # «остался от убитого прогона»
    with single_run(lock):
        pass


# -- гейт по числу матчей (спринт 133) --------------------------------------------

def test_removal_verdict_needs_enough_matches():
    """Найдено на живом прогоне 2026-08-07.

    Четыре группы трека F имели покрытие 26.1% — на 1.1 процентного
    пункта выше LOW_COVERAGE — и все четыре получили «кандидат на
    удаление». Полтора пункта в другую сторону дали бы «преждевременен».
    Решение об удалении фичи, стоившей спринта, не может держаться на
    такой щепке; правильная мера — число матчей, и она же записана в
    самой задаче ревизии.
    """
    from training.ablation import MIN_VERDICT_MATCHES

    thin = verdict(0.0001, 0.0006, cov=0.99,
                   matches=MIN_VERDICT_MATCHES - 1)
    assert "преждевременен" in thin
    assert "кандидат на удаление" not in thin

    fat = verdict(0.0001, 0.0006, cov=0.99, matches=MIN_VERDICT_MATCHES)
    assert "кандидат на удаление" in fat


def test_match_gate_does_not_rescue_a_measured_effect():
    """Порог по объёму данных откладывает вердикт «удалить», но не
    отменяет измеренный эффект: значимая и заметная фича остаётся
    полезной, сколько бы матчей её ни несло."""
    from training.ablation import MIN_VERDICT_MATCHES

    v = verdict(0.010, 0.002, cov=0.05, matches=MIN_VERDICT_MATCHES // 10)
    assert "ПОЛЕЗНА" in v


def test_match_count_reaches_the_verdict():
    """Число матчей должно ДОХОДИТЬ до вердикта, а не только до отчёта.

    Посчитать его и не передать — та же ошибка, только тише: отчёт
    выглядит информативным, а решение принимается по-старому. Здесь фича
    — чистый шум при полном покрытии строк, но матчей всего сотня: без
    передачи числа матчей вердикт был бы «кандидат на удаление».
    """
    ds = synth_matches(100, seed=31)
    i = FEATURES.index("roshan_diff")
    rng = np.random.default_rng(31)
    ds.X[:, i] = rng.normal(size=len(ds.y))     # шум, но заполнено везде

    rows, _ = run(ds, {"F2_объективы": ["roshan_diff"]})
    r = rows[0]
    assert r["coverage"] == 1.0, "покрытие строк полное — гейт не о нём"
    assert r["matches_observed"] < 2000
    assert "преждевременен" in r["verdict"], r["verdict"]
    assert str(r["matches_observed"]) in r["verdict"], (
        "вердикт обязан назвать число, на котором он основан")


# -- выбор целей (спринт 134) -----------------------------------------------------

def test_only_keeps_just_the_named_targets():
    """Прогон по всем целям стоит десятки минут, а вопрос обычно про
    одну-две."""
    from training.ablation import select_targets

    all_targets = {"a": ["x"], "b": ["y"], "c": ["z"]}
    assert select_targets(all_targets, "c,a") == {"c": ["z"], "a": ["x"]}
    assert select_targets(all_targets, None) == all_targets
    assert select_targets(all_targets, "") == all_targets


def test_only_refuses_an_unknown_name():
    """Опечатка не должна давать пустой отчёт: «ничего не нашлось»
    неотличимо от «всё чисто», и это худший вид молчания."""
    from training.ablation import select_targets

    with pytest.raises(SystemExit) as e:
        select_targets({"база_объекты": ["towers_diff"]}, "база_обьекты")
    assert "нет таких целей" in str(e.value)
    assert "база_объекты" in str(e.value), "надо подсказать, что есть"
