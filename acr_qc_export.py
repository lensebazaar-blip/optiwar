"""ACR Gate-1 B — audited PDF export of the QC conversation review.

An operational review that only exists on a screen cannot be taken into a
meeting, filed, or shown to someone who does not have console access. This turns
a QC window into a page, under three constraints that are the whole point of the
module.

**Every export is written to the canonical stream before it is produced, and an
export whose audit row did not land is refused.** The Ops Console logs access
best-effort — a console that stops rendering because the audit insert failed
would be worse than one whose audit has a gap. A *document* is the opposite: it
leaves the system, it can be forwarded, and its only remaining tie to the
platform is the ledger entry saying who took it. So :func:`export` fails closed
(``AuditWriteFailed``) rather than emitting an unrecorded document.

**The paper carries its own ledger key.** ``document_id`` is a digest of the
content model — the reviews, the window and the actor — printed in the footer of
every page and stored in the ``QC_EXPORT`` payload. A page found on a desk can
be traced back to the export that produced it. It deliberately does *not* digest
the PDF bytes: those embed the generation timestamp, so re-exporting the same
window would produce a different id for identical findings.

**It cannot leak conversation text, by construction.** The exporter consumes
:func:`acr_qc.review_window` output only. It never reads ``chat_messages`` and
has no access to a transcript, so the PII boundary is inherited from the QC layer
rather than re-implemented here.

There is no PDF dependency: the writer below emits a text-only document from the
standard library. Adding ``reportlab`` to a production venv for one report is a
larger and less reversible change than ~120 lines of PDF, and this keeps the node
rebuildable from Git alone.

    pdf, meta = acr_qc_export.export(db, hours=24, actor="admin@…", ip="…")
"""
import hashlib
import json
from datetime import datetime

try:  # package import inside the app, flat import in tests/tools
    from . import acr
    from . import acr_qc
except ImportError:  # pragma: no cover - exercised by the flat-import path
    import acr
    import acr_qc


class AuditWriteFailed(Exception):
    """The export was refused because its audit row could not be stored."""


# ─── Content model ───

def content_model(reviews, hours, actor, generated_at):
    """The document's data, as a plain dict — the thing that gets digested.

    Ordered deterministically so the same findings always yield the same
    ``document_id``: reviews sorted by session, signals by name.
    """
    summary = acr_qc.summarize(reviews)
    rows = []
    for r in sorted(reviews, key=lambda r: r.get("session_id") or ""):
        signals = sorted(r.get("signals", ()), key=lambda s: s["signal"])
        rows.append(dict(
            session_id=r.get("session_id") or "",
            severity=acr_qc.worst_severity(r),
            reviewable=bool(r.get("reviewable")),
            outcome=r.get("outcome") or "",
            signals=[dict(signal=s["signal"], severity=s["severity"],
                          detail=_detail(s)) for s in signals],
        ))
    return dict(window_hours=int(hours), actor=actor or "unknown",
                generated_at=_iso(generated_at), summary=summary, sessions=rows)


def _detail(signal):
    """The signal's counters, minus the two keys every signal carries."""
    return {k: v for k, v in sorted(signal.items())
            if k not in ("signal", "severity")}


def _iso(when):
    if isinstance(when, datetime):
        return when.replace(microsecond=0).isoformat() + "Z"
    return str(when)


