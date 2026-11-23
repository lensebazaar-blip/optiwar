"""Frames held off sale because of third-party trademarks (brand audit, 23 Nov 2026).

Every frame below showed another company's brand in its live product photos:
on the frame itself (``frame_mark``: return to the vendor, never relist) or
only on the removable demo-lens sticker (``lens_sticker``: peel the sticker,
reshoot, relist under the new name in RENAME).

HELD frames are refused on both sites whatever the database says: no product
page (404), no listing, search, feed, sitemap, try-on or chat result. The
database switch (sell_on_in / sell_on_com = 0, see
scripts/brand_compliance_apply.py) is the primary control; this list is the
backstop so a stale row can never put one back on sale.

To release a frame: the products team confirms the physical frame carries no
other brand and new photos are in place, then remove its line here.
"""

# product_id: (sku, kind, what the photos showed)
HELD = {
    26: ('AL54', 'frame_mark', 'Dolce on arm'),
    27: ('AL66', 'frame_mark', 'Titan Eye+ on arm'),
    29: ('AL55', 'frame_mark', 'Dolce on arm'),
    45: ('AE45', 'frame_mark', 'TBOSS and Tommy flag on arm'),
    47: ('AE47', 'frame_mark', 'Tommy-style flag on arm'),
    49: ('AE39', 'frame_mark', 'TBOSS lens, Tommy flag on arm'),
    94: ('AG63', 'frame_mark', 'POLO SPORT inside arm and lens'),
    96: ('AG52', 'frame_mark', 'possible Ray-Ban script inside arm'),
    97: ('AG62', 'frame_mark', 'POLO SPORT inside arm and lens'),
    111: ('AK19', 'frame_mark', 'Tom Ford-style T at hinge'),
    112: ('AK20', 'frame_mark', 'Tom Ford-style T at hinge'),
    136: ('AE57', 'frame_mark', 'Tommy flag on arm'),
    137: ('AE55', 'frame_mark', 'Tommy flag on arm'),
    141: ('AE53', 'frame_mark', 'Tommy flag on arm'),
    145: ('BB27', 'frame_mark', 'Tommy-style flag on arm'),
    146: ('BB26', 'frame_mark', 'Tommy-style flag on arm'),
    170: ('BP36', 'frame_mark', 'Armani eagle on arm'),
    178: ('AI96', 'frame_mark', 'SHARAPOVA lens sticker, text on arm'),
    203: ('BB31', 'frame_mark', 'Tommy-style flag on arm'),
    204: ('BB32', 'frame_mark', 'Tommy-style flag on arm'),
    214: ('BP22', 'frame_mark', 'CADILLAC V on arm'),
    218: ('BQ50', 'frame_mark', 'Rodenstock-style R on arm'),
    219: ('BQ48', 'frame_mark', 'Rodenstock-style R on arm'),
    220: ('BQ43', 'frame_mark', 'Rodenstock-style R on arm'),
    222: ('BQ47', 'frame_mark', 'Rodenstock-style R on arm'),
    223: ('BQ51', 'frame_mark', 'Rodenstock-style R on arm'),
    224: ('BQ45', 'frame_mark', 'Rodenstock-style R on arm'),
    225: ('BQ49', 'frame_mark', 'Rodenstock-style R on arm'),
    261: ('BB22', 'frame_mark', 'Tommy-style flag on arm'),
    298: ('AA85', 'frame_mark', 'OLIVER on arm'),
    302: ('BB85', 'frame_mark', 'Tommy-style flag on arm'),
    330: ('AL71', 'lens_sticker', 'Porsche crest lens sticker'),
    331: ('AL76', 'lens_sticker', 'Porsche crest lens sticker'),
    332: ('AL69', 'lens_sticker', 'Porsche crest lens sticker'),
    333: ('AL73', 'lens_sticker', 'Porsche crest lens sticker'),
    334: ('AL75', 'lens_sticker', 'Porsche crest lens sticker'),
    336: ('AL70', 'lens_sticker', 'Porsche crest lens sticker'),
    340: ('AF83', 'frame_mark', 'Puma logo on arm'),
    366: ('BB33', 'frame_mark', 'Tommy-style flag on arm'),
    370: ('AI86', 'frame_mark', 'Chrome Hearts-style cross'),
    372: ('AI85', 'frame_mark', 'Chrome Hearts-style cross'),
    373: ('AI87', 'frame_mark', 'Chrome Hearts-style cross'),
    374: ('AN41', 'frame_mark', 'Chrome Hearts-style cross'),
    411: ('BB74', 'frame_mark', 'Tommy-style flag on arm'),
    423: ('BH83', 'frame_mark', 'Oakley logo on arm'),
    424: ('BH81', 'frame_mark', 'Tommy flag on arm'),
    426: ('AI91', 'frame_mark', 'Chrome Hearts-style cross'),
    427: ('AI71', 'frame_mark', 'TOMMY SPORTS inside arm and lens'),
    428: ('AI89', 'frame_mark', 'Chrome Hearts-style cross'),
    429: ('AI92', 'frame_mark', 'Chrome Hearts-style cross'),
    430: ('AI73', 'frame_mark', 'TOMMY SPORTS on arm and lens'),
    433: ('BO51', 'frame_mark', 'United Colors print on arm'),
    434: ('BO47', 'frame_mark', 'United Colors print on arm'),
    436: ('BO54', 'frame_mark', 'United Colors print on arm'),
    437: ('BO50', 'frame_mark', 'United Colors print on arm'),
    441: ('AL65', 'frame_mark', 'LV monogram on arm'),
    442: ('AL64', 'frame_mark', 'Chrome Hearts-style hinge'),
    444: ('AL61', 'frame_mark', 'Cartier on arm'),
    464: ('BH94', 'frame_mark', 'Tommy-style flag on arm'),
    485: ('AA12', 'frame_mark', 'Oakley logo on arm'),
    524: ('BP13', 'frame_mark', 'Oakley-style O on arm'),
    525: ('BP14', 'frame_mark', 'Oakley-style O on arm'),
    528: ('BQ26', 'frame_mark', 'Tommy-style flag on arm'),
    530: ('BP65', 'frame_mark', 'Armani-style eagle on arm'),
    531: ('BP67', 'frame_mark', 'Armani-style eagle on arm'),
    532: ('BP68', 'frame_mark', 'Armani-style eagle on arm'),
    533: ('BP69', 'frame_mark', 'Armani-style eagle on arm'),
    534: ('BP71', 'frame_mark', 'Armani-style eagle on arm'),
    545: ('AJ20', 'frame_mark', 'Tesla-style T on arm'),
    547: ('AJ21', 'frame_mark', 'Tesla-style T on arm'),
    549: ('BH92', 'frame_mark', 'Tommy flag on arm'),
    550: ('BH90', 'frame_mark', 'Nike swoosh on arm'),
    551: ('BH93', 'frame_mark', 'Tommy-style flag on arm'),
    552: ('BH91', 'frame_mark', 'Nike swoosh on arm'),
    584: ('AM45', 'frame_mark', 'Jack Johnson on arm'),
    587: ('AM44', 'frame_mark', 'Jack Johnson on arm'),
    660: ('AB16', 'frame_mark', 'Diesel logo on arm'),
    663: ('AB17', 'frame_mark', 'Diesel logo on arm'),
    665: ('AB29', 'frame_mark', 'BOSS on arm and lens'),
    666: ('AB31', 'frame_mark', 'BOSS on arm'),
    668: ('AB27', 'frame_mark', 'Ray-Ban logo on arm and lens'),
    669: ('AB81', 'frame_mark', 'CLUB MASTER on arm'),
    672: ('AD62', 'frame_mark', 'louis style on arm'),
    675: ('AD99', 'frame_mark', 'Oakley logo on arm'),
    684: ('AB69', 'frame_mark', 'Titan Eye+ on arm'),
    686: ('AB68', 'frame_mark', 'Puma logo on arm'),
    689: ('AB65', 'frame_mark', 'Puma logo on arm and lens'),
    690: ('AB72', 'frame_mark', 'Prada on arm'),
    691: ('AB74', 'frame_mark', 'Fastrack on arm'),
    692: ('AB63', 'frame_mark', 'Puma logo on arm'),
    693: ('AB67', 'frame_mark', 'Puma logo on arm'),
    694: ('AB71', 'frame_mark', 'Prada on arm'),
    695: ('AB64', 'frame_mark', 'Puma logo on arm'),
    696: ('AB85', 'frame_mark', 'RB model number inside arm'),
    767: ('AL11', 'lens_sticker', 'North Face lens sticker'),
    768: ('AL09', 'lens_sticker', 'North Face lens sticker'),
    770: ('AL12', 'lens_sticker', 'North Face lens sticker'),
    776: ('AN28', 'frame_mark', 'ZENNI on arm'),
    808: ('AL37', 'frame_mark', 'Fendi-like mark on arm'),
    813: ('AL40', 'frame_mark', 'Fendi-like mark on arm'),
    814: ('AL38', 'frame_mark', 'Fendi-like lens mark'),
    824: ('AI48', 'frame_mark', 'IDEE on arm'),
    829: ('AI47', 'frame_mark', 'IDEE on arm'),
    842: ('AN58', 'frame_mark', 'AJ monogram at hinge'),
    845: ('AN57', 'frame_mark', 'AJ monogram at hinge'),
    860: ('AG74', 'frame_mark', 'louis style on arm'),
    865: ('BB94', 'frame_mark', 'Tommy-style flag on arm'),
    934: ('AH25', 'frame_mark', 'Tommy-style flag on arm'),
    935: ('BD21', 'frame_mark', 'Kilsgaard inside arm'),
    965: ('AE56', 'frame_mark', 'Tommy flag on arm'),
}

