"""Resolve an India invoice's GST place of supply from PIN, state, or city."""
import csv
from functools import lru_cache
from pathlib import Path
import re

from services import india_states


_PINCODES_PATH = Path(__file__).resolve().parents[1] / "data" / "gst_pincodes.csv"
_PINCODES = None


def _load_pincodes():
    global _PINCODES
    if _PINCODES is None:
        by_pin = {}
        districts = set()
        with _PINCODES_PATH.open(encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                row["other_states"] = tuple(
                    state.strip() for state in (row.get("other_states") or "").split(";")
                    if state.strip())
                by_pin[row["pincode"]] = row
                district = (row.get("district") or "").strip()
                state = (row.get("state") or "").strip()
                if district and state:
                    districts.add((district, state))
        _PINCODES = (by_pin, tuple(sorted(districts)))
    return _PINCODES


@lru_cache(maxsize=None)
def _district_pattern(district):
    return re.compile(r"(?<!\w)" + re.escape(district) + r"(?!\w)", re.IGNORECASE)


def _result(state, source, note=""):
    return {
        "state": state,
        "code": india_states.GST_CODE[state],
        "source": source,
        "note": note,
    }


def resolve(pin, state, address_text) -> dict | None:
    """Resolve an invoice place of supply, preferring validated PIN evidence."""
    digits = re.sub(r"\D", "", str(pin or ""))
    by_pin, districts = _load_pincodes()
    customer_state = india_states.normalize(state)

    if len(digits) == 6 and digits in by_pin:
        row = by_pin[digits]
        pin_state = row["state"]
        candidates = (pin_state,) + row["other_states"]
        if row["other_states"]:
            if customer_state in candidates:
                return _result(customer_state, "pin+state")
            return None

        note = ""
        raw_state = str(state or "")
        if customer_state and customer_state != pin_state:
            note = f"Customer chose {customer_state}; pin {digits} is in {pin_state}"
        elif not customer_state and raw_state.strip():
            note = f"Customer entered '{raw_state}'; pin {digits} is in {pin_state}"
        return _result(pin_state, "pin", note)

    if customer_state:
        return _result(customer_state, "state")

    address = str(address_text or "")
    matched_states = {
        district_state
        for district, district_state in districts
        if _district_pattern(district).search(address)
    }
    if len(matched_states) == 1:
        return _result(next(iter(matched_states)), "city")
    return None
