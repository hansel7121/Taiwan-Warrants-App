# Coolify deploy target for the Hetzner migration (docs/adr/0002-hetzner-deploy-coolify-docker.md).
# Single container, single gunicorn worker — matches render.yaml's startCommand exactly.

# Stage 1: compile the Rust IV engine (rust/warrants_core) into an abi3 wheel.
# Kept in its own stage so the runtime image carries no Rust toolchain.
FROM python:3.11-slim AS rust-build
ENV PATH=/root/.cargo/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable --no-modify-path
RUN pip install --no-cache-dir "maturin>=1.7,<2.0"
WORKDIR /build
COPY rust/warrants_core/ ./warrants_core/
RUN cd warrants_core && maturin build --release --out /wheels

FROM python:3.11-slim

ENV TZ=Asia/Taipei \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

COPY requirements.txt .
COPY vendor/ ./vendor/
RUN pip install --no-cache-dir -r requirements.txt

# The compiled IV engine. logic/iv_engine.py falls back to logic/bs_python.py if
# this is ever absent, so the app stays correct — just slower.
COPY --from=rust-build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY . .

EXPOSE 5001

CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "240", "-b", "0.0.0.0:5001", "wsgi:app"]
