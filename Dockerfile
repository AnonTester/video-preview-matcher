FROM python:3.13-slim-trixie

# ffmpeg/ffprobe: probing + scene extraction + frame dumping
# libchromaprint-tools: provides fpcalc for audio fingerprinting
# mesa-va-drivers/libva2/libva-drm2: VAAPI decode on an AMD GPU
#   (radeonsi driver — if you have one, check `vainfo` lists H.264/HEVC
#   VAAPI decode entrypoints before relying on --hwaccel vaapi; harmless
#   to install even without a GPU, decode just falls back to software)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libchromaprint-tools \
        mesa-va-drivers \
        libva2 \
        libva-drm2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY templates/ templates/
COPY static/ static/
COPY VERSION .

EXPOSE 8000

# Generic defaults assuming a single bind-mounted /data volume for app
# state (see docker-compose.yml) — 04_serve.py reads these via DB_PATH/
# STAGE_DIR if set, falling back to data/library.db and <project>/
# _to_delete otherwise. Override with `environment:` in your compose
# file if you want a different layout (e.g. staging on the same
# filesystem as your library — see README's "Docker deployment" section
# for why that matters).
ENV DB_PATH=/data/library.db
ENV STAGE_DIR=/data/_to_delete

# Default command runs the review server. The pipeline stages (01/02/03)
# are one-off jobs, not long-running services — invoke them against this
# same image with `docker compose run --rm app python3 src/01_inventory.py ...`
# (see rebuild.sh / README) rather than baking them into CMD.
CMD ["python3", "src/04_serve.py", "--host", "0.0.0.0", "--port", "8000"]
