package handlers

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Список матчей для публичного сайта (спринт 192).
//
// ЧТО БЫЛО. Обработчик отдавал последние 50 отчётов без пагинации и без
// фильтров, а из полей карточки — match_id, финальную WP, narrative и
// дату. Для списка на сайте этого мало: нужны стороны, счёт,
// длительность, патч, уровень матча и пики.
//
// ОТКУДА ДАННЫЕ. Из MatchSummaries — карточки, которую пишет
// report-generator в момент генерации отчёта (миграция 015). Шлюз в
// ClickHouse не ходит: путь чтения в этом проекте не трогает ни витрину,
// ни модель, и список матчей — последнее место, где стоит это менять,
// потому что он будет самым горячим запросом сайта.
//
// ЛИСТАНИЕ — ПО match_id, А НЕ ПО ДАТЕ ОТЧЁТА. `generated_at` меняется
// при перегенерации (новая версия модели), и старый матч прыгает в
// начало списка: клиент, листающий по такому ключу, получает дубли и
// пропуски, причём каждая отдельная страница выглядит правильной.
// `match_id` у Valve монотонно растёт и неизменен.

const (
	matchesDefaultLimit = 20
	matchesMaxLimit     = 100
)

type matchCard struct {
	MatchID        int64     `json:"match_id"`
	RadiantWin     bool      `json:"radiant_win"`
	Winner         string    `json:"winner"` // "radiant" | "dire"
	KillsRadiant   int32     `json:"kills_radiant"`
	KillsDire      int32     `json:"kills_dire"`
	DurationS      int32     `json:"duration_s"`
	Patch          int32     `json:"patch"`
	Tier           string    `json:"tier"`
	RadiantHeroes  []string  `json:"radiant_heroes"`
	DireHeroes     []string  `json:"dire_heroes"`
	FinalRadiantWP *float64  `json:"final_radiant_wp"` // null — модель не считала
	GeneratedAt    time.Time `json:"generated_at"`
}

// ListMatches — GET /api/v1/matches?limit=&cursor=&patch=&tier=&query=
func (h *Handlers) ListMatches(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()

	limit, err := parseLimit(q.Get("limit"))
	if err != nil {
		writeProblemCtx(w, r, http.StatusBadRequest, "invalid-parameter",
			"Invalid limit", err.Error())
		return
	}
	cursor, err := parseCursor(q.Get("cursor"))
	if err != nil {
		writeProblemCtx(w, r, http.StatusBadRequest, "invalid-parameter",
			"Invalid cursor", err.Error())
		return
	}

	sql := `SELECT match_id, radiant_win, kills_radiant, kills_dire,
	               duration_s, patch, tier, radiant_heroes, dire_heroes,
	               final_radiant_wp, generated_at
	          FROM MatchSummaries WHERE TRUE`
	args := []any{}

	add := func(cond string, v any) {
		args = append(args, v)
		sql += " AND " + strings.Replace(cond, "?", "$"+strconv.Itoa(len(args)), 1)
	}

	if cursor > 0 {
		add("match_id < ?", cursor)
	}
	if s := q.Get("patch"); s != "" {
		p, err := strconv.Atoi(s)
		if err != nil {
			writeProblemCtx(w, r, http.StatusBadRequest, "invalid-parameter",
				"Invalid patch", "патч — целое число")
			return
		}
		add("patch = ?", p)
	}
	if s := q.Get("tier"); s != "" {
		add("tier = ?", s)
	}
	// Поиск. Строка из одних цифр — это номер матча, и никакой герой так
	// не называется, поэтому догадка однозначна. Иначе ищем героев, чьё
	// имя содержит запрос, и берём матчи, где хоть один из них играл —
	// за ЛЮБУЮ сторону: «матчи с Pudge» не должны зависеть от того, был
	// он за Radiant или за Dire.
	if s := strings.TrimSpace(q.Get("query")); s != "" {
		if id, err := strconv.ParseInt(s, 10, 64); err == nil {
			add("match_id = ?", id)
		} else {
			npcs := h.heroesMatching(s)
			if len(npcs) == 0 {
				// Ни один герой не подошёл — пустой список, а не вся
				// выдача. Отдать «всё» на непонятный запрос значит
				// молча соврать пользователю, что нашлось много.
				writeMatchPage(w, r, []matchCard{}, limit)
				return
			}
			add("(radiant_heroes || dire_heroes) && ?", npcs)
		}
	}

	// LIMIT берём на единицу больше запрошенного: лишняя строка — это
	// ответ на вопрос «есть ли следующая страница». Отдельный COUNT(*) по
	// тем же фильтрам стоил бы второго прохода по индексу ради факта,
	// который и так виден.
	args = append(args, limit+1)
	sql += " ORDER BY match_id DESC LIMIT $" + strconv.Itoa(len(args))

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	rows, err := h.DB.Query(ctx, sql, args...)
	if err != nil {
		writeProblemCtx(w, r, http.StatusInternalServerError, "internal-error",
			"Failed to list matches", err.Error())
		return
	}
	defer rows.Close()

	items := []matchCard{}
	for rows.Next() {
		var c matchCard
		if err := rows.Scan(&c.MatchID, &c.RadiantWin, &c.KillsRadiant,
			&c.KillsDire, &c.DurationS, &c.Patch, &c.Tier, &c.RadiantHeroes,
			&c.DireHeroes, &c.FinalRadiantWP, &c.GeneratedAt); err != nil {
			writeProblemCtx(w, r, http.StatusInternalServerError,
				"internal-error", "Failed to read match row", err.Error())
			return
		}
		c.Winner = "dire"
		if c.RadiantWin {
			c.Winner = "radiant"
		}
		items = append(items, c)
	}
	if err := rows.Err(); err != nil {
		writeProblemCtx(w, r, http.StatusInternalServerError, "internal-error",
			"Failed to list matches", err.Error())
		return
	}
	writeMatchPage(w, r, items, limit)
}

