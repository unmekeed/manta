"""Статические границы ClickHouse для VPS (спринт 161)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USERS = ROOT / "scripts" / "create-clickhouse-users.sh"


def script() -> str:
    return USERS.read_text(encoding="utf-8")


def grant(role: str) -> str:
    match = re.search(rf"^GRANT ([^;]+) ON manta\.\* TO {role};$",
                      script(), re.M)
    assert match, f"grant for {role} not found"
    return match.group(1)


def test_reader_and_trainer_are_read_only_distinct_identities():
    assert grant("manta_reader") == "SELECT"
    assert grant("manta_trainer") == "SELECT"
    text = script()
    assert "manta_ch_reader" in text
    assert "manta_ch_trainer" in text


def test_writer_has_runtime_mutations_but_no_schema_admin():
    privileges = {item.strip() for item in grant("manta_writer").split(",")}
    assert privileges == {"SELECT", "INSERT", "ALTER UPDATE", "ALTER DELETE"}
    for forbidden in ("CREATE", "DROP", "TRUNCATE", "ACCESS MANAGEMENT"):
        assert forbidden not in privileges


def test_rerun_resets_role_grants_and_passwords():
    text = script()
    for role in ("reader", "writer", "trainer"):
        assert f"REVOKE IF EXISTS ALL ON *.* FROM manta_{role};" in text
        assert f"ALTER USER manta_ch_{role} IDENTIFIED WITH sha256_hash" in text
        assert f"ALTER USER manta_ch_{role} DEFAULT ROLE manta_{role};" in text


def test_plaintext_passwords_are_not_embedded_in_clickhouse_sql():
    sql = script().split("<<SQL", 1)[1]
    for name in ("MANTA_CH_PASS_READER", "MANTA_CH_PASS_WRITER",
                 "MANTA_CH_PASS_TRAINER"):
        assert name not in sql
    assert "sha256_hash" in sql
