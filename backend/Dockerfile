FROM python:3.11-slim

RUN groupadd -r botuser && useradd -r -g botuser botuser

# curl is used by HEALTHCHECK below. The previous "python -c urllib.request"
# implementation took 200-400ms just to import urllib + start a Python
# interpreter, which under host CPU contention would itself blow past the
# 10s healthcheck timeout and cascade into container restarts (BUG-029).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R botuser:botuser /app
USER botuser

# 60s interval, 20s timeout, 5min start-period, 5 retries before kill.
# Under a CPU spike the bot can take a few seconds to respond — the old
# 30s/10s/3 settings restarted us aggressively and made the spike worse.
HEALTHCHECK --interval=60s --timeout=20s --start-period=300s --retries=5 \
    CMD curl --silent --fail --max-time 15 http://localhost:8000/api/health || exit 1

CMD ["python", "-u", "main.py"]
