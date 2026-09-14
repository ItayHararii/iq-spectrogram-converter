import pytest

from crfs_iq_recorder.frequency import (
    FrequencyError,
    display_from_hz,
    from_center_bandwidth,
    from_start_end,
    parse_to_integer_hz,
)


def test_default_center_bandwidth_matches_supplied_band():
    plan = from_center_bandwidth(800_000_000, 12_500_000)
    assert plan.center_hz == 800_000_000
    assert plan.bandwidth_hz == 12_500_000
    assert plan.start_hz == 793_750_000
    assert plan.end_hz == 806_250_000


def test_start_end_inverse_of_center_bandwidth():
    plan = from_start_end(793_750_000, 806_250_000)
    assert plan.center_hz == 800_000_000
    assert plan.bandwidth_hz == 12_500_000


def test_mhz_display_preserves_12_5():
    assert display_from_hz(12_500_000, "MHz") == "12.5"
    assert display_from_hz(800_000_000, "MHz") == "800"
    assert display_from_hz(793_750_000, "MHz") == "793.75"
    assert display_from_hz(806_250_000, "MHz") == "806.25"
    assert display_from_hz(800_000_000, "GHz") == "0.8"
    assert display_from_hz(12_500_000, "GHz") == "0.0125"


def test_mhz_round_trip_for_main_screen_defaults():
    assert parse_to_integer_hz("800", "MHz", field="center") == 800_000_000
    assert parse_to_integer_hz("12.5", "MHz", field="bandwidth") == 12_500_000
    assert parse_to_integer_hz("793.75", "MHz", field="start") == 793_750_000
    assert parse_to_integer_hz("806.25", "MHz", field="end") == 806_250_000
    hz = parse_to_integer_hz("0.8", "GHz", field="center")
    assert hz == 800_000_000
    assert parse_to_integer_hz("800000", "kHz", field="center") == 800_000_000
    assert parse_to_integer_hz("800000000", "Hz", field="center") == 800_000_000


def test_rejects_fractional_hz_without_rounding():
    with pytest.raises(FrequencyError, match="not an integer"):
        parse_to_integer_hz("800.0000001", "MHz", field="center")


def test_rejects_start_not_below_end():
    with pytest.raises(FrequencyError, match="below end"):
        from_start_end(800_000_000, 800_000_000)
    with pytest.raises(FrequencyError, match="below end"):
        from_start_end(900_000_000, 800_000_000)


def test_rejects_non_positive_bandwidth():
    with pytest.raises(FrequencyError, match="positive"):
        from_center_bandwidth(800_000_000, 0)


def test_rejects_odd_bandwidth_that_would_round_edges():
    with pytest.raises(FrequencyError, match="odd"):
        from_center_bandwidth(800_000_000, 1)


def test_rejects_odd_start_plus_end_center():
    with pytest.raises(FrequencyError, match="not an integer"):
        from_start_end(100, 101)
