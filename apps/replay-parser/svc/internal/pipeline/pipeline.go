// Package pipeline — конвейер разбора одного реплея (UC-01, Гл. 5):
// S3 → временный .dem → C++ ядро (demoinfo) → JSONL → ClickHouse.
package pipeline

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// Player — строка ростера из сводки ядра (порядок: Radiant 0-4, Dire 5-9).
type Player struct {
	Team int    `json:"team"` // 2 = Radiant, 3 = Dire
	Name string `json:"name"`
	Hero string `json:"hero"` // npc_dota_hero_*
}

// Summary — машиночитаемая сводка demoinfo --summary.
type Summary struct {
	MatchID       uint64   `json:"match_id"`
	Winner        string   `json:"winner"` // "Radiant" | "Dire"
	GameMode      int      `json:"game_mode"`
	PlaybackTimeS float64  `json:"playback_time_s"`
	Build         int      `json:"build"`
	Players       []Player `json:"players"`
}

// Result — итог обработки реплея; уходит в payload события replay.parsed.
type Result struct {
	MatchID      uint64   `json:"match_id"`
	Winner       string   `json:"winner"`
	DurationS    float64  `json:"duration_s"`
	Players      []Player `json:"players"`
	EventRows    int      `json:"event_rows"`
	PositionRows int      `json:"position_rows"`
	EconomyRows  int      `json:"economy_rows"`
	DurationMS   int64    `json:"duration_ms"`
}

type Pipeline struct {
	s3       *minio.Client
	ch       *CHClient
	demoinfo string
	workDir  string
	purge    bool
	log      *slog.Logger
}

func New(s3Endpoint, s3Access, s3Secret string, s3SSL bool,
	ch *CHClient, demoinfoPath, workDir string, purge bool,
	log *slog.Logger) (*Pipeline, error) {
	s3, err := minio.New(s3Endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(s3Access, s3Secret, ""),
		Secure: s3SSL,
	})
	if err != nil {
		return nil, fmt.Errorf("minio client: %w", err)
	}
	return &Pipeline{s3: s3, ch: ch, demoinfo: demoinfoPath,
		workDir: workDir, purge: purge, log: log}, nil
}

// Run скачивает реплей по s3://bucket/key, прогоняет через C++ ядро и
// загружает события и позиции в ClickHouse.
func (p *Pipeline) Run(ctx context.Context, replayURL string) (Result, error) {
	start := time.Now()

	bucket, key, err := parseS3URL(replayURL)
	if err != nil {
		return Result{}, err
	}

	tmp, err := os.MkdirTemp(p.workDir, "replay-*")
	if err != nil {
		return Result{}, fmt.Errorf("mkdtemp: %w", err)
	}
	defer os.RemoveAll(tmp)

	demPath := filepath.Join(tmp, "replay.dem")
	if err := p.download(ctx, bucket, key, demPath); err != nil {
		return Result{}, err
	}

	eventsPath := filepath.Join(tmp, "events.jsonl")
	posPath := filepath.Join(tmp, "positions.jsonl")
	ecoPath := filepath.Join(tmp, "economy.jsonl")
	summaryPath := filepath.Join(tmp, "summary.json")
	sum, err := p.runCore(ctx, demPath, eventsPath, posPath, ecoPath, summaryPath)
	if err != nil {
		return Result{}, err
	}

	evRows, err := p.loadEvents(ctx, sum.MatchID, eventsPath)
	if err != nil {
		return Result{}, err
	}
	posRows, err := p.loadPositions(ctx, sum.MatchID, posPath)
	if err != nil {
		return Result{}, err
	}
	ecoRows, err := p.loadEconomy(ctx, sum.MatchID, ecoPath)
	if err != nil {
		return Result{}, err
	}

	// Успешный разбор: события/фичи в ClickHouse, сам .dem больше не
	// нужен (опционально, PURGE_PARSED_REPLAYS). Ошибка удаления не
	// фатальна — реплей лишь займёт место до следующей чистки.
	if p.purge {
		if err := p.s3.RemoveObject(ctx, bucket, key,
			minio.RemoveObjectOptions{}); err != nil {
			p.log.Warn("replay purge failed", "bucket", bucket, "key", key,
				"err", err)
		} else {
			p.log.Info("replay purged", "bucket", bucket, "key", key)
		}
	}

	return Result{
		MatchID:      sum.MatchID,
		Winner:       sum.Winner,
		DurationS:    sum.PlaybackTimeS,
		Players:      sum.Players,
		EventRows:    evRows,
		PositionRows: posRows,
		EconomyRows:  ecoRows,
		DurationMS:   time.Since(start).Milliseconds(),
	}, nil
}

