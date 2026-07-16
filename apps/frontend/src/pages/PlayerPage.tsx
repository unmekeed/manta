// Профиль игрока (Гл. 7: /players/{id}/profile) — материализованные
// агрегаты по всем проанализированным матчам. Профили накапливаются с
// момента появления account_id в конвейере (миграция CH 007) — старые
// матчи в них не входят.

import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { api, heroLabel } from "../lib/api";

export default function PlayerPage() {
  const { playerId = "" } = useParams();
  const profile = useQuery({
    queryKey: ["player", playerId],
    queryFn: () => api.playerProfile(playerId),
    retry: false,
  });

  if (profile.isLoading) return <div className="loading">Загрузка профиля…</div>;
  if (profile.error || !profile.data)
    return (
      <div className="error-msg">
        Профиль не найден — он появится после первого проанализированного
        матча этого игрока.
      </div>
    );

  const p = profile.data;
  return (
    <>
      <h1>
        {p.nickname || `Игрок ${p.player_id}`}{" "}
        <span className={`wp-badge ${p.winrate >= 0.5 ? "radiant" : "dire"}`}>
          винрейт {(p.winrate * 100).toFixed(0)}%
        </span>
      </h1>

      <div className="panel profile-grid">
        <div>
          <div className="stat-num">{p.matches}</div>
          <div className="stat-label">матчей</div>
        </div>
        <div>
          <div className="stat-num">{p.wins}</div>
          <div className="stat-label">побед</div>
        </div>
        <div>
          <div className="stat-num">{p.avg_gpm.toFixed(0)}</div>
          <div className="stat-label">средний GPM</div>
        </div>
        <div>
          <div className="stat-num">{p.avg_xpm.toFixed(0)}</div>
          <div className="stat-label">средний XPM</div>
        </div>
        <div>
          <div className="stat-num">{p.main_lane || "—"}</div>
          <div className="stat-label">основная линия</div>
        </div>
      </div>

      <h2>Любимые герои</h2>
      <div className="panel">
        {p.top_heroes.length === 0 ? (
          "Пока нет данных."
        ) : (
          <ul className="hero-list">
            {p.top_heroes.map((h) => (
              <li key={h.hero}>
                <span className="hero-name">{heroLabel(h.hero)}</span> —{" "}
                {h.matches} {plural(Number(h.matches))}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="meta">
        steam id {p.player_id} · обновлено{" "}
        {new Date(p.updated_at).toLocaleString("ru-RU")}
      </div>
    </>
  );
}

function plural(n: number): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return "матч";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "матча";
  return "матчей";
}
