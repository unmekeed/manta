package main

import (
	"context"
	"crypto/tls"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/unmekeed/manta/api-gateway/internal/auth"
	"github.com/unmekeed/manta/api-gateway/internal/config"
	"github.com/unmekeed/manta/api-gateway/internal/events"
	"github.com/unmekeed/manta/api-gateway/internal/handlers"
	"github.com/unmekeed/manta/api-gateway/internal/router"
	"github.com/unmekeed/manta/api-gateway/internal/storage"
	corev1 "github.com/unmekeed/manta/proto/core/v1"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil)).With("service", "api-gateway")
	cfg := config.Load()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := pgxpool.New(ctx, cfg.PostgresDSN)
	if err != nil {
		logger.Error("postgres_connect_failed", "error", err)
		os.Exit(1)
	}
	defer pool.Close()

	replays, err := storage.NewReplayStore(
		cfg.S3Endpoint, cfg.S3AccessKey, cfg.S3SecretKey, cfg.S3Bucket, cfg.S3UseSSL)
	if err != nil {
		logger.Error("s3_init_failed", "error", err)
		os.Exit(1)
	}
	if err := replays.EnsureBucket(ctx); err != nil {
		logger.Error("s3_bucket_failed", "error", err)
		os.Exit(1)
	}

	relay, err := events.NewRelay(pool, cfg.KafkaBrokers, logger)
	if err != nil {
		logger.Error("kafka_init_failed", "error", err)
		os.Exit(1)
	}
	defer relay.Close()
	go relay.Run(ctx)

	jobStatus, err := events.NewJobStatusConsumer(pool, cfg.KafkaBrokers, logger)
	if err != nil {
		logger.Error("jobstatus_init_failed", "error", err)
		os.Exit(1)
	}
	defer jobStatus.Close()
	go jobStatus.Run(ctx)

	// Аутентификация (Гл. 9.2): ключи из окружения. Без ключей шлюз
	// работает открыто — это режим локального стенда, и он громко
	// логируется, чтобы не уехать в прод незамеченным.
	var deny auth.Denylist
	if d := auth.NewRedisDenylist(cfg.RedisAddr, cfg.RedisPassword); d != nil {
		defer d.Close()
		deny = d
	}
	authn, err := auth.New(deny)
	if err != nil {
		logger.Error("auth_init_failed", "error", err)
		os.Exit(1)
	}
	switch {
	case !authn.Enabled():
		logger.Warn("auth_disabled",
			"detail", "JWT_PUBLIC_KEY_FILE/JWT_PRIVATE_KEY_FILE не заданы — API открыт без токенов")
	case !authn.CanIssue():
		logger.Info("auth_enabled", "mode", "verify-only")
	default:
		logger.Info("auth_enabled", "mode", "issue+verify")
	}

	h := &handlers.Handlers{DB: pool, Replays: replays, Auth: authn}

	// Драфт-симулятор (C6): gRPC-клиент Draft Engine + словарь героев.
	// Ленивое соединение — недоступный движок даёт 503 на эндпоинтах
	// драфта, не мешая остальному API.
	if cfg.DraftGRPCAddr != "" {
		conn, err := grpc.NewClient(cfg.DraftGRPCAddr,
			grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			logger.Error("draft_grpc_init_failed", "error", err)
		} else {
			defer conn.Close()
			h.Draft = corev1.NewDraftServiceClient(conn)
		}
	}
	if heroes, err := handlers.LoadHeroes(cfg.HeroesPath); err != nil {
		logger.Warn("heroes_dict_missing", "error", err)
	} else {
		h.Heroes = heroes
	}
	srv := &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: router.New(h, authn, logger, cfg.RateLimitRPS, cfg.RateLimitBurst),
	}

	// TLS (NFR-SEC-01): минимум 1.3 — понижение версии не допускается,
	// поэтому MinVersion, а не список шифров. Сертификаты задаются
	// TLS_CERT_FILE/TLS_KEY_FILE; без них слушаем HTTP (dev-стенд).
	tlsOn := cfg.TLSCertFile != "" && cfg.TLSKeyFile != ""
	if tlsOn {
		srv.TLSConfig = &tls.Config{MinVersion: tls.VersionTLS13}
	}

	go func() {
		logger.Info("listening", "addr", cfg.ListenAddr, "tls", tlsOn)
		var err error
		if tlsOn {
			err = srv.ListenAndServeTLS(cfg.TLSCertFile, cfg.TLSKeyFile)
		} else {
			logger.Warn("tls_disabled",
				"detail", "TLS_CERT_FILE/TLS_KEY_FILE не заданы — трафик в открытом виде (NFR-SEC-01)")
			err = srv.ListenAndServe()
		}
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("server_failed", "error", err)
			stop()
		}
	}()

	<-ctx.Done()
	logger.Info("shutting_down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Error("shutdown_error", "error", err)
	}
}
