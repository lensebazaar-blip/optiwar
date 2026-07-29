/* EUR prefix for links - fallback only if not set by template */
try { if (typeof owEuPrefix === "undefined") owEuPrefix = window.location.pathname.startsWith("/eu/") || window.location.pathname === "/eu" ? "/eu" : ""; } catch(e) { window.owEuPrefix = window.location.pathname.startsWith("/eu/") || window.location.pathname === "/eu" ? "/eu" : ""; }
try { if (typeof owIsEur === "undefined") owIsEur = owEuPrefix === "/eu"; } catch(e) { window.owIsEur = window.owEuPrefix === "/eu"; }
/* ─── Popup open/close (new theme) ─── */
function openPopup(id) {
  document.getElementById(id).classList.add('active');
  document.body.style.overflow = 'hidden';
}
function closePopup(id) {
  document.getElementById(id).classList.remove('active');
  document.body.style.overflow = '';
}
document.querySelectorAll('.overlay').forEach(function(ov) {
  ov.addEventListener('click', function(e) {
    if (e.target === ov) { ov.classList.remove('active'); document.body.style.overflow = ''; }
  });
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    document.querySelectorAll('.overlay.active').forEach(function(ov) { ov.classList.remove('active'); });
    document.body.style.overflow = '';
  }
});

/* ─── Categories overlay (updated to use active class) ─── */
function openCategories() {
  document.getElementById("categories-overlay").classList.add('active');
  document.body.style.overflow = 'hidden';
}
function closeCategories() {
  document.getElementById("categories-overlay").classList.remove('active');
  document.body.style.overflow = '';
}

/* ─── Hero Carousel (auto-roll every 3s) ─── */
(function() {
  var hc = document.getElementById('heroCarousel');
  if (!hc) return;
  var hImgs = hc.querySelectorAll('img');
  var hDotsEl = document.getElementById('heroCarouselDots');
  if (!hDotsEl || !hImgs.length) return;
  var hCurr = 0;
  var hTimer;
  for (var i = 0; i < hImgs.length; i++) {
    var d = document.createElement('span');
    if (i === 0) d.className = 'active';
    d.setAttribute('data-idx', i);
    d.addEventListener('click', function() { heroGo(+this.getAttribute('data-idx')); });
    hDotsEl.appendChild(d);
  }
  var hDots = hDotsEl.querySelectorAll('span');
  function heroGo(idx) {
    hImgs[hCurr].classList.remove('active');
    hDots[hCurr].classList.remove('active');
    hCurr = ((idx % hImgs.length) + hImgs.length) % hImgs.length;
    hImgs[hCurr].classList.add('active');
    hDots[hCurr].classList.add('active');
  }
  window.heroSlide = function(dir) {
    heroGo(hCurr + dir);
    clearInterval(hTimer);
    hTimer = setInterval(function() { heroGo(hCurr + 1); }, 3000);
  };
  hTimer = setInterval(function() { heroGo(hCurr + 1); }, 3000);
})();

/* ─── Banner Solo (rotating announcements every 3s) ─── */
(function() {
  var items = document.querySelectorAll('#bannerSolo a');
  if (!items.length) return;
  var curr = 0;
  setInterval(function() {
    items[curr].classList.remove('active');
    items[curr].classList.add('exit');
    var prev = curr;
    curr = (curr + 1) % items.length;
    items[curr].classList.add('active');
    setTimeout(function() { items[prev].classList.remove('exit'); }, 500);
  }, 3000);
})();


/* ═══════════════════════════════════════════
   EXISTING SITE FUNCTIONALITY (preserved)
   ═══════════════════════════════════════════ */

window.addEventListener('load', function() {
    setTimeout(function() {
        var loader = document.querySelector('.loader');
        if (loader) loader.style.display = 'none';
        var nav = document.querySelector('nav');
        if (nav) nav.style.visibility = 'visible';
        var content = document.querySelector('.content');
        if (content) content.style.visibility = 'visible';
    }, 1000);

    const emailField = document.getElementById('customer_email');
    const nameField = document.getElementById('customer_name');
    const addressField = document.getElementById('customer_address');
    const phoneField = document.getElementById('customer_phone');
    const postcodeField = document.getElementById('customer_postcode');
    const stateField = document.getElementById('customer_state');

    if (emailField) emailField.value = '';
    if (nameField) nameField.value = '';
    if (addressField) addressField.value = '';
    if (phoneField) phoneField.value = '';
    if (postcodeField) postcodeField.value = '';
    if (stateField) stateField.value = '';

    const flashes = document.getElementById('flashes');
    if (flashes) {
        setTimeout(function() {
            flashes.style.display = 'none';
        }, 5000);
    }
});


