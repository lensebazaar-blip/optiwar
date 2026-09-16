#!/bin/bash
# Daily retention for face captures: expires scans nobody assigned within
# FACE_PENDING_SCAN_RETENTION_DAYS and deletes raw captures older than
# FACE_RAW_CAPTURE_RETENTION_DAYS (owner-set: 7 and 7). Measurements stay;
# only the photograph goes. Runs with exactly the gunicorn service's environment.
# Install: cp to /usr/local/sbin/, chmod 755, cron: 20 4 * * *  >> /var/log/optiwar/face_capture_purge.log
set -u
MAIN=$(systemctl show gunicorn -p MainPID --value)
[ -n "$MAIN" ] && [ "$MAIN" != 0 ] || { echo "$(date -u +%FT%TZ) gunicorn not running; purge skipped" >&2; exit 2; }
APP=/var/www/flask-optiwar-ow-release-090525/venv
exec /usr/bin/python3 - "$MAIN" "$APP" <<"PY"
import os, sys
pid, app = sys.argv[1], sys.argv[2]
env = dict(kv.split("=", 1) for kv in open("/proc/%s/environ" % pid, "rb").read().decode().split("\0") if "=" in kv)
code = ("from flaskr import create_app; from flaskr.db import get_db; from flaskr import face_profiles; import datetime\n"
        "app = create_app()\n"
        "with app.test_request_context(base_url=\"https://optiwar.com/\"):\n"
        "    r = face_profiles.run_retention(get_db(), app.root_path)\n"
        "print(datetime.datetime.utcnow().strftime(\"%Y-%m-%dT%H:%M:%SZ\"), \"face_capture_purge\", r)\n")
os.execve(app + "/bin/python", [app + "/bin/python", "-c", code], env)
PY
