// Тесты тепловых пятен (спринт 140).
//
// Проверяется то, что на картинке выглядит правдоподобно при любой
// ошибке: положение пятна, сохранение массы при размытии и то, что
// шкала вообще различает плотности. Тепловая карта — визуализация, и
// сдвинутая, зеркальная или выцветшая версия неотличима от настоящей,
// пока не сверишь с игрой.

import { describe, expect, it } from "vitest";

import { densityField, fieldToPixels, heatColor, type Cell } from "./heat";

const OUT = 64;
const GRID = 32;

function brightest(field: Float32Array, out = OUT): [number, number] {
  let best = 0;
  let idx = 0;
  field.forEach((v, i) => {
    if (v > best) {
      best = v;
      idx = i;
    }
  });
  return [idx % out, Math.floor(idx / out)];
}

describe("поле плотности", () => {
  it("переворачивает ось Y: юго-запад агрегата — низ картинки", () => {
    // ГЛАВНЫЙ тест файла. Агрегат считает (0,0) юго-западом, растр
    // адресуется сверху вниз. Перевёрнутая карта выглядит совершенно
    // правдоподобно — заметить её можно только зная, где обычно фармят.
    const sw: Cell[] = [[0, 0, 10]];
    const [x, y] = brightest(densityField(sw, GRID, OUT, 10));
    expect(x).toBeLessThan(OUT / 4);
    expect(y).toBeGreaterThan((OUT * 3) / 4);

    const ne: Cell[] = [[GRID - 1, GRID - 1, 10]];
    const [x2, y2] = brightest(densityField(ne, GRID, OUT, 10));
    expect(x2).toBeGreaterThan((OUT * 3) / 4);
    expect(y2).toBeLessThan(OUT / 4);
  });

  it("пятно приходится на ЦЕНТР клетки, а не на её угол", () => {
    // Иначе всё поле сдвинуто на полклетки — на карте это полтора
    // процента стороны, ровно та ошибка, которую глазом не поймать.
    //
    // Растр здесь КРУПНЫЙ намеренно. При 64 пикселях на 32 клетки
    // полклетки — это один пиксель, и любой разумный допуск проглотит
    // сдвиг; при 256 полклетки уже четыре пикселя, и допуск в один
    // пиксель ловит ошибку. Первая версия теста мерила на мелком растре
    // и мутацию «в угол клетки» не замечала.
    const big = 256;
    const mid = Math.floor(GRID / 2);
    const [x, y] = brightest(densityField([[mid, mid, 5]], GRID, big, 5), big);
    const expected = Math.floor((mid + 0.5) * (big / GRID));
    expect(Math.abs(x - expected)).toBeLessThanOrEqual(1);
    expect(Math.abs(y - (big - 1 - expected))).toBeLessThanOrEqual(1);
  });

  it("одиночная клетка с n = maxN даёт пик РОВНО 1", () => {
    // Ровно та ошибка, из-за которой первая версия рисовала пустую
    // карту: нормированное ядро сохраняет массу, размазывая её по сотням
    // пикселей, и пик выходил в тысячных долях — то есть в самом
    // холодном конце шкалы. Данные были, видно их не было.
    const f = densityField([[10, 10, 8]], GRID, OUT, 8);
    expect(Math.max(...f)).toBeCloseTo(1, 2);
  });

  it("sigma меняет ШИРИНУ пятна, но не его яркость", () => {
    // Два утверждения в одном тесте намеренно: они про одно свойство.
    // Если sigma перестанет влиять на ширину, размытие превратится в
    // украшение с зашитым радиусом; если начнёт влиять на яркость —
    // подкрутка резкости будет молча менять шкалу, и две карты одного
    // матча выйдут разной яркости без причины.
    const width = (sigma: number) => {
      const f = densityField([[16, 16, 8]], GRID, OUT, 8, sigma);
      return f.reduce((acc, v) => acc + (v > 0.25 ? 1 : 0), 0);
    };
    expect(width(1.5)).toBeGreaterThan(width(0.6) * 2);
    for (const sigma of [0.6, 0.9, 1.5]) {
      const f = densityField([[16, 16, 8]], GRID, OUT, 8, sigma);
      expect(Math.max(...f)).toBeCloseTo(1, 2);
    }
  });

  it("скопление клеток горячее одиночной, а не такой же", () => {
    const lone = densityField([[10, 10, 8]], GRID, OUT, 8);
    const cluster = densityField(
      [[10, 10, 8], [10, 11, 8], [11, 10, 8], [11, 11, 8]], GRID, OUT, 8);
    expect(Math.max(...cluster)).toBeGreaterThan(Math.max(...lone) * 1.5);
  });

  it("слабая клетка не разгорается до максимума шкалы", () => {
    // ГЛАВНАЯ проверка того, что нормировка идёт от ЯДРА, а не от
    // максимума самого поля. При нормировке по себе одинокая клетка с
    // n = 1 при maxN = 100 засветилась бы так же ярко, как забитая
    // клетка с n = 100, — и редкая фаза выглядела бы плотной.
    //
    // Проверка через max поля, а не через max по всей карте: у обеих
    // карт по одной клетке, и разниться они обязаны именно яркостью.
    const strong = densityField([[10, 10, 100]], GRID, OUT, 100);
    const weak = densityField([[10, 10, 25]], GRID, OUT, 100);
    expect(Math.max(...strong)).toBeCloseTo(1, 2);
    expect(Math.max(...weak)).toBeCloseTo(0.25, 2);
  });

  it("пятно круглое, а не вытянутое по одной оси", () => {
    // Размытие разделяемое: сначала по строкам, потом по столбцам.
    // Пропусти один из проходов — и пятно станет полосой. Пик при этом
    // не изменится, ширина по одной оси тоже, так что ни одна проверка
    // выше такого не заметит: полоса выглядит как обычная тепловая
    // метка, просто «так легли данные».
    const mid = Math.floor(GRID / 2);
    const f = densityField([[mid, mid, 8]], GRID, OUT, 8);
    const [cx, cy] = brightest(f);
    for (const d of [2, 4, 6]) {
      const alongX = f[cy * OUT + (cx + d)];
      const alongY = f[(cy + d) * OUT + cx];
      expect(alongX).toBeGreaterThan(0);
      expect(alongY).toBeCloseTo(alongX, 3);
    }
  });

  it("две далёкие точки дают два пятна, а не одно", () => {
    const f = densityField([[2, 2, 5], [29, 29, 5]], GRID, OUT, 5);
    const centre = f[Math.floor(OUT / 2) * OUT + Math.floor(OUT / 2)];
    expect(centre).toBeLessThan(0.001);
  });

  it("клетки вне сетки отбрасываются, а не заворачиваются", () => {
    // Отрицательный или запредельный индекс, посчитанный по модулю,
    // нарисовал бы пятно на противоположном краю карты.
    const f = densityField([[-3, 5, 9], [GRID + 4, 5, 9]], GRID, OUT, 9);
    expect(f.reduce((a, b) => a + b, 0)).toBe(0);
  });

  it("пустой вход и нулевой максимум дают пустое поле, а не деление на ноль", () => {
    expect(densityField([], GRID, OUT, 10).every((v) => v === 0)).toBe(true);
    expect(densityField([[1, 1, 5]], GRID, OUT, 0).every((v) => v === 0)).toBe(true);
  });
});