func parseS3URL(u string) (bucket, key string, err error) {
	rest, ok := strings.CutPrefix(u, "s3://")
	if !ok {
		return "", "", fmt.Errorf("unsupported replay_url %q (want s3://)", u)
	}
	bucket, key, ok = strings.Cut(rest, "/")
	if !ok || bucket == "" || key == "" {
		return "", "", fmt.Errorf("malformed replay_url %q", u)
	}
	return bucket, key, nil
}

func (p *Pipeline) download(ctx context.Context, bucket, key, dst string) error {
	obj, err := p.s3.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return fmt.Errorf("s3 get %s/%s: %w", bucket, key, err)
	}
	defer obj.Close()
	f, err := os.Create(dst)
	if err != nil {
		return fmt.Errorf("create %s: %w", dst, err)
	}
	defer f.Close()
	n, err := io.Copy(f, obj)
	if err != nil {
		return fmt.Errorf("download %s/%s: %w", bucket, key, err)
	}
	p.log.Info("replay downloaded", "bucket", bucket, "key", key, "bytes", n)
	return nil
}

// runCore запускает C++ demoinfo и читает машиночитаемую сводку --summary.
func (p *Pipeline) runCore(ctx context.Context, dem, events, positions,
	economy, summary string) (Summary, error) {
	cmd := exec.CommandContext(ctx, p.demoinfo,
		"--events", events, "--entities", positions, "--economy", economy,
		"--summary", summary, dem)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		return Summary{}, fmt.Errorf("demoinfo: %w (output tail: %s)", err, tail(out.String(), 400))
	}
	if strings.Contains(out.String(), "DESYNC") {
		return Summary{}, fmt.Errorf("demoinfo: entity decoder desync")
	}
	raw, err := os.ReadFile(summary)
	if err != nil {
		return Summary{}, fmt.Errorf("demoinfo summary: %w", err)
	}
	var sum Summary
	if err := json.Unmarshal(raw, &sum); err != nil {
		return Summary{}, fmt.Errorf("demoinfo summary: %w", err)
	}
	if sum.MatchID == 0 {
		return Summary{}, fmt.Errorf("demoinfo summary: match_id is 0")
	}
	return sum, nil
}

func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

// coreEvent — строка events.jsonl, которую пишет demoinfo (combat log).
type coreEvent struct {
	Type      string  `json:"type"`
	T         float64 `json:"t"`
	Attacker  string  `json:"attacker"`
	Target    string  `json:"target"`
	Inflictor string  `json:"inflictor"`
	Value     int64   `json:"value"`
}

// Отображение типов combat log на Enum8 ReplayEvents; остальные типы
// (модификаторы, золото, опыт) на слой сырых событий не пишутся — они
// агрегируются Feature Extractor'ом из EconomyTimeline.
var eventTypeMap = map[string]string{
	"DAMAGE":   "DAMAGE",
	"HEAL":     "HEAL",
	"DEATH":    "KILL",
	"ABILITY":  "ABILITY_CAST",
	"PURCHASE": "ITEM_PURCHASE",
}

const ticksPerSecond = 30

func (p *Pipeline) loadEvents(ctx context.Context, matchID uint64, path string) (int, error) {
	type row struct {
		MatchID     uint64  `json:"match_id"`
		Tick        uint32  `json:"tick"`
		GameTime    int32   `json:"game_time"`
		EventType   string  `json:"event_type"`
		PlayerID    uint64  `json:"player_id"`
		TargetID    uint64  `json:"target_id"`
		X           float32 `json:"x"`
		Y           float32 `json:"y"`
		Z           float32 `json:"z"`
		ValueAmount int32   `json:"value_amount"`
		Inflictor   string  `json:"inflictor"`
		Attacker    string  `json:"attacker"`
		Target      string  `json:"target"`
	}
	return p.loadJSONL(ctx, "ReplayEvents", path, func(line []byte, w *json.Encoder) (bool, error) {
		var ev coreEvent
		if err := json.Unmarshal(line, &ev); err != nil {
			return false, fmt.Errorf("bad event line: %w", err)
		}
		chType, ok := eventTypeMap[ev.Type]
		if !ok {
			return false, nil // тип вне схемы сырых событий — пропуск
		}
		return true, w.Encode(row{
			MatchID:     matchID,
			Tick:        uint32(ev.T * ticksPerSecond),
			GameTime:    int32(ev.T),
			EventType:   chType,
			ValueAmount: int32(ev.Value),
			Inflictor:   ev.Inflictor,
			Attacker:    ev.Attacker,
			Target:      ev.Target,
		})
	})
}

