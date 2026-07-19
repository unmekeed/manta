"""Сгенерированные gRPC-стабы (см. Makefile: proto-gen).

grpc_tools.protoc генерирует services_pb2_grpc с абсолютным импортом
`import services_pb2`, поэтому каталог пакета добавляется в sys.path.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from . import services_pb2, services_pb2_grpc  # noqa: E402,F401
