"""The terms a customer accepts at checkout, as versions that cannot drift.

A policy is text the customer was shown; "the customer accepted the terms" is
only provable if the exact text can be produced later. So every policy is a
string in this module, its version is the sha256 of that string, and the text
is sealed into ``policy_versions`` the first time the version is seen. An order
records which versions it was placed under in ``order_terms_acceptance``; a
later edit to the text is a new version, and old orders keep pointing at the
old one.

The returns policy differs by storefront: optiwar.in carries the discretionary
return and the customized-lens deduction, optiwar.com states that international
orders are absolutely non-returnable and non-refundable. Neither is a default
for the other.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

TERMS_URL = "/terms_and_conditions"
RETURNS_URL = "/terms_and_conditions#returns-policy"

LENS_DEDUCTION_CAP_PERCENT = 50

SITE_IN = "in"
SITE_COM = "com"

# ---------------------------------------------------------------------------
# Terms & Conditions (shared)

TERMS_VERSION_DATE = "2026-09-15"

# ---------------------------------------------------------------------------
# Returns, Replacements & Limited Warranty Policy — optiwar.in

RETURNS_IN = """\
OPTIWAR RETURNS, REPLACEMENTS & LIMITED WARRANTY POLICY

Effective for Retail and Wholesale Orders placed on optiwar.in

1. IMPORTANT — YOUR ORDER IS PLACED SUBJECT TO THESE TERMS

Optiwar operates as a modern electronic wholesale optical factory outlet. Our prices are based on centralized online ordering, factory/wholesale-style supply, automated processing and limited after-sale fitting services.

Optiwar is not a neighbourhood optical shop, boutique eyewear store or physical fitting centre.

Before placing and paying for an order, every customer is required to read and expressly accept this Returns, Replacements & Limited Warranty Policy and Optiwar's applicable Terms & Conditions.

By affirmatively accepting these terms and placing the order, the customer confirms that:

- the product details, measurements, prescription information, lens selections, colours, quantities and prices have been reviewed before purchase;
- spectacles may require normal post-delivery adjustment by a local optician;
- Optiwar does not provide an unconditional trial, satisfaction guarantee or change-of-mind return facility;
- customized and prescription products may have substantial non-recoverable value once manufactured;
- approved returns are subject to inspection;
- applicable return/reverse-pickup charges may be payable by the customer;
- where a discretionary return of prescription spectacles is accepted, the non-recoverable value of customized prescription lenses may be deducted from the refund as described below; and
- the customer accepts these conditions as part of the purchase contract.

Nothing in these terms excludes any right or remedy that cannot lawfully be excluded or limited.

2. NO GENERAL CHANGE-OF-MIND RETURN

Optiwar does not operate a "buy, try and return" business model.

A correctly supplied product is not automatically returnable or replaceable merely because the customer subsequently:

- changes their mind;
- dislikes the style, colour, size, appearance or feel;
- believes another frame would look better;
- finds the frame loose, tight, high, low, tilted or in need of normal adjustment;
- experiences an issue normally capable of correction through routine optical fitting;
- experiences subjective discomfort or adaptation;
- expected a higher degree of cosmetic finishing or boutique inspection than is reasonably inherent in the product supplied; or
- decides after delivery that the product is unsuitable for personal preference reasons.

Optiwar may nevertheless voluntarily authorize a return in writing. Such authorization remains subject to return charges, inspection and the refund calculation stated in this policy.

3. SPECTACLE FITTING, ALIGNMENT AND BALANCE

Spectacles are wearable optical products and may require adjustment to the individual customer's face after delivery.

This may include:

- temple adjustment;
- nose-pad adjustment;
- alignment;
- tilt;
- tightening;
- balancing;
- adjustment behind the ears; and
- other ordinary fitting work.

These matters do not, by themselves, establish a manufacturing defect.

A customer may obtain such adjustment from a competent local optical store at the customer's own cost.

Optiwar does not ordinarily reimburse third-party fitting or adjustment charges unless Optiwar expressly agrees to do so in writing before the expense is incurred.

