// Панель тепловых карт матча (спринт 111): выбор фазы игры и вида
// события, наложение сторон.
//
// Разделение по фазам — то, ради чего всё затевалось: карта присутствия
// «за весь матч» смешивает стояние на линии с добиванием базы и не
// показывает ничего. Фазы режутся по игровому времени (< 10 мин, 10–25,
// > 25), а не по доле матча — см. миграцию 020.

import { useState } from "react";

import HeatMap, { type Cell } from "./HeatMap";
import { storeFraction, storedFraction } from "../map/coords";
import type { HeatmapKind, Heatmaps, HeatmapPhase } from "../lib/api";

const PHASES: { key: HeatmapPhase; label: string }[] = [
  { key: "early", label: "Эрлигейм" },
  { key: "mid", label: "Мид" },
  { key: "late", label: "Лейтгейм" },
];

const KINDS: { key: HeatmapKind; label: string; hint: string }[] = [
  { key: "presence", label: "Присутствие", hint: "где находились герои" },
  { key: "farm", label: "Фарм", hint: "где росло золото" },
  { key: "death", label: "Смерти", hint: "где умирали" },
  { key: "ward", label: "Варды", hint: "куда ставили обзор" },
  { key: "smoke", label: "Смоки", hint: "откуда шли под смоком" },
  { key: "fight", label: "Драки", hint: "где случались важные бои" },
];

export default function HeatMapPanel({ maps }: { maps?: Heatmaps }) {
  const [phase, setPhase] = useState<HeatmapPhase>("early");
  const [kind, setKind] = useState<HeatmapKind>("presence");
  const [sides, setSides] = useState({ radiant: true, dire: true });
  // Калибровка выключена по умолчанию: она нужна, чтобы ПРОВЕРИТЬ
  // совмещение подложки с метками, а не чтобы смотреть карту каждый день.
  const [calibrate, setCalibrate] = useState(false);
  const [fraction, setFraction] = useState(storedFraction);

  // Матч без координат — это НЕ пустая карта. У JSON-матчей (источник
  // opendota_timeline) позиций нет в принципе, и молча показать пустое
  // поле значило бы соврать: пользователь прочитал бы это как «никто
  // никуда не ходил».
  if (!maps || !maps.phases || Object.keys(maps.phases).length === 0) {
    return (
      <section className="heatmaps">
        <h2>Карты</h2>
        <p className="muted">
          Для этого матча карт нет: они строятся из позиций и событий
          реплея, а матч собран из JSON-таймлайна, где координат нет.
        </p>
      </section>
    );
  }

  const block = maps.phases[phase]?.[kind];
  const available = new Set<string>();
  for (const p of Object.values(maps.phases)) {
    for (const k of Object.keys(p)) available.add(k);
  }

  return (
    <section className="heatmaps">
      <h2>Карты</h2>

      <div className="heat-tabs">
        {PHASES.map((p) => (
          <button
            key={p.key}
            className={p.key === phase ? "on" : ""}
            disabled={!maps.phases[p.key]}
            onClick={() => setPhase(p.key)}
          >
            {p.label}
          </button>
        ))}
      </div>

      <div className="heat-tabs kinds">
        {KINDS.map((k) => (
          <button
            key={k.key}
            className={k.key === kind ? "on" : ""}
            // Вид, которого нет НИ В ОДНОЙ фазе, гасим совсем; тот, что
            // есть в другой фазе, оставляем кликабельным — иначе кнопки
            // прыгали бы при переключении фаз.
            disabled={!available.has(k.key)}
            title={k.hint}
            onClick={() => setKind(k.key)}
          >
            {k.label}
          </button>
        ))}
      </div>

      <div className="heat-body">
        <HeatMap
          radiant={(block?.radiant ?? []) as Cell[]}
          dire={(block?.dire ?? []) as Cell[]}
          maxN={block?.max_n ?? 0}
          grid={maps.grid}
          show={sides}
          calibrate={calibrate}
          fraction={fraction}
        />
        <div className="heat-legend">
          <label>
            <input
              type="checkbox"
              checked={sides.radiant}
              onChange={(e) =>
                setSides((s) => ({ ...s, radiant: e.target.checked }))
              }
            />
            <span className="swatch r" /> Radiant
          </label>
          <label>
            <input
              type="checkbox"
              checked={sides.dire}
              onChange={(e) =>
                setSides((s) => ({ ...s, dire: e.target.checked }))
              }
            />
            <span className="swatch d" /> Dire
          </label>
          <label>
            <input
              type="checkbox"
              checked={calibrate}
              onChange={(e) => setCalibrate(e.target.checked)}
            />
            Калибровка
          </label>
          {calibrate && (
            <div className="calibrate-box">
              <label className="cal-slider">
                Масштаб подложки
                <input
                  type="range"
                  min={0.6}
                  max={1.1}
                  step={0.005}
                  value={fraction}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    setFraction(v);
                    storeFraction(v);
                  }}
                />
                <code>{fraction.toFixed(3)}</code>
              </label>
              <p className="muted">
                Центр подложки измерен по симметрии карты и не двигается;
                неизвестен только масштаб — по самой картинке он не
                определяется, потому что скриншот захватывает и заграничные
                скалы. Сверять надо не сетку, а МЕТКИ: варды у Рошана
                обязаны лечь на яму Рошана, смерти на линии — на линию,
                фарм — в лес, а не на реку. Подобранное значение
                сохраняется в браузере; чтобы оно стало общим для всех,
                пропишите его в PLAYABLE_FRACTION (src/map/coords.ts).
              </p>
            </div>
          )}
          {block ? (
            <p className="muted">
              Насыщенность — доля от максимума ({block.max_n} в клетке).
              Шкала своя у каждого вида: присутствие даёт сотни попаданий,
              смоки — единицы.
            </p>
          ) : (
            <p className="muted">
              В этой фазе таких событий не было.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
