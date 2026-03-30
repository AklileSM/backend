FROM python:3.11-slim

WORKDIR /app

# Runtime libraries needed by PotreeConverter (OpenMP, C++ STL, Intel TBB, LASzip)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget unzip libgomp1 libstdc++6 libtbb12 liblaszip-dev \
    && rm -rf /var/lib/apt/lists/*

# Install PotreeConverter 2.1.2 — pre-built Linux binary.
# After extraction, symlink the binary to /usr/local/bin so it is on PATH
# regardless of the directory name inside the zip.
ARG POTREE_CONVERTER_URL="https://github.com/potree/PotreeConverter/releases/download/2.1.2/PotreeConverter_2.1.2_x64_linux.zip"
RUN wget -q "$POTREE_CONVERTER_URL" -O /tmp/pc.zip \
    && mkdir -p /opt/potree \
    && unzip /tmp/pc.zip -d /opt/potree \
    && POTREE_BIN=$(find /opt/potree -name "PotreeConverter" -type f | head -1) \
    && chmod +x "$POTREE_BIN" \
    && ln -sf "$POTREE_BIN" /usr/local/bin/PotreeConverter \
    && rm /tmp/pc.zip \
    && echo "PotreeConverter installed at: $POTREE_BIN" \
    && ldd /usr/local/bin/PotreeConverter

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY run.py ./run.py
COPY schema.sql ./schema.sql

EXPOSE 3001

CMD ["python", "run.py"]
