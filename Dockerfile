# Coolify deploy target for the Hetzner migration (docs/adr/0002-hetzner-deploy-coolify-docker.md).
# Single container, single gunicorn worker — matches render.yaml's startCommand exactly.
FROM python:3.11-slim

ENV TZ=Asia/Taipei \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5001

CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "240", "-b", "0.0.0.0:5001", "wsgi:app"]
