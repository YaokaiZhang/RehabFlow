#!/bin/bash
# start.sh

echo "Starting Local Backend Services..."

# 1. Start Redis
echo "=> Starting Redis (Port 6379)..."
redis-server --daemonize yes --dir ./data --pidfile ./data/redis.pid

# 2. Start PostgreSQL
echo "=> Starting PostgreSQL (Port 5432)..."
pg_ctl -D data/postgres -l data/postgres/server.log start

# 3. Start Qdrant
echo "=> Starting Qdrant (Ports 6333, 6334)..."
export QDRANT__STORAGE__STORAGE_PATH="./data/qdrant"
nohup ./qdrant > data/qdrant/server.log 2>&1 &
echo $! > data/qdrant/qdrant.pid

echo "All services are running in the background."