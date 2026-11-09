"""Window functions. Each feature at time t depends only on its own series' window ending at t.

That is what makes the training path, the serving path and the point-in-time rule agree: a
feature never looks at another time step, another series, or anything after t. Every
reduction is a row-wise operation over a (times x window) matrix, so a row's result does not
depend on how many other rows are computed with it.

Missing data: gaps are NaN. A window needs at least `min_coverage` of its minutes present, or
the result is NaN (null in the table). std and slope also need two samples.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]
Index = NDArray[np.int64]
T = TypeVar("T")


class SeriesWindows:
    """Lazy, memoised window statistics for one series at a set of times."""

    def __init__(
        self, grid: Array, t_index: Index, min_coverage: float, level_minutes: int
    ) -> None:
        self.grid = grid  # one value per minute, NaN where missing
        self.t_index = t_index  # position of each evaluation time in the grid
        self.min_coverage = min_coverage
        self.level_minutes = level_minutes
        self._memo: dict[tuple[str, int], Any] = {}

    def _cached(self, key: str, window: int, make: Callable[[], T]) -> T:
        slot = (key, window)
        if slot not in self._memo:
            self._memo[slot] = make()
        return cast(T, self._memo[slot])

    def matrix(self, window: int) -> Array:
        """Values of the `window` minutes ending at each time; shape (times, window)."""

        def make() -> Array:
            offsets = np.arange(-window + 1, 1, dtype=np.int64)
            return np.asarray(self.grid[self.t_index[:, None] + offsets[None, :]], dtype=np.float64)

        return self._cached("matrix", window, make)

    def valid(self, window: int) -> NDArray[np.bool_]:
        """Which entries of the window are present."""
        return self._cached("valid", window, lambda: ~np.isnan(self.matrix(window)))

    def count(self, window: int) -> Index:
        """Present samples per row."""
        return self._cached("count", window, lambda: self.valid(window).sum(axis=1))

    def covered(self, window: int) -> NDArray[np.bool_]:
        """Rows with enough present samples to trust."""
        need = max(1, math.ceil(self.min_coverage * window))
        return self.count(window) >= need

    def mean(self, window: int) -> Array:
        """Mean of present samples; NaN without enough coverage."""

        def make() -> Array:
            total = np.where(self.valid(window), self.matrix(window), 0.0).sum(axis=1)
            count = np.maximum(self.count(window), 1)
            return np.where(self.covered(window), total / count, np.nan)

        return self._cached("mean", window, make)

    def minimum(self, window: int) -> Array:
        """Smallest present sample; NaN without enough coverage."""
        low = np.where(self.valid(window), self.matrix(window), np.inf).min(axis=1)
        return np.where(self.covered(window), low, np.nan)

    def maximum(self, window: int) -> Array:
        """Largest present sample; NaN without enough coverage."""
        high = np.where(self.valid(window), self.matrix(window), -np.inf).max(axis=1)
        return np.where(self.covered(window), high, np.nan)

    def std(self, window: int) -> Array:
        """Population standard deviation; needs coverage and two samples."""

        def make() -> Array:
            centred = np.where(
                self.valid(window), self.matrix(window) - self.mean(window)[:, None], 0.0
            )
            var = (centred * centred).sum(axis=1) / np.maximum(self.count(window), 1)
            ok = self.covered(window) & (self.count(window) >= 2)
            return np.where(ok, np.sqrt(np.maximum(var, 0.0)), np.nan)

        return self._cached("std", window, make)

    def slope_per_minute(self, window: int) -> Array:
        """Least-squares slope in metric units per minute; needs coverage and two samples."""

        def make() -> Array:
            valid = self.valid(window)
            count = np.maximum(self.count(window), 1)
            x = np.arange(-window + 1, 1, dtype=np.float64)[None, :]
            y = np.where(valid, self.matrix(window), 0.0)
            x_mean = np.where(valid, x, 0.0).sum(axis=1) / count
            y_mean = y.sum(axis=1) / count
            dx = np.where(valid, x - x_mean[:, None], 0.0)
            dy = np.where(valid, y - y_mean[:, None], 0.0)
            sxx = (dx * dx).sum(axis=1)
            sxy = (dx * dy).sum(axis=1)
            ok = self.covered(window) & (self.count(window) >= 2) & (sxx > 0)
            return np.where(ok, sxy / np.where(sxx > 0, sxx, 1.0), np.nan)

        return self._cached("slope", window, make)

    def last(self, window: int) -> Array:
        """Latest present sample in the window, however old; NaN if there is none."""

        def make() -> Array:
            valid = self.valid(window)
            from_end = valid[:, ::-1].argmax(axis=1)
            position = window - 1 - from_end
            picked = self.matrix(window)[np.arange(valid.shape[0]), position]
            return np.where(valid.any(axis=1), picked, np.nan)

        return self._cached("last", window, make)


FeatureFn = Callable[[SeriesWindows, int, Mapping[str, Any]], Array]


@dataclass(frozen=True)
class FunctionInfo:
    """A registered feature function and how it is documented."""

    fn: FeatureFn
    needs_window: bool
    description: str


def _last(sw: SeriesWindows, window: int, params: Mapping[str, Any]) -> Array:
    return sw.last(window)


def _mean(sw: SeriesWindows, window: int, params: Mapping[str, Any]) -> Array:
    return sw.mean(window)


def _min(sw: SeriesWindows, window: int, params: Mapping[str, Any]) -> Array:
    return sw.minimum(window)


def _max(sw: SeriesWindows, window: int, params: Mapping[str, Any]) -> Array:
    return sw.maximum(window)


def _std(sw: SeriesWindows, window: int, params: Mapping[str, Any]) -> Array:
    return sw.std(window)


def _slope(sw: SeriesWindows, window: int, params: Mapping[str, Any]) -> Array:
    return sw.slope_per_minute(window) * 60.0


def _time_to_full(sw: SeriesWindows, window: int, params: Mapping[str, Any]) -> Array:
    full, cap = float(params["full"]), float(params["cap_minutes"])
    level = sw.last(sw.level_minutes)
    slope = sw.slope_per_minute(window)
    with np.errstate(divide="ignore", invalid="ignore"):
        minutes = np.where(slope > 0, (full - level) / slope, cap)
    result = np.where(level >= full, 0.0, np.minimum(minutes, cap))
    return np.where(np.isnan(level) | np.isnan(slope), np.nan, result)


FUNCTIONS: dict[str, FunctionInfo] = {
    "last": FunctionInfo(
        _last, True,
        "Latest present sample in the window (the current level); null if none. "
        "It carries a value forward for at most the window length.",
    ),
    "mean": FunctionInfo(_mean, True, "Mean of the present samples."),
    "min": FunctionInfo(_min, True, "Smallest present sample."),
    "max": FunctionInfo(_max, True, "Largest present sample."),
    "std": FunctionInfo(
        _std, True, "Population standard deviation of the present samples; needs two."
    ),
    "slope": FunctionInfo(
        _slope, True,
        "Least-squares slope of value against time, in metric units per hour; needs two samples.",
    ),
    "time_to_full": FunctionInfo(
        _time_to_full, True,
        "Minutes until the metric reaches `full` if it keeps rising at the window's slope. "
        "0 once the current level is at or above `full`; `cap_minutes` when the slope is zero "
        "or negative, or the estimate is longer than the cap; null without a level or a slope.",
    ),
}  # fmt: skip
