import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../lib/api";

export default function MatchList() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["matches"],
    queryFn: api.matches,
  });

  if (isLoading) return <div className="loading">Загрузка матчей…</div>;
  if (error) return <div className="error-msg">Ошибка: {String(error)}</div>;
  if (!data?.length)
    return (
      <div className="error-msg">
        Отчётов пока нет — загрузите реплей через API или дождитесь сборщика.
      </div>
    );

  return (
    <>
      <h1>Разобранные матчи</h1>
      <div className="match-list">
        {data.map((m) => {
          const wp = parseFloat(m.final_radiant_wp);
          const radiantWon = wp >= 0.5;
          return (
            <Link key={m.match_id} to={`/matches/${m.match_id}`} className="match-card">
              <span className="mid">Матч {m.match_id}</span>{" "}
              <span className={`wp-badge ${radiantWon ? "radiant" : "dire"}`}>
                {radiantWon ? "Radiant" : "Dire"} · WP {(wp * 100).toFixed(0)}%
              </span>
              <div className="sub">{m.narrative}</div>
            </Link>
          );
        })}
      </div>
    </>
  );
}
