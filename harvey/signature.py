"""Email signature and HTML template.

Every email leaves as plain text: that is what the gate checks and what
most cold mail should be. With ``signature.enabled`` a short signature goes
under the body (above the compliance footer). With a ``signature.template``
other than "plain", the same text also goes out as an HTML alternative built
from a file in ``email_templates/``, so the logo and layout show in clients
that render HTML while the plain part stays exactly what was reviewed.

The HTML is built here from config and the reviewed text, never from model
output, so nothing a prospect's website says can add markup to it.
"""

import html
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.signature")

TEMPLATES_DIR = PROJECT_ROOT / "email_templates"
PLAIN = "plain"

# Names that sign as a department rather than a person. People reply to people.
_TEAM_NAME_RE = re.compile(
    r"\b(team|sales|support|info|admin|office|desk|department|marketing|hello|noreply)\b",
    re.IGNORECASE)


def signature_of(config):
    from harvey.config import SignatureConfig
    found = getattr(config, "signature", None)
    return found if isinstance(found, SignatureConfig) else SignatureConfig()


def signature_fields(config) -> dict[str, str]:
    """Name, title, company, phone and website, with persona fallbacks."""
    sig = signature_of(config)
    persona = getattr(config, "persona", None)
    website = re.sub(r"^https?://", "", sig.website, flags=re.IGNORECASE).rstrip("/")
    return {
        "name": sig.name or getattr(persona, "name", "") or "",
        "title": sig.title,
        "company": sig.company or getattr(persona, "company", "") or "",
        "phone": sig.phone,
        "website": website,
        "logo_url": sig.logo_url,
    }


def signs_as_team(config) -> bool:
    """The signature would be signed by a team, not a person."""
    return bool(_TEAM_NAME_RE.search(signature_fields(config)["name"]))


def signature_lines(config) -> list[str]:
    """The plain-text signature, one line each; [] when it's off."""
    if not signature_of(config).enabled:
        return []
    f = signature_fields(config)
    role = ", ".join(x for x in (f["title"], f["company"]) if x)
    contact = " | ".join(x for x in (f["phone"], f["website"]) if x)
    return [line for line in (f["name"], role, contact) if line]


def signature_text(config) -> str:
    return "\n".join(signature_lines(config))


def with_signature(body: str, config) -> str:
    """``body`` with the signature underneath, unless it's off or already there."""
    sig = signature_text(config)
    if not sig or sig in body:
        return body
    return body.rstrip() + "\n\n" + sig


def template_path(name: str):
    return TEMPLATES_DIR / f"{name}.html"


def _paragraphs(text: str) -> str:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    return "\n".join(
        '<p style="margin:0 0 14px 0">' + html.escape(b).replace("\n", "<br>") + "</p>"
        for b in blocks)


def render_html(body: str, config, footer_lines=()) -> str:
    """The HTML version of an email whose plain text is ``body`` (without
    signature or footer), or "" when the template is plain or missing."""
    sig = signature_of(config)
    if not sig.enabled or sig.template == PLAIN:
        return ""
    path = template_path(sig.template)
    try:
        template = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning(f"Email template {path.name} not found; sending plain text only.")
        return ""
    f = signature_fields(config)
    esc = {k: html.escape(v, quote=True) for k, v in f.items()}
    website_url = ("https://" + f["website"]) if f["website"] else ""
    logo_html = (
        f'<img src="{esc["logo_url"]}" alt="{esc["company"]}" width="140" '
        f'style="display:block;width:140px;max-width:140px;height:auto;border:0">'
        if f["logo_url"] else "")
    website_html = (
        f'<a href="{html.escape(website_url, quote=True)}" '
        f'style="color:inherit;text-decoration:none">{esc["website"]}</a>'
        if website_url else "")
    contact = " &nbsp;|&nbsp; ".join(x for x in (esc["phone"], website_html) if x)
    values = {
        "body_html": _paragraphs(body),
        "name": esc["name"],
        "title": esc["title"],
        "company": esc["company"],
        "role_line": ", ".join(x for x in (esc["title"], esc["company"]) if x),
        "contact_line": contact,
        "logo_html": logo_html,
        "footer_html": "<br>".join(html.escape(line) for line in footer_lines if line),
    }
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1), ""), template)


# --- Finding the logo on a website -------------------------------------------

_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ICON_RE = re.compile(r"<link\b[^>]*rel=[\"'][^\"']*apple-touch-icon[^\"']*[\"'][^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
# Images that sit near a logo but are someone else's or the wrong colours.
_NOT_OURS = ("partner", "client", "customer", "award", "sponsor", "certif",
             "badge", "trust", "footer", "white", "invert", "testimonial")
# Formats every major mail client shows. SVG isn't one (Gmail drops it).
_EMAIL_SAFE = (".png", ".jpg", ".jpeg", ".gif")


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
            for m in _ATTR_RE.finditer(tag)}


def _unwrap(src: str) -> str:
    """Image optimisers (Next.js /_next/image?url=...) hide the real file."""
    parsed = urlparse(src)
    if parsed.path.endswith("/_next/image") or "/_next/image" in parsed.path:
        inner = parse_qs(parsed.query).get("url")
        if inner:
            return inner[0]
    return src


def logo_candidates(page_html: str, base_url: str, company: str = "") -> list[str]:
    """Absolute URLs of likely logos on a page, best first."""
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) > 2]
    scored: list[tuple[int, int, str]] = []
    for index, tag in enumerate(_IMG_RE.findall(page_html or "")):
        a = _attrs(tag)
        src = _unwrap(a.get("src") or "")
        if not src or src.startswith("data:"):
            continue
        text = " ".join((a.get("alt", ""), a.get("class", ""), a.get("id", ""), src)).lower()
        if "logo" not in text:
            continue
        if any(bad in text for bad in _NOT_OURS):
            continue
        score = 3
        alt = a.get("alt", "").lower()
        if words and any(w in alt.replace(" ", "") or w in alt for w in words):
            score += 3
        if index < 5:
            score += 2
        if not urlparse(src).path.lower().endswith(_EMAIL_SAFE):
            score -= 4
        scored.append((score, -index, urljoin(base_url, src)))
    for tag in _ICON_RE.findall(page_html or ""):
        href = _attrs(tag).get("href")
        if href:
            scored.append((0, -10_000, urljoin(base_url, href)))
    scored.sort(reverse=True)
    return list(dict.fromkeys(url for _, _, url in scored))


async def find_logo(site_url: str, company: str = "", client=None) -> str:
    """The https URL of the site's logo, checked to be an image a mail client
    shows, or "" when none is found."""
    from harvey.public_http import public_http_client

    if not re.match(r"^https?://", site_url, re.IGNORECASE):
        site_url = "https://" + site_url
    own = client is None
    client = client or public_http_client(timeout=15.0, headers={"User-Agent": "Mozilla/5.0"})
    try:
        page = await client.get(site_url)
        if page.status_code >= 400:
            return ""
        for url in logo_candidates(page.text, str(page.url), company)[:6]:
            if not url.lower().startswith("https://"):
                continue
            try:
                r = await client.get(url)
            except Exception:
                continue
            kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if r.status_code == 200 and kind in ("image/png", "image/jpeg", "image/gif"):
                return str(r.url)
        return ""
    finally:
        if own:
            await client.aclose()
