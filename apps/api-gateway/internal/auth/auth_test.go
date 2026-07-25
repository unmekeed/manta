package auth

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// writeKeys кладёт свежую RSA-пару в файлы и настраивает окружение.
func writeKeys(t *testing.T) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	privPath := filepath.Join(dir, "priv.pem")
	privDER, _ := x509.MarshalPKCS8PrivateKey(key)
	os.WriteFile(privPath, pem.EncodeToMemory(
		&pem.Block{Type: "PRIVATE KEY", Bytes: privDER}), 0o600)
	pubPath := filepath.Join(dir, "pub.pem")
	pubDER, _ := x509.MarshalPKIXPublicKey(&key.PublicKey)
	os.WriteFile(pubPath, pem.EncodeToMemory(
		&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER}), 0o644)
	t.Setenv("JWT_PRIVATE_KEY_FILE", privPath)
	t.Setenv("JWT_PUBLIC_KEY_FILE", pubPath)
}

type memDeny struct{ revoked map[string]bool }

func (m *memDeny) Revoked(_ context.Context, jti string) (bool, error) {
	return m.revoked[jti], nil
}
func (m *memDeny) Revoke(_ context.Context, jti string, _ time.Duration) error {
	m.revoked[jti] = true
	return nil
}

func TestDisabledWithoutKeys(t *testing.T) {
	t.Setenv("JWT_PRIVATE_KEY_FILE", "")
	t.Setenv("JWT_PUBLIC_KEY_FILE", "")
	a, err := New(nil)
	if err != nil {
		t.Fatal(err)
	}
	if a.Enabled() || a.CanIssue() {
		t.Fatal("без ключей аутентификация должна быть выключена")
	}
}

func TestIssueAndVerify(t *testing.T) {
	writeKeys(t)
	a, err := New(nil)
	if err != nil {
		t.Fatal(err)
	}
	if !a.Enabled() || !a.CanIssue() {
		t.Fatal("с ключами ожидается режим issue+verify")
	}
	tok, claims, err := a.Issue("account-1", RolePremium, "premium_monthly")
	if err != nil {
		t.Fatal(err)
	}
	got, err := a.Verify(context.Background(), tok)
	if err != nil {
		t.Fatal(err)
	}
	if got.Subject != "account-1" || got.Role != RolePremium ||
		got.Plan != "premium_monthly" {
		t.Fatalf("клеймы не совпали: %+v", got)
	}
	if got.ID == "" || got.ID != claims.ID {
		t.Fatal("jti должен быть заполнен и стабилен")
	}
	if d := time.Until(got.ExpiresAt.Time); d > 16*time.Minute || d < 14*time.Minute {
		t.Fatalf("TTL access-токена должен быть ~15 минут, получено %v", d)
	}
}

func TestUnknownRoleRejected(t *testing.T) {
	writeKeys(t)
	a, _ := New(nil)
	if _, _, err := a.Issue("acc", "superuser", ""); err == nil {
		t.Fatal("выпуск токена с неизвестной ролью должен падать")
	}
}

// Классическая уязвимость JWT: токен с alg=none или HS256, подписанный
// публичным ключом как секретом. Проверяем, что оба отвергаются.
func TestAlgorithmConfusionRejected(t *testing.T) {
	writeKeys(t)
	a, _ := New(nil)

	none := jwt.NewWithClaims(jwt.SigningMethodNone, &Claims{
		Role: RoleAdmin,
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   "attacker",
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(time.Hour)),
		}})
	unsigned, _ := none.SignedString(jwt.UnsafeAllowNoneSignatureType)
	if _, err := a.Verify(context.Background(), unsigned); err == nil {
		t.Fatal("alg=none обязан отвергаться")
	}

	pubPEM, _ := os.ReadFile(os.Getenv("JWT_PUBLIC_KEY_FILE"))
	hs := jwt.NewWithClaims(jwt.SigningMethodHS256, &Claims{
		Role: RoleAdmin,
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   "attacker",
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(time.Hour)),
		}})
	forged, _ := hs.SignedString(pubPEM)
	if _, err := a.Verify(context.Background(), forged); err == nil {
		t.Fatal("HS256, подписанный публичным ключом, обязан отвергаться")
	}
}

