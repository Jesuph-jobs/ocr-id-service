FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv==0.11.26

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && groupadd --system --gid 10001 ocr \
    && useradd --system --uid 10001 --gid ocr --create-home --home-dir /home/ocr ocr \
    && mkdir -p /app/data/input /app/data/output/debug /home/ocr/.paddlex \
    && chown -R ocr:ocr /app /home/ocr

COPY --chown=ocr:ocr app ./app

ENV HOME=/home/ocr \
    PATH=/app/.venv/bin:$PATH

USER ocr

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.main"]
