FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

# Runs as root inside the container only so it can write to /share (owned by
# root on HA OS). The container has no host network, no HA API access and no
# extra privileges; see config.yaml.
WORKDIR /app
EXPOSE 8099
CMD ["python3", "/app/server.py"]
