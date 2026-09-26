"""An own CA and a server certificate signed by it, made with the openssl command line (like
docs/selbst-hosten.md does). Tests using it are skipped without openssl."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def available() -> bool:
    return shutil.which("openssl") is not None


@dataclass(frozen=True)
class OwnCerts:
    ca: Path
    cert: Path
    key: Path


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


def make(tmp: Path, host: str = "localhost") -> OwnCerts:
    tmp.mkdir(parents=True, exist_ok=True)
    ca, ca_key = tmp / "ca.pem", tmp / "ca.key"
    cert, key, csr = tmp / "server.pem", tmp / "server.key", tmp / "server.csr"
    ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    _openssl("req", "-x509", *ec, "-keyout", str(ca_key), "-out", str(ca), "-days", "30",
             "-subj", "/CN=Myboxi test CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign")  # fmt: skip
    _openssl("req", *ec, "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={host}")
    ext = tmp / "server.ext"
    ext.write_text(
        f"subjectAltName=DNS:{host},IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
    )
    _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key),
             "-CAcreateserial", "-out", str(cert), "-days", "30", "-extfile", str(ext))  # fmt: skip
    return OwnCerts(ca, cert, key)