Customers requiring repeated in-person fittings, boutique-level finishing, individual physical measurements or exceptionally stringent cosmetic inspection should use a physical optical service providing those facilities.

4. PRESCRIPTION LENSES — IMPORTANT CUSTOMIZED-PRODUCT CONDITION

Prescription lenses are manufactured, processed, surfaced, coated, edged and/or fitted according to the prescription and product selections associated with a particular order.

Once prescription lenses have been manufactured or fitted into a frame, they may have little or no reasonable resale value to Optiwar.

Accordingly, acceptance of a spectacle return does not automatically mean that the entire price paid for the spectacles will be refunded.

Where Optiwar accepts a discretionary return of spectacles containing customized prescription lenses, Optiwar may deduct the non-recoverable value attributable to the customized prescription lenses and associated processing.

The deduction will be determined from the order configuration and inspection and may be up to 50% of the relevant returned spectacle/product value, but will not exceed the applicable disclosed cap.

The customer will be informed of the refund calculation following inspection.

For clarity:

- Amount Paid
- − applicable customized prescription-lens deduction
- − applicable return/reverse-logistics charges
- − other charges expressly disclosed as non-refundable where applicable
- = Refund Payable

This deduction is intended for a discretionary return of correctly supplied customized goods. It is not intended to deprive a customer of a remedy where Optiwar itself supplied an incorrect product, incorrect prescription or product having a genuine covered manufacturing defect.

5. PRESCRIPTION ACCURACY, ADAPTATION AND VISUAL COMFORT

Subjective visual discomfort does not automatically establish that prescription lenses were manufactured incorrectly.

Where a prescription complaint is raised, Optiwar may verify:

Prescription supplied by customer → Prescription confirmed in order → Lens specification ordered → Lens actually supplied

If the spectacles correspond to the prescription and specifications accepted by the customer, adaptation, wearing preference or subjective visual comfort does not automatically create a right to replacement.

Where Optiwar establishes that it supplied an incorrect prescription or specification, the matter will instead be handled as an incorrect-supply claim.

6. COSMETIC CONDITIONS, MINOR MARKS AND THIRD-PARTY FITTING DAMAGE

Minor cosmetic variation that does not materially impair the normal use of the product does not automatically constitute a manufacturing defect.

In particular, Optiwar is not responsible for scratches, chips, marks, tool marks, deformation or damage caused after supply, including during:

- lens fitting or removal by another optical store;
- frame adjustment;
- heating or bending;
- tightening or repair;
- accidental impact;
- improper handling;
- improper cleaning;
- chemical exposure;
- modification; or
- other third-party work.

A scratch or chip produced while another optician is fitting lenses into a frame is not converted into an Optiwar manufacturing defect merely because it appears on an Optiwar-supplied frame.

A genuine manufacturing defect that existed in the product when supplied will be assessed separately.

7. DEFECTIVE OR INCORRECTLY SUPPLIED PRODUCTS

Where the customer claims that Optiwar supplied the wrong product, wrong specification, wrong prescription or a product with a genuine manufacturing defect, Optiwar will investigate the claim.

Optiwar may request reasonable supporting material, including:

- photographs or video;
- order number;
- product packaging;
- prescription;
- measurements;
- product labels;
- photographs of the alleged defect; or
- return of the product for physical inspection.

After verification, Optiwar will determine the appropriate repair, replacement, refund or other remedy according to the circumstances, applicable warranty and applicable law.

8. CUSTOMER MAY REQUEST A RETURN — BUT RETURN AUTHORIZATION IS REQUIRED

A customer wishing to return a product should first submit a return request to Optiwar.

Optiwar may respond:

- RETURN APPROVED
- RETURN APPROVED SUBJECT TO INSPECTION
- REPLACEMENT APPROVED
- FURTHER INFORMATION REQUIRED
- RETURN NOT APPROVED

Where Optiwar permits the goods to be returned for inspection, this does not constitute an advance promise of a full refund.

