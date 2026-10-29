"""Extract a 96x96 palette-indexed portrait plus its animation variants from the reference image.

Emits app/assets/lea-portrait.js: an RLE pixel map over a per-class palette, so a palette
index also tells the renderer which material class (skin, hair, neon circuit, ...) a cell is.
"""
import json
import os

import numpy as np
from PIL import Image

SRC = os.environ.get('LEA_REFERENCE', 'docs/reference/lea-portrait.jpg')
OUT = 'app/assets/lea-portrait.js'
W = 96
CROWN = 9                        # rows added on top: the reference crops the top of her head
H = W + CROWN

VOID, DARK, SKIN, HAIR, WARM, CIRCUIT, IMPLANT, EYE, LIP, BACK = range(10)
CLASS_NAMES = ['void', 'dark', 'skin', 'hair', 'warm', 'circuit', 'implant', 'eye', 'lip', 'back']
BUDGET = {VOID: 1, DARK: 7, SKIN: 13, HAIR: 15, WARM: 9, CIRCUIT: 10, IMPLANT: 8, EYE: 11, LIP: 7, BACK: 8}

# All vertical coordinates below are in the padded canvas, so they carry the CROWN offset.
IMPLANT_C = (35.0, 39.0 + CROWN)
IMPLANT_R = 14.0
EYE_BOX = (55, 34 + CROWN, 16, 9)        # x, y, w, h -- the lid aperture; the brow above stays put
MOUTH_BOX = (44, 59 + CROWN, 13, 11)     # tall enough for the jaw to drop into


def hair_edges(source, rows=6):
    """Left and right edge of the hair mass for the first `rows` rows of the artwork."""
    lum = source.max(axis=2)
    edges = []
    for y in range(rows):
        lit = np.nonzero(lum[y] >= 10)[0]
        lit = lit[lit > 56]                             # right of the HUD arc and the bangs
        edges.append((lit.min(), lit.max()))
    return edges


def rebuild_crown(source):
    """The reference frames her so the top of her skull is cut off by the border. Close it.

    Both edges of the hair are still converging at row 0, so the cap is the circular arc that
    meets them tangentially: that fixes its height rather than leaving it to taste, and keeps
    the crown a dome instead of the spike a straight continuation would give.
    """
    out = np.zeros((H, W, 3), dtype=float)
    out[CROWN:] = source

    edges = hair_edges(source)
    left, right = edges[0]
    half = (right - left) / 2 + .5
    centre = (left + right) / 2
    # How fast each edge closes per row, averaged over the rows we can actually see.
    closing = np.mean([(edges[y][0] - edges[y + 1][0]) + (edges[y + 1][1] - edges[y][1])
                       for y in range(len(edges) - 1)]) / 2
    # Circular cap of radius R whose base half-width is `half` and whose base slope is `closing`.
    rise = half * (np.sqrt(closing ** 2 + 1) - closing)
    radius = (half ** 2 + rise ** 2) / (2 * rise)
    apex = CROWN - rise

    row = source[0]
    for y in range(max(0, int(np.floor(apex))), CROWN):
        depth = y - apex                                # 0 at the apex, `rise` at the old row 0
        if depth <= 0:
            continue
        width = np.sqrt(max(0.0, 2 * radius * depth - depth ** 2))
        lean = .3 * (CROWN - y)                         # her head tilts, so the centre drifts
        shade = .74 + .26 * (depth / rise)              # the crown turns away from the rim light
        for x in range(int(round(centre + lean - width)), int(round(centre + lean + width)) + 1):
            u = (x - centre - lean) / max(width, 1e-6)
            out[y, x] = row[int(round(np.clip(centre + u * half, left, right)))] * shade
    return out


img = rebuild_crown(np.asarray(Image.open(SRC).convert('RGB').resize((W, W), Image.BOX)).astype(float))
R, G, B = img[..., 0], img[..., 1], img[..., 2]
LUM = img.max(axis=2)
XS = np.arange(W)[None, :].repeat(H, 0)
YS = np.arange(H)[:, None].repeat(W, 1)


