"""Generic change patterns. Each returns a per-minute delta series; none knows about scenarios."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Series = NDArray[np.float64]

PATTERNS = ("linear_ramp", "exponential_ramp", "step", "spike", "drift", "noise")


@dataclass(frozen=True)
class PatternSpec:
    """Parameters of one pattern, in minutes from the start of the run."""

    kind: str
    start_minute: int = 0
    duration_minutes: int | None = None
    magnitude: float = 0.0
    shape: float = 4.0
    width_minutes: int = 30
    period_minutes: int | None = None
    sigma: float = 0.0
    hold: bool = True


def _window(n: int, spec: PatternSpec) -> tuple[int, int]:
    start = min(spec.start_minute, n)
    end = n if spec.duration_minutes is None else min(n, start + spec.duration_minutes)
    return start, end


def _progress(n: int, start: int, end: int, hold: bool) -> Series:
    """0 before the window, 0 to 1 across it, then 1 (held) or 0 after it."""
    out = np.zeros(n)
    out[start:end] = np.linspace(0.0, 1.0, end - start)
    if hold:
        out[end:] = 1.0 if end > start else 0.0
    return out


def _spike(n: int, spec: PatternSpec, start: int, end: int) -> Series:
    out = np.zeros(n)
    width = max(spec.width_minutes, 1)
    step = spec.period_minutes if spec.period_minutes else n + 1
    for onset in range(start, end, max(step, 1)):
        stop = min(onset + width, end)
        out[onset:stop] = np.sin(np.pi * np.arange(stop - onset) / width)
    return out * spec.magnitude


def pattern_delta(spec: PatternSpec, n: int, rng: np.random.Generator) -> Series:
    """Return the change this pattern adds to a series of n minutes."""
    start, end = _window(n, spec)
    out = np.zeros(n)
    if spec.kind == "linear_ramp":
        out = _progress(n, start, end, spec.hold) * spec.magnitude
    elif spec.kind == "exponential_ramp":
        x = _progress(n, start, end, spec.hold)
        out = np.expm1(spec.shape * x) / np.expm1(spec.shape) * spec.magnitude
    elif spec.kind == "step":
        out[start:end] = spec.magnitude
    elif spec.kind == "spike":
        out = _spike(n, spec, start, end)
    elif spec.kind == "drift":
        walk = np.cumsum(rng.standard_normal(end - start))
        peak = float(np.max(np.abs(walk))) if walk.size else 0.0
        if peak > 0:
            out[start:end] = walk / peak * spec.magnitude
            if spec.hold:
                out[end:] = out[end - 1]
    elif spec.kind == "noise":
        out[start:end] = rng.normal(0.0, spec.sigma, end - start)
    else:
        raise ValueError(f"unknown pattern {spec.kind!r}; expected one of {PATTERNS}")
    return out