const favorites = JSON.parse(localStorage.getItem('favorites')) || [];
document.addEventListener('DOMContentLoaded', function () {
const hearts = document.querySelectorAll('.heart');
hearts.forEach(function (heart)   {
const productId = heart.getAttribute('data-product-id');
if (favorites.includes(productId)) {
	heart.classList.add('favorited');
}
});
console.log("Favorites initialized", favorites);
});

function toggleFavorite(element) {
const productId = element.getAttribute('data-product-id');
if (favorites.includes(productId)) {
	const index = favorites.indexOf(productId);
	if (index > -1) {
	favorites.splice(index, 1);
	console.log(`Remove from favorites product ${productId}`);
	}
	element.classList.remove('favorited');
} else {
	favorites.push(productId);
	element.classList.add('favorited');
}
	localStorage.setItem('favorites', JSON.stringify(favorites));
	console.log("Current Favorites", favorites);
}



function viewFavorites(event) {
	event.preventDefault();
	console.log('Viewed favorites ');
}

function postFavorites(event) {
if (event) { 
	event.preventDefault();
	console.log("Default navigation prevented");
}

const form = document.createElement('form');
form.method = 'POST';
form.action = '/favorites';
favorites.forEach(fav => {
	const input = document.createElement('input');
	input.type = 'hidden';
	input.name = 'favorites';
	input.value = fav;
	form.appendChild(input);
	});

	document.body.appendChild(form);
	form.submit();
}

function syncFavtoserver() {
fetch('/api/favorites', {
	method: 'POST',
	headers: {
		'Content-Type': 'application/json',
	},
	body: JSON.stringify({ favorites: favorites }),
	})
	.then(response =>  {
		if (!response.ok) {
			throw new Error(`HTTP Error status :  ${response.status}`);
		}
		return response.json();
	})
	.then(data => {
		if (data.redirect_url) {
		console.log(`Redirecting  ${data.redirect_url}`);
		window.location.href = data.redirect_url;
	} else {
		console.error("Redirect is not provided in URL");
	}
	})
	.catch(error => {
		console.log("Error Posting Favorites", error);
	});
}




// === Dynamic Product Renderer with Image Scroll and Bi-Directional Auto-Scroll ===

