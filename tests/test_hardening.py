import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_portal import login, register  # noqa: E402

from utsu_portal import create_app
from utsu_portal.formdef import safe_pattern
from utsu_portal.mailer import OutboxMailer
from utsu_portal.settings import Settings


def boot(**kw):
    mail = OutboxMailer()
    return TestClient(create_app(Settings(secret="s" * 40, admin_emails=("boss@x.ca",), **kw), mailer=mail)), mail


def role(c, h):
    return c.get("/api/me", headers=h).json()["status"]


def test_username_alone_never_grants_admin_but_allowlisted_email_does():
    c, mail = boot()
    register(c, mail, "boss", "evil@x.ca", sn="1111111111")  # squats the 'obvious' username
    h = login(c, mail, "boss")
    assert role(c, h) == "pending_review"
    assert c.get("/api/admin/verifications", headers=h).status_code == 403
    register(c, mail, "realadmin", "BOSS@x.ca", sn="2222222222")
    h2 = login(c, mail, "realadmin")
    assert role(c, h2) == "verified" and c.get("/api/admin/verifications", headers=h2).status_code == 200


def test_otp_resend_cannot_reset_guess_budget_and_has_cooldown():
    c, mail = boot()
    register(c, mail, "ada", "ada@x.ca")
    c.post("/api/auth/login", json={"username": "ada"})
    n = len(mail.outbox)
    c.post("/api/auth/login", json={"username": "ada"})  # inside cooldown: silently throttled
    assert len(mail.outbox) == n
    ctx = c.app.state.ctx
    for _ in range(5):
        assert c.post("/api/auth/token", json={"username": "ada", "code": "000000"}).status_code == 400
    t = ctx.now()
    ctx.now = lambda: t + 120  # cooldown over, but the window and its wrong guesses persist
    c.post("/api/auth/login", json={"username": "ada"})
    assert len(mail.outbox) == n
    ctx.now = lambda: t + 2000  # new window
    c.post("/api/auth/login", json={"username": "ada"})
    assert len(mail.outbox) == n + 1


def test_otp_sends_capped_per_window():
    c, mail = boot(otp_cooldown=0)
    register(c, mail, "ada", "ada@x.ca")
    n = len(mail.outbox)
    for _ in range(9):
        c.post("/api/auth/login", json={"username": "ada"})
    assert len(mail.outbox) - n == 5


def test_recovery_limited_per_username_and_uniform():
    c, mail = boot(rate_limit_auth=0)
    register(c, mail, "ada", "ada@x.ca")
    codes = []
    for i in range(7):
        r = c.post("/api/recovery/email-otp", json={"username": "ada", "student_number_trunc": f"{i:04d}", "new_email": "n@x.ca"})
        codes.append(r.status_code)
        if r.status_code == 200:
            assert "if the details match" in r.json()["detail"]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    # the correct digits are refused too once the budget is spent
    assert c.post("/api/recovery/start", json={"username": "ada", "student_number_trunc": "7890"}).status_code == 429


@pytest.mark.parametrize("p", [r"(a+)+$", r"(a|aa)+", r"(a*)*", r"(.*)\1", r"(?=x)a", r"a{1,100000}", r"(\d+)+x", r"(?P<n>a)", r"(?i)a", "x" * 201, r"(a)*", r"a{1,300}"])
def test_dangerous_patterns_rejected(p):
    with pytest.raises((ValueError, Exception)):
        safe_pattern(p)


@pytest.mark.parametrize("p", [r"[A-Z]{2}\d{4}", r"\d{3}-\d{4}", r"[a-z]+@utoronto\.ca", r"(ab)c?", r"(?:ab|cd)x", r"[)*+]{2}", r"\(\)*"])
def test_ordinary_patterns_ok(p):
    assert safe_pattern(p).pattern == p


def test_pattern_is_checked_by_re2_on_submit_and_publish():
    from utsu_portal import formdef
    assert type(safe_pattern(r"\d+")).__module__.startswith("re2")
    base = {"id": "t", "title": "T", "version": 1, "fields": [{"id": "a", "type": "text", "label": "A", "pattern": r"(a+)+$"}]}
    with pytest.raises(formdef.FormError, match="pattern"):
        formdef.validate_schema(base)
