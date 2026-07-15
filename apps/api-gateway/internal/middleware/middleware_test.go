package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func okHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
}

func TestTraceGeneratesID(t *testing.T) {
	h := Trace(okHandler())
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if got := rec.Header().Get("X-Trace-Id"); len(got) != 32 {
		t.Fatalf("expected 32-char trace id, got %q", got)
	}
}

func TestTracePropagatesTraceparent(t *testing.T) {
	h := Trace(okHandler())
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("traceparent", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
	h.ServeHTTP(rec, req)
	if got := rec.Header().Get("X-Trace-Id"); got != "0af7651916cd43dd8448eb211c80319c" {
		t.Fatalf("expected propagated trace id, got %q", got)
	}
}

func TestRateLimitExhaustsBurst(t *testing.T) {
	h := RateLimit(1, 3)(okHandler())
	var ok, limited int
	for i := 0; i < 10; i++ {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.RemoteAddr = "10.0.0.1:12345"
		h.ServeHTTP(rec, req)
		switch rec.Code {
		case http.StatusOK:
			ok++
		case http.StatusTooManyRequests:
			limited++
		}
	}
	if ok != 3 {
		t.Fatalf("expected exactly burst=3 allowed, got %d", ok)
	}
	if limited != 7 {
		t.Fatalf("expected 7 limited, got %d", limited)
	}
}

func TestRateLimitIsolatesClients(t *testing.T) {
	h := RateLimit(1, 1)(okHandler())
	for _, addr := range []string{"10.0.0.1:1", "10.0.0.2:1"} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.RemoteAddr = addr
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("first request from %s should pass, got %d", addr, rec.Code)
		}
	}
}
