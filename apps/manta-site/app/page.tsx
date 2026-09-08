"use client";

import { useEffect, useMemo, useState } from "react";

type MatchCard = {
  match_id: number; radiant_win: boolean; winner: "radiant" | "dire";
  kills_radiant: number; kills_dire: number; duration_s: number; patch: number;
  tier: string; radiant_heroes: string[]; dire_heroes: string[];
  final_radiant_wp: number | null; generated_at: string;
};
type Hero = { id: number; name: string; npc: string };
type TimelinePoint = { game_time: number; radiant_wp: number; net_worth_diff: number };
type GameError = { type: string; game_time: number; delta_wp: number; safety_index: number; explanation: string };
type Player = { player_id: number; hero_id: number; hero?: string; lane?: string; player_name?: string; laning_score: number; impact_score: number; errors: GameError[] };
type Analysis = { match_id: number; status: string; available: { players: boolean; positions: boolean; kills: boolean; heatmaps: boolean }; win_probability: { final_radiant: number }; players: Player[]; narrative: string; partial: boolean; report_version?: string; model_version?: string };
type DraftResult = { predicted_winrate_radiant: number; suggestions: { hero_id: number; expected_winrate: number; reason: string }[] };

const demoMatches: MatchCard[] = [{
  match_id: 8979582375, radiant_win: true, winner: "radiant", kills_radiant: 53,
  kills_dire: 38, duration_s: 2657, patch: 741, tier: "Professional",
  radiant_heroes: ["npc_dota_hero_dragon_knight", "npc_dota_hero_earth_spirit", "npc_dota_hero_windrunner", "npc_dota_hero_shadow_demon", "npc_dota_hero_centaur"],
  dire_heroes: ["npc_dota_hero_life_stealer", "npc_dota_hero_invoker", "npc_dota_hero_axe", "npc_dota_hero_hoodwink", "npc_dota_hero_crystal_maiden"],
  final_radiant_wp: .84, generated_at: new Date().toISOString(),
}, {
  match_id: 8965097316, radiant_win: false, winner: "dire", kills_radiant: 29,
  kills_dire: 41, duration_s: 2282, patch: 741, tier: "Professional",
  radiant_heroes: ["npc_dota_hero_puck", "npc_dota_hero_mars", "npc_dota_hero_terrorblade", "npc_dota_hero_rubick", "npc_dota_hero_enchantress"],
  dire_heroes: ["npc_dota_hero_storm_spirit", "npc_dota_hero_tidehunter", "npc_dota_hero_luna", "npc_dota_hero_tusk", "npc_dota_hero_disruptor"],
  final_radiant_wp: .18, generated_at: new Date(Date.now() - 86400000).toISOString(),
}];

