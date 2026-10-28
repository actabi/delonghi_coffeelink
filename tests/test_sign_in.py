"""Sign-in failures: which ones are retried, which ones stop and ask the user.

A refused password retried forever is how a Gigya account gets locked (403120):
before this, ConfigEntryNotReady signed in again on every retry and the poll
signed in again every 30 seconds. These cases pin the classification in
``ayla_client`` and the form errors the config flow derives from it.

``ayla_client`` is pure and is loaded under a package name of its own, so it
cannot race test_coordinator_session for the cached ``delonghi_coffeelink.*``
entries. ``config_flow`` needs a few Home Assistant names; they are put in
``sys.modules`` only while it loads and taken out again right after.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import aiohttp
import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"
)
_PKG = "delonghi_sign_in_under_test"
_package = types.ModuleType(_PKG)
_package.__path__ = [str(COMPONENT_DIR)]
sys.modules[_PKG] = _package


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.{name}", COMPONENT_DIR / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.{name}"] = module
    spec.loader.exec_module(module)
    return module


def _load_config_flow():
    """config_flow.py with the Home Assistant names it imports, stubbed briefly."""

    class _ConfigFlow:
        def __init_subclass__(cls, domain=None, **kwargs) -> None:
            super().__init_subclass__(**kwargs)

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlow = _ConfigFlow
    core = types.ModuleType("homeassistant.core")
    core.callback = lambda func: func
    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.config_entries = config_entries
    stubs = {
        "homeassistant": homeassistant,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.data_entry_flow": data_entry_flow,
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        return _load("config_flow")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


const = _load("const")
ayla = _load("ayla_client")
config_flow = _load_config_flow()


# --- a session that answers the sign-in posts in order -----------------------

class _Response:
    def __init__(self, body, status: int = 200) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _Session:
    """Answers each post with the next canned response, or raises it."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.posts: list[str] = []

    def post(self, url, data=None, timeout=None):
        self.posts.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


LOGIN_OK = _Response({
    "errorCode": 0,
    "sessionInfo": {"sessionToken": "st", "sessionSecret": "c2VjcmV0"},
})
JWT_OK = _Response({"errorCode": 0, "id_token": "jwt"})
AYLA_OK = _Response(
    {"access_token": "token", "refresh_token": "r", "expires_in": 3600}, status=200
)


def _gigya_error(code: int, message: str = "error") -> _Response:
    return _Response({"errorCode": code, "errorMessage": message})


def _client(session: _Session):
    return ayla.DelonghiAylaClient(session, "owner@example.invalid", "secret")


def _sign_in(client) -> None:
    asyncio.run(client.async_authenticate())


# --- classification ------------------------------------------------------------

@pytest.mark.parametrize(
    ("code", "transient"),
    [
        (403042, False),  # invalid login ID or password
        (403120, False),  # account locked
        (403002, False),  # request expired: the host clock is off
        (206001, False),  # account pending registration
        (403048, True),  # rate limited: the password was not judged
        (500001, True),  # Gigya failing
        ("not a number", True),
        (None, True),
    ],
)
def test_gigya_error_classification(code, transient):
    assert ayla.gigya_error_is_transient(code) is transient


@pytest.mark.parametrize(
    ("status", "transient"),
    [(400, False), (401, False), (403, False), (408, True), (429, True),
     (500, True), (503, True)],
)
def test_ayla_sso_status_classification(status, transient):
    assert ayla.ayla_sso_status_is_transient(status) is transient


def test_an_auth_error_nobody_classified_counts_as_refused():
    """Better to stop and ask than to retry forever."""
    assert ayla.AuthError("x").transient is False
    assert ayla.AuthError("x", transient=True).transient is True


# --- over the wire ---------------------------------------------------------------

def test_a_wrong_password_is_refused_and_never_tried_again():
    session = _Session(_gigya_error(403042, "Invalid LoginID"))
    client = _client(session)
    with pytest.raises(ayla.AuthError) as first:
        _sign_in(client)
    assert first.value.transient is False
    assert first.value.code == 403042
    # Buttons, services and the refresh a press requests would each sign in once
    # more; the client answers them from memory instead.
    for _ in range(3):
        with pytest.raises(ayla.AuthError) as again:
            _sign_in(client)
        assert again.value is first.value
    assert len(session.posts) == 1