if (typeof productsData !== 'undefined') {

let products = productsData;
let productsPerPage = 10;
let currentIndex = 0;

function loadProducts() {
    const productList = document.getElementById('product-list');
    if (!productList) return;
    const endIndex = currentIndex + productsPerPage;

    // Eager-load only the first row of homepage cards (measured above-fold);
    // all later cards lazy. No fetchpriority here — the LCP high-priority
    // candidate is the server-rendered hero, not this grid.
    const owHomeEager = 3;

    for (let i = currentIndex; i < endIndex && i < products.length; i++) {
        const product = products[i];
        const imagePaths = product.product_image
            ? product.product_image.split(',').map(img => img.trim())
            : ['new-logo.png'];

        // Server-owned responsive <picture> via the shared renderer when the
        // route supplied primary media; else graceful raw-<img> fallback.
        let imgTrackHTML;
        if (product.media && product.media.length && window.owPictureList) {
            imgTrackHTML = window.owPictureList(product.media, {
                alt: (product.product_name || 'Optiwar Frame'),
                width: 400, height: 400,
                sizes: '(max-width:767px) 250px, 260px',
                eager: i < owHomeEager
            });
        } else {
            imgTrackHTML = imagePaths.map(img => `<img src="/static/${img}" alt="Product Image" loading="lazy">`).join('');
        }

        const imageScrollHTML = `
            <div class="img-container normal">
                ${product.product_category === "Spectacles Frame" ? `<div class="s-badge">Prescription Price Included  👁️‍🗨️  <sup>1</sup></div>` : ''}
                <div class="scrolling-image-container">
                    <div class="scrolling-track">
                        ${imgTrackHTML}
                    </div>
                </div>
            </div>`;

        const heartHTML = `
            <div class="heart" data-product-id="${product.product_id}" onclick="toggleFavorite(this)">
                <svg class="heart-outline" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M20.8 4.6a5.6 5.6 0 0 0-7.9 0L12 5.5l-.9-.9A5.6 5.6 0 0 0 3.2 12l8.8 8.8 8.8-8.8a5.6 5.6 0 0 0 0-7.9z"/>
                </svg>
                <svg class="heart-filled" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" style="display: none;">
                    <path d="M20.8 4.6a5.6 5.6 0 0 0-7.9 0L12 5.5l-.9-.9A5.6 5.6 0 0 0 3.2 12l8.8 8.8 8.8-8.8a5.6 5.6 0 0 0 0-7.9z"/>
                </svg>
            </div>`;

        const discPct = Math.round(((product.product_price - product.product_special_price) / product.product_price) * 100);
        const priceHTML = `
            <div class="ow-card-pricing">
              <div class="ow-card-market">
                <span class="ow-market-price">Market Price  ${(typeof owIsEur !== 'undefined' && owIsEur && product.product_price_eur) ? '€' + product.product_price_eur : 'Rs ' + product.product_price}</span>
                <span class="ow-card-facefit-badge" data-pid="${product.product_id}">FACE FIT. STAY SHARP.</span>
              </div>
              <div class="ow-card-wholesale">
                <span class="ow-wh-label">Wholesale Price</span>
                <span class="ow-wh-price">${(typeof owIsEur !== 'undefined' && owIsEur && product.product_special_price_eur) ? '€' + product.product_special_price_eur : 'Rs ' + product.product_special_price}</span>
              </div>

              <div class="ow-card-includes">
                <span class="ow-inc-icon"><svg viewBox="0 0 24 24"><path d="M12 4.5C7 4.5 2.7 7.6 1 12c1.7 4.4 6 7.5 11 7.5s9.3-3.1 11-7.5c-1.7-4.4-6-7.5-11-7.5zm0 12.5c-2.8 0-5-2.2-5-5s2.2-5 5-5 5 2.2 5 5-2.2 5-5 5zm0-8c-1.7 0-3 1.3-3 3s1.3 3 3 3 3-1.3 3-3-1.3-3-3-3z"/></svg></span>
                <span class="ow-inc-text">INCLUDES FREE <span>| Plano Anti-Glare Lenses ✨</span></span>
              </div>
              <div class="ow-card-shipping">
                <svg viewBox="0 0 24 24"><path d="M20 8h-3V4H3c-1.1 0-2 .9-2 2v11h2c0 1.7 1.3 3 3 3s3-1.3 3-3h6c0 1.7 1.3 3 3 3s3-1.3 3-3h2v-5l-3-4zM6 18.5c-.8 0-1.5-.7-1.5-1.5s.7-1.5 1.5-1.5 1.5.7 1.5 1.5-.7 1.5-1.5 1.5zm13.5-9l1.96 2.5H17V9.5h2.5zm-1.5 9c-.8 0-1.5-.7-1.5-1.5s.7-1.5 1.5-1.5 1.5.7 1.5 1.5-.7 1.5-1.5 1.5z"/></svg>
                Free Shipping
              </div>
            </div>`;
        const commonInputs = `
            <input type="hidden" name="product_id" value="${product.product_id}">
            <input type="hidden" name="product_code" value="${product.product_code}">
            <input type="hidden" name="product_name" value="${product.product_name}">
            <input type="hidden" name="product_price" value="${product.product_price}">
            <input type="hidden" name="product_special_price" value="${product.product_special_price}">
            <input type="hidden" name="product_category" value="${product.product_category}">`;

        let formHTML = "";
        if (product.product_category === "Hearing Aids") {
            formHTML = `
                <details style="color: purple;">
                    <summary>Hearing Aid Details</summary>
                    <span>${product.form_factor}</span><br>
                    <span>${product.warranty} Warranty</span><br>
                    <span>${product.fitting_range} dB Fitting Range</span><br>
                    <span>${product.channels} Channels</span>
                </details>
                <form action="/add_to_cart" method="post">
                    ${commonInputs}
                    <button type="submit">Fast Checkout Now</button>
                </form>`;
        } else if (product.product_category === "Contact Lenses") {
            formHTML = `
                <form action="/cl_range/add_prescription_of_cl" method="post">
                    ${commonInputs}
                    <button type="submit">Buy Contact Lenses</button>
                </form>`;
        } else if (product.product_category === "Spectacles Frame") {
            formHTML = `
                <div class="ow-btn-stack">
                <form action="/add_to_cart" method="post">
                    ${commonInputs}
                    <button type="submit" class="ow-lens-btn ow-plano">
                      <span class="ow-spark"></span><span class="ow-spark"></span><span class="ow-spark"></span>
                      <div class="ow-btn-left"><div class="ow-btn-icon">✨</div><div class="ow-btn-text"><h3>Buy Frame with Plano Anti-Glare</h3><p>Zero power lenses with premium anti-glare coating</p></div></div>
                      <div class="ow-btn-arrow">→</div>
                    </button>
                </form>
                <form action="/add_prescription" method="post">
                    ${commonInputs}
                    <button type="submit" class="ow-lens-btn ow-special">
                      <span class="ow-spark"></span><span class="ow-spark"></span><span class="ow-spark"></span>
                      <div class="ow-btn-left"><div class="ow-btn-icon">👓</div><div class="ow-btn-text"><h3>Buy Frame with Special Made Lenses</h3><p>Prescription, blue-cut & progressive lens options</p></div></div>
                      <div class="ow-btn-arrow">→</div>
                    </button>
                </form>
                </div>`;
        }

        const productItem = `
            <li>
		<a href="${typeof owEuPrefix !== 'undefined' ? owEuPrefix : ''}/categories/${product.product_category.toLowerCase().replace(/\s+/g, '-')}/${product.product_slug}?pid=${product.product_id}" class="product_link">
             ${imageScrollHTML}
 		</a>
                <div class="ow-title-row"><a href="${typeof owEuPrefix !== 'undefined' ? owEuPrefix : ''}/categories/${product.product_category.toLowerCase().replace(/\s+/g, '-')}/${product.product_slug}?pid=${product.product_id}" class="product_link"><h3>${product.product_name}</h3></a>${heartHTML}${discPct > 0 ? `<div class="ow-card-discount ow-disc-inline"><span class="ow-disc-particle ow-disc-p1"></span><span class="ow-disc-particle ow-disc-p2"></span><span class="ow-disc-particle ow-disc-p3"></span><span class="ow-disc-particle ow-disc-p4"></span><div class="ow-disc-glow"></div><div class="ow-disc-badge"><div class="ow-disc-shimmer"></div><div class="ow-disc-ring ow-disc-ring1"></div><div class="ow-disc-ring ow-disc-ring2"></div><div class="ow-disc-content"><span class="ow-disc-num">${discPct}%</span><span class="ow-disc-off">OFF</span></div><div class="ow-disc-blight"></div></div></div>` : ""}</div>
                ${product.product_color ? `<h5>Color: ${product.product_color}</h5>` : ''}
                ${product.product_size ? `<h5>Size: ${product.product_size} mm</h5>` : ''}
                ${product.product_perception_value ? `<h6 class="center-text">Face fit: ${product.product_perception_value}</h6>` : ''}
                ${priceHTML}
                ${formHTML}
            </li>`;

        productList.insertAdjacentHTML('beforeend', productItem);
    }

    currentIndex = endIndex;
    enableGalleryOnTracks();
    observeLastProduct();
}

document.addEventListener('DOMContentLoaded', () => {
    loadProducts();
});

let observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting && currentIndex < products.length) {
            loadProducts();
        }
    });
}, { rootMargin: '200px' });

