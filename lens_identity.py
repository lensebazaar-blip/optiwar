"""What a lens is called, what it used to be called, and what is true of it.

One lens is one canonical product with one slug of our own. CooperVision's
price list says Aspire Go Max is what Clariti became, Aspire Pro what Biofinity
became, Aspire Air what Avaira Vitality became: that is one product each with
names it has been known by, not two products. The names live in
``contact_lens_aliases``; a customer, the assistant and search find the lens
by any of them, and the page says "formerly Clariti 1 Day" in one place.

``alias_type``:

``ALSO_KNOWN_AS``   a name the trade or the customer uses; shown on the page,
                    published as JSON-LD ``alternateName``, searchable
``LEGACY_SLUG``     a URL path an earlier site used for this lens; answered
                    with a 301 to the canonical page — on the storefront the
                    row names, and only where the lens is released there
``INTERNAL_CODE``   a manufacturer/material code that tracks a mould or a
                    power range (B4/U4/HO4); audit only, never customer-facing
``LEGACY_REF``      the id this lens had in a source system (LensBazaar);
                    provenance only

A redirect is exposure: on optiwar.in no lens exists today, so a LEGACY_SLUG
for ``.in`` is stored with the lens and answers nothing until the lens is
released there. ``redirect_target`` is never read from the alias row — the
target is the canonical product, resolved when the redirect is served.

``contact_lens_specs`` holds the technical statements a page, a passport, a
feed and the assistant read — value plus the condition it holds under, plus
where it came from and whether a person confirmed it. "Water content 58%" has
no condition; "BC 8.6 for powers +0.25 and above" does. A spec with a source
of ``LEGACY_DATABASE`` and ``verified = 0`` is displayed to nobody.
"""
import datetime
import hashlib
import json

ALIAS_ALSO_KNOWN_AS = "ALSO_KNOWN_AS"
ALIAS_LEGACY_SLUG = "LEGACY_SLUG"
ALIAS_INTERNAL_CODE = "INTERNAL_CODE"
ALIAS_LEGACY_REF = "LEGACY_REF"
ALIAS_TYPES = (ALIAS_ALSO_KNOWN_AS, ALIAS_LEGACY_SLUG, ALIAS_INTERNAL_CODE,
               ALIAS_LEGACY_REF)

# Where a statement came from, lowest to highest precedence.
SOURCE_LEGACY_DATABASE = "LEGACY_DATABASE"
SOURCE_SUPPLIER_LIST = "SUPPLIER_LIST"
SOURCE_CARTON = "CARTON_PHOTO"
SOURCE_MANUFACTURER_CHART = "MANUFACTURER_CHART"
SOURCE_OPS_DECISION = "OPS_COMMERCIAL_DECISION"
SOURCES = (SOURCE_LEGACY_DATABASE, SOURCE_SUPPLIER_LIST, SOURCE_CARTON,
           SOURCE_MANUFACTURER_CHART, SOURCE_OPS_DECISION)

