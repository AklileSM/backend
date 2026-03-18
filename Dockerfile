FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY run.py ./run.py
COPY schema.sql ./schema.sql

EXPOSE 3001

CMD ["python", "run.py"]
