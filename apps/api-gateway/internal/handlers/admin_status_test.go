package handlers

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
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

// names — перечень источников без времени последнего цикла.
//
// Отдельным помощником, а не литералом в каждом тесте: время цикла
// проверяется своими тестами ниже, и загромождать им проверки про
// счётчики значило бы прятать проверяемое свойство за шумом.
func names(list ...string) map[string]*time.Time {
	out := map[string]*time.Time{}
	for _, n := range list {
		out[n] = nil
	}
	return out
}

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
		names("opendota_timeline", "stratz_timeline"),
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
		names("opendota_timeline", "salts"),
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
		names("candidates"),
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
		names("zzz", "aaa", "opendota_timeline", "bbb"),
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

// -- два написания одного источника (спринт 198e) ------------------------------

// Живой перечень с машины 08.09.2026 — ровно то, что вернул UNION двух
// таблиц. Фикстура намеренно НЕ придумана: прежние тесты брали имена в
// одном написании, и потому задвоение через них проходило насквозь.
// «Вход должен быть боевым» — правило проекта, применённое к себе.
var liveRoster = names("opendota_public", "salts", "opendota", "stratz_timeline",
	"opendota_timeline", "gc-salts", "opendota-league", "opendota-public",
	"opendota-timeline", "opendota-timeline-pro", "opendota_league",
	"opendota_timeline_pro", "stratz-timeline")

func TestOneSourcePerRowDespiteTwoSpellings(t *testing.T) {
	// ГЛАВНОЕ. CollectedMatches пишет `opendota_timeline`, ApiBudget —
	// `opendota-timeline`. Это ОДИН источник; две строки означают, что в
	// каждой половина правды.
	got := mergeSources(liveRoster,
		map[string]collectedRow{
			"opendota_timeline": {Matches: 5},
			"opendota_public":   {Matches: 36, WithReplay: 36},
			"stratz_timeline":   {Matches: 9},
		},
		map[string]int64{
			"opendota-timeline": 60,
			"opendota-public":   7,
			"stratz-timeline":   29,
			"gc-salts":          8,
		})

	seen := map[string]int{}
	for _, s := range got {
		seen[s.Source]++
		if strings.Contains(s.Source, "-") {
			t.Errorf("в ответе осталось дефисное имя %q — два написания "+
				"одного источника доехали до страницы как разные", s.Source)
		}
	}
	for name, n := range seen {
		if n > 1 {
			t.Errorf("источник %q встречается %d раза", name, n)
		}
	}

	m := bySource(got)
	// Обе половины правды обязаны сойтись в одной строке. Иначе строка
	// «0 матчей, 60 вызовов» читается как «жжёт бюджет впустую» — то есть
	// как сигнатура аварии STRATZ, ради которой страница и делалась.
	if s := m["opendota_timeline"]; s.Matches != 5 || s.Calls != 60 {
		t.Errorf("opendota_timeline: %+v, ждали 5 матчей и 60 вызовов", s)
	}
	if s := m["stratz_timeline"]; s.Matches != 9 || s.Calls != 29 {
		t.Errorf("stratz_timeline: %+v, ждали 9 матчей и 29 вызовов", s)
	}
	if s := m["opendota_public"]; s.Matches != 36 || s.WithReplay != 36 ||
		s.Calls != 7 {
		t.Errorf("opendota_public: %+v", s)
	}
}

func TestSaltsAndGcSaltsStayApart(t *testing.T) {
	// `gc-salts` — скрипт добычи солей, а не коллектор `salts`. Замена
	// дефиса на подчёркивание их не сливает, и сливать не должна: у них
	// разная работа, и объединённая строка спрятала бы, что соли добывает
	// одно, а матчи собирает другое.
	got := mergeSources(names("salts", "gc-salts"),
		map[string]collectedRow{"salts": {Matches: 32, WithReplay: 32}},
		map[string]int64{"gc-salts": 8})
	m := bySource(got)
	if len(got) != 2 {
		t.Fatalf("ждали две отдельные строки, получили %+v", got)
	}
	if m["salts"].Matches != 32 || m["gc_salts"].Calls != 8 {
		t.Errorf("счётчики перепутаны: %+v", got)
	}
}

func TestCountersLandOnTheCanonicalNameEvenWithoutARoster(t *testing.T) {
	// Перечень строится за неделю, а счётчики — за сутки, и это разные
	// окна: свежий источник может дать матчи, ещё не попав в перечень.
	// Потеряй мы его счётчики — приток был бы занижен молча.
	got := mergeSources(nil,
		map[string]collectedRow{"новый_источник": {Matches: 3}},
		map[string]int64{"новый-источник": 11})
	if len(got) != 1 || got[0].Matches != 3 || got[0].Calls != 11 {
		t.Fatalf("счётчики без перечня потеряны: %+v", got)
	}
}

// -- источник, простаивающий неделями (спринт 200) -----------------------------

