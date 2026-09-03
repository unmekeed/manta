"""gRPC-сервер ML Service (Гл. 3.7, контракт proto/services.proto).

Реализует MLService:
- Predict       — вероятность победы Radiant по вектору фич;
- PredictStream — поток кадров матча → поток точек WP-кривой.

Модель — артефакт Win Probability (LightGBM + изотоническая калибровка,
см. training/train_winprob.py). Ключи FeatureVector.values должны
содержать фичи из training.dataset.FEATURES; отсутствующие ключи —
ошибка INVALID_ARGUMENT (молчаливый ноль исказил бы прогноз).

Запуск: PYTHONPATH=src python -m app [--port 50051] [--model PATH]
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from concurrent import futures

import grpc

import manta_grpc
import numpy as np
from prometheus_client import Counter, Gauge, Histogram

from explain.winprob_shap import explain_matrix
from gen import services_pb2, services_pb2_grpc
from predictors.win_probability import DEFAULT_MODEL, WinProbability

logger = logging.getLogger("ml-service")

PREDICT_LATENCY = Histogram(
    "ml_predict_latency_seconds", "Латентность Predict/PredictStream (на кадр)",
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1))
PREDICTIONS = Counter("ml_predictions_total", "Выполненные предсказания",
                      ["rpc"])
MODEL_LOADED = Gauge(
    "ml_model_loaded",
    "1 — основная модель загружена; 0 — сервис отвечает UNAVAILABLE")
MODEL_RELOADS = Counter(
    "ml_model_reloads_total",
    "Переключений на новую версию модели из реестра без перезапуска")
MODEL_FEATURES = Gauge(
    "ml_model_features", "Число фич в загруженной модели")


class ModelSlot:
    """Основная модель: может отсутствовать, и это НЕ повод падать.

    ЗАЧЕМ (спринт 145). На чистой машине реестр моделей пуст: обучение
    пойдёт после того, как сбор наберёт данные, а до тех пор
    `registry://win_probability/production` не разрешается. Раньше это
    роняло build_server на старте, а `restart: unless-stopped` превращал
    падение в бесконечный цикл — то есть НОРМАЛЬНОЕ состояние свежей
    установки выглядело как поломка сервиса и останавливало развёртывание.

    Показательно, что в том же build_server дополнительные модели
    (death_risk, laning) отсутствие уже переживали: warning в лог и работа
    без них. Исключением была ровно основная.

    Отсутствие не заминается: метрика ml_model_loaded держит 0, в логе
    предупреждение, вызовы получают UNAVAILABLE. Выдуманного числа никто
    не увидит — в этом и разница между «нет ответа» и «неверный ответ».

    Повторная попытка нужна, чтобы сервис ПОДХВАТИЛ модель, как только
    ml-autotrain её опубликует. Без неё контейнер пришлось бы
    перезапускать руками, а знать момент неоткуда.

    ЖИВОЙ ОТКАЗ 2-3 СЕНТЯБРЯ, из-за которого слот переписан (спринт 191).
    Всё сказанное выше было верно ровно для перехода «модели НЕТ → есть».
    Переход «есть → есть новее» не покрывался: загруженная модель
    кэшировалась навсегда. Гейт продвинул модель с новым составом фич,
    сервис продолжал отдавать старую, и полтора суток это никому не
    мешало — пока контейнер по неизвестной причине не перезапустился и не
    подхватил новую. Генерация отчётов встала на 29 часов.

    Дефект той же формы, что `--probe` в спринте 189: ВЕРНОЕ решение,
    применённое к части своих случаев и не применённое к соседним. И
    заметить это по коду нельзя — обе половины выглядят разумно.

    Теперь слот следит за версией стейджа в реестре и переключается сам.
    Спрашивается ТОЛЬКО номер версии (`stage_version`), а не артефакт:
    иначе проверка «не сменилась ли модель» стоила бы скачивания весов.
    """

    def __init__(self, spec, *, retry_after_s: float = 60.0,
                 refresh_after_s: float = 300.0,
                 loader=None, prober=None, clock=None):
        self._spec = spec
        self._retry_after = retry_after_s
        self._refresh_after = refresh_after_s
        self._load = loader or (lambda s: WinProbability(_resolve_model_path(s)))
        self._probe = prober or _stage_version_of
        self._clock = clock or time.monotonic
        self._model: WinProbability | None = None
        self._version: str | None = None
        self._last_try: float | None = None
        self._last_check: float | None = None
        MODEL_LOADED.set(0)

    @classmethod
    def ready(cls, model: WinProbability) -> "ModelSlot":
        """Слот с уже загруженной моделью — для тестов и прямых вызовов."""
        slot = cls.__new__(cls)
        slot._spec, slot._retry_after, slot._load = None, 0.0, None
        slot._refresh_after, slot._probe = 0.0, None
        slot._clock, slot._model, slot._last_try = time.monotonic, model, None
        slot._version, slot._last_check = None, None
        MODEL_LOADED.set(1)
        return slot

    def get(self) -> WinProbability | None:
        """Модель или None; по дороге — подхват новой версии из реестра.

        Ограничение по времени тут не про скорость: без него каждый запрос
        при пустом реестре ходил бы в MinIO, и сервис БЕЗ модели грузил бы
        хранилище сильнее, чем сервис с моделью.
        """
        if self._load is None:
            return self._model
        if self._model is not None:
            self._maybe_refresh()
            return self._model
        now = self._clock()
        if self._last_try is not None and now - self._last_try < self._retry_after:
            return None
        self._last_try = now
        return self._reload("основная модель загружена")

    def _maybe_refresh(self) -> None:
        """Сменилась ли версия стейджа — не чаще refresh_after_s.

        Сбой опроса НЕ роняет и не разряжает слот: недоступный на минуту
        реестр не повод перестать обслуживать запросы уже загруженной
        моделью. Молча продолжаем со старой — это худший из безопасных
        исходов, а не лучший из опасных.
        """
        if not self._refresh_after or self._probe is None:
            return
        now = self._clock()
        if (self._last_check is not None
                and now - self._last_check < self._refresh_after):
            return
        self._last_check = now
        try:
            latest = self._probe(self._spec)
        except Exception as e:  # noqa: BLE001 — реестр недоступен, работаем
            logger.warning("не удалось проверить версию модели (%s): %s",
                           self._spec, e)
            return
        if latest is None or latest == self._version:
            return
        logger.info("в реестре новая версия модели: %s → %s",
                    self._version, latest)
        before = self._model
        if self._reload("модель переключена на новую версию") is not before:
            MODEL_RELOADS.inc()

    def _reload(self, done_msg: str) -> WinProbability | None:
        try:
            model = self._load(self._spec)
        except Exception as e:  # noqa: BLE001 — любая беда реестра равнозначна
            if self._model is None:
                logger.warning("основная модель недоступна (%s): %s",
                               self._spec, e)
                MODEL_LOADED.set(0)
            else:
                # Уже работающую модель неудачная попытка не отнимает.
                logger.warning("новую версию загрузить не удалось (%s): %s",
                               self._spec, e)
            return self._model
        self._model = model
        self._version = self._safe_version()
        # Только что загруженную модель проверять на свежесть незачем:
        # без этой строки первый же следующий запрос шёл бы в реестр
        # снова, и ограничение по времени начинало бы действовать лишь со
        # второй проверки.
        self._last_check = self._clock()
        logger.info("%s (%s, версия %s, фич %d)", done_msg, self._spec,
                    self._version, len(getattr(model, "features", []) or []))
        MODEL_LOADED.set(1)
        MODEL_FEATURES.set(len(getattr(model, "features", []) or []))
        return model

    def _safe_version(self) -> str | None:
        if self._probe is None:
            return None
        try:
            return self._probe(self._spec)
        except Exception:  # noqa: BLE001 — версия справочна, модель уже есть
            return None


def _vector_from_features(fv, features: list[str]) -> np.ndarray:
    """FeatureVector → матрица (1, n) в порядке фич АРТЕФАКТА: сервер
    обслуживает ту версию модели, что загружена; лишние ключи клиента
    игнорируются (клиент новее модели — это нормально)."""
    missing = [f for f in features if f not in fv.values]
    if missing:
        raise KeyError(", ".join(missing))
    return np.array([[fv.values[f] for f in features]])


def _confidence(wp: float) -> float:
    """Грубая уверенность: расстояние от максимальной неопределённости 0.5,
    нормированное в [0, 1]. Честная оценка (квантили ансамбля) — Фаза 4."""
    return abs(wp - 0.5) * 2.0


NO_MODEL = ("модель win_probability ещё не обучена: реестр пуст. "
            "Сервис поднят и подхватит её автоматически")


class MLService(services_pb2_grpc.MLServiceServicer):
    def __init__(self, model: WinProbability | ModelSlot,
                 extra_models: dict[str, WinProbability] | None = None):
        # Принимается и готовая модель, и слот: прямые вызовы (тесты,
        # разбор одного матча) не должны знать про пустой реестр.
        self.slot = model if isinstance(model, ModelSlot) else ModelSlot.ready(model)
        # Дополнительные модели по model_name (Гл. 6.3: death_risk и т.п.).
        # Формат артефактов у всех одинаковый, сервятся тем же классом;
        # PredictResponse.win_probability_radiant несёт вероятность модели
        # (для death_risk — P(смерть в ближайшие 30 c)).
        self.extra = extra_models or {}

    def Predict(self, request, context):
        model = self.slot.get()
        if request.model_name not in ("", "win_probability"):
            model = self.extra.get(request.model_name)
            if model is None:
                context.abort(grpc.StatusCode.NOT_FOUND,
                              f"model {request.model_name!r} is not served")
        elif model is None:
            context.abort(grpc.StatusCode.UNAVAILABLE, NO_MODEL)
        try:
            X = _vector_from_features(request.features, model.features)
        except KeyError as missing:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          f"missing features: {missing}")
        with PREDICT_LATENCY.time():
            wp = float(model.predict(X)[0])
        PREDICTIONS.labels("predict").inc()
        return services_pb2.PredictResponse(
            win_probability_radiant=wp,
            model_version=model.version,
        )

    def PredictStream(self, request_iterator, context):
        # Модель берётся ОДИН раз на поток: подмена в середине кривой
        # означала бы точки от разных версий в одном ответе.
        model = self.slot.get()
        if model is None:
            context.abort(grpc.StatusCode.UNAVAILABLE, NO_MODEL)
        for frame in request_iterator:
            try:
                X = _vector_from_features(frame.features, model.features)
            except KeyError as missing:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                              f"frame t={frame.game_time}: missing features: "
                              f"{missing}")
            with PREDICT_LATENCY.time():
                wp = float(model.predict(X)[0])
                # SHAP кадра (TreeSHAP через pred_contrib — дёшево на
                # одном снапшоте): потребители прикладывают топ-вклады к
                # DetectedError в отчёте (Гл. 6.2, интерпретируемость).
                drivers = explain_matrix(model.booster, X,
                                         model.features, k=3)[0]
            PREDICTIONS.labels("stream").inc()
            yield services_pb2.WinProbability(
                game_time=frame.game_time,
                radiant=wp,
                confidence=_confidence(wp),
                top_contributions=[
                    services_pb2.FeatureContribution(
                        feature_name=name, contribution=val)
                    for name, val in drivers],
            )


def _stage_version_of(spec: str | os.PathLike) -> str | None:
    """Версия стейджа для `registry://name/stage`; None для всего прочего.

    Локальный путь версии не имеет, и это не ошибка: слот с файловой
    моделью просто никогда не переключается. Точная версия
    (`registry://name/0.9.0-...`) — тоже неподвижная цель, и опрашивать её
    смысла нет: она по определению не меняется.
    """
    spec_s = str(spec)
    if not spec_s.startswith("registry://"):
        return None
    from registry import registry_from_env

    name, _, ref = spec_s[len("registry://"):].partition("/")
    return registry_from_env().stage_version(name, ref or "production")


def _resolve_model_path(spec: str | os.PathLike) -> str | os.PathLike:
    """`registry://name/ref` → скачать из реестра во временный файл;
    иначе — локальный путь как есть."""
    spec_s = str(spec)
    if not spec_s.startswith("registry://"):
        return spec
    import tempfile

    from registry import registry_from_env

    name, _, ref = spec_s[len("registry://"):].partition("/")
    artifact, meta = registry_from_env().resolve(name, ref or "production")
    tmp = tempfile.NamedTemporaryFile(suffix=".pkl", delete=False)
    tmp.write(artifact)
    tmp.close()
    logger.info("model resolved from registry: %s (%s)",
                meta.get("registry_version"), spec_s)
    return tmp.name


def build_server(model_path: str | os.PathLike, port: int) -> tuple[grpc.Server, int]:
    """Собрать сервер; port=0 выбирает свободный порт (для тестов).

    Отсутствие основной модели сервер НЕ роняет: на чистой машине реестр
    пуст по определению, обучение идёт после сбора. Первая попытка
    делается здесь, чтобы предупреждение попало в лог сразу при старте, а
    не при первом запросе через сутки.
    """
    slot = ModelSlot(model_path,
                     retry_after_s=float(os.getenv("MODEL_RETRY_S", "60")),
                     refresh_after_s=float(os.getenv("MODEL_REFRESH_S", "300")))
    slot.get()
    # Дополнительные модели: NAME_MODEL_PATH из окружения (пусто/сбой —
    # сервим без них, Predict вернёт NOT_FOUND по этому имени).
    extra: dict[str, WinProbability] = {}
    for name, env, default in (
            ("death_risk", "DEATH_RISK_MODEL_PATH",
             "registry://death_risk/production"),
            ("laning", "LANING_MODEL_PATH", "registry://laning/production")):
        spec = os.getenv(env, default)
        if not spec:
            continue
        try:
            extra[name] = WinProbability(_resolve_model_path(spec))
            logger.info("extra model %s loaded (%s)", name, spec)
        except Exception as e:  # noqa: BLE001 — опциональная модель
            logger.warning("%s model unavailable (%s): %s", name, spec, e)
    # SO_REUSEPORT выключен: gRPC по умолчанию позволяет НЕСКОЛЬКИМ
    # процессам слушать один порт, и ядро молча балансирует соединения
    # между ними — задвоенный сервер со старой моделью отдавал бы часть
    # ответов незаметно. Пусть второй запуск падает с "address in use".
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8),
                         options=[("grpc.so_reuseport", 0)])
    services_pb2_grpc.add_MLServiceServicer_to_server(
        MLService(slot, extra), server)
    bound = manta_grpc.add_port(server, port, "ml-service")
    return server, bound


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("GRPC_PORT", "50051")))
    ap.add_argument("--model", default=os.getenv("MODEL_PATH", str(DEFAULT_MODEL)))
    args = ap.parse_args()

    metrics_port = int(os.getenv("METRICS_PORT", "9104"))
    if metrics_port:
        manta_grpc.serve_metrics(metrics_port, "ml-service")
    server, port = build_server(args.model, args.port)
    server.start()
    logger.info("ml-service gRPC listening on :%d (model %s)", port, args.model)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
