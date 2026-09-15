"""A return's refund is calculated from the accepted policy, not typed in.

    AMOUNT PAID
  − APPROVED CUSTOMIZED-LENS DEDUCTION   (≤ cap% of the relevant returned spectacle value)
  − APPROVED REVERSE LOGISTICS
  = PROPOSED REFUND

The operator supplies the claim type, the value of the spectacles being
returned, the two deductions, and the inspection that justifies them. This
module refuses anything the customer's accepted policy version does not allow:

* a deduction with no inspection reason and evidence;
* a lens deduction above the cap for that policy version;
* any deduction on an incorrect-supply or manufacturing-defect claim — those
  are Optiwar's remedies, not the customer's cost;
* a discretionary return on optiwar.com, whose orders were accepted as
  absolutely non-returnable/non-refundable, unless the operator records a
  written override reason (a documented commercial exception, not a default).

Every assessment is an append-only row; the refund that follows names the
assessment it executes and must carry exactly its proposed amount.
"""
import json
from datetime import datetime, timezone

try:
    from . import policy_terms
except ImportError:  # loaded as a plain module (tests, deploy tool)
    import policy_terms

DISCRETIONARY = 'DISCRETIONARY_RETURN'
INCORRECT_SUPPLY = 'INCORRECT_SUPPLY'
MANUFACTURING_DEFECT = 'MANUFACTURING_DEFECT'
CLAIM_TYPES = (DISCRETIONARY, INCORRECT_SUPPLY, MANUFACTURING_DEFECT)

# What the inspection must answer, YES/NO, before any deduction stands.
INSPECTION_FINDINGS = (
    'is_supplied_item', 'used', 'modified', 'damaged',
    'worked_on_elsewhere', 'missing_components', 'defective',
    'commercially_reusable', 'non_recoverable_customization',
)
MIN_REASON_CHARS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS return_assessments (
    assessment_id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id                 VARCHAR(255) NOT NULL,
    site                     VARCHAR(8)   NOT NULL,
    claim_type               VARCHAR(32)  NOT NULL,
    returns_policy_version   VARCHAR(80)  NULL,
    returns_sha256           CHAR(64)     NULL,
    currency                 VARCHAR(8)   NOT NULL,
    paid_minor               BIGINT NOT NULL,
    spectacle_value_minor    BIGINT NOT NULL DEFAULT 0,
    lens_deduction_cap_pct   INT    NOT NULL,
    lens_deduction_cap_minor BIGINT NOT NULL DEFAULT 0,
    lens_deduction_minor     BIGINT NOT NULL DEFAULT 0,
    reverse_logistics_minor  BIGINT NOT NULL DEFAULT 0,
    proposed_refund_minor    BIGINT NOT NULL,
    inspection_reason        TEXT NULL,
    inspection_evidence      TEXT NULL,
    inspection_findings      TEXT NULL,
    policy_override_reason   TEXT NULL,
    assessed_by              VARCHAR(191) NOT NULL,
    service_identity         VARCHAR(64)  NOT NULL,
    assessed_at              DATETIME NOT NULL,
    refund_id                BIGINT NULL,
    executed_at              DATETIME NULL,
    KEY idx_ra_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("return_assessments", SCHEMA),)


