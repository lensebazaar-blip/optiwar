#!/usr/bin/env bash
# Optiwar AI Glasses build: local server on https://127.0.0.1:8010 (self-signed). Ctrl+C to stop.
# optiwar.in behaviour: send Host optiwar.in (curl -k -H 'Host: optiwar.in' https://127.0.0.1:8010/).
cd "$(dirname "$0")"
source ./.env.local
exec <venv>/bin/flask --app local_app:app run --host 127.0.0.1 --port 8010 --cert=adhoc
