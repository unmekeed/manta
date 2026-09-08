package handlers

import (
	"context"
	"net/http"
	"sort"
	"strings"
	"time"
)

// Состояние системы для админской страницы (спринт 198).
//
// ЗАЧЕМ. Всё, что мы знаем о живости сбора, до сих пор доставалось двумя
// способами: `make doctor` на самой машине и 🔴/✅ в Telegram. Оба
// требуют, чтобы владелец в этот момент сидел за терминалом или читал
// чат, и оба показывают МОМЕНТ, а не динамику. Диагнозы сентября —
// мёртвый STRATZ при поднятом контейнере, доля потока, уходящая в
// никуда, потолок в 480 матчей, о котором не говорила ни одна настройка,
// — все стоили дней именно потому, что смотреть было некуда.
//
// ГДЕ ЭТО ЖИВЁТ И ПОЧЕМУ НЕ СНАРУЖИ. На внутреннем роутере (:8080) под
// ролью admin, и в `router.PublicRoutes` его НЕТ. Три независимых
// преграды: публичный mux про этот путь не знает вовсе, роль требует
// админского токена, а сам порт при доступе через Tailscale наружу не
// смотрит. Достаточно было бы одной, но каждая из трёх переживает отказ
// двух других — а история этого проекта состоит из отказов, каждый из
// которых считался невозможным.
//
// ТОЛЬКО POSTGRES. Шлюз в ClickHouse не ходит нигде, и заводить это
// исключение ради страницы состояния не стоит: `CollectedMatches`
// заполняют ОБА пути сбора и она отвечает на главный вопрос — сколько и
// каким источником собрано. Свежесть витрины и реплейных событий сюда не
// попадает; это честный пробел, и он назван в `docs/ROADMAP.md`, а не
// прикрыт похожим числом из другой таблицы.

// adminSource — приток одного источника за сутки.
type adminSource struct {
	Source     string `json:"source"`
	Matches    int64  `json:"matches"`
	WithReplay int64  `json:"with_replay"`
	Calls      int64  `json:"calls"`
}

type adminStatus struct {
	GeneratedAt time.Time     `json:"generated_at"`
	Sources     []adminSource `json:"sources"`
	Collection  adminCounts   `json:"collection"`
	Reports     adminCounts   `json:"reports"`
	Budget      adminBudget   `json:"budget"`
}

type adminCounts struct {
	Day    int64      `json:"day"`
	Week   int64      `json:"week"`
	Total  int64      `json:"total"`
	Latest *time.Time `json:"latest"` // null — не было НИКОГДА
}

type adminBudget struct {
	CallsToday int64   `json:"calls_today"`
	CallsMonth int64   `json:"calls_month"`
	UsdMonth   float64 `json:"usd_month"`
}

// costPerCall — цена вызова платного тарифа OpenDota, $0.01 за 100.
// Держится числом, потому что расход в вызовах не говорит владельцу
// ничего, а в долларах говорит всё (то же соображение, что в
// collector/budget.py).
const costPerCall = 0.0001

// usdFor — расход в долларах. Отдельной функцией, чтобы цена вызова
// проверялась тестом: перепутанный порядок нулей здесь не падает и не
// логируется, он просто показывает владельцу не ту сумму.
func usdFor(calls int64) float64 { return float64(calls) * costPerCall }

// Окно, за которое собирается СПИСОК источников (не их счётчики).
//
// Источники берутся из того, что реально писало за неделю, а не из
// зашитого перечня: перечень в Go разъехался бы с фабрикой источников на
// Python, и разъезд был бы молчаливым — ровно та беда, что уже случалась
// со списком имён в спринте 180.
//
// Плата за это названа честно: источник, молчащий дольше недели, со
// страницы исчезает. Для суточного дежурства это правильный размен —
// недельное молчание обнаруживают не по странице состояния, а по тому,
// что данных нет вовсе.
const adminRosterDays = 7

// Три отдельных запроса вместо одного с JOIN — намеренно. Решение
// «источник, ничего не собравший, всё равно показывается нулём» живёт
// НЕ в SQL, а в mergeSources ниже, и потому проверяется без базы. Сам
// SQL проверяется отдельно и по-настоящему: PREPARE на схеме из
// настоящих миграций (scripts/tests/test_sql_prepares.py).

