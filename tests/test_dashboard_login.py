"""Dashboard sign-in (harvey/dashboard_login.py) and public-host access.

Runs against throwaway paths: .env and the lockout file live in tmp_path.
Covers: a session token from the right credentials unlocks the API, wrong
ones count down to a lockout of the IP and the device, a correct password
is refused while locked, the lock expires, sign-out revokes the session,
X-Real-IP is trusted only from loopback, and the public hostname is
accepted with an https origin while other hosts stay refused.
"""

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import dashboard_login as login

USER = "operator"
PASSWORD = "correct horse battery"
PUBLIC = "dash.example.test"
LOCAL = "http://127.0.0.1:5555"
PROXY = ("127.0.0.1", 40000)  # nginx, as the dashboard sees it


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    # Another test may have loaded a real .env into the process environment.
    for key in (login.USER_KEY, login.HASH_KEY, "HARVEY_DASHBOARD_PUBLIC_HOST"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"HARVEY_DASHBOARD_USER={USER}\n"
        f"HARVEY_DASHBOARD_PASSWORD_HASH={login.hash_password(PASSWORD, iterations=1000)}\n"
        f"HARVEY_DASHBOARD_PUBLIC_HOST={PUBLIC}\n")
    monkeypatch.setattr(dash, "ENV_FILE", env_file)
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    monkeypatch.setattr(dash.login_limiter, "path", tmp_path / "data" / "login-guard.json")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    return tmp_path


def _client(base_url=LOCAL, client=("testclient", 50000)):
    return TestClient(dash.app, base_url=base_url, client=client)


def _sign_in(c, password=PASSWORD, device="device-aaaaaaaaaaaa", ip=None, username=USER):
    headers = {"X-Harvey-Device": device}
    if ip:
        headers["X-Real-IP"] = ip
    return c.post("/api/login", json={"username": username, "password": password},
                  headers=headers)


def test_right_credentials_unlock_the_api(env):
    with _client() as c:
        assert c.get("/api/harvey/status").status_code == 401
        r = _sign_in(c)
        assert r.status_code == 200
        token = r.json()["token"]
        ok = c.get("/api/harvey/status", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200


def test_three_wrong_passwords_lock_out_even_the_right_one(env):
    with _client() as c:
        first = _sign_in(c, "wrong")
        assert first.status_code == 401 and "2 attempts left" in first.json()["message"]
        assert _sign_in(c, "wrong").status_code == 401
        third = _sign_in(c, "wrong")
        assert third.status_code == 429
        assert int(third.headers["retry-after"]) == login.LOCK_SECONDS
        assert _sign_in(c).status_code == 429


def test_a_wrong_username_counts_too(env):
    with _client() as c:
        for _ in range(3):
            _sign_in(c, username="someone-else")
        assert _sign_in(c).status_code == 429


def test_the_device_stays_locked_from_a_new_ip(env):
    with _client(client=PROXY) as c:
        for _ in range(3):
            _sign_in(c, "wrong", device="device-bbbbbbbbbbbb", ip="203.0.113.5")
        assert _sign_in(c, device="device-bbbbbbbbbbbb", ip="203.0.113.99").status_code == 429


def test_the_ip_stays_locked_from_a_new_device(env):
    with _client(client=PROXY) as c:
        for _ in range(3):
            _sign_in(c, "wrong", device="device-cccccccccccc", ip="203.0.113.6")
        assert _sign_in(c, device="device-dddddddddddd", ip="203.0.113.6").status_code == 429
        assert _sign_in(c, device="device-eeeeeeeeeeee", ip="203.0.113.7").status_code == 200


def test_a_success_resets_the_count(env):
    with _client() as c:
        _sign_in(c, "wrong")
        _sign_in(c, "wrong")
        assert _sign_in(c).status_code == 200
        _sign_in(c, "wrong")
        _sign_in(c, "wrong")
        assert _sign_in(c).status_code == 200


def test_the_lock_expires_after_two_hours(tmp_path):
    limiter = login.LoginLimiter(tmp_path / "guard.json")
    keys = ["ip:203.0.113.8"]
    for _ in range(3):
        limiter.record_failure(keys, now=1000)
    assert limiter.locked_for(keys, now=1000 + login.LOCK_SECONDS - 1) > 0
    assert limiter.locked_for(keys, now=1000 + login.LOCK_SECONDS) == 0
    assert limiter.failures_left(keys, now=1000 + login.LOCK_SECONDS) == 3


def test_the_lock_survives_a_restart(tmp_path):
    keys = ["ip:203.0.113.9"]
    first = login.LoginLimiter(tmp_path / "guard.json")
    for _ in range(3):
        first.record_failure(keys)
    assert login.LoginLimiter(tmp_path / "guard.json").locked_for(keys) > 0


def test_x_real_ip_is_only_trusted_from_loopback():
    headers = {"x-real-ip": ["203.0.113.10"]}
    assert login.client_ip({"client": ("127.0.0.1", 1)}, headers) == "203.0.113.10"
    assert login.client_ip({"client": ("198.51.100.1", 1)}, headers) == "198.51.100.1"
    assert login.client_ip({"client": ("127.0.0.1", 1)}, {"x-real-ip": ["nope"]}) == "127.0.0.1"


def test_sign_out_revokes_the_session(env):
    with _client() as c:
        token = _sign_in(c).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        assert c.post("/api/logout", headers=auth).status_code == 200
        assert c.get("/api/harvey/status", headers=auth).status_code == 401


def test_sign_in_is_off_without_credentials(env):
    dash.ENV_FILE.write_text("")
    with _client() as c:
        assert _sign_in(c).status_code == 503


def test_sign_in_refuses_cross_site_requests(env):
    with _client() as c:
        r = c.post("/api/login", json={"username": USER, "password": PASSWORD},
                   headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403


def test_the_public_hostname_works_over_https(env):
    with _client(base_url=f"https://{PUBLIC}", client=PROXY) as c:
        assert c.get("/").status_code == 200
        r = c.post("/api/login", json={"username": USER, "password": PASSWORD},
                   headers={"Origin": f"https://{PUBLIC}", "Sec-Fetch-Site": "same-origin",
                            "X-Real-IP": "203.0.113.11"})
        assert r.status_code == 200
        plain = c.post("/api/login", json={"username": USER, "password": PASSWORD},
                       headers={"Origin": f"http://{PUBLIC}"})
        assert plain.status_code == 403


def test_other_hostnames_stay_refused(env):
    with _client(base_url="https://other.example.test", client=PROXY) as c:
        assert c.get("/").status_code == 400
        assert _sign_in(c).status_code == 400