ALIASES_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_aliases (
    alias_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id  INT NOT NULL,
    alias_type  VARCHAR(16) NOT NULL,
    value       VARCHAR(200) NOT NULL,
    value_norm  VARCHAR(200) NOT NULL,
    site        VARCHAR(32) NULL,
    note        VARCHAR(200) NULL,
    source_type VARCHAR(40) NULL,
    source_ref  VARCHAR(200) NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_cl_alias (alias_type, value_norm, site),
    KEY idx_cl_alias_product (product_id, alias_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SPECS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_specs (
    spec_id        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id     INT NOT NULL,
    spec_key       VARCHAR(40) NOT NULL,
    spec_value     VARCHAR(200) NOT NULL,
    unit           VARCHAR(16) NULL,
    condition_json TEXT NULL,
    condition_sig  CHAR(40) NOT NULL DEFAULT '',
    source_type    VARCHAR(40) NOT NULL,
    source_ref     VARCHAR(200) NULL,
    verified       TINYINT(1) NOT NULL DEFAULT 0,
    verified_by    VARCHAR(80) NULL,
    verified_at    DATETIME NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_cl_spec (product_id, spec_key, condition_sig)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (
    ("contact_lens_aliases", ALIASES_SCHEMA),
    ("contact_lens_specs", SPECS_SCHEMA),
)


def normalise(value):
    """How two names are compared: case, spacing and the ® nobody types."""
    text = (value or "").replace("\u00ae", "").replace("\u2122", "")
    return " ".join(text.lower().split())


def add_alias(cursor, product_id, alias_type, value, site=None, note=None,
              source_type=None, source_ref=None):
    if alias_type not in ALIAS_TYPES:
        raise ValueError("unknown alias type %r" % (alias_type,))
    value = (value or "").strip()
    if not value:
        raise ValueError("empty alias")
    if alias_type == ALIAS_LEGACY_SLUG and not site:
        raise ValueError("a legacy slug names the storefront it redirects on")
    norm = normalise(value) if alias_type != ALIAS_LEGACY_SLUG \
        else value.strip("/").lower()
    cursor.execute(
        "INSERT INTO contact_lens_aliases (product_id, alias_type, value, "
        "value_norm, site, note, source_type, source_ref) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
        "product_id = VALUES(product_id), note = VALUES(note), "
        "source_type = VALUES(source_type), source_ref = VALUES(source_ref)",
        (product_id, alias_type, value, norm, site, note, source_type,
         source_ref))


def aliases(cursor, product_id, alias_type=None):
    sql = ("SELECT alias_type, value, site, note, source_type, source_ref "
           "FROM contact_lens_aliases WHERE product_id = %s")
    args = [product_id]
    if alias_type:
        sql += " AND alias_type = %s"
        args.append(alias_type)
    cursor.execute(sql + " ORDER BY alias_type, value", tuple(args))
    return [dict(r) for r in (cursor.fetchall() or ())]


def also_known_as(cursor, product_id):
    """The customer-facing former/other names, in display order."""
    return [a["value"] for a in aliases(cursor, product_id,
                                        ALIAS_ALSO_KNOWN_AS)]


def find_by_name(cursor, name):
    """Product ids whose canonical or alias name matches ``name``."""
    norm = normalise(name)
    if not norm:
        return []
    cursor.execute(
        "SELECT DISTINCT product_id FROM contact_lens_aliases "
        "WHERE alias_type = %s AND value_norm = %s",
        (ALIAS_ALSO_KNOWN_AS, norm))
    return [int(r["product_id"] if isinstance(r, dict) else r[0])
            for r in (cursor.fetchall() or ())]


def legacy_redirect(cursor, site, path):
    """The product a legacy path on this storefront belongs to, or ``None``.

    Only the row's own storefront answers, and the caller still has to check
    the product is released there before sending anyone: a redirect to a page
    that returns 404 is a lens exposed by its URL.
    """
    norm = (path or "").strip().strip("/").lower()
    if not norm or not site:
        return None
    cursor.execute(
        "SELECT product_id FROM contact_lens_aliases WHERE alias_type = %s "
        "AND value_norm = %s AND site = %s", (ALIAS_LEGACY_SLUG, norm, site))
    row = cursor.fetchone()
    if not row:
        return None
    return int(row["product_id"] if isinstance(row, dict) else row[0])


def condition_signature(condition):
    if not condition:
        return ""
    return hashlib.sha1(json.dumps(condition, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()


def set_spec(cursor, product_id, key, value, source_type, unit=None,
             condition=None, source_ref=None, verified=False,
             verified_by=None, now=None):
    """State one technical fact, under one condition, from one source.

    A restatement from a higher-precedence source replaces a lower one; a
    restatement from a lower source leaves the higher one standing. Two
    sources of the same rank disagreeing is a conflict the caller must
    resolve — it is refused here, not averaged.
    """
    if source_type not in SOURCES:
        raise ValueError("unknown spec source %r" % (source_type,))
    sig = condition_signature(condition)
    cursor.execute(
        "SELECT spec_value, source_type FROM contact_lens_specs "
        "WHERE product_id=%s AND spec_key=%s AND condition_sig=%s",
        (product_id, key, sig))
    row = cursor.fetchone()
    if row:
        row = dict(row) if isinstance(row, dict) else \
            {"spec_value": row[0], "source_type": row[1]}
        have, want = SOURCES.index(row["source_type"]), \
            SOURCES.index(source_type)
        if want < have:
            return False
        if want == have and str(row["spec_value"]) != str(value):
            raise ValueError("%s for product %s: %s says %r and %r; resolve "
                             "before restating" % (key, product_id,
                                                   source_type,
                                                   row["spec_value"], value))
    stamp = None
    if verified:
        stamp = (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO contact_lens_specs (product_id, spec_key, spec_value, "
        "unit, condition_json, condition_sig, source_type, source_ref, "
        "verified, verified_by, verified_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
        "spec_value = VALUES(spec_value), unit = VALUES(unit), "
        "condition_json = VALUES(condition_json), "
        "source_type = VALUES(source_type), source_ref = VALUES(source_ref), "
        "verified = VALUES(verified), verified_by = VALUES(verified_by), "
        "verified_at = VALUES(verified_at)",
        (product_id, key, str(value), unit,
         json.dumps(condition, sort_keys=True) if condition else None, sig,
         source_type, source_ref, 1 if verified else 0, verified_by, stamp))
    return True


def specs(cursor, product_id, verified_only=True):
    """The technical statements about one lens, conditions decoded."""
    sql = ("SELECT spec_key, spec_value, unit, condition_json, source_type, "
           "source_ref, verified FROM contact_lens_specs WHERE product_id=%s")
    if verified_only:
        sql += " AND verified = 1"
    cursor.execute(sql + " ORDER BY spec_key, condition_sig", (product_id,))
    out = []
    for row in cursor.fetchall() or ():
        row = dict(row)
        row["condition"] = json.loads(row.pop("condition_json")) \
            if row.get("condition_json") else None
        out.append(row)
    return out
