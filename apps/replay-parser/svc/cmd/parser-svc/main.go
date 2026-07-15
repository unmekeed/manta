// parser-svc — Go-обвязка Replay Parser (Гл. 5): потребляет
// match.downloaded, гоняет C++ ядро, грузит результат в ClickHouse и
// публикует replay.parsed.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/dota-ai-analyst/replay-parser-svc/internal/config"
	"github.com/dota-ai-analyst/replay-parser-svc/internal/consumer"
	"github.com/dota-ai-analyst/replay-parser-svc/internal/pipeline"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg := config.FromEnv()

	if _, err := os.Stat(cfg.DemoinfoPath); err != nil {
		log.Error("demoinfo binary not found", "path", cfg.DemoinfoPath, "err", err)
		os.Exit(1)
	}

	ch := pipeline.NewCHClient(cfg.ClickHouseURL, cfg.ClickHouseDB,
		cfg.ClickHouseUser, cfg.ClickHousePassword)
	pipe, err := pipeline.New(cfg.S3Endpoint, cfg.S3AccessKey, cfg.S3SecretKey,
		cfg.S3UseSSL, ch, cfg.DemoinfoPath, cfg.WorkDir,
		cfg.PurgeParsedReplays, log)
	if err != nil {
		log.Error("pipeline init failed", "err", err)
		os.Exit(1)
	}

	cons, err := consumer.New(cfg.Brokers, cfg.GroupID, cfg.TopicIn,
		cfg.TopicOut, cfg.TopicDLQ, pipe, log)
	if err != nil {
		log.Error("consumer init failed", "err", err)
		os.Exit(1)
	}
	defer cons.Close()

	ctx, stop := signal.NotifyContext(context.Background(),
		syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	consumer.ServeMetrics(cfg.MetricsAddr)
	log.Info("parser-svc started", "brokers", cfg.Brokers,
		"topic_in", cfg.TopicIn, "demoinfo", cfg.DemoinfoPath,
		"metrics", cfg.MetricsAddr)
	if err := cons.Run(ctx); err != nil && ctx.Err() == nil {
		log.Error("consumer loop failed", "err", err)
		os.Exit(1)
	}
	log.Info("parser-svc stopped")
}
