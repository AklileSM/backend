FROM python:3.11-slim

WORKDIR /app

# Runtime library needed by PotreeConverter (OpenMP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget unzip libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install PotreeConverter 2.1.n — pre-built Linux binary
# If the download URL changes, override POTREE_CONVERTER_URL at build time.
ARG POTREE_CONVERTER_URL="https://github.com/potree/PotreeConverter/releases/download/2.1.n/PotreeConverter_linux.zip"
RUN wget -q "$POTREE_CONVERTER_URL" -O /tmp/pc.zip \
    && mkdir -p /opt/potree \
    && unzip /tmp/pc.zip -d /opt/potree \
    && find /opt/potree -name "PotreeConverter" -type f -exec chmod +x {} \; \
    && rm /tmp/pc.zip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY run.py ./run.py
COPY schema.sql ./schema.sql

EXPOSE 3001

CMD ["python", "run.py"]
