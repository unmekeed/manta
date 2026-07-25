package handlers

import (
	"encoding/json"
	"net/http"

	"github.com/unmekeed/manta/api-gateway/internal/auth"
	"github.com/unmekeed/manta/api-gateway/internal/middleware"
)

// Эндпоинты аутентификации (Гл. 9.2). Полноценные потоки входа (Steam
// OpenID, email+пароль, refresh-ротация) — зона сервиса аккаунтов; шлюз
// проверяет токены и отдаёт JWKS. IssueToken существует для
// service-to-service и отладки и требует роли admin.

// JWKS — GET /.well-known/jwks.json (RFC 7517): публичный ключ для
// внешних верификаторов, kid-версионирование при ротации.
func (h *Handlers) JWKS(w http.ResponseWriter, _ *http.Request) {
	body, err := h.Auth.JWKS()
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal-error",
			"JWKS failed", err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/jwk-set+json")
	w.Header().Set("Cache-Control", "public, max-age=300")
	_, _ = w.Write(body)
}

type issueRequest struct {
	Subject string `json:"sub"`
	Role    string `json:"role"`
	Plan    string `json:"plan,omitempty"`
}

// IssueToken — POST /api/v1/auth/token (роль admin). Выдаёт access-токен
// с TTL 15 минут (JWT_ACCESS_TTL).
func (h *Handlers) IssueToken(w http.ResponseWriter, r *http.Request) {
	if !h.Auth.CanIssue() {
		writeProblem(w, http.StatusServiceUnavailable, "service-unavailable",
			"Token issuance disabled",
			"нет приватного ключа (JWT_PRIVATE_KEY_FILE): инстанс verify-only")
		return
	}
	var req issueRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&req); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad-request",
			"Invalid body", err.Error())
		return
	}
	if req.Subject == "" || req.Role == "" {
		writeProblem(w, http.StatusBadRequest, "bad-request",
			"Invalid body", "обязательны sub и role")
		return
	}
	token, claims, err := h.Auth.Issue(req.Subject, req.Role, req.Plan)
	if err != nil {
		writeProblem(w, http.StatusBadRequest, "bad-request",
			"Token issue failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"access_token": token,
		"token_type":   "Bearer",
		"expires_at":   claims.ExpiresAt.Time,
		"role":         claims.Role,
		"jti":          claims.ID,
	})
}

// RevokeToken — POST /api/v1/auth/revoke: кладёт jti текущего токена в
// denylist до истечения его срока (Гл. 9.2.2). Роль — любая
// аутентифицированная: пользователь всегда вправе завершить свою сессию.
func (h *Handlers) RevokeToken(w http.ResponseWriter, r *http.Request) {
	claims, ok := middleware.ClaimsFrom(r.Context())
	if !ok {
		writeProblem(w, http.StatusUnauthorized, "unauthorized",
			"Unauthorized", "требуется токен доступа")
		return
	}
	if err := h.Auth.Revoke(r.Context(), claims); err != nil {
		writeProblem(w, http.StatusServiceUnavailable, "service-unavailable",
			"Revoke failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"revoked": claims.ID, "until": claims.ExpiresAt.Time})
}

// Me — GET /api/v1/auth/me: кто я по текущему токену (клиенту нужно знать
// свою роль, чтобы не показывать недоступные разделы).
func (h *Handlers) Me(w http.ResponseWriter, r *http.Request) {
	claims, ok := middleware.ClaimsFrom(r.Context())
	if !ok {
		writeJSON(w, http.StatusOK, map[string]any{
			"role": auth.RoleAnonymous, "authenticated": false})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"sub": claims.Subject, "role": claims.Role, "plan": claims.Plan,
		"authenticated": true, "expires_at": claims.ExpiresAt.Time})
}
