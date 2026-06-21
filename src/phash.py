"""
phash.py — minimal perceptual hash (pHash), dependency-free beyond numpy/Pillow.

Used as a fallback when the `imagehash` package isn't installed. Produces
64-bit DCT-based perceptual hashes compatible in spirit with imagehash's
phash() (same algorithm: grayscale -> resize -> DCT -> median-threshold
top-left low-frequency block -> bitstring). Hamming distance between two
hex hashes is what the matching stage uses for similarity.

If `imagehash` is installed, fingerprint.py uses it directly instead of
this module, since it's the more battle-tested implementation. This file
exists purely so the pipeline is fully runnable/testable without network
access to pip in restricted environments.
"""

import numpy as np
from PIL import Image


def _dct_2d(a: np.ndarray) -> np.ndarray:
    """2D DCT-II via two 1D DCTs (no scipy dependency)."""
    def dct_1d(x: np.ndarray) -> np.ndarray:
        n = x.shape[-1]
        result = np.zeros_like(x, dtype=np.float64)
        factor = np.pi / n
        for k in range(n):
            coeff = np.cos(factor * (np.arange(n) + 0.5) * k)
            result[..., k] = np.sum(x * coeff, axis=-1)
        return result

    rows = dct_1d(a)
    cols = dct_1d(rows.T).T
    return cols


def phash(image: Image.Image, hash_size: int = 8, highfreq_factor: int = 4) -> str:
    """
    Compute a perceptual hash of a PIL Image, returned as a hex string.
    hash_size=8 -> 64-bit hash (16 hex chars).
    """
    img_size = hash_size * highfreq_factor
    img = image.convert("L").resize((img_size, img_size), Image.LANCZOS)
    pixels = np.asarray(img, dtype=np.float64)

    dct = _dct_2d(pixels)
    dct_low = dct[:hash_size, :hash_size]

    # Exclude the [0,0] DC term from the median (it's just average brightness)
    med = np.median(dct_low.flatten()[1:])
    diff = dct_low > med

    bits = diff.flatten()
    # Pack bits -> hex
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return format(val, f"0{hash_size * hash_size // 4}x")


def hamming_distance(hex_a: str, hex_b: str) -> int:
    """Hamming distance between two equal-length hex hash strings."""
    int_a = int(hex_a, 16)
    int_b = int(hex_b, 16)
    return bin(int_a ^ int_b).count("1")


def is_blank(image: Image.Image, std_threshold: float = 4.0) -> bool:
    """
    True if the image is near-uniform (solid black/white/color) — e.g. a
    fade-to-black transition, a blank intro/logo card, or a forced
    first-frame grab landing before a fade-in finishes.

    These collapse to a DEGENERATE pHash: the DCT of a constant signal is
    zero everywhere, so the median-threshold step (`dct_low > median`)
    compares every value against itself and is False for all of them —
    every uniform frame in the entire library hashes to the same all-zero
    bitstring, regardless of its actual color. Confirmed against real
    library data: two unrelated movies' blank intro frames matched with
    Hamming distance 0 ("identical"), producing a confident-looking but
    meaningless match. Frames this flat carry no information to match on,
    so the fix is to never fingerprint them in the first place, for
    either phash backend (real `imagehash` has the same DCT degeneracy on
    a constant image — this isn't specific to the vendored fallback).
    """
    arr = np.asarray(image.convert("L"), dtype=np.float64)
    return float(arr.std()) < std_threshold


def color_signature(image: Image.Image, bins: int = 4) -> str:
    """
    Coarse per-channel color histogram, encoded as a hex string.

    Standard pHash operates on grayscale luminance only, by design, so it
    is structurally blind to two frames that share identical composition
    (edges/shapes) but differ in color (e.g. a red-tinted scene vs a
    blue-tinted scene with the same framing). That's rare with real
    footage, which almost always has enough texture for pHash alone, but
    it's cheap to guard against: a tiny (bins^3) color histogram is
    combined with the pHash Hamming distance at matching time so a
    pHash-only collision can't produce a false positive between two
    differently-colored scenes. Not used standalone, since it would
    itself false-positive on the crop/flip-invariance properties we need.
    """
    small = image.convert("RGB").resize((32, 32), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float64) / 256.0
    idx = (arr * bins).astype(int).clip(0, bins - 1)
    flat_idx = idx[..., 0] * bins * bins + idx[..., 1] * bins + idx[..., 2]
    hist, _ = np.histogram(flat_idx, bins=bins ** 3, range=(0, bins ** 3))
    hist = hist / hist.sum()
    # Quantize to a coarse hex digest (sufficient for a distance check, not exact reconstruction)
    quantized = (hist * 15).astype(int).clip(0, 15)
    return "".join(format(q, "x") for q in quantized)


def color_distance(sig_a: str, sig_b: str) -> float:
    """Normalized L1 distance between two color signatures, 0 (identical) .. 1 (max different)."""
    a = np.array([int(c, 16) for c in sig_a], dtype=np.float64)
    b = np.array([int(c, 16) for c in sig_b], dtype=np.float64)
    return float(np.abs(a - b).sum() / (15 * len(a)))


if __name__ == "__main__":
    # Quick self-test: identical image -> distance 0; shifted image -> small distance
    import sys

    a = Image.new("RGB", (320, 180))
    px = a.load()
    for x in range(320):
        for y in range(180):
            px[x, y] = (x % 256, y % 256, (x + y) % 256)

    h1 = phash(a)
    h2 = phash(a)
    print(f"identical image hashes match: {h1 == h2} ({h1})")

    b = a.resize((160, 90)).resize((320, 180))  # lossy resize roundtrip
    h3 = phash(b)
    d = hamming_distance(h1, h3)
    print(f"resized-roundtrip distance: {d} (expect small, <10)")
    sys.exit(0 if h1 == h2 and d < 10 else 1)
