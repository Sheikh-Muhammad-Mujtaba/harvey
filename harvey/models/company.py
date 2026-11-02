"""Company data model."""

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


# Registration words nobody says out loud. Writing "Acme Trading LLC" in an
# email tells the reader it was merged in from a list.
_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(?:l\.?l\.?c\.?|fz[\s-]?llc|fz[\s-]?co|fze|dmcc|ltd\.?|limited|plc|llp|"
    r"inc\.?|incorporated|corp\.?|corporation|co\.?|pvt\.?|private|gmbh|s\.?a\.?|ag|b\.?v\.?|"
    r"est\.?|establishment|(?:general\s+)?trading|&\s*co\.?)$",
    re.IGNORECASE)


def short_company_name(name: str) -> str:
    """How a person writes a company's name: "Acme Marble Trading LLC" ->
    "Acme Marble". Never shortens a name to nothing."""
    full = " ".join((name or "").split())
    short = full
    while True:
        cut = _LEGAL_SUFFIX_RE.sub("", short).rstrip(" ,.-&")
        if cut == short or len(cut) < 2:
            break
        short = cut
    return short or full


def _utcnow() -> datetime:
    """Naive UTC now (consistent with DB storage; avoids deprecated utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Company(BaseModel):
    id: str = ""
    name: str = ""
    domain: str = ""
    website: str = ""
    description: str = ""
    industry: str = ""
    company_size: str = ""
    location: str = ""
    phone: str = ""
    source: str = ""          # how we found them: google_dork, linkedin, company_scrape
    source_url: str = ""      # specific URL where we found the info
    # Provider-stable identity ("dataforseo:ChIJ...", "osm:node/123"). The
    # dedup key for businesses that have no website — which are precisely the
    # best prospects for anyone selling one.
    external_id: str = ""
    notes: str = ""
    # Buying signals: detected tools on their site, and timely triggers
    # (hiring, funding, ...) as [{"type": ..., "detail": ..., "found_at": ...}]
    tech_stack: list[str] = Field(default_factory=list)
    signals: list[dict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        """Domains are dedup keys — normalize case/whitespace."""
        return (v or "").strip().lower()
