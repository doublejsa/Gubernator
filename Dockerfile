# Gubernator app image (works on amd64 and arm64)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Build deps for paramiko/cryptography/onnxruntime, plus postgres client for backups
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r backend/requirements.txt

# App code
COPY backend /app/backend
COPY frontend /app/frontend
COPY prompt_guide.md run.py setup_paypal_plan.py setup_paypal_webhook.py send_trial_reminders.py /app/

# Pre-download the embedding model at build time so first request isn't slow
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" || true

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
