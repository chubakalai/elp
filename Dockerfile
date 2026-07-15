FROM python:3.12-slim

# tzdata is required for zoneinfo.ZoneInfo("America/New_York") to resolve --
# slim images do not ship the IANA tz database by default.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV MUSKMETER_DATA_DIR=/data
ENV MUSKMETER_CONFIG_PATH=/data/config.json
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "start.py"]
