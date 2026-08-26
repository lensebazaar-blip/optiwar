"""Contact-lens catalogue: product facts, and the matrix of what can be ordered.

Three tables beside ``products``, which keeps its meaning as the commercial
record (price, slug, images, cart identity) while nothing about frames changes:

``contact_lens_products``   one row per lens: brand, modality, pack, availability
``contact_lens_variants``   one row per orderable parameter combination
``contact_lens_images``     per-colour imagery, replacing the hardcoded dict

The variant table is the authority on what a customer may order. Not the Python
range helpers in ``cl_range_model.py`` (hardcoded per ``product_id``), not the
LLM, and not a nearest match: a combination is orderable when a row exists, and
otherwise it is refused.

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
    matrix_version        INT NOT NULL DEFAULT 1,
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_cl_brand (brand),
    KEY idx_cl_type (lens_type),
    KEY idx_cl_modality (modality),
    KEY idx_cl_availability (availability),
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

IMAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_images (
    image_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    color_code VARCHAR(40) NULL,
    image_url  VARCHAR(500) NOT NULL,
    image_type VARCHAR(16) NOT NULL,
    sort_order SMALLINT NOT NULL DEFAULT 0,
    KEY idx_cl_images (product_id, color_code, sort_order),
    CONSTRAINT fk_cl_image_product FOREIGN KEY (product_id)
        REFERENCES products (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (
    ("contact_lens_products", PROFILE_SCHEMA),
    ("contact_lens_variants", VARIANTS_SCHEMA),
    ("contact_lens_images", IMAGES_SCHEMA),
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
    cursor.execute("SHOW COLUMNS FROM products")
    return {_first(row) for row in cursor.fetchall()}


def _products_indexes(cursor):
    cursor.execute("SHOW INDEX FROM products")
    return {_col(row, "Key_name", 2) for row in cursor.fetchall()}


def _first(row):
    if isinstance(row, dict):
        return row.get("Field") or list(row.values())[0]
    return row[0]


def _col(row, key, pos):
    if isinstance(row, dict):
        return row.get(key)
    return row[pos]
