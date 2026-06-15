import pytest
from backend.services import transit_data as td


@pytest.mark.skipif(not td.available(), reason="transit CSVs not present")
def test_pickup_envelope_is_data_backed_and_distinct():
    env = td.pickup_envelope(road="rough_secondary")
    assert env["mode"] == "pickup"
    assert env["source_file"].endswith(".csv")          # real CSV, not industry_reference
    assert 0.0 < env["g_rms"] < 5.0
    assert "g_p95" in env and "shock_risk_p95" in env


def test_pickup_listed_in_available_modes(monkeypatch):
    monkeypatch.setattr(td, "_exists", lambda key: True)  # pretend all CSVs present
    modes = td.available_modes()
    assert "pickup" in modes


from backend.agents.transit import TransitAgent


def test_rail_envelope_reference_values():
    env = td.rail_envelope()
    assert env["mode"] == "rail"
    assert env["source_file"] == "industry_reference"
    # Rail: low high-frequency vibration vs air, dominant low-freq coupling shock.
    assert env["g_rms"] < td.air_envelope()["g_rms"]
    assert env["coupling_shock_g"] > 0


def test_available_modes_includes_reference_modes():
    modes = td.selectable_modes()        # data-backed + reference
    assert {"air", "rail"} <= set(modes)


def test_sequence_for_rail_and_pickup():
    agent = TransitAgent()
    seq_rail = agent._sequence_for(["rail"])
    seq_pickup = agent._sequence_for(["pickup"])
    assert any("rail" in s.lower() or "coupling" in s.lower() for s in seq_rail)
    assert seq_pickup  # non-empty, reuses truck-style PSD sequence
