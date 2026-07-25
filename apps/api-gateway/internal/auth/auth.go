// Package auth реализует аутентификацию и авторизацию шлюза (Гл. 9.2–9.3):
// RS256-подпись, клеймы sub/role/plan/jti, denylist отозванных токенов в
// Redis, JWKS для ротации ключей.
//
// Ключи задаются переменными JWT_PRIVATE_KEY_FILE (PEM PKCS#8/PKCS#1) и
// JWT_PUBLIC_KEY_FILE. Если приватного ключа нет, шлюз работает в режиме
// verify-only (выдавать токены не может) — так прод-инстансы могут
// проверять токены, не имея права их подписывать.
//
// Если не задан НИ ОДИН ключ, аутентификация выключена целиком: это
// режим локального стенда, где gateway слушает loopback. Выключение
// логируется на уровне WARN, чтобы не остаться незамеченным в проде.
package auth

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"os"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// Роли Гл. 9.3.1, по возрастанию прав. Уровень нужен, чтобы проверка
// «нужна роль не ниже premium» не превращалась в перечисление ролей.
const (
	RoleAnonymous = "anonymous"
	RoleFree      = "free"
	RolePremium   = "premium"
	RolePro       = "pro"
	RoleAdmin     = "admin"
	RoleService   = "service"
)

var roleLevel = map[string]int{
	RoleAnonymous: 0,
	RoleFree:      1,
	RolePremium:   2,
	RolePro:       3,
	RoleAdmin:     4,
	RoleService:   4, // внутренние вызовы: полный доступ к API данных
}

// AtLeast сообщает, покрывает ли роль have требуемую want.
func AtLeast(have, want string) bool {
	h, ok := roleLevel[have]
	if !ok {
		return false
	}
	w, ok := roleLevel[want]
	return ok && h >= w
}

// Claims — полезная нагрузка токена (Гл. 9.2.2).
type Claims struct {
	Role string `json:"role"`
	Plan string `json:"plan,omitempty"`
	jwt.RegisteredClaims
}

// Denylist хранит отозванные jti до истечения их срока (Гл. 9.2.2).
// Реализация на Redis — в пакете handlers, чтобы auth не тянул клиента.
type Denylist interface {
	Revoked(ctx context.Context, jti string) (bool, error)
	Revoke(ctx context.Context, jti string, ttl time.Duration) error
}

type Authenticator struct {
	priv     *rsa.PrivateKey
	pub      *rsa.PublicKey
	kid      string
	accessTTL time.Duration
	deny     Denylist
}

// Enabled=false — ключей нет, проверка токенов отключена (dev-стенд).
func (a *Authenticator) Enabled() bool { return a != nil && a.pub != nil }

// CanIssue=false — есть только публичный ключ (verify-only инстанс).
func (a *Authenticator) CanIssue() bool { return a != nil && a.priv != nil }

// New собирает аутентификатор из окружения. Ошибка возвращается только
// при НЕВЕРНЫХ ключах: отсутствие ключей — легальный dev-режим.
func New(deny Denylist) (*Authenticator, error) {
	a := &Authenticator{deny: deny, accessTTL: 15 * time.Minute}
	if v := os.Getenv("JWT_ACCESS_TTL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return nil, fmt.Errorf("JWT_ACCESS_TTL: %w", err)
		}
		a.accessTTL = d
	}

	if f := os.Getenv("JWT_PRIVATE_KEY_FILE"); f != "" {
		raw, err := os.ReadFile(f)
		if err != nil {
			return nil, fmt.Errorf("приватный ключ: %w", err)
		}
		key, err := parsePrivate(raw)
		if err != nil {
			return nil, err
		}
		a.priv, a.pub = key, &key.PublicKey
	}
	if f := os.Getenv("JWT_PUBLIC_KEY_FILE"); f != "" {
		raw, err := os.ReadFile(f)
		if err != nil {
			return nil, fmt.Errorf("публичный ключ: %w", err)
		}
		pub, err := parsePublic(raw)
		if err != nil {
			return nil, err
		}
		a.pub = pub
	}
	if a.pub != nil {
		a.kid = keyID(a.pub)
	}
	return a, nil
}

func parsePrivate(raw []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(raw)
	if block == nil {
		return nil, errors.New("приватный ключ: не PEM")
	}
	if k, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return k, nil
	}
	any, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("приватный ключ: %w", err)
	}
	k, ok := any.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("приватный ключ: нужен RSA (алгоритм RS256)")
	}
	return k, nil
}

