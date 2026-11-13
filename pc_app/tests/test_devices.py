import hashlib
import io
import json

import pytest

import volgas_devices as d


def test_compare():
    assert d.compare(3, 3) == d.CURRENT
    assert d.compare(3, 4) == d.UPDATE
    assert d.compare(4, 3) == d.NEWER
    assert d.compare("26.1.1", "26.1.2") == d.UPDATE
    assert d.compare("26.1.1", "26.10.0") == d.UPDATE   # not a string comparison
    assert d.compare(None, 3) == d.UNKNOWN
    assert d.compare(3, "unknown") == d.UNKNOWN


def test_parse_manifest_skips_bad_entries():
    data = {"dash": {"version": 3, "file": "w.bin", "sha256": "ABC"},
            "hub": {"version": 1, "file": "missing.bin"},
            "toaster": {"version": 1, "file": "w.bin"},
            "pedals": {"file": "w.bin"}}
    out = d.parse_manifest(data, {"w.bin": "https://x/w.bin"})
    assert out == {"dash": {"version": 3, "file": "w.bin", "sha256": "abc", "url": "https://x/w.bin"}}


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_fetch_manifest_and_download():
    image = b"firmware" * 1000
    manifest = {"dash": {"version": 3, "file": "w.bin", "sha256": hashlib.sha256(image).hexdigest()}}
    responses = {
        "api": json.dumps({"assets": [{"name": "firmware.json", "browser_download_url": "m"},
                                      {"name": "w.bin", "browser_download_url": "w"}]}).encode(),
        "m": json.dumps(manifest).encode(),
        "w": image,
    }
    http_get = lambda url, timeout: FakeResponse(responses[url])
    out = d.fetch_manifest(http_get, "api")
    assert out["dash"]["version"] == 3
    assert d.download_firmware(http_get, out["dash"]) == image
    out["dash"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        d.download_firmware(http_get, out["dash"])


def test_release_without_firmware_is_empty():
    http_get = lambda url, timeout: FakeResponse(json.dumps({"assets": [{"name": "Volgas.exe",
                                                                         "browser_download_url": "x"}]}).encode())
    assert d.fetch_manifest(http_get, "api") == {}
