# check=skip=SecretsUsedInArgOrEnv
# (VITE_API_KEY below is the public development key, not a secret; see its comment.)
# The Relay: one image for the API + web UI (`relay serve`) and for the stand-in model
# service (compose.yaml runs it twice). Build: docker build -t the-relay .
#
# Stage 1 builds the React UI. Stage 2 is the Python service, with the UI copied into
# src/relay/app/static/ (where `relay serve` serves it). Packages are installed at the exact
# versions the test suite ran with (docker/constraints.txt), on Python 3.12: the pinned
# numpy 2.5 and PyMuPDF 2.1 publish no Python 3.11 builds.

# --- Stage 1: the web UI ---------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# The UI's Bearer key is baked in at build time. The default is the public development key,
# which the server accepts only when [auth] dev_key_enabled allows it
# (docker/relay.compose.toml). Never pass a real key here: it would ship inside the image.
ARG VITE_API_KEY=relay-dev-key
RUN VITE_API_KEY="${VITE_API_KEY}" npm run build

# --- Stage 2: the service --------------------------------------------------------------------
FROM python:3.12-slim AS service
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /app

# Dependencies first (a cached layer while only the code changes).
COPY pyproject.toml README.md ./
COPY docker/constraints.txt docker/constraints.txt
COPY src/relay/__init__.py src/relay/__init__.py
RUN pip install -c docker/constraints.txt ".[redis]" && pip uninstall -y the-relay

# The code, the tracked corpus manifests, the stand-in calibration profile, the scripts that
# build the sample indexes on first start, and the UI built in stage 1.
COPY src/ src/
COPY scripts/ scripts/
COPY data/papers/manifest.json data/papers/manifest.json
COPY data/retrieval_manifest.json data/retrieval_manifest.json
COPY evidence/standin_calibration/ evidence/standin_calibration/
COPY relay.example.toml ./
COPY docker/entrypoint.sh docker/entrypoint.sh
COPY --from=web /web/dist/ src/relay/app/static/
RUN pip install --no-deps -e . \
    && sed -i 's/\r$//' docker/entrypoint.sh && chmod 0755 docker/entrypoint.sh \
    && useradd --create-home --uid 1000 relay \
    && mkdir -p data && chown -R relay:relay data

USER relay
EXPOSE 8000
# The first start downloads the papers and models and builds the indexes (several minutes);
# failures inside the start period do not count, and the first success ends it.
HEALTHCHECK --interval=15s --timeout=5s --start-period=15m --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["relay", "serve", "--host", "0.0.0.0", "--port", "8000"]