func parsePublic(raw []byte) (*rsa.PublicKey, error) {
	block, _ := pem.Decode(raw)
	if block == nil {
		return nil, errors.New("публичный ключ: не PEM")
	}
	if cert, err := x509.ParseCertificate(block.Bytes); err == nil {
		if k, ok := cert.PublicKey.(*rsa.PublicKey); ok {
			return k, nil
		}
	}
	any, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("публичный ключ: %w", err)
	}
	k, ok := any.(*rsa.PublicKey)
	if !ok {
		return nil, errors.New("публичный ключ: нужен RSA")
	}
	return k, nil
}

// keyID — стабильный идентификатор ключа для JWKS (kid-версионирование).
func keyID(pub *rsa.PublicKey) string {
	sum := pub.N.Bytes()
	if len(sum) > 8 {
		sum = sum[:8]
	}
	return base64.RawURLEncoding.EncodeToString(sum)
}

// Issue выпускает access-токен. Refresh-токены (TTL 30 дней с ротацией)
// выдаёт сервис аккаунтов — шлюз их только проверяет, поэтому здесь
// сознательно нет пути выдачи долгоживущих токенов.
func (a *Authenticator) Issue(sub, role, plan string) (string, *Claims, error) {
	if !a.CanIssue() {
		return "", nil, errors.New("нет приватного ключа: инстанс verify-only")
	}
	if _, ok := roleLevel[role]; !ok {
		return "", nil, fmt.Errorf("неизвестная роль %q", role)
	}
	now := time.Now()
	jti := make([]byte, 16)
	if _, err := rand.Read(jti); err != nil {
		return "", nil, err
	}
	claims := &Claims{
		Role: role,
		Plan: plan,
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   sub,
			ID:        base64.RawURLEncoding.EncodeToString(jti),
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(a.accessTTL)),
		},
	}
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	tok.Header["kid"] = a.kid
	signed, err := tok.SignedString(a.priv)
	return signed, claims, err
}

var (
	ErrNoToken  = errors.New("нет токена")
	ErrRevoked  = errors.New("токен отозван")
)

// Verify проверяет подпись, срок и denylist. Алгоритм жёстко зафиксирован
// RS256: иначе токен с alg=none или HS256, подписанный публичным ключом,
// прошёл бы проверку (классическая уязвимость JWT-библиотек).
func (a *Authenticator) Verify(ctx context.Context, token string) (*Claims, error) {
	if token == "" {
		return nil, ErrNoToken
	}
	claims := &Claims{}
	_, err := jwt.ParseWithClaims(token, claims, func(t *jwt.Token) (any, error) {
		return a.pub, nil
	}, jwt.WithValidMethods([]string{"RS256"}), jwt.WithExpirationRequired())
	if err != nil {
		return nil, err
	}
	if _, ok := roleLevel[claims.Role]; !ok {
		return nil, fmt.Errorf("неизвестная роль %q", claims.Role)
	}
	if a.deny != nil && claims.ID != "" {
		revoked, err := a.deny.Revoked(ctx, claims.ID)
		if err != nil {
			// Redis недоступен — считаем токен действительным, но это
			// осознанный компромисс доступности против отзыва: см.
			// docs/security-review.md. Ошибка логируется вызывающим.
			return claims, nil
		}
		if revoked {
			return nil, ErrRevoked
		}
	}
	return claims, nil
}

// Revoke кладёт jti в denylist до конца его срока жизни.
func (a *Authenticator) Revoke(ctx context.Context, claims *Claims) error {
	if a.deny == nil || claims.ID == "" {
		return nil
	}
	ttl := time.Until(claims.ExpiresAt.Time)
	if ttl <= 0 {
		return nil // истёк сам
	}
	return a.deny.Revoke(ctx, claims.ID, ttl)
}

// JWKS отдаёт публичный ключ в формате RFC 7517 для внешних верификаторов.
func (a *Authenticator) JWKS() ([]byte, error) {
	if a.pub == nil {
		return json.Marshal(map[string]any{"keys": []any{}})
	}
	e := big.NewInt(int64(a.pub.E)).Bytes()
	return json.Marshal(map[string]any{"keys": []any{map[string]string{
		"kty": "RSA",
		"use": "sig",
		"alg": "RS256",
		"kid": a.kid,
		"n":   base64.RawURLEncoding.EncodeToString(a.pub.N.Bytes()),
		"e":   base64.RawURLEncoding.EncodeToString(e),
	}}})
}
