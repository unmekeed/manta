// Общая подложка карты для всех карт матча (спринт 139).
//
// ЗАЧЕМ ОТДЕЛЬНЫЙ КОМПОНЕНТ. До этого спринта тепловая карта и карта
// смертей рисовали схему КАЖДАЯ СВОЮ — одинаковыми, но скопированными
// строками. Разъехавшиеся река и линии на двух картах одного матча
// читались бы как две разные карты.
//
// ЧТО ВНУТРИ. Настоящая карта 7.41 картинкой. Прежняя стилизованная
// схема осталась, но перешла в РЕЗЕРВ: она рисуется, только если
// картинка не загрузилась. Рисовать её поверх фотографии нельзя —
// схематичная река идёт ровно по диагонали, а настоящая петляет, и две
// реки на одной карте противоречили бы друг другу.
//
// РЕЖИМ КАЛИБРОВКИ. Совмещение подложки с метками нельзя объявить — его
// можно только увидеть. Масштаб картинки по ней самой не измеряется (см.
// coords.ts), поэтому здесь есть режим, рисующий поверх сетку агрегатов,
// оси и рамку проходимой зоны. Смотреть надо не на сетку, а на то,
// попадают ли РЕАЛЬНЫЕ метки туда, где им место на картинке: варды у
// Рошана — на яму Рошана, смерти на линии — на линию.

import { useId, useState } from "react";

import mapImage from "../assets/dota-map-7.41.webp";
import { storedFraction, underlayRect } from "../map/coords";

export interface MapFrameProps {
  /** Сторона viewBox: подложка считается от неё, а не от зашитой сотни. */
  S: number;
  /** Показать сетку калибровки поверх подложки. */
  calibrate?: boolean;
  /** Шаг сетки калибровки в клетках — тот же, что у агрегатов. */
  grid?: number;
  /** Доля стороны файла, занятая проходимой картой; по умолчанию — из
   *  сохранённой калибровки, чтобы все карты страницы совпадали. */
  fraction?: number;
}

export default function MapFrame({
  S,
  calibrate = false,
  grid = 32,
  fraction,
}: MapFrameProps) {
  // Уникальный id на каждый экземпляр: на странице матча карт несколько,
  // а одинаковые id в одном документе молча склеились бы в первый.
  const clipId = `map-clip-${useId()}`;
  const [failed, setFailed] = useState(false);
  const u = underlayRect(S, fraction ?? storedFraction());

  return (
    <>
      {failed ? (
        // Резерв: та самая схема, что была до спринта 139.
        <g className="map-fallback">
          <polygon points={`0,${S} ${S},${S} 0,0`} className="half radiant" />
          <polygon points={`${S},0 ${S},${S} 0,0`} className="half dire" />
          <line x1={0} y1={0} x2={S} y2={S} className="river" />
          <line x1={4} y1={S - 4} x2={S - 4} y2={S - 4} className="lane" />
          <line x1={4} y1={S - 4} x2={4} y2={4} className="lane" />
          <line x1={4} y1={4} x2={S - 4} y2={4} className="lane" />
          <line x1={S - 4} y1={S - 4} x2={S - 4} y2={4} className="lane" />
        </g>
      ) : (
        <>
          <defs>
            <clipPath id={clipId}>
              <rect x={0} y={0} width={S} height={S} />
            </clipPath>
          </defs>
          <image
            href={mapImage}
            x={u.x}
            y={u.y}
            width={u.size}
            height={u.size}
            // Единичный квадрат — это ПРОХОДИМАЯ карта, а файл шире неё:
            // по краям сшитого скриншота нарисованы заграничные скалы.
            // Всё, что выходит за квадрат, обрезается, иначе метки
            // читались бы относительно чужого края.
            clipPath={`url(#${clipId})`}
            className="map-underlay"
            preserveAspectRatio="none"
            onError={() => setFailed(true)}
          />
        </>
      )}

      {calibrate && (
        <g className="calibration">
          {Array.from({ length: grid - 1 }, (_, i) => {
            const p = ((i + 1) * S) / grid;
            const major = (i + 1) % 8 === 0;
            const cls = `cal-grid${major ? " major" : ""}`;
            return (
              <g key={i}>
                <line x1={p} y1={0} x2={p} y2={S} className={cls} />
                <line x1={0} y1={p} x2={S} y2={p} className={cls} />
              </g>
            );
          })}
          <rect x={0} y={0} width={S} height={S} className="cal-bounds" />
          <line x1={S / 2} y1={0} x2={S / 2} y2={S} className="cal-axis" />
          <line x1={0} y1={S / 2} x2={S} y2={S / 2} className="cal-axis" />
          {/* Подписи углов ловят переворот оси Y. Зеркальная карта
              выглядит совершенно правдоподобно, и без якоря заметить её
              можно только зная, где обычно фармят. */}
          <text x={3} y={S - 3} className="cal-label r">ЮЗ · Radiant</text>
          <text x={S - 3} y={8} className="cal-label d" textAnchor="end">
            СВ · Dire
          </text>
        </g>
      )}
    </>
  );
}
