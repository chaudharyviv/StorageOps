from pathlib import Path

from simulator.catalogue import CATALOGUE_PATH, emitted_metrics, parse, render


def test_catalogue_matches_what_the_simulator_emits() -> None:
    listed = parse(CATALOGUE_PATH.read_text(encoding="utf-8"))
    emitted = emitted_metrics()

    unlisted = sorted(set(emitted) - set(listed))
    stale = sorted(set(listed) - set(emitted))
    hint = "regenerate with `python -m simulator.catalogue`"
    assert not unlisted, f"emitted but not in docs/METRICS.md: {unlisted}; {hint}"
    assert not stale, f"listed in docs/METRICS.md but never emitted: {stale}; {hint}"
    wrong = sorted(m for m in emitted if emitted[m] != listed[m])
    assert not wrong, f"unit or object types differ for {wrong}; {hint}"


def test_render_and_parse_round_trip() -> None:
    emitted = emitted_metrics()
    assert parse(render(emitted)) == emitted


def test_catalogue_includes_metrics_from_scenario_extra_objects(tmp_path: Path) -> None:
    (tmp_path / "trident_pvc_burst.yaml").write_text(
        """
scenario: trident_pvc_burst
duration_minutes: 10
extra_objects:
  - object_type: trident_backend
    object_id: backend1
    metrics:
      - {name: pvc.count, unit: count, base: 1}
effects: []
labels:
  - {object_id: backend1, start_minute: 1}
variants:
  mild: {split: heldout}
""",
        encoding="utf-8",
    )
    found = emitted_metrics([tmp_path])
    assert found["pvc.count"] == ("count", {"trident_backend"})
    assert "iops" in found  # the healthy cluster is always included