def classify():
    cls = np.full((H, W), VOID, dtype=int)
    dist_implant = np.hypot(XS - IMPLANT_C[0], YS - IMPLANT_C[1])

    cyan = (G > R * 1.3) & (B > R * 1.15) & (LUM > 34)
    warm = (R > G * 1.2) & (G > B * 1.4) & (LUM > 62)
    pink = (R > G * 1.4) & (B > G * 1.02) & (LUM > 52)
    lit, bright = LUM >= 14, LUM >= 42
    subject = XS >= 38                                  # head, neck and shoulder live right of this

    cls[lit] = DARK
    cls[bright & subject & ~cyan & ~warm & ~pink] = SKIN
    cls[bright & subject & pink] = HAIR
    cls[bright & subject & warm] = WARM
    cls[cyan] = CIRCUIT
    cls[cyan & (dist_implant <= IMPLANT_R)] = IMPLANT
    cls[lit & ((XS <= 13) & (YS >= 34 + CROWN) | (XS >= 85) & (YS >= 57 + CROWN)) & ~cyan] = BACK
    cls[LUM < 14] = VOID

    skin_ref = np.median(img[(cls == SKIN) & (XS > 44) & (XS < 78)], axis=0)

    # The lid aperture: lashes, iris and eyeshadow are whatever departs from the cheek tone.
    x0, y0, w, h = EYE_BOX
    sub = img[y0:y0 + h, x0:x0 + w]
    cls[y0:y0 + h, x0:x0 + w][np.sqrt(((sub - skin_ref) ** 2).sum(axis=2)) > 48] = EYE

    # The lips: the only reddish thing on this face.
    x0, y0, w, h = MOUTH_BOX
    sub = img[y0:y0 + h, x0:x0 + w]
    cls[y0:y0 + h, x0:x0 + w][(sub[..., 0] > sub[..., 1] * 1.35) & (sub[..., 0] > 14)] = LIP
    return cls, skin_ref


def build_palette(cls):
    """One adaptive palette per class, so a palette index also carries its class."""
    palette, pal_class, lookup = [], [], {}
    for cid, budget in BUDGET.items():
        mask = cls == cid
        if not mask.any():
            continue
        pixels = img[mask].astype('uint8').reshape(-1, 1, 3)
        k = max(1, min(budget, len(np.unique(pixels.reshape(-1, 3), axis=0))))
        quant = Image.fromarray(pixels).quantize(colors=k, method=Image.MEDIANCUT, dither=Image.NONE)
        colors = np.array(quant.getpalette()[:k * 3]).reshape(k, 3)
        lookup[cid] = (len(palette), colors.astype(float))
        palette.extend(tuple(int(v) for v in c) for c in colors)
        pal_class.extend([cid] * k)
    return palette, pal_class, lookup


def index_map(cls, lookup):
    idx = np.zeros((H, W), dtype=int)
    for cid, (base, colors) in lookup.items():
        ys, xs = np.nonzero(cls == cid)
        if len(ys) == 0:
            continue
        diff = img[ys, xs][:, None, :] - colors[None, :, :]
        idx[ys, xs] = base + np.argmin((diff ** 2).sum(axis=2), axis=1)
    return idx


def nearest(color, cid, lookup):
    base, colors = lookup[cid]
    return base + int(np.argmin(((colors - np.array(color, dtype=float)) ** 2).sum(axis=1)))


def lidded_eye(idx, cls, lookup, drop):
    """Lower the upper lid by `drop` rows; the lash line follows it down."""
    x0, y0, w, h = EYE_BOX
    out = idx[y0:y0 + h, x0:x0 + w].copy()
    lid_src = idx[y0 + h, x0:x0 + w]             # untouched cheek row just below the aperture
    lash = nearest((16, 9, 18), EYE, lookup)
    for yy in range(min(drop + 1, h)):
        for xx in range(w):
            if cls[y0 + yy, x0 + xx] == EYE or yy < drop:
                out[yy, xx] = lash if yy == drop else lid_src[xx]
    if drop >= h - 2:                             # fully shut: nothing of the eye survives
        for yy in range(h):
            for xx in range(w):
                if cls[y0 + yy, x0 + xx] == EYE:
                    out[yy, xx] = lash if yy == drop else lid_src[xx]
    return out