// $1 используется ДВАЖДЫ, и обе подстановки обязаны быть одного типа.
//
// Первая редакция писала `($1 || ' days')::interval` в одной половине и
// `CURRENT_DATE - $1::int` в другой: конкатенация требует text, вычитание
// из даты — int, и pgx не смог угодить обоим. Отказ пришёл только на
// живой базе, первым же запросом: «cannot find encode plan».
//
// Вторая редакция писала `$1 * INTERVAL '1 day'` без приведения — и
// сломалась ТОЖЕ: Postgres вывел параметр как double precision (такой
// оператор с интервалом есть), после чего `date - double precision` не
// нашлось. Оба раза отказ приходил только на живой базе.
//
// Приведение `::int` в ОБОИХ местах снимает вывод типа как таковой:
// гадать больше нечего. Проверяется это теперь не глазами, а прогоном
// PREPARE на настоящей схеме — scripts/tests/test_sql_prepares.py.
const adminRosterSQL = `
SELECT DISTINCT source_name FROM CollectedMatches
 WHERE collected_at > NOW() - ($1::int * INTERVAL '1 day')
UNION
SELECT DISTINCT source FROM ApiBudget
 WHERE day > CURRENT_DATE - $1::int`

const adminCollectedSQL = `
SELECT source_name, count(*), count(*) FILTER (WHERE has_replay)
  FROM CollectedMatches
 WHERE collected_at > NOW() - INTERVAL '1 day'
 GROUP BY source_name`

const adminSpentSQL = `
SELECT source, sum(calls) FROM ApiBudget
 WHERE day = CURRENT_DATE GROUP BY source`

// collectedRow — приток источника за сутки.
type collectedRow struct{ Matches, WithReplay int64 }

// canonicalSource — одно имя источника из двух его написаний.
//
// ЖИВОЙ ДЕФЕКТ 08.09.2026, увиденный на первой же работающей странице.
// `CollectedMatches.source_name` пишет `opendota_timeline`, а
// `ApiBudget.source` — `opendota-timeline`: это ОДИН источник, но
// объединение двух таблиц давало две строки, и в каждой половина правды.
//
// Строка «opendota-timeline: 0 матчей, 60 вызовов» при этом читается
// ровно как «источник жжёт бюджет впустую» — сигнатура аварии STRATZ,
// ради которой страница и делалась. Ложная тревога в инструменте,
// которому положено верить, хуже отсутствия инструмента.
//
// Ловушка была ИЗВЕСТНА: в спринте 196 она описана в `COLLECTED_AS`
// (collector/__main__.py) и закрыта тестом — на стороне Python. Здесь в
// неё шагнули заново, потому что урок жил в другом языке.
//
// `gc-salts` при этом остаётся отдельной строкой, и правильно: это
// скрипт добычи солей, а не коллектор `salts`. Механическая замена
// дефиса на подчёркивание их не сливает — и не должна.
func canonicalSource(name string) string {
	return strings.ReplaceAll(name, "-", "_")
}

// mergeSources сводит перечень источников со счётчиками суток.
//
// ГЛАВНОЕ СВОЙСТВО: источник из перечня попадает в ответ ВСЕГДА, даже
// если не собрал ничего и не потратил ни вызова. Пропусти мы такие —
// «источник умер» стало бы неотличимо от «источника нет», а это ровно та
// пара, которую мы двое суток разбирали руками в сентябре: контейнер
// stratz стоял `Up`, отвечал 403 каждый цикл, и не было ни одного места,
// где это выглядело бы как ноль.
//
// Сортировка по притоку убывающе, при равенстве — по имени: молчащие
// источники собираются внизу ОДНОЙ группой, а не рассыпаны по списку.
// Стабильность важнее красоты — страницу читают глазами и сравнивают с
// тем, что было вчера.
func mergeSources(roster []string, collected map[string]collectedRow,
	spent map[string]int64) []adminSource {
	merged := map[string]*adminSource{}
	order := []string{}
	add := func(name string) *adminSource {
		key := canonicalSource(name)
		if s, ok := merged[key]; ok {
			return s
		}
		merged[key] = &adminSource{Source: key}
		order = append(order, key)
		return merged[key]
	}
	for _, name := range roster {
		add(name)
	}
	// Счётчики раскладываются по КАНОНИЧЕСКОМУ имени, а не по тому, под
	// которым пришли: иначе половина чисел осталась бы в строке, которой
	// больше нет.
	for name, c := range collected {
		s := add(name)
		s.Matches += c.Matches
		s.WithReplay += c.WithReplay
	}
	for name, calls := range spent {
		add(name).Calls += calls
	}
	out := make([]adminSource, 0, len(order))
	for _, key := range order {
		out = append(out, *merged[key])
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Matches != out[j].Matches {
			return out[i].Matches > out[j].Matches
		}
		return out[i].Source < out[j].Source
	})
	return out
}

