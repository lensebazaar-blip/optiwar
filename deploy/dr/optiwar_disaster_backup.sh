#!/bin/bash
# Optiwar — comprehensive disaster-recovery bundle.
#
# Produces /root/dr_backups/optiwar_dr_<ts>.tar, one self-describing archive
# from which a clean Rocky Linux host can be rebuilt with
# optiwar_restore_server.sh. Everything sensitive (EnvironmentFiles, the
# gunicorn unit and its drop-ins, TLS private keys, MySQL grants, customer
# uploads) lives in ONE gpg-encrypted sub-archive; the rest is plain so the
# inventory can be read without the passphrase.
#
# Exit status is truthful: 0 only if every mandatory stage AND its
# verification passed. Anything else is "backup incomplete". A summary of the
# run is written to $DR_DIR/latest_status.json for the daily report.
#
# Usage: optiwar_disaster_backup.sh [--images] [--skip-restore-test]
#   --images             also archive flaskr/static (1.6 GB, changes rarely)
#   --skip-restore-test  do not load the dump into a scratch database
#
# Config (root-only, 0600): /etc/optiwar/dr.env
#   DR_PASSPHRASE_FILE   file holding the gpg passphrase (0600, required)
#   DR_DIR               bundle directory            (default /root/dr_backups)
#   DR_KEEP_BUNDLES      local bundles to keep       (default 7)
#   DR_KEEP_IMAGE_TARS   local image archives to keep(default 1)
#
# No secret value is ever printed; the DB password reaches mysqldump through a
# 0600 defaults file, the passphrase reaches gpg through a file descriptor.
set -u
umask 077

APP_ROOT=/var/www/flask-optiwar-ow-release-090525
FLASKR=$APP_ROOT/venv/lib/python3.11/site-packages/flaskr
VENV=$APP_ROOT/venv
RELEASES=/root/deploy_releases
PARKED_IMAGES=/root/lens_sources/incoming        # read-only, never touched
CONF=/etc/optiwar/dr.env

WANT_IMAGES=0; RESTORE_TEST=1
for a in "$@"; do
  case "$a" in
    --images) WANT_IMAGES=1 ;;
    --skip-restore-test) RESTORE_TEST=0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

[ -r "$CONF" ] && . "$CONF"
DR_DIR=${DR_DIR:-/root/dr_backups}
DR_KEEP_BUNDLES=${DR_KEEP_BUNDLES:-7}
DR_KEEP_IMAGE_TARS=${DR_KEEP_IMAGE_TARS:-1}
DR_PASSPHRASE_FILE=${DR_PASSPHRASE_FILE:-}

TS=$(date +%Y%m%d-%H%M%S)
HOST=$(hostname)
WORK=$DR_DIR/optiwar_dr_$TS
BUNDLE=$DR_DIR/optiwar_dr_$TS.tar
LOG=$DR_DIR/optiwar_dr_$TS.log
mkdir -p "$DR_DIR" "$WORK"/{db,app,system,inventory,secrets}
exec > >(tee -a "$LOG") 2>&1

declare -A STAGE
FAILED=0
ok()   { STAGE[$1]=OK;   echo "[OK]   $1 ${2:-}"; }
fail() { STAGE[$1]=FAIL; echo "[FAIL] $1 ${2:-}"; FAILED=1; }
note() { echo "       $*"; }

echo "== Optiwar DR backup $TS on $HOST =="

# ----------------------------------------------------------------- preflight
if [ -z "$DR_PASSPHRASE_FILE" ] || [ ! -r "$DR_PASSPHRASE_FILE" ]; then
  fail preflight "DR_PASSPHRASE_FILE missing/unreadable — refusing to write plaintext secrets"
  echo '{"ok":false,"stage":"preflight"}' > "$DR_DIR/latest_status.json"
  exit 3
fi
[ "$(stat -c %a "$DR_PASSPHRASE_FILE")" = "600" ] || note "WARNING: $DR_PASSPHRASE_FILE is not 0600"
FREE_MB=$(df -Pm "$DR_DIR" | awk 'NR==2{print $4}')
[ "$FREE_MB" -gt 2048 ] && ok preflight "${FREE_MB} MB free" || fail preflight "only ${FREE_MB} MB free"

