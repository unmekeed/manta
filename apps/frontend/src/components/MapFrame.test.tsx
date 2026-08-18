// Тесты подложки карты (спринт 139).
//
// Проверяется то, что нельзя увидеть на скриншоте: поведение при сбое
// загрузки картинки и совпадение геометрии подложки с квадратом карты.

import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import MapFrame from "./MapFrame";
import { underlayRect } from "../map/coords";

function svg(ui: React.ReactNode) {
  const { container } = render(<svg viewBox="0 0 100 100">{ui}</svg>);
  return container.querySelector("svg")!;
}

describe("подложка", () => {
  it("картинка растянута и сдвинута ровно по расчёту coords", () => {
    // Геометрия не должна дублироваться в компоненте: посчитанная
    // вторым способом, она разойдётся с первым при первой же правке
    // калибровки.
    const el = svg(<MapFrame S={100} fraction={0.85} />).querySelector("image")!;
    const r = underlayRect(100, 0.85);
    expect(Number(el.getAttribute("width"))).toBeCloseTo(r.size);
    expect(Number(el.getAttribute("x"))).toBeCloseTo(r.x);
    expect(Number(el.getAttribute("y"))).toBeCloseTo(r.y);
  });

  it("подложка обрезается квадратом карты", () => {
    // Файл шире проходимой карты: по краям сшитого скриншота нарисованы
    // заграничные скалы. Без обрезки они вылезут за рамку, и метки будут
    // читаться относительно чужого края.
    const s = svg(<MapFrame S={100} />);
    const clip = s.querySelector("clipPath")!;
    const rect = clip.querySelector("rect")!;
    expect(rect.getAttribute("width")).toBe("100");
    expect(rect.getAttribute("height")).toBe("100");
    expect(s.querySelector("image")!.getAttribute("clip-path"))
      .toBe(`url(#${clip.id})`);
  });

  it("у двух карт на странице разные id обрезки", () => {
    // Одинаковые id в одном документе молча склеиваются в первый. Пока
    // геометрия совпадает, это незаметно; разойдётся — заметить будет
    // нечем.
    const { container } = render(
      <div>
        <svg><MapFrame S={100} /></svg>
        <svg><MapFrame S={100} /></svg>
      </div>,
    );
    const ids = Array.from(container.querySelectorAll("clipPath")).map((c) => c.id);
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(2);
  });
});

describe("резерв", () => {
  it("схема рисуется, только если картинка не загрузилась", () => {
    // Пока картинка на месте, схематичная река не рисуется: она идёт
    // ровно по диагонали, а настоящая петляет, и две реки на одной
    // карте противоречили бы друг другу.
    const ok = svg(<MapFrame S={100} />);
    expect(ok.querySelector(".river")).toBeNull();
    expect(ok.querySelector("image")).not.toBeNull();

    const el = svg(<MapFrame S={100} />);
    // fireEvent, а не dispatchEvent: обновление состояния надо прогнать
    // через act, иначе тест увидит дорисовку ДО перерисовки и решит, что
    // резерва нет.
    fireEvent.error(el.querySelector("image")!);
    expect(el.querySelector("image")).toBeNull();
    expect(el.querySelector(".river")).not.toBeNull();
    expect(el.querySelectorAll(".lane")).toHaveLength(4);
  });
});

describe("режим калибровки", () => {
  it("выключен по умолчанию", () => {
    expect(svg(<MapFrame S={100} />).querySelector(".calibration")).toBeNull();
  });

  it("сетка совпадает с сеткой агрегатов", () => {
    // Сетка калибровки обязана быть ТОЙ ЖЕ, по которой считаются
    // агрегаты: чужая сетка показывала бы совмещение с тем, чего на
    // карте нет.
    const el = svg(<MapFrame S={100} calibrate grid={8} />);
    const g = el.querySelectorAll(".cal-grid");
    // По (grid−1) линии на ось: край карты — это рамка, а не линия сетки.
    expect(g).toHaveLength(2 * (8 - 1));
  });

  it("углы подписаны — иначе зеркальную карту не поймать", () => {
    // Перевёрнутая по Y карта выглядит совершенно правдоподобно.
    // Единственное, что её выдаёт, — якорь: юго-запад это Radiant.
    const el = svg(<MapFrame S={100} calibrate />);
    const labels = Array.from(el.querySelectorAll(".cal-label"))
      .map((t) => t.textContent);
    expect(labels.join(" ")).toContain("Radiant");
    expect(labels.join(" ")).toContain("Dire");
    const r = el.querySelector(".cal-label.r")!;
    // Radiant — внизу картинки: SVG рисует сверху вниз, юго-запад снизу.
    expect(Number(r.getAttribute("y"))).toBeGreaterThan(50);
  });
});