function observeLastProduct() {
    const items = document.querySelectorAll('#product-list > li');
    if (items.length > 0) {
        observer.disconnect();
        observer.observe(items[items.length - 1]);
    }
}

} // end if productsData

/* ---------------------------------------------------------------------------
 * Multi-angle product-card gallery (homepage grid, favourites, category JS
 * cards, ...). Defined GLOBALLY (outside the productsData guard) so every page
 * initialises existing containers on load, and loadProducts() re-runs it for
 * infinite-scroll cards.
 *
 * Two independent layers:
 *   1. MANUAL navigation — drag, horizontal wheel/trackpad, arrow keys and
 *      native horizontal scroll. Works ALWAYS, including under
 *      prefers-reduced-motion (so a reduced-motion desktop user is never
 *      frozen on image 1).
 *   2. AUTOPLAY — a gentle 4s continuous loop, visible cards only
 *      (IntersectionObserver, so off-screen products never run timers),
 *      paused on hover/focus, and permanently handed to the user the moment
 *      they interact (drag/wheel/key/touch).
 *
 * All movement is native scrollLeft (no translateX), so the card is genuinely
 * swipeable/scrollable on every device. The all_frames carousel manages its
 * own dots-based navigation and is skipped.
 * ------------------------------------------------------------------------- */