# --------------------------------------------------------------- identity
REL_DIR=$(readlink -e "$RELEASES/previous" 2>/dev/null || true)
RELEASE=$(basename "${REL_DIR:-unknown}")
GIT_SHA=$(awk '/^repo /{print $NF}' "$REL_DIR/manifest.txt" 2>/dev/null || true)
GIT_SHA=${GIT_SHA:-unknown}
note "release=$RELEASE git=$GIT_SHA"

# --------------------------------------------------------------------- DB
eval "$(systemctl show gunicorn -p Environment | sed 's/^Environment=//' | tr ' ' '\n' \
        | grep -E '^MYSQL_(USER|PASSWORD|DB|HOST)=' | sed 's/^/export /')"
MYSQL_HOST=${MYSQL_HOST:-localhost}; MYSQL_DB=${MYSQL_DB:-optiwar2}
CNF=$(mktemp); chmod 600 "$CNF"
printf '[client]\nuser=%s\npassword=%s\nhost=%s\n' "$MYSQL_USER" "$MYSQL_PASSWORD" "$MYSQL_HOST" > "$CNF"
DUMP=$WORK/db/${MYSQL_DB}.sql.gz
mysqldump --defaults-extra-file="$CNF" --single-transaction --routines --triggers --events \
          --hex-blob --default-character-set=utf8mb4 "$MYSQL_DB" | gzip -6 > "$DUMP"
RC=${PIPESTATUS[0]}
if [ "$RC" = 0 ] && gzip -t "$DUMP" && zcat "$DUMP" | tail -1 | grep -q "Dump completed"; then
  ok db_dump "$(du -h "$DUMP" | cut -f1) $(zcat "$DUMP" | grep -c '^CREATE TABLE') tables"
  DUMP_BYTES=$(stat -c %s "$DUMP")
else
  DUMP_BYTES=0
  fail db_dump "mysqldump rc=$RC or trailer missing"
fi
mysql --defaults-extra-file="$CNF" -N -e \
  "SELECT table_name, table_rows FROM information_schema.tables WHERE table_schema='$MYSQL_DB' ORDER BY 1" \
  > "$WORK/db/table_rowcounts.txt" 2>/dev/null
mysql --defaults-extra-file="$CNF" -N -e \
  "SELECT default_character_set_name, default_collation_name FROM information_schema.schemata WHERE schema_name='$MYSQL_DB'" \
  > "$WORK/db/charset.txt" 2>/dev/null

# grants for the application users -> secrets (they carry password hashes)
{
  for u in $(mysql --defaults-extra-file="$CNF" -N -e \
      "SELECT CONCAT(\"'\",user,\"'@'\",host,\"'\") FROM mysql.user WHERE user IN ('$MYSQL_USER','oslb6','optiwaruser','optiwar_ro','optiwar_closer','reportuser')" 2>/dev/null); do
    mysql --defaults-extra-file="$CNF" -N -e "SHOW CREATE USER $u" 2>/dev/null | sed 's/$/;/'
    mysql --defaults-extra-file="$CNF" -N -e "SHOW GRANTS FOR $u" 2>/dev/null | sed 's/$/;/'
  done
} > "$WORK/secrets/mysql_grants.sql"
[ -s "$WORK/secrets/mysql_grants.sql" ] && ok db_grants "$(grep -c 'CREATE USER' "$WORK/secrets/mysql_grants.sql") users" || fail db_grants

