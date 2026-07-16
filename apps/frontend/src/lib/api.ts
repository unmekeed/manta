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
  account_id?: string; // steam64 строкой (> 2^53); "0"/нет — аноним/старый отчёт
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

export interface HeatmapPlayer {
  player_id: number;
  hero: string;
  player_name: string;
  team: number; // 2 = Radiant, 3 = Dire
  cells: [number, number, number][]; // [gx, gy, count], (0,0) — юго-запад
}

export interface Heatmap {
  grid: number;
  players: HeatmapPlayer[];
}

export interface PlayerProfile {
  player_id: string; // steam64 (в строке — не влезает в double)
  nickname: string;
  matches: number;
  wins: number;
  winrate: number;
  avg_gpm: number;
  avg_xpm: number;
  main_lane: string;
  top_heroes: { hero: string; matches: string | number }[];
  updated_at: string;
}

export interface MetaHero {
  hero: string;
  hero_id: number;
  matches: number;
  wins: number;
  winrate: number;
  shrunk_winrate: number; // байесовское сглаживание к 0.5
  pick_rate: number;
  avg_gpm: number;
  updated_at: string;
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
  heatmap: (matchId: string) =>
    get<Heatmap>(`/api/v1/matches/${matchId}/heatmap`),
  playerProfile: (playerId: string) =>
    get<PlayerProfile>(`/api/v1/players/${playerId}/profile`),
  metaHeroes: () =>
    get<{ heroes: MetaHero[] }>("/api/v1/meta/heroes").then((r) => r.heroes),
};

export const heroLabel = (npc: string) =>
  npc.replace("npc_dota_hero_", "").replace(/_/g, " ");