func TestASourceIdleForWeeksIsStillListed(t *testing.T) {
	// ЖИВОЙ СЛУЧАЙ 08.09.2026. Первая работающая страница НЕ показала
	// `candidates` — источник, за которым закреплено 58% бюджета вызовов.
	// Он неделю не собрал ничего и не потратил ни вызова, и потому исчез:
	// перечень строился из тех, кто ПИСАЛ за неделю.
	//
	// То есть страница, сделанная ради различения «источник умер» и
	// «источника нет», сама же стёрла это различие — на самом важном
	// источнике. Теперь перечень идёт из CollectorCursor: строка там
	// заводится на каждый когда-либо работавший источник и от простоя не
	// пропадает.
	long := time.Now().Add(-30 * 24 * time.Hour)
	got := mergeSources(
		map[string]*time.Time{"candidates": &long, "salts": nil},
		map[string]collectedRow{"salts": {Matches: 32, WithReplay: 32}},
		nil)

	s, ok := bySource(got)["candidates"]
	if !ok {
		t.Fatal("простаивающий источник пропал из списка — «умер» снова " +
			"неотличимо от «его нет»")
	}
	if s.Matches != 0 || s.Calls != 0 {
		t.Errorf("candidates: %+v, ждали нули", s)
	}
	if s.LastCycleAt == nil || !s.LastCycleAt.Equal(long) {
		t.Errorf("не показано, КОГДА источник работал в последний раз: %+v", s)
	}
}

func TestTheFresherCycleTimeWins(t *testing.T) {
	// Курсор пишется под одним написанием имени, бюджет под другим
	// (дефис против подчёркивания, спринт 198e). После канонизации в одну
	// строку сходятся два времени, и взять надо БОЛЕЕ СВЕЖЕЕ: старое
	// объявило бы работающий источник простаивающим.
	old := time.Now().Add(-10 * 24 * time.Hour)
	recent := time.Now().Add(-1 * time.Hour)

	// ПРОГОНЯЕТСЯ МНОГО РАЗ. Обход map в Go намеренно рандомизирован, и
	// одиночный вызов проверяет лишь ОДИН порядок из двух. Первая
	// редакция этого теста так и делала — и мутацию «брать первое
	// непустое время» пережила: половину запусков она проходила по
	// удаче. Недетерминированный тест хуже отсутствующего: он создаёт
	// уверенность, которой не заслужил.
	for i := 0; i < 50; i++ {
		got := mergeSources(
			map[string]*time.Time{"opendota-timeline": &old,
				"opendota_timeline": &recent},
			nil, nil)
		if len(got) != 1 {
			t.Fatalf("два написания дали %d строк: %+v", len(got), got)
		}
		if got[0].LastCycleAt == nil || !got[0].LastCycleAt.Equal(recent) {
			t.Fatalf("прогон %d: взято не более свежее время: %+v",
				i, got[0])
		}
	}
}

func TestTheRosterComesFromTheCursorRegistry(t *testing.T) {
	// ЖИВОЙ СЛУЧАЙ 08.09.2026: `candidates` — источник с 58% бюджета —
	// со страницы ИСЧЕЗ, потому что перечень строился из тех, кто ПИСАЛ
	// за неделю, а он неделю ничего не писал.
	//
	// Проверка текстовая и потому слабая, но она закрывает дыру, которую
	// не видит ничто другое: `mergeSources` получает перечень уже
	// готовым, а тест PREPARE проверяет лишь то, что запрос валиден.
	// Убери CollectorCursor — запрос останется валидным, тесты логики
	// останутся зелёными, и источник снова пропадёт молча.
	if !strings.Contains(adminRosterSQL, "CollectorCursor") {
		t.Fatal("перечень источников больше не берётся из реестра курсоров " +
			"— простаивающий источник снова исчезнет со страницы")
	}
	if !strings.Contains(adminRosterSQL, "updated_at") {
		t.Fatal("время последнего цикла не запрашивается — «молчит» опять " +
			"станет неотличимо от «его нет»")
	}
}

func TestANeverRunSourceHasNullCycleTime(t *testing.T) {
	// Источник есть в бюджете, но курсора у него нет: он ни разу не
	// довёл цикл до конца. Нулевое время (0001-01-01) выглядело бы как
	// «работал две тысячи лет назад» — то есть как поломка там, где
	// верно «ни разу не работал».
	got := mergeSources(map[string]*time.Time{"новый": nil}, nil,
		map[string]int64{"новый": 5})
	if got[0].LastCycleAt != nil {
		t.Fatalf("время цикла выдумано: %v", *got[0].LastCycleAt)
	}
	body, err := json.Marshal(got[0])
	if err != nil {
		t.Fatal(err)
	}
	if !contains(string(body), `"last_cycle_at":null`) {
		t.Fatalf("неизвестное время сериализовано не как null: %s", body)
	}
}
