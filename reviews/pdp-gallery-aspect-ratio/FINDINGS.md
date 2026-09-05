# Gallery hero renders as a tiny strip in a mostly-empty square — real-photo/frame mismatch

Screenshot on file (desktop, `optiwar.com/categories/contact-lenses/
precision1-daily-disposable-contact-lenses--30-pack`): the gallery's main
image area is a large near-square white box with the product photo rendered
small and centered, most of the frame empty. Not related to PR #83/#86/the
declutter fix (those all check out correctly in the same screenshot — BC/DIA
renders once, the per-eye header total is gone) — this is a separate,
previously-unflagged issue in the gallery itself.

## Root cause

`templates/product_page_lens.html`:
```css
.lpdp-hero { ... aspect-ratio:1 / 1; ... }
.lpdp-hero img { width:100%; height:100%; object-fit:contain; ... }
```
The frame is forced **square**. The actual product photography is not —
measured the five real files directly:

| File | Aspect ratio |
|---|---|
| front (hero) | 3.07 : 1 |
| side, usage icons | 4.82 : 1 |
| side, material/count | 1.60 : 1 |
| end panel | 5.14 : 1 |

These are wide panoramic box-front/side crops. Feeding a 3:1–5:1 image into a
1:1 frame with `object-fit:contain` scales it down until it fits the width,
leaving the frame's height mostly empty — exactly the screenshot. This isn't
a logic bug (`contain` is doing exactly what it's told); it's a frame shape
that doesn't match the actual photography.

## Fix, verified against the real files

Two changes, same idea I applied to the LensBazaar reference using these
same source images:

1. Change `.lpdp-hero`'s aspect ratio from `1/1` to something the photography
   actually fits — around `2.4/1` reads well across all four images without
   excessive letterboxing on the widest ones (5.14:1) or excessive cropping
   on the narrowest (1.60:1). Worth the owner eyeballing 2–3 ratio options
   against the real photo set rather than taking this number as final.
2. Switch `object-fit` from `contain` to `cover` (with `object-position:
   center`) so the frame fills edge-to-edge instead of letterboxing — standard
   retail-photography treatment, and these are flat-color box panels with
   centered text/logo, so a small amount of edge cropping from `cover` loses
   nothing meaningful.
3. Apply the same two changes to the thumbnail strip (`.lpdp-thumbs img`) —
   same mismatch, smaller and less noticeable but present there too.

## What this doesn't touch

Nothing in `_lens_eye_cards.html` or the purchase flow — this is gallery CSS
only, in the same file the declutter fix already touched
(`product_page_lens.html`), no new files.
