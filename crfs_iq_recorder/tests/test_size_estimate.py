from decimal import Decimal

import pytest

from crfs_iq_recorder.sensor_storage import estimated_recording_bytes
from crfs_iq_recorder.size_estimate import (
    empirical_size_mb,
    estimated_size_mb,
    format_estimate,
    format_mb,
    iq_sample_rate_hz,
    recorded_seconds_from_bytes,
    technical_payload_bytes,
    wave_size_model,
)

# RFeye Node 40-8, firmware 2.25-325, 31 Oct 2026: (bandwidth, seconds, sample rate, bytes on disk, parts).
HARDWARE_WAVE_SIZES = [
    (1_000_000, 2, 1_953_125, 15_625_784, 1),
    (5_000_000, 2, 7_812_500, 62_853_684, 1),
    (6_000_000, 2, 7_812_500, 62_853_684, 1),
    (8_000_000, 2, 15_625_000, 125_706_804, 1),
    (10_000_000, 2, 15_625_000, 125_706_804, 1),
    (12_500_000, 2, 15_625_000, 125_706_804, 1),
    (20_000_000, 10, 31_250_000, 1_252_874_128, 3),
    (20_000_000, 40, 31_250_000, 5_003_115_104, 10),
    (21_000_000, 5, 31_250_000, 628_532_464, 2),
    (30_000_000, 2, 62_500_000, 502_825_520, 1),
    (40_000_000, 2, 62_500_000, 502_825_520, 1),
    (40_000_000, 10, 62_500_000, 2_501_557_488, 5),
    (50_000_000, 5, 62_500_000, 1_252_874_140, 3),
    (60_000_000, 2, 62_500_000, 502_825_520, 1),
]


@pytest.mark.parametrize("bandwidth, seconds, rate, actual_bytes, parts", HARDWARE_WAVE_SIZES)
def test_sample_rate_model_matches_hardware_within_one_percent(bandwidth, seconds, rate, actual_bytes, parts):
    assert iq_sample_rate_hz(bandwidth) == rate
    model = wave_size_model(bandwidth, seconds)
    assert model.sample_rate_hz == rate
    assert model.parts == parts
    error = (model.total_bytes - actual_bytes) / actual_bytes
    assert -0.002 < error < 0.01, f"{bandwidth} Hz x {seconds} s: {error:+.3%}"
    assert estimated_recording_bytes(bandwidth, seconds) == model.total_bytes


def test_recording_estimate_follows_the_connected_models_highest_rate():
    """Node 100-18, 6 Nov 2026: 2110-2170 MHz (60 MHz) for 5 s is recorded at 125 MS/s (2.43-2.5 GB).
    1.17.0 estimated it with the Node 40-8's 62.5 MS/s cap: half the size, for the storage check
    and for the 98 % rule that decides when the capture is complete."""
    node_100 = estimated_recording_bytes(60_000_000, 5, max_rate_hz=125_000_000)
    assert node_100 == wave_size_model(60_000_000, 5, max_rate_hz=125_000_000).total_bytes
    assert node_100 >= 2_500_000_000
    assert estimated_recording_bytes(60_000_000, 5) < node_100 // 2 + 1_000_000


def test_legacy_linear_formula_was_far_off_where_the_rate_steps():
    # 12.5 MHz is recorded at 15.625 MS/s, so the 61.051 MB/10 MHz rule overshoots by about 21 %.
    actual = 125_706_804
    legacy = float(empirical_size_mb(12_500_000, 2)) * 1_000_000
    assert legacy / actual > 1.2
    assert abs(float(estimated_size_mb(12_500_000, 2)) * 1_000_000 / actual - 1) < 0.01


def test_recorded_seconds_from_bytes_recovers_capture_time():
    # 40 MHz x 40 s request that the sensor cut short: 2 parts, 823 MB.
    assert recorded_seconds_from_bytes(823_159_264, 40_000_000, parts=2) == pytest.approx(3.29, abs=0.01)
    assert recorded_seconds_from_bytes(1_252_874_128, 20_000_000, parts=3) == pytest.approx(10.02, abs=0.01)


def test_sample_rate_is_capped_and_steps_by_powers_of_two():
    assert iq_sample_rate_hz(100_000_000) == 62_500_000
    assert iq_sample_rate_hz(50_000_000) == 62_500_000
    assert iq_sample_rate_hz(49_000_000) == 62_500_000
    assert iq_sample_rate_hz(25_000_000) == 31_250_000
    assert iq_sample_rate_hz(12_500_000) == 15_625_000
    assert iq_sample_rate_hz(12_500_001) == 31_250_000
    assert iq_sample_rate_hz(100_000) == 1_953_125


def test_empirical_reference_cases():
    assert empirical_size_mb(10_000_000, 1) == Decimal("61.051")
    assert empirical_size_mb(20_000_000, 1) == Decimal("122.102")
    assert empirical_size_mb(10_000_000, 10) == Decimal("610.510")
    assert empirical_size_mb(12_500_000, Decimal("0.1")) == Decimal("7.631375")


def test_empirical_format_keeps_expected_digits():
    assert format_mb(empirical_size_mb(12_500_000, "0.1")) == "7.631375"


def test_estimate_switches_to_gb_and_tb():
    assert format_estimate(Decimal("7.631375")) == "7.6 MB"
    assert format_estimate(Decimal("62.875692")) == "62.9 MB"
    assert format_estimate(Decimal("0.04")) == "< 0.1 MB"
    assert format_estimate(Decimal("2442.04")) == "2.44 GB"
    assert format_estimate(Decimal("1000")) == "1.00 GB"
    assert format_estimate(Decimal("1000000")) == "1.00 TB"


def test_technical_estimate_does_not_use_bandwidth():
    est = technical_payload_bytes(12_500_000, 0.1, bytes_per_complex_sample=4, number_of_recorded_channels=1)
    assert est.payload_bytes == Decimal("5000000")
    assert est.megabytes_decimal == Decimal("5")
