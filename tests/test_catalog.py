import pytest

from usda_cdl import catalog


def test_source_files_default_years():
    files = catalog.source_files("30m")
    assert [f.year for f in files] == [2024, 2025]  # shrunken by conftest
    assert files[-1].url.endswith("2025_30m_cdls.zip")
    assert files[-1].tif_name == "2025_30m_cdls.tif"


def test_source_files_unknown_year():
    with pytest.raises(ValueError, match="No known national"):
        catalog.source_files("30m", [1999])


def test_parse_years():
    assert catalog.parse_years("2025") == [2025]
    assert catalog.parse_years("2008-2010") == [2008, 2009, 2010]
    assert catalog.parse_years("2008,2010,2012-2013") == [2008, 2010, 2012, 2013]
