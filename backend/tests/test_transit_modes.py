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
