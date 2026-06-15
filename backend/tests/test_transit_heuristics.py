from backend.services import transit_data as td


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
