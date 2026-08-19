"""Сервис без модели поднимается и говорит об этом честно (спринт 145).

ЧТО СЛУЧИЛОСЬ. На свежей VPS ml-service падал на старте:

    KeyError: 'win_probability/versions/production/model.pkl'

Реестр моделей пуст — и это НОРМАЛЬНОЕ состояние чистой установки:
обучение идёт после того, как сбор наберёт данные, а сбор только начался.
Но `build_server` загружал модель безусловно, а `restart: unless-stopped`
превращал отказ в бесконечный цикл. Развёртывание останавливалось на том,
что по замыслу должно было работать.

Показательно, что рядом, в том же build_server, дополнительные модели
(death_risk, laning) отсутствие уже переживали — warning и работа без них.
Исключением была ровно основная. То есть решение было принято правильно,
но применено к двум случаям из трёх.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной проверки вида «сервис вернул хоть что-нибудь».
Отсутствующая модель обязана давать UNAVAILABLE, а не число: молчаливая
заглушка (0.5, последняя известная кривая) была бы хуже отказа —
неверный ответ дороже отсутствующего, и заметить его нечем.
"""
import sys
from pathlib import Path

import grpc
import joblib
import pytest
from prometheus_client import REGISTRY

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from app import MLService, ModelSlot, build_server  # noqa: E402
from gen import services_pb2, services_pb2_grpc  # noqa: E402
from training.dataset import FEATURES  # noqa: E402
from training.train_winprob import train  # noqa: E402
from training.dataset import synth_matches  # noqa: E402

MISSING = "registry://win_probability/production"


def fv(**overrides):
    values = {f: 0.0 for f in FEATURES}
    values.update({"game_time": 1800.0, "kills_total": 20.0})
    values.update(overrides)
    return services_pb2.FeatureVector(values=values)


