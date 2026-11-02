"""Email signature and HTML template, the last-email rules, and `harvey rewrite`."""

from datetime import datetime, timezone
from email import message_from_bytes, policy

import pytest

from harvey.agents.writer import style_problems
from harvey.config import ComplianceConfig, SignatureConfig, load_config, set_signature_value
from harvey.models.company import short_company_name
from harvey.signature import (
    logo_candidates, render_html, signature_lines, signs_as_team, with_signature,
)
from harvey.state import REWRITTEN_REASON
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg, FakeProvider, make_sender, seed_campaign, seed_prospect, state,
)

ADDRESS = "Acme Co, 100 Main St, Austin, TX 78701"
LINE = ComplianceConfig().opt_out_line


def _cfg(**sig):
    cfg = Cfg()
    cfg.signature = SignatureConfig(**sig)
    return cfg


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


# ── The signature ──

def test_signature_is_off_by_default():
    assert signature_lines(Cfg()) == []
    assert with_signature("Body?", Cfg()) == "Body?"


def test_signature_falls_back_to_the_persona():
    cfg = _cfg(enabled=True, title="Founder", phone="+1 555 0100", website="https://example.com/")
    assert signature_lines(cfg) == ["Harvey", "Founder, X", "+1 555 0100 | example.com"]


def test_signature_is_added_once():
    cfg = _cfg(enabled=True, name="Sam Lee")
    once = with_signature("Body?", cfg)
    assert once == "Body?\n\nSam Lee\nX"
    assert with_signature(once, cfg) == once


def test_a_team_name_is_flagged():
    assert signs_as_team(_cfg(enabled=True, name="Example Sales Team"))
    assert not signs_as_team(_cfg(enabled=True, name="Sam Lee"))


def test_logo_url_must_be_https():
    with pytest.raises(ValueError):
        SignatureConfig(logo_url="http://example.com/logo.png")


def test_template_names_cannot_leave_the_folder():
    with pytest.raises(ValueError):
        SignatureConfig(template="../secrets")
    assert SignatureConfig(template="Classic.html").template == "classic"


# ── The HTML version ──

def test_plain_template_has_no_html():
    assert render_html("Body?", _cfg(enabled=True)) == ""


def test_html_escapes_the_body_and_shows_the_logo():
    cfg = _cfg(enabled=True, name="Sam Lee", website="example.com",
               logo_url="https://example.com/logo.png", template="classic")
    out = render_html("Hi <b>Jo</b>,\n\nQuestion?", cfg, [ADDRESS, LINE])
    assert "&lt;b&gt;Jo&lt;/b&gt;" in out and "<b>Jo</b>" not in out
    assert "Sam Lee" in out and ADDRESS in out
    assert "{{" not in out


def test_missing_template_falls_back_to_plain():
    assert render_html("Body?", _cfg(enabled=True, template="nope")) == ""


# ── Sending ──

async def _approved(state, pid, kind="sequence", body="A short note. Question?"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="acme stock counts",
        body=body, send_at=_now_iso(), status="approved",
        campaign_id="c1" if kind == "sequence" else "", step=2, kind=kind,
    )


class HtmlProvider(FakeProvider):
    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", html=""):
        result = await super().send_email(to_email, subject, body, thread_ref, in_reply_to)
        self.sent[-1]["html"] = html
        return result


