"""Canonical India GST states and accepted address variants."""
import re


STATES = (
    ("01", "Jammu & Kashmir"),
    ("02", "Himachal Pradesh"),
    ("03", "Punjab"),
    ("04", "Chandigarh"),
    ("05", "Uttarakhand"),
    ("06", "Haryana"),
    ("07", "Delhi"),
    ("08", "Rajasthan"),
    ("09", "Uttar Pradesh"),
    ("10", "Bihar"),
    ("11", "Sikkim"),
    ("12", "Arunachal Pradesh"),
    ("13", "Nagaland"),
    ("14", "Manipur"),
    ("15", "Mizoram"),
    ("16", "Tripura"),
    ("17", "Meghalaya"),
    ("18", "Assam"),
    ("19", "West Bengal"),
    ("20", "Jharkhand"),
    ("21", "Odisha"),
    ("22", "Chhattisgarh"),
    ("23", "Madhya Pradesh"),
    ("24", "Gujarat"),
    ("26", "Dadra & Nagar Haveli and Daman & Diu"),
    ("27", "Maharashtra"),
    ("29", "Karnataka"),
    ("30", "Goa"),
    ("31", "Lakshadweep"),
    ("32", "Kerala"),
    ("33", "Tamil Nadu"),
    ("34", "Puducherry"),
    ("35", "Andaman & Nicobar Islands"),
    ("36", "Telangana"),
    ("37", "Andhra Pradesh"),
    ("38", "Ladakh"),
)

STATE_NAMES = tuple(name for _code, name in STATES)
GST_CODE = {name: code for code, name in STATES}

ALIASES = {
    "orissa": "Odisha",
    "pondicherry": "Puducherry",
    "uttaranchal": "Uttarakhand",
    "new delhi": "Delhi",
    "nct of delhi": "Delhi",
    "jammu and kashmir": "Jammu & Kashmir",
    "andaman and nicobar islands": "Andaman & Nicobar Islands",
    "dadra and nagar haveli and daman and diu":
        "Dadra & Nagar Haveli and Daman & Diu",
    "chattisgarh": "Chhattisgarh",
    "chhatisgarh": "Chhattisgarh",
}

STATE_CODES = {
    "an": "Andaman & Nicobar Islands", "ap": "Andhra Pradesh", "ar": "Arunachal Pradesh", "as": "Assam",
    "br": "Bihar", "ch": "Chandigarh", "ct": "Chhattisgarh", "cg": "Chhattisgarh",
    "dn": "Dadra & Nagar Haveli and Daman & Diu", "dd": "Dadra & Nagar Haveli and Daman & Diu",
    "dl": "Delhi", "ga": "Goa", "gj": "Gujarat", "hr": "Haryana", "hp": "Himachal Pradesh",
    "jk": "Jammu & Kashmir", "jh": "Jharkhand", "ka": "Karnataka", "kl": "Kerala", "la": "Ladakh",
    "ld": "Lakshadweep", "mp": "Madhya Pradesh", "mh": "Maharashtra", "mn": "Manipur", "ml": "Meghalaya",
    "mz": "Mizoram", "nl": "Nagaland", "or": "Odisha", "od": "Odisha", "py": "Puducherry", "pb": "Punjab",
    "rj": "Rajasthan", "sk": "Sikkim", "tn": "Tamil Nadu", "tg": "Telangana", "ts": "Telangana",
    "tr": "Tripura", "up": "Uttar Pradesh", "ut": "Uttarakhand", "uk": "Uttarakhand", "wb": "West Bengal",
}

_STATE_BY_NAME = {name.casefold(): name for name in STATE_NAMES}
_WHITESPACE = re.compile(r"\s+")
STATE_LOOKUP = dict(_STATE_BY_NAME)
STATE_LOOKUP.update(ALIASES)
STATE_LOOKUP.update(STATE_CODES)


class InvalidState(ValueError):
    """Raised when an India address has no accepted GST state."""


def normalize(value):
    """Return the canonical GST state name for an accepted state value."""
    if value is None:
        return None
    key = _WHITESPACE.sub(" ", str(value).strip()).casefold()
    if not key:
        return None
    if key in _STATE_BY_NAME:
        return _STATE_BY_NAME[key]
    if key in ALIASES:
        return ALIASES[key]
    return STATE_CODES.get(key)


def require(value):
    """Return a canonical GST state name or raise for an invalid value."""
    state = normalize(value)
    if state is None:
        raise InvalidState("Please choose your state from the list.")
    return state


def is_india_address(country, is_india_host):
    """Whether the country identifies India, or is blank on the India host."""
    value = str(country or "").strip().casefold()
    return value in ("india", "in", "bharat") or (not value and bool(is_india_host))
