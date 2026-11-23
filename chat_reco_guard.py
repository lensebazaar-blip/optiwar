"""Frames the chat lists must be frames we sell.

The model is told to recommend only from the catalogue. On 23 Nov 2026 it
listed "JOHN JACOBS JF1563" and "VINCENT CHASE" at Rs 699 to a customer: two
other companies' brands, on frames that do not exist. An instruction is not a
control, so this is the check behind it, applied to every reply before the
customer sees it:

* a list line that offers a frame (it carries a frame size, or a price in a
  turn that searched the catalogue and is not about lenses) must name a
  catalogue code, or it is removed;
* a line whose label names another eyewear brand is removed whatever else it
  says;
* a real code under a wrong name gets the catalogue's own name.

Only list lines are judged. Prose is left alone: "I can see BOSS on your
photo" describes the customer's own frame and offers nothing.
"""
import re

# Brands we do not sell. Whole words, lower case. "ray-ban meta" is allowed
# only while the AI Glasses line is on (see ai_glasses_on below).
THIRD_PARTY_BRANDS = (
    "john jacobs", "vincent chase", "lenskart", "titan", "titan eye", "fastrack",
    "idee", "coolwinks", "specsmakers", "eyemyeye", "gkb", "lawrence & mayo",
    "ray-ban", "ray ban", "rayban", "oakley", "persol", "vogue", "carrera",
    "police", "gucci", "prada", "armani", "hugo boss", "boss", "tommy hilfiger",
    "tommy", "ralph lauren", "polo", "calvin klein", "lacoste", "burberry",
    "versace", "dior", "chanel", "cartier", "tom ford", "montblanc", "mont blanc",
    "bvlgari", "fendi", "dolce", "louis vuitton", "michael kors", "fossil",
    "guess", "diesel", "puma", "nike", "adidas", "reebok", "maui jim",
    "silhouette", "lindberg", "rodenstock", "warby parker", "zenni",
    "specsavers", "porsche", "ferrari", "mercedes", "north face", "benetton",
)

NOTHING_FOUND = "I couldn't find frames in our catalogue that match that exactly."

_ITEM = re.compile(r"^(?P<lead>\s*(?:(?P<num>\d+)[.)]|[-*•])\s+)"
                   r"(?P<label>.+?)\s+[—–-]\s+(?P<rest>.+)$")
_SIZE = re.compile(r"\b\d{2}\s*-\s*\d{2}\s*-\s*\d{3}\b")
_PRICE = re.compile(r"₹|€|\bRs\.?\s?\d|\bINR\b|\bEUR\b")
# A priced list line about lenses is not a frame offer.
_LENS = re.compile(r"lens|coating|blue ?cut|anti|glare|progressive|bifocal|single vision|"
                   r"photo|polari|thin|index|upgrade|power|delivery|shipping", re.I)
_BRANDS = re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])"
                     % "|".join(re.escape(b) for b in
                                sorted(THIRD_PARTY_BRANDS, key=len, reverse=True)))


def _plain(text):
    return re.sub(r"[^a-z0-9+]+", " ", (text or "").lower()).strip()


def _label_text(label):
    return re.sub(r"[*_`]+", "", label or "").strip()


def catalogue_index(products):
    """{CODE: name} for the frames the chat may offer."""
    return {str(p.get("code") or "").strip().upper(): (p.get("name") or "").strip()
            for p in products or [] if str(p.get("code") or "").strip()}


def check(reply, products, searched=False, ai_glasses_on=False):
    """Return ``(reply, removed)``: the reply with every unsupported frame line
    taken out, and the labels that were removed (empty when nothing was)."""
    if not reply:
        return reply, []
    index = catalogue_index(products)
    lines = reply.split("\n")
    out, removed, kept_frames, first_removed_at, renamed = [], [], 0, None, False

    for line in lines:
        m = _ITEM.match(line)
        if not m:
            out.append(line)
            continue
        label = _label_text(m.group("label"))
        low = label.lower()
        branded = bool(_BRANDS.search(low))
        if branded and ai_glasses_on and low.startswith(("ray-ban meta", "ray ban meta")):
            out.append(line)
            continue
        offers_frame = bool(_SIZE.search(line)) or (
            searched and bool(_PRICE.search(m.group("rest")))
            and not _LENS.search(label))
        if not offers_frame and not branded:
            out.append(line)
            continue
        code = next((t for t in reversed(re.findall(r"[A-Za-z0-9]+", label))
                     if t.upper() in index), None)
        if branded or code is None:
            removed.append(label)
            if first_removed_at is None:
                first_removed_at = len(out)
            continue
        real_name = index[code.upper()]
        if real_name and _plain(label) != _plain("%s %s" % (real_name, code)):
            # a real frame under a name the catalogue does not give it
            line = "%s%s %s%s" % (m.group("lead"), real_name, code.upper(),
                                  line[m.end("label"):])
            renamed = True
        kept_frames += 1
        out.append(line)

    if not removed:
        return ("\n".join(out) if renamed else reply), []
    if kept_frames == 0:
        out.insert(first_removed_at, NOTHING_FOUND)
    return "\n".join(_renumber(out)), removed


def _renumber(lines):
    """Number each run of numbered list lines 1, 2, 3 again after removals."""
    out, n = [], 0
    for line in lines:
        m = re.match(r"^(\s*)(\d+)([.)]\s+)", line)
        if m:
            n += 1
            line = "%s%d%s%s" % (m.group(1), n, m.group(3), line[m.end():])
        elif line.strip():
            n = 0
        out.append(line)
    return out