The final outcome and refund amount may depend upon physical inspection.

Customers should not send goods to an Optiwar location until return instructions have been issued.

9. RETURN / REVERSE-PICKUP COST

Unless Optiwar expressly confirms otherwise in writing, the customer is responsible for the cost of returning goods under an approved discretionary return or replacement request.

The customer may be instructed either:

- A. Customer Return: Send the product to the designated Optiwar return location using an appropriate courier at the customer's cost; or
- B. Optiwar Reverse Pickup: Request Optiwar to arrange reverse pickup, in which case the applicable reverse-pickup/logistics charge will be collected from or deducted from the amount payable to the customer.

Where applicable, the relevant GST invoice/document for reverse logistics will be provided.

The reverse-pickup price may change from time to time according to courier, location, package weight and other logistics factors.

10. INSPECTION DETERMINES THE RETURN OUTCOME

All returned goods may be physically inspected before the final refund or replacement decision.

Inspection may determine:

- whether the returned item is the item supplied by Optiwar;
- whether it has been used;
- whether prescription lenses have already been manufactured/fitted;
- whether the frame or lenses have been modified;
- whether another optical store has worked on the product;
- whether scratches/chips/damage occurred after delivery;
- whether components are missing;
- whether the claimed defect is present;
- whether the product corresponds to the order;
- whether the product remains commercially reusable; and
- what portion, if any, of the product has become non-recoverable because of customization.

The refund is determined from the actual circumstances found on inspection, rather than automatically from the customer's characterization of the complaint or Optiwar's initial authorization to return the goods.

11. REFUND CALCULATION FOR AN APPROVED DISCRETIONARY RETURN

Where a return of a correctly supplied product is accepted by Optiwar as a commercial accommodation, the refund may be calculated as follows:

Non-prescription product:

- Eligible product value
- − applicable return/reverse-pickup charges
- − any specifically disclosed non-refundable charges
- = Refund

Spectacles containing customized prescription lenses:

- Eligible returned product value
- − customized prescription-lens/processing value (up to 50% of the relevant returned spectacle value)
- − applicable return/reverse-pickup charges
- − any specifically disclosed non-refundable charges
- = Refund

Optiwar will not apply an arbitrary percentage merely because a return has occurred. The applicable deduction should correspond to the returned product, its customization and the conditions disclosed when the order was placed, subject to the stated maximum.

Where Optiwar is responsible for an incorrect supply or a covered manufacturing defect, the appropriate remedy will be determined separately rather than automatically applying this discretionary-return formula.

12. USED, ALTERED OR CUSTOMER-DAMAGED PRODUCTS

Optiwar may reject a discretionary refund where inspection shows that a product has been materially used, damaged, modified, improperly handled, altered, repaired or worked upon by a third party.

If a returned product is rejected following inspection, Optiwar may require the customer to pay the cost of sending the product back to them.

The customer should provide collection/redelivery instructions within the period communicated by Optiwar.

Unclaimed returned goods will be dealt with according to the applicable terms and law.

13. CONTACT LENSES AND SEALED/HYGIENE-SENSITIVE PRODUCTS

Opened, worn, used, unsealed or tampered contact-lens packs and other hygiene-sensitive products are not accepted for discretionary return.

Customers must carefully verify brand, product, power, cylinder, axis, ADD, dominant-eye/design information, BC, quantity and other applicable parameters before confirming the order.

Where Optiwar's ordering system prevents a manufacturer-unsupported prescription combination, the customer must select from the available manufacturer-supported combinations.

An error in prescription information entered or confirmed by the customer does not make an otherwise correctly supplied contact-lens product defective.

Incorrectly supplied goods or genuine covered product defects will be reviewed separately.

14. RETURN DECISIONS ARE PRODUCT-SPECIFIC

Optiwar sells different categories of optical products.

The returnability of a frame, finished prescription spectacles, prescription lenses, accessories and sealed contact lenses cannot necessarily be treated identically.

A written return authorization for one component does not automatically authorize return or refund of every other component in the order.

