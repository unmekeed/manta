package router

import (
	"log/slog"
	"net/http"

	"github.com/unmekeed/manta/api-gateway/internal/handlers"
	"github.com/unmekeed/manta/api-gateway/internal/middleware"
)

// New собирает маршрутизатор шлюза с цепочкой middleware (Гл. 3.2, Гл. 7.3).
func New(h *handlers.Handlers, logger *slog.Logger, rps, burst int) http.Handler {
	mux := http.NewServeMux()

	// Служебные пробы и метрики (без rate limit)
	mux.HandleFunc("GET /healthz", h.Healthz)
	mux.HandleFunc("GET /readyz", h.Readyz)
	mux.Handle("GET /metrics", middleware.MetricsHandler())

	// Публичный API v1
	api := http.NewServeMux()
	api.HandleFunc("POST /api/v1/matches/upload", h.UploadReplay)
	api.HandleFunc("GET /api/v1/jobs/{jobId}", h.GetJob)
	api.HandleFunc("GET /api/v1/matches", h.ListMatches)
	api.HandleFunc("GET /api/v1/matches/{matchId}/timeline", h.GetMatchTimeline)
	api.HandleFunc("GET /api/v1/matches/{matchId}/analysis", h.GetMatchAnalysis)
	api.HandleFunc("GET /api/v1/matches/{matchId}/heatmap", h.GetMatchHeatmap)
	api.HandleFunc("GET /api/v1/players/{playerId}/profile", h.GetPlayerProfile)
	api.HandleFunc("GET /api/v1/meta/heroes", h.GetMetaHeroes)
	api.HandleFunc("POST /api/v1/draft/simulate", h.SimulateDraft)

	mux.Handle("/api/v1/", middleware.Chain(api,
		middleware.RateLimit(rps, burst),
	))

	return middleware.Chain(mux,
		middleware.Trace,
		middleware.Logging(logger),
		middleware.Metrics,
	)
}
