from usda_cdl.publish import REPO_POINTER, plan_uploads


def make_store(tmp_path):
    store = tmp_path / "store"
    (store / "chunks").mkdir(parents=True)
    (store / "chunks" / "AAAA").write_bytes(b"x" * 100)
    (store / "chunks" / "BBBB").write_bytes(b"y" * 200)
    (store / REPO_POINTER).write_bytes(b"pointer")
    return store


def test_plan_uploads_fresh_remote(tmp_path):
    store = make_store(tmp_path)
    uploads = plan_uploads(store, {}, "product/v1.icechunk")
    keys = [k for _, k, _ in uploads]
    assert keys == ["product/v1.icechunk/chunks/AAAA", "product/v1.icechunk/chunks/BBBB"]
    # the mutable pointer is never part of the bulk sync
    assert not any(k.endswith(REPO_POINTER) for k in keys)


def test_plan_uploads_skips_up_to_date(tmp_path):
    store = make_store(tmp_path)
    remote = {"product/v1.icechunk/chunks/AAAA": 100}
    uploads = plan_uploads(store, remote, "product/v1.icechunk")
    assert [k for _, k, _ in uploads] == ["product/v1.icechunk/chunks/BBBB"]


def test_plan_uploads_reuploads_size_mismatch(tmp_path):
    store = make_store(tmp_path)
    remote = {
        "product/v1.icechunk/chunks/AAAA": 1,  # truncated remote copy
        "product/v1.icechunk/chunks/BBBB": 200,
    }
    uploads = plan_uploads(store, remote, "product/v1.icechunk")
    assert [k for _, k, _ in uploads] == ["product/v1.icechunk/chunks/AAAA"]
