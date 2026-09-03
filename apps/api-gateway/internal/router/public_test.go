package router

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	"github.com/unmekeed/manta/api-gateway/internal/handlers"
)

// Публичный слой: что наружу можно, а что нельзя (спринт 192).
//
// ГЛАВНЫЙ РИСК ЭТОГО КОДА — не ошибка в обработчике, а лишний маршрут.
// Наружу смотрит машина, на которой лежит вся платформа; загрузка
// реплеев, выпуск токенов, GDPR-экспорт и стирание данных субъекта
// открытыми быть не должны никогда. Ошибка такого рода не падает и не
// пишется в лог: эндпоинт просто работает, и узнаём мы о нём от того,
// кто его нашёл.

// Ожидаемый состав. Список записан здесь ВТОРОЙ РАЗ намеренно — это не
// дубль реализации, а утверждение о том, каким состав должен быть.
// Сверять реализацию с самой собой бессмысленно: тест был бы зелёным при
// любом её изменении, в том числе при добавлении шестого маршрута.
var wantPublic = []string{
	"GET /api/v1/heroes",
	"GET /api/v1/matches",
	"GET /api/v1/matches/{matchId}/analysis",
	"GET /api/v1/matches/{matchId}/timeline",
	"POST /api/v1/draft/simulate",
}

func TestPublicRoutesAreExactlyTheAllowedFive(t *testing.T) {
	got := []string{}
	for pattern := range PublicRoutes(&handlers.Handlers{}) {
		got = append(got, pattern)
	}
	sort.Strings(got)
	if strings.Join(got, "\n") != strings.Join(wantPublic, "\n") {
		t.Fatalf("состав публичного слоя изменился.\nбыло ожидаемо:\n%s\nстало:\n%s\n"+
			"Если маршрут добавлен намеренно — обнови список ЗДЕСЬ и объясни в "+
			"коммите, почему он безопасен наружу.",
			strings.Join(wantPublic, "\n"), strings.Join(got, "\n"))
	}
}

// Список выше проверяет намерение. Этот тест проверяет ЭФФЕКТ: что
// приватные пути на публичном слушателе действительно не отвечают.
// Разница существенная — маршрут мог бы попасть в mux мимо PublicRoutes.
func TestPrivateEndpointsAreNotServedPublicly(t *testing.T) {
	srv := httptest.NewServer(NewPublic(&handlers.Handlers{},
		slog.New(slog.NewTextHandler(io.Discard, nil)), 1000, 1000,
		[]string{"https://mantaml.com"}))
	defer srv.Close()

	private := []struct{ method, path string }{
		{"POST", "/api/v1/matches/upload"},
		{"POST", "/api/v1/auth/token"},
		{"POST", "/api/v1/auth/revoke"},
		{"GET", "/api/v1/auth/me"},
		{"GET", "/api/v1/players/42/export"},
		{"DELETE", "/api/v1/players/42/data"},
		{"GET", "/api/v1/jobs/abc"},
		{"GET", "/metrics"},
		{"GET", "/.well-known/jwks.json"},
	}
	for _, p := range private {
		req, err := http.NewRequest(p.method, srv.URL+p.path, nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		// 404 или 405 — оба означают «этого здесь нет». Что угодно
		// другое означает, что путь обслуживается.
		if resp.StatusCode != http.StatusNotFound &&
			resp.StatusCode != http.StatusMethodNotAllowed {
			t.Errorf("%s %s открыт наружу: %d", p.method, p.path, resp.StatusCode)
		}
	}
}

// -- CORS ---------------------------------------------------------------------

func TestCORSAllowsOnlyListedOrigins(t *testing.T) {
	h := CORS([]string{"https://mantaml.com"})(okHandler())

	allowed := requestWithOrigin(t, h, "https://mantaml.com")
	if allowed.Header().Get("Access-Control-Allow-Origin") != "https://mantaml.com" {
		t.Error("свой домен не пропущен")
	}
	stranger := requestWithOrigin(t, h, "https://evil.example")
	if stranger.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Error("чужой домен получил разрешение CORS")
	}
}

func TestCORSDoesNotReflectArbitraryOrigins(t *testing.T) {
	// ПОЙМАНО МЫСЛЕННОЙ МУТАЦИЕЙ: реализация вида «вернуть тот Origin,
	// что пришёл» проходит проверку «свой домен пропущен» и выглядит
	// работающим CORS, а на деле снимает защиту целиком.
	h := CORS(nil)(okHandler())
	rec := requestWithOrigin(t, h, "https://evil.example")
	if rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Error("пустой список источников разрешил чужой домен")
	}
}

func TestCORSAnswersPreflight(t *testing.T) {
	// Маршруты объявлены как "GET /..." и "POST /...", поэтому OPTIONS до
	// mux не дойдёт: без обработки preflight браузер получал бы 405 на
	// каждый предварительный запрос, и сайт не работал бы вовсе.
	h := CORS([]string{"https://mantaml.com"})(okHandler())
	req := httptest.NewRequest(http.MethodOptions, "/api/v1/matches", nil)
	req.Header.Set("Origin", "https://mantaml.com")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("preflight ответил %d", rec.Code)
	}
	if got := rec.Header().Get("Access-Control-Allow-Methods"); got != "GET, POST, OPTIONS" {
		t.Errorf("методы preflight: %q", got)
	}
	for _, want := range []string{"Authorization", "Content-Type", "Idempotency-Key"} {
		if !strings.Contains(rec.Header().Get("Access-Control-Allow-Headers"), want) {
			t.Errorf("заголовок %s не разрешён", want)
		}
	}
}

func TestCORSMarksResponseAsVaryingByOrigin(t *testing.T) {
	// Без Vary промежуточный кэш отдаст ответ, собранный для одного
	// источника, запросу с другого — то есть либо пустит чужого, либо
	// закроет своего. Оба исхода тихие.
	h := CORS([]string{"https://mantaml.com"})(okHandler())
	rec := requestWithOrigin(t, h, "https://mantaml.com")
	if !strings.Contains(rec.Header().Get("Vary"), "Origin") {
		t.Error("ответ не помечен как зависящий от Origin")
	}
}

func okHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
}

func requestWithOrigin(t *testing.T, h http.Handler, origin string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/matches", nil)
	req.Header.Set("Origin", origin)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}
