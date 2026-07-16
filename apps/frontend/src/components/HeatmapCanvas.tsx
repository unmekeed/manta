// Тепловая карта позиций (Гл. 8: HeatmapCanvas) поверх стилизованной
// миникарты. Данные — разреженные сетки плотности по игрокам
// (GET /matches/{id}/heatmap): [gx, gy, count], (0,0) — юго-запад
// (база Radiant), ось y вверх; канва инвертирует y.
//
// Карта рисуется схематично (без игровых ассетов): диагональ реки
// разделяет половины Radiant (юго-запад) и Dire (северо-восток).

import { useEffect, useMemo, useRef, useState } from "react";

import { heroLabel, type Heatmap } from "../lib/api";

const SIZE = 480; // px, канва квадратная
const RADIANT = [107, 208, 107] as const;
const DIRE = [224, 90, 90] as const;

type Filter = "all" | "radiant" | "dire" | number;

function accumulate(hm: Heatmap, filter: Filter): Map<string, { r: number; d: number }> {
  const acc = new Map<string, { r: number; d: number }>();
  for (const p of hm.players) {
    const isRadiant = p.team === 2 || (p.team !== 3 && p.player_id < 5);
    if (filter === "radiant" && !isRadiant) continue;
    if (filter === "dire" && isRadiant) continue;
    if (typeof filter === "number" && p.player_id !== filter) continue;
    for (const [gx, gy, n] of p.cells) {
      const key = `${gx},${gy}`;
      const cell = acc.get(key) ?? { r: 0, d: 0 };
      if (isRadiant) cell.r += n;
      else cell.d += n;
      acc.set(key, cell);
    }
  }
  return acc;
}

function drawMap(ctx: CanvasRenderingContext2D) {
  ctx.fillStyle = "#101613";
  ctx.fillRect(0, 0, SIZE, SIZE);
  // Река: разделяет половины Radiant (юго-запад) и Dire (северо-восток).
  // В мировых координатах это анти-диагональ (x + y ≈ 0); на канве с
  // инвертированным y — линия из левого верхнего в правый нижний угол.
  ctx.strokeStyle = "rgba(90, 140, 190, 0.35)";
  ctx.lineWidth = SIZE * 0.06;
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(SIZE, SIZE);
  ctx.stroke();
  // Базы.
  ctx.fillStyle = "rgba(107, 208, 107, 0.12)";
  ctx.fillRect(0, SIZE * 0.82, SIZE * 0.18, SIZE * 0.18);
  ctx.fillStyle = "rgba(224, 90, 90, 0.12)";
  ctx.fillRect(SIZE * 0.82, 0, SIZE * 0.18, SIZE * 0.18);
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, SIZE - 1, SIZE - 1);
}

export default function HeatmapCanvas({ heatmap }: { heatmap: Heatmap }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const cells = useMemo(() => accumulate(heatmap, filter), [heatmap, filter]);

  useEffect(() => {
    const ctx = canvasRef.current?.getContext("2d");
    if (!ctx) return;
    drawMap(ctx);

    let max = 0;
    for (const c of cells.values()) max = Math.max(max, c.r + c.d);
    if (max === 0) return;

    const px = SIZE / heatmap.grid;
    for (const [key, c] of cells) {
      const [gx, gy] = key.split(",").map(Number);
      const total = c.r + c.d;
      // sqrt сжимает динамический диапазон: фонтан не «выжигает» карту.
      const alpha = Math.min(1, Math.sqrt(total / max)) * 0.85;
      // Цвет — смесь команд по вкладу в ячейку.
      const w = c.r / total;
      const rgb = [
        Math.round(RADIANT[0] * w + DIRE[0] * (1 - w)),
        Math.round(RADIANT[1] * w + DIRE[1] * (1 - w)),
        Math.round(RADIANT[2] * w + DIRE[2] * (1 - w)),
      ];
      ctx.fillStyle = `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
      // Мировая ось y вверх → канва: инвертируем gy.
      ctx.fillRect(gx * px, (heatmap.grid - 1 - gy) * px, px, px);
    }
  }, [cells, heatmap.grid]);

  return (
    <div className="heatmap">
      <div className="heatmap-controls">
        <button
          className={filter === "all" ? "active" : ""}
          onClick={() => setFilter("all")}
        >
          Все
        </button>
        <button
          className={filter === "radiant" ? "active radiant" : "radiant"}
          onClick={() => setFilter("radiant")}
        >
          Radiant
        </button>
        <button
          className={filter === "dire" ? "active dire" : "dire"}
          onClick={() => setFilter("dire")}
        >
          Dire
        </button>
        <select
          value={typeof filter === "number" ? filter : ""}
          onChange={(e) =>
            setFilter(e.target.value === "" ? "all" : Number(e.target.value))
          }
        >
          <option value="">— игрок —</option>
          {heatmap.players.map((p) => (
            <option key={p.player_id} value={p.player_id}>
              {p.player_name || heroLabel(p.hero) || `Игрок ${p.player_id}`}
            </option>
          ))}
        </select>
      </div>
      <canvas
        ref={canvasRef}
        width={SIZE}
        height={SIZE}
        className="heatmap-canvas"
        aria-label="Тепловая карта позиций"
      />
    </div>
  );
}
