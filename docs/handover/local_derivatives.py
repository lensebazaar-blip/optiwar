#!/usr/bin/env python3
"""Local-only: build the responsive ladder (AVIF + WebP at embed_helper widths)
for every catalog master under repo/static/catalog/opticalbazaar, trimming the
blank canvas around the frame first (production's derivatives are trimmed; the
server-side generator is not in the repo). Never run on production. Idempotent."""
import os, sys, time
from multiprocessing import Pool
from PIL import Image, ImageChops

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'repo', 'static', 'catalog', 'opticalbazaar')
WIDTHS = (200, 400, 800, 1200, 2000)
WHITE = (255, 255, 255)

def trim(img, thresh=238, margin=0.05):
    g = img.convert('L').point(lambda v: 255 if v < thresh else 0)
    box = g.getbbox()
    if not box:
        return img
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w < img.size[0] * 0.08 or h < img.size[1] * 0.04:   # nothing sensible found
        return img
    mx, my = int(w * margin), int(h * margin)
    # pad to at least a 4:3 canvas so narrow trims do not look stretched in cards
    x0, y0 = max(0, x0 - mx), max(0, y0 - my)
    x1, y1 = min(img.size[0], x1 + mx), min(img.size[1], y1 + my)
    cut = img.crop((x0, y0, x1, y1))
    cw, ch = cut.size
    want_h = int(cw * 0.75)
    if ch < want_h:
        canvas = Image.new('RGB', (cw, want_h), WHITE); canvas.paste(cut, (0, (want_h - ch) // 2)); cut = canvas
    return cut

def one(path):
    d, name = os.path.split(path); stem = os.path.splitext(name)[0]
    out = os.path.join(d, 'derivatives'); os.makedirs(out, exist_ok=True)
    targets = [(os.path.join(out, '%s-%d.%s' % (stem, w, f)), w, f) for w in WIDTHS for f in ('avif', 'webp', 'jpg')]
    if all(os.path.exists(t[0]) for t in targets):
        return 0
    try:
        img = Image.open(path); img.load(); img = img.convert('RGB')
    except Exception as e:
        return 'bad %s: %s' % (path, e)
    cut = trim(img)
    n = 0
    for dst, w, f in targets:
        if os.path.exists(dst):
            continue
        scale = w / float(cut.size[0])
        im = cut.resize((w, max(1, int(round(cut.size[1] * scale)))), Image.LANCZOS)
        tmp = dst + '.tmp'
        if f == 'avif':
            im.save(tmp, 'AVIF', quality=58, speed=8)
        elif f == 'webp':
            im.save(tmp, 'WEBP', quality=78, method=4)
        else:
            im.save(tmp, 'JPEG', quality=82, optimize=True)
        os.replace(tmp, dst); n += 1
    return n

def main():
    files = []
    for code in sorted(os.listdir(ROOT)):
        d = os.path.join(ROOT, code)
        if not os.path.isdir(d) or code == 'derivatives':
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith(('.jpg', '.jpeg', '.png')) and os.path.isfile(os.path.join(d, name)):
                files.append(os.path.join(d, name))
    print('masters:', len(files), flush=True)
    t = time.time(); done = 0; written = 0; errors = 0
    with Pool(max(2, (os.cpu_count() or 4) - 1)) as pool:
        for r in pool.imap_unordered(one, files, chunksize=8):
            done += 1
            if isinstance(r, str):
                errors += 1; print(r, flush=True)
            else:
                written += r
            if done % 250 == 0:
                print('%d/%d masters, %d files written, %d errors, %.0fs' % (done, len(files), written, errors, time.time() - t), flush=True)
    print('DONE %d masters, %d files written, %d errors, %.0fs' % (done, written, errors, time.time() - t), flush=True)

if __name__ == '__main__':
    main()
