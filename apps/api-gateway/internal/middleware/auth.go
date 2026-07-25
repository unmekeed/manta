package middleware

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"

	"github.com/unmekeed/manta/api-gateway/internal/auth"
)

const ClaimsKey ctxKey = "claims"

// ClaimsFrom достаёт клеймы запроса. ok=false — запрос анонимный.
func ClaimsFrom(ctx context.Context) (*auth.Claims, bool) {
	c, ok := ctx.Value(ClaimsKey).(*auth.Claims)
	return c, ok
}

// RoleFrom — роль запроса; при выключенной аутентификации и без токена
// это anonymous.
func RoleFrom(ctx context.Context) string {
	if c, ok := ClaimsFrom(ctx); ok {
		return c.Role
	}
	return auth.RoleAnonymous
}

func problem(w http.ResponseWriter, status int, typ, title, detail string) {
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"type": typ, "title": title, "status": status, "detail": detail,
	})
}

// Auth проверяет Bearer-токен, если он есть, и кладёт клеймы в контекст.
// Отсутствие токена НЕ ошибка на этом слое — доступ решает RequireRole:
// публичные эндпоинты должны работать анонимно (Гл. 9.3.2).
// Невалидный/отозванный токен — 401 сразу: молча понижать до anonymous
// опасно, клиент должен узнать, что его сессия мертва.
func Auth(a *auth.Authenticator) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !a.Enabled() {
				next.ServeHTTP(w, r)
				return
			}
			hdr := r.Header.Get("Authorization")
			if hdr == "" {
				next.ServeHTTP(w, r)
				return
			}
			token, ok := strings.CutPrefix(hdr, "Bearer ")
			if !ok {
				problem(w, http.StatusUnauthorized, "unauthorized",
					"Unauthorized", "ожидается схема Bearer")
				return
			}
			claims, err := a.Verify(r.Context(), strings.TrimSpace(token))
			if err != nil {
				problem(w, http.StatusUnauthorized, "unauthorized",
					"Unauthorized", err.Error())
				return
			}
			ctx := context.WithValue(r.Context(), ClaimsKey, claims)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// RequireRole требует роль не ниже want (матрица доступа Гл. 9.3.2).
// При выключенной аутентификации пропускает всё — иначе локальный стенд
// без ключей перестал бы работать целиком.
func RequireRole(a *auth.Authenticator, want string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !a.Enabled() {
				next.ServeHTTP(w, r)
				return
			}
			claims, ok := ClaimsFrom(r.Context())
			if !ok {
				problem(w, http.StatusUnauthorized, "unauthorized",
					"Unauthorized", "требуется токен доступа")
				return
			}
			if !auth.AtLeast(claims.Role, want) {
				problem(w, http.StatusForbidden, "forbidden", "Forbidden",
					"требуется роль не ниже "+want)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
