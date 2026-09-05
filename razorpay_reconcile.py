#!/usr/bin/env python3
"""Reconcile recent Pending orders against Razorpay — the safety net.

Runs every few minutes from cron, under the application's virtualenv and
environment, and asks Razorpay about every recent order that is still
``Pending`` locally with no successful payment row. A captured payment whose
amount, currency and receipt all reconcile is applied through the same
``settle`` -> ``apply_paid_order`` path as the webhook and the browser, and
the customer receives the normal paid-order acknowledgement. Anything
Razorpay reports unpaid is left alone. Conflicting evidence is never applied;
it is logged as ``PAYMENT_RECONCILIATION_EXCEPTION`` and alerted.

Invariant: a payment captured at Razorpay must not be Pending at Optiwar for
longer than the grace period. Finding one is RED even though this job then
fixes it — it means the webhook and the browser callback both failed — so it
is written to the alerts log at once rather than waiting for the 06:00 report.

Cron (see deploy/DEPLOYMENT.md):
*/10 * * * * set -a; . /etc/optiwar/optiwar-secrets.env; set +a; \
  /var/www/flask-optiwar-ow-release-090525/venv/bin/python \
  /var/www/flask-optiwar-ow-release-090525/venv/lib/python3.11/site-packages/flaskr/razorpay_reconcile.py \
  >> /var/log/optiwar/razorpay_reconcile.log 2>&1
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

GRACE_MINUTES = int(os.environ.get('RAZORPAY_RECONCILE_GRACE_MINUTES', '30'))
MIN_AGE_MINUTES = int(os.environ.get('RAZORPAY_RECONCILE_MIN_AGE_MINUTES', '10'))
MAX_AGE_HOURS = int(os.environ.get('RAZORPAY_RECONCILE_MAX_AGE_HOURS', '72'))
STATE_FILE = os.environ.get('RAZORPAY_RECONCILE_STATE',
                            '/var/log/optiwar/razorpay_reconcile_latest.json')
ALERT_LOG = os.environ.get('OPTIWAR_ALERT_LOG', '/var/log/optiwar/alerts.log')


def alert(text):
    """Same channel as /root/backups/optiwar_alert_check.sh."""
    ts = datetime.now().strftime('%Y-%m-%dT%H:%M:%S%z')
    try:
        with open(ALERT_LOG, 'a') as fh:
            fh.write('%s ALERT %s\n' % (ts, text))
    except OSError:
        pass
    hook = os.environ.get('ALERT_WEBHOOK')
    if hook:
        try:
            req = urllib.request.Request(
                hook, data=json.dumps({'text': '[optiwar] %s' % text}).encode(),
                headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5)
        except Exception:  # noqa: BLE001
            pass


def main():
    # Run from inside site-packages/flaskr the package is importable already;
    # run from a checkout, its parent directory has to be.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from flaskr import create_app
    from flaskr.db import get_db
    from flaskr.notifications import notify_payment_success, notify_order_confirmed
    from flaskr.payments import (fetch_razorpay_orders_by_receipt,
                                 fetch_razorpay_order_payments)
    from flaskr.razorpay_settlement import (reconcile_pending, notify_paid_order,
                                            summary_json)

    app = create_app()
    started = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def on_settled(order_row, settled):
        host = order_row.get('site_from') or 'optiwar.com'
        with app.test_request_context(base_url='https://%s/' % host):
            notify_paid_order(get_db().cursor(), order_row['order_id'], settled, host,
                              notify_payment_success, notify_order_confirmed,
                              logger=app.logger)

    with app.test_request_context(base_url='https://optiwar.com/'):
        summary = reconcile_pending(
            get_db(), fetch_razorpay_orders_by_receipt, fetch_razorpay_order_payments,
            logger=app.logger, grace_minutes=GRACE_MINUTES,
            min_age_minutes=MIN_AGE_MINUTES, max_age_hours=MAX_AGE_HOURS,
            now_ts=int(time.time()), on_settled=on_settled)

    text = summary_json(summary, started)
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as fh:
            fh.write(text)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        print('state file not written: %s' % exc)

    print('[%s] checked=%d settled=%d unpaid=%d duplicate=%d exception=%d over_grace=%d'
          % (started, summary['checked'], summary['settled'], summary['unpaid'],
             summary['duplicate'], summary['exception'], summary['over_grace']))
    for e in summary['exceptions']:
        alert('PAYMENT_RECONCILIATION_EXCEPTION order=%s payment=%s %s'
              % (e['order_id'], e['payment_id'], e['detail']))
    if summary['over_grace']:
        alert('PAYMENT_INVARIANT_RED %d order(s) captured at Razorpay but Pending '
              'locally beyond %d min: %s'
              % (summary['over_grace'], GRACE_MINUTES, ', '.join(summary['settled_orders'])))
    return 1 if summary['exceptions'] else 0


if __name__ == '__main__':
    sys.exit(main())
