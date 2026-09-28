#!/bin/bash
# Record that a DR bundle was copied off this server and its checksum verified
# there. The daily report reads offhost_status.json; without this record the
# report says the last off-host copy is unknown — deliberately.
#
# Usage: optiwar_dr_confirm_offhost.sh <bundle-file-name> <sha256-as-verified-off-host> [where]
set -u
DR_DIR=${DR_DIR:-/root/dr_backups}
[ -r /etc/optiwar/dr.env ] && . /etc/optiwar/dr.env
NAME=${1:?bundle file name}; SHA=${2:?sha256 verified off-host}; WHERE=${3:-owner-download}
LOCAL=$(cut -d' ' -f1 "$DR_DIR/$NAME.sha256" 2>/dev/null)
if [ -z "$LOCAL" ]; then echo "no local checksum for $NAME" >&2; exit 2; fi
if [ "$LOCAL" != "$SHA" ]; then echo "MISMATCH: local $LOCAL != off-host $SHA — copy is not trustworthy" >&2; exit 1; fi
printf '{"bundle":"%s","sha256":"%s","where":"%s","confirmed_at":"%s"}\n' \
  "$NAME" "$SHA" "$WHERE" "$(date -Is)" > "$DR_DIR/offhost_status.json"
chmod 600 "$DR_DIR/offhost_status.json"
echo "off-host copy of $NAME confirmed ($WHERE)"
