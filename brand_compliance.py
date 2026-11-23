"""Trademark hold and display-name policy, generated from settled ops data."""

import re
import unicodedata

HELD = {
    485: ('AA12', 'Oakley', 'frame_mark'),
    298: ('AA85', 'Oliver Peoples / s.Oliver', 'frame_mark'),
    660: ('AB16', 'Diesel', 'frame_mark'),
    663: ('AB17', 'Diesel', 'frame_mark'),
    668: ('AB27', 'Ray-Ban', 'frame_mark'),
    665: ('AB29', 'Hugo Boss', 'frame_mark'),
    666: ('AB31', 'Hugo Boss', 'frame_mark'),
    692: ('AB63', 'Puma', 'frame_mark'),
    695: ('AB64', 'Puma', 'frame_mark'),
    689: ('AB65', 'Puma', 'frame_mark'),
    693: ('AB67', 'Puma', 'frame_mark'),
    686: ('AB68', 'Puma', 'frame_mark'),
    684: ('AB69', 'Titan Eye+', 'frame_mark'),
    694: ('AB71', 'Prada', 'frame_mark'),
    690: ('AB72', 'Prada', 'frame_mark'),
    691: ('AB74', 'Fastrack', 'frame_mark'),
    669: ('AB81', 'Ray-Ban (Clubmaster name)', 'frame_mark'),
    696: ('AB85', 'Ray-Ban (RB model no.)', 'frame_mark'),
    672: ('AD62', 'Louis Vuitton (name)', 'frame_mark'),
    675: ('AD99', 'Oakley', 'frame_mark'),
    49: ('AE39', 'Hugo Boss', 'frame_mark'),
    45: ('AE45', 'Hugo Boss', 'frame_mark'),
    47: ('AE47', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    141: ('AE53', 'Tommy Hilfiger', 'frame_mark'),
    137: ('AE55', 'Tommy Hilfiger', 'frame_mark'),
    965: ('AE56', 'Tommy Hilfiger', 'frame_mark'),
    136: ('AE57', 'Tommy Hilfiger', 'frame_mark'),
    340: ('AF83', 'Puma', 'frame_mark'),
    96: ('AG52', 'Ray-Ban', 'frame_mark'),
    97: ('AG62', 'Polo Ralph Lauren', 'frame_mark'),
    94: ('AG63', 'Polo Ralph Lauren', 'frame_mark'),
    860: ('AG74', 'Louis Vuitton (name)', 'frame_mark'),
    934: ('AH25', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    829: ('AI47', 'IDEE', 'frame_mark'),
    824: ('AI48', 'IDEE', 'frame_mark'),
    427: ('AI71', 'Tommy Hilfiger', 'frame_mark'),
    430: ('AI73', 'Tommy Hilfiger', 'frame_mark'),
    372: ('AI85', 'Chrome Hearts (lookalike cross)', 'frame_mark'),
    370: ('AI86', 'Chrome Hearts (lookalike cross)', 'frame_mark'),
    373: ('AI87', 'Chrome Hearts (lookalike cross)', 'frame_mark'),
    428: ('AI89', 'Chrome Hearts (lookalike cross)', 'frame_mark'),
    426: ('AI91', 'Chrome Hearts (lookalike cross)', 'frame_mark'),
    429: ('AI92', 'Chrome Hearts (lookalike cross)', 'frame_mark'),
    178: ('AI96', 'Maria Sharapova (name)', 'frame_mark'),
    545: ('AJ20', 'Tesla (lookalike T)', 'frame_mark'),
    547: ('AJ21', 'Tesla (lookalike T)', 'frame_mark'),
    111: ('AK19', 'Tom Ford', 'frame_mark'),
    112: ('AK20', 'Tom Ford', 'frame_mark'),
    768: ('AL09', 'The North Face', 'lens_sticker'),
    767: ('AL11', 'The North Face', 'lens_sticker'),
    770: ('AL12', 'The North Face', 'lens_sticker'),
    808: ('AL37', 'Fendi (lookalike)', 'frame_mark'),
    814: ('AL38', 'Fendi (lookalike)', 'frame_mark'),
    813: ('AL40', 'Fendi (lookalike)', 'frame_mark'),
    26: ('AL54', 'Dolce & Gabbana (lookalike)', 'frame_mark'),
    29: ('AL55', 'Dolce & Gabbana (lookalike)', 'frame_mark'),
    444: ('AL61', 'Cartier', 'frame_mark'),
    442: ('AL64', 'Chrome Hearts (lookalike cross)', 'frame_mark'),
    441: ('AL65', 'Louis Vuitton', 'frame_mark'),
    27: ('AL66', 'Titan Eye+', 'frame_mark'),
    332: ('AL69', 'Porsche', 'lens_sticker'),
    336: ('AL70', 'Porsche', 'lens_sticker'),
    330: ('AL71', 'Porsche', 'lens_sticker'),
    333: ('AL73', 'Porsche', 'lens_sticker'),
    334: ('AL75', 'Porsche', 'lens_sticker'),
    331: ('AL76', 'Porsche', 'lens_sticker'),
    587: ('AM44', 'Jack Johnson (name)', 'frame_mark'),
    584: ('AM45', 'Jack Johnson (name)', 'frame_mark'),
    776: ('AN28', 'Zenni', 'frame_mark'),
    374: ('AN41', 'Chrome Hearts (lookalike cross)', 'frame_mark'),
    845: ('AN57', 'Armani Jeans (AJ)', 'frame_mark'),
    842: ('AN58', 'Armani Jeans (AJ)', 'frame_mark'),
    261: ('BB22', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    146: ('BB26', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    145: ('BB27', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    203: ('BB31', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    204: ('BB32', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    366: ('BB33', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    411: ('BB74', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    302: ('BB85', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    865: ('BB94', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    935: ('BD21', 'Kilsgaard', 'frame_mark'),
    424: ('BH81', 'Tommy Hilfiger', 'frame_mark'),
    423: ('BH83', 'Oakley', 'frame_mark'),
    550: ('BH90', 'Nike', 'frame_mark'),
    552: ('BH91', 'Nike', 'frame_mark'),
    549: ('BH92', 'Tommy Hilfiger', 'frame_mark'),
    551: ('BH93', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    464: ('BH94', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    434: ('BO47', 'Benetton', 'frame_mark'),
    437: ('BO50', 'Benetton', 'frame_mark'),
    433: ('BO51', 'Benetton', 'frame_mark'),
    436: ('BO54', 'Benetton', 'frame_mark'),
    524: ('BP13', 'Oakley (lookalike O)', 'frame_mark'),
    525: ('BP14', 'Oakley (lookalike O)', 'frame_mark'),
    214: ('BP22', 'Cadillac', 'frame_mark'),
    170: ('BP36', 'Emporio Armani', 'frame_mark'),
    530: ('BP65', 'Armani (lookalike eagle)', 'frame_mark'),
    531: ('BP67', 'Armani (lookalike eagle)', 'frame_mark'),
    532: ('BP68', 'Armani (lookalike eagle)', 'frame_mark'),
    533: ('BP69', 'Armani (lookalike eagle)', 'frame_mark'),
    534: ('BP71', 'Armani (lookalike eagle)', 'frame_mark'),
    528: ('BQ26', 'Tommy Hilfiger (lookalike flag)', 'frame_mark'),
    220: ('BQ43', 'Rodenstock (lookalike R)', 'frame_mark'),
    224: ('BQ45', 'Rodenstock (lookalike R)', 'frame_mark'),
    222: ('BQ47', 'Rodenstock (lookalike R)', 'frame_mark'),
    219: ('BQ48', 'Rodenstock (lookalike R)', 'frame_mark'),
    225: ('BQ49', 'Rodenstock (lookalike R)', 'frame_mark'),
    218: ('BQ50', 'Rodenstock (lookalike R)', 'frame_mark'),
    223: ('BQ51', 'Rodenstock (lookalike R)', 'frame_mark'),
}

# product_id: (sku, new product_name). The name carries no SKU: every page
# prints product_name followed by product_code, so "OPTIWAR Round" + "BO76"
# reads "OPTIWAR Round BO76".
RENAMED = {
    671: ('AD61', 'OPTIWAR Browline'),
    676: ('AD65', 'OPTIWAR Panto'),
    670: ('AD98', 'OPTIWAR Round'),
    50: ('AE01', 'OPTIWAR Round'),
    98: ('AG53', 'OPTIWAR Frame'),
    202: ('AG69', 'OPTIWAR Frame'),
    862: ('AG72', 'OPTIWAR Frame'),
    864: ('AG75', 'OPTIWAR Frame'),
    863: ('AG76', 'OPTIWAR Frame'),
    786: ('AH02', 'OPTIWAR Kids'),
    768: ('AL09', 'OPTIWAR Half-Rim'),
    767: ('AL11', 'OPTIWAR Half-Rim'),
    770: ('AL12', 'OPTIWAR Half-Rim'),
    332: ('AL69', 'OPTIWAR Round Metal'),
    336: ('AL70', 'OPTIWAR Round Metal'),
    330: ('AL71', 'OPTIWAR Aviator'),
    333: ('AL73', 'OPTIWAR Aviator'),
    334: ('AL75', 'OPTIWAR Aviator'),
    331: ('AL76', 'OPTIWAR Aviator'),
    156: ('AM86', 'OPTIWAR Frame'),
    839: ('BH27', 'OPTIWAR Round'),
    821: ('BH38', 'OPTIWAR Rectangle'),
    818: ('BH40', 'OPTIWAR Rectangle'),
    819: ('BH41', 'OPTIWAR Rectangle'),
    435: ('BO59', 'OPTIWAR Rectangle'),
    417: ('BO76', 'OPTIWAR Round'),
    198: ('BP98', 'OPTIWAR Rectangle'),
    196: ('BP99', 'OPTIWAR Rectangle'),
    543: ('BQ01', 'OPTIWAR Rectangle'),
    542: ('BQ02', 'OPTIWAR Rectangle'),
    76: ('BQ54', 'OPTIWAR Cat Eye'),
    78: ('BQ55', 'OPTIWAR Cat Eye'),
}

KEEP_ON_SALE = {1: "BB44", 362: "AK98"}


STYLE_LABELS = {
    "product_category_clubmaster": "Browline",
    "product_category_wayfarer": "Classic Square",
}
BANNED_STYLE_WORDS = ("clubmaster", "club master", "wayfarer")


def held_ids():
    return frozenset(HELD)


def sql_exclude(alias=None):
    if not HELD:
        return ""
    prefix = "%s." % alias if alias else ""
    ids = ",".join(str(product_id) for product_id in HELD)
    return " AND %sproduct_id NOT IN (%s)" % (prefix, ids)


def is_held(product):
    if isinstance(product, dict):
        product = product.get("product_id")
    try:
        return int(product) in HELD
    except (TypeError, ValueError):
        return False


def display_name(product):
    if isinstance(product, dict):
        product_id = product.get("product_id")
        try:
            renamed = RENAMED.get(int(product_id))
        except (TypeError, ValueError):
            renamed = None
        return renamed[1] if renamed else product.get("product_name")
    try:
        renamed = RENAMED.get(int(product))
    except (TypeError, ValueError):
        renamed = None
    return renamed[1] if renamed else None


def slugify(name):
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)
