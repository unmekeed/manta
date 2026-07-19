// Типизированный клиент REST API шлюза (контракты Гл. 7).

export interface TimelinePoint {
  game_time: number;
  radiant_wp: number;
  net_worth_diff: number;
}

export interface Timeline {
  match_id: number;
  points: TimelinePoint[];
}

export interface FeatureContribution {
  feature: string;
  value: number; // SHAP-вклад в log-odds; знак — в чью пользу тянет фича
}

export interface MapPos {
  x: number; // доли карты 0..1, (0,0) — юго-запад (база Radiant)
  y: number;
}

export interface GameError {
  type: string;
  game_time: number;
  delta_wp: number;
  safety_index: number;
  explanation: string;
  pos?: MapPos;
  top_contributions?: FeatureContribution[];
}

export interface PlayerAnalysis {
  player_id: number;
  hero_id: number;
  hero: string;
  lane: string;
  player_name: string;
  laning_score: number;
  impact_score: number;
  errors: GameError[];
}

export interface MatchAnalysis {
  match_id: number;
  status: string;
  win_probability: { final_radiant: number };
  players: PlayerAnalysis[];
  narrative: string;
  partial: boolean;
  report_version: string;
  model_version: string;
}

export interface MatchListItem {
  match_id: number;
  final_radiant_wp: string;
  narrative: string;
  report_version: string;
  generated_at: string;
}

export interface Hero {
  id: number;
  name: string;
  npc: string;
}

export interface DraftState {
  radiant_picks: number[];
  dire_picks: number[];
  bans: number[];
  next_action: "radiant_pick" | "dire_pick";
}

export interface DraftSuggestion {
  hero_id: number;
  expected_winrate: number;
  reason: string;
}

export interface DraftRecommendation {
  predicted_winrate_radiant: number;
  suggestions: DraftSuggestion[];
}

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`${url}: HTTP ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`${url}: HTTP ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  matches: () =>
    get<{ matches: MatchListItem[] }>("/api/v1/matches").then((r) => r.matches),
  analysis: (matchId: string) =>
    get<MatchAnalysis>(`/api/v1/matches/${matchId}/analysis`),
  timeline: (matchId: string) =>
    get<Timeline>(`/api/v1/matches/${matchId}/timeline`),
  heroes: () =>
    get<{ heroes: Hero[] }>("/api/v1/heroes").then((r) => r.heroes),
  simulateDraft: (state: DraftState) =>
    post<DraftRecommendation>("/api/v1/draft/simulate", state),
};

export const heroLabel = (npc: string) =>
  npc.replace("npc_dota_hero_", "").replace(/_/g, " ");

// Человекочитаемые метки фич WP-модели (для SHAP-драйверов у ошибок).
const FEATURE_LABELS: Record<string, string> = {
  game_time: "время игры",
  networth_diff: "разрыв нетворса",
  networth_rel: "отн. разрыв нетворса",
  xp_diff: "разрыв опыта",
  kills_diff: "разница убийств",
  kills_total: "сумма убийств",
  position_advance: "продвижение по карте",
  alive_diff: "живые герои",
  towers_diff: "вышки",
  rax_diff: "бараки",
};

export const featureLabel = (name: string) => FEATURE_LABELS[name] ?? name;
