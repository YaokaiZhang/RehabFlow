ARG IMAGE_REGISTRY=docker.io
FROM ${IMAGE_REGISTRY}/library/python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY backend /app/backend
COPY alembic.ini /app/alembic.ini
COPY scripts /app/scripts

WORKDIR /app/backend
EXPOSE 8000
CMD ["sh", "-c", "alembic -c ../alembic.ini upgrade head && PYTHONPATH=. python scripts/setup_langgraph_checkpoints.py && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --loop asyncio --http h11"]
