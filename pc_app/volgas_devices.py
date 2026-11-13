"""What Volgas knows about its devices' firmware: which devices exist,
how their versions compare, and the firmware list published with each
GitHub release.

A release can carry a `firmware.json` asset:

    {"dash": {"version": 3, "file": "wheel-3.bin", "sha256": "<hex>"},
     "hub":  {"version": 1, "file": "hub-1.bin",   "sha256": "<hex>"}}

plus the .bin files it names. No firmware.json means no device firmware has
been published (yet) - that's an answer, not an error.
"""
import hashlib
import json
import re

DEVICES = ("pedals", "dash", "hub", "wheelbase")

DEVICE_NAMES = {
    "pedals": "Pedals",
    "dash": "Dash",
    "hub": "Hub",
    "wheelbase": "Wheelbase",
}

# Which devices Volgas can actually update. The others show up on the Updates
# page with their version and a check, but their update isn't built yet.
UPDATABLE = {"dash"}

MANIFEST_ASSET = "firmware.json"

CURRENT, UPDATE, NEWER, UNKNOWN = "current", "update", "newer", "unknown"


def format_wheel_version(raw):
    """The dash/wheel firmware's 16-bit version as text: below 256 a plain
    number (releases 1-5), otherwise major << 8 | minor (0x0501 = "5.1").
    None for 0 (not reported)."""
    if not raw:
        return None
    return str(raw) if raw < 256 else f"{raw >> 8}.{raw & 255}"


def version_tuple(version):
    """Comparable form of a firmware version: 3, "3", "26.1.1" -> tuples of
    ints. None if there's nothing numeric to compare."""
    if version is None:
        return None
    nums = re.findall(r"\d+", str(version))
    return tuple(int(n) for n in nums) if nums else None


def compare(installed, available):
    """CURRENT / UPDATE (available is newer) / NEWER (the device has a newer
    one than what's published) / UNKNOWN (either side has no version)."""
    a, b = version_tuple(installed), version_tuple(available)
    if a is None or b is None:
        return UNKNOWN
    if a == b:
        return CURRENT
    return UPDATE if b > a else NEWER


def parse_manifest(data, assets):
    """{device: {"version", "file", "sha256", "url"}} from a firmware.json
    dict; assets maps file names to download URLs. Entries for unknown
    devices, or whose file isn't attached to the release, are skipped."""
    result = {}
    if not isinstance(data, dict):
        return result
    for device, entry in data.items():
        if device not in DEVICES or not isinstance(entry, dict):
            continue
        name = entry.get("file")
        if "version" not in entry or name not in assets:
            continue
        result[device] = {"version": entry["version"], "file": name,
                          "sha256": str(entry.get("sha256", "")).lower(), "url": assets[name]}
    return result


def fetch_manifest(http_get, api_url):
    """The latest release's firmware list ({} if it has none). http_get(url,
    timeout) returns a response usable as a context manager, like
    volgas._http_get."""
    with http_get(api_url, 10) as r:
        release = json.load(r)
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    url = assets.get(MANIFEST_ASSET)
    if not url:
        return {}
    with http_get(url, 10) as r:
        return parse_manifest(json.load(r), assets)


def download_firmware(http_get, entry, progress=lambda fraction: None):
    """The bytes of a manifest entry's file, checked against its sha256."""
    chunks, done = [], 0
    with http_get(entry["url"], 30) as r:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            done += len(chunk)
            if total:
                progress(done / total)
    image = b"".join(chunks)
    if entry["sha256"] and hashlib.sha256(image).hexdigest() != entry["sha256"]:
        raise ValueError("the download doesn't match its checksum")
    return image
