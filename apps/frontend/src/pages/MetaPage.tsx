// Мета героев (Гл. 7: /meta/heroes) — агрегаты по всем проанализированным
// матчам. Сортировка по сглаженному винрейту: сырой винрейт героя с парой
// матчей — шум (сглаживание прижимает его к 50%).

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, heroLabel, type MetaHero } from "../lib/api";

type SortKey = "shrunk_winrate" | "matches" | "avg_gpm";

export default function MetaPage() {
  const meta = useQuery({ queryKey: ["meta"], queryFn: api.metaHeroes });
  const [sort, setSort] = useState<SortKey>("shrunk_winrate");

  if (meta.isLoading) return <div className="loading">Загрузка меты…</div>;
  if (meta.error || !meta.data || meta.data.length === 0)
    return (
      <div className="error-msg">
        Мета пока пуста — появится после первых проанализированных матчей.
      </div>
    );

  const heroes = [...meta.data].sort((a, b) => b[sort] - a[sort]);
  const updated = heroes[0]?.updated_at;

  return (
    <>
      <h1>Мета героев</h1>
      <div className="panel">
        <table className="players">
          <thead>
            <tr>
              <th>Герой</th>
              <th className="sortable" onClick={() => setSort("matches")}>
                Пики{sort === "matches" ? " ▾" : ""}
              </th>
              <th>Пикрейт</th>
              <th>Винрейт</th>
              <th className="sortable" onClick={() => setSort("shrunk_winrate")}>
                Винрейт (сглаж.){sort === "shrunk_winrate" ? " ▾" : ""}
              </th>
              <th className="sortable" onClick={() => setSort("avg_gpm")}>
                GPM{sort === "avg_gpm" ? " ▾" : ""}
              </th>
            </tr>
          </thead>
          <tbody>
            {heroes.map((h) => (
              <tr key={h.hero}>
                <td className="hero-name">{heroLabel(h.hero)}</td>
                <td>{h.matches}</td>
                <td>{(h.pick_rate * 100).toFixed(1)}%</td>
                <td>{(h.winrate * 100).toFixed(0)}%</td>
                <td>
                  <span
                    className={
                      h.shrunk_winrate >= 0.5 ? "wr-positive" : "wr-negative"
                    }
                  >
                    {(h.shrunk_winrate * 100).toFixed(1)}%
                  </span>
                </td>
                <td>{h.avg_gpm.toFixed(0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="meta">
        {heroes.length} героев ·{" "}
        {updated && `обновлено ${new Date(updated).toLocaleString("ru-RU")}`} ·
        сглаживание: +10 виртуальных матчей с винрейтом 50%
      </div>
    </>
  );
}
