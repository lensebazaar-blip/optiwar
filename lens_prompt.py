"""What the model is allowed to say about contact lenses, built from catalogue rows.

The rule this replaces was a sentence in the system prompt — "We DO NOT sell
contact lenses on optiwar.com" — and it was recited to .com customers as fact.
A prompt that states current availability in prose is a claim nobody updates: it
was right when written and wrong the day the catalogue changed, and the model has
no way to know which.

So availability is not written here either. Every sentence about a specific lens
is generated from rows the caller fetched through the release gate in
``catalogue.py``, and this module never queries anything: it takes the rows and
the matrix summary and turns them into prompt text. The three constants below say
what to do when there is nothing to offer, which is a *policy* (say none is
available, name nothing) rather than a statement about the catalogue.

Deliberately dependency-free so it is testable without Flask or a database — the
behaviour being protected is the text, and the text is the part that reaches a
customer.
"""

SECTION_IN = """
CONTACT LENSES ON THIS STORE:
  This store does not currently sell contact lenses. If a customer asks, say
  contact lenses are not currently available on this store, and offer frames or
  a support ticket instead.
  Do NOT name, describe, price or recommend any contact lens product, and do NOT
  point the customer at another Optiwar store unless they explicitly ask about
  our global/international store — only then may you say optiwar.com carries
  contact lenses.
"""

SECTION_NONE = """
CONTACT LENSES ON THIS STORE:
  No contact lens is released for sale on this store right now, so there is
  nothing you can offer. Say that none is currently available, and never name,
  price or describe a specific lens. Do not promise a date.
"""

RULES = """  These are the ONLY contact lenses you may discuss, and the facts above are the
  only facts about them. Never state a power, cylinder, axis, ADD, colour, base
  curve, diameter, pack size or price that is not listed here, and never say a
  prescription is or is not available from your own knowledge — the supported
  range shown is a range, not a guarantee that every combination inside it
  exists. If a customer asks for a specific prescription, tell them the product
  page confirms the exact combination when they select it, and offer to take them
  there. Do not offer a nearest or corrected power.
"""


def _span(lo, hi, fmt="%+.2f"):
    if lo is None and hi is None:
        return None
    if lo is None or hi is None or float(lo) == float(hi):
        return fmt % float(lo if lo is not None else hi)
    return "%s to %s" % (fmt % float(lo), fmt % float(hi))


def matrix_range(summary):
    """The supported range of one lens, as its variant matrix actually holds it.

    A range and nothing more: manufacturers leave holes in a range and steps are
    not uniform, so this describes a lens and never decides an order.
    """
    summary = summary or {}
    parts = []
    for label, lo, hi, fmt in (
            ("SPH", "sph_min", "sph_max", "%+.2f"),
            ("CYL", "cyl_min", "cyl_max", "%+.2f"),
            ("AXIS", "axis_min", "axis_max", "%d"),
            ("ADD", "add_min", "add_max", "%+.2f"),
            ("BC", "bc_min", "bc_max", "%.1f"),
            ("DIA", "dia_min", "dia_max", "%.1f")):
        text = _span(summary.get(lo), summary.get(hi), fmt)
        if text:
            parts.append("%s %s" % (label, text))
    colors = [name or code for code, name in (summary.get("colors") or ())]
    if colors:
        parts.append("colours: %s" % ", ".join(str(c) for c in colors[:14]))
    return "; ".join(parts)


def lens_line(row, summary=None):
    """One lens as canonical facts: brand, modality, pack, price, matrix range."""
    price = row.get("product_special_price_eur") or row.get("product_price_eur")
    facts = [" ".join(p for p in (str(row.get("brand") or "").strip(),
                                  str(row.get("product_name") or "").strip())
                      if p)]
    kind = " ".join(p for p in (
        str(row.get("modality") or "").lower(),
        str(row.get("lens_type") or "").lower().replace("_", " ")) if p)
    if kind:
        facts.append(kind)
    if row.get("pack_quantity"):
        facts.append("pack of %s" % row["pack_quantity"])
    if row.get("replacement_days"):
        facts.append("replace every %s days" % row["replacement_days"])
    if row.get("material"):
        facts.append(str(row["material"]))
    if price is not None:
        facts.append("\u20ac%.2f per pack" % float(price))
    if row.get("availability"):
        facts.append("availability %s" % row["availability"])
    rng = matrix_range(summary)
    if rng:
        facts.append(rng)
    return "  - %s" % " | ".join(f for f in facts if f)


def contact_lens_section(lenses, is_india=False):
    """The CONTACT LENSES block of the system prompt for this storefront.

    ``lenses`` is a sequence of ``(row, matrix_summary)`` already filtered by the
    release gate. Empty means nothing may be offered — the same answer as an
    unreadable catalogue, because a catalogue we cannot read is not a claim we
    can make.
    """
    if is_india:
        return SECTION_IN
    lines = [lens_line(row, summary) for row, summary in (lenses or ())]
    if not lines:
        return SECTION_NONE
    return ("\nCONTACT LENSES WE SELL ON THIS STORE (%d):\n%s\n%s"
            % (len(lines), "\n".join(lines), RULES))


_LENS_WORDS = ("contact lens", "contact lenses")


def is_lens_availability_faq(item):
    """Whether a knowledge-base FAQ answers "do you sell contact lenses".

    Such an answer is the same defect in another file: hand-written prose about
    what a vertical currently offers, fed to the model as fact. The catalogue
    section answers that question, so these are dropped from the prompt.
    """
    if "contact_lens" in (item.get("intent") or "").lower():
        return True
    text = " ".join(str(item.get(k) or "").lower()
                    for k in ("question", "answer"))
    return any(w in text for w in _LENS_WORDS)
