package pipeline

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"testing"
)

func TestParseS3URL(t *testing.T) {
	cases := []struct {
		in          string
		bucket, key string
		wantErr     bool
	}{
		{"s3://replays/fixtures/1.dem", "replays", "fixtures/1.dem", false},
		{"s3://replays/a/b/c.dem", "replays", "a/b/c.dem", false},
		{"s3://replays", "", "", true},
		{"s3://", "", "", true},
		{"http://replays/1.dem", "", "", true},
		{"", "", "", true},
	}
	for _, c := range cases {
		bucket, key, err := parseS3URL(c.in)
		if (err != nil) != c.wantErr {
			t.Errorf("parseS3URL(%q): err=%v, wantErr=%v", c.in, err, c.wantErr)
			continue
		}
		if bucket != c.bucket || key != c.key {
			t.Errorf("parseS3URL(%q) = (%q, %q), want (%q, %q)",
				c.in, bucket, key, c.bucket, c.key)
		}
	}
}

// enumValues читает допустимые значения event_type ИЗ МИГРАЦИЙ, а не из
// копии в тесте. Раньше список был захардкожен, и тест «сверял схему»,
// сверяясь с самим собой: добавление значения в ClickHouse его бы не
// коснулось, а удаление — не поймалось бы. Рассинхрон роняет INSERT
// целиком, то есть весь реплейный путь.
func enumValues(t *testing.T) map[string]bool {
	t.Helper()
	dir := filepath.Join("..", "..", "..", "..", "..",
		"infra", "migrations", "clickhouse")
	files, err := filepath.Glob(filepath.Join(dir, "*.sql"))
	if err != nil || len(files) == 0 {
		t.Fatalf("миграции ClickHouse не найдены в %s: %v", dir, err)
	}
	sort.Strings(files)
	re := regexp.MustCompile(`(?s)event_type\s+Enum8\((.*?)\)`)
	val := regexp.MustCompile(`'([A-Z_]+)'`)
	out := map[string]bool{}
	// Последняя миграция, задающая event_type, и есть действующая схема.
	for _, f := range files {
		body, err := os.ReadFile(f)
		if err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		if m := re.FindSubmatch(body); m != nil {
			out = map[string]bool{}
			for _, v := range val.FindAllSubmatch(m[1], -1) {
				out[string(v[1])] = true
			}
		}
	}
	if len(out) == 0 {
		t.Fatal("в миграциях не найдено определение event_type Enum8")
	}
	return out
}

func TestEventTypeMapMatchesEnum(t *testing.T) {
	valid := enumValues(t)
	for from, to := range eventTypeMap {
		if !valid[to] {
			t.Errorf("eventTypeMap[%q] = %q: нет такого значения в Enum8 "+
				"(допустимы %v)", from, to, valid)
		}
	}
}

func TestSmokeTypeIsInEnum(t *testing.T) {
	// SMOKE не попадает в eventTypeMap: у смоука нет своего типа в
	// DOTA_COMBATLOG_TYPES, он распознаётся по имени модификатора уже
	// внутри loadEvents. Проверяем отдельно — иначе значение уехало бы
	// из схемы незамеченным.
	if !enumValues(t)["SMOKE"] {
		t.Error("SMOKE отсутствует в Enum8 event_type — смоуки писать некуда")
	}
}

func TestSummaryDecode(t *testing.T) {
	raw := `{"match_id":8892914077,"winner":"Dire","game_mode":2,` +
		`"playback_time_s":4497.3,"build":10836,"players":[` +
		`{"team":2,"name":"Yatoro","hero":"npc_dota_hero_naga_siren"}]}`
	var s Summary
	if err := json.Unmarshal([]byte(raw), &s); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if s.MatchID != 8892914077 || s.Winner != "Dire" ||
		len(s.Players) != 1 || s.Players[0].Hero != "npc_dota_hero_naga_siren" {
		t.Fatalf("сводка распарсена неверно: %+v", s)
	}
}