15. COMMUNICATION AND WRITTEN RECORD

Return and replacement decisions should be communicated in writing.

The customer should retain the order confirmation, invoice, prescription details, return authorization and return tracking information.

Optiwar will similarly retain the applicable order and acceptance record according to its record-retention practices.

16. FAILED DELIVERY, RETURNED PACKAGES AND UNCLAIMED PARCELS

Where the courier cannot deliver an order (for example an incorrect or incomplete address, an unreachable phone number, refusal at the door or repeated absence) and returns the package to Optiwar, the order is not cancelled and the amount paid is not refunded on that account. Optiwar will inform the customer that the package is coming back.

Once the returned package has been physically received and checked in by Optiwar's operations team, the customer will be notified that it can be shipped again. For eligible orders placed on optiwar.in, reshipment is offered on payment of a fixed reshipping charge of Rs 250 through the customer's account (My Orders). The customer should confirm or correct the delivery address and phone number before paying, as the reshipment will otherwise be attempted to the same address.

Optiwar will hold a returned package for sixty (60) days counted from the date its operations team physically confirmed receipt of the package (not from the date the courier first failed to deliver, initiated the return or marked the shipment as returned). During this period Optiwar will make reasonable attempts to notify the customer at the e-mail address and phone number on the order.

If the reshipping charge has not been paid and the package has not been reshipped when the holding period ends, the package will be treated as abandoned. Reshipment through the customer's account is then no longer available, and Optiwar may handle or dispose of the abandoned goods as permitted by applicable law. The customer may write to support@optiwar.com within the holding period if the notified deadline needs to be reviewed. Nothing in this clause limits any right the customer has under law that cannot be excluded.

17. ACCEPTANCE AT CHECKOUT

This policy forms part of the terms upon which Optiwar accepts an order.

The customer is shown the applicable terms before payment/order confirmation, and the checkout requires an affirmative, un-preselected acceptance of Optiwar's Terms & Conditions and this Returns, Replacements & Limited Warranty Policy. The version accepted is recorded against the order.

18. CUSTOMER ACKNOWLEDGEMENT

By affirmatively accepting the applicable terms and placing an order, the customer acknowledges that the disclosed conditions formed part of the purchase decision.

Acceptance of these terms does not mean that Optiwar can disregard obligations that legally cannot be excluded. Rather, the purpose of these terms is to clearly define in advance the service Optiwar is selling, what services it is not selling, the treatment of customized optical products, and the procedure applicable if a return is subsequently requested.
"""

# ---------------------------------------------------------------------------
# Returns policy — optiwar.com (international)

RETURNS_COM = """\
OPTIWAR RETURNS, REPLACEMENTS & LIMITED WARRANTY POLICY — INTERNATIONAL ORDERS

Effective for Retail and Wholesale Orders placed on optiwar.com

1. INTERNATIONAL ORDERS ARE NON-RETURNABLE AND NON-REFUNDABLE

Orders placed on optiwar.com are exported from India. Once an order has been placed and paid, it is absolutely non-returnable and non-refundable.

Once goods are exported from India they are considered shipped with the generated AWB, as per Reserve Bank of India regulations. Any confiscation, detention, refusal or delay of goods outside Indian jurisdiction is outside Optiwar's control and is the liability of the customer. A chargeback on exported goods is un-authorised under the applicable regulations.

Optiwar does not operate a "buy, try and return" facility, trial period, satisfaction guarantee or change-of-mind return for international orders.

2. IMPORTANT — YOUR ORDER IS PLACED SUBJECT TO THESE TERMS

Optiwar operates as a modern electronic wholesale optical factory outlet. Our prices are based on centralized online ordering, factory/wholesale-style supply, automated processing and limited after-sale fitting services.

Optiwar is not a neighbourhood optical shop, boutique eyewear store or physical fitting centre.

Before placing and paying for an order, every customer is required to read and expressly accept this policy and Optiwar's applicable Terms & Conditions.

