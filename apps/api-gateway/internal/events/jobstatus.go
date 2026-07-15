package events

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/dota-ai-analyst/api-gateway/internal/middleware"
)

// JobStatusConsumer переводит AnalysisJobs по событиям конвейера:
// replay.parsed → 'done' (+ match_id, completed_at), dlq.parser → 'failed'.
// Gateway — владелец таблицы AnalysisJobs, поэтому переход статуса живёт
// здесь, а не в parser-svc (Гл. 2.2: сервис владеет своими данными).
// Обновление идемпотентно (WHERE status <> 'done'), поэтому at-least-once
// доставка Kafka безопасна.
type JobStatusConsumer struct {
	db     *pgxpool.Pool
	client *kgo.Client
	logger *slog.Logger
}

type parsedPayload struct {
	JobID   string `json:"job_id"`
	MatchID uint64 `json:"match_id"`
}

type dlqPayload struct {
	Reason   string          `json:"reason"`
	Original json.RawMessage `json:"original"`
}

func NewJobStatusConsumer(db *pgxpool.Pool, brokers []string, logger *slog.Logger) (*JobStatusConsumer, error) {
	client, err := kgo.NewClient(
		kgo.SeedBrokers(brokers...),
		kgo.ConsumerGroup("api-gateway-jobstatus"),
		kgo.ConsumeTopics("replay.parsed", "dlq.parser"),
		kgo.DisableAutoCommit(),
		kgo.AllowAutoTopicCreation(),
	)
	if err != nil {
		return nil, fmt.Errorf("kafka client: %w", err)
	}
	return &JobStatusConsumer{db: db, client: client, logger: logger}, nil
}

func (c *JobStatusConsumer) Close() { c.client.Close() }

func (c *JobStatusConsumer) Run(ctx context.Context) {
	for {
		fetches := c.client.PollFetches(ctx)
		if fetches.IsClientClosed() || ctx.Err() != nil {
			return
		}
		fetches.EachError(func(topic string, part int32, err error) {
			c.logger.Error("jobstatus_fetch_error", "topic", topic, "error", err)
		})
		fetches.EachRecord(func(rec *kgo.Record) {
			c.handle(ctx, rec)
			if err := c.client.CommitRecords(ctx, rec); err != nil {
				c.logger.Error("jobstatus_commit_failed", "error", err)
			}
		})
	}
}

func (c *JobStatusConsumer) handle(ctx context.Context, rec *kgo.Record) {
	var env Envelope
	if err := json.Unmarshal(rec.Value, &env); err != nil {
		c.logger.Error("jobstatus_bad_envelope", "topic", rec.Topic, "error", err)
		return
	}

	switch rec.Topic {
	case "replay.parsed":
		var p parsedPayload
		if err := json.Unmarshal(env.Payload, &p); err != nil || p.JobID == "" {
			return // событие не про job (например, ручной прогон) — пропуск
		}
		tag, err := c.db.Exec(ctx,
			`UPDATE AnalysisJobs
			    SET status = 'done', match_id = $2, completed_at = NOW()
			  WHERE job_id::text = $1 AND status <> 'done'`,
			p.JobID, int64(p.MatchID))
		if err != nil {
			c.logger.Error("jobstatus_update_failed", "job_id", p.JobID, "error", err)
			return
		}
		if tag.RowsAffected() > 0 {
			middleware.JobCompleted("done")
			c.logger.Info("job_done", "job_id", p.JobID, "match_id", p.MatchID)
		}

	case "dlq.parser":
		var d dlqPayload
		if err := json.Unmarshal(env.Payload, &d); err != nil {
			return
		}
		// job_id достаём из исходного события внутри DLQ-конверта.
		var orig Envelope
		var op parsedPayload
		if json.Unmarshal(d.Original, &orig) != nil ||
			json.Unmarshal(orig.Payload, &op) != nil || op.JobID == "" {
			return
		}
		tag, err := c.db.Exec(ctx,
			`UPDATE AnalysisJobs
			    SET status = 'failed', completed_at = NOW()
			  WHERE job_id::text = $1 AND status NOT IN ('done', 'failed')`,
			op.JobID)
		if err != nil {
			c.logger.Error("jobstatus_update_failed", "job_id", op.JobID, "error", err)
			return
		}
		if tag.RowsAffected() > 0 {
			middleware.JobCompleted("failed")
			c.logger.Warn("job_failed", "job_id", op.JobID, "reason", d.Reason)
		}
	}
}
