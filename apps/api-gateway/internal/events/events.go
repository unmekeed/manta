// Package events реализует конверт события (Гл. 2.3.3) и Outbox-паттерн
// (Гл. 2.5): событие пишется в PostgreSQL в одной транзакции с бизнес-
// сущностью, фоновый relay доставляет его в Kafka (at-least-once).
package events

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/twmb/franz-go/pkg/kgo"
)

const producerName = "api-gateway@0.1.0"

// Envelope — единый конверт события шины данных.
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

// NewEnvelope собирает конверт с заполненными служебными полями.
func NewEnvelope(eventType, traceID, partitionKey string, payload any) (Envelope, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return Envelope{}, fmt.Errorf("marshal payload: %w", err)
	}
	return Envelope{
		EventID:       uuid.NewString(),
		EventType:     eventType,
		SchemaVersion: "1.0.0",
		TraceID:       traceID,
		OccurredAt:    time.Now().UTC(),
		Producer:      producerName,
		PartitionKey:  partitionKey,
		Payload:       raw,
	}, nil
}

// WriteOutbox добавляет событие в EventOutbox внутри переданной транзакции.
func WriteOutbox(ctx context.Context, tx pgx.Tx, topic string, env Envelope) error {
	body, err := json.Marshal(env)
	if err != nil {
		return fmt.Errorf("marshal envelope: %w", err)
	}
	_, err = tx.Exec(ctx,
		`INSERT INTO EventOutbox (topic, partition_key, envelope) VALUES ($1, $2, $3)`,
		topic, env.PartitionKey, body)
	return err
}

// Relay периодически публикует неотправленные события из outbox в Kafka.
type Relay struct {
	db     *pgxpool.Pool
	client *kgo.Client
	logger *slog.Logger
	period time.Duration
	batch  int
}

func NewRelay(db *pgxpool.Pool, brokers []string, logger *slog.Logger) (*Relay, error) {
	client, err := kgo.NewClient(
		kgo.SeedBrokers(brokers...),
		kgo.AllowAutoTopicCreation(),
	)
	if err != nil {
		return nil, fmt.Errorf("kafka client: %w", err)
	}
	return &Relay{db: db, client: client, logger: logger, period: time.Second, batch: 100}, nil
}

func (r *Relay) Close() { r.client.Close() }

// Run крутит цикл доставки до отмены контекста.
func (r *Relay) Run(ctx context.Context) {
	ticker := time.NewTicker(r.period)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if n, err := r.flushOnce(ctx); err != nil {
				r.logger.Error("outbox_flush_failed", "error", err)
			} else if n > 0 {
				r.logger.Info("outbox_flushed", "events", n)
			}
		}
	}
}

// flushOnce публикует одну пачку событий; строки блокируются через
// FOR UPDATE SKIP LOCKED, что позволяет запускать несколько реплик relay.
func (r *Relay) flushOnce(ctx context.Context) (int, error) {
	tx, err := r.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	rows, err := tx.Query(ctx, `
		SELECT outbox_id, topic, partition_key, envelope
		FROM EventOutbox
		WHERE published_at IS NULL
		ORDER BY outbox_id
		LIMIT $1
		FOR UPDATE SKIP LOCKED`, r.batch)
	if err != nil {
		return 0, err
	}

	type rec struct {
		id       int64
		topic    string
		key      string
		envelope []byte
	}
	var recs []rec
	for rows.Next() {
		var re rec
		if err := rows.Scan(&re.id, &re.topic, &re.key, &re.envelope); err != nil {
			rows.Close()
			return 0, err
		}
		recs = append(recs, re)
	}
	rows.Close()
	if len(recs) == 0 {
		return 0, tx.Commit(ctx)
	}

	kafkaRecords := make([]*kgo.Record, 0, len(recs))
	for _, re := range recs {
		kafkaRecords = append(kafkaRecords, &kgo.Record{
			Topic: re.topic,
			Key:   []byte(re.key),
			Value: re.envelope,
		})
	}
	if err := r.client.ProduceSync(ctx, kafkaRecords...).FirstErr(); err != nil {
		return 0, fmt.Errorf("kafka produce: %w", err)
	}

	ids := make([]int64, len(recs))
	for i, re := range recs {
		ids[i] = re.id
	}
	if _, err := tx.Exec(ctx,
		`UPDATE EventOutbox SET published_at = NOW() WHERE outbox_id = ANY($1)`, ids); err != nil {
		return 0, err
	}
	return len(recs), tx.Commit(ctx)
}