By affirmatively accepting these terms and placing the order, the customer confirms that:

- the product details, measurements, prescription information, lens selections, colours, quantities and prices have been reviewed before purchase;
- spectacles may require normal post-delivery adjustment by a local optician at the customer's own cost;
- international orders cannot be returned or refunded once placed;
- customized and prescription products are manufactured to the order and have no resale value to Optiwar; and
- the customer accepts these conditions as part of the purchase contract.

Nothing in these terms excludes any right or remedy that cannot lawfully be excluded or limited.

3. SPECTACLE FITTING, ALIGNMENT AND BALANCE

Spectacles are wearable optical products and may require adjustment to the individual customer's face after delivery — temple adjustment, nose-pad adjustment, alignment, tilt, tightening, balancing, adjustment behind the ears and other ordinary fitting work.

These matters do not, by themselves, establish a manufacturing defect. A customer may obtain such adjustment from a competent local optical store at the customer's own cost. Optiwar does not reimburse third-party fitting or adjustment charges.

4. PRESCRIPTION ACCURACY, ADAPTATION AND VISUAL COMFORT

Subjective visual discomfort does not automatically establish that prescription lenses were manufactured incorrectly.

Where a prescription complaint is raised, Optiwar may verify: Prescription supplied by customer → Prescription confirmed in order → Lens specification ordered → Lens actually supplied.

If the spectacles correspond to the prescription and specifications accepted by the customer, adaptation, wearing preference or subjective visual comfort does not create a right to replacement or refund.

5. COSMETIC CONDITIONS, MINOR MARKS AND THIRD-PARTY WORK

Minor cosmetic variation that does not materially impair the normal use of the product does not constitute a manufacturing defect.

Optiwar is not responsible for scratches, chips, marks, tool marks, deformation or damage caused after supply, including during lens fitting or removal by another optical store, frame adjustment, heating or bending, tightening or repair, accidental impact, improper handling, improper cleaning, chemical exposure, modification or other third-party work.

6. INCORRECTLY SUPPLIED PRODUCTS AND GENUINE MANUFACTURING DEFECTS

Where the customer claims that Optiwar supplied the wrong product, wrong specification, wrong prescription or a product with a genuine manufacturing defect present when supplied, Optiwar will investigate the claim on the evidence: photographs or video, order number, product packaging, prescription, measurements and product labels.

After verification, Optiwar will determine the appropriate remedy according to the circumstances, applicable warranty and applicable law. This is the only route by which an international order is reviewed; it is not a return or refund facility for correctly supplied goods.

7. CONTACT LENSES AND SEALED/HYGIENE-SENSITIVE PRODUCTS

Contact-lens packs and other hygiene-sensitive products cannot be returned. Customers must carefully verify brand, product, power, cylinder, axis, ADD, dominant-eye/design information, BC, quantity and other applicable parameters before confirming the order.

An error in prescription information entered or confirmed by the customer does not make an otherwise correctly supplied contact-lens product defective.

8. COMMUNICATION AND WRITTEN RECORD

Decisions on incorrect-supply and defect claims are communicated in writing. The customer should retain the order confirmation, invoice, prescription details and tracking information. Optiwar retains the applicable order and acceptance record according to its record-retention practices.

9. ACCEPTANCE AT CHECKOUT

