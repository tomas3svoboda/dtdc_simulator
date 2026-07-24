"""OPC UA security + certificate/PKI management (Phase 3).

Two security modes are offered from the GUI:

  * ``NONE``           — anonymous, no encryption (sandbox default, as before).
  * ``BASIC256SHA256`` — Basic256Sha256 Sign & Encrypt. The server presents a
    self-signed certificate (generated on demand into ``pki/own``); the client
    trusts that public certificate to establish an encrypted session.

Client-certificate trust is optional: upload client public certs into
``pki/trusted`` and enable ``require_client_trust`` to reject un-trusted clients
(asyncua ``CertificateValidator`` over a ``TrustStore``). Certificate generation
uses asyncua's ``cert_gen`` (thin wrapper over ``cryptography``), so no new
dependency is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from asyncua.crypto import cert_gen
from asyncua.crypto.truststore import TrustStore
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from cryptography.x509.oid import ExtendedKeyUsageOID

APP_URI = "urn:dtdc:simulator:server"


class SecurityMode(str, Enum):
    NONE = "None"
    BASIC256SHA256 = "Basic256Sha256"


@dataclass(frozen=True)
class PkiPaths:
    """Layout of the on-disk PKI store (default ``./pki``)."""

    root: Path

    @classmethod
    def at(cls, root: str | Path = "pki") -> "PkiPaths":
        return cls(Path(root))

    @property
    def own_dir(self) -> Path:
        return self.root / "own"

    @property
    def trusted_dir(self) -> Path:
        return self.root / "trusted"

    @property
    def rejected_dir(self) -> Path:
        return self.root / "rejected"

    @property
    def own_cert(self) -> Path:
        return self.own_dir / "server_cert.der"

    @property
    def own_key(self) -> Path:
        return self.own_dir / "server_key.pem"

    def ensure_dirs(self) -> None:
        for d in (self.own_dir, self.trusted_dir, self.rejected_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass
class SecurityConfig:
    """What ``OpcUaAdapter.build`` needs to configure the server's security."""

    mode: SecurityMode = SecurityMode.NONE
    cert_file: Path | None = None
    key_file: Path | None = None
    validator: CertificateValidator | None = None


async def ensure_server_certificate(
    pki: PkiPaths, app_uri: str = APP_URI, host_name: str = "localhost"
) -> tuple[Path, Path]:
    """Generate the server's self-signed cert (DER) + private key (PEM) into
    ``pki/own`` if they don't already exist. Returns ``(cert_file, key_file)``."""
    pki.ensure_dirs()
    await cert_gen.setup_self_signed_certificate(
        key_file=pki.own_key,
        cert_file=pki.own_cert,
        app_uri=app_uri,
        host_name=host_name,
        cert_use=[ExtendedKeyUsageOID.SERVER_AUTH],
        subject_attrs={
            "countryName": "XX",
            "organizationName": "DTDC Simulator",
            "commonName": "DTDC Simulator OPC UA Server",
        },
    )
    return pki.own_cert, pki.own_key


def list_trusted(pki: PkiPaths) -> list[str]:
    """File names of the currently trusted client certificates."""
    if not pki.trusted_dir.exists():
        return []
    return sorted(p.name for p in pki.trusted_dir.iterdir() if p.is_file())


def save_trusted_cert(pki: PkiPaths, filename: str, data: bytes) -> Path:
    """Store an uploaded client public certificate in the trust store."""
    pki.ensure_dirs()
    safe = Path(filename).name  # never let an upload escape the trusted dir
    if not safe:
        raise ValueError("empty certificate file name")
    dest = pki.trusted_dir / safe
    dest.write_bytes(data)
    return dest


def _trust_validator(pki: PkiPaths) -> CertificateValidator | None:
    trusted = [p for p in pki.trusted_dir.glob("*") if p.is_file()]
    if not trusted:
        return None
    store = TrustStore(trust_locations=[pki.trusted_dir], crl_locations=[])
    options = CertificateValidatorOptions.TRUSTED | CertificateValidatorOptions.PEER_CLIENT
    return CertificateValidator(options, store)


async def make_security_config(
    mode: SecurityMode,
    pki: PkiPaths,
    *,
    require_client_trust: bool = False,
    app_uri: str = APP_URI,
    host_name: str = "localhost",
) -> SecurityConfig:
    """Assemble the security configuration for the given mode, generating the
    server certificate on demand for the encrypted mode."""
    if mode is SecurityMode.NONE:
        return SecurityConfig(mode=SecurityMode.NONE)

    cert_file, key_file = await ensure_server_certificate(pki, app_uri, host_name)
    validator = _trust_validator(pki) if require_client_trust else None
    if validator is not None:
        await validator.trust_store.load()
    return SecurityConfig(
        mode=SecurityMode.BASIC256SHA256,
        cert_file=cert_file,
        key_file=key_file,
        validator=validator,
    )
