"""icp.service_tags: which service each lead is for, from its company's category."""

import asyncio
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

import harvey.config as config_mod
import harvey.dashboard as dash
from harvey import service_tags
from harvey.export import collect_prospects

MAPPING = {"ERP": ["wholesale", "Pharmacy"], "Digital marketing": ["dentist", "pharmacy"]}


def _config(mapping=MAPPING):
    return types.SimpleNamespace(icp=types.SimpleNamespace(service_tags=mapping))


def test_tags_follow_the_mapping_case_insensitively():
    cfg = _config()
    assert service_tags.tags_for("Wholesale", cfg) == ["ERP"]
    assert service_tags.tags_for("pharmacy", cfg) == ["ERP", "Digital marketing"]
    assert service_tags.tags_for("hospital", cfg) == []
    assert service_tags.tags_for("", cfg) == []


def test_no_mapping_means_no_tags():
    assert service_tags.tags_for("wholesale", _config({})) == []
    assert service_tags.tags_for("wholesale", None) == []


def test_contact_takes_its_companys_category_first():
    cfg = _config()
    assert service_tags.tags_for_prospect("dentist", "wholesale", cfg) == ["ERP"]
    assert service_tags.tags_for_prospect("dentist", "", cfg) == ["Digital marketing"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: _config())
    state = dash._state()
    asyncio.run(state.init_db())
    con = sqlite3.connect(dash.DB_PATH)
    con.execute("INSERT INTO companies (id, name, industry) VALUES ('c1', 'Acme Supplies', 'wholesale')")
    con.execute("INSERT INTO prospects (id, company_id, email, email_status, title, source, industry) "
                "VALUES ('p1', 'c1', 'info@acme.example.com', 'verified', 'General inbox', "
                "'company_inbox', 'dentist')")
    con.commit()
    con.close()
    return state


def test_dashboard_lists_carry_the_tags(env):
    auth = {"Authorization": f"Bearer {dash.token_store.get()}"}
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        companies = c.get("/api/companies", headers=auth).json()
        prospects = c.get("/api/prospects", headers=auth).json()
    assert companies[0]["service_tags"] == ["ERP"]
    assert prospects[0]["service_tags"] == ["ERP"]
    assert "company_industry" not in prospects[0]


def test_export_has_a_service_column(env):
    rows = asyncio.run(collect_prospects(env, config=_config()))
    assert rows[0]["service"] == "ERP"
