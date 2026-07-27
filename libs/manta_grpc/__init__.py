"""mTLS для внутренних gRPC-сервисов (Гл. 9.1, NFR-SEC-01; спринт 71).

Спринт 58 закрыл TLS на периметре (gateway), но между собой сервисы
общались открытым текстом: любой процесс, дотянувшийся до :50051, мог
спрашивать модель и получать ответы. mTLS решает обе стороны сразу —
сервер проверяет клиента, клиент проверяет сервер.

Конфигурация одна на все сервисы (три файла):

    MANTA_MTLS_CA_FILE     — корневой сертификат, которым подписаны свои
    MANTA_MTLS_CERT_FILE   — сертификат этого процесса
    MANTA_MTLS_KEY_FILE    — его приватный ключ

Если хотя бы одного нет — mTLS ВЫКЛЮЧЕН и всё работает как раньше
(insecure). Это та же конвенция, что у JWT и TLS в спринте 58: локальный
стенд не меняет поведение молча, прод обязан включить. Факт выключения
логируется — «не настроено» и «настроено» обязаны различаться в логе,
иначе незамеченная опечатка в пути читается как «всё защищено».

Почему require_client_auth=True, а не False: без этого сервер лишь
предъявляет свой сертификат, и любой клиент по-прежнему подключается.
Это был бы обычный TLS, а не взаимный, — то есть не то, что требует
Гл. 9.1 для внутреннего периметра.
"""
from __future__ import annotations

import logging
import os

import grpc

logger = logging.getLogger("manta.grpc")

CA_ENV = "MANTA_MTLS_CA_FILE"
CERT_ENV = "MANTA_MTLS_CERT_FILE"
KEY_ENV = "MANTA_MTLS_KEY_FILE"


def _paths() -> tuple[str, str, str] | None:
    ca, cert, key = (os.getenv(CA_ENV, ""), os.getenv(CERT_ENV, ""),
                     os.getenv(KEY_ENV, ""))
    if not (ca and cert and key):
        return None
    missing = [p for p in (ca, cert, key) if not os.path.isfile(p)]
    if missing:
        # Указан, но не найден — это опечатка в конфиге, а не «выключено».
        # Молча падать в insecure здесь нельзя: администратор считает, что
        # включил mTLS.
        raise FileNotFoundError(
            f"mTLS настроен, но файлы не найдены: {', '.join(missing)}")
    return ca, cert, key


def enabled() -> bool:
    return _paths() is not None


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def add_port(server: grpc.Server, port: int, service: str) -> int:
    """Открыть порт сервера: mTLS, если настроен, иначе insecure."""
    paths = _paths()
    if paths is None:
        logger.warning("mtls_disabled service=%s port=%d — трафик открытым "
                       "текстом (для прода задать %s/%s/%s)",
                       service, port, CA_ENV, CERT_ENV, KEY_ENV)
        return server.add_insecure_port(f"[::]:{port}")
    ca, cert, key = paths
    creds = grpc.ssl_server_credentials(
        [(_read(key), _read(cert))],
        root_certificates=_read(ca),
        # Без этого флага получился бы обычный TLS: сервер представляется,
        # но пускает кого угодно.
        require_client_auth=True)
    logger.info("mtls_enabled service=%s port=%d", service, port)
    return server.add_secure_port(f"[::]:{port}", creds)


def channel(target: str, service: str = "") -> grpc.Channel:
    """Канал к внутреннему сервису: mTLS, если настроен, иначе insecure."""
    paths = _paths()
    if paths is None:
        return grpc.insecure_channel(target)
    ca, cert, key = paths
    creds = grpc.ssl_channel_credentials(root_certificates=_read(ca),
                                         private_key=_read(key),
                                         certificate_chain=_read(cert))
    logger.info("mtls_client target=%s service=%s", target, service or target)
    return grpc.secure_channel(target, creds)
