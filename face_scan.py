"""Face-scan capture: get a photo from a customer and a measurement back.

The journey this serves is deliberately the short one:

    staff creates a request -> customer gets a WhatsApp link -> customer uploads
    one photo holding a bank card under their eyes -> staff reads PD and face
    width off it -> the order can be placed.

Two decisions are worth stating because they are what keep it short and safe.

**A link, not an inbound message.** MSG91 inbound media would mean a second
webhook, media fetched by id, and a dependency on a provider hook that is
currently paused. A tokenised link reuses the upload path this application
already hardened for prescriptions, and works the moment the outbound template
is approved.

**A human owns the number.** No millimetre is inferred here. A phone photo has
no scale, which is why the customer is asked to hold a card in frame and why
staff confirm they measured against it before a value is stored. A wrong PD is
not a wrong row in a table, it is a wrong pair of glasses on somebody's face.

A face photo is biometric-adjacent, so consent is explicit, the file lives
outside the web root, only an authenticated admin can retrieve it, every access
is audited, and ``purge_due_images`` exists so retention is a scheduled fact
rather than an intention.
"""
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta

from flask import (Blueprint, current_app, jsonify, render_template, request,
                   send_file, session, url_for)

from .db import get_db
from .ops import _require_ops_auth

bp = Blueprint('face_scan', __name__)

# Link lifetime and retention. Both are configuration rather than constants
# because they are policy, and policy is the owner's to set.
DEFAULT_TOKEN_HOURS = 72
DEFAULT_RETENTION_DAYS = 90

MAX_UPLOAD_ATTEMPTS = 5
MAX_FILE_SIZE = 8 * 1024 * 1024
MIN_FILE_SIZE = 1024
ALLOWED_EXTENSIONS = ('jpg', 'jpeg', 'png', 'webp', 'heic')
MAGIC_BYTES = {
    'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
    'png': (b'\x89PNG\r\n\x1a\n',),
    'webp': (b'RIFF',),
    'heic': (b'ftyp',),
}
CONTENT_TYPES = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                 'webp': 'image/webp', 'heic': 'image/heic'}

# The same bounds the fitting endpoint enforces (ai_api.recommend_frame_fit),
# stated here so a typo is refused at entry rather than at order time.
FACE_WIDTH_MIN, FACE_WIDTH_MAX = 100.0, 180.0
PD_MIN, PD_MAX = 40.0, 80.0

ST_REQUESTED = 'REQUESTED'
ST_SENT = 'SENT'
ST_SEND_FAILED = 'SEND_FAILED'
ST_UPLOADED = 'UPLOADED'
ST_MEASURED = 'MEASURED'
ST_CANCELLED = 'CANCELLED'

# A token is usable only while the request is waiting for a photo. Once one
# arrives the link is spent, which is what makes it single-use.
OPEN_STATUSES = (ST_REQUESTED, ST_SENT, ST_SEND_FAILED)

# Where a photo came from. A customer who followed their own link ticked the
# consent box themselves; a photo a staff member received on WhatsApp or by mail
# did not, so the staff member asserts it and is recorded as having done so.
SRC_CUSTOMER_LINK = 'customer_link'
SRC_STAFF_UPLOAD = 'staff_upload'

_SCHEMA_READY = False


def _upload_dir():
    root = current_app.root_path
    path = os.path.join(os.path.dirname(root), 'secure_uploads', 'face_scans')
    os.makedirs(path, exist_ok=True)
    return path


def _token_hours():
    return int(current_app.config.get('FACE_SCAN_TOKEN_HOURS',
                                      DEFAULT_TOKEN_HOURS))


def _retention_days():
    return int(current_app.config.get('FACE_SCAN_RETENTION_DAYS',
                                      DEFAULT_RETENTION_DAYS))


