package router

import (
	"log/slog"
	"net/http"

	"github.com/unmekeed/manta/api-gateway/internal/auth"
	"github.com/unmekeed/manta/api-gateway/internal/handlers"
	"github.com/unmekeed/manta/api-gateway/internal/middleware"
)

// New собирает маршрутизатор шлюза с цепочкой middleware (Гл. 3.2, Гл. 7.3)
// и матрицей доступа Гл. 9.3.2.
//
// Матрица (указана минимально необходимая роль; проверка — «не ниже»):
//
//	anonymous — мета, список матчей, разборы, герои, драфт;
//	free      — загрузка реплеев (POST /matches/upload);
//	premium   — GDPR-экспорт данных субъекта;
//	admin     — выпуск токенов, стирание данных субъекта.
//
// GDPR-эндпоинты закрыты сознательно: экспорт и стирание данных по
// никнейму — это доступ к персональным данным, анонимно он недопустим
// (Гл. 9.3.3). Проверку владения ресурсом (свой ник против чужого) делает
// сервис аккаунтов, когда появится привязка аккаунта к никнейму.
func New(h *handlers.Handlers, a *auth.Authenticator, logger *slog.Logger,
	rps, burst int) http.Handler {
	mux := http.NewServeMux()

	// Служебные пробы, метрики и JWKS (без rate limit и авторизации).
	mux.HandleFunc("GET /healthz", h.Healthz)
	mux.HandleFunc("GET /readyz", h.Readyz)
	mux.Handle("GET /metrics", middleware.MetricsHandler())
	mux.HandleFunc("GET /.well-known/jwks.json", h.JWKS)

	api := http.NewServeMux()
	api.HandleFunc("GET /api/v1/jobs/{jobId}", h.GetJob)
	api.HandleFunc("GET /api/v1/matches", h.ListMatches)
	api.HandleFunc("GET /api/v1/matches/{matchId}/timeline", h.GetMatchTimeline)
	api.HandleFunc("GET /api/v1/matches/{matchId}/analysis", h.GetMatchAnalysis)
	api.HandleFunc("GET /api/v1/heroes", h.ListHeroes)
	api.HandleFunc("POST /api/v1/draft/simulate", h.SimulateDraft)
	api.HandleFunc("GET /api/v1/auth/me", h.Me)

	guarded := func(role string, handler http.HandlerFunc) http.Handler {
		return middleware.RequireRole(a, role)(handler)
	}
	api.Handle("POST /api/v1/matches/upload",
		guarded(auth.RoleFree, h.UploadReplay))
	api.Handle("POST /api/v1/auth/revoke",
		guarded(auth.RoleFree, h.RevokeToken))
	api.Handle("GET /api/v1/players/{playerId}/export",
		guarded(auth.RolePremium, h.ExportPlayerData))
	api.Handle("DELETE /api/v1/players/{playerId}/data",
		guarded(auth.RoleAdmin, h.ErasePlayerData))
	api.Handle("POST /api/v1/auth/token",
		guarded(auth.RoleAdmin, h.IssueToken))

	// Auth идёт ДО RateLimit: невалидный токен отбивается раньше, чем
	// расходуется бюджет лимитера.
	mux.Handle("/api/v1/", middleware.Chain(api,
		middleware.Auth(a),
		middleware.RateLimit(rps, burst),
	))

	return middleware.Chain(mux,
		middleware.Trace,
		middleware.Logging(logger),
		middleware.Metrics,
	)
}
