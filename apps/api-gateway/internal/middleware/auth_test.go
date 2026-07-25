package middleware

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/unmekeed/manta/api-gateway/internal/auth"
)

func withKeys(t *testing.T) *auth.Authenticator {
	t.Helper()
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	dir := t.TempDir()
	priv := filepath.Join(dir, "priv.pem")
	der, _ := x509.MarshalPKCS8PrivateKey(key)
	os.WriteFile(priv, pem.EncodeToMemory(
		&pem.Block{Type: "PRIVATE KEY", Bytes: der}), 0o600)
	t.Setenv("JWT_PRIVATE_KEY_FILE", priv)
	t.Setenv("JWT_PUBLIC_KEY_FILE", "")
	a, err := auth.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func authOKHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}
}

func do(h http.Handler, token string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(http.MethodGet, "/api/v1/x", nil)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestDisabledAuthPassesEverything(t *testing.T) {
	t.Setenv("JWT_PRIVATE_KEY_FILE", "")
	t.Setenv("JWT_PUBLIC_KEY_FILE", "")
	a, _ := auth.New(nil)
	h := Chain(authOKHandler(), Auth(a), RequireRole(a, auth.RoleAdmin))
	// Локальный стенд без ключей: закрытый эндпоинт обязан работать,
	// иначе обновление кода сломало бы рабочую машину.
	if got := do(h, "").Code; got != http.StatusOK {
		t.Fatalf("ожидался 200 при выключенной аутентификации, получен %d", got)
	}
}

func TestAnonymousRejectedOnGuardedRoute(t *testing.T) {
	a := withKeys(t)
	h := Chain(authOKHandler(), Auth(a), RequireRole(a, auth.RoleFree))
	if got := do(h, "").Code; got != http.StatusUnauthorized {
		t.Fatalf("без токена ожидался 401, получен %d", got)
	}
}

func TestInsufficientRoleForbidden(t *testing.T) {
	a := withKeys(t)
	tok, _, _ := a.Issue("acc", auth.RoleFree, "")
	h := Chain(authOKHandler(), Auth(a), RequireRole(a, auth.RolePremium))
	if got := do(h, tok).Code; got != http.StatusForbidden {
		t.Fatalf("free на premium-ресурсе: ожидался 403, получен %d", got)
	}
}

func TestSufficientRoleAllowed(t *testing.T) {
	a := withKeys(t)
	tok, _, _ := a.Issue("acc", auth.RoleAdmin, "")
	h := Chain(authOKHandler(), Auth(a), RequireRole(a, auth.RolePremium))
	if got := do(h, tok).Code; got != http.StatusOK {
		t.Fatalf("admin на premium-ресурсе: ожидался 200, получен %d", got)
	}
}

func TestInvalidTokenIs401(t *testing.T) {
	a := withKeys(t)
	h := Chain(authOKHandler(), Auth(a))
	if got := do(h, "не.jwt.вовсе").Code; got != http.StatusUnauthorized {
		t.Fatalf("мусорный токен: ожидался 401, получен %d", got)
	}
	// Схема должна быть именно Bearer.
	r := httptest.NewRequest(http.MethodGet, "/api/v1/x", nil)
	r.Header.Set("Authorization", "Basic dXNlcjpwYXNz")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("схема Basic: ожидался 401, получен %d", w.Code)
	}
}

func TestPublicRouteStaysAnonymous(t *testing.T) {
	a := withKeys(t)
	var seen string
	h := Chain(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		seen = RoleFrom(r.Context())
	}), Auth(a))
	do(h, "")
	if seen != auth.RoleAnonymous {
		t.Fatalf("публичный маршрут без токена: ожидалась роль anonymous, получена %q", seen)
	}
}

func TestClaimsReachHandler(t *testing.T) {
	a := withKeys(t)
	tok, issued, _ := a.Issue("account-42", auth.RolePro, "team_yearly")
	var got *auth.Claims
	h := Chain(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		got, _ = ClaimsFrom(r.Context())
	}), Auth(a))
	do(h, tok)
	if got == nil || got.Subject != "account-42" || got.Role != auth.RolePro ||
		got.ID != issued.ID {
		t.Fatalf("клеймы не доехали до обработчика: %+v", got)
	}
}
