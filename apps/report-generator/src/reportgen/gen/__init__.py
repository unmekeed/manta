"""Сгенерированные gRPC-стабы (см. Makefile: proto-gen)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from . import services_pb2, services_pb2_grpc  # noqa: E402,F401