class _Clock:
    """Ручные часы: ждать настоящую минуту в тесте незачем."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _boom(_spec):
    raise KeyError("win_probability/versions/production/model.pkl")


# -- слот ---------------------------------------------------------------------

def test_slot_without_model_returns_none_and_does_not_raise():
    """Пустой реестр — не исключение, а состояние."""
    slot = ModelSlot(MISSING, loader=_boom, clock=_Clock())
    assert slot.get() is None


def test_slot_picks_the_model_up_when_it_appears():
    """Обучение прошло — сервис подхватывает модель БЕЗ перезапуска.

    Без этого «не падать на старте» означало бы «молчать до тех пор, пока
    кто-нибудь не догадается перезапустить контейнер», а знать момент
    неоткуда: публикация в реестр происходит в другом сервисе.
    """
    clock = _Clock()
    published = []

    def loader(_spec):
        if not published:
            raise KeyError("пусто")
        return published[0]

    slot = ModelSlot(MISSING, retry_after_s=60, loader=loader, clock=clock)
    assert slot.get() is None

    published.append(object())
    clock.t += 60
    assert slot.get() is published[0]


def test_retry_is_throttled():
    """Повторная попытка — не чаще retry_after_s.

    Без ограничения каждый запрос при пустом реестре ходил бы в MinIO, и
    сервис БЕЗ модели грузил бы хранилище сильнее, чем сервис с моделью.
    """
    clock = _Clock()
    tries = []

    def loader(_spec):
        tries.append(clock.t)
        raise KeyError("пусто")

    slot = ModelSlot(MISSING, retry_after_s=60, loader=loader, clock=clock)
    slot.get()
    clock.t += 59
    slot.get()
    assert tries == [0.0], "попытка повторена раньше срока"
    clock.t += 1
    slot.get()
    assert tries == [0.0, 60.0]


def test_loaded_model_is_not_reloaded():
    """Загруженную модель слот больше не трогает."""
    clock = _Clock()
    tries = []
    model = object()

    def loader(_spec):
        tries.append(clock.t)
        return model

    slot = ModelSlot(MISSING, retry_after_s=60, loader=loader, clock=clock)
    assert slot.get() is model
    clock.t += 10_000
    assert slot.get() is model
    assert len(tries) == 1


def test_metric_tracks_the_slot():
    """`ml_model_loaded` — единственная телеметрия, доходящая сама.

    На VPS браузера нет, и без метрики «сервис жив, но не обслуживает»
    выглядело бы неотличимо от «сервис работает».
    """
    clock = _Clock()
    box = []

    def loader(_spec):
        if not box:
            raise KeyError("пусто")
        return box[0]

    slot = ModelSlot(MISSING, retry_after_s=1, loader=loader, clock=clock)
    slot.get()
    assert REGISTRY.get_sample_value("ml_model_loaded") == 0

    box.append(object())
    clock.t += 1
    slot.get()
    assert REGISTRY.get_sample_value("ml_model_loaded") == 1


# -- сервер -------------------------------------------------------------------

@pytest.fixture(scope="module")
def empty_registry_channel():
    """Сервер, поднятый при пустом реестре — ровно случай чистой машины."""
    server, port = build_server("registry://win_probability/production", 0)
    server.start()
    with grpc.insecure_channel(f"localhost:{port}") as ch:
        yield ch
    server.stop(0)


def test_server_starts_without_a_model(empty_registry_channel):
    """Главное утверждение спринта: старт не падает.

    build_server ходит в НАСТОЯЩИЙ реестр — здесь его нет ни в каком виде
    (ни MinIO, ни MLflow), то есть воспроизводится состояние чистой
    установки, а не подделанное исключение.
    """
    assert empty_registry_channel is not None


def test_predict_without_a_model_is_unavailable(empty_registry_channel):
    """Отказ, а не выдуманное число."""
    stub = services_pb2_grpc.MLServiceStub(empty_registry_channel)
    with pytest.raises(grpc.RpcError) as e:
        stub.Predict(services_pb2.PredictRequest(
            match_id=1, model_name="win_probability", features=fv()))
    assert e.value.code() == grpc.StatusCode.UNAVAILABLE
    assert "не обучена" in e.value.details()


def test_stream_without_a_model_is_unavailable(empty_registry_channel):
    """Поток тоже отказывает — и до первого кадра, а не на середине."""
    stub = services_pb2_grpc.MLServiceStub(empty_registry_channel)
    frames = iter([services_pb2.FeatureFrame(game_time=600, features=fv())])
    with pytest.raises(grpc.RpcError) as e:
        list(stub.PredictStream(frames))
    assert e.value.code() == grpc.StatusCode.UNAVAILABLE


def test_unknown_model_name_is_still_not_found(empty_registry_channel):
    """Отсутствие ОСНОВНОЙ модели не переписывает ответ про чужое имя.

    Иначе клиент, спросивший несуществующую модель, получал бы «подожди,
    обучимся» вместо «такой модели у нас нет» — и ждал бы вечно.
    """
    stub = services_pb2_grpc.MLServiceStub(empty_registry_channel)
    with pytest.raises(grpc.RpcError) as e:
        stub.Predict(services_pb2.PredictRequest(
            match_id=1, model_name="выдуманная", features=fv()))
    assert e.value.code() == grpc.StatusCode.NOT_FOUND


def test_direct_construction_still_takes_a_plain_model(tmp_path):
    """MLService(модель) продолжает работать без всякого слота.

    Прямые вызовы (тесты, разбор одного матча) не должны знать про пустой
    реестр — иначе починка старта расползлась бы по всем потребителям.
    """
    artifact = train(synth_matches(60), num_rounds=40)
    path = tmp_path / "wp.pkl"
    joblib.dump(artifact, path)
    from predictors.win_probability import WinProbability

    svc = MLService(WinProbability(path))
    assert svc.slot.get() is not None
    assert svc.slot.get().features
