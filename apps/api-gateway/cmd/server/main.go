package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/dota-ai-analyst/api-gateway/internal/config"
	"github.com/dota-ai-analyst/api-gateway/internal/events"
	"github.com/dota-ai-analyst/api-gateway/internal/handlers"
	"github.com/dota-ai-analyst/api-gateway/internal/router"
	"github.com/dota-ai-analyst/api-gateway/internal/storage"
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

	h := &handlers.Handlers{DB: pool, Replays: replays}
	srv := &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: router.New(h, logger, cfg.RateLimitRPS, cfg.RateLimitBurst),
	}

	go func() {
		logger.Info("listening", "addr", cfg.ListenAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
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
