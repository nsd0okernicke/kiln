#!/bin/bash
# Clear all messages from the Kiln message database
# Usage: ./clear-messages.sh [PROJECT_ROOT]

set -euo pipefail

PROJECT_ROOT="${1:-.}"
DB_PATH="$PROJECT_ROOT/.Kiln/messages.db"

if [[ ! -f "$DB_PATH" ]]; then
    echo "Error: No messages.db found at $DB_PATH" >&2
    exit 1
fi

echo "Clearing all messages from: $DB_PATH"
echo ""

python << PYTHON
import sqlite3
conn = sqlite3.connect(r'$DB_PATH')
cursor = conn.cursor()

# Get count before
cursor.execute('SELECT COUNT(*) FROM messages')
before = cursor.fetchone()[0]
print(f'Messages before: {before}')

# Delete all
cursor.execute('DELETE FROM messages')
conn.commit()

# Get count after
cursor.execute('SELECT COUNT(*) FROM messages')
after = cursor.fetchone()[0]
print(f'Messages after: {after}')

conn.close()
PYTHON

echo ""
echo "✓ Database cleared"

