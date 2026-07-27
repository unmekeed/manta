"""Тесты mTLS-обвязки внутренних gRPC-сервисов (Гл. 9.1, спринт 71).

Смысл mTLS проверяется единственным способом — поднять настоящий сервер
и попробовать подключиться. Тест «функция вернула ServerCredentials»
ничего не доказывает: ровно так выглядела бы и конфигурация с
require_client_auth=False, где любой клиент по-прежнему проходит.

Поэтому здесь настоящий handshake на настоящем порту:
  1. клиент со своим сертификатом — проходит;
  2. клиент БЕЗ сертификата — отвергается;
  3. клиент с сертификатом от ЧУЖОГО CA — отвергается.
"""
import subprocess
import sys
from concurrent import futures
from pathlib import Path

import grpc
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import manta_grpc


def _make_ca(dir_: Path, name: str) -> tuple[Path, Path]:
    ca, key = dir_ / f"{name}-ca.pem", dir_ / f"{name}-ca-key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(key), "-out", str(ca), "-subj", f"/CN={name} CA"],
        check=True, capture_output=True)
    return ca, key


def _make_cert(dir_: Path, name: str, ca: Path, ca_key: Path) -> tuple[Path, Path]:
    key, csr, crt = (dir_ / f"{name}-key.pem", dir_ / f"{name}.csr",
                     dir_ / f"{name}-cert.pem")
    ext = dir_ / f"{name}.ext"
    ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n"
                   "extendedKeyUsage=serverAuth,clientAuth\n")
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}"],
        check=True, capture_output=True)
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(csr), "-days", "1",
         "-CA", str(ca), "-CAkey", str(ca_key), "-CAcreateserial",
         "-extfile", str(ext), "-out", str(crt)],
        check=True, capture_output=True)
    return crt, key


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    ca, ca_key = _make_ca(d, "own")
    cert, key = _make_cert(d, "service", ca, ca_key)
    alien_ca, alien_ca_key = _make_ca(d, "alien")
    alien_cert, alien_key = _make_cert(d, "alien-client", alien_ca, alien_ca_key)
    return {"ca": ca, "cert": cert, "key": key,
            "alien_ca": alien_ca, "alien_cert": alien_cert,
            "alien_key": alien_key}


def _env(monkeypatch, pki_):
    monkeypatch.setenv("MANTA_MTLS_CA_FILE", str(pki_["ca"]))
    monkeypatch.setenv("MANTA_MTLS_CERT_FILE", str(pki_["cert"]))
    monkeypatch.setenv("MANTA_MTLS_KEY_FILE", str(pki_["key"]))