const adminCollectionSQL = `
SELECT count(*) FILTER (WHERE collected_at > NOW() - INTERVAL '1 day'),
       count(*) FILTER (WHERE collected_at > NOW() - INTERVAL '7 days'),
       count(*),
       max(collected_at)
  FROM CollectedMatches`

const adminReportsSQL = `
SELECT count(*) FILTER (WHERE generated_at > NOW() - INTERVAL '1 day'),
       count(*) FILTER (WHERE generated_at > NOW() - INTERVAL '7 days'),
       count(*),
       max(generated_at)
  FROM MatchReports`

const adminBudgetSQL = `
SELECT COALESCE(sum(calls) FILTER (WHERE day = CURRENT_DATE), 0),
       COALESCE(sum(calls) FILTER (WHERE day >= date_trunc('month',
                                                           CURRENT_DATE)), 0)
  FROM ApiBudget`

// AdminStatus — GET /api/v1/admin/status (роль admin).
func (h *Handlers) AdminStatus(w http.ResponseWriter, r *http.Request) {
	if h.DB == nil {
		writeProblemCtx(w, r, http.StatusServiceUnavailable,
			"about:blank", "storage unavailable", "нет подключения к базе")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	out := adminStatus{GeneratedAt: time.Now().UTC(), Sources: []adminSource{}}

	fail := func(err error) {
		writeProblemCtx(w, r, http.StatusInternalServerError,
			"about:blank", "status query failed", err.Error())
	}

	var roster []string
	rows, err := h.DB.Query(ctx, adminRosterSQL, adminRosterDays)
	if err != nil {
		fail(err)
		return
	}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			rows.Close()
			fail(err)
			return
		}
		roster = append(roster, name)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		fail(err)
		return
	}

	collected := map[string]collectedRow{}
	rows, err = h.DB.Query(ctx, adminCollectedSQL)
	if err != nil {
		fail(err)
		return
	}
	for rows.Next() {
		var name string
		var c collectedRow
		if err := rows.Scan(&name, &c.Matches, &c.WithReplay); err != nil {
			rows.Close()
			fail(err)
			return
		}
		collected[name] = c
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		fail(err)
		return
	}

	spent := map[string]int64{}
	rows, err = h.DB.Query(ctx, adminSpentSQL)
	if err != nil {
		fail(err)
		return
	}
	for rows.Next() {
		var name string
		var calls int64
		if err := rows.Scan(&name, &calls); err != nil {
			rows.Close()
			fail(err)
			return
		}
		spent[name] = calls
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		fail(err)
		return
	}
	out.Sources = mergeSources(roster, collected, spent)

	for _, q := range []struct {
		sql string
		dst *adminCounts
	}{{adminCollectionSQL, &out.Collection}, {adminReportsSQL, &out.Reports}} {
		if err := h.DB.QueryRow(ctx, q.sql).Scan(
			&q.dst.Day, &q.dst.Week, &q.dst.Total, &q.dst.Latest); err != nil {
			writeProblemCtx(w, r, http.StatusInternalServerError,
				"about:blank", "status query failed", err.Error())
			return
		}
	}

	if err := h.DB.QueryRow(ctx, adminBudgetSQL).Scan(
		&out.Budget.CallsToday, &out.Budget.CallsMonth); err != nil {
		writeProblemCtx(w, r, http.StatusInternalServerError,
			"about:blank", "status query failed", err.Error())
		return
	}
	out.Budget.UsdMonth = usdFor(out.Budget.CallsMonth)

	// Без кэширования и без ETag, в отличие от публичных ответов: это
	// страница дежурного, и показанное «всё хорошо» минутной давности
	// здесь хуже, чем медленная правда.
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, out)
}
