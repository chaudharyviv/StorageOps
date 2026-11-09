import numpy as np
import pytest

from contracts import ObjectType
from simulator.engine import apply_effect
from simulator.patterns import PatternSpec, pattern_delta
from simulator.scenario import Effect, Target


def rng() -> np.random.Generator:
    return np.random.default_rng(7)


def test_linear_ramp_reaches_magnitude_and_holds() -> None:
    spec = PatternSpec("linear_ramp", start_minute=20, duration_minutes=30, magnitude=10.0)
    delta = pattern_delta(spec, 100, rng())
    assert np.all(delta[:20] == 0)
    assert delta[49] == pytest.approx(10.0)
    assert np.all(np.diff(delta[20:50]) > 0)
    assert np.all(delta[50:] == 10.0)


def test_ramp_without_hold_returns_to_zero() -> None:
    spec = PatternSpec(
        "linear_ramp", start_minute=20, duration_minutes=30, magnitude=10.0, hold=False
    )
    delta = pattern_delta(spec, 100, rng())
    assert delta[49] == pytest.approx(10.0)
    assert np.all(delta[50:] == 0)


def test_exponential_ramp_is_slow_then_steep() -> None:
    spec = PatternSpec("exponential_ramp", magnitude=100.0, shape=4.0)
    delta = pattern_delta(spec, 101, rng())
    assert delta[0] == 0
    assert delta[-1] == pytest.approx(100.0)
    assert delta[50] < 20.0  # a linear ramp would be at 50


def test_step_applies_only_inside_its_window() -> None:
    spec = PatternSpec("step", start_minute=10, duration_minutes=5, magnitude=3.0)
    delta = pattern_delta(spec, 30, rng())
    assert delta.tolist() == [0.0] * 10 + [3.0] * 5 + [0.0] * 15


def test_spike_repeats_every_period() -> None:
    spec = PatternSpec("spike", start_minute=10, width_minutes=10, period_minutes=40, magnitude=5.0)
    delta = pattern_delta(spec, 100, rng())
    peaks = [15, 55, 95]
    assert [float(delta[p]) for p in peaks] == pytest.approx([5.0, 5.0, 5.0])
    assert delta[10] == 0 and delta[20] == 0 and delta[30] == 0


def test_drift_is_scaled_to_magnitude_and_seeded() -> None:
    spec = PatternSpec("drift", magnitude=60.0)
    first = pattern_delta(spec, 500, np.random.default_rng(1))
    again = pattern_delta(spec, 500, np.random.default_rng(1))
    other = pattern_delta(spec, 500, np.random.default_rng(2))
    assert float(np.max(np.abs(first))) == pytest.approx(60.0)
    assert np.array_equal(first, again)
    assert not np.array_equal(first, other)


def test_noise_has_requested_sigma_inside_window_only() -> None:
    spec = PatternSpec("noise", start_minute=1000, sigma=2.0)
    delta = pattern_delta(spec, 21000, rng())
    assert np.all(delta[:1000] == 0)
    assert float(np.std(delta[1000:])) == pytest.approx(2.0, rel=0.05)


def test_unknown_pattern_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown pattern"):
        pattern_delta(PatternSpec("sawtooth"), 10, rng())


def target() -> Target:
    return Target(ObjectType.QOS, "qos1", "iops")


def test_multiply_scales_the_series() -> None:
    effect = Effect(target(), PatternSpec("step", magnitude=0.5), mode="multiply")
    assert apply_effect(np.full(5, 10.0), effect, rng()).tolist() == [15.0] * 5


def test_effect_clamps_apply_after_the_pattern() -> None:
    effect = Effect(target(), PatternSpec("step", magnitude=100.0), minimum=0.0, maximum=50.0)
    assert apply_effect(np.full(3, 10.0), effect, rng()).tolist() == [50.0] * 3
