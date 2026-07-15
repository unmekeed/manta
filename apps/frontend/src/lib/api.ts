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

export interface GameError {
  type: string;
  game_time: number;
  delta_wp: number;
  safety_index: number;
  explanation: string;
}

export interface PlayerAnalysis {
  player_id: number;
  hero_id: number;
  hero: string;
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

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url);
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
};

export const heroLabel = (npc: string) =>
  npc.replace("npc_dota_hero_", "").replace(/_/g, " ");
