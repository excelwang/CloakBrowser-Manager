"""Tests for browser_manager pure functions — proxy parsing, fingerprint args, profile defaults."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import types
import urllib.request
from pathlib import Path

import pytest

import socket

from backend.browser_manager import (
    BASE_CDP_PORT,
    CDP_PORT_RANGE,
    _init_profile_defaults,
    _normalize_proxy,
    _validate_proxy,
    BrowserManager,
)


class _FakePage:
    def __init__(self, observed: dict, *, version_body: str = "", capability: dict | None = None):
        self.observed = observed
        self.version_body = version_body
        self.capability = capability or {"available": False}
        self.closed = False

    async def goto(self, _url: str, wait_until: str | None = None):
        return None

    async def close(self):
        self.closed = True

    async def evaluate(self, script: str):
        if "document.body ? document.body.innerText" in script:
            return self.version_body
        if "PublicKeyCredential" in script or "navigator.credentials" in script:
            return self.capability
        return self.observed


class _FakeCDPSession:
    def __init__(self, arguments: list[str] | None = None, error: Exception | None = None):
        self.arguments = arguments
        self.error = error

    async def send(self, _method: str):
        if self.error:
            raise self.error
        return {"arguments": self.arguments or []}


class _FakeContext:
    def __init__(
        self,
        observed: dict,
        arguments: list[str] | None = None,
        cdp_error: Exception | None = None,
        *,
        version_body: str | None = None,
        capability: dict | None = None,
    ):
        self.observed = observed
        self.arguments = arguments
        self.cdp_error = cdp_error
        self.version_body = version_body or _chrome_version_body(arguments or [])
        self.capability = capability or {"available": True}
        self.pages = [_FakePage(observed, version_body=self.version_body, capability=self.capability)]

    async def new_cdp_session(self, _page: _FakePage):
        return _FakeCDPSession(self.arguments, self.cdp_error)

    async def new_page(self):
        return _FakePage(self.observed, version_body=self.version_body, capability=self.capability)


# ── _normalize_proxy ─────────────────────────────────────────────────────────


def test_normalize_already_http():
    assert _normalize_proxy("http://user:pass@host:8080") == "http://user:pass@host:8080"


def test_normalize_already_https():
    assert _normalize_proxy("https://host:443") == "https://host:443"


def test_normalize_already_socks5():
    assert _normalize_proxy("socks5://host:1080") == "socks5://host:1080"


def test_normalize_host_port_user_pass():
    assert _normalize_proxy("proxy.com:8080:myuser:mypass") == "http://myuser:mypass@proxy.com:8080"


def test_normalize_host_port_only():
    assert _normalize_proxy("proxy.com:8080") == "http://proxy.com:8080"


def test_normalize_three_parts():
    # 3 parts doesn't match any pattern — returned as-is
    assert _normalize_proxy("a:b:c") == "a:b:c"


def test_normalize_five_parts():
    # 5 parts doesn't match — returned as-is
    assert _normalize_proxy("a:b:c:d:e") == "a:b:c:d:e"


def test_normalize_empty_parts():
    # host:port:user:pass with empty parts
    result = _normalize_proxy(":8080:user:pass")
    assert result == "http://user:pass@:8080"


# ── _validate_proxy ──────────────────────────────────────────────────────────


def test_validate_valid_http():
    _validate_proxy("http://proxy.com:8080")  # should not raise


def test_validate_valid_socks5():
    _validate_proxy("socks5://proxy.com:1080")  # should not raise


def test_validate_valid_with_auth():
    _validate_proxy("http://user:pass@proxy.com:8080")  # should not raise


def test_validate_bad_scheme():
    with pytest.raises(ValueError, match="Invalid proxy scheme 'ftp'"):
        _validate_proxy("ftp://host:80")


def test_validate_no_hostname():
    with pytest.raises(ValueError, match="missing hostname"):
        _validate_proxy("http://:8080")


def test_validate_no_port():
    with pytest.raises(ValueError, match="missing port"):
        _validate_proxy("http://host")


# ── _build_fingerprint_args ──────────────────────────────────────────────────

# Use the BrowserManager instance to call the method
_mgr = BrowserManager()


def test_build_args_always_includes_base():
    args = _mgr._build_fingerprint_args({})
    assert "--disable-infobars" in args
    assert "--test-type" in args
    assert "--use-angle=swiftshader" in args


def test_build_args_seed():
    args = _mgr._build_fingerprint_args({"fingerprint_seed": 42})
    assert "--fingerprint=42" in args


def test_build_args_no_seed():
    args = _mgr._build_fingerprint_args({"fingerprint_seed": None})
    assert not any(a.startswith("--fingerprint=") for a in args)


def test_build_args_platform():
    args = _mgr._build_fingerprint_args({"platform": "macos"})
    assert "--fingerprint-platform=macos" in args


def test_build_args_gpu():
    args = _mgr._build_fingerprint_args({
        "gpu_vendor": "NVIDIA Corporation",
        "gpu_renderer": "NVIDIA GeForce RTX 3070",
    })
    assert "--fingerprint-gpu-vendor=NVIDIA Corporation" in args
    assert "--fingerprint-gpu-renderer=NVIDIA GeForce RTX 3070" in args


def test_build_args_hardware_concurrency():
    args = _mgr._build_fingerprint_args({"hardware_concurrency": 8})
    assert "--fingerprint-hardware-concurrency=8" in args


def test_build_args_screen():
    args = _mgr._build_fingerprint_args({"screen_width": 2560, "screen_height": 1440})
    assert "--fingerprint-screen-width=2560" in args
    assert "--fingerprint-screen-height=1440" in args


def test_build_args_empty_profile():
    args = _mgr._build_fingerprint_args({})
    # Only the 3 base args
    assert len(args) == 3


# ── launch_args appended to extra_args ────────────────────────────────────────


def test_launch_args_appended_to_fingerprint_args():
    """launch_args from profile should appear in the args list after fingerprint args."""
    profile = {
        "fingerprint_seed": 42,
        "platform": "windows",
        "launch_args": ["--load-extension=/tmp/ext", "--disable-features=Foo"],
    }
    args = _mgr._build_fingerprint_args(profile)
    args += profile.get("launch_args") or []
    assert "--load-extension=/tmp/ext" in args
    assert "--disable-features=Foo" in args
    # Fingerprint args still present
    assert "--fingerprint=42" in args


def test_launch_args_empty_no_effect():
    profile = {"launch_args": []}
    args = _mgr._build_fingerprint_args(profile)
    base_count = len(args)
    args += profile.get("launch_args") or []
    assert len(args) == base_count


def test_launch_args_none_no_effect():
    profile = {"launch_args": None}
    args = _mgr._build_fingerprint_args(profile)
    base_count = len(args)
    args += profile.get("launch_args") or []
    assert len(args) == base_count


# ── stealth integrity ─────────────────────────────────────────────────────────


def _observed_browser_state(**overrides):
    state = {
        "webdriver": False,
        "language": "en-US",
        "languages": ["en-US"],
        "timezone": "America/New_York",
        "screen": {"width": 1366, "height": 768, "availWidth": 1366, "availHeight": 720},
        "inner": {"width": 1366, "height": 635},
        "outer": {"width": 1366, "height": 720},
    }
    state.update(overrides)
    return state


def _chrome_version_body(arguments: list[str]) -> str:
    command_line = shlex.join(["/usr/bin/cloakbrowser", *arguments])
    return f"Profile Path\t/tmp/profile\nCommand Line\t{command_line}\nExecutable Path\t/usr/bin/cloakbrowser\n"


def _webauthn_disabled_capability_snapshot() -> dict:
    return {
        "available": True,
        "publicKeyCredentialPresent": False,
        "publicKeyCredentialGetClientCapabilitiesPresent": False,
        "navigatorCredentialsCreatePresent": False,
        "navigatorCredentialsGetPresent": False,
        "publicKeyCredentialClientCapabilities": None,
    }


@pytest.mark.asyncio
async def test_direct_cdp_launch_preserves_http_proxy_as_browser_arg(tmp_path, monkeypatch):
    import cloakbrowser

    monkeypatch.setattr(cloakbrowser, "__path__", [], raising=False)

    fake_browser_module = types.ModuleType("cloakbrowser.browser")
    fake_browser_module.ensure_binary = lambda: "/usr/bin/cloakbrowser"
    fake_browser_module.maybe_resolve_geoip = lambda geoip, proxy, timezone, locale: (timezone, locale, None)
    fake_browser_module._resolve_backend = lambda backend: "playwright"
    fake_browser_module._resolve_webrtc_args = lambda args, proxy: list(args or [])

    def fake_resolve_proxy_config(proxy):
        return {
            "proxy": {
                "server": "http://proxy.example:8080",
                "username": "mesh",
                "password": "secret",
            }
        }, []

    fake_browser_module._resolve_proxy_config = fake_resolve_proxy_config
    fake_browser_module.build_args = (
        lambda stealth_args, extra_args, timezone=None, locale=None, headless=True: ["--stealth-default", *extra_args]
    )

    fake_context = object()

    class FakeBrowser:
        contexts = [fake_context]

        async def close(self):
            return None

    class FakeChromium:
        async def connect_over_cdp(self, cdp_url):
            assert cdp_url == "http://127.0.0.1:5100"
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            return None

    class FakePlaywrightStarter:
        async def start(self):
            return FakePlaywright()

    fake_browser_module._import_async_playwright = lambda backend: FakePlaywrightStarter
    monkeypatch.setitem(sys.modules, "cloakbrowser.browser", fake_browser_module)

    created: dict[str, object] = {}

    class FakeProcess:
        returncode = None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(binary, *args, **kwargs):
        created["binary"] = binary
        created["args"] = list(args)
        created["env"] = kwargs.get("env")
        return FakeProcess()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse())

    mgr = BrowserManager()
    process, pw, browser, context, launch_record, runtime_artifacts = await mgr._launch_browser_via_cdp(
        profile={
            "id": "proxy-case",
            "user_data_dir": tmp_path / "profile",
            "headless": False,
            "screen_width": 1366,
            "screen_height": 768,
        },
        display=100,
        cdp_port=5100,
        proxy="http://mesh:secret@proxy.example:8080",
        extra_args=["--disable-infobars", "--remote-debugging-port=5100"],
    )

    try:
        assert process.returncode is None
        assert pw is not None
        assert browser is not None
        assert context is fake_context
        assert created["binary"] == "/usr/bin/cloakbrowser"
        assert "--proxy-server=http://proxy.example:8080" in created["args"]
        assert not any("mesh:secret" in str(arg) for arg in created["args"])
        assert "--remote-debugging-port=5100" in created["args"]
        assert f"--user-data-dir={tmp_path / 'profile'}" in created["args"]
        assert launch_record["launchPath"] == "direct_binary_cdp_attach"
        assert launch_record["proxyTransport"] == "command_line"
        assert launch_record["proxyInput"] == "http://mesh:secret@proxy.example:8080"
        assert launch_record["normalizedProxy"] == "http://proxy.example:8080"
        assert runtime_artifacts
        assert any(str(arg).startswith("--load-extension=") for arg in created["args"])
    finally:
        mgr._cleanup_runtime_artifacts(runtime_artifacts)


@pytest.mark.asyncio
async def test_stealth_integrity_passes_without_automation_args():
    mgr = BrowserManager()
    result = await mgr._check_stealth_integrity(
        context=_FakeContext(_observed_browser_state(), arguments=["--disable-infobars"]),
        profile={"locale": "en-US", "timezone": "America/New_York", "screen_width": 1366, "screen_height": 768},
        requested_args=["--disable-infobars"],
    )
    assert result["passed"] is True
    assert result["errors"] == []
    assert result["launchFactAudit"]["passed"] is True
    assert result["command_line"]["source"] == "chrome://version"


@pytest.mark.asyncio
async def test_stealth_integrity_fails_on_enable_automation_arg():
    mgr = BrowserManager()
    result = await mgr._check_stealth_integrity(
        context=_FakeContext(_observed_browser_state(), arguments=["--enable-automation"]),
        profile={"locale": "en-US", "timezone": "America/New_York", "screen_width": 1366, "screen_height": 768},
        requested_args=[],
    )
    assert result["passed"] is False
    assert "BROWSER_LAUNCH_ARGS_MISMATCH" in result["errors"]


@pytest.mark.asyncio
async def test_stealth_integrity_fails_on_webdriver_true():
    mgr = BrowserManager()
    result = await mgr._check_stealth_integrity(
        context=_FakeContext(_observed_browser_state(webdriver=True), arguments=[]),
        profile={"locale": "en-US", "timezone": "America/New_York", "screen_width": 1366, "screen_height": 768},
        requested_args=[],
    )
    assert result["passed"] is False
    assert "BROWSER_LAUNCH_FACT_AUDIT_FAILED" in result["errors"]


@pytest.mark.asyncio
async def test_stealth_integrity_fails_when_proxy_missing_from_command_line():
    mgr = BrowserManager()
    result = await mgr._check_stealth_integrity(
        context=_FakeContext(_observed_browser_state(), arguments=["--disable-infobars"]),
        profile={"locale": "en-US", "timezone": "America/New_York", "screen_width": 1366, "screen_height": 768},
        requested_args=["--disable-infobars"],
        launch_record={
            "launchPath": "playwright_launch_persistent_context_async",
            "proxyTransport": "sdk_kwarg",
            "args": ["--disable-infobars"],
            "proxyInput": "http://user:pass@proxy.example:8080",
            "normalizedProxy": "http://user:pass@proxy.example:8080",
        },
    )
    assert result["passed"] is False
    assert "BROWSER_PROXY_NOT_EFFECTIVE" in result["errors"]


@pytest.mark.asyncio
async def test_stealth_integrity_passes_when_proxy_present_in_command_line():
    mgr = BrowserManager()
    args = [
        "--disable-infobars",
        "--proxy-server=http://user:pass@proxy.example:8080",
    ]
    result = await mgr._check_stealth_integrity(
        context=_FakeContext(_observed_browser_state(), arguments=args),
        profile={"locale": "en-US", "timezone": "America/New_York", "screen_width": 1366, "screen_height": 768},
        requested_args=["--disable-infobars"],
        launch_record={
            "launchPath": "playwright_launch_persistent_context_async",
            "proxyTransport": "sdk_kwarg",
            "args": ["--disable-infobars"],
            "proxyInput": "http://user:pass@proxy.example:8080",
            "normalizedProxy": "http://user:pass@proxy.example:8080",
        },
    )
    assert result["passed"] is True
    assert result["launchFactAudit"]["passed"] is True


@pytest.mark.asyncio
async def test_stealth_integrity_fails_explicit_webauthn_disable_when_capability_persists():
    mgr = BrowserManager()
    args = [
        "--disable-webauthn",
        "--disable-features=WebAuthentication,WebAuthnUI,WebAuthnPlatformAuthenticator,WebAuthnCrossDevice,WebAuthnExtensions",
        "--disable-blink-features=WebAuthentication",
    ]
    result = await mgr._check_stealth_integrity(
        context=_FakeContext(
            _observed_browser_state(),
            arguments=args,
            capability={
                "available": True,
                "publicKeyCredentialPresent": True,
                "publicKeyCredentialGetClientCapabilitiesPresent": True,
                "navigatorCredentialsCreatePresent": True,
                "navigatorCredentialsGetPresent": True,
                "publicKeyCredentialClientCapabilities": {"conditionalCreate": True},
            },
        ),
        profile={"locale": "en-US", "timezone": "America/New_York", "screen_width": 1366, "screen_height": 768},
        requested_args=args,
        launch_record={
            "launchPath": "playwright_launch_persistent_context_async",
            "proxyTransport": "none",
            "args": args,
        },
    )
    assert result["passed"] is False
    assert "BROWSER_CAPABILITY_POLICY_MISMATCH" in result["errors"]


@pytest.mark.asyncio
async def test_stealth_integrity_passes_explicit_webauthn_disable_when_capability_removed():
    mgr = BrowserManager()
    args = [
        "--disable-webauthn",
        "--disable-features=WebAuthentication,WebAuthnUI,WebAuthnPlatformAuthenticator,WebAuthnCrossDevice,WebAuthnExtensions",
        "--disable-blink-features=WebAuthentication",
    ]
    result = await mgr._check_stealth_integrity(
        context=_FakeContext(
            _observed_browser_state(),
            arguments=args,
            capability=_webauthn_disabled_capability_snapshot(),
        ),
        profile={"locale": "en-US", "timezone": "America/New_York", "screen_width": 1366, "screen_height": 768},
        requested_args=args,
        launch_record={
            "launchPath": "playwright_launch_persistent_context_async",
            "proxyTransport": "none",
            "args": args,
        },
    )
    assert result["passed"] is True
    assert result["launchFactAudit"]["passed"] is True


# ── _allocate_cdp_port ───────────────────────────────────────────────────────


def test_allocate_cdp_port_returns_free_port():
    mgr = BrowserManager()
    port = mgr._allocate_cdp_port()
    assert BASE_CDP_PORT <= port < BASE_CDP_PORT + CDP_PORT_RANGE


def test_allocate_cdp_port_skips_occupied():
    mgr = BrowserManager()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", BASE_CDP_PORT))
        blocker.listen(1)
        port = mgr._allocate_cdp_port()
        assert port == BASE_CDP_PORT + 1


def test_allocate_cdp_port_advances_counter():
    mgr = BrowserManager()
    p1 = mgr._allocate_cdp_port()
    p2 = mgr._allocate_cdp_port()
    assert p2 == p1 + 1


def test_allocate_cdp_port_wraps_around():
    mgr = BrowserManager()
    mgr._next_cdp_port = BASE_CDP_PORT + CDP_PORT_RANGE - 1
    p1 = mgr._allocate_cdp_port()
    assert p1 == BASE_CDP_PORT + CDP_PORT_RANGE - 1
    p2 = mgr._allocate_cdp_port()
    assert p2 == BASE_CDP_PORT


def test_allocate_cdp_port_all_occupied_raises():
    mgr = BrowserManager()
    blockers = []
    try:
        for i in range(CDP_PORT_RANGE):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", BASE_CDP_PORT + i))
            s.listen(1)
            blockers.append(s)
        with pytest.raises(ValueError, match="No free CDP ports"):
            mgr._allocate_cdp_port()
    finally:
        for s in blockers:
            s.close()


# ── _init_profile_defaults ───────────────────────────────────────────────────


def test_init_creates_bookmarks(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    bookmarks_path = tmp_path / "Default" / "Bookmarks"
    assert bookmarks_path.exists()
    data = json.loads(bookmarks_path.read_text())
    children = data["roots"]["bookmark_bar"]["children"]
    assert len(children) == 4  # 4 folders
    folder_names = {f["name"] for f in children}
    assert folder_names == {"Detection Tests", "Fingerprint", "Headers & TLS", "reCAPTCHA"}


def test_init_creates_preferences(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    prefs_path = tmp_path / "Default" / "Preferences"
    assert prefs_path.exists()
    data = json.loads(prefs_path.read_text())
    assert "default_search_provider_data" in data
    assert "DuckDuckGo" in data["default_search_provider_data"]["template_url_data"]["short_name"]


def test_init_idempotent(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    bookmarks_path = tmp_path / "Default" / "Bookmarks"
    original = bookmarks_path.read_text()

    # Write a sentinel to the file
    bookmarks_path.write_text("SENTINEL")

    # Second call should NOT overwrite (file already exists)
    _init_profile_defaults(tmp_path)
    assert bookmarks_path.read_text() == "SENTINEL"
