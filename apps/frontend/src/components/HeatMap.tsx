// Тепловая карта матча (спринт 111): сетка 32×32 поверх подложки карты.
//
// Со спринта 139 подложка — настоящая карта 7.41, общая с картой
// смертей, и живёт в MapFrame. Раньше схема была скопирована в оба
// компонента, а границы карты задавались в проекте тремя разными
// числами; под общей подложкой такое расхождение стало бы видно глазом.
//
// Клетки приходят разреженно: [gx, gy, n]. Пустые не рисуются вовсе —
// это и есть «там никого не было», рисовать их прозрачными смысла нет.
//
// Ось Y инвертируется: агрегат считает (0,0) юго-западом (база Radiant),
// а SVG рисует сверху вниз. Переворот делается ЗДЕСЬ и только здесь —
// в coords.ts единичное пространство остаётся с началом на юго-западе.
// Перепутать особенно легко и особенно дорого: карта получится
// правдоподобной, просто зеркальной, и заметить это можно только зная,
// где обычно фармят.

import MapFrame from "./MapFrame";

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
  /** Сетка и оси поверх подложки — чтобы сверить совмещение. */
  calibrate?: boolean;
  /** Масштаб подложки; прокидывается из панели при калибровке. */
  fraction?: number;
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
  calibrate = false,
  fraction,
}: HeatMapProps) {
  return (
    <svg
      className="heat-map map-frame"
      viewBox={`0 0 ${S} ${S}`}
      role="img"
      aria-label="Тепловая карта"
    >
      <MapFrame S={S} calibrate={calibrate} grid={grid} fraction={fraction} />
      {show.radiant && cells(radiant, maxN, grid, "r")}
      {show.dire && cells(dire, maxN, grid, "d")}
    </svg>
  );
}
