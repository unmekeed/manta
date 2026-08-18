// Тесты координат карты на фронте (спринт 139).
//
// Числа здесь — копия libs/dota_map.py, и копия опасна: разъехавшись,
// она даст не падение, а КРИВОЕ НАЛОЖЕНИЕ — агрегаты посчитаны по одной
// сетке, нарисованы по другой. Совпадение с питоном стережёт отдельно
// scripts/tests/test_map_coords_agree.py; здесь проверяется поведение.

import { beforeEach, describe, expect, it } from "vitest";

import {
  CELL_MAX,
  CELL_MIN,
  PLAYABLE_FRACTION,
  UNITS_PER_CELL,
  WORLD_HALF,
  cellToUnit,
  storeFraction,
  storedFraction,
  underlayRect,
  worldToUnit,
} from "./coords";

describe("два пространства сходятся в одну точку", () => {
  it("вард в клетке 64 и герой в мировой −8192 стоят в одном углу", () => {
    // ГЛАВНОЕ свойство модуля. Позиции приходят из парсера в мировых
    // единицах, варды — из OpenDota в клетках. На общей подложке они
    // обязаны получить одну координату, иначе тепловая карта и варды
    // нарисуются со сдвигом, и подложка сделает сдвиг видимым.
    for (const cell of [CELL_MIN, 96, 128, 160, CELL_MAX]) {
      const world = (cell - (CELL_MIN + CELL_MAX) / 2) * UNITS_PER_CELL;
      const [uc, vc] = cellToUnit(cell, cell);
      const [uw, vw] = worldToUnit(world, world);
      expect(uc).toBeCloseTo(uw, 9);
      expect(vc).toBeCloseTo(vw, 9);
    }
  });

  it("шаг клетки выводится из границ, а не записан отдельно", () => {
    // Записанный отдельно, он пережил бы правку WORLD_HALF и увёл бы две
    // системы врозь молча — ровно так расхождение и завелось.
    expect(UNITS_PER_CELL).toBeCloseTo((2 * WORLD_HALF) / (CELL_MAX - CELL_MIN));
  });
});

describe("единичное пространство", () => {
  it("(0,0) — юго-запад, база Radiant", () => {
    // Переворот оси Y делается ОДИН раз, в компоненте отрисовки. Если он
    // случится ещё и здесь, карта выйдет правдоподобной, но зеркальной,
    // и заметить это можно только зная, где обычно фармят.
    expect(worldToUnit(-WORLD_HALF, -WORLD_HALF)).toEqual([0, 0]);
    expect(worldToUnit(WORLD_HALF, WORLD_HALF)).toEqual([1, 1]);
    expect(worldToUnit(0, 0)).toEqual([0.5, 0.5]);
  });
});

describe("подложка", () => {
  it("картинка шире квадрата и центрирована на нём", () => {
    // Игровая часть занимает долю файла, значит файл растягивается и
    // сдвигается так, чтобы ЦЕНТРЫ совпали. Центр измерен по симметрии
    // карты — если сдвиг перестанет быть симметричным, измерение
    // пропадёт впустую.
    const r = underlayRect(100, 0.8);
    expect(r.size).toBeCloseTo(125);
    expect(r.x).toBeCloseTo(-12.5);
    expect(r.y).toBeCloseTo(r.x);
    expect(r.x + r.size / 2).toBeCloseTo(50);
  });

  it("доля 1.0 — картинка ровно по квадрату, без сдвига", () => {
    const r = underlayRect(100, 1);
    expect(r.size).toBe(100);
    // toBeCloseTo, а не toEqual: −0 и 0 в JS не равны по toEqual, а
    // рисуются одинаково. Тест обязан ловить сдвиг, а не знак нуля.
    expect(r.x).toBeCloseTo(0);
    expect(r.y).toBeCloseTo(0);
  });
});

describe("сохранённая калибровка", () => {
  beforeEach(() => localStorage.clear());

  it("подобранное значение переживает перезагрузку", () => {
    storeFraction(0.87);
    expect(storedFraction()).toBeCloseTo(0.87);
  });

  it("без сохранённого берётся значение из кода", () => {
    expect(storedFraction()).toBe(PLAYABLE_FRACTION);
  });

  it("мусор и запредельные значения не применяются", () => {
    // Ноль обратил бы масштаб в бесконечность и карта исчезла бы без
    // следа — чинить пришлось бы через консоль браузера, догадавшись,
    // что дело в localStorage.
    for (const bad of ["0", "нет", "-1", "5", ""]) {
      localStorage.setItem("manta.map.playableFraction", bad);
      expect(storedFraction()).toBe(PLAYABLE_FRACTION);
    }
  });
});