function enableGalleryOnTracks() {
    var isMobile = window.matchMedia('(max-width: 767px)').matches;

    document.querySelectorAll('.scrolling-image-container').forEach(function (container) {
        if (container.dataset.owGallery) return;
        if (container.closest('#frames-list')) return; // all_frames = own carousel
        var track = container.querySelector('.scrolling-track');
        if (!track) return;
        // <picture> uses display:contents, so the flex children are the <img>s.
        var slides = track.querySelectorAll('img');
        if (slides.length <= 1) return;
        container.dataset.owGallery = '1';
        container.classList.add('ow-shop-gallery');
        // Kill native drag-and-drop on the images and the wrapping product link
        // so a mouse drag produces pointermove (drag-to-scroll) instead of a DnD
        // ghost that swallows the gesture.
        for (var s = 0; s < slides.length; s++) slides[s].draggable = false;
        var ownerLink = container.closest('a');
        if (ownerLink) ownerLink.draggable = false;

        function step() {
            var w = slides[0].getBoundingClientRect().width;
            var cs = getComputedStyle(track);
            var gap = parseFloat(cs.columnGap || cs.gap || '0') || 0;
            return (w + gap) || 1;
        }
        function nearestIndex() { return Math.round(container.scrollLeft / step()); }
        function goTo(i) { container.scrollTo({ left: i * step(), behavior: 'smooth' }); }

        var idx = 0, timer = null, stopped = false, paused = false;
        function advance() {
            if (paused || stopped) return;
            idx = (nearestIndex() + 1) % slides.length; // continuous loop
            goTo(idx);
        }
        // Autoplay runs regardless of prefers-reduced-motion (explicit owner
        // decision so the multi-angle preview is always visible). Manual
        // navigation still works, and any interaction stops it for that card.
        function start() { if (!timer && !stopped) timer = setInterval(advance, 4000); }
        function stop() { if (timer) { clearInterval(timer); timer = null; } }
        function userTookOver() { stopped = true; stop(); } // manual control wins

        // --- Manual: keyboard (always available) ---
        if (!container.hasAttribute('tabindex')) container.setAttribute('tabindex', '0');
        container.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowRight') { e.preventDefault(); userTookOver(); goTo(Math.min(slides.length - 1, nearestIndex() + 1)); }
            else if (e.key === 'ArrowLeft') { e.preventDefault(); userTookOver(); goTo(Math.max(0, nearestIndex() - 1)); }
        });

        // --- Manual: horizontal wheel / shift-wheel (trackpad) ---
        container.addEventListener('wheel', function (e) {
            var dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : (e.shiftKey ? e.deltaY : 0);
            if (dx) { userTookOver(); container.scrollLeft += dx; e.preventDefault(); }
        }, { passive: false });

        // --- Manual: mouse drag-to-scroll (desktop). Document-level listeners
        // (no pointer capture) + scroll-snap disabled mid-drag so the browser
        // does not fight the drag; native anchor/image drag cancelled. ---
        container.addEventListener('dragstart', function (e) { e.preventDefault(); });
        var dragging = false, startX = 0, startScroll = 0, moved = false;
        function onMove(e) {
            if (!dragging) return;
            var d = e.clientX - startX;
            if (Math.abs(d) > 6) moved = true;
            container.scrollLeft = startScroll - d;
            e.preventDefault();
        }
        function onUp() {
            if (!dragging) return;
            dragging = false;
            container.classList.remove('ow-dragging');
            document.removeEventListener('pointermove', onMove, true);
            document.removeEventListener('pointerup', onUp, true);
            userTookOver();
        }
        container.addEventListener('pointerdown', function (e) {
            if (e.pointerType === 'touch') return; // touch = native scroll
            if (e.button != null && e.button !== 0) return;
            dragging = true; moved = false;
            startX = e.clientX; startScroll = container.scrollLeft;
            container.classList.add('ow-dragging');
            document.addEventListener('pointermove', onMove, true);
            document.addEventListener('pointerup', onUp, true);
        });
        // A drag must not follow the card's product link / open zoom.
        container.addEventListener('click', function (e) {
            if (moved) { e.preventDefault(); e.stopPropagation(); moved = false; }
        }, true);

        // --- Autoplay pause/stop hooks ---
        container.addEventListener('mouseenter', function () { paused = true; });
        container.addEventListener('mouseleave', function () { paused = false; });
        container.addEventListener('focusin', function () { paused = true; });
        container.addEventListener('focusout', function () { paused = false; });
        if (isMobile) {
            container.addEventListener('touchstart', function () { userTookOver(); }, { passive: true });
        }

        // --- Autoplay only for visible cards ---
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) { if (e.isIntersecting) start(); else stop(); });
        }, { threshold: 0.5 });
        io.observe(container);
    });
}
// Back-compat alias (older call sites / cached templates).
function enableAutoScrollOnTracks() { enableGalleryOnTracks(); }
// Initialise galleries on every page, not just those with productsData.
document.addEventListener('DOMContentLoaded', enableGalleryOnTracks);

