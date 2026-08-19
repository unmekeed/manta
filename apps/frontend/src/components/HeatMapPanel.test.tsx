// Панель тепловых карт: ползунок времени (спринт 148).
//
// Чистая выборка кадра проверена в src/map/minutes.test.ts. Здесь —
// только то, что видно на месте: какой орган управления показан, что
// происходит с фазами у старых матчей и не превращается ли «вся игра» в
// нулевую минуту при снятии галочки.
//
// ЗАЧЕМ ЭТО ОТДЕЛЬНО. Функция может быть безупречной и не вызываться —
// самая дорогая дыра нынешней серии спринтов. Ползунок, не подключённый
// к выборке, выглядит работающим: он двигается.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import HeatMapPanel from "./HeatMapPanel";
import type { Heatmaps } from "../lib/api";

const PHASES_ONLY: Heatmaps = {
  grid: 32,
  phases: {
    early: { presence: { max_n: 3, radiant: [[1, 1, 3]], dire: [] } },
    mid: { presence: { max_n: 2, radiant: [[9, 9, 2]], dire: [] } },
  },
};

const WITH_MINUTES: Heatmaps = {
  ...PHASES_ONLY,
  by_minute: {
    minutes: 4,
    kinds: {
      presence: {
        max_n: 5,
        max_by_minute: [3, 0, 2, 1],
        radiant: [
          [0, 1, 1, 3],
          [2, 1, 1, 2],
          [3, 9, 9, 1],
        ],
        dire: [],
      },
    },
  },
};

describe("матч без поминутного слоя", () => {
  it("показывает фазы, а не ползунок", () => {
    // Матчи до спринта 147 минутной разбивки не имеют, и восстановить её
    // неоткуда: фазовый агрегат её уже не содержит. Ползунок для них был
    // бы обещанием, которое нечем выполнить.
    render(<HeatMapPanel maps={PHASES_ONLY} />);
    expect(screen.getByRole("button", { name: "Эрлигейм" })).toBeTruthy();
    expect(screen.queryByRole("slider")).toBeNull();
  });
});

describe("матч с поминутным слоем", () => {
  it("показывает ползунок вместо вкладок фаз", () => {
    render(<HeatMapPanel maps={WITH_MINUTES} />);
    expect(screen.queryByRole("button", { name: "Эрлигейм" })).toBeNull();
    expect(screen.getByRole("slider")).toBeTruthy();
  });

  it("открывается на всей игре", () => {
    // Общая карта отвечает на «куда вообще ходили», минутный кадр — на «в
    // каком порядке». Второй вопрос возникает после первого.
    render(<HeatMapPanel maps={WITH_MINUTES} />);
    expect(screen.getByText("вся игра")).toBeTruthy();
    expect((screen.getByRole("slider") as HTMLInputElement).disabled).toBe(true);
  });

  it("снятая галочка отдаёт ползунок и показывает минуту", () => {
    render(<HeatMapPanel maps={WITH_MINUTES} />);
    fireEvent.click(screen.getByLabelText("Вся игра"));
    expect((screen.getByRole("slider") as HTMLInputElement).disabled).toBe(false);
    expect(screen.getByText("0:00")).toBeTruthy();
  });

  it("ползунок доходит до последней минуты, но не дальше", () => {
    // max === minutes - 1. Единица разницы не видна на глаз, а стоит
    // либо потерянной последней минуты, либо пустого кадра в конце,
    // неотличимого от «там ничего не было».
    render(<HeatMapPanel maps={WITH_MINUTES} />);
    expect((screen.getByRole("slider") as HTMLInputElement).max).toBe("3");
  });

  it("ползунок меняет показанную минуту", () => {
    render(<HeatMapPanel maps={WITH_MINUTES} />);
    fireEvent.click(screen.getByLabelText("Вся игра"));
    fireEvent.change(screen.getByRole("slider"), { target: { value: "3" } });
    expect(screen.getByText("3:00")).toBeTruthy();
  });

  it("пустая минута названа фактом матча, а не пробелом в данных", () => {
    // Минута 1 пуста по построению (max_by_minute[1] === 0). Молчаливое
    // пустое поле читается как «сломалось»; спринты 92 и 99 этому уже
    // научили.
    render(<HeatMapPanel maps={WITH_MINUTES} />);
    fireEvent.click(screen.getByLabelText("Вся игра"));
    fireEvent.change(screen.getByRole("slider"), { target: { value: "1" } });
    expect(screen.getByText(/это факт матча/)).toBeTruthy();
  });
});
