package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// GDPR-эндпоинты (Гл. 9.7): право на экспорт и право на удаление.
//
// Что считается персональными данными в Manta (см. docs/security-review.md
// §4): единственный PII — игровой никнейм, публично отдаваемый OpenDota.
// Он лежит в двух местах:
//   - ClickHouse PlayerMatchFeatures.player_name;
//   - Postgres MatchReports.analysis (jsonb, внутри players[]).
// Реальных account_id платформа не хранит (player_id витрины — слот 0–9),
// поэтому субъект данных идентифицируется НИКНЕЙМОМ: {playerId} в путях
// ниже — это никнейм. Так честнее, чем делать вид, что есть внутренний id.
//
// Удаление реализовано как анонимизация: строки матчей — не персональные
// данные и нужны моделям, стирается именно связь «никнейм ↔ статистика».
// Это соответствует принципу минимизации: после операции по субъекту
// нельзя найти ни одной записи.

type chClient struct {
	url, db, user, password string
}

func newCHClient() *chClient {
	return &chClient{
		url:      envOr("CLICKHOUSE_URL", "http://localhost:8123"),
		db:       envOr("CLICKHOUSE_DB", "manta"),
		user:     envOr("CLICKHOUSE_USER", "dota"),
		password: envOr("CLICKHOUSE_PASSWORD", "dota_dev_password"),
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func (c *chClient) do(ctx context.Context, query string, format string) ([]byte, error) {
	q := url.Values{"database": {c.db}}
	if format != "" {
		q.Set("default_format", format)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		c.url+"/?"+q.Encode(), strings.NewReader(query))
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-ClickHouse-User", c.user)
	req.Header.Set("X-ClickHouse-Key", c.password)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("clickhouse %d: %s", resp.StatusCode,
			truncate(string(body), 200))
	}
	return body, nil
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}

// chQuote экранирует строку для литерала ClickHouse. Никнейм приходит из
// пути запроса — без экранирования это была бы SQL-инъекция.
func chQuote(s string) string {
	r := strings.NewReplacer(`\`, `\\`, `'`, `\'`)
	return "'" + r.Replace(s) + "'"
}

func playerFromPath(r *http.Request) string {
	id, _ := url.PathUnescape(r.PathValue("playerId"))
	return strings.TrimSpace(id)
}

// ExportPlayerData — GET /api/v1/players/{playerId}/export (Гл. 9.7,
// право на переносимость). Отдаёт JSON со всеми записями субъекта.
func (h *Handlers) ExportPlayerData(w http.ResponseWriter, r *http.Request) {
	player := playerFromPath(r)
	if player == "" {
		writeProblem(w, http.StatusBadRequest, "bad-request",
			"Invalid player", "пустой идентификатор игрока")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()

	ch := newCHClient()
	raw, err := ch.do(ctx, fmt.Sprintf(
		`SELECT match_id, team, hero, player_name, won, gpm, xpm,
		        lh_at_10, dn_at_10, lane, tier
		   FROM PlayerMatchFeatures FINAL
		  WHERE player_name = %s ORDER BY match_id`, chQuote(player)),
		"JSONEachRow")
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal-error",
			"Export failed", err.Error())
		return
	}
	var matches []json.RawMessage
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if line != "" {
			matches = append(matches, json.RawMessage(line))
		}
	}

	// Отчёты, в которых субъект упомянут (jsonb-путь players[].player_name).
	var reports []map[string]any
	rows, err := h.DB.Query(ctx, `
		SELECT match_id, generated_at
		  FROM MatchReports
		 WHERE analysis->'players' @> $1::jsonb
		 ORDER BY generated_at DESC`,
		fmt.Sprintf(`[{"player_name": %q}]`, player))
	if err == nil {
		defer rows.Close()
		for rows.Next() {
			var id int64
			var at time.Time
			if err := rows.Scan(&id, &at); err == nil {
				reports = append(reports, map[string]any{
					"match_id": id, "generated_at": at})
			}
		}
	}

	w.Header().Set("Content-Disposition",
		fmt.Sprintf("attachment; filename=%q", "manta-export-"+player+".json"))
	writeJSON(w, http.StatusOK, map[string]any{
		"subject":       player,
		"exported_at":   time.Now().UTC(),
		"note":          "идентификатор субъекта — игровой никнейм: платформа не хранит account_id (docs/security-review.md §4)",
		"match_records": matches,
		"reports":       reports,
		"counts": map[string]int{
			"match_records": len(matches), "reports": len(reports)},
	})
}

// ErasePlayerData — DELETE /api/v1/players/{playerId}/data (Гл. 9.7,
// право на удаление). Анонимизирует никнейм в витрине и в отчётах.
//
// В ClickHouse это мутация ALTER TABLE … UPDATE: она асинхронна, поэтому
// ответ 202 Accepted, а не 204 — врать про мгновенное удаление нельзя.
// Postgres обновляется синхронно в той же операции.
func (h *Handlers) ErasePlayerData(w http.ResponseWriter, r *http.Request) {
	player := playerFromPath(r)
	if player == "" {
		writeProblem(w, http.StatusBadRequest, "bad-request",
			"Invalid player", "пустой идентификатор игрока")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()

	ch := newCHClient()
	before, err := ch.do(ctx, fmt.Sprintf(
		`SELECT count() FROM PlayerMatchFeatures FINAL WHERE player_name = %s`,
		chQuote(player)), "TabSeparated")
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal-error",
			"Erasure failed", err.Error())
		return
	}
	if _, err := ch.do(ctx, fmt.Sprintf(
		`ALTER TABLE PlayerMatchFeatures UPDATE player_name = '' WHERE player_name = %s`,
		chQuote(player)), ""); err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal-error",
			"Erasure failed", err.Error())
		return
	}

	// jsonb_set по каждому элементу players[] с этим никнеймом.
	tag, err := h.DB.Exec(ctx, `
		UPDATE MatchReports SET analysis = jsonb_set(analysis, '{players}', (
		    SELECT jsonb_agg(CASE WHEN p->>'player_name' = $1
		                          THEN jsonb_set(p, '{player_name}', '""'::jsonb)
		                          ELSE p END)
		      FROM jsonb_array_elements(analysis->'players') p))
		 WHERE analysis->'players' @> $2::jsonb`,
		player, fmt.Sprintf(`[{"player_name": %q}]`, player))
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal-error",
			"Erasure failed (postgres)", err.Error())
		return
	}

	writeJSON(w, http.StatusAccepted, map[string]any{
		"subject":            player,
		"accepted_at":        time.Now().UTC(),
		"match_records":      strings.TrimSpace(string(before)),
		"reports_updated":    tag.RowsAffected(),
		"clickhouse_mutation": "асинхронная: SELECT * FROM system.mutations WHERE table='PlayerMatchFeatures' AND is_done=0",
		"note":               "никнейм стёрт; обезличенная игровая статистика сохранена (принцип минимизации, Гл. 9.7)",
	})
}
