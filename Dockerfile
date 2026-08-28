# Wind Fleet Monitor — container image.
# This version ships no live NWP provider: NWP_PROVIDER stays "stub", the wind rose is drawn
# from telemetry, and the HRRR scientific stack (herbie-data / xarray / cfgrib / eccodes) is
# not installed. That keeps the image small and needs no GRIB system libraries.

# Pinned to amd64: it is Cloud Run's runtime arch, and timezonefinder ships x86_64 wheels only
# — an arm64 build would try to compile it from source and fail in this compiler-less slim
# image. On Apple Silicon this build runs under emulation.
FROM --platform=linux/amd64 python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # --- app configuration (config.py reads these from the environment) ---
    NWP_PROVIDER=stub \
    DUCKDB_PATH=/tmp/fleet.duckdb \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Cloud Run (and most PaaS) route traffic to $PORT; default 8080. Streamlit needs it passed
# explicitly, so the CMD is shell form to expand the variable.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true"]
