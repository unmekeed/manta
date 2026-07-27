package pii

import "testing"

// Зафиксированные векторы. Те же значения продублированы в
// apps/feature-extractor/tests/test_pseudonym.py: экстрактор пишет
// псевдоним, шлюз ищет по нему, и расхождение реализаций означало бы, что
// GDPR-запрос молча не находит данные субъекта. Соль здесь тестовая —
// боевая живёт в MANTA_PII_SALT вне git.
var vectors = map[string]string{
	"Dendi":     "45b618f463685171",
	"dendi":     "45b618f463685171",
	"DENDI":     "45b618f463685171",
	"  Dendi  ": "45b618f463685171",
	"Мираж":     "1448dab5b648b7f4",
	"МИРАЖ":     "1448dab5b648b7f4",
	"Straße":    "1bbf31d07bdde277",
	"STRASSE":   "1bbf31d07bdde277",
	"ﬁx":        "d6340f5e062a8b27",
	"日本語":       "cfa37b7eaa481eca",
	"":          "",
}

var testSalt = []byte("manta-test-salt")

func TestPseudonymVectors(t *testing.T) {
	for nick, want := range vectors {
		if got := Pseudonym(nick, testSalt); got != want {
			t.Errorf("Pseudonym(%q) = %q, ожидалось %q", nick, got, want)
		}
	}
}

// Case folding обязан совпадать с питоновским str.casefold, а не с
// strings.ToLower: немецкое ß сворачивается в ss, и ToLower этого не
// делает. Именно на таком нике GDPR-поиск разъехался бы между сервисами.
func TestFoldingMatchesPythonCasefold(t *testing.T) {
	if Pseudonym("Straße", testSalt) != Pseudonym("STRASSE", testSalt) {
		t.Fatal("ß и SS должны давать один псевдоним (полный case folding)")
	}
	if Pseudonym("ﬁx", testSalt) == "" {
		t.Fatal("лигатура должна хешироваться, а не отбрасываться")
	}
}

func TestSaltChangesPseudonym(t *testing.T) {
	// Без секретной соли псевдоним подбирался бы перебором по публичному
	// списку ников — соль обязана влиять на результат.
	if Pseudonym("Dendi", testSalt) == Pseudonym("Dendi", []byte("other")) {
		t.Fatal("разная соль должна давать разный псевдоним")
	}
}

func TestEnabledDefaultsToPlain(t *testing.T) {
	t.Setenv(modeEnv, "")
	if Enabled() {
		t.Fatal("по умолчанию режим plain — стенд не должен менять поведение молча")
	}
	t.Setenv(modeEnv, "  PseudoNymize ")
	if !Enabled() {
		t.Fatal("значение режима должно читаться без учёта регистра и пробелов")
	}
}
