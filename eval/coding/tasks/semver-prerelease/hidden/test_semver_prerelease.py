from semverx.version import compare


def test_core_versions_compare_numerically():
    assert compare("1.2.10", "1.2.9") == 1
    assert compare("1.0.0", "1.0.0") == 0


def test_prerelease_is_below_its_release():
    assert compare("1.0.0-rc.1", "1.0.0") == -1
    assert compare("1.0.0", "1.0.0-rc.1") == 1


def test_prerelease_identifiers_follow_semver_order():
    order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
    for low, high in zip(order, order[1:]):
        assert compare(low, high) == -1, (low, high)


def test_build_metadata_is_ignored():
    assert compare("1.0.0+abc", "1.0.0+xyz") == 0
