package middleware

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Метрики шлюза (имена — реестр Гл. 11.2.2).
var (
	requestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "api_request_duration_seconds",
		Help:    "Длительность HTTP-запросов шлюза.",
		Buckets: prometheus.DefBuckets,
	}, []string{"method", "route", "status"})

	jobCompletions = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "job_completion_rate",
		Help: "Завершения заданий анализа по статусам (done/failed).",
	}, []string{"status"})
)

// JobCompleted инкрементирует счётчик завершённых заданий (jobstatus-консьюмер).
func JobCompleted(status string) { jobCompletions.WithLabelValues(status).Inc() }

// MetricsHandler отдаёт /metrics в формате Prometheus.
func MetricsHandler() http.Handler { return promhttp.Handler() }

// Metrics измеряет длительность и статус каждого запроса. Роутом служит
// шаблон маршрута (mux.Pattern), а не сырой путь — иначе кардинальность
// взорвётся на match_id.
func Metrics(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		route := r.Pattern
		if route == "" {
			route = "unmatched"
		}
		requestDuration.WithLabelValues(
			r.Method, route, strconv.Itoa(rec.status),
		).Observe(time.Since(start).Seconds())
	})
}
