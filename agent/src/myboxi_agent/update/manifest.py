"""Fetch and verify the channel manifest (SPEC v0.7 §11.1).

The signature is Ed25519 over the manifest's bytes, checked with the ``openssl`` command
(OpenSSL 3, part of the image) against every key in the trusted keys directory, so the box
needs no extra Python dependency for it.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import httpx
from pydantic import ValidationError

from myboxi_protocol.updates import UpdateManifest

MAX_MANIFEST_BYTES = 64 * 1024


class ManifestError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def verify_signature(data: bytes, signature: bytes, keys_dir: Path) -> bool:
    keys = sorted(keys_dir.glob("*.pem"))
    with tempfile.TemporaryDirectory() as tmp:
        data_path, sig_path = Path(tmp) / "manifest.json", Path(tmp) / "manifest.json.sig"
        data_path.write_bytes(data)
        sig_path.write_bytes(signature)
        for key in keys:
            result = subprocess.run(  # noqa: S603 - fixed argument list
                ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key), "-rawin",  # noqa: S607
                 "-in", str(data_path), "-sigfile", str(sig_path)],
                capture_output=True,
                timeout=30,
                check=False,
            )  # fmt: skip
            if result.returncode == 0:
                return True
    return False


async def _get(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    if len(response.content) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest_too_large")
    return response.content


async def fetch_manifest(client: httpx.AsyncClient, url: str, keys_dir: Path) -> UpdateManifest:
    """Raises httpx.HTTPError when offline and ManifestError when the manifest is not valid."""
    data = await _get(client, url)
    signature = await _get(client, url + ".sig")
    if not verify_signature(data, signature, keys_dir):
        raise ManifestError("bad_signature")
    try:
        return UpdateManifest.model_validate_json(data)
    except ValidationError:
        raise ManifestError("bad_manifest") from None
