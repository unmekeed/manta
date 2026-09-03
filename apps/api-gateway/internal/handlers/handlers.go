package handlers

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/unmekeed/manta/api-gateway/internal/auth"
	"github.com/unmekeed/manta/api-gateway/internal/events"
	"github.com/unmekeed/manta/api-gateway/internal/middleware"
	"github.com/unmekeed/manta/api-gateway/internal/storage"
	corev1 "github.com/unmekeed/manta/proto/core/v1"
)

// Handlers объединяет зависимости HTTP-обработчиков шлюза.
type Handlers struct {
	DB      *pgxpool.Pool
	Replays *storage.ReplayStore
	Draft   corev1.DraftServiceClient // nil — драфт-эндпоинты вернут 503
	Heroes  []Hero
	Auth    *auth.Authenticator // аутентификация (Гл. 9.2); nil-safe методы
}

// problem — тело ошибки в формате RFC 7807 (Гл. 7.5).
type problem struct {
	Type   string `json:"type"`
	Title  string `json:"title"`
	Status int    `json:"status"`
	Detail string `json:"detail,omitempty"`
	// TraceID дублирует заголовок X-Trace-Id в ТЕЛЕ (спринт 192).
	// Заголовок доходит до человека не всегда: скриншот ошибки,
	// пересланное в поддержку сообщение, лог фронтенда — везде остаётся
	// только тело. Идентификатор, по которому запрос ищется в наших
	// логах, обязан лежать там же, где текст ошибки.
	TraceID string `json:"trace_id,omitempty"`
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

// writeProblemCtx — то же, но с trace_id из контекста запроса.
func writeProblemCtx(w http.ResponseWriter, r *http.Request, status int,
	typ, title, detail string) {
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(problem{
		Type: typ, Title: title, Status: status, Detail: detail,
		TraceID: traceIDOf(r),
	})
}

func traceIDOf(r *http.Request) string {
	if r == nil {
		return ""
	}
	id, _ := r.Context().Value(middleware.TraceIDKey).(string)
	return id
}

// publicMaxAge — сколько публичному GET разрешено лежать в кэше.
//
// Минута, а не час: список пополняется несколько раз в час, а отчёт
// перегенерируется при смене версии модели. Долгий кэш показывал бы
// вчерашнюю выдачу как сегодняшнюю — то есть врал бы тихо.
const publicMaxAge = 60

// writePublicJSON отдаёт публичный GET с ETag и Cache-Control.
//
// ETag считается ПО ТЕЛУ, а не по времени генерации. Тело — ровно то, что
// увидит клиент, и совпадение тел означает, что перекачивать нечего.
// Метка по времени соврала бы в обе стороны: при перегенерации без
// изменений заставила бы качать заново, а при изменении внутри той же
// секунды отдала бы 304 на изменившийся ответ.
func writePublicJSON(w http.ResponseWriter, r *http.Request, v any) {
	body, err := json.Marshal(v)
	if err != nil {
		writeProblemCtx(w, r, http.StatusInternalServerError, "internal-error",
			"Failed to encode response", err.Error())
		return
	}
	writePublicBytes(w, r, body)
}

// writePublicBytes — то же для уже готового JSON (отчёты хранятся в базе
// как JSONB и отдаются как есть; перекладывать их через map ради ETag
// значило бы разбирать и собирать заново сотни килобайт на каждый
// запрос).
func writePublicBytes(w http.ResponseWriter, r *http.Request, body []byte) {
	sum := sha256.Sum256(body)
	etag := `"` + hex.EncodeToString(sum[:16]) + `"`
	w.Header().Set("ETag", etag)
	w.Header().Set("Cache-Control", "public, max-age="+strconv.Itoa(publicMaxAge))
	for _, candidate := range strings.Split(r.Header.Get("If-None-Match"), ",") {
		if c := strings.TrimSpace(candidate); c != "" && c == etag {
			w.WriteHeader(http.StatusNotModified)
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
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
		writeProblemCtx(w, r, http.StatusNotFound, "report-not-found",
			"Report is not generated yet",
			fmt.Sprintf("match %s: no report; загрузите реплей или дождитесь обработки", matchID))
		return
	}
	// Разбор и таймлайн — публичные GET, и они самые тяжёлые в API:
	// сотни килобайт на матч. Их кэширование экономит больше всего.
	writePublicBytes(w, r, body)
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

// ListMatches — GET /api/v1/matches: последние матчи с готовыми отчётами
// (для главной страницы фронтенда). Лёгкая проекция MatchReports.
