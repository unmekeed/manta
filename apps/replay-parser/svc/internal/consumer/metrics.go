package consumer

import (
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Метрики парсера (реестр Гл. 11.2.2).
var (
	parseDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "replay_parse_duration_seconds",
		Help:    "Полное время обработки реплея (скачивание+разбор+загрузка).",
		Buckets: []float64{1, 2.5, 5, 10, 20, 40, 80, 160},
	})
	parsed = promauto.NewCounter(prometheus.CounterOpts{
		Name: "replays_parsed_total",
		Help: "Успешно разобранные реплеи.",
	})
	dlq = promauto.NewCounter(prometheus.CounterOpts{
		Name: "replays_dlq_total",
		Help: "События, отправленные в dlq.parser.",
	})
	// Дубликаты считаются ОТДЕЛЬНО от ошибок. Их рост — норма
	// at-least-once доставки; их рост ВМЕСТО разобранных реплеев —
	// признак того, что события публикуются по два раза, и это уже
	// повод смотреть. Свалив их в один счётчик с отказами, мы потеряли
	// бы оба смысла.
	duplicates = promauto.NewCounter(prometheus.CounterOpts{
		Name: "replays_duplicate_total",
		Help: "События о уже разобранных матчах (реплей удалён после разбора).",
	})
)

// ServeMetrics поднимает /metrics на addr (пустой addr — выключено).
func ServeMetrics(addr string) {
	if addr == "" {
		return
	}
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	go http.ListenAndServe(addr, mux) //nolint:errcheck — best effort
}