// corePosition — строка positions.jsonl (сэмплы позиций героев).
type corePosition struct {
	Tick  uint32  `json:"tick"`
	Class string  `json:"class"`
	X     float32 `json:"x"`
	Y     float32 `json:"y"`
}

func (p *Pipeline) loadPositions(ctx context.Context, matchID uint64, path string) (int, error) {
	type row struct {
		MatchID  uint64  `json:"match_id"`
		PlayerID uint64  `json:"player_id"`
		GameTime int32   `json:"game_time"`
		X        float32 `json:"x"`
		Y        float32 `json:"y"`
		IsAlive  uint8   `json:"is_alive"`
		Hero     string  `json:"hero"`
	}
	return p.loadJSONL(ctx, "PositionSnapshots", path, func(line []byte, w *json.Encoder) (bool, error) {
		var pos corePosition
		if err := json.Unmarshal(line, &pos); err != nil {
			return false, fmt.Errorf("bad position line: %w", err)
		}
		return true, w.Encode(row{
			MatchID:  matchID,
			GameTime: int32(pos.Tick / ticksPerSecond),
			X:        pos.X,
			Y:        pos.Y,
			IsAlive:  1,
			Hero:     pos.Class,
		})
	})
}

// coreEconomy — строка economy.jsonl (сэмплы DataTeamPlayer_t каждые
// 300 тиков). team: 2 = Radiant, 3 = Dire; slot: 0..4 внутри команды.
type coreEconomy struct {
	Tick      uint32 `json:"tick"`
	Team      int    `json:"team"`
	Slot      int    `json:"slot"`
	NetWorth  int64  `json:"net_worth"`
	TotalGold int64  `json:"total_gold"`
	TotalXP   int64  `json:"total_xp"`
	LH        int64  `json:"lh"`
	DN        int64  `json:"dn"`
}

func (p *Pipeline) loadEconomy(ctx context.Context, matchID uint64, path string) (int, error) {
	type row struct {
		MatchID   uint64 `json:"match_id"`
		PlayerID  uint64 `json:"player_id"`
		GameTime  int32  `json:"game_time"`
		NetWorth  int32  `json:"net_worth"`
		TotalGold int32  `json:"total_gold"`
		TotalXP   int32  `json:"total_xp"`
		LH        uint16 `json:"lh"`
		DN        uint16 `json:"dn"`
	}
	return p.loadJSONL(ctx, "EconomyTimeline", path, func(line []byte, w *json.Encoder) (bool, error) {
		var ec coreEconomy
		if err := json.Unmarshal(line, &ec); err != nil {
			return false, fmt.Errorf("bad economy line: %w", err)
		}
		// player_id 0..9: слоты Radiant, затем Dire — сквозная нумерация,
		// совпадающая с порядком игроков в CDemoFileInfo.
		playerID := uint64(ec.Slot)
		if ec.Team == 3 {
			playerID += 5
		}
		return true, w.Encode(row{
			MatchID:   matchID,
			PlayerID:  playerID,
			GameTime:  int32(ec.Tick / ticksPerSecond),
			NetWorth:  int32(ec.NetWorth),
			TotalGold: int32(ec.TotalGold),
			TotalXP:   int32(ec.TotalXP),
			LH:        uint16(ec.LH),
			DN:        uint16(ec.DN),
		})
	})
}

// loadJSONL трансформирует JSONL-файл ядра построчно и стримит результат
// в ClickHouse одним INSERT (pipe: без буферизации всего файла в памяти).
func (p *Pipeline) loadJSONL(ctx context.Context, table, path string,
	transform func(line []byte, w *json.Encoder) (bool, error)) (int, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	pr, pw := io.Pipe()
	rows := 0
	errCh := make(chan error, 1)
	go func() {
		enc := json.NewEncoder(pw)
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		var terr error
		for sc.Scan() {
			ok, err := transform(sc.Bytes(), enc)
			if err != nil {
				terr = err
				break
			}
			if ok {
				rows++
			}
		}
		if terr == nil {
			terr = sc.Err()
		}
		pw.CloseWithError(terr)
		errCh <- terr
	}()

	if err := p.ch.InsertJSONEachRow(ctx, table, pr); err != nil {
		<-errCh
		return 0, err
	}
	if terr := <-errCh; terr != nil {
		return 0, terr
	}
	p.log.Info("clickhouse insert done", "table", table, "rows", rows)
	return rows, nil
}
