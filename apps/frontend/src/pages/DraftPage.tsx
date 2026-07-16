// Драфт-симулятор (Гл. 8, роадмап 17–18) поверх бейзлайна Draft Engine:
// оценка драфта и рекомендации — сглаженные винрейты нашей меты
// (POST /api/v1/draft/simulate). Синергии/контрпики придут с GNN.

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, heroLabel, type DraftState, type MetaHero } from "../lib/api";

type Slot = "radiant_picks" | "dire_picks" | "bans";

const SLOT_TITLE: Record<Slot, string> = {
  radiant_picks: "Пики Radiant",
  dire_picks: "Пики Dire",
  bans: "Баны",
};
const SLOT_LIMIT: Record<Slot, number> = {
  radiant_picks: 5,
  dire_picks: 5,
  bans: 14,
};

export default function DraftPage() {
  const meta = useQuery({ queryKey: ["meta"], queryFn: api.metaHeroes });
  const [draft, setDraft] = useState<DraftState>({
    radiant_picks: [],
    dire_picks: [],
    bans: [],
    next_action: "radiant_pick",
  });
  const sim = useMutation({ mutationFn: api.simulateDraft });

  if (meta.isLoading) return <div className="loading">Загрузка меты…</div>;
  if (meta.error || !meta.data || meta.data.length === 0)
    return (
      <div className="error-msg">
        Драфт-симулятору нужна мета — она появится после первых
        проанализированных матчей.
      </div>
    );

  const heroes = meta.data.filter((h) => h.hero_id > 0);
  const byId = new Map(heroes.map((h) => [h.hero_id, h]));
  const taken = new Set([
    ...draft.radiant_picks,
    ...draft.dire_picks,
    ...draft.bans,
  ]);

  const add = (slot: Slot, id: number) => {
    if (!id || taken.has(id) || draft[slot].length >= SLOT_LIMIT[slot]) return;
    setDraft({ ...draft, [slot]: [...draft[slot], id] });
    sim.reset();
  };
  const remove = (slot: Slot, id: number) => {
    setDraft({ ...draft, [slot]: draft[slot].filter((x) => x !== id) });
    sim.reset();
  };

  const wp = sim.data?.predicted_winrate_radiant;

  return (
    <>
      <h1>Драфт-симулятор</h1>

      {(Object.keys(SLOT_TITLE) as Slot[]).map((slot) => (
        <div key={slot}>
          <h2>{SLOT_TITLE[slot]}</h2>
          <div className="panel draft-slot">
            {draft[slot].map((id) => (
              <button
                key={id}
                className="chip"
                title="убрать"
                onClick={() => remove(slot, id)}
              >
                {heroLabel(byId.get(id)?.hero ?? `#${id}`)} ✕
              </button>
            ))}
            {draft[slot].length < SLOT_LIMIT[slot] && (
              <HeroSelect
                heroes={heroes}
                taken={taken}
                onPick={(id) => add(slot, id)}
              />
            )}
          </div>
        </div>
      ))}

      <h2>Следующее действие</h2>
      <div className="panel draft-slot">
        <select
          value={draft.next_action}
          onChange={(e) =>
            setDraft({
              ...draft,
              next_action: e.target.value as DraftState["next_action"],
            })
          }
        >
          <option value="radiant_pick">пик Radiant</option>
          <option value="dire_pick">пик Dire</option>
          <option value="radiant_ban">бан Radiant</option>
          <option value="dire_ban">бан Dire</option>
        </select>
        <button
          className="chip primary"
          disabled={sim.isPending}
          onClick={() => sim.mutate(draft)}
        >
          {sim.isPending ? "Считаю…" : "Симулировать"}
        </button>
      </div>

      {sim.error && (
        <div className="error-msg">Не удалось получить рекомендацию.</div>
      )}
      {sim.data && (
        <>
          <h2>Оценка драфта</h2>
          <div className="panel">
            <div className="wp-line">
              <span className="wr-positive">
                Radiant {((wp ?? 0.5) * 100).toFixed(1)}%
              </span>
              <span className="score-bar draft-bar">
                <i style={{ width: `${(wp ?? 0.5) * 100}%` }} />
              </span>
              <span className="wr-negative">
                {(100 - (wp ?? 0.5) * 100).toFixed(1)}% Dire
              </span>
            </div>
          </div>

          <h2>Рекомендации ({sim.data.model})</h2>
          <div className="errors">
            {sim.data.recommendations.length === 0 ? (
              <div className="panel">Нет кандидатов — мета слишком мала.</div>
            ) : (
              sim.data.recommendations.map((r) => (
                <div className="error-item" key={r.hero_id}>
                  <span className="who hero-name">{heroLabel(r.hero)}</span>
                  {r.reason}
                </div>
              ))
            )}
          </div>
        </>
      )}
    </>
  );
}

function HeroSelect({
  heroes,
  taken,
  onPick,
}: {
  heroes: MetaHero[];
  taken: Set<number>;
  onPick: (id: number) => void;
}) {
  return (
    <select value="" onChange={(e) => onPick(Number(e.target.value))}>
      <option value="">+ герой…</option>
      {heroes
        .filter((h) => !taken.has(h.hero_id))
        .sort((a, b) => a.hero.localeCompare(b.hero))
        .map((h) => (
          <option key={h.hero_id} value={h.hero_id}>
            {heroLabel(h.hero)}
          </option>
        ))}
    </select>
  );
}
