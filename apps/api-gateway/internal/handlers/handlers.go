package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/unmekeed/manta/api-gateway/internal/events"
	"github.com/unmekeed/manta/api-gateway/internal/middleware"
	"github.com/unmekeed/manta/api-gateway/internal/storage"
)

// Handlers объединяет зависимости HTTP-обработчиков шлюза.
type Handlers struct {
	DB      *pgxpool.Pool
	Replays *storage.ReplayStore
}

// problem — тело ошибки в формате RFC 7807 (Гл. 7.5).
type problem struct {
	Type   string `json:"type"`
	Title  string `json:"title"`
	Status int    `json:"status"`
	Detail string `json:"detail,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeProblem(w http.ResponseWriter, status int, typ, title, detail string) {
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(problem{Type: typ, Title: title, Status: status, Detail: detail})
}

// Healthz — liveness-проба: процесс жив (Гл. 11.8.2).
func (h *Handlers) Healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// Readyz — readiness-проба: шлюз готов принимать трафик, БД доступна.
func (h *Handlers) Readyz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := h.DB.Ping(ctx); err != nil {
		writeProblem(w, http.StatusServiceUnavailable,
			"service-unavailable", "Dependency not ready", "postgres: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

// UploadReplay принимает файл реплея и ставит задание в очередь (UC-01):
// файл выгружается в S3, затем в одной транзакции создаются AnalysisJob и
// outbox-событие match.downloaded; relay доставит его в Kafka (Гл. 2.5).
func (h *Handlers) UploadReplay(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseMultipartForm(64 << 20); err != nil {
		writeProblem(w, http.StatusBadRequest,
			"invalid-replay", "Invalid multipart form", err.Error())
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeProblem(w, http.StatusBadRequest,
			"invalid-replay", "Missing file field", err.Error())
		return
	}
	defer file.Close()

	// Минимальная валидация: непустой файл (SEC: полная проверка магии
	// формата выполняется парсером в изолированной среде).
	if header.Size == 0 {
		writeProblem(w, http.StatusBadRequest, "invalid-replay", "Empty file", "")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	traceID, _ := ctx.Value(middleware.TraceIDKey).(string)
	objectKey := fmt.Sprintf("uploads/%d-%s", time.Now().UnixNano(), header.Filename)
	replayURL, err := h.Replays.PutReplay(ctx, objectKey, file, header.Size)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError,
			"internal-error", "Failed to store replay", err.Error())
		return
	}

	tx, err := h.DB.Begin(ctx)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError,
			"internal-error", "Failed to begin transaction", err.Error())
		return
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	var jobID string
	if err := tx.QueryRow(ctx,
		`INSERT INTO AnalysisJobs (status, replay_url) VALUES ('queued', $1) RETURNING job_id`,
		replayURL,
	).Scan(&jobID); err != nil {
		writeProblem(w, http.StatusInternalServerError,
			"internal-error", "Failed to enqueue job", err.Error())
		return
	}

	env, err := events.NewEnvelope("match.downloaded", traceID, "job_id:"+jobID, map[string]any{
		"job_id":     jobID,
		"replay_url": replayURL,
		"source":     "user_upload",
	})
	if err == nil {
		err = events.WriteOutbox(ctx, tx, "match.downloaded", env)
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError,
			"internal-error", "Failed to write outbox event", err.Error())
		return
	}

	if err := tx.Commit(ctx); err != nil {
		writeProblem(w, http.StatusInternalServerError,
			"internal-error", "Failed to commit", err.Error())
		return
	}

	writeJSON(w, http.StatusAccepted, map[string]any{
		"job_id":                 jobID,
		"replay_url":             replayURL,
		"estimated_time_seconds": 10,
	})
}

// GetJob возвращает статус задания анализа.
func (h *Handlers) GetJob(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("jobId")
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()

	var status string
	var createdAt time.Time
	err := h.DB.QueryRow(ctx,
		`SELECT status, created_at FROM AnalysisJobs WHERE job_id = $1`, jobID,
	).Scan(&status, &createdAt)
	if err != nil {
		writeProblem(w, http.StatusNotFound, "not-found", "Job not found", jobID)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"job_id":     jobID,
		"status":     status,
		"created_at": createdAt,
	})
}

// reportColumn отдаёт JSONB-колонку отчёта из MatchReports как есть:
// отчёт материализован Report Generator'ом, путь чтения — один SELECT.
func (h *Handlers) reportColumn(w http.ResponseWriter, r *http.Request,
	column string) {
	matchID := r.PathValue("matchId")
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	var body []byte
	// column подставляется только из фиксированного списка вызовов ниже.
	err := h.DB.QueryRow(ctx,
		`SELECT `+column+`::text FROM MatchReports WHERE match_id = $1`,
		matchID).Scan(&body)
	if err != nil {
		writeProblem(w, http.StatusNotFound, "report-not-found",
			"Report is not generated yet",
			fmt.Sprintf("match %s: no report; загрузите реплей или дождитесь обработки", matchID))
		return
	}
	// NULL-колонка (отчёт сгенерирован до появления поля, напр. heatmap
	// из миграции 004): 404 до перегенерации отчёта.
	if len(body) == 0 {
		writeProblem(w, http.StatusNotFound, "report-not-found",
			"Report section is not generated yet",
			fmt.Sprintf("match %s: отчёт старой версии, поле %s появится после перегенерации", matchID, column))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

// GetMatchTimeline — GET /api/v1/matches/{matchId}/timeline (схема Timeline):
// поминутная WP-кривая и разница net worth.
func (h *Handlers) GetMatchTimeline(w http.ResponseWriter, r *http.Request) {
	h.reportColumn(w, r, "timeline")
}

// GetMatchAnalysis — GET /api/v1/matches/{matchId}/analysis (схема
// MatchAnalysis): итоговая WP, оценки игроков, нарратив.
func (h *Handlers) GetMatchAnalysis(w http.ResponseWriter, r *http.Request) {
	h.reportColumn(w, r, "analysis")
}

// GetMatchHeatmap — GET /api/v1/matches/{matchId}/heatmap (Гл. 7):
// разреженные сетки плотности позиций по игрокам (grid 64x64).
func (h *Handlers) GetMatchHeatmap(w http.ResponseWriter, r *http.Request) {
	h.reportColumn(w, r, "heatmap")
}

// GetPlayerProfile — GET /api/v1/players/{playerId}/profile (Гл. 7):
// материализованные агрегаты игрока (PlayerProfiles, миграция 005).
// playerId — steam64 (account_id из реплеев).
func (h *Handlers) GetPlayerProfile(w http.ResponseWriter, r *http.Request) {
	playerID := r.PathValue("playerId")
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	var (
		nickname, mainLane string
		matches, wins      int
		avgGPM, avgXPM     float64
		topHeroes          []byte
		updatedAt          time.Time
	)
	err := h.DB.QueryRow(ctx,
		`SELECT nickname, matches, wins, avg_gpm, avg_xpm, main_lane,
		        top_heroes, updated_at
		   FROM PlayerProfiles WHERE account_id = $1`, playerID,
	).Scan(&nickname, &matches, &wins, &avgGPM, &avgXPM, &mainLane,
		&topHeroes, &updatedAt)
	if err != nil {
		writeProblem(w, http.StatusNotFound, "not-found",
			"Player profile not found",
			fmt.Sprintf("player %s: профиль появится после первого проанализированного матча", playerID))
		return
	}
	winrate := 0.0
	if matches > 0 {
		winrate = float64(wins) / float64(matches)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"player_id":  playerID,
		"nickname":   nickname,
		"matches":    matches,
		"wins":       wins,
		"winrate":    winrate,
		"avg_gpm":    avgGPM,
		"avg_xpm":    avgXPM,
		"main_lane":  mainLane,
		"top_heroes": json.RawMessage(topHeroes),
		"updated_at": updatedAt,
	})
}

// GetMetaHeroes — GET /api/v1/meta/heroes (Гл. 7): мета героев из
// материализованной MetaHeroes (миграция 006). ban_rate из контракта
// недоступен (драфт-события не извлекаются) и не возвращается.
func (h *Handlers) GetMetaHeroes(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	rows, err := h.DB.Query(ctx, `
		SELECT hero, hero_id, matches, wins, winrate, shrunk_winrate,
		       pick_rate, avg_gpm, updated_at
		  FROM MetaHeroes ORDER BY matches DESC, hero`)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError,
			"internal-error", "Failed to list meta", err.Error())
		return
	}
	defer rows.Close()

	type item struct {
		Hero          string    `json:"hero"`
		HeroID        int       `json:"hero_id"`
		Matches       int       `json:"matches"`
		Wins          int       `json:"wins"`
		Winrate       float64   `json:"winrate"`
		ShrunkWinrate float64   `json:"shrunk_winrate"`
		PickRate      float64   `json:"pick_rate"`
		AvgGPM        float64   `json:"avg_gpm"`
		UpdatedAt     time.Time `json:"updated_at"`
	}
	items := []item{}
	for rows.Next() {
		var it item
		if err := rows.Scan(&it.Hero, &it.HeroID, &it.Matches, &it.Wins,
			&it.Winrate, &it.ShrunkWinrate, &it.PickRate, &it.AvgGPM,
			&it.UpdatedAt); err != nil {
			continue
		}
		items = append(items, it)
	}
	writeJSON(w, http.StatusOK, map[string]any{"heroes": items})
}

// ListMatches — GET /api/v1/matches: последние матчи с готовыми отчётами
// (для главной страницы фронтенда). Лёгкая проекция MatchReports.
func (h *Handlers) ListMatches(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	rows, err := h.DB.Query(ctx, `
		SELECT match_id,
		       analysis->'win_probability'->>'final_radiant',
		       analysis->>'narrative',
		       COALESCE(analysis->>'report_version', ''),
		       generated_at
		  FROM MatchReports ORDER BY generated_at DESC LIMIT 50`)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError,
			"internal-error", "Failed to list matches", err.Error())
		return
	}
	defer rows.Close()

	type item struct {
		MatchID       int64     `json:"match_id"`
		FinalRadiant  string    `json:"final_radiant_wp"`
		Narrative     string    `json:"narrative"`
		ReportVersion string    `json:"report_version"`
		GeneratedAt   time.Time `json:"generated_at"`
	}
	items := []item{}
	for rows.Next() {
		var it item
		if err := rows.Scan(&it.MatchID, &it.FinalRadiant, &it.Narrative,
			&it.ReportVersion, &it.GeneratedAt); err != nil {
			continue
		}
		items = append(items, it)
	}
	writeJSON(w, http.StatusOK, map[string]any{"matches": items})
}
