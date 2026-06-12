FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# State DB volume mount point
VOLUME ["/app/data"]
ENV STATE_DB_PATH=/app/data/trading_state.db

# Non-root user for security
RUN useradd -m trader && chown -R trader:trader /app
USER trader

ENTRYPOINT ["python", "main.py"]
CMD ["--dry-run"]
