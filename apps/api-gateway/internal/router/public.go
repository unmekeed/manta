package router

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/unmekeed/manta/api-gateway/internal/handlers"
	"github.com/unmekeed/manta/api-gateway/internal/middleware"
)

// Публичный слой API для сайта (спринт 192).
//
// ЗАЧЕМ ОТДЕЛЬНЫЙ РОУТЕР, А НЕ ФИЛЬТР В NGINX. Сайту нужны пять
// маршрутов. Остальные — загрузка реплеев, выпуск токенов, GDPR-экспорт
// и стирание — наружу смотреть не должны никогда.
//
// Это можно было сделать списком location в nginx. Тогда список
// разрешённого жил бы В КОНФИГЕ ПРОКСИ, а список существующего — в коде,
// и разъезжались бы они молча: новый приватный маршрут, случайно
// подошедший под префикс, оказался бы публичным, и узнали бы мы об этом
// от того, кто его нашёл.
//
// Здесь наоборот: публичный mux ЗНАЕТ только пять маршрутов, всё
// остальное отвечает 404 по построению, а не по правилу фильтрации.
// Забыть закрыть новый приватный эндпоинт невозможно — чтобы он стал
// публичным, его надо дописать сюда руками.
//
// Список и регистрация — ОДНО И ТО ЖЕ место (publicRoutes ниже): если бы
// список лежал отдельно «для теста», а регистрация отдельно, тест
// проверял бы список, а пользователь ходил бы в регистрацию.

// PublicRoutes — паттерны net/http, разрешённые наружу. Порядок не важен.
func PublicRoutes(h *handlers.Handlers) map[string]http.HandlerFunc {
	return map[string]http.HandlerFunc{
		"GET /api/v1/matches":                    h.ListMatches,
		"GET /api/v1/matches/{matchId}/analysis": h.GetMatchAnalysis,
		"GET /api/v1/matches/{matchId}/timeline": h.GetMatchTimeline,
		"GET /api/v1/heroes":                     h.ListHeroes,
		"POST /api/v1/draft/simulate":            h.SimulateDraft,
	}
}

// NewPublic собирает публичный слушатель: только разрешённые маршруты,
// CORS для доменов Manta и общий rate limit.
//
// Аутентификации здесь нет намеренно: все пять маршрутов и во внутреннем
// шлюзе доступны анонимно (матрица Гл. 9.3.2). Заголовок Authorization
// в CORS всё же разрешён — чтобы появление платных разделов не требовало
// менять preflight и ловить потом «почему браузер не шлёт токен».
func NewPublic(h *handlers.Handlers, logger *slog.Logger,
	rps, burst int, origins []string) http.Handler {
	mux := http.NewServeMux()
	for pattern, handler := range PublicRoutes(h) {
		mux.HandleFunc(pattern, handler)
	}

	return middleware.Chain(mux,
		middleware.Trace,
		middleware.Logging(logger),
		middleware.Metrics,
		CORS(origins),
		middleware.RateLimit(rps, burst),
	)
}

// CORS пускает браузер только с перечисленных источников.
//
// Ровно перечисленных: никаких «*» и никакого отражения любого Origin
// обратно в заголовок. Отражение выглядит как работающий CORS и снимает
// защиту целиком — любая страница в интернете сможет ходить в API от
// имени браузера пользователя.
//
// Пустой список источников — это ЗАПРЕТ всем, а не разрешение всем.
// Обратное умолчание означало бы, что забытая переменная окружения
// открывает API наружу, и выглядело бы это как исправная работа.
func CORS(origins []string) func(http.Handler) http.Handler {
	allowed := make(map[string]bool, len(origins))
	for _, o := range origins {
		if o = strings.TrimSpace(o); o != "" {
			allowed[o] = true
		}
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			origin := r.Header.Get("Origin")
			if origin != "" && allowed[origin] {
				w.Header().Set("Access-Control-Allow-Origin", origin)
				// Ответ зависит от Origin, и кэш обязан это знать: без
				// Vary промежуточный кэш отдал бы ответ, собранный для
				// одного источника, запросу с другого.
				w.Header().Add("Vary", "Origin")
				w.Header().Set("Access-Control-Allow-Methods",
					"GET, POST, OPTIONS")
				w.Header().Set("Access-Control-Allow-Headers",
					"Authorization, Content-Type, Idempotency-Key")
				w.Header().Set("Access-Control-Max-Age", "600")
			}
			// Preflight обрабатывается ДО mux: маршруты объявлены как
			// "GET /..." и "POST /...", и OPTIONS до них не дойдёт —
			// браузер получил бы 405 на каждый предварительный запрос.
			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
