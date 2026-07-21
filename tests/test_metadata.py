from usda_cdl import config, metadata


def test_bundled_classes():
    classes = metadata.load_bundled_classes()
    assert classes[1]["name"] == "Corn"
    assert classes[5]["name"] == "Soybeans"
    assert classes[24]["name"] == "Winter Wheat"
    assert classes[81]["name"] == "Clouds/No Data"
    assert classes[1]["color"] == "#FFD200"
    assert len(classes) > 100


def test_crop_type_attrs_cf_consistency():
    classes = metadata.load_bundled_classes()
    attrs = metadata.crop_type_attrs(classes)
    assert len(attrs["flag_values"]) == len(attrs["flag_meanings"].split())
    assert "missing_value" not in attrs  # would trigger float mask-and-scale
    assert config.NODATA in attrs["flag_values"]
    assert attrs["class_names"]["1"] == "Corn"
    # CF tokens: no spaces/slashes
    for token in attrs["flag_meanings"].split():
        assert token == token.lower()
        assert "/" not in token and " " not in token


def test_read_vat_dbf_matches_bundled():
    vat_path = "data/2025_30m_cdls/2025_30m_cdls.tif.vat.dbf"
    import pathlib
    import pytest

    if not pathlib.Path(vat_path).exists():
        pytest.skip("real 2025 VAT not on disk")
    classes = metadata.read_vat_dbf(vat_path)
    assert classes == metadata.load_bundled_classes()


def test_spatial_ref_attrs():
    from tests.conftest import TEST_GRID

    attrs = metadata.spatial_ref_attrs(TEST_GRID)
    assert "crs_wkt" in attrs
    assert attrs["grid_mapping_name"] == "albers_conical_equal_area"
    assert attrs["GeoTransform"].startswith("-2417835.0 30.0 0.0 3321225.0")
