"""Тесты псевдонимизации никнеймов (Гл. 9.7, спринт 70).

Главная проверка — кросс-языковая. Псевдоним считают ДВА сервиса:
экстрактор пишет его в витрину (Python), шлюз хеширует ник из
GDPR-запроса, чтобы найти строки (Go). Разъезд реализаций не уронил бы
ничего заметно — он просто привёл бы к тому, что экспорт и стирание
молча не находят данные субъекта. Для GDPR это невыполненное требование,
поэтому векторы зафиксированы в обоих тестах и обязаны совпадать.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extractor.pseudonym import HASH_LEN, apply, enabled, pseudonym

SALT = b"manta-test-salt"

# Те же значения, что в apps/api-gateway/internal/pii/pii_test.go.
VECTORS = {
    "Dendi": "45b618f463685171",
    "dendi": "45b618f463685171",
    "DENDI": "45b618f463685171",
    "  Dendi  ": "45b618f463685171",
    "Мираж": "1448dab5b648b7f4",
    "МИРАЖ": "1448dab5b648b7f4",
    "Straße": "1bbf31d07bdde277",
    "STRASSE": "1bbf31d07bdde277",
    "ﬁx": "d6340f5e062a8b27",
    "日本語": "cfa37b7eaa481eca",
    "": "",
}


def test_matches_go_implementation():
    for nick, want in VECTORS.items():
        assert pseudonym(nick, SALT) == want, nick


def test_case_folding_not_lowercase():
    """str.lower() не свернул бы ß в ss — и Go с Python разошлись бы
    ровно на таком нике."""
    assert pseudonym("Straße", SALT) == pseudonym("STRASSE", SALT)
    assert "Straße".lower() != "strasse", "иначе тест ничего не проверяет"


def test_salt_matters():
    assert pseudonym("Dendi", SALT) != pseudonym("Dendi", b"other")


def test_hash_length_and_charset():
    h = pseudonym("Dendi", SALT)
    assert len(h) == HASH_LEN
    assert all(c in "0123456789abcdef" for c in h)


def test_plain_mode_keeps_nickname(monkeypatch):
    monkeypatch.delenv("MANTA_PII_MODE", raising=False)
    monkeypatch.setenv("MANTA_PII_SALT", "s")
    assert not enabled()
    row = apply({"player_name": "Dendi"})
    assert row["player_name"] == "Dendi", "режим plain не должен трогать ник"
    assert row["player_hash"], "хеш пишется всегда — витрина готова к переключению"


def test_pseudonymize_mode_drops_nickname(monkeypatch):
    monkeypatch.setenv("MANTA_PII_MODE", "pseudonymize")
    monkeypatch.setenv("MANTA_PII_SALT", "s")
    assert enabled()
    row = apply({"player_name": "Dendi"})
    assert row["player_name"] == "", "ник не должен попасть в витрину"
    assert row["player_hash"] == pseudonym("Dendi", b"s")


def test_empty_nickname_gives_empty_hash(monkeypatch):
    """Пустой ник не должен получать псевдоним: иначе все безымянные
    строки склеились бы в одного «субъекта», и GDPR-стирание по нему
    задело бы чужие данные."""
    monkeypatch.setenv("MANTA_PII_MODE", "pseudonymize")
    monkeypatch.setenv("MANTA_PII_SALT", "s")
    assert apply({"player_name": ""})["player_hash"] == ""
    assert apply({"player_name": "   "})["player_hash"] == ""
