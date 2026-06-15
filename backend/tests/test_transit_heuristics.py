import pytest

from backend.services import transit_data as td
from backend.agents.transit import TransitAgent


def test_manual_handling_drop_height_is_parameterised():
    assert td.manual_handling_envelope()["drop_height_m"] == 0.91          # default kept
    assert td.manual_handling_envelope(drop_height_m=0.5)["drop_height_m"] == 0.5
    assert td.manual_handling_envelope(drop_height_m=1.5)["drop_height_m"] == 1.5


def test_blended_envelope_uses_user_drop_height():
    env = td.blended_envelope(
        mode_mix={"manual_handling": 1.0},
        manual_drop_height_m=1.0,
    )
    assert env["drop_height_m"] == 1.0
    assert td.blended_envelope(mode_mix={"manual_handling": 1.0})["drop_height_m"] == 0.91


def test_durations_accumulate_into_composite_minutes():
    env = td.blended_envelope(
        mode_mix={"truck": 0.5, "rail": 0.5},
        durations_min={"truck": 8 * 60, "rail": 12 * 60},   # 8h truck + 12h rail
    )
    # Composite vibration exposure is the weighted sum of per-mode minutes.
    assert env["vibration_duration_min"] == pytest.approx(0.5 * 480 + 0.5 * 720)


def test_transit_agent_carries_duration():
    agent = TransitAgent()
    te = agent.build({"truck": 1.0}, durations_min={"truck": 240})
    assert te.vibration_duration_min == 240


def test_empty_mode_mix_duration_matches_fallback():
    env = td.blended_envelope(mode_mix={})
    # falls back to truck; duration should be truck's default, not 0
    assert env["vibration_duration_min"] == td._DEFAULT_DURATION_MIN["truck"]


def test_longer_vibration_duration_accumulates_more_cycles():
    from backend.agents.ista2a import Ista2AAgent

    agent = Ista2AAgent()
    # g_rms=0.9 -> g_peak=2.7 (above the fatigue threshold), so the S/N branch
    # runs and cycle counts are populated (not None).
    # _vibration_fatigue returns (g_peak, n_cycles, n_to_fail, verdict, rationale).
    short = agent._vibration_fatigue(0.9, 60)
    long = agent._vibration_fatigue(0.9, 12 * 60)

    short_cycles = short[1]
    long_cycles = long[1]
    assert short_cycles is not None and long_cycles is not None
    # More transit minutes => more accumulated vibration cycles.
    assert long_cycles > short_cycles
    # cycles-to-fail depends only on g_peak, so it must not change with duration.
    assert long[2] == short[2]
    # Longer exposure must never produce a *safer* verdict.
    safer = {"pass": 0, "fail": 1}
    assert safer[long[3]] >= safer[short[3]]
