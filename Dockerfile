# syntax=docker/dockerfile:1

# Debian-based (glibc) image: pyosmium ships manylinux wheels, so no compiler is needed.
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# `docker build --target test .` runs the test suite inside the image.
FROM base AS test
COPY . .
RUN python -m unittest discover -s tests -t .

FROM base AS runtime
RUN useradd --uid 10001 --create-home bandobuddy \
    && mkdir /data \
    && chown bandobuddy:bandobuddy /data
COPY --chown=bandobuddy:bandobuddy bandobuddy ./bandobuddy
USER bandobuddy

# The database and the ~2.3 GB OpenStreetMap extract live in /data: mount a volume there.
ENV BANDOBUDDY_DATA=/data \
    BANDOBUDDY_HOST=0.0.0.0 \
    BANDOBUDDY_PORT=8642 \
    BANDOBUDDY_NO_BROWSER=1
VOLUME ["/data"]
EXPOSE 8642

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8642/healthz', timeout=4)"]

ENTRYPOINT ["python", "-m", "bandobuddy"]