def viseme(idx, cls, lookup, drop, spread=0):
    """One mouth shape: drop the lip run by `drop` rows to open the jaw, then stretch the
    corners out (spread > 0, as in "e") or purse them in (spread < 0, as in "o")."""
    x0, y0, w, h = MOUTH_BOX
    out = idx[y0:y0 + h, x0:x0 + w].copy()
    cavity = nearest((18, 6, 13), LIP, lookup)
    is_lip = cls[y0:y0 + h, x0:x0 + w] == LIP
    columns = {xx: np.nonzero(is_lip[:, xx])[0] for xx in range(w)}
    columns = {xx: rows for xx, rows in columns.items() if len(rows)}

    def paint(xx, rows, source_x):
        top, bottom = rows[0], rows[-1]
        for yy in range(min(bottom + drop, h - 1), top - 1, -1):
            src = yy - drop
            out[yy, xx] = idx[y0 + src, x0 + source_x] if src >= top else cavity

    for xx, rows in columns.items():
        paint(xx, rows, xx)

    corners = sorted(columns)
    left, right = corners[0], corners[-1]
    if spread > 0:                               # the smile stretches past the lip corners
        for step in range(1, spread + 1):
            for edge, inner in ((left - step, left), (right + step, right)):
                if 0 <= edge < w:
                    paint(edge, columns[inner], inner)
    elif spread < 0:                             # pursed: the corners fall back to bare skin
        for step in range(0, -spread):
            for edge in (left + step, right - step):
                rows = columns.get(edge)
                if rows is None:
                    continue
                above = max(rows[0] - 1, 0)
                for yy in range(rows[0], min(rows[-1] + drop + 1, h)):
                    out[yy, edge] = out[above, edge]
    return out


ALPHABET = ('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
            '0123456789!#$%&()*+,-./:;<=>?@[]^_{|}~')


def rle(values):
    out, prev, run = [], None, 0
    for v in list(values) + [None]:
        if v == prev and run < len(ALPHABET):
            run += 1
            continue
        if prev is not None:
            out.append(ALPHABET[prev] + ALPHABET[run - 1])
        prev, run = v, 1
    return ''.join(out)


def main():
    cls, skin_ref = classify()
    palette, pal_class, lookup = build_palette(cls)
    idx = index_map(cls, lookup)
    assert len(palette) <= len(ALPHABET), f'palette {len(palette)} exceeds alphabet'

    # Mouth shapes are visemes: index 0 is the resting artwork, the rest are driven by speech.
    # `open` is the widest a syllable goes; `gape` is reserved for surprise.
    mouth_shapes = ['ajar', 'open', 'wide', 'round', 'narrow', 'gape']
    variants = {
        'eyeHalf': lidded_eye(idx, cls, lookup, 3),
        'eyeShut': lidded_eye(idx, cls, lookup, 6),
        'mouthAjar': viseme(idx, cls, lookup, 1),
        'mouthOpen': viseme(idx, cls, lookup, 2),
        'mouthWide': viseme(idx, cls, lookup, 1, spread=2),
        'mouthRound': viseme(idx, cls, lookup, 2, spread=-2),
        'mouthNarrow': viseme(idx, cls, lookup, 1, spread=-3),
        'mouthGape': viseme(idx, cls, lookup, 4),
    }
    data = {
        'width': W,
        'height': H,
        'crown': CROWN,
        'alphabet': ALPHABET,
        'palette': ['%02x%02x%02x' % c for c in palette],
        'paletteClass': pal_class,
        'classNames': CLASS_NAMES,
        'pixels': rle(idx.ravel()),
        'implant': {'cx': IMPLANT_C[0], 'cy': IMPLANT_C[1], 'r': IMPLANT_R},
        'eye': {'box': list(EYE_BOX), 'half': rle(variants['eyeHalf'].ravel()),
                'shut': rle(variants['eyeShut'].ravel())},
        'mouth': {'box': list(MOUTH_BOX), 'shapes': mouth_shapes,
                  'frames': [rle(variants['mouth' + name.capitalize()].ravel()) for name in mouth_shapes]},
    }
    with open(OUT, 'w') as fh:
        fh.write('/* LEA portrait map: 96x96 indexed pixels traced from the reference artwork.\n'
                 '   Generated by tools/build_portrait.py -- edit that script, not this file. */\n')
        fh.write('window.LEA_PORTRAIT = ' + json.dumps(data, separators=(',', ':')) + ';\n')

    if os.environ.get('LEA_PORTRAIT_PREVIEW'):
        pal = np.array(palette, dtype=np.uint8)
        Image.fromarray(pal[idx]).resize((576, 576), Image.NEAREST).save('check_base.png')
        for name, grid in variants.items():
            preview = idx.copy()
            x0, y0, w, h = EYE_BOX if name.startswith('eye') else MOUTH_BOX
            preview[y0:y0 + h, x0:x0 + w] = grid
            Image.fromarray(pal[preview][30:72, 40:76]).resize((36 * 14, 42 * 14), Image.NEAREST).save(f'check_{name}.png')

    print('skin ref', skin_ref.round(1))
    print('classes:', {CLASS_NAMES[c]: int((cls == c).sum()) for c in range(10)})
    print('palette:', len(palette), 'entries; pixels rle:', len(data['pixels']), 'chars')
    print('wrote', OUT)


if __name__ == '__main__':
    main()
