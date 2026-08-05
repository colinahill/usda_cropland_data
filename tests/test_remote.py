from pathlib import Path

import pytest

from usda_cdl import remote, store


class FakeS3:
    """Minimal stand-in for the boto3 S3 client used by remote.py."""

    def __init__(self, contents=()):
        self.contents = list(contents)
        self.puts: list[dict] = []
        self.deleted: list[str] = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        return {}

    def delete_objects(self, **kwargs):
        raise AssertionError("batch DeleteObjects is unimplemented on data.source.coop")

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        contents = self.contents

        class Paginator:
            def paginate(self, **kwargs):
                prefix = kwargs["Prefix"]
                yield {"Contents": [obj for obj in contents if obj["Key"].startswith(prefix)]}

        return Paginator()


def test_upload_readme_targets_product_root(tmp_path, monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text("# landing page\n")
    fake = FakeS3()
    monkeypatch.setattr(remote, "client", lambda _: fake)

    remote.upload_readme("acct", "prod", readme=readme)

    (put,) = fake.puts
    assert put["Key"] == "prod/README.md"
    assert put["Bucket"] == "acct"
    assert put["ContentType"] == "text/markdown"
    assert put["Body"] == b"# landing page\n"


def test_upload_readme_missing_file(monkeypatch):
    monkeypatch.setattr(remote, "client", lambda _: FakeS3())
    with pytest.raises(FileNotFoundError):
        remote.upload_readme("acct", "prod", readme=Path("does/not/exist.md"))


def test_store_keys_scoped_to_version_prefix():
    prefix = f"prod/{store.STORE_SUBPATH}"
    fake = FakeS3(
        [
            {"Key": f"{prefix}/repo", "Size": 10},
            {"Key": f"{prefix}/chunks/AAAA", "Size": 100},
            {"Key": "prod/README.md", "Size": 5},  # landing page must not be listed
            {"Key": "prod/v0.0.1.icechunk/repo", "Size": 7},  # other version must not be listed
        ]
    )

    keys, total = remote.store_keys("acct", "prod", s3=fake)

    assert keys == [f"{prefix}/repo", f"{prefix}/chunks/AAAA"]
    assert total == 110


def test_delete_keys_uses_per_key_deletes():
    fake = FakeS3()
    keys = [f"chunks/{n}" for n in range(25)]

    remote.delete_keys("acct", keys, s3=fake, workers=4)

    assert sorted(fake.deleted) == sorted(keys)
