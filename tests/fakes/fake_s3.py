"""A filesystem-backed stand-in for the boto3 S3 client, for tests only.

Implements just the surface :mod:`hls_toolkit.s3_io` uses — ``head_object``,
``download_file``, ``upload_file``, ``list_objects_v2`` (paginated) and
``delete_objects`` — against a directory tree, so the real S3 code paths
(including upload verification) run unchanged without network or credentials.
"""
import os
import shutil
from pathlib import Path


class FakeClientError(Exception):
    def __init__(self, code="404"):
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploads = []
        self.downloads = []
        self.deletes = []
        self.undeletable = set()

    def _path(self, bucket: str, key: str) -> Path:
        return self.root / bucket / key

    def head_object(self, Bucket, Key):
        path = self._path(Bucket, Key)
        if not path.is_file():
            raise FakeClientError("404")
        return {"ContentLength": path.stat().st_size}

    def download_file(self, bucket, key, dest, Config=None, Callback=None):
        path = self._path(bucket, key)
        if not path.is_file():
            raise FakeClientError("404")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        self.downloads.append(f"s3://{bucket}/{key}")
        if Callback:
            Callback(path.stat().st_size)

    def upload_file(self, filename, bucket, key, ExtraArgs=None, Config=None):
        dest = self._path(bucket, key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(filename, dest)
        self.uploads.append({"key": key, "content_type": (ExtraArgs or {}).get("ContentType")})

    def get_paginator(self, _operation):
        return _FakePaginator(self)

    def list_objects_v2(self, Bucket, Prefix=""):
        base = self.root / Bucket
        contents = []
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    key = path.relative_to(base).as_posix()
                    if key.startswith(Prefix):
                        contents.append({"Key": key, "Size": path.stat().st_size})
        return {"Contents": contents}

    def delete_objects(self, Bucket, Delete):
        # Like S3, a key that cannot be deleted is reported in "Errors" rather
        # than raised. Tests list such keys in `undeletable`.
        deleted, errors = [], []
        for obj in Delete.get("Objects", []):
            if obj["Key"] in self.undeletable:
                errors.append({"Key": obj["Key"], "Code": "AccessDenied",
                               "Message": "Access Denied"})
                continue
            path = self._path(Bucket, obj["Key"])
            if path.is_file():
                path.unlink()
            deleted.append(obj)
        self.deletes.extend(o["Key"] for o in deleted)
        return {"Deleted": deleted, "Errors": errors}

    # -- helpers for tests -------------------------------------------------
    def put(self, bucket: str, key: str, data: bytes) -> str:
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"s3://{bucket}/{key}"

    def keys_under(self, bucket: str, prefix: str = ""):
        base = self.root / bucket
        if not base.is_dir():
            return []
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*")
                      if p.is_file() and p.relative_to(base).as_posix().startswith(prefix))


class _FakePaginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, Bucket, Prefix=""):
        yield self.client.list_objects_v2(Bucket=Bucket, Prefix=Prefix)


def install(monkeypatch_target, root: Path) -> FakeS3Client:
    """Point :mod:`hls_toolkit.s3_io` at a fake client rooted at `root`."""
    client = FakeS3Client(root)
    monkeypatch_target.get_s3_client = lambda region=None: client

    class _Cfg:
        def __init__(self, **kwargs):
            pass

    monkeypatch_target._transfer_config = lambda: _Cfg()

    # s3_io imports ClientError lazily from botocore; give head_object something
    # to catch when boto3 is not installed at all.
    import builtins
    if not hasattr(builtins, "_fake_s3_installed"):
        builtins._fake_s3_installed = True
    return client
