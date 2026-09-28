#!/bin/bash
# Optiwar — rebuild a clean Rocky Linux 8 host from a DR bundle.
#
# Runs the restore in numbered steps; each step is idempotent and can be
# re-run alone. Nothing here touches DNS. External side effects are disabled
# by default (--drill): the restored app cannot send WhatsApp/email, capture
# payments or call the Ops platform until you remove the drill overrides.
#
# Usage: optiwar_restore_server.sh --bundle /path/optiwar_dr_<ts>.tar \
#            --passphrase-file /root/dr_passphrase [--images <tar.gz>] \
#            [--drill] [--from N] [--to N] [--mysql-root-password-file F]
#
# Steps:
#   1 os        packages: nginx, mariadb, redis, python3.11, certbot, gpg, fail2ban, firewalld
#   2 unpack    extract bundle, verify SHA256SUMS, decrypt secrets (root-only)
#   3 app       /var/www/... tree, venv from requirements-freeze, code, static images, secure_uploads
#   4 mysql     start MariaDB, create database + users/grants, load dump, verify table counts
#   5 redis     config + start (cache/session only)
#   6 config    /etc/optiwar (0600), gunicorn.service + drop-ins, drill overrides
#   7 systemd   daemon-reload, enable + start gunicorn, wait for 127.0.0.1:8000
#   8 nginx     site configs, certificates (from bundle or self-signed placeholder), start
#   9 cron      root crontab, /usr/local/sbin scripts, report/catalog code, timers
#  10 perms     ownership/permission manifest applied to sensitive paths
#  11 smoke     local HTTP checks through nginx and gunicorn, DB row sanity
#  12 report    RESTORE_REPORT.txt with duration, problems, manual steps left
set -u
APP_ROOT=/var/www/flask-optiwar-ow-release-090525
FLASKR=$APP_ROOT/venv/lib/python3.11/site-packages/flaskr
BUNDLE=""; PASS=""; IMAGES=""; DRILL=0; FROM=1; TO=12; ROOTPW_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) BUNDLE=$2; shift 2 ;;
    --passphrase-file) PASS=$2; shift 2 ;;
    --images) IMAGES=$2; shift 2 ;;
    --drill) DRILL=1; shift ;;
    --from) FROM=$2; shift 2 ;;
    --to) TO=$2; shift 2 ;;
    --mysql-root-password-file) ROOTPW_FILE=$2; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ -n "$BUNDLE" ] && [ -r "$BUNDLE" ] || { echo "--bundle required" >&2; exit 2; }
[ -n "$PASS" ] && [ -r "$PASS" ] || { echo "--passphrase-file required" >&2; exit 2; }
umask 077
START=$(date +%s)
WORK=/root/dr_restore
REPORT=$WORK/RESTORE_REPORT.txt
PROBLEMS=(); MANUAL=()
mkdir -p "$WORK"
log() { echo "[$(date +%T)] $*" | tee -a "$WORK/restore.log"; }
problem() { PROBLEMS+=("$*"); log "PROBLEM: $*"; }
manual() { MANUAL+=("$*"); }
run_step() { [ "$1" -ge "$FROM" ] && [ "$1" -le "$TO" ]; }
mysql_root() { if [ -n "$ROOTPW_FILE" ]; then mysql -uroot -p"$(cat "$ROOTPW_FILE")" "$@"; else mysql -uroot "$@"; fi; }

B="" # bundle dir after unpack
find_bundle_dir() { B=$(find "$WORK" -maxdepth 1 -type d -name 'optiwar_dr_*' | sort | tail -1); }