function heroName(npc: string) { return npc.replace("npc_dota_hero_", "").replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase()); }
function initials(npc: string) { return npc.replace("npc_dota_hero_", "").split("_").map(x => x[0]).join("").slice(0, 3).toUpperCase(); }
function duration(seconds: number) { return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; }
function percent(value: number | null | undefined) { return value == null ? "—" : `${Math.round(value * 100)}%`; }
const demoHeroes: Hero[] = Array.from(new Set(demoMatches.flatMap(m => [...m.radiant_heroes, ...m.dire_heroes]))).map((npc, i) => ({ id: i + 1, npc, name: heroName(npc) }));
const demoTimeline: TimelinePoint[] = Array.from({ length: 45 }, (_, i) => ({ game_time: i * 60, radiant_wp: Math.max(.18, Math.min(.91, .46 + i * .008 + Math.sin(i / 3) * .09)), net_worth_diff: Math.round((i - 14) * 620 + Math.sin(i) * 900) }));
const demoAnalysis: Analysis = { match_id: demoMatches[0].match_id, status: "completed", available: { players: true, positions: true, kills: true, heatmaps: true }, win_probability: { final_radiant: .84 }, partial: true, report_version: "1.0", model_version: "0.9", narrative: "Равная линия перешла в контроль карты после двух выигранных драк. Решающий сигнал — преимущество по живым героям и сохранённый темп.", players: demoMatches[0].radiant_heroes.map((hero, i) => ({ player_id: i, hero_id: i + 1, hero, player_name: ["collapse", "Larl", "Yatoro", "Mira", "Miposhka"][i], lane: ["Offlane", "Mid", "Carry", "Support", "Hard support"][i], laning_score: .58 + i * .035, impact_score: .61 + i * .045, errors: i < 3 ? [{ type: "critical_death", game_time: 460 + i * 650, delta_wp: -.062 + i * .04, safety_index: .72, explanation: ["Рискованный выход к верхней руне", "Потеря темпа после размена", "Поздний отход из ключевой зоны"][i] }] : [] })) };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api/manta${path}`, { ...init, headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json() as Promise<T>;
}

function HeroTokens({ heroes, side }: { heroes: string[]; side: "radiant" | "dire" }) {
  return <div className={`hero-row ${side}`}>{heroes.map(hero => <span className={`hero-token ${side}`} title={heroName(hero)} key={hero}>{initials(hero)}</span>)}</div>;
}

function WpChart({ points }: { points: TimelinePoint[] }) {
  if (!points.length) return <div className="empty">Поминутная кривая пока не рассчитана.</div>;
  const width = 820, height = 235, maxTime = Math.max(...points.map(p => p.game_time), 1);
  const coords = points.map(p => `${(p.game_time / maxTime) * width},${height - Math.max(0, Math.min(1, p.radiant_wp)) * height}`).join(" ");
  const area = `0,${height} ${coords} ${width},${height}`;
  return <figure className="chart" aria-label="Вероятность победы Radiant по ходу матча">
    <span className="chart-label top">100%</span><span className="chart-label mid">50%</span><span className="chart-label bottom">0%</span>
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none"><defs><linearGradient id="liveFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#43d7a1" stopOpacity=".34"/><stop offset="100%" stopColor="#43d7a1" stopOpacity="0"/></linearGradient></defs><path className="grid-line" d={`M0 0H${width} M0 ${height / 2}H${width} M0 ${height}H${width}`}/><polygon points={area} fill="url(#liveFill)"/><polyline className="wp-line" points={coords}/></svg>
    <div className="axis"><span>0&apos;</span><span>{Math.round(maxTime / 240)}&apos;</span><span>{Math.round(maxTime / 120)}&apos;</span><span>{Math.round(maxTime / 80)}&apos;</span><span>{Math.round(maxTime / 60)}&apos;</span></div>
  </figure>;
}

function DraftSimulator({ heroes }: { heroes: Hero[] }) {
  const [radiant, setRadiant] = useState<number[]>([]), [dire, setDire] = useState<number[]>([]);
  const [side, setSide] = useState<"radiant" | "dire">("radiant"), [query, setQuery] = useState("");
  const [result, setResult] = useState<DraftResult | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const byId = useMemo(() => new Map(heroes.map(h => [h.id, h])), [heroes]);
  const picked = new Set([...radiant, ...dire]);
  const pool = heroes.filter(h => !picked.has(h.id) && h.name.toLowerCase().includes(query.toLowerCase())).slice(0, 24);
  const add = (id: number) => { const set = side === "radiant" ? setRadiant : setDire; const values = side === "radiant" ? radiant : dire; if (values.length < 5) set([...values, id]); setResult(null); };
  async function simulate() { setBusy(true); setError(""); try { setResult(await api<DraftResult>("/draft/simulate", { method: "POST", body: JSON.stringify({ radiant_picks: radiant, dire_picks: dire, bans: [], next_action: `${side}_pick` }) })); } catch { setError("Draft Engine пока недоступен по публичному адресу."); } finally { setBusy(false); } }
  return <section id="draft" className="tab-section"><div className="detail-card draft-live"><div className="detail-title"><div><p className="eyebrow">DRAFT LAB</p><h2>Соберите состав и проверьте следующий выбор</h2></div><span>Ответ приходит из POST /draft/simulate</span></div>
    <div className="draft-teams">{(["radiant", "dire"] as const).map(team => { const list = team === "radiant" ? radiant : dire; return <div className={`draft-team ${team}`} key={team}><button className={side === team ? "side-toggle active" : "side-toggle"} onClick={() => setSide(team)}>{team === "radiant" ? "Radiant" : "Dire"} · {list.length}/5</button><div className="draft-picks">{list.map(id => <button key={id} onClick={() => (team === "radiant" ? setRadiant : setDire)(list.filter(x => x !== id))}>{byId.get(id)?.name || id} ×</button>)}</div></div>; })}</div>
    <div className="draft-controls"><input value={query} onChange={e => setQuery(e.target.value)} placeholder="Найти героя…"/><button className="primary-button" disabled={busy || radiant.length + dire.length === 0} onClick={simulate}>{busy ? "Считаю…" : "Симулировать"}</button></div>
    <div className="hero-pool">{pool.map(h => <button onClick={() => add(h.id)} key={h.id}>{h.name}</button>)}</div>
    {error && <p className="api-error">{error}</p>}{result && <div className="draft-result"><strong>Radiant {percent(result.predicted_winrate_radiant)}</strong><div className="probability"><i style={{ width: percent(result.predicted_winrate_radiant) }}/></div>{result.suggestions.slice(0, 5).map(s => <p key={s.hero_id}><b>{byId.get(s.hero_id)?.name || `Hero ${s.hero_id}`}</b><span>{percent(s.expected_winrate)}</span>{s.reason}</p>)}</div>}
  </div></section>;
}

export default function Home() {
  const [matches, setMatches] = useState<MatchCard[]>([]), [heroes, setHeroes] = useState<Hero[]>([]);
  const [selected, setSelected] = useState<MatchCard | null>(null), [analysis, setAnalysis] = useState<Analysis | null>(null), [timeline, setTimeline] = useState<TimelinePoint[]>([]);
  const [cursor, setCursor] = useState(""), [query, setQuery] = useState(""), [loading, setLoading] = useState(true), [demo, setDemo] = useState(false), [detailLoading, setDetailLoading] = useState(false);
  async function loadMatches(search = "", next = "") { setLoading(true); try { const params = new URLSearchParams({ limit: "12" }); if (search) params.set("query", search); if (next) params.set("cursor", next); const [page, dictionary] = await Promise.all([api<{ matches: MatchCard[]; next_cursor: string }>(`/matches?${params}`), heroes.length ? Promise.resolve({ heroes }) : api<{ heroes: Hero[] }>("/heroes")]); setMatches(next ? [...matches, ...page.matches] : page.matches); setCursor(page.next_cursor); setHeroes(dictionary.heroes); setDemo(false); if (!next && page.matches[0]) setSelected(page.matches[0]); } catch { setMatches(demoMatches); setHeroes(demoHeroes); setSelected(demoMatches[0]); setCursor(""); setDemo(true); } finally { setLoading(false); } }
  useEffect(() => { void (async () => { try { const [page, dictionary] = await Promise.all([api<{ matches: MatchCard[]; next_cursor: string }>("/matches?limit=12"), api<{ heroes: Hero[] }>("/heroes")]); setMatches(page.matches); setCursor(page.next_cursor); setHeroes(dictionary.heroes); setDemo(false); if (page.matches[0]) setSelected(page.matches[0]); } catch { setMatches(demoMatches); setHeroes(demoHeroes); setSelected(demoMatches[0]); setDemo(true); } finally { setLoading(false); } })(); }, []);
  const selectedId = selected?.match_id, selectedRadiantWin = selected?.radiant_win, selectedWp = selected?.final_radiant_wp;
  useEffect(() => { if (!selectedId) return; void (async () => { if (demo) { await Promise.resolve(); setAnalysis({ ...demoAnalysis, match_id: selectedId, win_probability: { final_radiant: selectedWp ?? .5 }, narrative: selectedId === demoAnalysis.match_id ? demoAnalysis.narrative : "Демонстрационный разбор: подключение к публичному API ещё не опубликовано." }); setTimeline(demoTimeline.map(p => ({ ...p, radiant_wp: selectedRadiantWin ? p.radiant_wp : 1 - p.radiant_wp }))); setDetailLoading(false); return; } try { const [a, t] = await Promise.all([api<Analysis>(`/matches/${selectedId}/analysis`), api<{ points: TimelinePoint[] }>(`/matches/${selectedId}/timeline`)]); setAnalysis(a); setTimeline(t.points || []); } catch { setAnalysis(null); setTimeline([]); } finally { setDetailLoading(false); } })(); }, [selectedId, selectedRadiantWin, selectedWp, demo]);
  const heroByNpc = useMemo(() => new Map(heroes.map(h => [h.npc, h.name])), [heroes]);
  const a = analysis, available = a?.available || { players: false, positions: false, kills: false, heatmaps: false };
  function submit(e: { preventDefault(): void }) { e.preventDefault(); void loadMatches(query.trim()); }
  return <main className="min-h-screen"><header className="topbar"><a href="#matches" className="brand"><span className="brand-mark"><span/></span><span>MANTA</span><b>BETA</b></a><form className="search-wrap" onSubmit={submit}><span>⌕</span><input aria-label="Номер матча или герой" value={query} onChange={e => setQuery(e.target.value)} placeholder="Номер матча или герой…"/><button>Найти</button></form><div className="header-actions"><span className={demo ? "live-pill demo" : "live-pill"}><i/>{demo ? "демо · API ожидает публикации" : "живые данные"}</span></div></header>
    <div className="shell"><aside className="sidebar"><nav><p className="eyebrow">АНАЛИТИКА</p><a className="active" href="#overview">⌁ Обзор</a><a href="#matches">⚔ Матчи</a><a href="#draft">◇ Драфт</a><a href="#players">◎ Игроки</a></nav><div className="recent"><p className="eyebrow">НЕДАВНИЕ</p>{matches.slice(0, 6).map(m => <button aria-label={`Открыть матч ${m.match_id}`} className={selected?.match_id === m.match_id ? "recent-match active" : "recent-match"} key={m.match_id} onClick={() => { setDetailLoading(true); setSelected(m); }}><span><b>{m.match_id}</b><small>{duration(m.duration_s)} · {m.tier}</small></span><i aria-hidden="true" className={m.radiant_win ? "radiant-dot" : "dire-dot"}/></button>)}</div><div className="model-card"><div><span>◉ WP MODEL</span><b>LIVE</b></div><strong>Контракт API v1</strong><small>5 публичных маршрутов · fail-closed</small></div></aside>
      <section className="workspace"><section id="matches"><div className="section-head"><div><p className="eyebrow">MATCH EXPLORER</p><h1>Последние разобранные матчи</h1></div>{loading && <span className="loading-copy">обновляем…</span>}</div><div className="match-strip">{matches.map(m => <button key={m.match_id} className={selected?.match_id === m.match_id ? "match-mini active" : "match-mini"} onClick={() => { setDetailLoading(true); setSelected(m); }}><span>#{m.match_id}</span><strong className={m.winner}>{m.kills_radiant} : {m.kills_dire}</strong><small>{m.tier} · {duration(m.duration_s)}</small></button>)}</div>{cursor && !demo && <button className="load-more" onClick={() => void loadMatches(query, cursor)}>Показать ещё</button>}</section>
      {selected && <><div className="crumbs"><span>Матчи</span><span>/</span><strong>#{selected.match_id}</strong></div><section className="score-card" id="overview"><div className="match-meta"><div><b>{selected.tier.toUpperCase()}</b><span>Патч {selected.patch}</span><span>{duration(selected.duration_s)}</span></div><div>{detailLoading ? "загружаем разбор…" : a ? "разбор готов" : "разбор недоступен"}</div></div><div className="score-grid"><div className="team radiant-team"><div><span>RADIANT</span><strong>{selected.radiant_win ? "ПОБЕДА" : "ПОРАЖЕНИЕ"}</strong></div><HeroTokens heroes={selected.radiant_heroes} side="radiant"/></div><div className="score"><strong>{selected.kills_radiant}</strong><span>:</span><strong>{selected.kills_dire}</strong></div><div className="team dire-team"><HeroTokens heroes={selected.dire_heroes} side="dire"/><div><span>DIRE</span><strong>{selected.radiant_win ? "ПОРАЖЕНИЕ" : "ПОБЕДА"}</strong></div></div></div></section>
      <nav className="tabs"><a href="#overview">Обзор</a><a href="#players">Игроки</a><a href="#moments">Ошибки</a><a href="#draft">Драфт</a></nav>
      <div className="main-grid"><section className="wp-card"><div className="section-head"><div><p className="eyebrow">WIN PROBABILITY</p><h2>Как менялась игра</h2></div><div className="wp-now"><span>Radiant</span><strong>{percent(a?.win_probability.final_radiant ?? selected.final_radiant_wp)}</strong></div></div><WpChart points={timeline}/><p className="chart-note">Поминутная модель отделяет факт 50/50 от отсутствующего прогноза.</p></section><section className="insight-card"><div className="section-head"><div><p className="eyebrow">MANTA INSIGHT</p><h2>Почему победила команда</h2></div><span className="spark">✦</span></div><p className="summary">{a?.narrative || "Для этого матча полный разбор пока не готов."}</p><div className="availability">{Object.entries(available).map(([key, ok]) => <div key={key}><i className={ok ? "ok" : ""}/><span>{{ players: "Игроки", positions: "Позиции", kills: "Убийства", heatmaps: "Тепловые карты" }[key as keyof typeof available]}</span><b>{ok ? "доступно" : "нет данных"}</b></div>)}</div></section></div>
      <section id="players" className="tab-section detail-card"><div className="detail-title"><div><p className="eyebrow">PLAYER IMPACT</p><h2>Вклад игроков</h2></div><span>{available.players ? "Лейнинг и влияние на вероятность победы" : "Источник матча не содержит поигроковый разрез"}</span></div>{available.players && a?.players.length ? <div className="table-wrap"><table><thead><tr><th>Игрок</th><th>Герой</th><th>Линия</th><th>Лейнинг</th><th>Impact</th><th>Ошибки</th></tr></thead><tbody>{a.players.map(p => <tr key={p.player_id}><td>{p.player_name || `Игрок ${p.player_id}`}</td><td>{heroByNpc.get(p.hero || "") || heroName(p.hero || "unknown")}</td><td>{p.lane || "—"}</td><td>{percent(p.laning_score)}</td><td className="impact">{percent(p.impact_score)}</td><td>{p.errors?.length || "—"}</td></tr>)}</tbody></table></div> : <div className="empty">Поигроковые данные для этого матча недоступны — это ограничение источника, а не ошибка отчёта.</div>}</section>
      <section id="moments" className="tab-section turning-points"><div className="section-head"><div><p className="eyebrow">КЛЮЧЕВЫЕ ОШИБКИ</p><h2>Где менялась игра</h2></div></div>{available.kills && a ? <div className="event-grid">{a.players.flatMap(p => p.errors || []).sort((x,y) => Math.abs(y.delta_wp) - Math.abs(x.delta_wp)).slice(0, 6).map((e, i) => <article className="event-card" key={`${e.game_time}-${i}`}><div><span className="minute">{duration(e.game_time)}</span><b className={e.delta_wp >= 0 ? "positive" : "negative"}>{e.delta_wp >= 0 ? "+" : ""}{percent(e.delta_wp)}</b></div><h3>{e.type.replaceAll("_", " ")}</h3><p>{e.explanation}</p></article>)}</div> : <div className="empty">События убийств отсутствуют в исходных данных этого матча.</div>}</section></>}
      <DraftSimulator heroes={heroes}/></section></div>
  </main>;
}
