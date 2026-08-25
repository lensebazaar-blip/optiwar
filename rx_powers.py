"""Reading eye powers the way they were stored, not the way they should be.

Checkout writes a power as four slash-separated components, ``SPH/CYL/AXIS/ADD``,
and every reader splits on ``/`` and gates the whole prescription block on a
slash being present. A power entered as sphere only — ``-2.00`` — therefore read
as *no prescription at all*: the confirmation page and the order history showed
"Complimentary Anti-Glare Plano Lens" for an order that has a prescription.

Rewriting historical ``rx_collector`` rows to satisfy a parser is the wrong way
round, so readers tolerate the short form instead: a missing component is zero —
no cylinder, no axis, no addition — which is what a sphere-only power means.
"""

# A stored power that is not a prescription. Checkout's own non-Rx marker, the
# string a NULL becomes on its way through the templates, and a bare zero: a
# lone "0" is a placeholder someone left behind, never a power anyone wrote.
NOT_A_POWER = ("", "0", "none", "null", "no rx selected", "no rx", "-")

MISSING = "0"
COMPONENTS = 4


def normalize_power(value) -> str:
    """``SPH/CYL/AXIS/ADD`` for a stored power, or ``""`` if there is none."""
    text = str(value or "").strip()
    if text.lower() in NOT_A_POWER:
        return ""
    parts = [p.strip() or MISSING for p in text.split("/")]
    return "/".join((parts + [MISSING] * COMPONENTS)[:COMPONENTS])


def normalize_rows(rows):
    """Normalize ``right_eye``/``left_eye`` in place for rows about to be shown.

    Done once where the rows are read rather than in each template, so a page
    that renders a prescription cannot forget to be tolerant.
    """
    for row in rows or ():
        for key in ("right_eye", "left_eye"):
            if key in row:
                row[key] = normalize_power(row[key])
    return rows
