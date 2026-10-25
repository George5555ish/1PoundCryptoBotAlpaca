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

# Paper orders after the risk gate approves them. Override to
# python main.py for a dry run. The paper host is still required.
CMD ["python", "main.py", "--send-orders"]