/* Single shared zoom modal (#owZoomModal in base.html). Legacy per-card
 * #zoomModal duplicates are gone, so getElementById is now unambiguous. */
var owZoomTrigger = null;   // element to restore focus to on close
var owZoomBound = false;    // backdrop/Escape/close/nav listeners bound once
var owZoomImgs = [];        // sibling gallery images the user can page through
var owZoomIdx = 0;          // current index within owZoomImgs

// Collect the gallery images that belong with the clicked image so the modal
// can page prev/next without closing. Falls back to just the clicked image.
function owCollectZoomImgs(imgElement) {
    // PDP: the hero image pages through the thumbnail strip (data-full masters).
    if (imgElement.id === 'pdpMain' || imgElement.closest('.pdp-hero')) {
        var strip = document.querySelector('.pdp-thumbstrip');
        if (strip) {
            var timgs = [].slice.call(strip.querySelectorAll('img'));
            if (timgs.length > 1) return timgs;
        }
    }
    var scope = imgElement.closest('.scrolling-track') ||
                imgElement.closest('.scrolling-image-container');
    var list = scope ? [].slice.call(scope.querySelectorAll('img')) : [imgElement];
    if (list.indexOf(imgElement) === -1) list = [imgElement];
    return list;
}

// Which image to open on: the clicked one, else the PDP's active thumb, else 0.
function owInitialZoomIdx(imgElement, list) {
    var i = list.indexOf(imgElement);
    if (i >= 0) return i;
    for (var j = 0; j < list.length; j++) {
        if (list[j].classList.contains('active') || list[j].getAttribute('aria-current') === 'true') return j;
    }
    return 0;
}

function owZoomSrc(el) {
    return el.dataset.zoom || el.dataset.full || el.currentSrc || el.src;
}

// Show image at index i (wraps around) inside the open modal.
function owZoomShow(i) {
    var img = document.getElementById('owZoomImg');
    if (!img || !owZoomImgs.length) return;
    var n = owZoomImgs.length;
    owZoomIdx = ((i % n) + n) % n;
    var el = owZoomImgs[owZoomIdx];
    img.src = owZoomSrc(el);
    img.alt = el.alt || '';
    var modal = document.getElementById('owZoomModal');
    if (modal) modal.classList.toggle('ow-zoom-multi', n > 1);
}

