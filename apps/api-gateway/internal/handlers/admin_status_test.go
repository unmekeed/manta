package handlers

import (
	"encoding/json"
	"testing"
)

// Страница состояния сбора (спринт 198).
//
// ГЛАВНОЕ, ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ, — не арифметика, а РАЗЛИЧИЕ между
// «источник ничего не собрал» и «источника нет». Оно и есть причина, по
// которой страница вообще делается.
//
// Живой случай 06–07.09.2026: контейнер `stratz-collector` стоял `Up 30
// hours`, каждый цикл получал 403 и не собирал ничего, а половина потока
// была за ним закреплена. Ни одно место в системе не показывало этот
// ноль: доктор смотрит на витрину целиком, Telegram шлёт сводку, логи
// надо открыть и знать, что искать. Источник, выпавший из списка,
// выглядит точно так же, как источник, которого никогда не было.
//
// База в этих тестах не нужна и намеренно не используется: решение
// «показывать нулём» вынесено из SQL в `mergeSources` именно затем,
// чтобы его можно было проверить.

func bySource(got []adminSource) map[string]adminSource {
	out := map[string]adminSource{}
	for _, s := range got {
		out[s.Source] = s
	}
	return out
}

func TestASilentSourceIsShownAsZeroNotOmitted(t *testing.T) {
	// Ровно расклад 07.09: opendota собирает, stratz жжёт вызовы впустую.
	got := mergeSources(
		[]string{"opendota_timeline", "stratz_timeline"},
		map[string]collectedRow{"opendota_timeline": {Matches: 41, WithReplay: 0}},
		map[string]int64{"opendota_timeline": 912, "stratz_timeline": 52},
	)
	m := bySource(got)
	s, ok := m["stratz_timeline"]
	if !ok {
		t.Fatal("молчащий источник пропал из списка — «умер» неотличимо " +
			"от «его нет», а это ровно то, что стоило двух суток в сентябре")
	}
	if s.Matches != 0 {
		t.Fatalf("молчащий источник показан с притоком %d", s.Matches)
	}
	// 52 вызова при нуле матчей — это и есть картинка «работает вхолостую».
	// Показать надо ОБА числа: расход без притока читается сразу, а по
	// одному притоку не отличить «не давали работать» от «не справился».
	if s.Calls != 52 {
		t.Fatalf("расход молчащего источника потерян: %d", s.Calls)
	}
}

func TestASourceWithNoCallsAtAllIsStillShown(t *testing.T) {
	// Источник, который не собрал НИЧЕГО и не потратил НИ ОДНОГО вызова:
	// процесс упал, контейнер не поднялся, ключ пуст. Самый тихий из
	// возможных отказов — и потому обязан быть виден.
	got := mergeSources(
		[]string{"opendota_timeline", "salts"},
		map[string]collectedRow{"opendota_timeline": {Matches: 5}},
		map[string]int64{"opendota_timeline": 100},
	)
	s, ok := bySource(got)["salts"]
	if !ok || s.Matches != 0 || s.Calls != 0 {
		t.Fatalf("полностью мёртвый источник не показан нулями: %+v (есть=%v)",
			s, ok)
	}
}

func TestReplayCountIsKeptSeparateFromTheTotal(t *testing.T) {
	// Матч с реплеем и матч из JSON — не одно и то же: у первого есть
	// поигроковый разбор, позиции и карты, у второго нет ничего этого
	// (спринт 195). Сложи мы их в одно число, падение реплейного пути
	// пряталось бы за исправным JSON-путём — ровно так его и не замечали
	// 3–6 сентября.
	got := mergeSources(
		[]string{"candidates"},
		map[string]collectedRow{"candidates": {Matches: 20, WithReplay: 3}},
		nil,
	)
	if got[0].Matches != 20 || got[0].WithReplay != 3 {
		t.Fatalf("разрез по реплеям потерян: %+v", got[0])
	}
}

func TestSilentSourcesGroupAtTheBottom(t *testing.T) {
	// Порядок — по притоку убывающе, при равенстве по имени. Молчащие
	// собираются одной группой внизу, а не рассыпаны по списку: страницу
	// читают глазами и сравнивают с тем, что было вчера, поэтому порядок
	// обязан быть устойчивым, а не зависеть от порядка строк из базы.
	got := mergeSources(
		[]string{"zzz", "aaa", "opendota_timeline", "bbb"},
		map[string]collectedRow{"opendota_timeline": {Matches: 7}},
		nil,
	)
	want := []string{"opendota_timeline", "aaa", "bbb", "zzz"}
	for i, name := range want {
		if got[i].Source != name {
			t.Fatalf("порядок %d: ждали %s, получили %s (%+v)",
				i, name, got[i].Source, got)
		}
	}
}

func TestAnEmptyRosterSerialisesAsAnEmptyList(t *testing.T) {
	// `null` вместо `[]` заставил бы страницу падать либо показывать
	// «нет данных» там, где верно «источников нет ни одного». Разные
	// утверждения, и различать их — задача этого же рода, что и весь
	// остальной файл.
	body, err := json.Marshal(adminStatus{Sources: mergeSources(nil, nil, nil)})
	if err != nil {
		t.Fatal(err)
	}
	if !contains(string(body), `"sources":[]`) {
		t.Fatalf("пустой список сериализован не как []: %s", body)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

func TestMoneyIsCountedAtTheTariffPrice(t *testing.T) {
	// $0.01 за 100 вызовов. Ошибка на порядок здесь не падает и не
	// логируется — она просто показывает владельцу не ту сумму, и
	// решение «поднимать ли потолок» принимается по неверному числу.
	if got := usdFor(12600); got < 1.259 || got > 1.261 {
		t.Fatalf("12600 вызовов = $%.4f, ждали $1.26", got)
	}
	if got := usdFor(0); got != 0 {
		t.Fatalf("ноль вызовов стоит $%v", got)
	}
}

func TestUnknownLatestIsNullNotZeroTime(t *testing.T) {
	// Пустая таблица даёт NULL в max(), и он обязан доехать до страницы
	// как null. Нулевое время (0001-01-01) выглядело бы как «последний
	// матч собран две тысячи лет назад» — то есть как поломка там, где на
	// самом деле просто ничего ещё не было.
	body, err := json.Marshal(adminCounts{})
	if err != nil {
		t.Fatal(err)
	}
	if !contains(string(body), `"latest":null`) {
		t.Fatalf("неизвестное время сериализовано не как null: %s", body)
	}
}
