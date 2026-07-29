/* Shared client-side <picture> renderer.
 * Consumes a server-built media object (from build_media in embed_helper.py):
 *   { src, zoom, has_derivatives, avif, webp, jpg }
 * where avif/webp/jpg are width-descriptor srcset strings.
 * No derivative filenames are ever reconstructed here — the server is the
 * single source of truth. Used by all_frames buildCard, search
 * buildProductCard, and any other JS card surface.
 *
 * owPictureHTML(media, opts) -> HTML string
 *   opts: { sizes, width, height, cls, alt, eager, priority, zoom }
 *   - eager=false (default) -> loading="lazy"
 *   - eager=true            -> loading="eager"
 *   - eager && priority     -> adds fetchpriority="high" (LCP candidate only)
 *   - zoom=true             -> adds the fixed onclick="openZoom(this)" handler
 * There is intentionally NO generic attribute passthrough: every attribute is
 * a fixed, macro-controlled token, and all caller-supplied text (alt, cls) is
 * HTML-attribute-escaped, so hostile input cannot break out of the attribute.
 * src/srcset come from the server-built (trusted) media contract.
 */
(function () {
  function attr(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  window.owPictureHTML = function (media, opts) {
    opts = opts || {};
    if (!media || !media.src) return '';
    function num(v, d) { var n = parseInt(v, 10); return (isFinite(n) && n > 0) ? n : d; }
    var sizes = opts.sizes || '';
    var w = num(opts.width, 400);
    var h = num(opts.height, 400);
    var cls = opts.cls ? ' class="' + attr(opts.cls) + '"' : '';
    var alt = attr(opts.alt);
    var loading = opts.eager ? 'eager' : 'lazy';
    var prio = (opts.eager && opts.priority) ? ' fetchpriority="high"' : '';
    var zoom = opts.zoom ? ' onclick="openZoom(this)"' : '';
    var src = attr(media.src);
    var zoomUrl = attr(media.zoom || media.src);

    var img = '<img src="' + src + '" data-zoom="' + zoomUrl + '"'
      + ' alt="' + alt + '"' + cls + ' width="' + w + '" height="' + h + '"'
      + ' loading="' + loading + '"' + prio + ' decoding="async"' + zoom + '>';

    if (media.has_derivatives && media.avif) {
      return '<picture>'
        + '<source type="image/avif" sizes="' + attr(sizes) + '" srcset="' + attr(media.avif) + '">'
        + '<source type="image/webp" sizes="' + attr(sizes) + '" srcset="' + attr(media.webp) + '">'
        + img
        + '</picture>';
    }
    return img;
  };

  /* Render every media object in a product's media[] list (angles).
   * Only the FIRST angle may be eager/high-priority; the remaining angles are
   * off-screen in the strip, so they are always lazy (keeps eager loading to
   * above-fold and avoids many eager requests per card). */
  window.owPictureList = function (mediaList, opts) {
    if (!mediaList || !mediaList.length) return '';
    opts = opts || {};
    var out = '';
    for (var i = 0; i < mediaList.length; i++) {
      var o = opts;
      if (i > 0 && (opts.eager || opts.priority)) {
        o = {};
        for (var k in opts) { if (opts.hasOwnProperty(k)) o[k] = opts[k]; }
        o.eager = false;
        o.priority = false;
      }
      out += window.owPictureHTML(mediaList[i], o);
    }
    return out;
  };
})();
