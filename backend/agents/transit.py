"""Transit Profile Agent (5.5).

Now consumes the real Transit_data CSV envelopes via
`backend.services.transit_data.blended_envelope`. The previous hand-coded
mode-mix factor table is gone; every value the user sees is provenance-tagged
(file name + row count) so the report can cite real data.
"""
from __future__ import annotations

from ..schemas import TransitEnvelope
from ..services import transit_data as td


# Suggested ISTA-style sequence based on dominant modes.
def _sequence_for(dominant: list[str]) -> list[str]:
    seq = ["pre-conditioning"]
    if "truck" in dominant or "manual_handling" in dominant:
        seq.append("random vibration (truck PSD)")
    if "ship" in dominant:
        seq.append("low-frequency motion (sea state 4–5 swell)")
    if "air" in dominant:
        seq.append("low-pressure cycle + cargo vibration")
    if "manual_handling" in dominant:
        seq.append("free-fall drop sequence (corner/edge/face)")
    seq.append("compression test (warehouse stack)")
    seq.append("post-test inspection")
    return seq


class TransitAgent:
    def build(
        self,
        mode_mix: dict[str, float],
        *,
        road: str = "mixed",
        ship_severity: str = "moderate",
    ) -> TransitEnvelope:
        env = td.blended_envelope(
            mode_mix=mode_mix, road=road, ship_severity=ship_severity,
        )

        return TransitEnvelope(
            mode_mix=env["mode_mix"],
            vibration_g_rms=env["g_rms"],
            drop_height_m=env["drop_height_m"],
            compression_load_n=env["compression_load_n"],
            handling_fraction=env["handling_fraction"],
            dominant_risks=env["dominant_modes"],
            suggested_test_sequence=_sequence_for(env["dominant_modes"]),
            confidence="estimated",
        )

    # Convenience: full envelope with sources attached for the chart payload.
    def detailed(
        self,
        mode_mix: dict[str, float],
        *,
        road: str = "mixed",
        ship_severity: str = "moderate",
    ) -> dict:
        return td.blended_envelope(
            mode_mix=mode_mix, road=road, ship_severity=ship_severity,
        )