func TestExpiredRejected(t *testing.T) {
	writeKeys(t)
	t.Setenv("JWT_ACCESS_TTL", "1ns")
	a, _ := New(nil)
	tok, _, err := a.Issue("acc", RoleFree, "")
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(2 * time.Millisecond)
	if _, err := a.Verify(context.Background(), tok); err == nil {
		t.Fatal("истёкший токен обязан отвергаться")
	}
}

func TestForeignKeyRejected(t *testing.T) {
	writeKeys(t)
	issuer, _ := New(nil)
	tok, _, _ := issuer.Issue("acc", RoleAdmin, "")

	writeKeys(t) // другая пара ключей
	other, _ := New(nil)
	if _, err := other.Verify(context.Background(), tok); err == nil {
		t.Fatal("токен, подписанный чужим ключом, обязан отвергаться")
	}
}

func TestRevocation(t *testing.T) {
	writeKeys(t)
	deny := &memDeny{revoked: map[string]bool{}}
	a, _ := New(deny)
	tok, claims, _ := a.Issue("acc", RoleFree, "")

	if _, err := a.Verify(context.Background(), tok); err != nil {
		t.Fatalf("свежий токен должен проходить: %v", err)
	}
	if err := a.Revoke(context.Background(), claims); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Verify(context.Background(), tok); err != ErrRevoked {
		t.Fatalf("отозванный токен: ожидалось ErrRevoked, получено %v", err)
	}
}

func TestRoleHierarchy(t *testing.T) {
	cases := []struct {
		have, want string
		ok         bool
	}{
		{RoleAdmin, RolePremium, true},
		{RolePro, RolePremium, true},
		{RolePremium, RolePremium, true},
		{RoleFree, RolePremium, false},
		{RoleAnonymous, RoleFree, false},
		{RoleService, RolePro, true},
		{"unknown", RoleFree, false},
	}
	for _, c := range cases {
		if got := AtLeast(c.have, c.want); got != c.ok {
			t.Errorf("AtLeast(%q, %q) = %v, ожидалось %v",
				c.have, c.want, got, c.ok)
		}
	}
}

func TestJWKS(t *testing.T) {
	writeKeys(t)
	a, _ := New(nil)
	raw, err := a.JWKS()
	if err != nil {
		t.Fatal(err)
	}
	var set struct {
		Keys []map[string]string `json:"keys"`
	}
	if err := json.Unmarshal(raw, &set); err != nil {
		t.Fatal(err)
	}
	if len(set.Keys) != 1 {
		t.Fatalf("ожидался один ключ, получено %d", len(set.Keys))
	}
	k := set.Keys[0]
	if k["kty"] != "RSA" || k["alg"] != "RS256" || k["kid"] == "" {
		t.Fatalf("некорректный JWK: %+v", k)
	}
	if _, err := base64.RawURLEncoding.DecodeString(k["n"]); err != nil {
		t.Fatalf("модуль n должен быть base64url: %v", err)
	}
	// Приватного материала в JWKS быть не должно.
	if strings.Contains(string(raw), "\"d\"") {
		t.Fatal("JWKS не должен содержать приватную экспоненту")
	}
}

func TestVerifyOnlyInstance(t *testing.T) {
	writeKeys(t)
	issuer, _ := New(nil)
	tok, _, _ := issuer.Issue("acc", RolePro, "")

	// Тот же публичный ключ, приватного нет.
	t.Setenv("JWT_PRIVATE_KEY_FILE", "")
	verifier, err := New(nil)
	if err != nil {
		t.Fatal(err)
	}
	if !verifier.Enabled() || verifier.CanIssue() {
		t.Fatal("ожидался режим verify-only")
	}
	if _, err := verifier.Verify(context.Background(), tok); err != nil {
		t.Fatalf("verify-only инстанс должен проверять токены: %v", err)
	}
	if _, _, err := verifier.Issue("x", RoleFree, ""); err == nil {
		t.Fatal("verify-only инстанс не должен выпускать токены")
	}
}
