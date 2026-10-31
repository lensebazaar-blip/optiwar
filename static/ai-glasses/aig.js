/* AI Glasses pages: size buttons on the launch page cards; gallery, lens choice,
   prescription panel and live total on the product page. Plain ES5. */
(function () {
  function all(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
  function money(v) { return '₹' + Math.round(v).toLocaleString('en-IN'); }

  /* launch page: switch a card between Standard and Large */
  all('.aig-card').forEach(function (card) {
    var sizes = all('.aig-size', card);
    sizes.forEach(function (btn) {
      btn.addEventListener('click', function () {
        sizes.forEach(function (b) { b.classList.toggle('is-active', b === btn); });
        all('[data-card-link]', card).forEach(function (a) { a.setAttribute('href', btn.getAttribute('data-href')); });
        card.querySelector('[data-card-price]').textContent = btn.getAttribute('data-price');
        card.querySelector('[data-card-dispatch]').textContent = btn.getAttribute('data-dispatch');
        var badge = card.querySelector('[data-card-badge]');
        badge.textContent = btn.getAttribute('data-badge');
        badge.className = 'aig-badge aig-badge--' + btn.getAttribute('data-status');
        var mrp = card.querySelector('[data-card-mrp]');
        if (mrp) {
          mrp.textContent = btn.getAttribute('data-mrp');
          card.querySelector('[data-card-off]').textContent = btn.getAttribute('data-off');
          mrp.parentNode.hidden = !btn.getAttribute('data-mrp');
        }
      });
    });
  });

  /* product page */
  var form = document.getElementById('aig-form');
  if (!form) { return; }
  var main = document.getElementById('aig-main-img');
  all('.aig-thumb').forEach(function (t) {
    t.addEventListener('click', function () {
      main.src = t.getAttribute('data-src');
      all('.aig-thumb').forEach(function (x) { x.classList.toggle('is-active', x === t); });
    });
  });

  var price = document.getElementById('aig-price');
  var base = parseFloat(price.getAttribute('data-base')) || 0;
  var total = document.getElementById('aig-total');
  var rx = document.getElementById('aig-rx');
  var table = document.getElementById('aig-rx-table');
  var later = document.getElementById('aig-rx-later');

  function update() {
    var lens = form.querySelector('input[name="lens"]:checked');
    var extra = lens ? parseFloat(lens.getAttribute('data-price')) || 0 : 0;
    var sum = money(base + extra);
    price.textContent = sum;
    if (total) { total.textContent = sum; }
    if (rx) {
      var needsRx = lens && lens.getAttribute('data-rx') === '1';
      rx.hidden = !needsRx;
      rx.classList.toggle('is-prog', !!lens && lens.value === 'progressive');
      var mode = form.querySelector('input[name="rx_mode"]:checked');
      var isLater = mode && mode.value === 'later';
      table.hidden = isLater;
      later.hidden = !isLater;
    }
  }
  all('input[name="lens"], input[name="rx_mode"]', form).forEach(function (i) { i.addEventListener('change', update); });
  update();
})();
