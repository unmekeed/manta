import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import WpChart from "../components/WpChart";
import { api, heroLabel, type PlayerAnalysis } from "../lib/api";

function ScoreBar({ value }: { value: number }) {
  return (
    <>
      <span className="score-bar">
        <i style={{ width: `${Math.round(value * 100)}%` }} />
      </span>{" "}
      {value.toFixed(2)}
    </>
  );
}

function PlayersTable({ players }: { players: PlayerAnalysis[] }) {
  return (
    <table className="players">
      <thead>
        <tr>
          <th>Игрок</th>
          <th>Герой</th>
          <th>Линия</th>
          <th>Лейнинг</th>
          <th>Импакт</th>
          <th>Ошибки</th>
        </tr>
      </thead>
      <tbody>
        {players.map((p) => (
          <tr key={p.player_id} className={p.player_id < 5 ? "team-radiant" : "team-dire"}>
            <td>{p.player_name || `Игрок ${p.player_id}`}</td>
            <td className="hero-name">{heroLabel(p.hero)}</td>
            <td>{p.lane || "—"}</td>
            <td>
              <ScoreBar value={p.laning_score} />
            </td>
            <td>
              <ScoreBar value={p.impact_score} />
            </td>
            <td>{p.errors.length || "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function MatchPage() {
  const { matchId = "" } = useParams();
  const analysis = useQuery({
    queryKey: ["analysis", matchId],
    queryFn: () => api.analysis(matchId),
  });
  const timeline = useQuery({
    queryKey: ["timeline", matchId],
    queryFn: () => api.timeline(matchId),
  });

  if (analysis.isLoading || timeline.isLoading)
    return <div className="loading">Загрузка разбора…</div>;
  if (analysis.error || !analysis.data)
    return (
      <div className="error-msg">
        Отчёт не найден — матч ещё обрабатывается или не загружен.
      </div>
    );

  const a = analysis.data;
  const wp = a.win_probability.final_radiant;
  const errors = a.players
    .flatMap((p) =>
      p.errors.map((e) => ({ ...e, player: p.player_name || heroLabel(p.hero) })),
    )
    .sort((x, y) => x.delta_wp - y.delta_wp);

  return (
    <>
      <h1>
        Матч {a.match_id}{" "}
        <span className={`wp-badge ${wp >= 0.5 ? "radiant" : "dire"}`}>
          финальная WP Radiant {(wp * 100).toFixed(0)}%
        </span>
      </h1>
      <div className="narrative">{a.narrative}</div>

      <h2>Win Probability</h2>
      {timeline.data && <WpChart points={timeline.data.points} />}

      <h2>Игроки</h2>
      <div className="panel">
        <PlayersTable players={a.players} />
      </div>

      <h2>Ключевые ошибки (ΔWP)</h2>
      {errors.length === 0 ? (
        <div className="panel">Критических ошибок не обнаружено.</div>
      ) : (
        <div className="errors">
          {errors.slice(0, 8).map((e, i) => (
            <div className="error-item" key={i}>
              <span className="dwp">{(e.delta_wp * 100).toFixed(0)}%</span>
              <span className="who">{e.player}</span>
              {e.explanation}
              {e.safety_index >= 0.6 && (
                <span className="si-badge">риск {e.safety_index.toFixed(2)}</span>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="meta">
        отчёт v{a.report_version} · модель v{a.model_version} ·{" "}
        {a.partial ? "частичный разбор (бейзлайны)" : "полный разбор"}
      </div>
    </>
  );
}
