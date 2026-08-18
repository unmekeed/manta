// Слой тепловых пятен поверх подложки карты (спринт 140).
//
// Пиксели рисует canvas, а результат попадает в SVG как <image>. Почему
// не SVG-фигурами: тысяча полупрозрачных кругов с фильтром размытия
// перерисовывается на каждый ховер и тормозит; растр считается один раз
// и дальше просто масштабируется.
//
// Canvas ВНЕ документа: он нужен как холст, а не как элемент страницы.
// Вставленный в DOM, он потребовал бы согласовывать свой размер с
// вёрсткой SVG — то есть держать одну геометрию в двух местах.

import { useMemo } from "react";

import { densityField, fieldToPixels, type Cell } from "../map/heat";

// Сторона растра. 256 на 32 клетки — восемь пикселей на клетку: пятно
// уже гладкое, а вес картинки (dataURL в разметке) ещё умеренный.
const RASTER = 256;

export interface HeatBlobsProps {
  cells: Cell[];
  grid: number;
  maxN: number;
  /** Сторона viewBox карты. */
  S: number;
  className?: string;
}

export default function HeatBlobs({ cells, grid, maxN, S, className }: HeatBlobsProps) {
  const url = useMemo(() => {
    if (!cells.length || maxN <= 0) return null;
    const field = densityField(cells, grid, RASTER, maxN);
    const canvas = document.createElement("canvas");
    canvas.width = RASTER;
    canvas.height = RASTER;
    const ctx = canvas.getContext("2d");
    // Контекста может не быть (jsdom, отключённый canvas). Возвращаем
    // null, а вызывающий рисует клетками — карта остаётся, просто
    // угловатая. Пустой слой вместо неё читался бы как «событий нет».
    if (!ctx) return null;
    const img = ctx.createImageData(RASTER, RASTER);
    img.data.set(fieldToPixels(field));
    ctx.putImageData(img, 0, 0);
    return canvas.toDataURL();
  }, [cells, grid, maxN]);

  if (!url) return null;
  return (
    <image
      href={url}
      x={0}
      y={0}
      width={S}
      height={S}
      className={className}
      preserveAspectRatio="none"
    />
  );
}

/** Можно ли вообще нарисовать пятна в этом окружении. */
export function blobsSupported(): boolean {
  try {
    return !!document.createElement("canvas").getContext("2d");
  } catch {
    return false;
  }
}
