from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

FORMS_DIR = Path(__file__).parent / "forms"


def env_secret(name: str, default: str | None = None) -> str | None:
    """Read NAME, or the file named by NAME_FILE (Kubernetes/Docker secret mounts keep values out of env and manifests)."""
    path = os.environ.get(name + "_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default)


@dataclass
class Settings:
    secret: str = field(default_factory=lambda: secrets.token_hex(32))
    admins: tuple[str, ...] = ()  # usernames bootstrapped as admins
    store: str = "memory"  # memory | sqlite:<path> | firestore
    token_ttl: int = 8 * 3600
    otp_ttl: int = 600
    otp_attempts: int = 5
    max_image_bytes: int = 1_500_000
    division_codes: tuple[str, ...] = ("UTSG", "UTM", "UTSC")
    seed_forms: bool = True
    club_registration_approvals: int = 2  # distinct admins needed to approve a club
    expense_approvals_small: int = 1
    expense_approvals_large: int = 2
    expense_large_cents: int = 50_000  # expenses at/above this need the larger approval count
    default_tax_bps: int = 1300  # Ontario HST 13%
    max_receipt_bytes: int = 3_000_000
    sso_issuer: str = "http://localhost:8000"
    sso_key_pem: str | None = None  # RSA private key; an ephemeral one is generated when unset
    zoom_client_id: str = "zoom"
    zoom_client_secret: str | None = None  # SSO client secret shared with Zoom; zoom client is not registered when unset
    zoom_redirect_uris: tuple[str, ...] = ()
    zoom_url: str = ""  # meeting link handed to verified online attendees
    zoom_sso_url: str = ""  # Zoom vanity-URL SSO start (SP-initiated login)
    agm_verify_mode: str = "manual"  # manual (admin reviews scan) | auto
    agm_grant_ttl: int = 12 * 3600
    redis_url: str | None = None  # shared realtime bus + rate limits across replicas; in-process when unset
    data_key: str | None = None  # Fernet key(s), comma separated, for encrypting scans/receipts/audit; derived from secret if unset
    rate_limit_auth: int = 30  # requests per window per client IP on auth/recovery/token endpoints (0 disables)
    rate_window: int = 60
    max_body_bytes: int = 8_000_000
    hsts: bool = True
    auth_signing_key: str | None = None  # Ed25519 PEM held ONLY by the auth service (derived from secret when unset)
    auth_url: str | None = None  # set => portal runs against a separate auth service
    auth_public_key: str | None = None  # PEM public key(s) of the auth service, used to verify sessions
    service_token: str | None = None  # shared by auth service and portal for the internal API (>= 32 chars)

    @classmethod
    def from_env(cls) -> "Settings":
        env = env_secret
        secret = env("PORTAL_SECRET")
        if not secret:
            raise RuntimeError("PORTAL_SECRET must be set: it peppers the username/student-id hashes and must be stable")
        if len(secret) < 32:
            raise RuntimeError("PORTAL_SECRET must be at least 32 characters (e.g. `openssl rand -hex 32`)")
        admins = tuple(a.strip() for a in (env("PORTAL_ADMINS") or "").split(",") if a.strip())
        return cls(secret=secret, admins=admins, store=env("PORTAL_STORE", "memory"),
                   sso_issuer=env("PORTAL_BASE_URL", "http://localhost:8000").rstrip("/"), sso_key_pem=env("PORTAL_SSO_KEY"),
                   zoom_client_secret=env("PORTAL_ZOOM_CLIENT_SECRET"), zoom_url=env("PORTAL_ZOOM_URL", ""),
                   zoom_sso_url=env("PORTAL_ZOOM_SSO_URL", ""),
                   zoom_redirect_uris=tuple(u for u in env("PORTAL_ZOOM_REDIRECT_URIS", "").split(",") if u),
                   agm_verify_mode=env("PORTAL_AGM_VERIFY_MODE", "manual"),
                   token_ttl=int(env("PORTAL_TOKEN_TTL", str(8 * 3600))),
                   auth_signing_key=env("PORTAL_AUTH_SIGNING_KEY"), auth_url=env("PORTAL_AUTH_URL"),
                   auth_public_key=env("PORTAL_AUTH_PUBLIC_KEY"), service_token=env("PORTAL_SERVICE_TOKEN"),
                   redis_url=env("PORTAL_REDIS_URL"), data_key=env("PORTAL_DATA_KEY"),
                   rate_limit_auth=int(env("PORTAL_RATE_LIMIT_AUTH", "30")), hsts=env("PORTAL_HSTS", "1") != "0")
