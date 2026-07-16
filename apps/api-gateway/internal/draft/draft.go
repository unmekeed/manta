// Package draft — бейзлайн Draft Engine (Гл. 3.9, спринт 32).
//
// Честный бейзлайн без синергий и контрпиков: оценка драфта — разница
// средних сглаженных винрейтов пиков (мета из MetaHeroes, спринт 31),
// рекомендации — сильнейшие доступные герои меты. GNN-модель драфта
// (Гл. 6.2.3) заменит скоринг, контракт /draft/simulate не изменится.
package draft

import (
	"fmt"
	"sort"
)

// Hero — строка меты, вход скоринга.
type Hero struct {
	HeroID        int
	Name          string
	Matches       int
	ShrunkWinrate float64
}

// State — состояние драфта (схема DraftState, Гл. 7).
type State struct {
	RadiantPicks []int  `json:"radiant_picks"`
	DirePicks    []int  `json:"dire_picks"`
	Bans         []int  `json:"bans"`
	NextAction   string `json:"next_action"` // radiant_pick|dire_pick|radiant_ban|dire_ban
}

// Recommendation — элемент ответа (схема DraftRecommendation).
type Recommendation struct {
	HeroID          int     `json:"hero_id"`
	Hero            string  `json:"hero"`
	ExpectedWinrate float64 `json:"expected_winrate"`
	Reason          string  `json:"reason"`
}

// MinMatches — герои с меньшим числом матчей в рекомендации не попадают:
// их сглаженный винрейт — почти чистый приор 0.5, сигнала нет.
const MinMatches = 3

// MaxRecommendations — размер списка рекомендаций.
const MaxRecommendations = 5

// ValidActions — допустимые значения next_action.
var ValidActions = map[string]bool{
	"radiant_pick": true, "dire_pick": true,
	"radiant_ban": true, "dire_ban": true,
}

// PredictedWinrate — вероятность победы Radiant по текущим пикам:
// 0.5 + (среднее сглаженных винрейтов Radiant − Dire), клип [0.05, 0.95].
// Герой вне меты вносит нейтральные 0.5.
func PredictedWinrate(byID map[int]Hero, radiant, dire []int) float64 {
	wp := 0.5 + sideMean(byID, radiant) - sideMean(byID, dire)
	if wp < 0.05 {
		return 0.05
	}
	if wp > 0.95 {
		return 0.95
	}
	return wp
}

func sideMean(byID map[int]Hero, picks []int) float64 {
	if len(picks) == 0 {
		return 0.5
	}
	sum := 0.0
	for _, id := range picks {
		if h, ok := byID[id]; ok {
			sum += h.ShrunkWinrate
		} else {
			sum += 0.5
		}
	}
	return sum / float64(len(picks))
}

// Recommend — топ доступных героев меты для следующего действия.
// Для пика и бана метрика одна (нет модели синергий): пик берёт
// сильнейшего себе, бан отнимает сильнейшего у противника.
func Recommend(heroes []Hero, st State) []Recommendation {
	taken := map[int]bool{}
	for _, id := range st.RadiantPicks {
		taken[id] = true
	}
	for _, id := range st.DirePicks {
		taken[id] = true
	}
	for _, id := range st.Bans {
		taken[id] = true
	}

	cands := make([]Hero, 0, len(heroes))
	for _, h := range heroes {
		if !taken[h.HeroID] && h.Matches >= MinMatches && h.HeroID != 0 {
			cands = append(cands, h)
		}
	}
	sort.Slice(cands, func(i, j int) bool {
		if cands[i].ShrunkWinrate != cands[j].ShrunkWinrate {
			return cands[i].ShrunkWinrate > cands[j].ShrunkWinrate
		}
		return cands[i].Matches > cands[j].Matches // стабильность
	})
	if len(cands) > MaxRecommendations {
		cands = cands[:MaxRecommendations]
	}

	verb := "пик"
	if st.NextAction == "radiant_ban" || st.NextAction == "dire_ban" {
		verb = "бан: лишить соперника героя с"
	}
	out := make([]Recommendation, 0, len(cands))
	for _, h := range cands {
		out = append(out, Recommendation{
			HeroID:          h.HeroID,
			Hero:            h.Name,
			ExpectedWinrate: h.ShrunkWinrate,
			Reason: fmt.Sprintf(
				"%s сглаженным винрейтом %.1f%% на %d матчах меты",
				verb, h.ShrunkWinrate*100, h.Matches),
		})
	}
	return out
}
