# Local-only launcher (not part of the repo): uses the local MariaDB login from .env.local,
# and serves https://in.localhost:8010 as optiwar.in (Chrome resolves *.localhost locally).
import os
from flaskr import create_app

app = create_app()

# Local-only: production has AVIF/WebP derivatives for every catalog photo and the
# committed manifest says so. This box builds them with local_derivatives.py; until a
# master's ladder exists here, forget its manifest entry so the template emits a plain
# <img> instead of <picture> sources that would 404.
try:
    import os as _os
    import flaskr.embed_helper as _eh
    _root = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'repo', 'static', 'catalog')
    _ver = getattr(_eh, '_VER', {})
    _drop = []
    for _k in list(_ver):
        if '/derivatives/' in _k:
            continue
        _rel = _k if _k.startswith('opticalbazaar/') or _k.startswith('contact-lenses/') or _k.startswith('rayban-meta/') else 'opticalbazaar/' + _k
        _d, _n = _os.path.split(_rel); _stem = _os.path.splitext(_n)[0]
        if not _os.path.exists(_os.path.join(_root, _d, 'derivatives', _stem + '-400.avif')):
            _drop.append(_k)
    for _k in _drop:
        _ver.pop(_k, None)
    print('embed manifest: %d masters without local derivatives hidden, %d kept' % (len(_drop), len(_ver)))
except Exception as _e:  # never block local start-up
    print('embed manifest filter skipped:', _e)
app.config['MYSQL_USER'] = os.environ.get('LOCAL_MYSQL_USER', app.config['MYSQL_USER'])

_inner = app.wsgi_app


def _india_host(environ, start_response):
    host = environ.get('HTTP_HOST', '')
    if host.split(':')[0] == 'in.localhost':
        environ['HTTP_HOST'] = 'optiwar.in'
    return _inner(environ, start_response)


app.wsgi_app = _india_host


# Local-only test sign-in (this launcher only; never in the repo). Signs in the local TEST
# customer (customers.is_test = 1), so /test-checkout makes is_test orders: no payment, no stock.
from flask import redirect, request, session  # noqa: E402

LOCAL_TEST_EMAIL = 'aig.localtest@optiwar.local'


@app.route('/__local/test-login')
def _local_test_login():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return 'Not found', 404
    from flaskr.db import get_db
    cur = get_db().cursor()
    cur.execute('SELECT customer_id, customer_name FROM customers WHERE customer_email=%s AND is_test=1',
                (LOCAL_TEST_EMAIL,))
    row = cur.fetchone()
    if not row:
        return 'Local test customer missing', 404
    session['user_id'] = row['customer_id']
    session['user_name'] = row['customer_name']
    session['user_email'] = LOCAL_TEST_EMAIL
    return redirect(request.args.get('next') or '/ai-glasses')
