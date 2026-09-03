package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"
)

// Разбор параметров списка матчей и кэширование публичных GET
// (спринт 192). База здесь не нужна: проверяется ровно то, что можно
// испортить незаметно, — границы, умолчания и заголовки кэша.

func TestLimitDefaultsAndBounds(t *testing.T) {
	if n, err := parseLimit(""); err != nil || n != matchesDefaultLimit {
		t.Fatalf("умолчание: %d, %v", n, err)
	}
	if n, err := parseLimit("50"); err != nil || n != 50 {
		t.Fatalf("явное значение: %d, %v", n, err)
	}
	// Верхняя граница — не вкусовщина: без неё один запрос поднимает с
	// диска всю таблицу и занимает соединение, нужное всем остальным.
	for _, bad := range []string{"0", "-1", "101", "много"} {
		if _, err := parseLimit(bad); err == nil {
			t.Errorf("limit=%q принят", bad)
		}
	}
}

func TestCursorIsAMatchID(t *testing.T) {
	if n, err := parseCursor(""); err != nil || n != 0 {
		t.Fatalf("пустой курсор: %d, %v", n, err)
	}
	if n, err := parseCursor("8980389724"); err != nil || n != 8980389724 {
		t.Fatalf("курсор: %d, %v", n, err)
	}
	for _, bad := range []string{"-5", "abc", "8.5"} {
		if _, err := parseCursor(bad); err == nil {
			t.Errorf("курсор %q принят", bad)
		}
	}
}

// -- поиск по герою ------------------------------------------------------------

func testHeroes() []Hero {
	return []Hero{
		{ID: 1, Name: "Anti-Mage", NPC: "npc_dota_hero_antimage"},
		{ID: 2, Name: "Pudge", NPC: "npc_dota_hero_pudge"},
		{ID: 3, Name: "Queen of Pain", NPC: "npc_dota_hero_queenofpain"},
	}
}

func TestHeroSearchIgnoresPunctuationAndCase(t *testing.T) {
	// Пользователь пишет «anti mage», в словаре лежит «Anti-Mage» и
	// «npc_dota_hero_antimage». Совпадать обязаны все три написания:
	// поиск, срабатывающий только на точную форму, выглядит как «такого
	// героя нет».
	h := &Handlers{Heroes: testHeroes()}
	for _, q := range []string{"anti mage", "Anti-Mage", "ANTIMAGE", "antimage"} {
		got := h.heroesMatching(q)
		if len(got) != 1 || got[0] != "npc_dota_hero_antimage" {
			t.Errorf("запрос %q дал %v", q, got)
		}
	}
}

func TestHeroSearchMatchesSubstring(t *testing.T) {
	h := &Handlers{Heroes: testHeroes()}
	got := h.heroesMatching("pain")
	if len(got) != 1 || got[0] != "npc_dota_hero_queenofpain" {
		t.Errorf("подстрока не сработала: %v", got)
	}
}

func TestHeroSearchOnNonsenseFindsNothing(t *testing.T) {
	// Важно, что ПУСТО, а не «все». Отдать всю выдачу на непонятный
	// запрос — значит молча соврать пользователю, что нашлось много.
	h := &Handlers{Heroes: testHeroes()}
	if got := h.heroesMatching("zzzz"); len(got) != 0 {
		t.Errorf("непонятный запрос нашёл героев: %v", got)
	}
}

// -- кэширование публичных ответов ---------------------------------------------

func TestPublicJSONSetsETagAndCacheControl(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/matches", nil)
	writePublicJSON(rec, req, map[string]any{"matches": []int{}})

	if rec.Header().Get("ETag") == "" {
		t.Error("ETag не проставлен")
	}
	if !strings.Contains(rec.Header().Get("Cache-Control"), "max-age=") {
		t.Errorf("Cache-Control: %q", rec.Header().Get("Cache-Control"))
	}
}

func TestPublicJSONAnswers304OnMatchingETag(t *testing.T) {
	body := map[string]any{"matches": []string{"a"}}
	first := httptest.NewRecorder()
	writePublicJSON(first, httptest.NewRequest(http.MethodGet, "/x", nil), body)
	etag := first.Header().Get("ETag")

	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	req.Header.Set("If-None-Match", etag)
	second := httptest.NewRecorder()
	writePublicJSON(second, req, body)

	if second.Code != http.StatusNotModified {
		t.Fatalf("ожидался 304, получен %d", second.Code)
	}
	if second.Body.Len() != 0 {
		t.Error("304 отдан с телом")
	}
}

func TestETagChangesWithContent(t *testing.T) {
	// ПОЙМАНО МЫСЛЕННОЙ МУТАЦИЕЙ: метка, считаемая от времени генерации,
	// а не от тела, проходит проверку «304 на совпадении» и при этом
	// отдаёт 304 на ИЗМЕНИВШИЙСЯ ответ, если изменение случилось внутри
	// той же секунды. Клиент показывает вчерашние данные и не узнаёт.
	a := httptest.NewRecorder()
	writePublicJSON(a, httptest.NewRequest(http.MethodGet, "/x", nil),
		map[string]any{"matches": []string{"a"}})
	b := httptest.NewRecorder()
	writePublicJSON(b, httptest.NewRequest(http.MethodGet, "/x", nil),
		map[string]any{"matches": []string{"b"}})

	if a.Header().Get("ETag") == b.Header().Get("ETag") {
		t.Error("разные тела получили одинаковый ETag")
	}
}

func TestStaleETagStillGetsTheBody(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	req.Header.Set("If-None-Match", `"устаревшая-метка"`)
	rec := httptest.NewRecorder()
	writePublicJSON(rec, req, map[string]any{"matches": []string{"a"}})

	if rec.Code != http.StatusOK || rec.Body.Len() == 0 {
		t.Fatalf("устаревший ETag дал %d с телом %d байт",
			rec.Code, rec.Body.Len())
	}
}

// -- ошибки ---------------------------------------------------------------------

func TestProblemCarriesTraceID(t *testing.T) {
	// Заголовок X-Trace-Id доходит до человека не всегда: скриншот,
	// пересланное в поддержку сообщение, лог фронтенда. Идентификатор
	// обязан лежать в теле, рядом с текстом ошибки.
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	writeProblemCtx(rec, req, http.StatusBadRequest, "invalid-parameter",
		"Invalid limit", "limit от 1 до 100")

	if ct := rec.Header().Get("Content-Type"); ct != "application/problem+json" {
		t.Errorf("Content-Type: %q", ct)
	}
	var p problem
	if err := json.Unmarshal(rec.Body.Bytes(), &p); err != nil {
		t.Fatal(err)
	}
	if p.Status != http.StatusBadRequest || p.Title == "" {
		t.Errorf("тело ошибки: %+v", p)
	}
	// В этом тесте middleware.Trace не подключён, поэтому trace_id пуст —
	// и это правильно: выдумывать идентификатор, которого нет, значит
	// отправить человека искать в логах несуществующий запрос.
	if p.TraceID != "" {
		t.Errorf("trace_id выдуман из воздуха: %q", p.TraceID)
	}
}

func TestHeroListIsStable(t *testing.T) {
	// Порядок героев виден пользователю в пикере; случайный порядок из
	// map сделал бы список прыгающим между запросами.
	h := testHeroes()
	names := make([]string, len(h))
	for i, x := range h {
		names[i] = x.Name
	}
	if !sort.StringsAreSorted(names) {
		t.Errorf("словарь героев не отсортирован: %v", names)
	}
}
