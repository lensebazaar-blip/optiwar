"""Contact-lens catalogue: product facts, and what can be ordered against them.

Four tables beside ``products``, which keeps its meaning as the commercial
record (price, slug, images, cart identity) while nothing about frames changes:

``contact_lens_products``    one row per lens: brand, modality, pack, availability
``contact_lens_param_rules`` the selectable values of one parameter
``contact_lens_variants``    one row per orderable parameter combination
``contact_lens_images``      per-colour imagery, replacing the hardcoded dict

One lens is one product. A prescription is order configuration, never another
``products`` row, and what a customer may choose is stated in exactly one of two
shapes — ``param_mode`` on the profile says which, and no other shape exists:

``RULES``   the source states each parameter independently: these spheres, these
            cylinders, these axes. Every combination of the stated values is
            orderable, because that is what the source asserts and nothing
            narrower has been supplied. 78 rows describe a toric lens.
``MATRIX``  the source states availability per combination — sphere -4.50 in
            cylinder -0.75 at axis 10 and 20 only. One row per combination.

The distinction is provenance, not convenience: MATRIX is used when a
manufacturer chart supplies the dependencies, and RULES when the source holds
none. It is never right to invent the dependency by materialising a cross
product and calling it a matrix — the row count would then claim a manufacturer
fact nobody supplied. A lens is never in both shapes.

Either way the stored values are the authority on what a customer may order.
Not the Python range helpers in ``cl_range_model.py`` (hardcoded per
``product_id``), not the LLM, and not a nearest match: a selection is orderable
when it is stated, and otherwise it is refused.

NULL vs 0.00 is load-bearing:

    SPH = 0.00    plano — a real, orderable power
    CYL = NULL    this lens has no cylinder at all

so a spherical lens is not silently treated as a toric with zero cylinder.

Because MySQL treats NULLs as distinct in a UNIQUE index, a unique key over the
nullable parameter columns would not actually prevent duplicate spherical rows
(``UNIQUE (sph, cyl, ...)`` admits ten copies of ``-2.00, NULL, NULL``). The
uniqueness is therefore carried by a stored generated column that maps NULL to a
sentinel, so re-running the importer upserts instead of accumulating.

Availability is deliberately NOT frame stock. Lenses are continuously
replenished: an order does not decrement ``product_quantity``, and a lens is
never OUT_OF_STOCK. It is IN_STOCK, or ON_ORDER with a lead time, and it is
purchasable in both states.
"""
try:
    from . import lens_minimums
except ImportError:  # run as a plain module (tests, deploy tool, scripts)
    import lens_minimums

VERTICAL = "CONTACT_LENS"

AVAILABILITY_IN_STOCK = "IN_STOCK"
AVAILABILITY_ON_ORDER = "ON_ORDER"
AVAILABILITIES = (AVAILABILITY_IN_STOCK, AVAILABILITY_ON_ORDER)

MODALITIES = ("DAILY", "MONTHLY", "CONVENTIONAL")
LENS_TYPES = ("SPHERICAL", "TORIC", "MULTIFOCAL", "TORIC_MULTIFOCAL", "COLOR")

# ---------------------------------------------------------------------------
# Schema. Additive in both directions: the columns carry defaults so the running
# release keeps inserting products without knowing about them, and the three
# tables are read by nothing until the lens code ships.
# ---------------------------------------------------------------------------

PRODUCTS_COLUMNS = (
    ("product_vertical", "VARCHAR(24) NOT NULL DEFAULT 'EYEWEAR'"),
    ("sell_on_com", "TINYINT(1) NOT NULL DEFAULT 1"),
    ("sell_on_in", "TINYINT(1) NOT NULL DEFAULT 1"),
)

# Columns added to an existing ``contact_lens_products``. The release flag
# defaults to 0, so a lens loaded by the importer is in the database and on no
# surface until somebody sets it.
PROFILE_COLUMNS = (
    ("merchant_enabled", "TINYINT(1) NOT NULL DEFAULT 0"),
    # Where a lens came from, so re-running the importer updates the product it
    # created last time instead of making a second one.
    ("source_system", "VARCHAR(32) NULL"),
    ("source_ref", "VARCHAR(64) NULL"),
    ("imported_at", "DATETIME NULL"),
    # Which shape states what is orderable, and where that statement came from,
    # so a reader can tell an independent parameter list from a manufacturer's
    # combination chart without counting rows.
    ("param_mode", "VARCHAR(8) NOT NULL DEFAULT 'MATRIX'"),
    ("param_source", "VARCHAR(40) NULL"),
    # The source's own manufacturer string, kept beside the canonical one we
    # publish: "Johnsons and Johnsons" is not shown to anybody, and is the only
    # way to prove what the export said.
    ("source_manufacturer", "VARCHAR(120) NULL"),
    # Minimum boxes, per product and enforced on .com only. NULL is no minimum.
    ("min_boxes_single_eye", "SMALLINT UNSIGNED NULL"),
    ("min_boxes_both_per_eye", "SMALLINT UNSIGNED NULL"),
    # The row of ``contact_lens_min_order`` (lens_minimums) those two were
    # taken from, so a re-seed can refresh them and a reader can see why.
    ("min_order_model", "VARCHAR(120) NULL"),
    # EUR is the price we are given; the INR columns on ``products`` are derived
    # from it. The rate and when it was applied are recorded so a rupee price
    # can be explained, and so nobody converts a converted price again.
    ("eur_inr_rate", "DECIMAL(10,4) NULL"),
    ("eur_inr_rate_at", "DATETIME NULL"),
)

