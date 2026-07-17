"""Интеграционные тесты gRPC-сервера MLService (in-process, порт 0)."""
import sys
from pathlib import Path

import grpc
import joblib
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from app import build_server  # noqa: E402
from gen import services_pb2, services_pb2_grpc  # noqa: E402
from training.dataset import FEATURES, synth_matches  # noqa: E402
from training.train_winprob import train  # noqa: E402


@pytest.fixture(scope="module")
def channel(tmp_path_factory):
    artifact = train(synth_matches(80), num_rounds=80)
    path = tmp_path_factory.mktemp("model") / "wp.pkl"
    joblib.dump(artifact, path)
    server, port = build_server(path, 0)
    server.start()
    with grpc.insecure_channel(f"localhost:{port}") as ch:
        yield ch
    server.stop(0)


def fv(**overrides):
    values = {"game_time": 1800.0, "networth_diff": 0.0, "xp_diff": 0.0,
              "kills_diff": 0.0, "kills_total": 20.0, "position_advance": 0.0}
    values.update(overrides)
    return services_pb2.FeatureVector(values=values)


def test_predict_returns_probability(channel):
    stub = services_pb2_grpc.MLServiceStub(channel)
    resp = stub.Predict(services_pb2.PredictRequest(
        match_id=1, model_name="win_probability", features=fv()))
    assert 0.0 <= resp.win_probability_radiant <= 1.0
    assert resp.model_version


def test_predict_reacts_to_advantage(channel):
    stub = services_pb2_grpc.MLServiceStub(channel)
    ahead = stub.Predict(services_pb2.PredictRequest(
        features=fv(networth_diff=25000.0, xp_diff=30000.0, kills_diff=10.0,
                    position_advance=0.6)))
    behind = stub.Predict(services_pb2.PredictRequest(
        features=fv(networth_diff=-25000.0, xp_diff=-30000.0, kills_diff=-10.0,
                    position_advance=-0.6)))
    assert ahead.win_probability_radiant > 0.6
    assert behind.win_probability_radiant < 0.4


def test_predict_missing_feature_is_invalid_argument(channel):
    stub = services_pb2_grpc.MLServiceStub(channel)
    bad = services_pb2.FeatureVector(values={"game_time": 60.0})
    with pytest.raises(grpc.RpcError) as e:
        stub.Predict(services_pb2.PredictRequest(features=bad))
    assert e.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_unknown_model_not_found(channel):
    stub = services_pb2_grpc.MLServiceStub(channel)
    with pytest.raises(grpc.RpcError) as e:
        stub.Predict(services_pb2.PredictRequest(
            model_name="draft", features=fv()))
    assert e.value.code() == grpc.StatusCode.NOT_FOUND


def test_predict_stream_curve(channel):
    stub = services_pb2_grpc.MLServiceStub(channel)
    frames = (
        services_pb2.FeatureFrame(
            match_id=1, game_time=t,
            features=fv(game_time=float(t), networth_diff=float(t) * 15,
                        xp_diff=float(t) * 18, kills_diff=float(t) / 200,
                        position_advance=min(float(t) / 3000, 1.0)))
        for t in range(60, 1801, 300)
    )
    curve = list(stub.PredictStream(frames))
    assert len(curve) == 6
    assert [p.game_time for p in curve] == list(range(60, 1801, 300))
    assert all(0.0 <= p.radiant <= 1.0 and 0.0 <= p.confidence <= 1.0
               for p in curve)
    # Растущее преимущество Radiant → WP не убывает (изотоника даёт плато)
    # и к концу уверенно выше 0.5.
    assert curve[-1].radiant >= curve[0].radiant
    assert curve[-1].radiant > 0.6


def test_predict_stream_shap_contributions(channel):
    """PredictStream отдаёт SHAP-вклады кадра: топ-3 по |значению|,
    осмысленный знак у решающей фичи при большом преимуществе."""
    stub = services_pb2_grpc.MLServiceStub(channel)
    frames = iter([
        services_pb2.FeatureFrame(
            match_id=1, game_time=1800,
            features=fv(game_time=1800.0, networth_diff=20000.0,
                        xp_diff=24000.0, kills_diff=15.0, kills_total=30.0,
                        position_advance=0.8)),
        services_pb2.FeatureFrame(
            match_id=1, game_time=1860,
            features=fv(game_time=1860.0, networth_diff=-20000.0,
                        xp_diff=-24000.0, kills_diff=-15.0, kills_total=30.0,
                        position_advance=-0.8)),
    ])
    ahead, behind = list(stub.PredictStream(frames))
    for p in (ahead, behind):
        assert 1 <= len(p.top_contributions) <= 3
        names = {c.feature_name for c in p.top_contributions}
        assert names  # имена фич из артефакта
    # Уверенное преимущество Radiant → главный вклад положительный;
    # зеркальная позиция → отрицательный.
    assert ahead.top_contributions[0].contribution > 0
    assert behind.top_contributions[0].contribution < 0
