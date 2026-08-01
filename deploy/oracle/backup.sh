#!/bin/sh
set -eu

database=/var/lib/no-comment-booking/data/service.db
backup_dir=/var/backups/no-comment-booking
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary="$backup_dir/service-$timestamp.db.tmp"
output="$backup_dir/service-$timestamp.db.gz"

install -d -m 0700 "$backup_dir"
if [ ! -f "$database" ]; then
  exit 0
fi

sqlite3 "$database" ".backup '$temporary'"
gzip -9 "$temporary"
mv "$temporary.gz" "$output"
chmod 0600 "$output"
find "$backup_dir" -type f -name 'service-*.db.gz' -mtime +14 -delete
