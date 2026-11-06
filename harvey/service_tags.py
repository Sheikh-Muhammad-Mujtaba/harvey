"""Which of your services each lead is for.

``icp.service_tags`` maps a tag to the business categories it fits, e.g.

    service_tags:
      ERP: [wholesale, works, pharmacy]
      Digital marketing: [estate_agent, dentist, restaurant]

A company gets every tag whose list holds its category (companies.industry,
compared case-insensitively), so a pharmacy can be both. A contact gets its
company's tags, or its own industry's when it has no company. Tags are
worked out when read, never stored, so changing the mapping re-tags every
lead at once. The writer is told which service to lead with, and the
dashboard and CSV export show the tags.
"""


def _mapping(config) -> dict[str, list[str]]:
    icp = getattr(config, "icp", None)
    return dict(getattr(icp, "service_tags", None) or {})


def tags_for(category: str, config) -> list[str]:
    """The tags for one category, in the order the mapping lists them."""
    key = (category or "").strip().lower()
    if not key:
        return []
    return [tag for tag, cats in _mapping(config).items()
            if key in {str(c).strip().lower() for c in cats or []}]


def tags_for_prospect(prospect_industry: str, company_industry: str, config) -> list[str]:
    return tags_for(company_industry or prospect_industry, config)
