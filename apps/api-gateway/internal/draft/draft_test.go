package draft

import "testing"

var meta = []Hero{
	{HeroID: 1, Name: "npc_dota_hero_axe", Matches: 40, ShrunkWinrate: 0.58},
	{HeroID: 2, Name: "npc_dota_hero_kez", Matches: 30, ShrunkWinrate: 0.55},
	{HeroID: 3, Name: "npc_dota_hero_lina", Matches: 20, ShrunkWinrate: 0.52},
	{HeroID: 4, Name: "npc_dota_hero_pudge", Matches: 50, ShrunkWinrate: 0.44},
	{HeroID: 5, Name: "npc_dota_hero_meepo", Matches: 1, ShrunkWinrate: 0.90}, // < MinMatches
}

func byID() map[int]Hero {
	m := map[int]Hero{}
	for _, h := range meta {
		m[h.HeroID] = h
	}
	return m
}

func TestPredictedWinrateNeutralOnEmptyDraft(t *testing.T) {
	if wp := PredictedWinrate(byID(), nil, nil); wp != 0.5 {
		t.Fatalf("empty draft: want 0.5, got %v", wp)
	}
}

func TestPredictedWinrateFavorsStrongerSide(t *testing.T) {
	wp := PredictedWinrate(byID(), []int{1}, []int{4}) // 0.58 vs 0.44
	want := 0.5 + 0.58 - 0.44
	if diff := wp - want; diff > 1e-9 || diff < -1e-9 {
		t.Fatalf("want %v, got %v", want, wp)
	}
	// Неизвестный герой нейтрален.
	if wp := PredictedWinrate(byID(), []int{999}, nil); wp != 0.5 {
		t.Fatalf("unknown hero must be neutral, got %v", wp)
	}
}

func TestRecommendExcludesTakenAndThinSamples(t *testing.T) {
	recs := Recommend(meta, State{
		RadiantPicks: []int{1},
		Bans:         []int{2},
		NextAction:   "dire_pick",
	})
	for _, r := range recs {
		if r.HeroID == 1 || r.HeroID == 2 {
			t.Fatalf("taken hero %d recommended", r.HeroID)
		}
		if r.HeroID == 5 {
			t.Fatal("hero below MinMatches recommended")
		}
	}
	if len(recs) == 0 || recs[0].HeroID != 3 {
		t.Fatalf("want strongest available (3) first, got %+v", recs)
	}
}

func TestRecommendOrderedAndCapped(t *testing.T) {
	recs := Recommend(meta, State{NextAction: "radiant_pick"})
	if len(recs) > MaxRecommendations {
		t.Fatalf("too many recommendations: %d", len(recs))
	}
	for i := 1; i < len(recs); i++ {
		if recs[i].ExpectedWinrate > recs[i-1].ExpectedWinrate {
			t.Fatal("recommendations not sorted by winrate desc")
		}
	}
}
