"""Locally generated TLS credentials; never installs a global trust root."""

from __future__ import annotations

import ipaddress
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def prepare_certificate(folder: Path, address: str) -> tuple[Path, Path]:
    ip = ipaddress.ip_address(address)
    if ip.version != 4 or ip.is_unspecified or ip.is_multicast:
        raise ValueError("请选择具体的本机 IPv4 地址")
    folder.mkdir(parents=True, exist_ok=True)
    certificate, private_key = folder / f"{ip}.pem", folder / f"{ip}.key"
    if certificate.exists() and private_key.exists():
        cert = x509.load_pem_x509_certificate(certificate.read_bytes())
        if cert.not_valid_after_utc > datetime.now(timezone.utc) + timedelta(days=7):
            check = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            check.load_cert_chain(certificate, private_key)
            return certificate, private_key
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TF LabControl " + address)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    temporary = private_key.with_suffix(".key.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    os.replace(temporary, private_key)
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return certificate, private_key