def ensure_schema(db):
    """Create the request and audit tables once per process."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cur = db.cursor()
    # token_hash, never the token: a database copy or a stray backup must not
    # hand somebody a working upload link.
    cur.execute(
        """CREATE TABLE IF NOT EXISTS face_scan_requests (
               request_id      VARCHAR(32) NOT NULL PRIMARY KEY,
               token_hash      CHAR(64) NOT NULL,
               customer_id     BIGINT NULL,
               contact_name    VARCHAR(191) NULL,
               contact_phone   VARCHAR(64) NULL,
               status          VARCHAR(24) NOT NULL DEFAULT 'REQUESTED',
               created_by      VARCHAR(191) NULL,
               created_at      DATETIME NOT NULL,
               expires_at      DATETIME NOT NULL,
               sent_at         DATETIME NULL,
               msg91_request_id VARCHAR(191) NULL,
               send_error      VARCHAR(255) NULL,
               consent_at      DATETIME NULL,
               uploaded_at     DATETIME NULL,
               upload_attempts INT NOT NULL DEFAULT 0,
               image_path      VARCHAR(255) NULL,
               image_bytes     INT NULL,
               image_sha256    CHAR(64) NULL,
               image_source    VARCHAR(24) NULL,
               received_by     VARCHAR(191) NULL,
               purge_after     DATETIME NULL,
               purged_at       DATETIME NULL,
               pd_mm           DECIMAL(5,1) NULL,
               face_width_mm   DECIMAL(5,1) NULL,
               measured_by     VARCHAR(191) NULL,
               measured_at     DATETIME NULL,
               measurement_note VARCHAR(255) NULL,
               UNIQUE KEY uq_token (token_hash),
               KEY idx_status (status, created_at),
               KEY idx_customer (customer_id, created_at),
               KEY idx_purge (purge_after)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cur.execute(
        """CREATE TABLE IF NOT EXISTS face_scan_audit (
               id          BIGINT AUTO_INCREMENT PRIMARY KEY,
               request_id  VARCHAR(32) NULL,
               action      VARCHAR(32) NOT NULL,
               actor       VARCHAR(191) NULL,
               ip          VARCHAR(64) NULL,
               detail      VARCHAR(255) NULL,
               occurred_at DATETIME NOT NULL,
               KEY idx_request (request_id, occurred_at),
               KEY idx_action (action, occurred_at)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    # A ledger created before provenance was recorded still has to answer "who
    # gave us this photo", so the two columns are added rather than assumed.
    cur.execute("""SELECT COLUMN_NAME FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA=DATABASE()
                      AND TABLE_NAME='face_scan_requests'
                      AND COLUMN_NAME IN ('image_source','received_by')""")
    have = {r['COLUMN_NAME'] for r in cur.fetchall()}
    if 'image_source' not in have:
        cur.execute("""ALTER TABLE face_scan_requests
                         ADD COLUMN image_source VARCHAR(24) NULL""")
    if 'received_by' not in have:
        cur.execute("""ALTER TABLE face_scan_requests
                         ADD COLUMN received_by VARCHAR(191) NULL""")
    db.commit()
    cur.close()
    _SCHEMA_READY = True


def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    return (fwd.split(',')[0].strip() if fwd else request.remote_addr) or ''


def audit(db, request_id, action, actor=None, detail=None):
    """Append one row to the face-scan audit trail. Never raises.

    Append-only and best-effort in that order: an audit failure must not deny a
    customer their upload, but it must be loud in the log.
    """
    try:
        cur = db.cursor()
        cur.execute(
            """INSERT INTO face_scan_audit
                 (request_id, action, actor, ip, detail, occurred_at)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (request_id, action, actor, _client_ip(),
             (detail or '')[:255] or None, datetime.now()))
        db.commit()
        cur.close()
    except Exception as e:  # noqa: BLE001 - audit must not break the journey
        current_app.logger.error("[FACE-SCAN] audit failed %s/%s: %s"
                                 % (request_id, action, e))


def _hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def create_request(db, customer_id=None, contact_name=None, contact_phone=None,
                   created_by=None, hours=None):
    """Create a request and return ``(request_id, token)``.

    The token is returned exactly once, here, and never stored in recoverable
    form. A lost link is re-created, not recovered.
    """
    ensure_schema(db)
    request_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(hours=hours or _token_hours())
    cur = db.cursor()
    cur.execute(
        """INSERT INTO face_scan_requests
             (request_id, token_hash, customer_id, contact_name, contact_phone,
              status, created_by, created_at, expires_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (request_id, _hash_token(token), customer_id, contact_name,
         contact_phone, ST_REQUESTED, created_by, now, expires))
    db.commit()
    cur.close()
    audit(db, request_id, 'CREATED', actor=created_by,
          detail='expires %s' % expires.strftime('%Y-%m-%d %H:%M'))
    return request_id, token


def _by_token(db, token):
    ensure_schema(db)
    cur = db.cursor()
    cur.execute("SELECT * FROM face_scan_requests WHERE token_hash=%s",
                (_hash_token(token),))
    row = cur.fetchone()
    cur.close()
    return row


def _by_id(db, request_id):
    ensure_schema(db)
    cur = db.cursor()
    cur.execute("SELECT * FROM face_scan_requests WHERE request_id=%s",
                (request_id,))
    row = cur.fetchone()
    cur.close()
    return row


def token_state(row):
    """Why a link cannot be used, or ``None`` when it can.

    Separate from the routes so the page and the upload agree on one answer;
    a page that renders a form the upload will refuse is its own kind of defect.
    """
    if not row:
        return 'unknown'
    if row['status'] == ST_CANCELLED:
        return 'cancelled'
    if row['status'] not in OPEN_STATUSES:
        return 'used'
    if row['expires_at'] and row['expires_at'] < datetime.now():
        return 'expired'
    if (row['upload_attempts'] or 0) >= MAX_UPLOAD_ATTEMPTS:
        return 'too_many_attempts'
    return None


def _valid_image(file_storage):
    """(ext, size, error). Extension, bounds and magic bytes, in that order."""
    name = file_storage.filename or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext == 'jpe':
        ext = 'jpeg'
    if ext not in ALLOWED_EXTENSIONS:
        return None, 0, 'Please upload a photo (JPG, PNG, WEBP or HEIC).'
    file_storage.seek(0, 2)
    size = file_storage.tell()
    file_storage.seek(0)
    if size > MAX_FILE_SIZE:
        return None, size, 'That photo is larger than 8MB. Please send a smaller one.'
    if size < MIN_FILE_SIZE:
        return None, size, 'That file looks empty. Please take the photo again.'
    header = file_storage.read(16)
    file_storage.seek(0)
    ok = False
    for magic in MAGIC_BYTES[ext]:
        # HEIC carries 'ftyp' a few bytes in rather than at offset 0.
        if (magic in header) if ext == 'heic' else header.startswith(magic):
            ok = True
            break
    if not ok:
        return None, size, ('That file is not the image type its name claims. '
                            'Please upload the photo directly from your phone.')
    return ext, size, None


def store_upload(db, row, file_storage, source=SRC_CUSTOMER_LINK, actor=None):
    """Save a validated photo against an open request. Returns (ok, error).

    The attempt is counted before the file is examined, so a caller cannot use
    repeated rejections as an unlimited probe of the validator.

    ``source`` records who produced the file: the customer through their own
    link, or a staff member attaching a photo the customer sent by some other
    channel. Both are legitimate; conflating them is not, because only one of
    them carries the customer's own consent tick.
    """
    cur = db.cursor()
    cur.execute("""UPDATE face_scan_requests SET upload_attempts=upload_attempts+1
                   WHERE request_id=%s""", (row['request_id'],))
    db.commit()
    cur.close()

    ext, size, error = _valid_image(file_storage)
    if error:
        audit(db, row['request_id'], 'UPLOAD_REJECTED', detail=error)
        return False, error

    data = file_storage.read()
    file_storage.seek(0)
    digest = hashlib.sha256(data).hexdigest()
    filename = "%s_%s.%s" % (row['request_id'],
                             datetime.now().strftime('%Y%m%d_%H%M%S'), ext)
    path = os.path.join(_upload_dir(), filename)
    with open(path, 'wb') as fh:
        fh.write(data)

    now = datetime.now()
    cur = db.cursor()
    # Guarded by status: two tabs posting the same link cannot both consume it,
    # and the second gets the same "already used" answer as a stale link.
    cur.execute(
        """UPDATE face_scan_requests
              SET status=%s, uploaded_at=%s, consent_at=COALESCE(consent_at,%s),
                  image_path=%s, image_bytes=%s, image_sha256=%s, purge_after=%s,
                  image_source=%s, received_by=%s
            WHERE request_id=%s AND status IN %s""",
        (ST_UPLOADED, now, now, filename, size, digest,
         now + timedelta(days=_retention_days()), source, actor,
         row['request_id'], OPEN_STATUSES))
    claimed = cur.rowcount == 1
    db.commit()
    cur.close()
    if not claimed:
        try:
            os.remove(path)
        except OSError:
            pass
        return False, 'This link has already been used.'
    audit(db, row['request_id'], 'UPLOADED', actor=actor,
          detail='%s, %s bytes, sha256 %s' % (source, size, digest[:12]))
    return True, None


def record_measurements(db, request_id, pd_mm, face_width_mm, actor, note=None):
    """Store staff-read measurements. Returns (ok, error).

    Refuses values outside the bounds the fitting logic accepts, and refuses to
    record anything against a request with no photo: a measurement whose source
    image is absent cannot be checked by anybody later.
    """
    row = _by_id(db, request_id)
    if not row:
        return False, 'Unknown request.'
    if not row['image_path'] or row['purged_at']:
        return False, 'No photo is available for this request.'
    try:
        face_width_mm = float(face_width_mm)
        pd_mm = float(pd_mm)
    except (TypeError, ValueError):
        return False, 'Both measurements must be numbers, in millimetres.'
    if not FACE_WIDTH_MIN <= face_width_mm <= FACE_WIDTH_MAX:
        return False, ('face width must be between %g and %gmm'
                       % (FACE_WIDTH_MIN, FACE_WIDTH_MAX))
    if not PD_MIN <= pd_mm <= PD_MAX:
        return False, 'PD must be between %g and %gmm' % (PD_MIN, PD_MAX)
    cur = db.cursor()
    cur.execute(
        """UPDATE face_scan_requests
              SET pd_mm=%s, face_width_mm=%s, measured_by=%s, measured_at=%s,
                  measurement_note=%s, status=%s
            WHERE request_id=%s""",
        (pd_mm, face_width_mm, actor, datetime.now(),
         (note or '')[:255] or None, ST_MEASURED, request_id))
    db.commit()
    cur.close()
    audit(db, request_id, 'MEASURED', actor=actor,
          detail='pd=%.1f face_width=%.1f' % (pd_mm, face_width_mm))
    return True, None


def purge_due_images(db, now=None):
    """Delete photos past their retention date, keeping the measurements.

    Retention is about the image, not the fact: the millimetres a human read
    stay, the biometric-adjacent photo does not.
    """
    ensure_schema(db)
    cur = db.cursor()
    cur.execute("""SELECT request_id, image_path FROM face_scan_requests
                    WHERE image_path IS NOT NULL AND purged_at IS NULL
                      AND purge_after IS NOT NULL AND purge_after <= %s""",
                (now or datetime.now(),))
    due = cur.fetchall()
    cur.close()
    purged = []
    for item in due:
        try:
            os.remove(os.path.join(_upload_dir(), item['image_path']))
        except OSError:
            pass  # already gone; the row still needs closing
        cur = db.cursor()
        cur.execute("""UPDATE face_scan_requests
                          SET purged_at=%s, image_path=NULL
                        WHERE request_id=%s""",
                    (now or datetime.now(), item['request_id']))
        db.commit()
        cur.close()
        audit(db, item['request_id'], 'PURGED', actor='retention')
        purged.append(item['request_id'])
    return purged


def send_request_whatsapp(db, row, token, site_host=None):
    """Send the customer their upload link. Returns the tracked send result.

    ``send_whatsapp`` is template-only, so this needs an approved template with
    a URL variable; without ``FACE_SCAN_WA_TEMPLATE`` configured it reports
    ``no_template`` rather than pretending to have sent something.
    """
    from .notifications import send_whatsapp_tracked
    template = current_app.config.get('FACE_SCAN_WA_TEMPLATE', '')
    phone = (row['contact_phone'] or '').replace('+', '').replace(' ', '')
    phone = phone.replace('-', '')
    if not template:
        result = {"ok": False, "status": "skipped", "error": "no_template",
                  "request_id": ""}
    elif not phone:
        result = {"ok": False, "status": "skipped", "error": "no_phone",
                  "request_id": ""}
    else:
        link = upload_link(token, site_host)
        result = send_whatsapp_tracked(phone, template, {
            "body_1": {"type": "text", "value": row['contact_name'] or 'there'},
            "body_2": {"type": "text", "value": link},
        })
    cur = db.cursor()
    cur.execute(
        """UPDATE face_scan_requests
              SET status=%s, sent_at=%s, msg91_request_id=%s, send_error=%s
            WHERE request_id=%s AND status IN %s""",
        (ST_SENT if result["ok"] else ST_SEND_FAILED,
         datetime.now() if result["ok"] else None,
         result.get("request_id") or None,
         (result.get("error") or '')[:255] or None,
         row['request_id'], OPEN_STATUSES))
    db.commit()
    cur.close()
    audit(db, row['request_id'], 'SENT' if result["ok"] else 'SEND_FAILED',
          detail=result.get("error") or result.get("request_id") or None)
    return result


def upload_link(token, site_host=None):
    """The customer-facing link. Absolute, because it travels over WhatsApp."""
    base = (current_app.config.get('FACE_SCAN_LINK_BASE')
            or (('https://%s' % site_host) if site_host else ''))
    return '%s%s' % (base.rstrip('/'), url_for('face_scan.upload_page',
                                               token=token))


# ── customer-facing ──

@bp.route('/face-scan/<token>', methods=['GET'])
def upload_page(token):
    """The page a WhatsApp link opens. No login: the token is the authority."""
    db = get_db()
    row = _by_token(db, token)
    state = token_state(row)
    if row:
        audit(db, row['request_id'], 'OPENED', detail=state or 'ok')
    return render_template('face_scan_upload.html', token=token, state=state,
                           name=(row or {}).get('contact_name') or '',
                           retention_days=_retention_days()), (
        200 if state is None else 410)


@bp.route('/face-scan/<token>', methods=['POST'])
def upload_photo(token):
    """Accept one validated photo against an open, consented request."""
    db = get_db()
    row = _by_token(db, token)
    state = token_state(row)
    if state:
        return jsonify({'error': state}), 410 if state != 'unknown' else 404
    if request.form.get('consent') not in ('1', 'true', 'on', 'yes'):
        return jsonify({'error': 'consent_required'}), 400
    file_storage = request.files.get('photo')
    if not file_storage or not file_storage.filename:
        return jsonify({'error': 'no_file'}), 400
    ok, error = store_upload(db, row, file_storage)
    if not ok:
        return jsonify({'error': error}), 400
    return jsonify({'success': True,
                    'message': 'Thank you — our team will confirm your '
                               'measurements shortly.'})


# ── staff-facing ──

@bp.route('/admin/face-scan', methods=['GET'])
def admin_list():
    """Open and recently measured requests, newest first."""
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    db = get_db()
    ensure_schema(db)
    cur = db.cursor()
    cur.execute("""SELECT request_id, customer_id, contact_name, contact_phone,
                          status, created_at, expires_at, uploaded_at,
                          measured_at, pd_mm, face_width_mm, measured_by,
                          send_error, purged_at
                     FROM face_scan_requests
                    ORDER BY created_at DESC LIMIT 100""")
    rows = cur.fetchall()
    cur.close()
    if request.args.get('format') == 'json':
        return jsonify({'requests': rows})
    return render_template('face_scan_admin.html', rows=rows)


@bp.route('/admin/face-scan/new', methods=['POST'])
def admin_create():
    """Create a request and, when a template is configured, send the link.

    Returns the link itself as well: the template may not be approved yet, and
    a staff member who can copy the link into any channel is not blocked on it.
    """
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json(silent=True) or request.form
    phone = (data.get('phone') or '').strip()
    name = (data.get('name') or '').strip()
    customer_id = data.get('customer_id') or None
    if not phone:
        return jsonify({'error': 'phone required'}), 400
    try:
        customer_id = int(customer_id) if customer_id else None
    except (TypeError, ValueError):
        return jsonify({'error': 'customer_id must be numeric'}), 400
    db = get_db()
    actor = session.get('user_email') or 'bearer_token'
    request_id, token = create_request(db, customer_id=customer_id,
                                       contact_name=name, contact_phone=phone,
                                       created_by=actor)
    row = _by_id(db, request_id)
    send = send_request_whatsapp(db, row, token, site_host=request.host)
    return jsonify({'request_id': request_id,
                    'link': upload_link(token, request.host),
                    'whatsapp': send.get('status'),
                    'whatsapp_error': send.get('error') or ''})


@bp.route('/admin/face-scan/<request_id>/photo', methods=['POST'])
def admin_upload(request_id):
    """Attach a photo the customer sent us by some other channel.

    A customer who was already asked for a scan, and replied on WhatsApp or by
    mail, has a photo we cannot fetch programmatically: MSG91 hands over inbound
    media only through a webhook we do not have. So staff attach it here and it
    joins the same validated, audited, retained path as a link upload.

    The customer's own consent tick is missing by definition, so the staff member
    asserts consent explicitly and is recorded as the one who did.
    """
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.form
    if data.get('consent_confirmed') not in ('1', 'true', 'on', 'yes'):
        return jsonify({'error': 'consent_not_confirmed'}), 400
    file_storage = request.files.get('photo')
    if not file_storage or not file_storage.filename:
        return jsonify({'error': 'no_file'}), 400
    db = get_db()
    row = _by_id(db, request_id)
    if not row:
        return jsonify({'error': 'not_found'}), 404
    state = token_state(row)
    if state:
        return jsonify({'error': state}), 409
    actor = session.get('user_email') or 'bearer_token'
    ok, error = store_upload(db, row, file_storage,
                             source=SRC_STAFF_UPLOAD, actor=actor)
    if not ok:
        return jsonify({'error': error}), 400
    return jsonify({'success': True, 'request_id': request_id})


@bp.route('/admin/face-scan/receive', methods=['POST'])
def admin_receive():
    """One step for a customer already asked: create the record and attach the photo.

    Nobody should have to create a link they are never going to send in order to
    file a photo that has already arrived.
    """
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.form
    if data.get('consent_confirmed') not in ('1', 'true', 'on', 'yes'):
        return jsonify({'error': 'consent_not_confirmed'}), 400
    file_storage = request.files.get('photo')
    if not file_storage or not file_storage.filename:
        return jsonify({'error': 'no_file'}), 400
    customer_id = data.get('customer_id') or None
    try:
        customer_id = int(customer_id) if customer_id else None
    except (TypeError, ValueError):
        return jsonify({'error': 'customer_id must be numeric'}), 400
    db = get_db()
    actor = session.get('user_email') or 'bearer_token'
    request_id, _token = create_request(
        db, customer_id=customer_id,
        contact_name=(data.get('name') or '').strip() or None,
        contact_phone=(data.get('phone') or '').strip() or None,
        created_by=actor)
    row = _by_id(db, request_id)
    ok, error = store_upload(db, row, file_storage,
                             source=SRC_STAFF_UPLOAD, actor=actor)
    if not ok:
        return jsonify({'error': error, 'request_id': request_id}), 400
    return jsonify({'success': True, 'request_id': request_id,
                    'next': url_for('face_scan.admin_detail',
                                    request_id=request_id)})


@bp.route('/admin/face-scan/<request_id>', methods=['GET'])
def admin_detail(request_id):
    """The photo beside the measurement form."""
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    db = get_db()
    row = _by_id(db, request_id)
    if not row:
        return jsonify({'error': 'not_found'}), 404
    if request.args.get('format') == 'json':
        safe = {k: v for k, v in row.items() if k != 'token_hash'}
        return jsonify(safe)
    return render_template('face_scan_detail.html', r=row,
                           pd_range=(PD_MIN, PD_MAX),
                           width_range=(FACE_WIDTH_MIN, FACE_WIDTH_MAX))


@bp.route('/admin/face-scan/<request_id>/image', methods=['GET'])
def admin_image(request_id):
    """Serve the photo to an authenticated admin, and record that it was read."""
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    db = get_db()
    row = _by_id(db, request_id)
    if not row or not row['image_path'] or row['purged_at']:
        return jsonify({'error': 'no_image'}), 404
    # basename defends the read against a stored path that ever stops being one.
    name = os.path.basename(row['image_path'])
    path = os.path.join(_upload_dir(), name)
    if not os.path.exists(path):
        return jsonify({'error': 'no_image'}), 404
    audit(db, request_id, 'IMAGE_VIEWED',
          actor=session.get('user_email') or 'bearer_token')
    ext = name.rsplit('.', 1)[-1].lower()
    resp = send_file(path, mimetype=CONTENT_TYPES.get(ext,
                                                      'application/octet-stream'))
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.route('/admin/face-scan/<request_id>/measurements', methods=['POST'])
def admin_measure(request_id):
    """Record what a human measured, attributed to that human."""
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json(silent=True) or request.form
    if data.get('scale_confirmed') not in ('1', 'true', 'on', 'yes'):
        return jsonify({'error': 'scale_reference_not_confirmed'}), 400
    db = get_db()
    ok, error = record_measurements(
        db, request_id, data.get('pd_mm'), data.get('face_width_mm'),
        actor=session.get('user_email') or 'bearer_token',
        note=data.get('note'))
    if not ok:
        return jsonify({'error': error}), 400
    row = _by_id(db, request_id)
    return jsonify({'success': True, 'pd_mm': float(row['pd_mm']),
                    'face_width_mm': float(row['face_width_mm']),
                    'measured_by': row['measured_by']})
