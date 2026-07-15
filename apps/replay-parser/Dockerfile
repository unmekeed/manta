# Replay Parser: C++ ядро (demoinfo) + Go-обвязка (parser-svc) в одном
# образе — сервис запускает ядро как подпроцесс (Гл. 5, Гл. 13.4).

FROM debian:bookworm-slim AS cpp-build
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake g++ make libsnappy-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY CMakeLists.txt ./
COPY include include
COPY src src
COPY tests tests
RUN cmake -B build -S . -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build -j"$(nproc)" \
    && ctest --test-dir build --output-on-failure

FROM golang:1.24 AS go-build
WORKDIR /src
COPY svc/go.mod svc/go.sum ./
RUN go mod download
COPY svc .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/parser-svc ./cmd/parser-svc

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libsnappy1v5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 65532 parser
COPY --from=cpp-build /src/build/demoinfo /usr/local/bin/demoinfo
COPY --from=go-build /out/parser-svc /usr/local/bin/parser-svc
ENV DEMOINFO_PATH=/usr/local/bin/demoinfo WORK_DIR=/tmp
USER parser
ENTRYPOINT ["/usr/local/bin/parser-svc"]