# product_id: (sku, old name, new name). Applied to product_name, product_slug
# and product_details by scripts/brand_compliance_apply.py.
RENAME = {
    50: ('AE01', 'D & T AE01', 'OPTIWAR Round AE01'),
    76: ('BQ54', 'QUEEN ELIZABETH BQ54', 'OPTIWAR Cat Eye BQ54'),
    78: ('BQ55', 'QUEEN ELIZABETH BQ55', 'OPTIWAR Cat Eye BQ55'),
    98: ('AG53', 'TOM HARDY T0037 AG53', 'OPTIWAR Frame AG53'),
    156: ('AM86', 'DASAULTS AM86', 'OPTIWAR Frame AM86'),
    196: ('BP99', 'BANFF CENTRE BP99', 'OPTIWAR Rectangle BP99'),
    198: ('BP98', 'BANFF CENTRE BP98', 'OPTIWAR Rectangle BP98'),
    202: ('AG69', 'LOUIS STYLE N-5072 AG69', 'OPTIWAR Frame AG69'),
    330: ('AL71', 'BRABUS 7803 AL71', 'OPTIWAR Aviator AL71'),
    331: ('AL76', 'BRABUS 7807 AL76', 'OPTIWAR Aviator AL76'),
    332: ('AL69', 'BRABUS 8023 AL69', 'OPTIWAR Round Metal AL69'),
    333: ('AL73', 'BRABUS 7803 AL73', 'OPTIWAR Aviator AL73'),
    334: ('AL75', 'BRABUS 7803 AL75', 'OPTIWAR Aviator AL75'),
    336: ('AL70', 'BRABUS 8023 AL70', 'OPTIWAR Round Metal AL70'),
    417: ('BO76', 'STAR WARS BO76', 'OPTIWAR Round BO76'),
    435: ('BO59', 'TOMMY BO59', 'OPTIWAR Rectangle BO59'),
    542: ('BQ02', 'BANFF CENTRE BQ02', 'OPTIWAR Rectangle BQ02'),
    543: ('BQ01', 'BANFF CENTRE BQ01', 'OPTIWAR Rectangle BQ01'),
    670: ('AD98', 'D & T AD98', 'OPTIWAR Round AD98'),
    671: ('AD61', 'LOUIS STYLE AD61', 'OPTIWAR Browline AD61'),
    676: ('AD65', 'LOUIS STYLE AD65', 'OPTIWAR Panto AD65'),
    767: ('AL11', 'NORTH FACE NF 1002 AL11', 'OPTIWAR Half-Rim AL11'),
    768: ('AL09', 'NORTH FACE NF 1004 AL09', 'OPTIWAR Half-Rim AL09'),
    770: ('AL12', 'NORTH FACE NF 1003 AL12', 'OPTIWAR Half-Rim AL12'),
    786: ('AH02', 'LOUIS STYLE K-8006A AH02', 'OPTIWAR Kids AH02'),
    818: ('BH40', 'SHANGRI-LA BH40', 'OPTIWAR Rectangle BH40'),
    819: ('BH41', 'SHANGRI-LA BH41', 'OPTIWAR Rectangle BH41'),
    821: ('BH38', 'GAS BH38', 'OPTIWAR Rectangle BH38'),
    839: ('BH27', 'UNITED COLORS BH27', 'OPTIWAR Round BH27'),
    862: ('AG72', 'LOUIS STYLE L-1032 AG72', 'OPTIWAR Frame AG72'),
    863: ('AG76', 'LOUIS STYLE L-1032 AG76', 'OPTIWAR Frame AG76'),
    864: ('AG75', 'LOUIS STYLE L-1032 AG75', 'OPTIWAR Frame AG75'),
}


def held_ids():
    return frozenset(HELD)


def is_held(product_or_id):
    """True when this product is held off sale for a trademark reason."""
    pid = product_or_id.get("product_id") if isinstance(product_or_id, dict) else product_or_id
    try:
        return int(pid) in HELD
    except (TypeError, ValueError):
        return False


def sql_exclude(alias=None):
    """`` AND <alias.>product_id NOT IN (...)`` for any product read path."""
    prefix = ("%s." % alias) if alias else ""
    return " AND %sproduct_id NOT IN (%s)" % (prefix, ",".join(str(i) for i in sorted(HELD)))
