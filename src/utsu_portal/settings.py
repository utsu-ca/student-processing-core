from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

FORMS_DIR = Path(__file__).parent / "forms"


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

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("PORTAL_SECRET")
        if not secret:
            raise RuntimeError("PORTAL_SECRET must be set: it peppers the username/student-id hashes and must be stable")
        admins = tuple(a.strip() for a in os.environ.get("PORTAL_ADMINS", "").split(",") if a.strip())
        env = os.environ.get
        return cls(secret=secret, admins=admins, store=env("PORTAL_STORE", "memory"),
                   sso_issuer=env("PORTAL_BASE_URL", "http://localhost:8000").rstrip("/"), sso_key_pem=env("PORTAL_SSO_KEY"),
                   zoom_client_secret=env("PORTAL_ZOOM_CLIENT_SECRET"), zoom_url=env("PORTAL_ZOOM_URL", ""),
                   zoom_sso_url=env("PORTAL_ZOOM_SSO_URL", ""),
                   zoom_redirect_uris=tuple(u for u in env("PORTAL_ZOOM_REDIRECT_URIS", "").split(",") if u),
                   agm_verify_mode=env("PORTAL_AGM_VERIFY_MODE", "manual"))
