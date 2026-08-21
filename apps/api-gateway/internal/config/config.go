package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config задаёт параметры процесса API Gateway. Все значения берутся из
// переменных окружения, что соответствует запуску в Kubernetes (Гл. 12.8).
type Config struct {
	Production      bool
	ListenAddr      string
	PostgresDSN     string
	ShutdownTimeout time.Duration
	RateLimitRPS    int
	RateLimitBurst  int

	S3Endpoint  string
	S3AccessKey string
	S3SecretKey string
	S3Bucket    string
	S3UseSSL    bool

	KafkaBrokers []string

	DraftGRPCAddr string // Draft Engine (пусто — эндпоинты драфта отдают 503)
	HeroesPath    string // libs/data/heroes.json; пусто — путь по умолчанию

	// TLS (NFR-SEC-01). Пусто — слушаем HTTP (локальный стенд).
	TLSCertFile       string
	TLSKeyFile        string
	JWTPrivateKeyFile string
	JWTPublicKeyFile  string
	// Redis для denylist отозванных токенов (Гл. 9.2.2).
	RedisAddr     string
	RedisPassword string
}

func Load() Config {
	return Config{
		Production:      os.Getenv("MANTA_PROD") != "" && os.Getenv("MANTA_PROD") != "0",
		ListenAddr:      getEnv("GATEWAY_LISTEN_ADDR", ":8080"),
		PostgresDSN:     getEnv("POSTGRES_DSN", "postgres://dota:dota_dev_password@localhost:5432/manta"),
		ShutdownTimeout: getDuration("GATEWAY_SHUTDOWN_TIMEOUT", 10*time.Second),
		RateLimitRPS:    getInt("GATEWAY_RATE_LIMIT_RPS", 20),
		RateLimitBurst:  getInt("GATEWAY_RATE_LIMIT_BURST", 40),

		S3Endpoint:  getEnv("S3_ENDPOINT", "localhost:9500"),
		S3AccessKey: getEnv("S3_ACCESS_KEY", "dota"),
		S3SecretKey: getEnv("S3_SECRET_KEY", "dota_dev_password"),
		S3Bucket:    getEnv("S3_BUCKET", "replays"),
		S3UseSSL:    getEnv("S3_USE_SSL", "false") == "true",

		KafkaBrokers: []string{getEnv("KAFKA_BROKERS", "localhost:9092")},

		DraftGRPCAddr: getEnv("DRAFT_GRPC_ADDR", "localhost:50053"),
		HeroesPath:    getEnv("HEROES_PATH", ""),

		TLSCertFile:       getEnv("TLS_CERT_FILE", ""),
		TLSKeyFile:        getEnv("TLS_KEY_FILE", ""),
		JWTPrivateKeyFile: getEnv("JWT_PRIVATE_KEY_FILE", ""),
		JWTPublicKeyFile:  getEnv("JWT_PUBLIC_KEY_FILE", ""),
		RedisAddr:         getEnv("REDIS_ADDR", "localhost:6379"),
		RedisPassword:     getEnv("REDIS_PASSWORD", ""),
	}
}

// Validate запрещает production-процессу даже начинать подключение к
// инфраструктуре без verify-ключа JWT и полной TLS-пары. Dev-режим
// намеренно сохраняет прежний fail-open контракт локального стенда.
func (c Config) Validate() error {
	if !c.Production {
		return nil
	}
	if c.JWTPublicKeyFile == "" {
		return fmt.Errorf("MANTA_PROD=1: JWT_PUBLIC_KEY_FILE is required")
	}
	if c.TLSCertFile == "" {
		return fmt.Errorf("MANTA_PROD=1: TLS_CERT_FILE is required")
	}
	if c.TLSKeyFile == "" {
		return fmt.Errorf("MANTA_PROD=1: TLS_KEY_FILE is required")
	}
	return nil
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func getDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
