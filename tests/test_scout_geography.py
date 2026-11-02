"""Web-search discovery keeps only companies whose own site names a target area."""

import asyncio
import types

from harvey.agents.scout import Scout


def _scout(geography, pages):
    scout = Scout.__new__(Scout)
    scout.config = types.SimpleNamespace(icp=types.SimpleNamespace(geography=geography))

    async def read_site(domain):
        return {"base": f"https://{domain}", "pages": pages.get(domain, [])}
    scout._read_site = read_site
    return scout


GEO = ["Exampleton, United Kingdom", "Sampleville, United Arab Emirates"]


def _check(scout, domain, html=""):
    return asyncio.run(scout._in_geography(domain, html))


def test_site_naming_a_target_city_passes():
    s = _scout(GEO, {"a.example.com": [("https://a.example.com/", "Visit our Exampleton showroom")]})
    assert _check(s, "a.example.com")


def test_country_short_forms_count():
    s = _scout(GEO, {"b.example.com": [("https://b.example.com/contact", "Offices across the UAE")],
                     "c.example.com": [("https://c.example.com/", "Delivering across England")]})
    assert _check(s, "b.example.com") and _check(s, "c.example.com")


def test_site_elsewhere_is_rejected():
    s = _scout(GEO, {"d.example.com": [("https://d.example.com/", "Proudly serving Springfield, Ohio")]})
    assert not _check(s, "d.example.com")


def test_partial_words_dont_match():
    # "UK" inside "Ukulele" or "Sampleville" inside a longer word isn't a place.
    s = _scout(GEO, {"e.example.com": [("https://e.example.com/", "Ukulele lessons at Sampleville2000 Online")]})
    assert not _check(s, "e.example.com")


def test_homepage_html_alone_is_enough():
    assert _check(_scout(GEO, {}), "f.example.com", "<p>Sampleville branch</p>")


def test_no_geography_means_no_check():
    assert _check(_scout([], {}), "g.example.com")
