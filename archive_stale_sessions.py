#!/usr/bin/env python3
"""
Archive stale chat sessions — runs hourly via cron.
Moves sessions to 'archived' status after 24h of inactivity.

Cron entry:
0 * * * * /var/www/flask-optiwar-ow-release-090525/venv/bin/python /var/www/flask-optiwar-ow-release-090525/venv/lib/python3.11/site-packages/flaskr/archive_stale_sessions.py >> /var/log/optiwar/archive_cron.log 2>&1
"""
import MySQLdb
import os
import MySQLdb.cursors
from datetime import datetime

DB_CONFIG = {
    'host': 'localhost',
    'user': 'oslb6',
    'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'optiwar2',
}

def archive_stale_sessions():
    db = MySQLdb.connect(
        **DB_CONFIG,
        cursorclass=MySQLdb.cursors.DictCursor,
        charset='utf8mb4',
        autocommit=True,
    )
    cur = db.cursor()

    # Archive resolved sessions older than 24h
    cur.execute("""
        UPDATE chat_sessions SET status = 'archived'
        WHERE status = 'resolved'
          AND resolved_at < NOW() - INTERVAL 24 HOUR
    """)
    resolved_count = cur.rowcount

    # Archive human_open sessions with no activity for 24h
    cur.execute("""
        UPDATE chat_sessions SET status = 'archived'
        WHERE status = 'human_open'
          AND last_activity < NOW() - INTERVAL 24 HOUR
    """)
    human_open_count = cur.rowcount

    # Archive abandoned/ai_pending older than 24h
    cur.execute("""
        UPDATE chat_sessions SET status = 'archived'
        WHERE status IN ('abandoned', 'ai_pending')
          AND last_activity < NOW() - INTERVAL 24 HOUR
    """)
    other_count = cur.rowcount

    total = resolved_count + human_open_count + other_count
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] Archived {total} sessions (resolved:{resolved_count}, human_open:{human_open_count}, other:{other_count})")

    db.close()

if __name__ == '__main__':
    archive_stale_sessions()