This policy forms part of the terms upon which Optiwar accepts an order. The customer is shown the applicable terms before payment, and the checkout requires an affirmative, un-preselected acceptance of Optiwar's Terms & Conditions and this policy. The version accepted is recorded against the order.
"""

# .in 2026-09-27: clause 16 (failed delivery, returned packages, 60-day hold
# from physical receipt, abandonment). Earlier versions stay sealed as accepted.
RETURNS_VERSION_DATE = {SITE_IN: "2026-09-27", SITE_COM: "2026-09-15"}
RETURNS_TEXT = {SITE_IN: RETURNS_IN, SITE_COM: RETURNS_COM}

# What the customer ticks, immediately before paying. Each is the concise form
# of its site's policy; a change to the wording is a new returns version because
# the text is part of the sealed document.
ACCEPTANCE_IN = (
    "I have reviewed my order and prescription selections and agree to Optiwar's "
    "Terms & Conditions and Returns, Replacements & Limited Warranty Policy. "
    "I understand that Optiwar is an electronic wholesale/factory-outlet service; "
    "correctly supplied customized prescription products have restricted returns; "
    "prescription-lens/customization value of up to 50% of the relevant returned "
    "spectacle value may be deducted from an approved discretionary return after "
    "inspection; and applicable return/reverse-pickup charges may also be payable."
)
ACCEPTANCE_COM = (
    "I have reviewed my order and prescription selections and agree to Optiwar's "
    "Terms & Conditions and Returns, Replacements & Limited Warranty Policy. "
    "I understand that Optiwar is an electronic wholesale/factory-outlet service "
    "exporting from India, and that international orders are absolutely "
    "non-returnable and non-refundable once placed."
)
ACCEPTANCE_TEXT = {SITE_IN: ACCEPTANCE_IN, SITE_COM: ACCEPTANCE_COM}

FORM_FIELD = "terms_accepted"
FORM_VERSION_FIELD = "returns_policy_version"

# Disclosure flags recorded YES/NO against the order.
DISCLOSURES = ("lens_deduction_shown", "reverse_charge_shown",
               "contact_lens_hygiene_shown", "international_non_returnable_shown",
               "returned_parcel_holding_shown")

SCHEMA_VERSIONS = """
CREATE TABLE IF NOT EXISTS policy_versions (
    policy_version_id INT AUTO_INCREMENT PRIMARY KEY,
    kind          VARCHAR(32)  NOT NULL,
    site          VARCHAR(8)   NOT NULL,
    version       VARCHAR(80)  NOT NULL,
    sha256        CHAR(64)     NOT NULL,
    body          MEDIUMTEXT   NOT NULL,
    published_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY ux_policy_sha (kind, site, sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SCHEMA_ACCEPTANCE = """
CREATE TABLE IF NOT EXISTS order_terms_acceptance (
    acceptance_id           INT AUTO_INCREMENT PRIMARY KEY,
    order_id                VARCHAR(64)  NOT NULL,
    checkout_token          VARCHAR(64)  NULL,
    site                    VARCHAR(8)   NOT NULL,
    terms_version           VARCHAR(80)  NOT NULL,
    terms_sha256            CHAR(64)     NOT NULL,
    returns_policy_version  VARCHAR(80)  NOT NULL,
    returns_sha256          CHAR(64)     NOT NULL,
    acceptance_text_sha256  CHAR(64)     NOT NULL,
    accepted_at             DATETIME     NOT NULL,
    customer_id             INT          NULL,
    ip_address              VARCHAR(64)  NULL,
    disclosures             TEXT         NOT NULL,
    UNIQUE KEY ux_ota_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("policy_versions", SCHEMA_VERSIONS),
          ("order_terms_acceptance", SCHEMA_ACCEPTANCE))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def site_key(site):
    s = (site or "").strip().lower()
    if s in (SITE_IN, "india") or "optiwar.in" in s or s.startswith("in.optiwar"):
        return SITE_IN
    return SITE_COM


def is_india(site):
    return site_key(site) == SITE_IN


_TERMS_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "templates", "terms-and-conditions.html")


def terms_text():
    """The T&C document as deployed: the template source itself. The returns
    section is inserted from this module at render time, so the two hashes
    together identify exactly what the customer could read."""
    with open(_TERMS_TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


def current(site):
    """Versions in force for a storefront: {kind: {version, sha256, text}}."""
    key = site_key(site)
    returns = RETURNS_TEXT[key] + "\n\nACCEPTANCE TEXT:\n" + ACCEPTANCE_TEXT[key]
    terms_body = terms_text()
    return {
        "site": key,
        "terms": {"version": TERMS_VERSION_DATE, "sha256": sha(terms_body),
                  "text": terms_body},
        "returns": {"version": "%s-%s" % (RETURNS_VERSION_DATE[key], key),
                    "sha256": sha(returns), "text": returns},
        "acceptance_text": ACCEPTANCE_TEXT[key],
        "acceptance_sha256": sha(ACCEPTANCE_TEXT[key]),
    }


def ensure_schema(cursor):
    for _name, ddl in TABLES:
        cursor.execute(ddl)


def seal_versions(cursor, site):
    """Insert the current versions into policy_versions if not already sealed.

    Idempotent by (kind, site, sha256); the body is written once and never
    updated, which is what makes an old order's version reproducible.
    """
    cur = current(site)
    for kind in ("terms", "returns"):
        doc = cur[kind]
        cursor.execute(
            "INSERT IGNORE INTO policy_versions (kind, site, version, sha256, body) "
            "VALUES (%s, %s, %s, %s, %s)",
            (kind, cur["site"], doc["version"], doc["sha256"], doc["text"]))


# ---------------------------------------------------------------------------
# Cart-derived disclosures

def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def is_contact_lens(item):
    return (item or {}).get("vertical") == "CONTACT_LENS" or \
        (item or {}).get("product_category") == "Contact Lenses"


def is_spectacle(item):
    """Every eyewear line that is not a contact lens: frames and sunglasses
    alike take prescription lenses, and the deduction is about those lenses."""
    return bool(item) and not is_contact_lens(item)


def spectacle_summary(cart):
    """Frame / prescription lens customization / other options / total.

    ``ATC_total`` is the frame, ``server_total_price`` the optical-lens
    customization, add-on names/prices the other options; the same fields the
    checkout totals from, so the summary cannot disagree with the Pay button.
    """
    out = []
    for item in cart or []:
        if not is_spectacle(item):
            continue
        frame = _f(item.get("ATC_total"))
        lens = _f(item.get("server_total_price"))
        others = []
        for n in (1, 2, 3):
            name = item.get("addon_%d_name" % n)
            if name:
                others.append((name, _f(item.get("addon_%d_price" % n))))
        if item.get("recommendations") and not others:
            others.append((str(item.get("recommendations")), 0.0))
        out.append({
            "product_name": item.get("product_name"),
            "quantity": int(item.get("order_quantity") or 1),
            "frame": frame,
            "lens": lens,
            "lens_label": item.get("recommendations") or None,
            "others": others,
            "total": frame + lens,
            "customized": lens > 0,
        })
    return out


def disclosures_for(cart, site):
    """Which disclosures the checkout shows for this cart on this site — all
    flags present, YES/NO, so the record says what was *not* shown too."""
    key = site_key(site)
    has_custom = any(s["customized"] for s in spectacle_summary(cart))
    has_cl = any(is_contact_lens(i) for i in cart or [])
    intl = key == SITE_COM
    return {
        "lens_deduction_shown": bool(has_custom and not intl),
        "reverse_charge_shown": not intl,
        "contact_lens_hygiene_shown": has_cl,
        "international_non_returnable_shown": intl,
        "returned_parcel_holding_shown": not intl,
    }


def checkout_context(cart, site):
    cur = current(site)
    return {
        "policy_site": cur["site"],
        "policy_is_international": cur["site"] == SITE_COM,
        "terms_version": cur["terms"]["version"],
        "returns_policy_version": cur["returns"]["version"],
        "acceptance_text": cur["acceptance_text"],
        "disclosures": disclosures_for(cart, site),
        "spectacle_summary": spectacle_summary(cart),
        "lens_deduction_cap_percent": LENS_DEDUCTION_CAP_PERCENT,
        "terms_url": TERMS_URL,
        "returns_url": RETURNS_URL,
        "terms_field": FORM_FIELD,
        "terms_version_field": FORM_VERSION_FIELD,
    }


# ---------------------------------------------------------------------------
# Acceptance

class NotAccepted(Exception):
    """The form did not carry an affirmative acceptance of the current version."""


def accepted(form, site):
    """True only for an explicit tick of the version currently in force.

    A missing box is a refusal; a stale version means the customer read a
    different document than the one being recorded, which is also a refusal.
    """
    if (form.get(FORM_FIELD) or "").strip().lower() not in ("1", "on", "true", "yes"):
        return False
    posted = (form.get(FORM_VERSION_FIELD) or "").strip()
    return posted == current(site)["returns"]["version"]


def require(form, site):
    if not accepted(form, site):
        raise NotAccepted(
            "Please tick the box to accept Optiwar's Terms & Conditions and "
            "Returns, Replacements & Limited Warranty Policy before paying.")


def record(cursor, order_id, site, cart, checkout_token=None, customer_id=None,
           ip_address=None, now=None):
    """Snapshot the accepted versions against the order. Write-once per order."""
    cur = current(site)
    seal_versions(cursor, site)
    accepted_at = now or datetime.now(timezone.utc).replace(tzinfo=None)
    disclosures = disclosures_for(cart, site)
    cursor.execute(
        "INSERT IGNORE INTO order_terms_acceptance "
        "(order_id, checkout_token, site, terms_version, terms_sha256, "
        " returns_policy_version, returns_sha256, acceptance_text_sha256, "
        " accepted_at, customer_id, ip_address, disclosures) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (str(order_id), checkout_token, cur["site"],
         cur["terms"]["version"], cur["terms"]["sha256"],
         cur["returns"]["version"], cur["returns"]["sha256"],
         cur["acceptance_sha256"], accepted_at, customer_id, ip_address,
         json.dumps({k: ("YES" if v else "NO") for k, v in disclosures.items()})))
    return {"order_id": str(order_id), "site": cur["site"],
            "terms_version": cur["terms"]["version"],
            "returns_policy_version": cur["returns"]["version"],
            "accepted_at": accepted_at, "disclosures": disclosures}


def for_order(cursor, order_id):
    """The acceptance an order was placed under, or None for a pre-policy order."""
    cursor.execute(
        "SELECT order_id, site, terms_version, terms_sha256, "
        "returns_policy_version, returns_sha256, accepted_at, disclosures "
        "FROM order_terms_acceptance WHERE order_id=%s", (str(order_id),))
    row = cursor.fetchone()
    if not row:
        return None
    row = dict(row)
    try:
        row["disclosures"] = json.loads(row.get("disclosures") or "{}")
    except ValueError:
        row["disclosures"] = {}
    return row


def sealed_text(cursor, kind, sha256):
    cursor.execute("SELECT body FROM policy_versions WHERE kind=%s AND sha256=%s",
                   (kind, sha256))
    row = cursor.fetchone()
    return row["body"] if row else None


def confirmation_line(site_host, acceptance=None):
    """The sentence the confirmation email carries, with the version it names."""
    host = site_host or "optiwar.com"
    line = ("Your order was placed subject to the Optiwar Terms & Conditions and "
            "Returns, Replacements & Limited Warranty Policy accepted at checkout.")
    if acceptance:
        line += " (Terms %s, Returns policy %s.)" % (
            acceptance.get("terms_version"), acceptance.get("returns_policy_version"))
    return line, "https://%s%s" % (host, TERMS_URL), "https://%s%s" % (host, RETURNS_URL)


# ---------------------------------------------------------------------------
# Rendering the policy text as HTML

_HEADING = re.compile(r"^\d+\.\s+\S")


def render_html(text):
    """Plain policy text → HTML. Numbered lines are headings, '- ' lines list
    items, everything else a paragraph. Text is escaped; nothing is invented."""
    from html import escape
    out, in_list = [], False
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % escape(line[2:]))
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if i == 0:
            out.append("<h2 id=\"returns-policy\">%s</h2>" % escape(line))
        elif _HEADING.match(line):
            out.append("<h3>%s</h3>" % escape(line))
        else:
            out.append("<p>%s</p>" % escape(line))
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def returns_html(site):
    return render_html(RETURNS_TEXT[site_key(site)])
