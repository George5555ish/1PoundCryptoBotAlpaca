FROM python:3.11-slim

# stdout is the platform log. The decision database and charts live on
# DATA_DIR, which should be a mounted volume so a redeploy does not wipe them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data

# Dry-run unless the platform start command adds --send-orders.
# Paper host is still required. A live Alpaca URL is refused at startup.
CMD ["python", "main.py"]
