FROM python:3.12-slim

# libgomp1 is required by onnxruntime; nothing else is needed because we use
# the headless OpenCV build (no libGL).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    DOCKETSCAN_DATA=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY docketscan ./docketscan
COPY static ./static

# writable data dir (mount persistent storage here to keep records)
RUN mkdir -p /data && chmod 777 /data

EXPOSE 7860

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}"]
