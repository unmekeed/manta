// Package consumer — Kafka-петля parser-svc: match.downloaded → pipeline →
// replay.parsed | dlq.parser (Гл. 2.3, реестр топиков).
//
// Семантика at-least-once: оффсет коммитится только после успешной
// обработки записи и публикации результата. Ошибка обработки отправляет
// исходное событие в DLQ с описанием причины и тоже коммитится — битая
// запись не должна блокировать партицию (Гл. 2.4.2).
package consumer

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/unmekeed/manta/replay-parser-svc/internal/pipeline"
)

const producerName = "replay-parser-svc@0.1.0"

// Envelope — конверт события шины (Гл. 2.3.3); формат совпадает с
// api-gateway (дублирование осознанное: сервисы независимы, общий контракт
// зафиксирован схемой, а не общим Go-пакетом).
type Envelope struct {
	EventID       string          `json:"event_id"`
	EventType     string          `json:"event_type"`
	SchemaVersion string          `json:"schema_version"`
	TraceID       string          `json:"trace_id"`
	OccurredAt    time.Time       `json:"occurred_at"`
	Producer      string          `json:"producer"`
	PartitionKey  string          `json:"partition_key"`
	Payload       json.RawMessage `json:"payload"`
}

type matchDownloaded struct {
	JobID     string `json:"job_id"`
	ReplayURL string `json:"replay_url"`
	Source    string `json:"source"`
	Tier      string `json:"tier"` // Premium | Professional | ... (Гл. 4.2)
}

type Consumer struct {
	client   *kgo.Client
	pipe     *pipeline.Pipeline
	topicOut string
	topicDLQ string
	log      *slog.Logger
}

func New(brokers []string, groupID, topicIn, topicOut, topicDLQ string,
	pipe *pipeline.Pipeline, log *slog.Logger) (*Consumer, error) {
	client, err := kgo.NewClient(
		kgo.SeedBrokers(brokers...),
		kgo.ConsumerGroup(groupID),
		kgo.ConsumeTopics(topicIn),
		kgo.DisableAutoCommit(),
		kgo.AllowAutoTopicCreation(),
	)
	if err != nil {
		return nil, fmt.Errorf("kafka client: %w", err)
	}
	return &Consumer{client: client, pipe: pipe,
		topicOut: topicOut, topicDLQ: topicDLQ, log: log}, nil
}

func (c *Consumer) Close() { c.client.Close() }

// Run крутит петлю потребления до отмены контекста.
func (c *Consumer) Run(ctx context.Context) error {
	for {
		fetches := c.client.PollFetches(ctx)
		if fetches.IsClientClosed() || ctx.Err() != nil {
			return ctx.Err()
		}
		fetches.EachError(func(topic string, part int32, err error) {
			c.log.Error("fetch error", "topic", topic, "partition", part, "err", err)
		})
		fetches.EachRecord(func(rec *kgo.Record) {
			c.handle(ctx, rec)
			if err := c.client.CommitRecords(ctx, rec); err != nil {
				c.log.Error("commit failed", "err", err)
			}
		})
	}
}

func (c *Consumer) handle(ctx context.Context, rec *kgo.Record) {
	var env Envelope
	if err := json.Unmarshal(rec.Value, &env); err != nil {
		c.toDLQ(ctx, rec, "", fmt.Sprintf("bad envelope: %v", err))
		return
	}
	var msg matchDownloaded
	if err := json.Unmarshal(env.Payload, &msg); err != nil || msg.ReplayURL == "" {
		c.toDLQ(ctx, rec, env.TraceID, fmt.Sprintf("bad payload: %v", err))
		return
	}

	c.log.Info("parsing replay", "job_id", msg.JobID, "replay_url", msg.ReplayURL,
		"trace_id", env.TraceID)
	res, err := c.pipe.Run(ctx, msg.ReplayURL)
	if err != nil {
		c.log.Error("parse failed", "job_id", msg.JobID, "err", err)
		c.toDLQ(ctx, rec, env.TraceID, err.Error())
		return
	}
	parsed.Inc()
	parseDuration.Observe(float64(res.DurationMS) / 1000.0)

	out, err := c.envelope("replay.parsed", env.TraceID,
		"match_id:"+fmt.Sprint(res.MatchID), map[string]any{
			"job_id":        msg.JobID,
			"match_id":      res.MatchID,
			"tier":          msg.Tier,
			"winner":        res.Winner,
			"duration_s":    res.DurationS,
			"players":       res.Players,
			"event_rows":    res.EventRows,
			"position_rows": res.PositionRows,
			"economy_rows":  res.EconomyRows,
			"duration_ms":   res.DurationMS,
		})
	if err != nil {
		c.log.Error("marshal replay.parsed", "err", err)
		return
	}
	c.produce(ctx, c.topicOut, out)
	c.log.Info("replay parsed", "job_id", msg.JobID, "match_id", res.MatchID,
		"events", res.EventRows, "positions", res.PositionRows,
		"economy", res.EconomyRows, "duration_ms", res.DurationMS)
}

func (c *Consumer) toDLQ(ctx context.Context, rec *kgo.Record, traceID, reason string) {
	env, err := c.envelope("dlq.parser", traceID, string(rec.Key), map[string]any{
		"reason":    reason,
		"original":  json.RawMessage(rec.Value),
		"partition": rec.Partition,
		"offset":    rec.Offset,
	})
	if err != nil {
		c.log.Error("marshal dlq event", "err", err)
		return
	}
	dlq.Inc()
	c.produce(ctx, c.topicDLQ, env)
}

func (c *Consumer) envelope(eventType, traceID, key string, payload any) (*kgo.Record, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	if traceID == "" {
		traceID = uuid.NewString()
	}
	body, err := json.Marshal(Envelope{
		EventID:       uuid.NewString(),
		EventType:     eventType,
		SchemaVersion: "1.0.0",
		TraceID:       traceID,
		OccurredAt:    time.Now().UTC(),
		Producer:      producerName,
		PartitionKey:  key,
		Payload:       raw,
	})
	if err != nil {
		return nil, err
	}
	topic := eventType
	return &kgo.Record{Topic: topic, Key: []byte(key), Value: body}, nil
}

func (c *Consumer) produce(ctx context.Context, topic string, rec *kgo.Record) {
	rec.Topic = topic
	if err := c.client.ProduceSync(ctx, rec).FirstErr(); err != nil {
		c.log.Error("produce failed", "topic", topic, "err", err)
	}
}
