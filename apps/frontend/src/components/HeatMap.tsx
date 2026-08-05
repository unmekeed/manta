// Тепловая карта матча (спринт 111): сетка 32×32 поверх той же
// стилизованной схемы карты, что у DeathMap — половины Radiant/Dire и
// река по диагонали, без ассетов Valve.
//
// Клетки приходят разреженно: [gx, gy, n]. Пустые не рисуются вовсе —
// это и есть «там никого не было», рисовать их прозрачными смысла нет.
//
// Ось Y инвертируется, как в DeathMap: агрегат считает (0,0) юго-западом
// (база Radiant), а SVG рисует сверху вниз. Перепутать здесь особенно
// легко и особенно дорого — карта получится правдоподобной, просто
// зеркальной, и заметить это можно только зная, где обычно фармят.

export type Cell = [number, number, number]; // gx, gy, n

const S = 100; // сторона viewBox

// Минимальная непрозрачность непустой клетки. Клетка с одним попаданием
// при максимуме в 400 дала бы alpha 0.0025 — то есть невидимый, но
// существующий факт. Для карт смоков и вардов это как раз важные
// единичные события.
const MIN_ALPHA = 0.12;

export interface HeatMapProps {
  radiant: Cell[];
  dire: Cell[];
  maxN: number;
  grid: number;
  /** Какие стороны показывать; обе — наложением. */
  show: { radiant: boolean; dire: boolean };
}

function cells(list: Cell[], maxN: number, grid: number, cls: string) {
  const step = S / grid;
  return list.map(([gx, gy, n]) => {
    const a = MIN_ALPHA + (1 - MIN_ALPHA) * (maxN > 0 ? n / maxN : 0);
    return (
      <rect
        key={`${cls}-${gx}-${gy}`}
        x={gx * step}
        y={(grid - 1 - gy) * step}
        width={step}
        height={step}
        className={`heat-cell ${cls}`}
        opacity={Math.min(a, 1)}
      >
        <title>{`${n}`}</title>
      </rect>
    );
  });
}

export default function HeatMap({
  radiant,
  dire,
  maxN,
  grid,
  show,
}: HeatMapProps) {
  return (
    <svg
      className="heat-map map-frame"
      viewBox={`0 0 ${S} ${S}`}
      role="img"
      aria-label="Тепловая карта"
    >
      <polygon points={`0,${S} ${S},${S} 0,0`} className="half radiant" />
      <polygon points={`${S},0 ${S},${S} 0,0`} className="half dire" />
      <line x1={0} y1={0} x2={S} y2={S} className="river" />
      <line x1={4} y1={96} x2={96} y2={96} className="lane" />
      <line x1={4} y1={96} x2={4} y2={4} className="lane" />
      <line x1={4} y1={4} x2={96} y2={4} className="lane" />
      <line x1={96} y1={96} x2={96} y2={4} className="lane" />

      {show.radiant && cells(radiant, maxN, grid, "r")}
      {show.dire && cells(dire, maxN, grid, "d")}
    </svg>
  );
}