describe("шкала", () => {
  it("холодный низ, горячий верх", () => {
    const [r0, , b0] = heatColor(0);
    const [r1, , b1] = heatColor(1);
    expect(b0).toBeGreaterThan(r0);
    expect(r1).toBeGreaterThan(b1);
  });

  it("прозрачность растёт вместе с плотностью", () => {
    // Постоянная альфа закрасила бы подложку ровным ковром, включая
    // места, где герой прошёл однажды, и карта перестала бы отвечать на
    // вопрос «где он был ЧАЩЕ».
    expect(heatColor(0)[3]).toBeLessThan(heatColor(0.2)[3]);
    expect(heatColor(0.2)[3]).toBeLessThan(heatColor(0.5)[3]);
  });

  it("значения за пределами 0..1 не ломают цвет", () => {
    for (const v of [-5, 2, NaN]) {
      const c = heatColor(v);
      for (const ch of c.slice(0, 3)) {
        expect(ch).toBeGreaterThanOrEqual(0);
        expect(ch).toBeLessThanOrEqual(255);
      }
    }
  });

  it("шкала различает плотности, а не красит всё одним", () => {
    const seen = new Set(
      [0.1, 0.3, 0.5, 0.7, 0.9].map((v) => heatColor(v).slice(0, 3).join(",")),
    );
    expect(seen.size).toBe(5);
  });
});

describe("пиксели", () => {
  it("на каждое значение поля приходится ровно четыре байта", () => {
    const f = densityField([[5, 5, 3]], GRID, OUT, 3);
    expect(fieldToPixels(f)).toHaveLength(f.length * 4);
  });

  it("пустое поле полностью прозрачно", () => {
    // Иначе карта без событий закрасила бы подложку синим ковром и
    // читалась бы как «здесь везде понемногу были».
    const px = fieldToPixels(new Float32Array(16));
    for (let i = 3; i < px.length; i += 4) expect(px[i]).toBe(0);
  });
});
