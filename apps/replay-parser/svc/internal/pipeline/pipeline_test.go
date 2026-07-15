package pipeline

import (
	"encoding/json"
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

func TestEventTypeMapMatchesEnum(t *testing.T) {
	// Значения обязаны существовать в Enum8 ClickHouse ReplayEvents
	// (миграция 002) — рассинхрон уронит INSERT целиком.
	valid := map[string]bool{
		"DAMAGE": true, "HEAL": true, "KILL": true,
		"ABILITY_CAST": true, "ITEM_PURCHASE": true, "WARD_PLACE": true,
	}
	for from, to := range eventTypeMap {
		if !valid[to] {
			t.Errorf("eventTypeMap[%q] = %q: нет такого значения в Enum8", from, to)
		}
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