@pytest.mark.asyncio
async def test_signature_goes_between_body_and_footer(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.compliance = ComplianceConfig(postal_address=ADDRESS, opt_out_line=LINE)
    sender.config.signature = SignatureConfig(enabled=True, name="Sam Lee", website="example.com")
    await sender._drain_due()
    assert provider.sent[0]["body"] == (
        "A short note. Question?\n\nSam Lee\nX\nexample.com\n\n" + ADDRESS + "\n" + LINE)


@pytest.mark.asyncio
async def test_replies_are_signed_without_footer(state):
    pid = await seed_prospect(state, status="replied")
    await _approved(state, pid, kind="reply", body="Tuesday works. Talk then?")
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.signature = SignatureConfig(enabled=True, name="Sam Lee")
    await sender._drain_due()
    assert provider.sent[0]["body"] == "Tuesday works. Talk then?\n\nSam Lee\nX"


@pytest.mark.asyncio
async def test_an_html_template_goes_out_alongside_the_plain_text(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)
    provider = HtmlProvider()
    sender = make_sender(state, provider)
    sender.config.signature = SignatureConfig(
        enabled=True, name="Sam Lee", template="classic",
        logo_url="https://example.com/logo.png")
    await sender._drain_due()
    assert provider.sent[0]["body"].startswith("A short note. Question?\n\nSam Lee")
    assert 'src="https://example.com/logo.png"' in provider.sent[0]["html"]


@pytest.mark.asyncio
async def test_smtp_builds_a_multipart_alternative(monkeypatch):
    from harvey.integrations import smtp_mail

    captured = {}

    async def fake_send(msg, **kw):
        captured["msg"] = msg

    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", fake_send)

    class Env:
        smtp_host = "smtp.example.com"; smtp_port = 587
        smtp_username = "sam@example.com"; smtp_password = "x"

    provider = smtp_mail.SmtpImapProvider(Cfg(), Env())
    result = await provider.send_email("jo@example.net", "hello", "Plain?", html="<p>Plain?</p>")
    assert result.ok
    msg = message_from_bytes(captured["msg"].as_bytes(), policy=policy.default)
    assert msg.get_content_type() == "multipart/alternative"
    assert msg.get_body(("plain",)).get_content().strip() == "Plain?"
    assert "<p>Plain?</p>" in msg.get_body(("html",)).get_content()


# ── Finding the logo ──

PAGE = """
<header><img alt="Acme Widgets Logo" src="/_next/image?url=%2Flogo.png&amp;w=384&amp;q=75"></header>
<img alt="Partner Logo 0" src="/partner-logos/widgetco.png">
<img alt="Acme" src="/hero.jpg">
<footer><img alt="Acme Widgets Logo" src="/logo-footer.png"></footer>
<link rel="apple-touch-icon" href="/apple-icon.png">
"""


def test_logo_finder_picks_the_header_logo():
    found = logo_candidates(PAGE, "https://example.com/", "Acme Widgets")
    assert found[0] == "https://example.com/logo.png"
    assert "https://example.com/partner-logos/widgetco.png" not in found
    assert "https://example.com/logo-footer.png" not in found
    assert found[-1] == "https://example.com/apple-icon.png"


def test_logo_finder_prefers_formats_mail_clients_show():
    page = '<img src="/logo.svg" alt="logo"><img src="/brand-logo.png" alt="logo">'
    assert logo_candidates(page, "https://example.com/")[0] == "https://example.com/brand-logo.png"


# ── Saving a setting ──

def test_set_signature_value_keeps_the_rest_of_the_file(tmp_path):
    path = tmp_path / "harvey.yaml"
    from harvey.paths import PROJECT_ROOT
    text = (PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    set_signature_value("logo_url", "https://example.com/logo.png", path)
    out = path.read_text(encoding="utf-8")
    assert "logo_url: 'https://example.com/logo.png'" in out
    assert out.count("# Your signature, added under every email") == 1
    assert load_config(str(path)).signature.logo_url == "https://example.com/logo.png"


def test_set_signature_value_adds_the_section(tmp_path):
    from harvey.paths import PROJECT_ROOT
    text = (PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8")
    start = text.index("signature:")
    end = text.index("\n\n", start)
    path = tmp_path / "harvey.yaml"
    path.write_text(text[:start] + text[end + 2:], encoding="utf-8")
    set_signature_value("name", "Sam Lee", path)
    assert load_config(str(path)).signature.name == "Sam Lee"


def test_set_signature_value_refuses_bad_values(tmp_path):
    from harvey.config import ConfigError
    from harvey.paths import PROJECT_ROOT
    path = tmp_path / "harvey.yaml"
    path.write_text((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        set_signature_value("logo_url", "http://example.com/logo.png", path)
    assert path.read_text(encoding="utf-8") == before


# ── How company names and last emails read ──

@pytest.mark.parametrize("name,short", [
    ("Acme Marble & Granite Trading LLC", "Acme Marble & Granite"),
    ("Acme General Trading L.L.C.", "Acme"),
    ("Example Pvt. Ltd.", "Example"),
    ("Example FZ-LLC", "Example"),
    ("Acme Holdings PLC", "Acme Holdings"),
    ("Trading LLC", "Trading"),
    ("Acme", "Acme"),
])
def test_short_company_name(name, short):
    assert short_company_name(name) == short


def test_style_problems_catch_the_old_breakup():
    body = ("Hi there,\n\nNothing back, so this likely isn't a priority at Acme right now. "
            "Fair enough.\n\nReply here if that changes.\n\nAcme Sales Team")
    problems = style_problems("closing the loop", body)
    assert len(problems) == 3
    assert not style_problems("stock counts at acme",
                              "Jo, last note from me.\n\nWe keep stock in step with the yard. "
                              "Reply \"later\" and I'll check back.")


@pytest.mark.asyncio
async def test_sender_writes_the_short_company_name(state):
    from harvey.models.prospect import Prospect
    sender = make_sender(state, FakeProvider())
    p = Prospect(first_name="Jo", company="Acme Marble Trading LLC")
    assert sender._render("{{company}}'s yard", p) == "Acme Marble's yard"


# ── harvey rewrite ──

@pytest.mark.asyncio
async def test_rewrite_puts_unsent_prospects_back_and_leaves_the_rest(state):
    fresh = await seed_prospect(state, email="jo@acme.com", status="queued")
    emailed = await seed_prospect(state, email="sam@acme.com", status="contacted")
    campaign = await seed_campaign(state, [fresh, emailed])
    await state.update_campaign(campaign.id, status="active")
    pending = await state.add_outbox_item(
        prospect_id=fresh, campaign_id=campaign.id, step=1, to_email="jo@acme.com",
        subject="s", body="b", send_at=_now_iso(), status="pending_review")
    sent = await state.add_outbox_item(
        prospect_id=emailed, campaign_id=campaign.id, step=1, to_email="sam@acme.com",
        subject="s", body="b", send_at=_now_iso(), status="approved")
    await state.update_outbox_item(sent, status="sent")
    follow_up = await state.add_outbox_item(
        prospect_id=emailed, campaign_id=campaign.id, step=2, to_email="sam@acme.com",
        subject="s", body="b", send_at=_now_iso(), status="pending_review")
    unstaged = await seed_campaign(state, [fresh])

    plan = await state.discard_unsent_drafts(dry_run=True)
    assert plan == {"drafts": 1, "prospects": 1, "campaigns": None}
    assert (await state.get_outbox_item(pending))["status"] == "pending_review"

    done = await state.discard_unsent_drafts()
    assert done["drafts"] == 1 and done["prospects"] == 1
    item = await state.get_outbox_item(pending)
    assert item["status"] == "cancelled" and item["error"] == REWRITTEN_REASON
    assert (await state.get_prospect(fresh)).status == "new"
    # Someone already emailed keeps their sequence, and so does their campaign.
    assert (await state.get_outbox_item(follow_up))["status"] == "pending_review"
    assert (await state.get_prospect(emailed)).status == "contacted"
    assert [c.id for c in await state.get_campaigns_by_status("active")] == [campaign.id]
    # A campaign never staged can't be staged later with the old copy.
    assert await state.get_campaigns_by_status("draft") == []
    assert [c.id for c in await state.get_campaigns_by_status("rewritten")] == [unstaged.id]


def test_the_signature_website_counts_as_your_own_domain():
    from harvey.gate import allowed_link_domains
    assert "example.org" not in allowed_link_domains(_cfg(website="example.org"))
    assert "example.org" in allowed_link_domains(_cfg(enabled=True, website="https://example.org/"))


# ── The Writer rewrites a draft that breaks the last-email rules ──

class _ScriptedBrain:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


def _writer_config():
    from types import SimpleNamespace
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="instantly", require_approval=False)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="Sam Lee", company="C", role="R", tone="t"),
    )


BAD = [
    {"step": 1, "subject": "acme yard", "body": "{{first_name}}, how do you count slabs?", "delay_days": 0},
    {"step": 2, "subject": "stock", "body": "{{first_name}}, we sync stock. Useful?", "delay_days": 3},
    {"step": 3, "subject": "closing the loop",
     "body": "Hi there,\n\nNothing back, fair enough.\n\nC Sales Team", "delay_days": 4},
]
FIXED = BAD[:2] + [
    {"step": 3, "subject": "stock at {{company}}",
     "body": "{{first_name}}, last note from me. We keep stock in step with the yard. "
             "Reply \"later\" and I'll check back.", "delay_days": 4},
]


@pytest.mark.asyncio
async def test_writer_rewrites_a_breakup_that_breaks_the_rules(state):
    from harvey.agents.writer import Writer
    brain = _ScriptedBrain(BAD, FIXED)
    writer = Writer(brain, state, _writer_config(), None)
    writer.skills = ""
    pid = await seed_prospect(state)
    steps = await writer._write_sequence([await state.get_prospect(pid)])
    assert steps[2].subject == "stock at {{company}}"
    assert len(brain.prompts) == 2 and "Email 3:" in brain.prompts[1]
    assert "Sign off with just the sender's first name (Sam)" in brain.prompts[0]


@pytest.mark.asyncio
async def test_writer_keeps_the_draft_when_the_rewrite_is_no_better(state):
    from harvey.agents.writer import Writer
    brain = _ScriptedBrain(BAD, None)
    writer = Writer(brain, state, _writer_config(), None)
    writer.skills = ""
    pid = await seed_prospect(state)
    steps = await writer._write_sequence([await state.get_prospect(pid)])
    assert [s.subject for s in steps] == [b["subject"] for b in BAD]