# Columns added to an existing ``contact_lens_images``. An image is a
# photograph of a commerce view, and what a surface may do with it depends on
# which view it is: the label sample states one physical box's power against an
# offer covering the whole matrix, so it belongs on the page and not in a
# merchant feed. ``gmc_eligible`` defaults to 1 so imagery loaded before these
# columns existed keeps behaving as it did.
IMAGES_COLUMNS = (
    ("view_code", "VARCHAR(32) NULL"),
    ("view_name", "VARCHAR(24) NULL"),
    ("alt_text", "VARCHAR(255) NULL"),
    ("gmc_eligible", "TINYINT(1) NOT NULL DEFAULT 1"),
)

# One row per view of one product, so re-running the image importer replaces
# the record of that view instead of adding a second one.
IMAGES_INDEXES = (
    ("uq_cl_image_view", "product_id, color_code, view_code"),
)

PARAM_MODE_RULES = "RULES"
PARAM_MODE_MATRIX = "MATRIX"
PARAM_MODES = (PARAM_MODE_RULES, PARAM_MODE_MATRIX)

# The parameters a lens can be configured on, in the order a customer meets
# them. ``color`` is a code; the rest are numbers held as canonical text so the
# rule and the form compare as strings.
PARAMETERS = ("base_curve", "diameter", "sph", "cyl", "axis", "add_power",
              "color")

# Idempotence is carried by the index, not by the importer remembering: two rows
# claiming the same source product are refused by the database.
PROFILE_INDEXES = (
    ("uq_cl_source", "source_system, source_ref"),
)

PRODUCTS_INDEXES = (
    ("idx_vertical_status", "product_vertical, product_status"),
    ("idx_com_listing", "sell_on_com, show_in_listings, product_status"),
    ("idx_in_listing", "sell_on_in, show_in_listings, product_status"),
)

PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_products (
    product_id            INT NOT NULL PRIMARY KEY,
    brand                 VARCHAR(80) NOT NULL,
    manufacturer          VARCHAR(120) NOT NULL,
    gtin                  VARCHAR(32) NULL,
    manufacturer_mpn      VARCHAR(80) NULL,
    modality              VARCHAR(16) NOT NULL,
    lens_type             VARCHAR(20) NOT NULL,
    pack_quantity         SMALLINT UNSIGNED NOT NULL,
    material              VARCHAR(100) NULL,
    water_content         DECIMAL(5,2) NULL,
    silicone_hydrogel     TINYINT(1) NOT NULL DEFAULT 0,
    replacement_days      SMALLINT UNSIGNED NULL,
    availability          VARCHAR(16) NOT NULL DEFAULT 'IN_STOCK',
    lead_time_days        SMALLINT UNSIGNED NULL,
    expected_available_at DATETIME NULL,
    prescription_required TINYINT(1) NOT NULL DEFAULT 1,
    color_enabled         TINYINT(1) NOT NULL DEFAULT 0,
    merchant_enabled      TINYINT(1) NOT NULL DEFAULT 0,
    matrix_version        INT NOT NULL DEFAULT 1,
    source_system         VARCHAR(32) NULL,
    source_ref            VARCHAR(64) NULL,
    imported_at           DATETIME NULL,
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_cl_brand (brand),
    KEY idx_cl_type (lens_type),
    KEY idx_cl_modality (modality),
    KEY idx_cl_availability (availability),
    UNIQUE KEY uq_cl_source (source_system, source_ref),
    CONSTRAINT fk_cl_product FOREIGN KEY (product_id)
        REFERENCES products (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# ``variant_sig`` is what uniqueness is enforced on. NULL means "parameter does
# not apply to this lens", and COALESCE maps it to a sentinel no real value can
# collide with, so one spherical -2.00 row can exist exactly once.
VARIANTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_variants (
    variant_id  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id  INT NOT NULL,
    sph         DECIMAL(5,2) NULL,
    cyl         DECIMAL(5,2) NULL,
    axis        SMALLINT NULL,
    add_power   DECIMAL(5,2) NULL,
    base_curve  DECIMAL(4,2) NULL,
    diameter    DECIMAL(4,2) NULL,
    color_code  VARCHAR(40) NOT NULL DEFAULT '',
    color_name  VARCHAR(80) NULL,
    available   TINYINT(1) NOT NULL DEFAULT 1,
    variant_sig VARCHAR(160) AS (CONCAT_WS('|',
                    COALESCE(CAST(sph AS CHAR), 'NA'),
                    COALESCE(CAST(cyl AS CHAR), 'NA'),
                    COALESCE(CAST(axis AS CHAR), 'NA'),
                    COALESCE(CAST(add_power AS CHAR), 'NA'),
                    COALESCE(CAST(base_curve AS CHAR), 'NA'),
                    COALESCE(CAST(diameter AS CHAR), 'NA'),
                    color_code)) PERSISTENT,
    UNIQUE KEY uq_cl_variant (product_id, variant_sig),
    KEY idx_cl_select (product_id, available, sph, cyl, axis, add_power),
    CONSTRAINT fk_cl_variant_product FOREIGN KEY (product_id)
        REFERENCES products (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# One row per selectable value of one parameter. Deliberately not a matrix: it
# says "this lens is made in these cylinders", which is all a source that holds
# no combination data can honestly say.
PARAM_RULES_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_param_rules (
    rule_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    parameter  VARCHAR(16) NOT NULL,
    value      VARCHAR(40) NOT NULL,
    label      VARCHAR(80) NULL,
    sort_order INT NOT NULL DEFAULT 0,
    available  TINYINT(1) NOT NULL DEFAULT 1,
    UNIQUE KEY uq_cl_rule (product_id, parameter, value),
    KEY idx_cl_rule_read (product_id, available, parameter, sort_order),
    CONSTRAINT fk_cl_rule_product FOREIGN KEY (product_id)
        REFERENCES products (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

IMAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_images (
    image_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    color_code VARCHAR(40) NULL,
    image_url  VARCHAR(500) NOT NULL,
    image_type VARCHAR(16) NOT NULL,
    sort_order SMALLINT NOT NULL DEFAULT 0,
    view_code  VARCHAR(32) NULL,
    view_name  VARCHAR(24) NULL,
    alt_text   VARCHAR(255) NULL,
    gmc_eligible TINYINT(1) NOT NULL DEFAULT 1,
    KEY idx_cl_images (product_id, color_code, sort_order),
    UNIQUE KEY uq_cl_image_view (product_id, color_code, view_code),
    CONSTRAINT fk_cl_image_product FOREIGN KEY (product_id)
        REFERENCES products (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (
    ("contact_lens_products", PROFILE_SCHEMA),
    ("contact_lens_param_rules", PARAM_RULES_SCHEMA),
    ("contact_lens_variants", VARIANTS_SCHEMA),
    ("contact_lens_images", IMAGES_SCHEMA),
    ("contact_lens_min_order", lens_minimums.SCHEMA),
)

_SCHEMA_READY = False


def ensure_schema(cursor):
    """Create the three tables and add the ``products`` columns if absent.

    Idempotent and once per process. Normally everything here has already been
    applied deliberately by ``deploy/deploy.py migrate``, which reads this
    module's declarations so the two cannot disagree; this exists so a fresh
    database (a test, a new node) is not a special case.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    for _name, ddl in TABLES:
        cursor.execute(ddl)
    have_profile = _table_columns(cursor, "contact_lens_products")
    for name, decl in PROFILE_COLUMNS:
        if name not in have_profile:
            cursor.execute("ALTER TABLE contact_lens_products ADD COLUMN %s %s"
                           % (name, decl))
    have_profile_idx = _table_indexes(cursor, "contact_lens_products")
    for name, cols in PROFILE_INDEXES:
        if name not in have_profile_idx:
            cursor.execute("ALTER TABLE contact_lens_products ADD UNIQUE KEY"
                           " %s (%s)" % (name, cols))
    have_images = _table_columns(cursor, "contact_lens_images")
    for name, decl in IMAGES_COLUMNS:
        if name not in have_images:
            cursor.execute("ALTER TABLE contact_lens_images ADD COLUMN %s %s"
                           % (name, decl))
    have_images_idx = _table_indexes(cursor, "contact_lens_images")
    for name, cols in IMAGES_INDEXES:
        if name not in have_images_idx:
            cursor.execute("ALTER TABLE contact_lens_images ADD UNIQUE KEY"
                           " %s (%s)" % (name, cols))
    have = _products_columns(cursor)
    for name, decl in PRODUCTS_COLUMNS:
        if name not in have:
            cursor.execute("ALTER TABLE products ADD COLUMN %s %s" % (name, decl))
    idx = _products_indexes(cursor)
    for name, cols in PRODUCTS_INDEXES:
        if name not in idx:
            cursor.execute("ALTER TABLE products ADD KEY %s (%s)" % (name, cols))
    _SCHEMA_READY = True


def _products_columns(cursor):
    return _table_columns(cursor, "products")


def _table_columns(cursor, table):
    cursor.execute("SHOW COLUMNS FROM %s" % table)
    return {_first(row) for row in cursor.fetchall()}


def _products_indexes(cursor):
    return _table_indexes(cursor, "products")


def _table_indexes(cursor, table):
    cursor.execute("SHOW INDEX FROM %s" % table)
    return {_col(row, "Key_name", 2) for row in cursor.fetchall()}


def _first(row):
    if isinstance(row, dict):
        return row.get("Field") or list(row.values())[0]
    return row[0]


def _col(row, key, pos):
    if isinstance(row, dict):
        return row.get(key)
    return row[pos]
