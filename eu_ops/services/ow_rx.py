"""Optiwar prescription parsing, display formatting, and validation."""

from decimal import Decimal, InvalidOperation


_EYE_KEYS = ("sph", "cyl", "axis", "add")
_QUARTER = Decimal("0.25")


def parse_eye(raw) -> dict | None:
    """Parse SPH/CYL/AXIS/ADD slash notation, filling omitted fields with zero."""
    if raw is None or not str(raw).strip():
        return None
    parts = str(raw).split("/")
    if len(parts) > 4:
        return None
    values = []
    try:
        for part in parts:
            value = float(part.strip()) if part.strip() else 0.0
            if value != value or value in (float("inf"), float("-inf")):
                return None
            values.append(value)
    except (TypeError, ValueError, OverflowError):
        return None
    values.extend([0.0] * (4 - len(values)))
    return dict(zip(_EYE_KEYS, values))


def format_eye(raw) -> dict | None:
    """Format a stored prescription for compact human-readable display."""
    values = parse_eye(raw)
    if values is None:
        return None

    sph = values["sph"]
    cyl = values["cyl"]
    axis = values["axis"]
    add = values["add"]
    axis_text = str(int(axis)) if axis.is_integer() else f"{axis:g}"
    return {
        "sph": "0.00" if sph == 0 else f"{sph:+.2f}",
        "cyl": "—" if cyl == 0 else f"{cyl:+.2f}",
        "axis": "—" if cyl == 0 else f"{axis_text}°",
        "add": "—" if add == 0 else f"{add:+.2f}",
    }


def format_eye_input(raw) -> dict | None:
    """Format values for Rx edit fields without hiding zero powers."""
    values = parse_eye(raw)
    if values is None:
        return None

    def signed(value):
        return "0.00" if value == 0 else f"{value:+.2f}"

    axis = values["axis"]
    return {
        "sph": signed(values["sph"]),
        "cyl": signed(values["cyl"]),
        "axis": (str(int(axis)) if axis.is_integer() else f"{axis:g}")
        if values["cyl"] != 0 else "",
        "add": signed(values["add"]),
    }


def needs_add(recommendations) -> bool:
    text = str(recommendations or "").casefold()
    return any(term in text for term in ("progressive", "bifocal", "biofocal"))


def _decimal(value):
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _is_quarter_step(value):
    return value % _QUARTER == 0


def _stored_float(value):
    number = float(value)
    if number == 0:
        number = 0.0
    return str(number)


def validate_eye(sph, cyl, axis, add, require_add) -> tuple[str, list]:
    """Validate form values and return normalized Optiwar slash notation."""
    errors = []

    sph_text = "" if sph is None else str(sph).strip()
    sph_value = _decimal(sph_text) if sph_text else None
    if not sph_text:
        errors.append("SPH is required")
    elif sph_value is None:
        errors.append("SPH must be a number")
    else:
        if sph_value < Decimal("-20.00") or sph_value > Decimal("20.00"):
            errors.append("SPH must be between -20.00 and +20.00")
        if not _is_quarter_step(sph_value):
            errors.append("SPH must be in 0.25 steps")

    cyl_text = "" if cyl is None else str(cyl).strip()
    cyl_value = Decimal(0) if not cyl_text else _decimal(cyl_text)
    if cyl_value is None:
        errors.append("CYL must be a number")
    else:
        if cyl_value < Decimal("-6.00") or cyl_value > Decimal("6.00"):
            errors.append("CYL must be between -6.00 and +6.00")
        if not _is_quarter_step(cyl_value):
            errors.append("CYL must be in 0.25 steps")

    axis_value = Decimal(0)
    if cyl_value is not None and cyl_value != 0:
        axis_text = "" if axis is None else str(axis).strip()
        parsed_axis = _decimal(axis_text) if axis_text else None
        if not axis_text:
            errors.append("AXIS 1–180 is required when CYL is not 0")
        elif parsed_axis is None:
            errors.append("AXIS must be an integer from 1 to 180")
        elif parsed_axis != parsed_axis.to_integral_value() or not (
                Decimal(1) <= parsed_axis <= Decimal(180)):
            errors.append("AXIS must be an integer from 1 to 180")
        else:
            axis_value = parsed_axis

    add_text = "" if add is None else str(add).strip()
    add_value = Decimal(0) if not add_text else _decimal(add_text)
    if add_value is None:
        errors.append("ADD must be a number")
    else:
        if add_value < Decimal("0.00") or add_value > Decimal("4.00"):
            errors.append("ADD must be between 0.00 and +4.00")
        if not _is_quarter_step(add_value):
            errors.append("ADD must be in 0.25 steps")
        if require_add and add_value <= 0:
            errors.append("ADD must be greater than 0.00 for progressive/bifocal lenses")

    if errors:
        return "", errors

    return "/".join((
        _stored_float(sph_value),
        _stored_float(cyl_value),
        _stored_float(axis_value),
        _stored_float(add_value),
    )), []