function owZoomNext() { owZoomShow(owZoomIdx + 1); }
function owZoomPrev() { owZoomShow(owZoomIdx - 1); }

function owEnsureZoomBound() {
    if (owZoomBound) return;
    var modal = document.getElementById('owZoomModal');
    if (!modal) return;
    owZoomBound = true;
    // Backdrop click closes; clicks on the image / nav arrows do not.
    modal.addEventListener('click', function (e) {
        var t = e.target;
        if (t.classList && t.classList.contains('ow-zoom-next')) { e.stopPropagation(); owZoomNext(); return; }
        if (t.classList && t.classList.contains('ow-zoom-prev')) { e.stopPropagation(); owZoomPrev(); return; }
        if (t === modal || (t.classList && t.classList.contains('ow-zoom-close'))) closeZoom();
    });
    document.addEventListener('keydown', function (e) {
        if (!modal.classList.contains('open')) return;
        if (e.key === 'Escape') closeZoom();
        else if (e.key === 'ArrowRight') { e.preventDefault(); owZoomNext(); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); owZoomPrev(); }
    });
    // Mobile: horizontal swipe on the zoomed image pages prev/next.
    var tsx = 0, tsy = 0, tracking = false;
    modal.addEventListener('touchstart', function (e) {
        var p = e.touches && e.touches[0]; if (!p) return;
        tsx = p.clientX; tsy = p.clientY; tracking = true;
    }, { passive: true });
    modal.addEventListener('touchend', function (e) {
        if (!tracking) return; tracking = false;
        var p = e.changedTouches && e.changedTouches[0]; if (!p) return;
        var dx = p.clientX - tsx, dy = p.clientY - tsy;
        if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) {
            if (dx < 0) owZoomNext(); else owZoomPrev();
        }
    }, { passive: true });
}

function openZoom(imgElement) {
    // A swipe/drag on the gallery must not open the modal.
    if (window.__owSuppressZoom) { window.__owSuppressZoom = false; return; }
    var modal = document.getElementById('owZoomModal');
    var img = document.getElementById('owZoomImg');
    if (!modal || !img) return;
    owEnsureZoomBound();
    owZoomImgs = owCollectZoomImgs(imgElement);
    owZoomShow(owInitialZoomIdx(imgElement, owZoomImgs));
    owZoomTrigger = imgElement;
    modal.classList.add('open');
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    var btn = modal.querySelector('.ow-zoom-close');
    if (btn) btn.focus();
}

function closeZoom() {
    var modal = document.getElementById('owZoomModal');
    if (!modal) return;
    modal.classList.remove('open');
    modal.style.display = 'none';
    document.body.style.overflow = '';
    if (owZoomTrigger && typeof owZoomTrigger.focus === 'function') {
        try { owZoomTrigger.focus(); } catch (e) {}
    }
    owZoomTrigger = null;
    owZoomImgs = [];
    owZoomIdx = 0;
    modal.classList.remove('ow-zoom-multi');
}

/* Distinguish a swipe/drag on a gallery from a deliberate tap. A horizontal
 * (or any >10px) drag sets __owSuppressZoom so the follow-up click does not
 * open the zoom modal. Runs on every page (all_frames, favorites, home, ...). */
(function () {
    var sx = 0, sy = 0;
    function point(e) { return e.touches && e.touches[0] ? e.touches[0] : e; }
    function inGallery(t) { return t && t.closest && t.closest('.scrolling-image-container'); }
    function down(e) {
        if (!inGallery(e.target)) return;
        var p = point(e);
        sx = p.clientX; sy = p.clientY;
        window.__owSuppressZoom = false;
    }
    function move(e) {
        if (!inGallery(e.target)) return;
        var p = point(e);
        if (Math.abs(p.clientX - sx) > 10 || Math.abs(p.clientY - sy) > 10) {
            window.__owSuppressZoom = true;
        }
    }
    document.addEventListener('pointerdown', down, true);
    document.addEventListener('pointermove', move, true);
    document.addEventListener('touchstart', down, true);
    document.addEventListener('touchmove', move, true);
})();


function copyToClipboard(text) {
    navigator.clipboard.writeText(text);
}
