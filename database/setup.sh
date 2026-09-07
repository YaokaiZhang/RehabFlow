#!/bin/bash
# setup.sh
set -e

echo "=== 1. Creating local data directories (Volumes) ==="
mkdir -p data/postgres data/qdrant

# echo "=== 2. Installing PostgreSQL and Redis via Conda ==="
# # Note: Ensure your preferred Conda environment is activated before running this!
# # conda install -y -c conda-forge postgresql redis

# echo "=== 3. Downloading Qdrant ==="
# if [ ! -f ./qdrant ]; then
#     echo "Fetching latest Qdrant release..."
#     curl -L https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-unknown-linux-gnu.tar.gz | tar xz
#     chmod +x qdrant
# else
#     echo "Qdrant binary already exists. Skipping."
# fi

echo "=== 4. Initializing PostgreSQL Database ==="
if [ ! -s data/postgres/PG_VERSION ]; then
    initdb -D data/postgres
    
    # Temporarily start the server to create the user and database
    echo "Configuring Postgres credentials..."
    pg_ctl -D data/postgres -l data/postgres/setup.log start
    sleep 2 # Give it a moment to boot
    
    # Replicating the POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB environment variables
    psql -d postgres -c "CREATE USER rehab WITH PASSWORD 'rehab';"
    psql -d postgres -c "CREATE DATABASE rehab_agent OWNER rehab;"
    psql -d postgres -c "CREATE DATABASE rehab_agent_quality OWNER rehab;"
    
    # Stop the temporary server
    pg_ctl -D data/postgres stop
    echo "Database configured successfully."
else
    echo "PostgreSQL already initialized in data/postgres. Skipping."
fi

echo "=== Setup Complete! Run ./start.sh to launch services. ==="