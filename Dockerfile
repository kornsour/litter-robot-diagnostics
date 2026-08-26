# Runtime image for the always-on autoreset watchdog (and, optionally, capture).
#
# There is no OS keyring in a container, so credentials arrive as environment
# variables. auth.py already falls back to WHISKER_USERNAME / WHISKER_PASSWORD
# when the keyring is unavailable; refreshed tokens simply are not persisted,
# and the container re-authenticates on restart.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Unprivileged, and owning the volume mount point so SQLite can write there.
RUN useradd --create-home --uid 10001 watchdog \
    && mkdir -p /data \
    && chown watchdog:watchdog /data
USER watchdog

VOLUME ["/data"]

# Detection-only by default. Add --arm in compose (or on the command line) to
# let the watchdog actually send commands to the unit.
ENTRYPOINT ["lr4-diagnostics"]
CMD ["autoreset", "--database", "/data/lr4-diagnostics.sqlite"]
