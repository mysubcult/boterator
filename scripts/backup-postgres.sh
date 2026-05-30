#!/bin/sh
set -eu

BACKUP_DIR="${BACKUP_DIR:-/opt/boterator/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
PROJECT_DIR="${PROJECT_DIR:-/opt/boterator}"

mkdir -p "$BACKUP_DIR"

cd "$PROJECT_DIR"
file="$BACKUP_DIR/boterator-$(date -u +%Y%m%d-%H%M%S).sql.gz"

docker compose exec -T postgres pg_dump -U boterator boterator | gzip -9 > "$file"
find "$BACKUP_DIR" -type f -name 'boterator-*.sql.gz' -mtime "+$RETENTION_DAYS" -delete

echo "Backup written to $file"