if run_step 1; then
  log "STEP 1 os packages"
  dnf -y -q install epel-release >/dev/null 2>&1
  dnf -y -q module enable python311 >/dev/null 2>&1 || true
  dnf -y -q install nginx mariadb-server mariadb redis python3.11 python3.11-pip python3.11-devel \
      gcc gcc-c++ make certbot python3-certbot-nginx gnupg2 fail2ban firewalld tar gzip rsync \
      mysql-devel openssl-devel libjpeg-turbo-devel zlib-devel >/dev/null 2>&1 || problem "dnf install returned $?"
  systemctl enable --now firewalld >/dev/null 2>&1
  for s in http https; do firewall-cmd -q --permanent --add-service=$s; done; firewall-cmd -q --reload
  log "os packages done"
fi

if run_step 2; then
  log "STEP 2 unpack + verify"
  tar xf "$BUNDLE" -C "$WORK" || problem "bundle tar extract failed"
  find_bundle_dir
  [ -n "$B" ] || { problem "no optiwar_dr_* directory in bundle"; exit 1; }
  ( cd "$B" && sha256sum -c --quiet SHA256SUMS ) && log "SHA256SUMS verified" || problem "SHA256SUMS verification FAILED"
  gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASS" -d "$B/secrets.tar.gz.gpg" > "$B/secrets.tar.gz" \
    && tar xzf "$B/secrets.tar.gz" -C "$B" && rm -f "$B/secrets.tar.gz" && chmod -R go-rwx "$B/secrets" \
    && log "secrets decrypted to $B/secrets (root-only)" || problem "secrets decrypt failed — wrong passphrase?"
  cat "$B/MANIFEST.txt" | tee -a "$WORK/restore.log" >/dev/null
fi
find_bundle_dir; [ -n "$B" ] || { echo "run step 2 first" >&2; exit 1; }

if run_step 3; then
  log "STEP 3 application"
  mkdir -p "$APP_ROOT"
  [ -x "$APP_ROOT/venv/bin/python" ] || python3.11 -m venv "$APP_ROOT/venv" || problem "venv creation failed"
  "$APP_ROOT/venv/bin/pip" -q install --upgrade pip >/dev/null 2>&1
  "$APP_ROOT/venv/bin/pip" -q install -r "$B/app/requirements-freeze.txt" >/dev/null 2>"$WORK/pip.err" \
    || problem "pip install from freeze had errors (see $WORK/pip.err)"
  tar xzf "$B/app/flaskr_code.tar.gz" -C "$(dirname "$FLASKR")" || problem "code extract failed"
  if [ -n "$IMAGES" ] && [ -r "$IMAGES" ]; then tar xzf "$IMAGES" -C "$FLASKR" || problem "static images extract failed"
  else manual "static product images: extract optiwar_static_images_*.tar.gz into $FLASKR/ (or re-derive)"; fi
  [ -f "$B/secrets/secure_uploads.tar.gz" ] && tar xzf "$B/secrets/secure_uploads.tar.gz" -C "$(dirname "$FLASKR")"
  mkdir -p /var/log/optiwar /root/deploy_releases
  [ -d "$B/app/last_release_backup" ] && cp -a "$B/app/last_release_backup" "/root/deploy_releases/$(cat "$B/app/RELEASE.txt" | awk -F= '/^release=/{print $2}')" 2>/dev/null
  log "application restored: $(head -2 "$B/app/RELEASE.txt" | tr '\n' ' ')"
fi

