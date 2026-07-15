// Package config — конфигурация parser-svc из переменных окружения
// (12-factor, Гл. 13.2 спецификации).
package config

import (
	"os"
	"strings"
)

type Config struct {
	// Kafka.
	Brokers  []string
	GroupID  string
	TopicIn  string // match.downloaded
	TopicOut string // replay.parsed
	TopicDLQ string // dlq.parser

	// Хранилище реплеев (MinIO/S3).
	S3Endpoint  string
	S3AccessKey string
	S3SecretKey string
	S3UseSSL    bool

	// Аналитический слой.
	ClickHouseURL      string // http://host:8123
	ClickHouseDB       string
	ClickHouseUser     string
	ClickHousePassword string

	// C++ ядро.
	DemoinfoPath string // путь к бинарю demoinfo
	WorkDir      string // каталог для временных .dem и .jsonl

	// Удалять реплей из S3 после успешного разбора: сырые события уже
	// в ClickHouse, а бесконечное накопление .dem заполняет диск
	// (инцидент XMinioStorageFull). Выключено по умолчанию — прод может
	// хотеть архив.
	PurgeParsedReplays bool

	// Адрес /metrics (Prometheus); пусто — выключено.
	MetricsAddr string
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func FromEnv() Config {
	return Config{
		Brokers:  strings.Split(getenv("KAFKA_BROKERS", "localhost:9092"), ","),
		GroupID:  getenv("KAFKA_GROUP_ID", "replay-parser"),
		TopicIn:  getenv("TOPIC_IN", "match.downloaded"),
		TopicOut: getenv("TOPIC_OUT", "replay.parsed"),
		TopicDLQ: getenv("TOPIC_DLQ", "dlq.parser"),

		// 9500 — маппинг MinIO в dev docker-compose (9000 занят нативным
		// портом ClickHouse).
		S3Endpoint:  getenv("S3_ENDPOINT", "localhost:9500"),
		S3AccessKey: getenv("S3_ACCESS_KEY", "dota"),
		S3SecretKey: getenv("S3_SECRET_KEY", "dota_dev_password"),
		S3UseSSL:    getenv("S3_USE_SSL", "") == "true",

		ClickHouseURL:      getenv("CLICKHOUSE_URL", "http://localhost:8123"),
		ClickHouseDB:       getenv("CLICKHOUSE_DB", "dota_analyst"),
		ClickHouseUser:     getenv("CLICKHOUSE_USER", "dota"),
		ClickHousePassword: getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"),

		DemoinfoPath: getenv("DEMOINFO_PATH", "./build/demoinfo"),
		WorkDir:      getenv("WORK_DIR", os.TempDir()),

		PurgeParsedReplays: getenv("PURGE_PARSED_REPLAYS", "") == "true",
		MetricsAddr:        getenv("METRICS_ADDR", ":9101"),
	}
}
