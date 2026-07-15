package pipeline

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// CHClient — минимальный клиент ClickHouse поверх HTTP-интерфейса
// (INSERT ... FORMAT JSONEachRow). Драйвер не нужен: сервис только
// пишет батчи строк, подготовленных из JSONL парсера.
type CHClient struct {
	base     string
	db       string
	user     string
	password string
	http     *http.Client
}

func NewCHClient(baseURL, db, user, password string) *CHClient {
	return &CHClient{
		base:     baseURL,
		db:       db,
		user:     user,
		password: password,
		http:     &http.Client{Timeout: 120 * time.Second},
	}
}

// InsertJSONEachRow отправляет поток строк JSONEachRow в таблицу.
func (c *CHClient) InsertJSONEachRow(ctx context.Context, table string, body io.Reader) error {
	q := url.Values{}
	q.Set("database", c.db)
	q.Set("query", fmt.Sprintf("INSERT INTO %s FORMAT JSONEachRow", table))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		c.base+"/?"+q.Encode(), body)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("X-ClickHouse-User", c.user)
	req.Header.Set("X-ClickHouse-Key", c.password)

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("clickhouse insert %s: %w", table, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("clickhouse insert %s: status %d: %s",
			table, resp.StatusCode, string(msg))
	}
	return nil
}
