package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	corev1 "github.com/unmekeed/manta/proto/core/v1"
)

// -- словарь героев -----------------------------------------------------------

// Hero — элемент /api/v1/heroes: то, что нужно фронтенду для пикера.
type Hero struct {
	ID   uint32 `json:"id"`
	Name string `json:"name"` // локализованное имя Valve ("Anti-Mage")
	NPC  string `json:"npc"`  // npc_dota_hero_* — ключ иконок/отчётов
}

// LoadHeroes читает libs/data/heroes.json (словарь спринта 18 — источник
// истины о героях в монорепо) и отдаёт плоский список для фронтенда.
func LoadHeroes(path string) ([]Hero, error) {
	if path == "" {
		path = filepath.Join("..", "..", "libs", "data", "heroes.json")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var byNPC map[string]struct {
		ID   uint32 `json:"id"`
		Name string `json:"localized_name"`
	}
	if err := json.Unmarshal(raw, &byNPC); err != nil {
		return nil, err
	}
	heroes := make([]Hero, 0, len(byNPC))
	for npc, h := range byNPC {
		heroes = append(heroes, Hero{ID: h.ID, Name: h.Name, NPC: npc})
	}
	sort.Slice(heroes, func(i, j int) bool { return heroes[i].Name < heroes[j].Name })
	return heroes, nil
}

// ListHeroes — GET /api/v1/heroes: словарь героев для драфт-пикера.
func (h *Handlers) ListHeroes(w http.ResponseWriter, _ *http.Request) {
	if len(h.Heroes) == 0 {
		writeProblem(w, http.StatusServiceUnavailable, "service-unavailable",
			"Hero dictionary not loaded", "heroes.json отсутствует")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"heroes": h.Heroes})
}

// -- симуляция драфта ---------------------------------------------------------

type draftRequest struct {
	RadiantPicks []uint32 `json:"radiant_picks"`
	DirePicks    []uint32 `json:"dire_picks"`
	Bans         []uint32 `json:"bans"`
	NextAction   string   `json:"next_action"` // radiant_pick | dire_pick
}

type draftSuggestion struct {
	HeroID          uint32  `json:"hero_id"`
	ExpectedWinrate float64 `json:"expected_winrate"`
	Reason          string  `json:"reason"`
}

type draftResponse struct {
	PredictedWinrateRadiant float64           `json:"predicted_winrate_radiant"`
	Suggestions             []draftSuggestion `json:"suggestions"`
}

// SimulateDraft — POST /api/v1/draft/simulate: прокси к Draft Engine
// (gRPC :50053, спринт 37). Тело запроса — состояние драфта, ответ —
// винрейт Radiant и топ-подсказки для действующей стороны.
func (h *Handlers) SimulateDraft(w http.ResponseWriter, r *http.Request) {
	if h.Draft == nil {
		writeProblem(w, http.StatusServiceUnavailable, "service-unavailable",
			"Draft engine not configured", "")
		return
	}
	var req draftRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad-request",
			"Invalid draft state", err.Error())
		return
	}
	if len(req.RadiantPicks) > 5 || len(req.DirePicks) > 5 {
		writeProblem(w, http.StatusBadRequest, "bad-request",
			"Invalid draft state", "максимум 5 пиков на сторону")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	rec, err := h.Draft.SimulateDraft(ctx, &corev1.DraftState{
		RadiantPicks: req.RadiantPicks,
		DirePicks:    req.DirePicks,
		Bans:         req.Bans,
		NextAction:   req.NextAction,
	})
	if err != nil {
		if status.Code(err) == codes.Unavailable {
			writeProblem(w, http.StatusServiceUnavailable, "service-unavailable",
				"Draft engine unavailable", err.Error())
			return
		}
		writeProblem(w, http.StatusInternalServerError, "internal-error",
			"Draft simulation failed", err.Error())
		return
	}

	resp := draftResponse{
		PredictedWinrateRadiant: rec.GetPredictedWinrateRadiant(),
		Suggestions:             make([]draftSuggestion, 0, len(rec.GetSuggestions())),
	}
	for _, s := range rec.GetSuggestions() {
		resp.Suggestions = append(resp.Suggestions, draftSuggestion{
			HeroID:          s.GetHeroId(),
			ExpectedWinrate: s.GetExpectedWinrate(),
			Reason:          s.GetReason(),
		})
	}
	writeJSON(w, http.StatusOK, resp)
}
