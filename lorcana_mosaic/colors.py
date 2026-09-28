"""sRGB -> CIELAB conversion, vectorised over arbitrary numpy shapes."""

from __future__ import annotations

import numpy as np

# sRGB (D65) -> XYZ
_M = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float32,
)

_WHITE = np.array([0.95047, 1.00000, 1.08883], dtype=np.float32)

_EPS = np.float32(0.008856)
_KAPPA = np.float32(7.787)


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(np.float32)
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """rgb: (..., 3) floats in 0..1 -> lab: (..., 3).

    L in 0..100, a/b roughly -128..127.
    """
    lin = srgb_to_linear(rgb)
    xyz = lin @ _M.T
    xyz = xyz / _WHITE

    f = np.where(xyz > _EPS, np.cbrt(np.maximum(xyz, 1e-12)), _KAPPA * xyz + 16.0 / 116.0)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]

    lab = np.empty_like(f)
    lab[..., 0] = 116.0 * fy - 16.0
    lab[..., 1] = 500.0 * (fx - fy)
    lab[..., 2] = 200.0 * (fy - fz)
    return lab


def image_to_lab(arr_uint8: np.ndarray) -> np.ndarray:
    """(H, W, 3) uint8 -> (H, W, 3) float32 Lab."""
    return rgb_to_lab(arr_uint8.astype(np.float32) / 255.0)
