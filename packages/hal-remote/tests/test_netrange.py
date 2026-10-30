"""Tests for `halremote.netrange`: the from= table of the design and `--from` validation."""

import pytest

from halremote import netrange


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("192.168.1.57", "192.168.1.0/24"),
        ("10.4.5.6", "10.4.5.0/24"),
        ("172.20.9.9", "172.20.9.0/24"),
        ("100.101.102.103", "100.101.102.103/32"),
        ("127.0.0.1", "127.0.0.1"),
        ("::1", "::1"),
        ("fd12:3456:789a:1::42", "fd12:3456:789a:1::/64"),
        ("2a00:1450:4001:2:abcd::1", "2a00:1450:4001:2::/64"),
        ("::ffff:192.168.1.57", "192.168.1.0/24"),
    ],
)
def test_address_maps_to_the_documented_range(addr, expected):
    assert netrange.derive(addr) == expected


def test_prefix_lengths_are_configurable():
    assert netrange.derive("192.168.1.57", v4_prefix=16) == "192.168.0.0/16"
    assert netrange.derive("fd00::1", v6_prefix=48) == "fd00::/48"


@pytest.mark.parametrize("addr", ["8.8.8.8", "fe80::1", "not-an-ip", "", "::ffff:8.8.8.8"])
def test_public_link_local_and_garbage_addresses_are_refused(addr):
    with pytest.raises(netrange.FromPatternError):
        netrange.derive(addr)


@pytest.mark.parametrize(
    "pattern", ["any", "192.168.1.0/24", "10.0.0.0/8,fd00::/8", "192.168.1.*", "!10.0.0.1,*"]
)
def test_valid_from_values_are_accepted(pattern):
    assert netrange.validate(pattern) == pattern


def test_validate_strips_spaces_between_items():
    assert netrange.validate("10.0.0.0/8, 192.168.0.0/16") == "10.0.0.0/8,192.168.0.0/16"


@pytest.mark.parametrize("pattern", ["", "a,,b", '1.2.3.4"', "10.0.0.0/33", "host.example.com", "1.2.3.4 5"])
def test_invalid_from_values_are_refused(pattern):
    with pytest.raises(netrange.FromPatternError):
        netrange.validate(pattern)
