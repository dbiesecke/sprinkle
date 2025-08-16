# syntax=docker/dockerfile:1
FROM python:3.11

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RCLONE_CONFIG=/config/rclone.conf \
    SPRINKLE_CONFIG=/config/sprinkle.conf \
    TZ=Etc/UTC

# Besseres non-interactive apt
ARG DEBIAN_FRONTEND=noninteractive

# rclone + tini (sauberes PID1) + Basics
RUN apt-get update && apt-get install -y --no-install-recommends \
      rclone tzdata ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Erst Requirements, dann Code – bessere Layer-Caches
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . ./

# Falls setup.py/pyproject Entry Points liefert (unschädlich, auch wenn nicht)
RUN pip install .

RUN curl https://rclone.org/install.sh | bash

# Unprivilegierter Nutzer
RUN useradd -u 10001 -m -d /home/sprinkle -s /usr/sbin/nologin sprinkle \
    && mkdir -p /config /data \
    && chown -R sprinkle:sprinkle /app /config /data

VOLUME ["/config", "/data"]

USER sprinkle

# Standardmäßig zeigt der Container die CLI-Hilfe.
# Beim Aufruf kannst du hinten die Sprinkle-Subcommands anhängen (ls, backup, …).
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/app/sprinkle.py", "-c", "/config/sprinkle.conf"]
CMD ["--help"]
