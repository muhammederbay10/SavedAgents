# Postgres 16 with PostGIS and pgvector. No single official image ships both,
# so both extensions are installed from the PostgreSQL apt repository that the
# official image already uses.
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-16-postgis-3 \
        postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*

COPY init-extensions.sql /docker-entrypoint-initdb.d/