if [ "$RESTORE_TEST" = 1 ] && [ "${STAGE[db_dump]}" = OK ]; then
  SCRATCH="${MYSQL_DB}_dr_test_$$"
  if mysql --defaults-extra-file="$CNF" -e "CREATE DATABASE \`$SCRATCH\` CHARACTER SET utf8mb4" 2>/dev/null; then
    zcat "$DUMP" | mysql --defaults-extra-file="$CNF" "$SCRATCH" 2>"$WORK/db/restore_test.err"
    S=$(mysql --defaults-extra-file="$CNF" -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$MYSQL_DB'")
    D=$(mysql --defaults-extra-file="$CNF" -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$SCRATCH'")
    SO=$(mysql --defaults-extra-file="$CNF" -N -e "SELECT COUNT(*) FROM $MYSQL_DB.orders" 2>/dev/null)
    DO=$(mysql --defaults-extra-file="$CNF" -N -e "SELECT COUNT(*) FROM $SCRATCH.orders" 2>/dev/null)
    mysql --defaults-extra-file="$CNF" -e "DROP DATABASE \`$SCRATCH\`" 2>/dev/null
    [ "$S" = "$D" ] && [ "$SO" = "$DO" ] && [ ! -s "$WORK/db/restore_test.err" ] \
      && ok db_restore_test "tables $S=$D orders $SO=$DO" \
      || fail db_restore_test "tables $S/$D orders $SO/$DO $(head -c 200 "$WORK/db/restore_test.err")"
  else
    fail db_restore_test "cannot create scratch database"
  fi
fi
rm -f "$CNF"

# -------------------------------------------------------------------- app
{
  echo "release=$RELEASE"; echo "git_commit=$GIT_SHA"; echo "app_root=$APP_ROOT"
  echo "python=$("$VENV/bin/python" --version 2>&1)"
  echo "gunicorn=$("$VENV/bin/gunicorn" --version 2>&1)"
} > "$WORK/app/RELEASE.txt"
"$VENV/bin/pip" freeze > "$WORK/app/requirements-freeze.txt" 2>/dev/null
tar czf "$WORK/app/flaskr_code.tar.gz" -C "$(dirname "$FLASKR")" \
    --exclude='flaskr/static' --exclude='flaskr/__pycache__' --exclude='*/__pycache__' \
    --exclude='flaskr/*.log' --exclude='flaskr/backups' \
    --exclude='*.bak*' --exclude='*.orig' --exclude='*.py.*' flaskr 2>/dev/null; TAR_RC=$?
# editor/deploy leftovers (*.bak*, *.py.pre_*, backups/) in the app tree hold historic credentials:
# listed here, never archived in plain; the release harness keeps real history in /root/deploy_releases
find "$FLASKR" -maxdepth 2 \( -name '*.bak*' -o -name '*.orig' -o -name '*.py.*' \) -printf '%p\n' | sort > "$WORK/app/excluded_bak_files.txt"
[ $TAR_RC -le 1 ] && tar tzf "$WORK/app/flaskr_code.tar.gz" >/dev/null 2>&1 \
  && ok app_code "$(du -h "$WORK/app/flaskr_code.tar.gz" | cut -f1)" || fail app_code
[ -d "$REL_DIR" ] && cp -a "$REL_DIR" "$WORK/app/last_release_backup" 2>/dev/null
[ -d "$RELEASES" ] && ls -1 "$RELEASES" > "$WORK/app/release_history.txt"

if [ "$WANT_IMAGES" = 1 ]; then
  IMG=$DR_DIR/optiwar_static_images_$TS.tar.gz
  tar czf "$IMG" -C "$FLASKR" static 2>/dev/null
  [ $? -le 1 ] && tar tzf "$IMG" >/dev/null 2>&1 && ok static_images "$(du -h "$IMG" | cut -f1) -> $IMG" || fail static_images
fi

# customer uploads (face captures, prescriptions) -> secrets
SU=$(dirname "$FLASKR")/secure_uploads
if [ -d "$SU" ]; then
  tar czf "$WORK/secrets/secure_uploads.tar.gz" -C "$(dirname "$SU")" secure_uploads 2>/dev/null
  [ $? -le 1 ] && ok secure_uploads "$(du -h "$WORK/secrets/secure_uploads.tar.gz" | cut -f1)" || fail secure_uploads
else
  note "secure_uploads absent"
fi

# ----------------------------------------------------------------- system
S=$WORK/system
copy_plain() { # copy_plain <path> [<path>...]  (missing paths are noted, not fatal)
  for p in "$@"; do
    if [ -e "$p" ]; then mkdir -p "$S$(dirname "$p")"; cp -a "$p" "$S$(dirname "$p")/" 2>/dev/null || note "copy failed: $p"
    else note "absent: $p"; fi
  done
}
copy_plain /etc/nginx /etc/redis.conf /etc/redis /etc/my.cnf /etc/my.cnf.d /etc/fail2ban \
           /etc/logrotate.d /etc/cron.d /etc/cron.daily /etc/cron.weekly /etc/cron.hourly \
           /etc/hosts /etc/sysctl.d /etc/php-fpm.d /etc/php.ini \
           /etc/letsencrypt/renewal /etc/letsencrypt/renewal-hooks /etc/letsencrypt/cli.ini \
           /usr/local/sbin /root/backups/optiwar_backup.sh /root/backups/optiwar_metrics_snapshot.sh \
           /root/backups/optiwar_alert_check.sh /root/backups/_enc.sh \
           /root/optiwar_disaster_backup.sh /root/optiwar_restore_server.sh /root/optiwar_dr_confirm_offhost.sh \
           /root/RESTORE_NOTES.txt
mkdir -p "$S/root"
tar czf "$S/root/root_venv.tar.gz" -C /root venv 2>/dev/null   # cron python env (no credentials)
# report/catalog/cron scripts under /root carry hard-coded credentials -> secrets
mkdir -p "$WORK/secrets/root"
tar czf "$WORK/secrets/root/reports_code.tar.gz" -C /root --exclude='reports/daily_*' --exclude='reports/reports/daily_*' \
    --exclude='*/__pycache__' --exclude='reports/_stale_*' --exclude='reports/backup_*' reports 2>/dev/null
tar czf "$WORK/secrets/root/catalog_code.tar.gz" -C /root --exclude='*/__pycache__' --exclude='*.log' catalog 2>/dev/null
cp -a /root/error_summary_cron.py /root/missing_order_search_copy.py "$WORK/secrets/root/" 2>/dev/null
mkdir -p "$S/cron"
for u in $(cut -d: -f1 /etc/passwd); do crontab -l -u "$u" > "$S/cron/crontab_$u.txt" 2>/dev/null || rm -f "$S/cron/crontab_$u.txt"; done
systemctl list-timers --all --no-pager > "$S/cron/systemd_timers.txt"
systemctl list-unit-files --no-pager --type=service --state=enabled > "$S/systemd_enabled_services.txt"
firewall-cmd --list-all-zones > "$S/firewalld_zones.txt" 2>/dev/null
getenforce > "$S/selinux.txt" 2>/dev/null
# redacted gunicorn unit so the structure is readable without the passphrase
mkdir -p "$S/etc/systemd/system/gunicorn.service.d"
sed -E 's/^(Environment="?[A-Z0-9_]+=).*/\1<redacted>"/' /etc/systemd/system/gunicorn.service > "$S/etc/systemd/system/gunicorn.service.REDACTED"
for f in /etc/systemd/system/gunicorn.service.d/*.conf; do
  sed -E 's/^(Environment="?[A-Z0-9_]+=).*/\1<redacted>"/' "$f" > "$S/etc/systemd/system/gunicorn.service.d/$(basename "$f").REDACTED"
done
[ -d "$S/etc/nginx" ] && [ -s "$S/cron/crontab_root.txt" ] && ok system_config || fail system_config

# ---------------------------------------------------------------- secrets
SEC=$WORK/secrets
mkdir -p "$SEC/etc/systemd/system" "$SEC/etc/letsencrypt"
cp -a /etc/optiwar "$SEC/etc/" 2>/dev/null; rm -f "$SEC/etc/optiwar/dr_passphrase"
cp -a /etc/systemd/system/gunicorn.service /etc/systemd/system/gunicorn.service.d "$SEC/etc/systemd/system/" 2>/dev/null
for d in live archive keys accounts; do [ -d /etc/letsencrypt/$d ] && cp -a /etc/letsencrypt/$d "$SEC/etc/letsencrypt/"; done
cp -a /root/.my.cnf "$SEC/root_my.cnf" 2>/dev/null
cp -a /root/.ssh/authorized_keys "$SEC/root_authorized_keys" 2>/dev/null
SECTAR=$WORK/secrets.tar.gz
tar czf "$SECTAR" -C "$WORK" secrets 2>/dev/null
gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$DR_PASSPHRASE_FILE" \
    --cipher-algo AES256 --symmetric -o "$WORK/secrets.tar.gz.gpg" "$SECTAR"
if [ -s "$WORK/secrets.tar.gz.gpg" ]; then
  PLAIN=$(sha256sum "$SECTAR" | cut -d' ' -f1)
  DEC=$(gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$DR_PASSPHRASE_FILE" -d "$WORK/secrets.tar.gz.gpg" 2>/dev/null | sha256sum | cut -d' ' -f1)
  [ "$PLAIN" = "$DEC" ] && ok secrets_encrypted "$(du -h "$WORK/secrets.tar.gz.gpg" | cut -f1), decrypt round-trip verified" \
                        || fail secrets_encrypted "decrypt round-trip mismatch"
else
  fail secrets_encrypted "gpg produced nothing"
fi
SECRET_ITEMS=$( { find "$SEC" -type f ! -path '*/letsencrypt/*' | sed "s|$SEC/||"; \
                  for d in live archive keys accounts; do [ -d "$SEC/etc/letsencrypt/$d" ] && echo "etc/letsencrypt/$d/ ($(find "$SEC/etc/letsencrypt/$d" -type f | wc -l) files)"; done; } | sort)
shred -u "$SECTAR" 2>/dev/null || rm -f "$SECTAR"
find "$SEC" -type f -exec shred -u {} \; 2>/dev/null; rm -rf "$SEC"

# -------------------------------------------------------------- inventory
INV=$WORK/inventory
cp /etc/os-release "$INV/os-release" 2>/dev/null
rpm -qa | sort > "$INV/rpm_packages.txt"
python3 --version > "$INV/python_system.txt" 2>&1
(command -v node >/dev/null && node --version; command -v npm >/dev/null && npm --version) > "$INV/node.txt" 2>/dev/null
mysql --version > "$INV/mariadb.txt" 2>/dev/null
redis-server --version > "$INV/redis.txt" 2>/dev/null
nginx -v > "$INV/nginx.txt" 2>&1
find "$FLASKR" -maxdepth 1 -printf '%M %u:%g %p\n' | sort > "$INV/permissions_flaskr_top.txt"
find "$(dirname "$FLASKR")/secure_uploads" /etc/optiwar /etc/systemd/system/gunicorn.service.d /var/log/optiwar \
     -maxdepth 1 -printf '%M %u:%g %p\n' 2>/dev/null | sort > "$INV/permissions_sensitive.txt"
[ -d "$PARKED_IMAGES" ] && { echo "$PARKED_IMAGES: $(ls -1 "$PARKED_IMAGES" | wc -l) files, $(du -sh "$PARKED_IMAGES" | cut -f1) — NOT in this bundle (owner instruction: do not touch); archive separately, read-only" > "$INV/parked_images.txt"; }

FLAGS=$( { systemctl show gunicorn -p Environment | sed 's/^Environment=//' | tr ' ' '\n'; \
           cat /etc/optiwar/*.env 2>/dev/null; } | grep -oE '^[A-Z][A-Z0-9_]+=' | tr -d '=' | sort -u | tr '\n' ' ')
cat > "$WORK/SYSTEM_INVENTORY.txt" <<EOF
OPTIWAR SYSTEM INVENTORY (non-secret)         generated $TS on $HOST
os:              $(. /etc/os-release; echo "$PRETTY_NAME")
release:         $RELEASE   git: $GIT_SHA
app_root:        $APP_ROOT
flaskr:          $FLASKR
python (venv):   $("$VENV/bin/python" --version 2>&1)
service:         gunicorn.service  (bind 127.0.0.1:8000; $(grep -oE '\-w [0-9]+' /etc/systemd/system/gunicorn.service) workers, $(grep -oE '\-\-threads [0-9]+' /etc/systemd/system/gunicorn.service))
EnvironmentFile: $(grep -h EnvironmentFile /etc/systemd/system/gunicorn.service /etc/systemd/system/gunicorn.service.d/*.conf 2>/dev/null | cut -d= -f2- | tr '\n' ' ')
drop-ins:        $(ls /etc/systemd/system/gunicorn.service.d/*.conf 2>/dev/null | xargs -n1 basename | tr '\n' ' ')
config dir:      /etc/optiwar ($(ls /etc/optiwar 2>/dev/null | tr '\n' ' '))
nginx sites:     $(ls /etc/nginx/conf.d/*.conf 2>/dev/null | xargs -n1 basename | tr '\n' ' ')
tls domains:     $(ls /etc/letsencrypt/renewal 2>/dev/null | sed 's/\.conf$//' | tr '\n' ' ')
database:        $MYSQL_DB @ $MYSQL_HOST ($(mysql --version 2>/dev/null | grep -oE 'Distrib [^,]+'))
db users:        $MYSQL_USER (app), optiwar_ro (reports), optiwar_closer (ACR closure), reportuser (remote)
redis:           $(systemctl is-active redis 2>/dev/null) — sessions/cache only, NOT authoritative (RDB $(grep -E '^dir ' /etc/redis.conf 2>/dev/null | cut -d' ' -f2))
ports (firewalld): $(firewall-cmd --list-ports 2>/dev/null) services: $(firewall-cmd --list-services 2>/dev/null)
selinux:         $(getenforce 2>/dev/null)
secure_uploads:  $(dirname "$FLASKR")/secure_uploads
logs:            /var/log/optiwar
release backups: $RELEASES ($(ls -1 "$RELEASES" 2>/dev/null | wc -l) entries)
legacy backups:  /root/backups (daily 03:15 optiwar_backup.sh)
dr backups:      $DR_DIR
parked images:   $PARKED_IMAGES (excluded, see inventory/parked_images.txt)
feature/config names ($(echo "$FLAGS" | wc -w)): $FLAGS
integrations:    Razorpay, Paytm(legacy), MSG91 (WhatsApp), DeepSeek/OpenAI (AI), KET support, SMTP mail.ket.ltd, Google OAuth, GMC, GA4, ElevenLabs
cron (root):
$(sed 's/^/  /' "$S/cron/crontab_root.txt" 2>/dev/null | grep -v '^\s*#' | grep -v '^\s*$')
systemd timers:  $(systemctl list-timers --all --no-pager 2>/dev/null | awk 'NR>1 && $NF ~ /service/ {print $(NF-1)}' | tr '\n' ' ')
EOF
[ -s "$INV/rpm_packages.txt" ] && ok inventory "$(wc -l < "$INV/rpm_packages.txt") rpms" || fail inventory

# --------------------------------------------------- manifest + checksums
cp "$(dirname "$0")/RESTORE_NOTES.txt" "$WORK/RESTORE_NOTES.txt" 2>/dev/null \
  || cp /root/RESTORE_NOTES.txt "$WORK/RESTORE_NOTES.txt" 2>/dev/null \
  || echo "see optiwar_restore_server.sh --help and deploy/dr/RESTORE_RUNBOOK.md in the repo" > "$WORK/RESTORE_NOTES.txt"
cat > "$WORK/MANIFEST.txt" <<EOF
hostname:        $HOST
created_at:      $(date -Is)
release:         $RELEASE
git_commit:      $GIT_SHA
database:        $MYSQL_DB  dump=db/${MYSQL_DB}.sql.gz  size=$(stat -c %s "$DUMP" 2>/dev/null) bytes  tables=$(wc -l < "$WORK/db/table_rowcounts.txt")
db_restore_test: ${STAGE[db_restore_test]:-skipped}
app_code:        app/flaskr_code.tar.gz (static/ excluded$( [ "$WANT_IMAGES" = 1 ] && echo "; images in $(basename "$IMG")" ))
config included: $(cd "$S" && find . -type f | wc -l) files under system/ (nginx, systemd redacted, redis, mariadb, fail2ban, logrotate, cron, firewalld, letsencrypt renewal)
systemd units:   gunicorn.service + $(ls /etc/systemd/system/gunicorn.service.d/*.conf 2>/dev/null | wc -l) drop-ins (plaintext copies only inside secrets.tar.gz.gpg)
nginx configs:   $(ls /etc/nginx/conf.d/*.conf 2>/dev/null | wc -l) site files
cron/timers:     $(grep -cvE '^\s*(#|$)' "$S/cron/crontab_root.txt" 2>/dev/null) root cron lines, $(grep -c '\.timer' "$S/cron/systemd_timers.txt") timers
encrypted secrets: secrets.tar.gz.gpg  ${STAGE[secrets_encrypted]:-FAIL}  (AES256 symmetric)  contents:
$(echo "$SECRET_ITEMS" | sed 's/^/  /')
stages:
$(for k in "${!STAGE[@]}"; do echo "  $k=${STAGE[$k]}"; done | sort)
EOF
( cd "$WORK" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
( cd "$WORK" && sha256sum -c --quiet SHA256SUMS ) && ok checksums "$(wc -l < "$WORK/SHA256SUMS") files" || fail checksums

tar cf "$BUNDLE" -C "$DR_DIR" "$(basename "$WORK")" && tar tf "$BUNDLE" >/dev/null 2>&1 \
  && ok bundle "$BUNDLE $(du -h "$BUNDLE" | cut -f1)" || fail bundle
chmod 600 "$BUNDLE"
BUNDLE_SHA=$(sha256sum "$BUNDLE" | cut -d' ' -f1)
echo "$BUNDLE_SHA  $(basename "$BUNDLE")" > "$BUNDLE.sha256"
rm -rf "$WORK"

# -------------------------------------------------------------- retention
# only our own artefacts, by name pattern, newest N kept; nothing else on disk
cd "$DR_DIR" || exit 4
ls -1t optiwar_dr_*.tar 2>/dev/null | tail -n +$((DR_KEEP_BUNDLES+1)) | while read -r f; do
  rm -f "$f" "$f.sha256" "${f%.tar}.log"; note "retention: removed $f"; done
ls -1t optiwar_static_images_*.tar.gz 2>/dev/null | tail -n +$((DR_KEEP_IMAGE_TARS+1)) | while read -r f; do
  rm -f "$f"; note "retention: removed $f"; done

# ----------------------------------------------------------------- status
[ "$FAILED" = 0 ] && VERDICT=OK || VERDICT=INCOMPLETE
STAGES_JSON=$(for k in "${!STAGE[@]}"; do printf '"%s":"%s",' "$k" "${STAGE[$k]}"; done | sed 's/,$//')
cat > "$DR_DIR/latest_status.json" <<EOF
{"ok": $([ "$FAILED" = 0 ] && echo true || echo false), "verdict": "$VERDICT", "created_at": "$(date -Is)",
 "bundle": "$BUNDLE", "bundle_sha256": "$BUNDLE_SHA", "bundle_bytes": $(stat -c %s "$BUNDLE" 2>/dev/null || echo 0),
 "release": "$RELEASE", "git_commit": "$GIT_SHA", "db_dump_bytes": ${DUMP_BYTES:-0},
 "images_included": $WANT_IMAGES, "stages": {$STAGES_JSON}}
EOF
echo
echo "== RESULT: $VERDICT =="
echo "bundle:  $BUNDLE"
echo "sha256:  $BUNDLE_SHA"
echo "off-host copy (run on your own machine, then verify):"
echo "  scp root@$(hostname -I | awk '{print $1}'):$BUNDLE ."
echo "  echo '$BUNDLE_SHA  $(basename "$BUNDLE")' | sha256sum -c"
echo "then record it:  /root/optiwar_dr_confirm_offhost.sh $(basename "$BUNDLE") $BUNDLE_SHA"
[ "$FAILED" = 0 ] && exit 0 || exit 1
