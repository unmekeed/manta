package middleware

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"net"
	"net/http"
	"sync"
	"time"
)

type ctxKey string

const TraceIDKey ctxKey = "trace_id"

// Trace извлекает trace_id из заголовка W3C traceparent либо генерирует новый
// и кладёт его в контекст запроса и заголовок ответа (NFR-MNT-02).
func Trace(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		traceID := parseTraceparent(r.Header.Get("traceparent"))
		if traceID == "" {
			traceID = newTraceID()
		}
		w.Header().Set("X-Trace-Id", traceID)
		ctx := context.WithValue(r.Context(), TraceIDKey, traceID)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func parseTraceparent(tp string) string {
	// формат: version-traceid-spanid-flags
	if len(tp) >= 35 {
		return tp[3:35]
	}
	return ""
}

func newTraceID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// statusRecorder перехватывает код ответа для структурированного лога.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// Logging пишет структурированный JSON-лог по каждому запросу (Гл. 10.5).
func Logging(logger *slog.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
			next.ServeHTTP(rec, r)
			traceID, _ := r.Context().Value(TraceIDKey).(string)
			logger.Info("http_request",
				"method", r.Method,
				"path", r.URL.Path,
				"status", rec.status,
				"duration_ms", time.Since(start).Milliseconds(),
				"trace_id", traceID,
			)
		})
	}
}

// tokenBucket — минимальная реализация ограничителя частоты на клиента.
type tokenBucket struct {
	tokens   float64
	lastSeen time.Time
}

// RateLimit реализует token bucket по IP клиента (Гл. 2.4.2, Гл. 7.8).
// Продовая версия будет использовать Redis; для каркаса достаточно памяти процесса.
func RateLimit(rps, burst int) func(http.Handler) http.Handler {
	var (
		mu      sync.Mutex
		buckets = map[string]*tokenBucket{}
	)
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			host, _, err := net.SplitHostPort(r.RemoteAddr)
			if err != nil {
				host = r.RemoteAddr
			}
			mu.Lock()
			b, ok := buckets[host]
			now := time.Now()
			if !ok {
				b = &tokenBucket{tokens: float64(burst), lastSeen: now}
				buckets[host] = b
			}
			b.tokens += now.Sub(b.lastSeen).Seconds() * float64(rps)
			if b.tokens > float64(burst) {
				b.tokens = float64(burst)
			}
			b.lastSeen = now
			allowed := b.tokens >= 1
			if allowed {
				b.tokens--
			}
			mu.Unlock()

			if !allowed {
				w.Header().Set("Retry-After", "1")
				http.Error(w, `{"type":"rate-limited","title":"Too Many Requests","status":429}`,
					http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// Chain последовательно применяет middleware слева направо.
func Chain(h http.Handler, mws ...func(http.Handler) http.Handler) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}