if run_step 4; then
  log "STEP 4 mariadb"
  [ -d "$B/system/etc/my.cnf.d" ] && cp -a "$B/system/etc/my.cnf.d/"*.cnf /etc/my.cnf.d/ 2>/dev/null
  systemctl enable --now mariadb >/dev/null 2>&1 || problem "mariadb failed to start"
  DBNAME=$(awk '/^database:/{print $2}' "$B/MANIFEST.txt")
  DUMP=$(ls "$B"/db/*.sql.gz | head -1)
  CS=$(awk '{print $1}' "$B/db/charset.txt" 2>/dev/null); CS=${CS:-utf8mb4}
  mysql_root -e "CREATE DATABASE IF NOT EXISTS \`$DBNAME\` CHARACTER SET $CS" || problem "create database"
  mysql_root < "$B/secrets/mysql_grants.sql" 2>"$WORK/grants.err" || problem "grants: $(head -c 200 "$WORK/grants.err")"
  zcat "$DUMP" | mysql_root "$DBNAME" 2>"$WORK/load.err" || problem "dump load: $(head -c 200 "$WORK/load.err")"
  EXP=$(wc -l < "$B/db/table_rowcounts.txt"); GOT=$(mysql_root -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DBNAME'")
  [ "$EXP" = "$GOT" ] && log "database $DBNAME: $GOT tables (matches source)" || problem "table count $GOT != $EXP"
  for t in orders order_reshipments ai_events face_profiles; do
    mysql_root -N -e "SELECT '$t', COUNT(*) FROM $DBNAME.$t" 2>/dev/null | tee -a "$WORK/restore.log"; done
fi

if run_step 5; then
  log "STEP 5 redis"
  [ -f "$B/system/etc/redis.conf" ] && cp -a "$B/system/etc/redis.conf" /etc/redis.conf
  systemctl enable --now redis >/dev/null 2>&1 && log "redis active (cache/sessions only)" || problem "redis failed"
fi

if run_step 6; then
  log "STEP 6 configuration"
  mkdir -p /etc/optiwar && cp -a "$B/secrets/etc/optiwar/." /etc/optiwar/ && chmod 750 /etc/optiwar && chmod 600 /etc/optiwar/* 
  cp -a "$B/secrets/etc/systemd/system/gunicorn.service" /etc/systemd/system/
  mkdir -p /etc/systemd/system/gunicorn.service.d
  cp -a "$B/secrets/etc/systemd/system/gunicorn.service.d/"*.conf /etc/systemd/system/gunicorn.service.d/
  rm -f /etc/systemd/system/gunicorn.service.d/*.bak* /etc/systemd/system/gunicorn.service.d/*REDACTED 2>/dev/null
  chmod 600 /etc/systemd/system/gunicorn.service /etc/systemd/system/gunicorn.service.d/*.conf
  if [ "$DRILL" = 1 ]; then
    cat > /etc/systemd/system/gunicorn.service.d/zz-dr-drill.conf <<'EOF'
# DR DRILL — external side effects disabled. Remove this file to go live.
[Service]
Environment="MSG91_AUTH_KEY=drill-disabled"
Environment="SMTP_HOST=127.0.0.1" Environment="SMTP_PORT=9"
Environment="RAZORPAY_KEY_ID=rzp_test_drill" Environment="RAZORPAY_KEY_SECRET=drill"
Environment="RESHIP_OPS_WEBHOOK_URL=" Environment="RESHIP_OPS_WEBHOOK_SECRET="
Environment="KET_SUPPORT_URL=http://127.0.0.1:9/" Environment="DEEPSEEK_API_KEY=drill" Environment="OPENAI_API_KEY=drill"
Environment="RESHIP_ENABLED_IN=false" Environment="ACR_ACTIONS_ENABLED=0"
EOF
    log "drill overrides installed (zz-dr-drill.conf): no WhatsApp/email/Razorpay/Ops/AI calls"
  fi
  [ -f "$B/system/etc/fail2ban/jail.local" ] && cp -a "$B/system/etc/fail2ban/jail.local" /etc/fail2ban/ && systemctl enable --now fail2ban >/dev/null 2>&1
  [ -d "$B/system/etc/logrotate.d" ] && cp -a "$B/system/etc/logrotate.d/." /etc/logrotate.d/
fi

if run_step 7; then
  log "STEP 7 gunicorn"
  systemctl daemon-reload; systemctl enable gunicorn >/dev/null 2>&1; systemctl restart gunicorn
  for i in $(seq 1 30); do curl -fs -o /dev/null http://127.0.0.1:8000/ -H 'Host: optiwar.in' && break; sleep 2; done
  curl -fs -o /dev/null http://127.0.0.1:8000/ -H 'Host: optiwar.in' && log "gunicorn answering on :8000" \
    || problem "gunicorn not answering: $(journalctl -u gunicorn -n 5 --no-pager | tail -3)"
fi

if run_step 8; then
  log "STEP 8 nginx + tls"
  cp -a "$B/system/etc/nginx/." /etc/nginx/
  rm -f /etc/nginx/conf.d/*.bak* /etc/nginx/conf.d/*.disabled 2>/dev/null
  mkdir -p /etc/letsencrypt
  for d in live archive keys accounts; do [ -d "$B/secrets/etc/letsencrypt/$d" ] && cp -a "$B/secrets/etc/letsencrypt/$d" /etc/letsencrypt/; done
  for d in renewal renewal-hooks; do [ -d "$B/system/etc/letsencrypt/$d" ] && cp -a "$B/system/etc/letsencrypt/$d" /etc/letsencrypt/; done
  [ -f "$B/system/etc/letsencrypt/cli.ini" ] && cp -a "$B/system/etc/letsencrypt/cli.ini" /etc/letsencrypt/
  chmod -R go-rwx /etc/letsencrypt/live /etc/letsencrypt/archive /etc/letsencrypt/keys 2>/dev/null
  mkdir -p /var/www/certbot /var/cache/nginx
  nginx -t 2>"$WORK/nginx.err" && systemctl enable --now nginx >/dev/null 2>&1 && systemctl reload nginx \
    && log "nginx config valid, running" || problem "nginx -t: $(tail -2 "$WORK/nginx.err")"
  systemctl enable --now certbot-renew.timer >/dev/null 2>&1 || manual "enable certbot renewal timer"
  manual "TLS: certificates restored from bundle are the production ones; after DNS switch run 'certbot renew --dry-run'"
fi

if run_step 9; then
  log "STEP 9 cron + jobs"
  [ -d "$B/system/usr/local/sbin" ] && cp -a "$B/system/usr/local/sbin/." /usr/local/sbin/ && chmod 700 /usr/local/sbin/optiwar-* 2>/dev/null
  mkdir -p /root/backups /root/catalog /root/reports
  for f in optiwar_backup.sh optiwar_metrics_snapshot.sh optiwar_alert_check.sh _enc.sh; do [ -f "$B/system/root/backups/$f" ] && cp -a "$B/system/root/backups/$f" /root/backups/; done
  for f in optiwar_disaster_backup.sh optiwar_restore_server.sh optiwar_dr_confirm_offhost.sh RESTORE_NOTES.txt; do [ -f "$B/system/root/$f" ] && cp -a "$B/system/root/$f" /root/; done
  for f in error_summary_cron.py missing_order_search_copy.py; do [ -f "$B/secrets/root/$f" ] && cp -a "$B/secrets/root/$f" /root/ && chmod 600 "/root/$f"; done
  [ -f "$B/secrets/root/reports_code.tar.gz" ] && tar xzf "$B/secrets/root/reports_code.tar.gz" -C /root && chmod -R go-rwx /root/reports
  [ -f "$B/secrets/root/catalog_code.tar.gz" ] && tar xzf "$B/secrets/root/catalog_code.tar.gz" -C /root && chmod -R go-rwx /root/catalog
  [ -f "$B/system/root/root_venv.tar.gz" ] && tar xzf "$B/system/root/root_venv.tar.gz" -C /root
  mkdir -p /var/log/my_crons
  [ -d "$B/system/etc/cron.d" ] && cp -a "$B/system/etc/cron.d/." /etc/cron.d/
  if [ "$DRILL" = 1 ]; then
    sed 's/^\([^#]\)/#DRILL# \1/' "$B/system/cron/crontab_root.txt" | crontab -
    log "root crontab installed DISABLED (every job commented with #DRILL#)"
  else
    crontab "$B/system/cron/crontab_root.txt" && log "root crontab installed ($(grep -cvE '^\s*(#|$)' "$B/system/cron/crontab_root.txt") jobs)"
  fi
  systemctl enable --now crond >/dev/null 2>&1
  MISSING=$(crontab -l | grep -vE '^\s*#' | grep -oE '(/[A-Za-z0-9_./-]+\.(sh|py))' | sort -u | while read -r p; do [ -e "$p" ] || echo "$p"; done)
  [ -z "$MISSING" ] && log "every cron target exists" || problem "cron targets missing: $(echo $MISSING)"
fi

if run_step 10; then
  log "STEP 10 permissions"
  chown -R root:root "$APP_ROOT" /etc/optiwar
  chmod 700 "$(dirname "$FLASKR")/secure_uploads" 2>/dev/null
  chmod 600 /etc/optiwar/* /etc/systemd/system/gunicorn.service.d/*.conf 2>/dev/null
  log "permissions applied (root-owned app tree, 0600 secrets, 0700 secure_uploads)"
fi

if run_step 11; then
  log "STEP 11 smoke"
  for h in optiwar.in optiwar.com; do
    for p in / /profile/ /ops/reship /api/face-context; do
      code=$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$h:443:127.0.0.1" "https://$h$p" 2>/dev/null || echo 000)
      [ "$code" = 000 ] && code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $h" "http://127.0.0.1$p")
      case "$code" in 200|302|401|403) log "  $h$p -> $code";; *) problem "smoke $h$p -> $code";; esac
    done
  done
  DBNAME=$(awk '/^database:/{print $2}' "$B/MANIFEST.txt")
  for t in order_reshipments ai_events ai_actions face_profiles face_scans policy_versions; do
    n=$(mysql_root -N -e "SELECT COUNT(*) FROM $DBNAME.$t" 2>/dev/null) && log "  table $t rows=$n" || problem "table $t missing"
  done
  redis-cli ping | grep -q PONG && log "  redis PONG" || problem "redis not answering"
fi

if run_step 12; then
  DUR=$(( $(date +%s) - START ))
  {
    echo "OPTIWAR RESTORE REPORT  $(date -Is)  host=$(hostname)  drill=$DRILL"
    echo "bundle: $BUNDLE"; grep -E '^(release|git_commit|created_at):' "$B/MANIFEST.txt"
    echo "duration: ${DUR}s (steps $FROM..$TO)"
    echo "problems (${#PROBLEMS[@]}):"; for p in "${PROBLEMS[@]}"; do echo "  - $p"; done
    echo "manual steps still required:"
    for m in "${MANUAL[@]}"; do echo "  - $m"; done
    echo "  - DNS: point optiwar.in / optiwar.com / in.optiwar.com / www.optiwar.com at this host (never automated)"
    echo "  - remote MySQL user reportuser@14.99.193.198: confirm bind-address/firewall if still needed"
    [ "$DRILL" = 1 ] && echo "  - DRILL host: remove zz-dr-drill.conf and uncomment crontab before any live use"
    echo "RPO: age of the bundle's DB dump at the moment of loss (daily bundle => <= 24h; /root/backups dump also daily)"
    echo "RTO: this run took ${DUR}s + OS provisioning + DNS TTL"
  } | tee "$REPORT"
  # machine-readable verdict; on a drill, copy this file to the production
  # server's /root/dr_backups/last_drill.json so the daily report can state it
  mkdir -p /root/dr_backups
  printf '{"result":"%s","finished_at":"%s","host":"%s","drill":%s,"bundle":"%s","duration_s":%d,"problems":%d,"open_items":%d}\n' \
    "$([ ${#PROBLEMS[@]} -eq 0 ] && echo PASS || echo FAIL)" "$(date -Is)" "$(hostname)" \
    "$([ "$DRILL" = 1 ] && echo true || echo false)" "$(basename "$BUNDLE")" "$DUR" \
    "${#PROBLEMS[@]}" "${#MANUAL[@]}" > /root/dr_backups/last_drill.json
  chmod 600 /root/dr_backups/last_drill.json
  [ ${#PROBLEMS[@]} -eq 0 ] && exit 0 || exit 1
fi
