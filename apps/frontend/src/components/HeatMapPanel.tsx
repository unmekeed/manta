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
import {
  frameCells,
  frameMax,
  minuteLabel,
  WHOLE_GAME,
} from "../map/minutes";
import type { HeatmapKind, Heatmaps, HeatmapPhase } from "../lib/api";

const PHASES: { key: HeatmapPhase; label: string }[] = [
  { key: "early", label: "Эрлигейм" },
  { key: "mid", label: "Мид" },
  { key: "late", label: "Лейтгейм" },
];

// blobs — рисовать пятнами вместо клеток. Пятна там, где величина
// НЕПРЕРЫВНА: присутствие и фарм — это «сколько времени провели», и
// плавная форма их и описывает. Смерти, варды, смоки и драки — события,
// каждое случилось в своей точке, и размазывать их значило бы придумать
// плавность там, где её нет: один вард превратился бы в облако.
const KINDS: { key: HeatmapKind; label: string; hint: string; blobs?: boolean }[] = [
  { key: "presence", label: "Присутствие", hint: "где находились герои", blobs: true },
  { key: "farm_core", label: "Фарм коров", blobs: true,
    hint: "позиции 1–3: где у них РОС счётчик добиток" },
  { key: "farm", label: "Фарм (все)", blobs: true,
    hint: "вся команда, включая саппортов" },
  { key: "death", label: "Смерти", hint: "где умирали" },
  { key: "ward", label: "Варды", hint: "куда ставили обзор" },
  { key: "smoke", label: "Смоки", hint: "откуда шли под смоком" },
  { key: "fight", label: "Драки", hint: "где случались важные бои" },
];

export default function HeatMapPanel({ maps }: { maps?: Heatmaps }) {
  const [phase, setPhase] = useState<HeatmapPhase>("early");
  // Минута ползунка. WHOLE_GAME — «вся игра»: с неё и начинаем, потому
  // что общая карта отвечает на вопрос «куда вообще ходили», а минутный
  // кадр — на «в каком порядке», и второй вопрос возникает после первого.
  const [minute, setMinute] = useState<number>(WHOLE_GAME);
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

  // Поминутный слой есть только у матчей, разобранных со спринта 147.
  // У остальных остаются фазы — не как заглушка, а потому что минутной
  // разбивки в их агрегате нет и восстановить её неоткуда.
  const byMinute = maps.by_minute;
  const minuteBlock = byMinute?.kinds?.[kind];
  const useMinutes = Boolean(byMinute?.minutes && minuteBlock);

  const block = maps.phases[phase]?.[kind];
  const available = new Set<string>();
  for (const p of Object.values(maps.phases)) {
    for (const k of Object.keys(p)) available.add(k);
  }
  for (const k of Object.keys(byMinute?.kinds ?? {})) available.add(k);

  // Что рисуем. Обе ветки дают один формат, чтобы HeatMap ничего не знал
  // про минуты: карта рисует клетки, а не времена.
  const radiant: Cell[] = useMinutes
    ? frameCells(minuteBlock!.radiant, minute)
    : ((block?.radiant ?? []) as Cell[]);
  const dire: Cell[] = useMinutes
    ? frameCells(minuteBlock!.dire, minute)
    : ((block?.dire ?? []) as Cell[]);
  const maxN = useMinutes ? frameMax(minuteBlock!, minute) : (block?.max_n ?? 0);
  const hasCells = radiant.length > 0 || dire.length > 0;

  return (
    <section className="heatmaps">
      <h2>Карты</h2>

      {useMinutes ? (
        <div className="heat-time">
          <label className="minute-slider">
            <input
              type="range"
              min={0}
              max={byMinute!.minutes - 1}
              step={1}
              // Ползунок в режиме «вся игра» стоит в нуле, но НЕ считает
              // себя выбравшим нулевую минуту: значение состояния при
              // этом WHOLE_GAME. Иначе, сняв галочку, пользователь
              // получил бы нулевую минуту вместо той, где остановился.
              value={minute === WHOLE_GAME ? 0 : minute}
              disabled={minute === WHOLE_GAME}
              onChange={(e) => setMinute(Number(e.target.value))}
            />
            <code className="minute-now">{minuteLabel(minute)}</code>
          </label>
          <label>
            <input
              type="checkbox"
              checked={minute === WHOLE_GAME}
              onChange={(e) =>
                setMinute(e.target.checked ? WHOLE_GAME : 0)
              }
            />
            Вся игра
          </label>
        </div>
      ) : (
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
      )}

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
          radiant={radiant}
          dire={dire}
          maxN={maxN}
          grid={maps.grid}
          show={sides}
          calibrate={calibrate}
          fraction={fraction}
          blobs={KINDS.find((k) => k.key === kind)?.blobs ?? false}
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
          {hasCells ? (
            <p className="muted">
              {KINDS.find((k) => k.key === kind)?.blobs
                ? "Цвет — плотность: синий редко, красный чаще всего. "
                : "Насыщенность — доля от максимума. "}
              Максимум {maxN} в клетке. Шкала своя у каждого вида:
              присутствие даёт сотни попаданий, смоки — единицы.
              {useMinutes && minute !== WHOLE_GAME
                ? " Шкала кадра — своя: в минуте попаданий единицы, за игру"
                  + " сотни, и общая шкала оставила бы кадры почти пустыми."
                : ""}
            </p>
          ) : (
            <p className="muted">
              {kind === "farm_core" && !useMinutes
                ? "Фарм коров считается со спринта 140; этот матч разобран "
                  + "раньше. Он появится после переразбора — а пока рядом "
                  + "есть «Фарм (все)»."
                : useMinutes
                  ? `На ${minuteLabel(minute)} таких событий не было — `
                    + "это факт матча, а не пробел в данных."
                  : "В этой фазе таких событий не было."}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
