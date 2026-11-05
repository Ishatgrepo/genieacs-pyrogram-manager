FROM python:3.12-slim AS build
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml requirements.lock ./
COPY app ./app
RUN pip wheel --wheel-dir=/wheels -r requirements.lock .

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels genieacs-pyrogram-manager && rm -rf /wheels
COPY --chown=app:app entrypoint.sh /app/entrypoint.sh
USER app
ENV HOME=/home/app PYTHONUNBUFFERED=1 APP_PORT=7860 MPLCONFIGDIR=/tmp/matplotlib
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz',timeout=4)"
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