def document_id(model):
    """A stable id for these findings: sha256 of the canonical model, 16 hex.

    ``generated_at`` is excluded, so re-exporting an unchanged window yields the
    same id: the digest names *the findings*, not the moment of printing. Two
    copies of one review on two desks must be recognisable as one review, and
    the audit stream already records when each was taken.
    """
    findings = {k: v for k, v in model.items() if k != "generated_at"}
    canonical = json.dumps(findings, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ─── Audited entry point ───

def export(db, hours=24, actor="unknown", ip=None, limit=200, now=None):
    """Review the window, record the export, and return ``(pdf_bytes, meta)``.

    Raises :class:`AuditWriteFailed` — and produces no document — when the
    ``QC_EXPORT`` event could not be written. ``acr.log_event`` returns the
    ``event_id`` it stored or ``None``, which is what makes that check possible
    without the event path having to raise.
    """
    reviews = acr_qc.review_window(db, hours=hours, limit=limit)
    model = content_model(reviews, hours, actor, now or datetime.utcnow())
    doc_id = document_id(model)
    summary = model["summary"]
    event_id = acr.log_event(
        db, acr.EV_QC_EXPORT,
        payload={"actor": actor, "ip": ip, "document_id": doc_id,
                 "scope": {"hours": int(hours), "limit": int(limit)},
                 "sessions": summary["sessions"],
                 "by_severity": summary["by_severity"], "format": "pdf"})
    if not event_id:
        raise AuditWriteFailed(
            "QC_EXPORT could not be recorded; export refused")
    pdf = render_pdf(model, doc_id)
    return pdf, dict(document_id=doc_id, event_id=event_id, summary=summary,
                     generated_at=model["generated_at"])


# ─── Layout ───

PAGE_W, PAGE_H = 595, 842           # A4 in points
MARGIN = 48
LINE = 13
BODY_TOP = PAGE_H - MARGIN - 58     # below the title block
BODY_BOTTOM = MARGIN + 24           # above the footer


def _lines(model, doc_id):
    """The document as (text, size, indent) lines, before pagination."""
    s = model["summary"]
    out = [
        ("AI conversation QC review", 15, 0),
        ("Window: last %d hours   Generated: %s   Exported by: %s"
         % (model["window_hours"], model["generated_at"], model["actor"]), 8, 0),
        ("", 9, 0),
        ("Summary", 11, 0),
        ("Sessions reviewed: %d   reviewable: %d   unreviewable: %d"
         % (s["sessions"], s["reviewable"], s["unreviewable"]), 9, 0),
        ("Worst severity per session — FAIL: %d   WARN: %d   INFO: %d"
         % (s["by_severity"].get(acr_qc.FAIL, 0), s["by_severity"].get(acr_qc.WARN, 0),
            s["by_severity"].get(acr_qc.INFO, 0)), 9, 0),
        ("", 9, 0),
    ]
    for name in sorted(s["by_signal"]):
        out.append(("%-32s %4d   [%s]"
                    % (name, s["by_signal"][name], acr_qc.SIGNALS.get(name, "")),
                    9, 12))
    out += [("", 9, 0), ("Sessions", 11, 0)]
    if not model["sessions"]:
        out.append(("No sessions with canonical activity in this window.", 9, 12))
    for row in model["sessions"]:
        out.append(("[%s] %s%s" % (row["severity"], row["session_id"],
                                   ("   outcome: " + row["outcome"])
                                   if row["outcome"] else ""), 9, 0))
        for sig in row["signals"]:
            detail = ", ".join("%s=%s" % (k, v) for k, v in sig["detail"].items())
            out.append(("%s%s" % (sig["signal"], ("   " + detail) if detail else ""),
                        8, 18))
    out += [
        ("", 9, 0),
        ("This document reports signals and counts only. It contains no "
         "conversation text, customer name, email, phone or prescription.", 7, 0),
        ("Document %s is recorded in the canonical event stream as QC_EXPORT."
         % doc_id, 7, 0),
    ]
    return out


def paginate(lines):
    """Split lines into pages that fit between the header and the footer."""
    pages, page, y = [], [], BODY_TOP
    for text, size, indent in lines:
        if y < BODY_BOTTOM:
            pages.append(page)
            page, y = [], BODY_TOP
        page.append((text, size, indent, y))
        y -= LINE
    pages.append(page)
    return pages


# ─── Minimal PDF writer ───
#
# Text-only, one built-in font, uncompressed streams. Uncompressed is a feature
# here: `strings report.pdf` shows exactly what the document says, so "contains
# no conversation text" is checkable by anyone holding the file, and the test
# that asserts it is a byte scan rather than a mock.

# WinAnsi has no code point for these, and a report that prints "?" where it
# meant a dash reads like a mojibake bug in the exporter.
_ASCII_FOLD = {"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
               "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00a0": " "}


def _esc(text):
    """PDF string escaping, ASCII only (the base font is not Unicode)."""
    out = []
    for ch in str(text):
        for c in _ASCII_FOLD.get(ch, ch):
            if c in "()\\":
                out.append("\\" + c)
            elif 32 <= ord(c) < 127:
                out.append(c)
            else:
                out.append("?")
    return "".join(out)


def _content_stream(page_lines, doc_id, page_no, page_count):
    parts = []
    for text, size, indent, y in page_lines:
        if not text:
            continue
        parts.append("BT /F1 %d Tf %d %d Td (%s) Tj ET"
                     % (size, MARGIN + indent, y, _esc(text)))
    parts.append("BT /F1 7 Tf %d %d Td (%s) Tj ET"
                 % (MARGIN, MARGIN,
                    _esc("Document %s   page %d of %d   Optiwar ACR — "
                         "authorised operational review"
                         % (doc_id, page_no, page_count))))
    return "\n".join(parts).encode("latin-1", "replace")


def render_pdf(model, doc_id=None):
    """The content model as PDF bytes. Deterministic for a fixed model."""
    doc_id = doc_id or document_id(model)
    pages = paginate(_lines(model, doc_id))
    count = len(pages)

    # Object 1 catalog, 2 pages, 3 font, then per page: page object + stream.
    objects = []
    page_ids = [4 + 2 * i for i in range(count)]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(("<< /Type /Pages /Count %d /Kids [%s] >>"
                    % (count, " ".join("%d 0 R" % i for i in page_ids))
                    ).encode("latin-1"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                   b"/Encoding /WinAnsiEncoding >>")
    for i, page_lines in enumerate(pages):
        stream = _content_stream(page_lines, doc_id, i + 1, count)
        objects.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
                        "/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
                        % (PAGE_W, PAGE_H, page_ids[i] + 1)).encode("latin-1"))
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") +
                       b" >>\nstream\n" + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += ("%d 0 obj\n" % n).encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    out += ("xref\n0 %d\n" % (len(objects) + 1)).encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode("ascii")
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_at)).encode("ascii")
    return bytes(out)
