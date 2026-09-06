package pipeline

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
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

// AlreadyParsed — есть ли у матча события в витрине (спринт 194).
//
// ЗАЧЕМ. Разобранный реплей удаляется из S3 (purge, экономия места).
// Поэтому повторная доставка того же события — а доставка у нас
// at-least-once, это контракт, а не сбой — приводит к попытке скачать
// уже удалённый объект и падает с «The specified key does not exist».
//
// Беда не в самой ошибке, а в том, что она НЕОТЛИЧИМА от настоящей
// потери реплея: файл пропал до разбора, и матч надо спасать. Обе
// ситуации давали одинаковый ERROR и одинаковый уход в DLQ. Приучившись
// пропускать первую, пропустишь и вторую.
//
// Различает их ровно этот вопрос: если события матча уже в витрине —
// реплей разобран, объекта нет законно. Если событий нет — реплей
// потерян, и это настоящая беда.
//
// Ошибка запроса трактуется как «не знаем» (false): лишний ERROR
// безопаснее проглоченной потери.
func (c *CHClient) AlreadyParsed(ctx context.Context, matchID int64) bool {
	if matchID <= 0 {
		return false
	}
	q := url.Values{}
	q.Set("database", c.db)
	q.Set("query", fmt.Sprintf(
		"SELECT count() FROM ReplayEvents WHERE match_id = %d LIMIT 1", matchID))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		c.base+"/?"+q.Encode(), nil)
	if err != nil {
		return false
	}
	req.Header.Set("X-ClickHouse-User", c.user)
	req.Header.Set("X-ClickHouse-Key", c.password)

	resp, err := c.http.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 64))
	if err != nil {
		return false
	}
	n, err := strconv.ParseInt(strings.TrimSpace(string(body)), 10, 64)
	return err == nil && n > 0
}
