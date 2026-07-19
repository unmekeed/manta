import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api, type DraftRecommendation, type Hero } from "../lib/api";

// Драфт-симулятор (C6): пики обеих команд → винрейт Radiant и топ-подсказки
// Draft Engine (частотный бейзлайн спринта 37; GNN — при 10^4+ матчей).

type Side = "radiant" | "dire";

function HeroChip({ hero, onRemove }: { hero: Hero; onRemove: () => void }) {
  return (
    <button className="hero-chip" onClick={onRemove} title="Убрать из драфта">
      {hero.name} ×
    </button>
  );
}

export default function DraftPage() {
  const heroesQ = useQuery({ queryKey: ["heroes"], queryFn: api.heroes });
  const [picks, setPicks] = useState<{ radiant: number[]; dire: number[] }>({
    radiant: [],
    dire: [],
  });
  const [side, setSide] = useState<Side>("radiant");
  const [filter, setFilter] = useState("");
  const [rec, setRec] = useState<DraftRecommendation | null>(null);

  const byId = useMemo(
    () => new Map((heroesQ.data ?? []).map((h) => [h.id, h])),
    [heroesQ.data],
  );

  const simulate = useMutation({
    mutationFn: () =>
      api.simulateDraft({
        radiant_picks: picks.radiant,
        dire_picks: picks.dire,
        bans: [],
        next_action: side === "radiant" ? "radiant_pick" : "dire_pick",
      }),
    onSuccess: setRec,
  });

  if (heroesQ.isLoading) return <div className="loading">Загрузка героев…</div>;
  if (heroesQ.error || !heroesQ.data)
    return <div className="error-msg">Словарь героев недоступен.</div>;

  const picked = new Set([...picks.radiant, ...picks.dire]);
  const pool = heroesQ.data.filter(
    (h) => !picked.has(h.id) && h.name.toLowerCase().includes(filter.toLowerCase()),
  );

  const addPick = (h: Hero) => {
    if (picks[side].length >= 5) return;
    setPicks({ ...picks, [side]: [...picks[side], h.id] });
    setRec(null);
  };
  const removePick = (team: Side, id: number) => {
    setPicks({ ...picks, [team]: picks[team].filter((x) => x !== id) });
    setRec(null);
  };

  const wp = rec ? rec.predicted_winrate_radiant : null;

  return (
    <>
      <h1>
        Драфт-симулятор
        <span className="beta-badge" title="Частотный бейзлайн на реплей-матчах датасета; точность растёт с его объёмом.">
          beta
        </span>
      </h1>

      <div className="draft-teams">
        {(["radiant", "dire"] as Side[]).map((team) => (
          <div key={team} className={`panel draft-team ${team}`}>
            <h3>
              {team === "radiant" ? "Radiant" : "Dire"} ({picks[team].length}/5)
              <label className="pick-side">
                <input
                  type="radio"
                  name="side"
                  checked={side === team}
                  onChange={() => setSide(team)}
                />{" "}
                пикает
              </label>
            </h3>
            <div className="chips">
              {picks[team].length === 0 && <span className="muted">нет пиков</span>}
              {picks[team].map((id) => {
                const h = byId.get(id);
                return h ? (
                  <HeroChip key={id} hero={h} onRemove={() => removePick(team, id)} />
                ) : null;
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="draft-controls">
        <input
          className="hero-filter"
          placeholder="Поиск героя…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <button
          className="simulate-btn"
          disabled={simulate.isPending || picks.radiant.length + picks.dire.length === 0}
          onClick={() => simulate.mutate()}
        >
          {simulate.isPending ? "Считаю…" : "Симулировать"}
        </button>
      </div>

      <div className="hero-pool panel">
        {pool.map((h) => (
          <button key={h.id} className="hero-pick" onClick={() => addPick(h)}>
            {h.name}
          </button>
        ))}
      </div>

      {simulate.error && (
        <div className="error-msg">Draft Engine недоступен — попробуйте позже.</div>
      )}

      {rec && (
        <>
          <h2>Прогноз</h2>
          <div className="panel draft-result">
            <div className="wp-line">
              <span className="radiant-label">Radiant {(wp! * 100).toFixed(1)}%</span>
              <span className="wp-track">
                <i style={{ width: `${Math.round(wp! * 100)}%` }} />
              </span>
              <span className="dire-label">{((1 - wp!) * 100).toFixed(1)}% Dire</span>
            </div>
          </div>

          <h2>
            Подсказки для {side === "radiant" ? "Radiant" : "Dire"}
          </h2>
          <div className="suggestions">
            {rec.suggestions.map((s) => {
              const h = byId.get(s.hero_id);
              return (
                <div className="suggestion" key={s.hero_id}>
                  <button
                    className="hero-pick add"
                    title="Добавить в драфт"
                    onClick={() => h && addPick(h)}
                  >
                    {h ? h.name : `hero_${s.hero_id}`}
                  </button>
                  <span className="ewr">{(s.expected_winrate * 100).toFixed(1)}%</span>
                  <span className="reason">
                    {s.reason.replace(
                      /hero_(\d+)/g,
                      (_, id) => byId.get(Number(id))?.name ?? `hero_${id}`,
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}
