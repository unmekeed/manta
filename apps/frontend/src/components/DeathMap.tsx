// Мини-карта смертей-ошибок (C6): точки смертей поверх подложки карты.
//
// Со спринта 139 подложка — настоящая карта 7.41, общая с тепловыми
// картами (MapFrame). Раньше схема была скопирована в оба компонента.
//
// Координаты приходят из отчёта в долях карты, (0,0) — юго-запад; SVG
// рисует сверху вниз, поэтому y инвертируется здесь, при отрисовке.

import MapFrame from "./MapFrame";

export interface DeathPoint {
  x: number;
  y: number;
  team: "radiant" | "dire";
  label: string;
}

const S = 100; // сторона viewBox

export default function DeathMap({
  deaths,
  calibrate = false,
}: {
  deaths: DeathPoint[];
  calibrate?: boolean;
}) {
  return (
    <svg
      className="death-map map-frame"
      viewBox={`0 0 ${S} ${S}`}
      role="img"
      aria-label="Карта смертей"
    >
      <MapFrame S={S} calibrate={calibrate} />

      {deaths.map((d, i) => (
        <circle
          key={i}
          cx={d.x * S}
          cy={(1 - d.y) * S}
          r={2.6}
          className={`death ${d.team}`}
        >
          <title>{d.label}</title>
        </circle>
      ))}
    </svg>
  );
}
