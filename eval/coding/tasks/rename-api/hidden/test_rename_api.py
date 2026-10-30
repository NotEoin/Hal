import pathlib
import warnings

import pytest

import geom
from geom.report import describe
from geom.shapes import area


def test_area_is_exported_and_computes():
    assert geom.area(2, 3) == 6 and area(2, 3) == 6


def test_old_name_still_works_with_a_deprecation_warning():
    with pytest.warns(DeprecationWarning):
        assert geom.area_of(2, 3) == 6


def test_report_does_not_use_the_deprecated_name():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert describe(2, 3) == "2x3 has area 6"


def test_report_source_has_no_old_name():
    source = pathlib.Path(geom.__file__).with_name("report.py").read_text()
    assert "area_of" not in source
