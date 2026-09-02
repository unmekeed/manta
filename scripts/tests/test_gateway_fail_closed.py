"""Контракт fail-closed gateway на VPS (спринт 162)."""
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "scripts" / "vps-bootstrap.sh"
GEN_KEYS = ROOT / "scripts" / "gen-dev-keys.sh"
MAIN = ROOT / "apps" / "api-gateway" / "cmd" / "server" / "main.go"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          **kwargs)


def test_generated_certificate_covers_host_and_compose_names(tmp_path):
    keys = tmp_path / "keys"
    run("bash", str(GEN_KEYS), str(keys), cwd=ROOT)
    cert = str(keys / "tls-cert.pem")
    run("openssl", "x509", "-in", cert, "-noout", "-checkhost", "localhost")
    run("openssl", "x509", "-in", cert, "-noout", "-checkhost", "api-gateway")


def test_key_generation_is_idempotent(tmp_path):
    keys = tmp_path / "keys"
    run("bash", str(GEN_KEYS), str(keys), cwd=ROOT)
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in keys.iterdir() if path.is_file()}
    run("bash", str(GEN_KEYS), str(keys), cwd=ROOT)
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in keys.iterdir() if path.is_file()}
    assert after == before


def test_gateway_security_is_validated_before_external_connections():
    src = MAIN.read_text(encoding="utf-8")
    assert src.index("cfg.Validate()") < src.index("pgxpool.New")
    assert src.index("tls.LoadX509KeyPair") < src.index("pgxpool.New")
    assert src.index("auth.New(deny)") < src.index("pgxpool.New")


def test_bootstrap_generates_keys_before_starting_stack_and_checks_runtime():
    src = BOOTSTRAP.read_text(encoding="utf-8")
    # Со спринта 176 порядок шагов задаёт список STEPS, а не
    # последовательность вызовов в хвосте: из него же берётся и разбор
    # --only, чтобы два перечня шагов не разошлись.
    steps = [e.split(":", 1)[0]
             for e in src.split("STEPS=(", 1)[1].split(")", 1)[0].split()]
    assert steps.index("keys") < steps.index("stack"), steps
    runtime = src[src.index("verify_gateway_security()"):
                  src.index("build_images()")]
    for required in ("MANTA_PROD=1", "JWT_PUBLIC_KEY_FILE=",
                     "TLS_CERT_FILE=", "TLS_KEY_FILE=", ".RW",
                     "docker port", "127.0.0.1:8080",
                     "https://localhost:8080/healthz"):
        assert required in runtime


def test_private_jwt_key_is_not_named_in_gateway_compose_mounts():
    overlay = (ROOT / "deployments" / "docker-compose.vps.yml").read_text(
        encoding="utf-8")
    gateway = overlay[overlay.index("  api-gateway:"):
                      overlay.index("  data-collector:")]
    assert "jwt-public.pem" in gateway
    assert "jwt-private.pem" not in gateway
    assert ":ro" in gateway
