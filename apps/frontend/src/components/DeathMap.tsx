// Мини-карта смертей-ошибок (C6): стилизованная схема карты Dota (без
// ассетов Valve) — половины Radiant/Dire, диагональная река, точки смертей.
// Координаты приходят из отчёта в долях карты, (0,0) — юго-запад; SVG
// рисует сверху вниз, поэтому y инвертируется.

export interface DeathPoint {
  x: number;
  y: number;
  team: "radiant" | "dire";
  label: string;
}

const S = 100; // сторона viewBox

export default function DeathMap({ deaths }: { deaths: DeathPoint[] }) {
  return (
    <svg
      className="death-map"
      viewBox={`0 0 ${S} ${S}`}
      role="img"
      aria-label="Карта смертей"
    >
      {/* половины карты: юго-запад — Radiant, северо-восток — Dire */}
      <polygon points={`0,${S} ${S},${S} 0,0`} className="half radiant" />
      <polygon points={`${S},0 ${S},${S} 0,0`} className="half dire" />
      {/* река по диагонали северо-запад → юго-восток */}
      <line x1={0} y1={0} x2={S} y2={S} className="river" />
      {/* линии для ориентира */}
      <line x1={4} y1={96} x2={96} y2={96} className="lane" />
      <line x1={4} y1={96} x2={4} y2={4} className="lane" />
      <line x1={4} y1={4} x2={96} y2={4} className="lane" />
      <line x1={96} y1={96} x2={96} y2={4} className="lane" />

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