// writeMatchPage отрезает лишнюю строку и проставляет курсор следующей
// страницы. Курсор — номер последнего отданного матча: он неизменен, а
// значит ссылка на страницу не протухает от перегенерации отчётов.
func writeMatchPage(w http.ResponseWriter, r *http.Request,
	items []matchCard, limit int) {
	next := ""
	if len(items) > limit {
		items = items[:limit]
		next = strconv.FormatInt(items[len(items)-1].MatchID, 10)
	}
	body := map[string]any{"matches": items, "next_cursor": next}
	writePublicJSON(w, r, body)
}

func parseLimit(s string) (int, error) {
	if s == "" {
		return matchesDefaultLimit, nil
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return 0, errInvalid("limit — целое число")
	}
	// Верхняя граница — не вкусовщина: без неё один запрос с limit=100000
	// поднимает с диска всю таблицу и занимает соединение к базе,
	// которое нужно всем остальным.
	if n < 1 || n > matchesMaxLimit {
		return 0, errInvalid("limit от 1 до " + strconv.Itoa(matchesMaxLimit))
	}
	return n, nil
}

func parseCursor(s string) (int64, error) {
	if s == "" {
		return 0, nil
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n < 0 {
		return 0, errInvalid("курсор — номер матча из next_cursor")
	}
	return n, nil
}

// heroesMatching — npc-имена героев, чьё локализованное или внутреннее
// имя содержит подстроку. Регистр не важен, как и подчёркивания:
// пользователь пишет «anti mage», а в словаре лежит и «Anti-Mage», и
// «npc_dota_hero_antimage».
func (h *Handlers) heroesMatching(query string) []string {
	needle := normalizeHeroQuery(query)
	if needle == "" {
		return nil
	}
	out := []string{}
	for _, hero := range h.Heroes {
		if strings.Contains(normalizeHeroQuery(hero.Name), needle) ||
			strings.Contains(normalizeHeroQuery(hero.NPC), needle) {
			out = append(out, hero.NPC)
		}
	}
	return out
}

func normalizeHeroQuery(s string) string {
	r := strings.NewReplacer(" ", "", "-", "", "_", "", "'", "")
	return r.Replace(strings.ToLower(strings.TrimSpace(s)))
}

type paramError string

func (e paramError) Error() string { return string(e) }

func errInvalid(msg string) error { return paramError(msg) }
