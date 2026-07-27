// Package pii — псевдонимизация никнеймов на стороне шлюза (Гл. 9.7).
//
// Зеркало apps/feature-extractor/src/extractor/pseudonym.py. Экстрактор
// пишет в витрину псевдоним, шлюз хеширует ник из GDPR-запроса и ищет по
// нему — поэтому обе реализации обязаны давать один и тот же хеш.
// Совпадение проверяется кросс-языковым тестом (pii_test.go сверяется с
// зафиксированными векторами, они же зашиты в питоновский тест).
//
// Схема: HMAC-SHA256(salt, casefold(nick)), первые 16 hex-символов.
// Соль — MANTA_PII_SALT, вне git. Без неё соответствие «хеш ↔ ник»
// невосстановимо: голый sha256 по публичному списку ников подбирается
// перебором и псевдонимизацией не является.
package pii

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"strings"

	"golang.org/x/text/cases"
)

// HashLen — длина псевдонима в hex-символах (64 бита).
const HashLen = 16

const (
	modeEnv = "MANTA_PII_MODE"
	saltEnv = "MANTA_PII_SALT"
)

// Enabled сообщает, включён ли режим псевдонимизации. В режиме plain
// витрина хранит ник как раньше, и GDPR ищет по нему.
func Enabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(modeEnv)), "pseudonymize")
}

var caseFolder = cases.Fold()

// Pseudonym возвращает псевдоним никнейма. Пустой ник → пустая строка.
//
// Регистр сворачивается через cases.Fold (полный Unicode case folding,
// аналог питоновского str.casefold): strings.ToLower не эквивалентен и
// на нелатинских никах дал бы другой хеш, из-за чего GDPR-запрос не нашёл
// бы собственные строки.
func Pseudonym(nick string, salt []byte) string {
	nick = strings.TrimSpace(nick)
	if nick == "" {
		return ""
	}
	mac := hmac.New(sha256.New, salt)
	mac.Write([]byte(caseFolder.String(nick)))
	return hex.EncodeToString(mac.Sum(nil))[:HashLen]
}

// PseudonymEnv — Pseudonym с солью из окружения.
func PseudonymEnv(nick string) string {
	return Pseudonym(nick, []byte(os.Getenv(saltEnv)))
}
