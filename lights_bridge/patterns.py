"""Colour helpers and the software patterns (computed here, sent to the light frame by frame)."""
import colorsys
import math
import random


def hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad colour {h!r}")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hsv(h, s=1.0, v=1.0):
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h % 1.0, s, v))


def scale(rgb, k):
    return tuple(max(0, min(255, round(c * k))) for c in rgb)


def mix(a, b, k):
    return tuple(round(a[i] + (b[i] - a[i]) * k) for i in range(3))


def palette(stops, x):
    """Colour at position x (0..1, wraps) along a looping list of RGB stops."""
    x = (x % 1.0) * len(stops)
    i = int(x)
    return mix(stops[i % len(stops)], stops[(i + 1) % len(stops)], x - i)


SUNSET = [(255, 94, 0), (255, 0, 80), (120, 0, 200), (255, 0, 80)]
OCEAN = [(0, 40, 255), (0, 200, 180), (0, 90, 255), (0, 255, 120)]
AURORA = [(0, 255, 90), (0, 180, 255), (150, 0, 255), (0, 255, 200)]
FOREST = [(20, 255, 0), (160, 255, 0), (0, 140, 30), (255, 200, 0)]

# id -> (label, f(t, i, n, base_rgb) -> rgb for segment i of n at phase t)
SOFT_PATTERNS = {
    "rainbow": ("Rainbow", lambda t, i, n, c: hsv(t + i / max(n, 1))),
    "sunset": ("Sunset", lambda t, i, n, c: palette(SUNSET, t + i / (2 * n))),
    "ocean": ("Ocean", lambda t, i, n, c: palette(OCEAN, t + i / (2 * n))),
    "aurora": ("Aurora", lambda t, i, n, c: palette(AURORA, t + i / (2 * n))),
    "forest": ("Forest", lambda t, i, n, c: palette(FOREST, t + i / (2 * n))),
    "breathe": ("Breathe", lambda t, i, n, c: scale(c, 0.3 + 0.7 * (0.5 - 0.5 * math.cos(2 * math.pi * t * 4)))),
    "candle": ("Candle", lambda t, i, n, c: scale((255, 120, 20), random.uniform(0.45, 1.0))),
    "fire": ("Fire", lambda t, i, n, c: mix((255, 20, 0), (255, 170, 0), random.random() * (1 - i / max(n, 1)))),
    "party": ("Party", lambda t, i, n, c: hsv(random.random())),
}
