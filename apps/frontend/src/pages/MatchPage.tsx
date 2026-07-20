import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import DeathMap, { type DeathPoint } from "../components/DeathMap";
import WpChart from "../components/WpChart";
import {
  api,
  featureLabel,
  heroLabel,
  type FeatureContribution,
  type PlayerAnalysis,
} from "../lib/api";

// SHAP-вклады снапшота после ошибки: какие фичи модель «увидела» главными
// в состоянии игры (Гл. 6.2; данные в отчёте со спринта 29).
function ErrorDrivers({ drivers }: { drivers: FeatureContribution[] }) {
  const max = Math.max(...drivers.map((d) => Math.abs(d.value)), 1e-9);
  return (
    <div className="drivers">
      {drivers.map((d) => (
        <span
          key={d.feature}
          className={`driver ${d.value >= 0 ? "pos" : "neg"}`}
          title={`SHAP-вклад ${d.value.toFixed(3)} в log-odds WP`}
        >
          <i style={{ width: `${Math.round((Math.abs(d.value) / max) * 36)}px` }} />
          {featureLabel(d.feature)}
        </span>
      ))}
    </div>
  );
}

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
      p.errors.map((e) => ({
        ...e,
        player: p.player_name || heroLabel(p.hero),
        team: (p.player_id < 5 ? "radiant" : "dire") as "radiant" | "dire",
      })),
    )
    .sort((x, y) => x.delta_wp - y.delta_wp);

  const deaths: DeathPoint[] = errors
    .filter((e) => e.pos)
    .map((e) => ({
      x: e.pos!.x,
      y: e.pos!.y,
      team: e.team,
      label:
        `${e.player}, ${Math.floor(e.game_time / 60)}' · ` +
        `ΔWP ${(e.delta_wp * 100).toFixed(0)}% · SI ${e.safety_index.toFixed(2)}`,
    }));

  return (
    <>
      <h1>
        Матч {a.match_id}{" "}
        <span className={`wp-badge ${wp >= 0.5 ? "radiant" : "dire"}`}>
          финальная WP Radiant {(wp * 100).toFixed(0)}%
        </span>
      </h1>
      <div className="narrative">{a.narrative}</div>

      <h2>
        Win Probability
        <span
          className="beta-badge"
          title="Модель WP в бета-версии: обучена на высокоранговых матчах текущего патча, Brier на про-эталоне ~0.15. Оценки уточняются по мере роста датасета."
        >
          beta
        </span>
      </h2>
      {timeline.data && <WpChart points={timeline.data.points} />}

      <h2>Игроки</h2>
      <div className="panel">
        <PlayersTable players={a.players} />
      </div>

      {deaths.length > 0 && (
        <>
          <h2>Карта смертей</h2>
          <div className="panel map-panel">
            <DeathMap deaths={deaths} />
            <div className="map-legend">
              <span className="dot radiant" /> Radiant
              <span className="dot dire" /> Dire
              <span className="muted">
                точка — смерть-ошибка; наведите для деталей
              </span>
            </div>
          </div>
        </>
      )}

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
              {e.safety_index >= (e.si_model ? 0.3 : 0.6) && (
                <span
                  className="si-badge"
                  title={e.si_model
                    ? "Death-Risk модель: калиброванная вероятность смерти в ближайшие 30 секунд в этой позиции"
                    : "Эвристический Safety Index (давление врагов + глубина захода)"}
                >
                  риск {e.safety_index.toFixed(2)}
                </span>
              )}
              {e.top_contributions && e.top_contributions.length > 0 && (
                <ErrorDrivers drivers={e.top_contributions} />
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
