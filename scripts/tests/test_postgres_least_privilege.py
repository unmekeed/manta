"""Контракт раздельных PostgreSQL-ролей VPS (спринт 159)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "infra" / "migrations" / "postgres"
USERS = ROOT / "scripts" / "create-db-users.sh"
BOOTSTRAP = ROOT / "scripts" / "vps-bootstrap.sh"


def grants_to(role: str) -> str:
    chunks = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        chunks.extend(match.group(0) for match in re.finditer(
            rf"GRANT\s+[^;]+?\s+TO\s+{role}\s*;", text, re.I | re.S))
    return "\n".join(chunks).lower()


def test_collector_can_write_only_collector_state():
    grants = grants_to("manta_collector")
    for table in ("collectedmatches", "collectorcursor", "playerranks",
                  "replaycandidates", "apibudget", "parkedreplays"):
        assert table in grants
    for forbidden in ("matchreports", "analysisjobs", "eventoutbox",
                      "accounts", "subscriptions"):
        assert forbidden not in grants


def test_reports_role_cannot_write_collector_or_gateway_tables():
    grants = grants_to("manta_reports")
    assert "matchreports" in grants
    for forbidden in ("collectedmatches", "collectorcursor",
                      "analysisjobs", "eventoutbox"):
        assert forbidden not in grants


def test_gateway_report_access_is_update_only_for_gdpr():
    grants = grants_to("manta_gateway")
    assert re.search(r"grant\s+update\s+on\s+matchreports", grants)
    assert not re.search(r"grant\s+[^;]*delete[^;]*matchreports", grants)


def test_login_users_are_mapped_to_the_matching_group_roles():
    text = USERS.read_text(encoding="utf-8")
    pairs = {
        "manta_collector_user": "manta_collector",
        "manta_reports_user": "manta_reports",
        "manta_gateway_user": "manta_gateway",
        "manta_ro_user": "manta_ro",
    }
    for user, role in pairs.items():
        assert re.search(rf"make_user\s+{user}\s+{role}\b", text)


def test_bootstrap_uses_container_psql_not_a_new_host_dependency():
    users = USERS.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'docker exec -i -e PGPASSWORD="$PGPASSWORD"' in users
    assert "POSTGRES_CONTAINER=manta-postgres-1" in bootstrap