def test_a_locked_account_keeps_its_code():
    client = _client(_Session(_gigya_error(403120, "Account temporarily locked")))
    with pytest.raises(ayla.AuthError) as err:
        _sign_in(client)
    assert err.value.code == ayla.GIGYA_ACCOUNT_LOCKED
    assert err.value.transient is False


def test_a_rate_limit_is_retried_on_the_next_sign_in():
    session = _Session(_gigya_error(403048, "Rate limit"), LOGIN_OK, JWT_OK, AYLA_OK)
    client = _client(session)
    with pytest.raises(ayla.AuthError) as err:
        _sign_in(client)
    assert err.value.transient is True
    _sign_in(client)
    assert client._access_token == "token"


def test_a_wrong_host_clock_is_reported_as_such():
    session = _Session(LOGIN_OK, _gigya_error(403002, "Request has expired"))
    with pytest.raises(ayla.AuthError) as err:
        _sign_in(_client(session))
    assert err.value.code == ayla.GIGYA_REQUEST_EXPIRED
    assert err.value.transient is False
    assert "clock (NTP)" in str(err.value)


def test_a_network_failure_is_transient_not_a_crash():
    """After a power cut HA can start before the internet is back."""
    session = _Session(aiohttp.ClientConnectionError("no route to host"))
    with pytest.raises(ayla.AuthError) as err:
        _sign_in(_client(session))
    assert err.value.transient is True


def test_an_ayla_server_error_is_transient():
    session = _Session(LOGIN_OK, JWT_OK, _Response("Bad gateway", status=502))
    with pytest.raises(ayla.AuthError) as err:
        _sign_in(_client(session))
    assert err.value.transient is True
    assert err.value.kind == ayla.AYLA_SSO_REFUSED


def test_an_answer_without_a_token_logs_its_keys_not_its_values():
    session = _Session(LOGIN_OK, JWT_OK, _Response({"error": "do-not-log-me"}))
    with pytest.raises(ayla.AuthError) as err:
        _sign_in(_client(session))
    assert err.value.transient is True
    assert "error" in str(err.value)
    assert "do-not-log-me" not in str(err.value)


def test_an_ayla_refusal_is_remembered_for_a_while_then_tried_once_more(monkeypatch):
    """Gigya accepted the password and Ayla refused: not for good, not every press."""
    now = [1000.0]
    monkeypatch.setattr(
        ayla, "time", types.SimpleNamespace(time=time.time, monotonic=lambda: now[0])
    )
    session = _Session(
        LOGIN_OK, JWT_OK, _Response("Unauthorized", status=401),
        LOGIN_OK, JWT_OK, AYLA_OK,
    )
    client = _client(session)
    with pytest.raises(ayla.AuthError) as first:
        _sign_in(client)
    assert first.value.kind == ayla.AYLA_SSO_REFUSED
    assert first.value.transient is False

    now[0] += ayla.AYLA_REFUSED_PAUSE - 1
    with pytest.raises(ayla.AuthError) as again:
        _sign_in(client)
    assert again.value is first.value
    assert len(session.posts) == 3  # answered from memory

    now[0] += 2
    _sign_in(client)
    assert client._access_token == "token"
    assert len(session.posts) == 6


# --- what the sign-in form says ----------------------------------------------------

@pytest.mark.parametrize(
    ("error", "form_error"),
    [
        (ayla.AuthError("bad password", code=403042), "invalid_auth"),
        (ayla.AuthError("locked", code=403120), "account_locked"),
        (ayla.AuthError("clock", code=403002), "clock_skew"),
        (ayla.AuthError("rate limit", code=403048, transient=True), "cannot_connect"),
        (ayla.AuthError("ayla 401", kind=ayla.AYLA_SSO_REFUSED), "ayla_refused"),
        (
            ayla.AuthError("ayla 503", kind=ayla.AYLA_SSO_REFUSED, transient=True),
            "cannot_connect",
        ),
    ],
)
def test_each_refusal_gets_a_form_error_the_user_can_act_on(error, form_error):
    assert config_flow._auth_error_key(error) == form_error


def test_every_form_error_has_a_text():
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
    texts = strings["config"]["error"]
    for key in ("invalid_auth", "account_locked", "clock_skew", "ayla_refused",
                "cannot_connect"):
        assert texts.get(key), key
    assert strings["config"]["step"]["reauth_confirm"]["data"]["password"]
    assert strings["config"]["abort"]["reauth_successful"]
    assert strings["exceptions"]["ayla_refused"]["message"]