def _serve(port_holder):
    """Пустой gRPC-сервер: для проверки handshake сервисы не нужны —
    неизвестный метод на установленном соединении даёт UNIMPLEMENTED, а
    отвергнутый handshake — UNAVAILABLE. Различие и есть предмет теста."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    port = manta_grpc.add_port(server, 0, "test")
    port_holder.append(port)
    server.start()
    return server


def _probe(channel) -> grpc.StatusCode:
    try:
        channel.unary_unary("/manta.Test/Ping")(b"", timeout=5)
        return grpc.StatusCode.OK
    except grpc.RpcError as e:
        return e.code()


def test_disabled_without_env(monkeypatch):
    for v in ("MANTA_MTLS_CA_FILE", "MANTA_MTLS_CERT_FILE", "MANTA_MTLS_KEY_FILE"):
        monkeypatch.delenv(v, raising=False)
    assert not manta_grpc.enabled(), "без переменных mTLS обязан быть выключен"


def test_partial_config_stays_disabled(monkeypatch, pki):
    """Задан только CA — это НЕ включённый mTLS. Иначе полуконфигурация
    молча дала бы сервер, который не проверяет клиентов."""
    monkeypatch.delenv("MANTA_MTLS_CERT_FILE", raising=False)
    monkeypatch.delenv("MANTA_MTLS_KEY_FILE", raising=False)
    monkeypatch.setenv("MANTA_MTLS_CA_FILE", str(pki["ca"]))
    assert not manta_grpc.enabled()


def test_missing_file_raises_not_silently_insecure(monkeypatch, pki):
    """Путь указан, но файла нет — это опечатка администратора, который
    считает, что включил mTLS. Молча падать в insecure нельзя."""
    monkeypatch.setenv("MANTA_MTLS_CA_FILE", str(pki["ca"]))
    monkeypatch.setenv("MANTA_MTLS_CERT_FILE", "/nope/cert.pem")
    monkeypatch.setenv("MANTA_MTLS_KEY_FILE", str(pki["key"]))
    with pytest.raises(FileNotFoundError):
        manta_grpc.enabled()


def test_own_client_connects(monkeypatch, pki):
    _env(monkeypatch, pki)
    ports = []
    server = _serve(ports)
    try:
        chan = manta_grpc.channel(f"localhost:{ports[0]}")
        # Соединение установлено: метода нет, но handshake прошёл.
        assert _probe(chan) == grpc.StatusCode.UNIMPLEMENTED
    finally:
        server.stop(0)


def test_client_without_cert_is_rejected(monkeypatch, pki):
    """Главная проверка: без require_client_auth=True этот тест прошёл бы
    как UNIMPLEMENTED — то есть посторонний клиент достучался бы."""
    _env(monkeypatch, pki)
    ports = []
    server = _serve(ports)
    try:
        creds = grpc.ssl_channel_credentials(
            root_certificates=pki["ca"].read_bytes())  # без своего сертификата
        chan = grpc.secure_channel(f"localhost:{ports[0]}", creds)
        assert _probe(chan) != grpc.StatusCode.UNIMPLEMENTED, \
            "клиент без сертификата не должен быть допущен"
    finally:
        server.stop(0)


def test_client_from_alien_ca_is_rejected(monkeypatch, pki):
    _env(monkeypatch, pki)
    ports = []
    server = _serve(ports)
    try:
        creds = grpc.ssl_channel_credentials(
            root_certificates=pki["ca"].read_bytes(),
            private_key=pki["alien_key"].read_bytes(),
            certificate_chain=pki["alien_cert"].read_bytes())
        chan = grpc.secure_channel(f"localhost:{ports[0]}", creds)
        assert _probe(chan) != grpc.StatusCode.UNIMPLEMENTED, \
            "сертификат чужого CA не должен приниматься"
    finally:
        server.stop(0)


def test_insecure_mode_still_works(monkeypatch):
    """Стенд без mTLS обязан продолжать работать — иначе спринт сломал бы
    всем локальную разработку."""
    for v in ("MANTA_MTLS_CA_FILE", "MANTA_MTLS_CERT_FILE", "MANTA_MTLS_KEY_FILE"):
        monkeypatch.delenv(v, raising=False)
    ports = []
    server = _serve(ports)
    try:
        chan = manta_grpc.channel(f"localhost:{ports[0]}")
        assert _probe(chan) == grpc.StatusCode.UNIMPLEMENTED
    finally:
        server.stop(0)


# -- Метрики не роняют сервис (инцидент 2026-07-27) ----------------------------

def test_metrics_port_busy_does_not_raise():
    """Ключевая проверка: занятый порт метрик обязан вернуть False, а не
    выбросить OSError. На локалке владельца ровно это положило семь
    сервисов разом — коллекторы неделю не собирали данные из-за
    Prometheus-эндпоинта, который к сбору отношения не имеет."""
    import socket
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        assert manta_grpc.serve_metrics(port, "test") is False
    finally:
        s.close()


def test_metrics_disabled_when_port_zero():
    """port=0 — осознанное выключение метрик, а не сбой."""
    assert manta_grpc.serve_metrics(0, "test") is False


def test_metrics_started_on_free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert manta_grpc.serve_metrics(port, "test") is True
