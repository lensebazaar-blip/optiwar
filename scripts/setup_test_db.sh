#!/usr/bin/env bash
# Create the local MariaDB user and database the DB-backed tests and the chat
# harness use. Idempotent, so it is safe to run on every boot of a dev box.
#
#   sudo scripts/setup_test_db.sh          # local box (uses root socket auth)
#   OPTIWAR_TEST_DB_ROOT_PW=root ...       # CI, where root has a password
#
# The ACR tables themselves are not created here: acr.ensure_schema() creates
# them, and tests/integration exercising that path is the point.
set -euo pipefail

DB="${OPTIWAR_TEST_DB_NAME:-optiwar2}"
USER="${OPTIWAR_TEST_DB_USER:-oslb6}"
PW="${OPTIWAR_TEST_DB_PASSWORD:-testpw}"
HOST="${OPTIWAR_TEST_DB_HOST:-127.0.0.1}"

mysql_root() {
  if [ -n "${OPTIWAR_TEST_DB_ROOT_PW:-}" ]; then
    mysql -h "$HOST" -u root -p"$OPTIWAR_TEST_DB_ROOT_PW" "$@"
  else
    mysql -u root "$@"
  fi
}

mysql_root <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB}\`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER IF NOT EXISTS '${USER}'@'%' IDENTIFIED BY '${PW}';
CREATE USER IF NOT EXISTS '${USER}'@'localhost' IDENTIFIED BY '${PW}';
GRANT ALL PRIVILEGES ON \`${DB}\`.* TO '${USER}'@'%';
GRANT ALL PRIVILEGES ON \`${DB}\`.* TO '${USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "test database ${DB} ready for ${USER}@${HOST}"
