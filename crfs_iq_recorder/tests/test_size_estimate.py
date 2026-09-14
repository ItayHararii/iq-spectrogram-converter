from decimal import Decimal

from crfs_iq_recorder.size_estimate import empirical_size_mb, format_estimate, format_mb, technical_payload_bytes


def test_empirical_reference_cases():
    assert empirical_size_mb(10_000_000, 1) == Decimal("61.051")
    assert empirical_size_mb(20_000_000, 1) == Decimal("122.102")
    assert empirical_size_mb(10_000_000, 10) == Decimal("610.510")
    assert empirical_size_mb(12_500_000, Decimal("0.1")) == Decimal("7.631375")


def test_empirical_format_keeps_expected_digits():
    assert format_mb(empirical_size_mb(12_500_000, "0.1")) == "7.631375"


def test_estimate_switches_to_gb_and_tb():
    assert format_estimate(Decimal("7.631375")) == "7.631375 MB"
    assert format_estimate(Decimal("2442.04")) == "2.44 GB"
    assert format_estimate(Decimal("1000")) == "1.00 GB"
    assert format_estimate(Decimal("1000000")) == "1.00 TB"


def test_technical_estimate_does_not_use_bandwidth():
    est = technical_payload_bytes(12_500_000, 0.1, bytes_per_complex_sample=4, number_of_recorded_channels=1)
    assert est.payload_bytes == Decimal("5000000")
    assert est.megabytes_decimal == Decimal("5")
