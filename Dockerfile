# Farmer: one container per farmer, with farm.py as the entrypoint.
#
# Build:
#   docker build -t farmer .
#
# Run (config.yaml holds credentials, so it's mounted at runtime, never baked in):
#   docker run -d --name mnq_noise_area --network host \
#     -v "$PWD/config.yaml:/app/config.yaml:ro" \
#     --stop-timeout 30 \
#     farmer --name mnq_noise_area --level info
#
# --network host lets "localhost" in config.yaml reach Redis on the host, as it does
# outside Docker. docker stop sends SIGINT (see STOPSIGNAL), which the farmer handles
# like Ctrl+C: it closes any open position before exiting.

# --- Build stage: dependencies into a virtualenv (git is needed for projectx-client) ---
FROM python:3.12-slim AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies first, so code changes don't reinstall them
WORKDIR /build
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip setuptools \
    && python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

# --- Runtime stage ---
FROM python:3.12-slim

# tzdata: strategies convert timestamps to America/Chicago
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Chicago

RUN useradd --create-home --uid 1000 farmer
WORKDIR /app
COPY --chown=farmer:farmer . .
USER farmer

# The farmer flattens open positions on KeyboardInterrupt (SIGINT), not SIGTERM
STOPSIGNAL SIGINT

ENTRYPOINT ["python", "farm.py"]
CMD ["--help"]