class AssessmentRejected(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _int(v, name):
    if v is None or v == '':
        return 0
    if isinstance(v, bool) or not isinstance(v, int):
        raise AssessmentRejected('%s_not_integer' % name,
                                 '%s must be an integer in minor units' % name)
    if v < 0:
        raise AssessmentRejected('%s_negative' % name, '%s must be >= 0' % name)
    return v


def cap_minor(spectacle_value_minor, cap_percent):
    """Floor: the customer is never charged the rounding."""
    return (spectacle_value_minor * cap_percent) // 100


def propose(facts, acceptance, claim_type, spectacle_value_minor=0,
            lens_deduction_minor=0, reverse_logistics_minor=0,
            inspection_reason=None, inspection_evidence=None,
            inspection_findings=None, policy_override_reason=None):
    """The proposed refund and every figure it came from. Pure; writes nothing.

    ``facts`` is ``refunds.preview()``'s output (paid amount from the provider);
    ``acceptance`` is ``policy_terms.for_order()`` or None for a pre-policy order,
    in which case the storefront decides the site and the current cap applies.
    """
    if claim_type not in CLAIM_TYPES:
        raise AssessmentRejected('bad_claim_type',
                                 'claim_type must be one of %s' % ', '.join(CLAIM_TYPES))
    if facts.get('payment_status') != 'PAID' or not facts.get('captured_minor'):
        raise AssessmentRejected('order_not_paid',
                                 'no captured payment to assess a refund against')
    paid = int(facts['captured_minor'])
    refundable = int(facts.get('max_refundable_minor') or 0)
    spectacle = _int(spectacle_value_minor, 'spectacle_value_minor')
    lens = _int(lens_deduction_minor, 'lens_deduction_minor')
    logistics = _int(reverse_logistics_minor, 'reverse_logistics_minor')
    site = policy_terms.site_key((acceptance or {}).get('site')
                                 or facts.get('storefront'))
    cap_pct = policy_terms.LENS_DEDUCTION_CAP_PERCENT
    cap = cap_minor(spectacle, cap_pct)
    reason = (inspection_reason or '').strip()
    evidence = (inspection_evidence or '').strip()
    override = (policy_override_reason or '').strip()
    findings = {k: bool((inspection_findings or {}).get(k)) for k in INSPECTION_FINDINGS}

    if claim_type == DISCRETIONARY and site == policy_terms.SITE_COM:
        if len(override) < MIN_REASON_CHARS:
            raise AssessmentRejected(
                'com_non_refundable',
                'optiwar.com orders were accepted as absolutely non-returnable and '
                'non-refundable; a discretionary refund needs a written '
                'policy_override_reason (at least %d characters)' % MIN_REASON_CHARS)
    elif override:
        raise AssessmentRejected('override_not_applicable',
                                 'policy_override_reason applies only to a '
                                 'discretionary return on optiwar.com')

    if claim_type != DISCRETIONARY and (lens or logistics):
        raise AssessmentRejected(
            'deduction_not_for_%s' % claim_type.lower(),
            'an incorrect-supply or manufacturing-defect claim carries no '
            'customized-lens or reverse-logistics deduction')
    if lens or logistics:
        if len(reason) < MIN_REASON_CHARS or not evidence:
            raise AssessmentRejected(
                'deduction_requires_inspection',
                'a deduction needs inspection_reason (at least %d characters) '
                'and inspection_evidence (photo/report references)' % MIN_REASON_CHARS)
        if not inspection_findings:
            raise AssessmentRejected(
                'deduction_requires_findings',
                'record the inspection findings (%s)' % ', '.join(INSPECTION_FINDINGS))
    if lens:
        if spectacle <= 0:
            raise AssessmentRejected('spectacle_value_required',
                                     'a lens deduction needs the value of the '
                                     'returned spectacles it is taken from')
        if spectacle > paid:
            raise AssessmentRejected('spectacle_value_exceeds_paid',
                                     'the returned spectacle value cannot exceed '
                                     'what was paid')
        if lens > cap:
            raise AssessmentRejected(
                'lens_deduction_exceeds_cap',
                'customized-lens deduction is capped at %d%% of the returned '
                'spectacle value = %d minor units' % (cap_pct, cap))

    proposed = paid - lens - logistics
    if proposed < 0:
        raise AssessmentRejected('deductions_exceed_paid',
                                 'deductions exceed the amount paid')
    if proposed > refundable:
        raise AssessmentRejected(
            'proposed_exceeds_refundable',
            'only %d minor units remain refundable on this payment' % refundable)

    return {
        'order_id': facts['order_id'],
        'site': site,
        'claim_type': claim_type,
        'returns_policy_version': (acceptance or {}).get('returns_policy_version'),
        'returns_sha256': (acceptance or {}).get('returns_sha256'),
        'currency': facts.get('currency'),
        'paid_minor': paid,
        'spectacle_value_minor': spectacle,
        'lens_deduction_cap_pct': cap_pct,
        'lens_deduction_cap_minor': cap,
        'lens_deduction_minor': lens,
        'reverse_logistics_minor': logistics,
        'proposed_refund_minor': proposed,
        'inspection_reason': reason or None,
        'inspection_evidence': evidence or None,
        'inspection_findings': findings if (lens or logistics) else None,
        'policy_override_reason': override or None,
    }


def record(cursor, proposal, assessed_by, service_identity, now=None):
    when = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cursor.execute(
        "INSERT INTO return_assessments (order_id, site, claim_type, "
        "returns_policy_version, returns_sha256, currency, paid_minor, "
        "spectacle_value_minor, lens_deduction_cap_pct, lens_deduction_cap_minor, "
        "lens_deduction_minor, reverse_logistics_minor, proposed_refund_minor, "
        "inspection_reason, inspection_evidence, inspection_findings, "
        "policy_override_reason, assessed_by, service_identity, assessed_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (proposal['order_id'], proposal['site'], proposal['claim_type'],
         proposal['returns_policy_version'], proposal['returns_sha256'],
         proposal['currency'], proposal['paid_minor'],
         proposal['spectacle_value_minor'], proposal['lens_deduction_cap_pct'],
         proposal['lens_deduction_cap_minor'], proposal['lens_deduction_minor'],
         proposal['reverse_logistics_minor'], proposal['proposed_refund_minor'],
         proposal['inspection_reason'], proposal['inspection_evidence'],
         json.dumps(proposal['inspection_findings'])
         if proposal['inspection_findings'] is not None else None,
         proposal['policy_override_reason'], assessed_by, service_identity, when))
    return cursor.lastrowid


def get(cursor, assessment_id):
    cursor.execute("SELECT * FROM return_assessments WHERE assessment_id=%s",
                   (assessment_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def for_order(cursor, order_id):
    cursor.execute("SELECT * FROM return_assessments WHERE order_id=%s "
                   "ORDER BY assessment_id", (order_id,))
    return [dict(r) for r in cursor.fetchall()]


def mark_executed(cursor, assessment_id, refund_id, now=None):
    when = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cursor.execute("UPDATE return_assessments SET refund_id=%s, executed_at=%s "
                   "WHERE assessment_id=%s AND refund_id IS NULL",
                   (refund_id, when, assessment_id))
    return cursor.rowcount == 1


def check_refund_matches(assessment, order_id, amount_minor, currency):
    """A return refund executes an assessment, once, at exactly its proposed amount."""
    if not assessment:
        raise AssessmentRejected('assessment_not_found', 'no such assessment')
    if assessment.get('refund_id'):
        raise AssessmentRejected('assessment_already_executed',
                                 'that assessment was refunded as refund %s'
                                 % assessment['refund_id'])
    if str(assessment['order_id']) != str(order_id):
        raise AssessmentRejected('assessment_order_mismatch',
                                 'that assessment belongs to another order')
    if int(assessment['proposed_refund_minor']) != amount_minor \
            or (assessment.get('currency') or None) != currency:
        raise AssessmentRejected(
            'amount_not_the_assessment',
            'a return refund must be exactly the assessed %s %d minor units'
            % (assessment.get('currency'), int(assessment['proposed_refund_minor'])))
