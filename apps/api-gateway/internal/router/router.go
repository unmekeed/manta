package router

import (
	"log/slog"
	"net/http"

	"github.com/dota-ai-analyst/api-gateway/internal/handlers"
	"github.com/dota-ai-analyst/api-gateway/internal/middleware"
)

// New собирает маршрутизатор шлюза с цепочкой middleware (Гл. 3.2, Гл. 7.3).
func New(h *handlers.Handlers, logger *slog.Logger, rps, burst int) http.Handler {
	mux := http.NewServeMux()

	// Служебные пробы (без rate limit)
	mux.HandleFunc("GET /healthz", h.Healthz)
	mux.HandleFunc("GET /readyz", h.Readyz)

	// Публичный API v1
	api := http.NewServeMux()
	api.HandleFunc("POST /api/v1/matches/upload", h.UploadReplay)
	api.HandleFunc("GET /api/v1/jobs/{jobId}", h.GetJob)
	api.HandleFunc("GET /api/v1/matches/{matchId}/timeline", h.GetMatchTimeline)
	api.HandleFunc("GET /api/v1/matches/{matchId}/analysis", h.GetMatchAnalysis)

	mux.Handle("/api/v1/", middleware.Chain(api,
		middleware.RateLimit(rps, burst),
	))

	return middleware.Chain(mux,
		middleware.Trace,
		middleware.Logging(logger),
	)
}
