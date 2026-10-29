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

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("PORTAL_SECRET")
        if not secret:
            raise RuntimeError("PORTAL_SECRET must be set: it peppers the username/student-id hashes and must be stable")
        admins = tuple(a.strip() for a in os.environ.get("PORTAL_ADMINS", "").split(",") if a.strip())
        return cls(secret=secret, admins=admins, store=os.environ.get("PORTAL_STORE", "memory"))
