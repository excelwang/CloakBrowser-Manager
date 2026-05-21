"""Launch/stop/track CloakBrowser instances per profile."""

from __future__ import annotations

import asyncio
import datetime
import importlib.metadata
import json
import logging
import os
import shutil
import socket
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from .vnc_manager import VNCManager

logger = logging.getLogger("cloakbrowser.manager.browser")


def _normalize_proxy(raw: str) -> str:
    """Convert common proxy formats to http://user:pass@host:port.

    Accepts:
      - http://user:pass@host:port  (already valid)
      - host:port:user:pass
      - host:port
    """
    if raw.startswith(("http://", "https://", "socks5://")):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"http://{user}:{passwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return raw


def _validate_proxy(url: str) -> None:
    """Validate that a normalized proxy URL has scheme, host, and port."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "socks5"):
        raise ValueError(
            f"Invalid proxy scheme '{parsed.scheme}'. Must be http, https, or socks5."
        )
    if not parsed.hostname:
        raise ValueError(f"Proxy URL missing hostname: {url}")
    if not parsed.port:
        raise ValueError(f"Proxy URL missing port: {url}")


def _init_profile_defaults(user_data_dir: Path) -> None:
    """Set up bookmarks and DuckDuckGo search on first launch."""
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # --- Bookmarks (only on first launch) ---
    bookmarks_path = default_dir / "Bookmarks"
    if not bookmarks_path.exists():
        ts = str(int(time.time() * 1_000_000))  # Chrome timestamp format
        _id = 1

        def bm(name: str, url: str) -> dict:
            nonlocal _id
            _id += 1
            return {"type": "url", "id": str(_id), "name": name, "url": url, "date_added": ts}

        def folder(name: str, children: list) -> dict:
            nonlocal _id
            _id += 1
            return {"type": "folder", "id": str(_id), "name": name, "children": children, "date_added": ts, "date_modified": ts}

        bookmarks = {
            "checksum": "",
            "roots": {
                "bookmark_bar": {
                    "type": "folder", "id": "1", "name": "Bookmarks bar",
                    "date_added": ts, "date_modified": ts,
                    "children": [
                        folder("Detection Tests", [
                            bm("Rebrowser Bot Detector", "https://bot-detector.rebrowser.net/"),
                            bm("Incolumitas", "https://bot.incolumitas.com/"),
                            bm("SannySort", "https://bot.sannysoft.com/"),
                            bm("BrowserScan Bot", "https://www.browserscan.net/bot-detection"),
                            bm("FingerprintJS Demo", "https://demo.fingerprint.com/web-scraping"),
                            bm("Pixelscan", "https://pixelscan.net/fingerprint-check"),
                            bm("CreepJS", "https://abrahamjuliot.github.io/creepjs/"),
                            bm("fingerprint-scan", "https://fingerprint-scan.com/"),
                            bm("DeviceInfo Bot", "https://deviceandbrowserinfo.com/are_you_a_bot"),
                        ]),
                        folder("Fingerprint", [
                            bm("BrowserLeaks Canvas", "https://browserleaks.com/canvas"),
                            bm("BrowserLeaks WebGL", "https://browserleaks.com/webgl"),
                            bm("BrowserLeaks Fonts", "https://browserleaks.com/fonts"),
                            bm("BrowserLeaks JS", "https://browserleaks.com/javascript"),
                            bm("FingerprintJS OSS", "https://fingerprintjs.github.io/fingerprintjs/"),
                            bm("Audio FP", "https://audiofingerprint.openwpm.com/"),
                            bm("DeviceInfo", "https://deviceandbrowserinfo.com/info_device"),
                        ]),
                        folder("Headers & TLS", [
                            bm("httpbin headers", "https://httpbin.org/headers"),
                            bm("httpbin IP", "https://httpbin.org/ip"),
                            bm("TLS Fingerprint", "https://tls.browserleaks.com/"),
                        ]),
                        folder("reCAPTCHA", [
                            bm("Google v3 Demo", "https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php"),
                            bm("2captcha v3", "https://2captcha.com/demo/recaptcha-v3"),
                            bm("Turnstile", "https://peet.ws/turnstile-test/non-interactive.html"),
                        ]),
                    ],
                },
                "other": {"type": "folder", "id": "2", "name": "Other bookmarks", "children": []},
                "synced": {"type": "folder", "id": "3", "name": "Mobile bookmarks", "children": []},
            },
            "version": 1,
        }
        bookmarks_path.write_text(json.dumps(bookmarks, indent=2))
        logger.info("Created default bookmarks for %s", user_data_dir.name)

    # --- DuckDuckGo as default search engine ---
    prefs_path = default_dir / "Preferences"
    if not prefs_path.exists():
        prefs = {
            "default_search_provider_data": {
                "template_url_data": {
                    "keyword": "duckduckgo.com",
                    "short_name": "DuckDuckGo",
                    "url": "https://duckduckgo.com/?q={searchTerms}",
                    "suggestions_url": "https://duckduckgo.com/ac/?q={searchTerms}&type=list",
                    "favicon_url": "https://duckduckgo.com/favicon.ico",
                }
            },
            "default_search_provider": {
                "enabled": True,
            },
        }
        prefs_path.write_text(json.dumps(prefs, indent=2))
        logger.info("Set DuckDuckGo as default search for %s", user_data_dir.name)


BASE_CDP_PORT = 5100
CDP_PORT_RANGE = 100  # cycle through 5100-5199 to avoid TIME_WAIT collisions


@dataclass
class RunningProfile:
    profile_id: str
    context: Any  # Playwright BrowserContext
    display: int
    ws_port: int
    cdp_port: int
    launched_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    browser: Any | None = None  # Playwright Browser CDP session
    playwright: Any | None = None  # Playwright driver instance
    process: asyncio.subprocess.Process | None = None
    stealth_integrity: dict[str, Any] = field(default_factory=dict)
    runtime_artifacts: list[str] = field(default_factory=list)


class StealthIntegrityError(RuntimeError):
    """Raised when a launched browser exposes automation indicators."""


class BrowserManager:
    def __init__(self):
        self.running: dict[str, RunningProfile] = {}
        self._launching: set[str] = set()  # profile IDs currently being launched
        self.vnc = VNCManager()
        self._lock = asyncio.Lock()
        self._next_cdp_port = BASE_CDP_PORT
        self._auto_launch_task: asyncio.Task | None = None

    async def launch(self, profile: dict[str, Any]) -> RunningProfile:
        """Launch a browser instance for the given profile."""
        profile_id = profile["id"]

        async with self._lock:
            if profile_id in self.running or profile_id in self._launching:
                raise RuntimeError(f"Profile {profile_id} is already running")
            self._launching.add(profile_id)

        display, ws_port = await self.vnc.allocate()

        context = None
        browser = None
        pw = None
        process = None
        runtime_artifacts: list[str] = []
        try:
            cdp_port = self._allocate_cdp_port()
        except ValueError:
            async with self._lock:
                self._launching.discard(profile_id)
            await self.vnc.stop_vnc(display)
            raise

        # Clean stale Chromium lock files (left by previous container crashes)
        user_data_dir = Path(profile["user_data_dir"])
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = user_data_dir / lock_file
            lock_path.unlink(missing_ok=True)

        # Set up bookmarks and search engine on first launch
        _init_profile_defaults(user_data_dir)

        try:
            # Start KasmVNC on the allocated display
            await self.vnc.start_vnc(
                display,
                ws_port,
                width=profile.get("screen_width", 1920),
                height=profile.get("screen_height", 1080),
            )

            # Build fingerprint args from profile settings
            extra_args = self._build_fingerprint_args(profile)
            extra_args += profile.get("launch_args") or []
            extra_args.append(f"--remote-debugging-port={cdp_port}")

            # Normalize proxy format (host:port:user:pass → http://user:pass@host:port)
            raw_proxy = profile.get("proxy") or None
            proxy = _normalize_proxy(raw_proxy) if raw_proxy else None
            if proxy:
                _validate_proxy(proxy)

            process, pw, browser, context, launch_record, runtime_artifacts = await self._launch_browser_via_cdp(
                profile=profile,
                display=display,
                cdp_port=cdp_port,
                proxy=proxy,
                extra_args=extra_args,
            )

            stealth_integrity = await self._check_stealth_integrity(
                context=context,
                profile=profile,
                requested_args=extra_args,
                launch_record=launch_record,
            )
            if not stealth_integrity.get("passed"):
                await self._stop_browser_runtime(
                    RunningProfile(
                        profile_id=profile_id,
                        context=context,
                        display=display,
                        ws_port=ws_port,
                        cdp_port=cdp_port,
                        browser=browser,
                        playwright=pw,
                        process=process,
                        stealth_integrity=stealth_integrity,
                        runtime_artifacts=runtime_artifacts,
                    )
                )
                context = None
                browser = None
                pw = None
                process = None
                runtime_artifacts = []
                raise StealthIntegrityError(
                    "CLOAKBROWSER_STEALTH_INTEGRITY_FAILED: "
                    + "; ".join(stealth_integrity.get("errors") or ["unknown stealth integrity failure"])
                )

            # Inject clipboard listener: captures copied text on every page
            # so the GET /clipboard endpoint can read it via page.evaluate()
            _clipboard_init_js = """
                window.__clipboardText = '';
                document.addEventListener('copy', () => {
                    const sel = window.getSelection();
                    if (sel) window.__clipboardText = sel.toString();
                });
                document.addEventListener('keydown', (e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key === 'c' && !e.altKey && !e.shiftKey) {
                        const sel = window.getSelection();
                        if (sel && sel.toString()) window.__clipboardText = sel.toString();
                    }
                });
            """
            await context.add_init_script(_clipboard_init_js)
            # Also inject into already-open pages (about:blank created before init_script)
            for p in context.pages:
                try:
                    await p.evaluate(_clipboard_init_js)
                except Exception as exc:
                    logger.debug("Clipboard init failed on existing page: %s", exc)

            running = RunningProfile(
                profile_id=profile_id,
                context=context,
                display=display,
                ws_port=ws_port,
                cdp_port=cdp_port,
                browser=browser,
                playwright=pw,
                process=process,
                stealth_integrity=stealth_integrity,
                runtime_artifacts=runtime_artifacts,
            )

            # Auto-cleanup if browser crashes or user closes Chrome via VNC
            browser.on("disconnected", lambda: asyncio.ensure_future(
                self._on_browser_closed(profile_id)
            ))

            async with self._lock:
                self.running[profile_id] = running
                self._launching.discard(profile_id)

            logger.info(
                "Launched profile %s on display :%d (ws_port=%d, cdp_port=%d)",
                profile_id, display, ws_port, cdp_port,
            )

            return running

        except BaseException:
            async with self._lock:
                self._launching.discard(profile_id)
            if context is not None or browser is not None or process is not None or pw is not None or runtime_artifacts:
                try:
                    await self._stop_browser_runtime(
                        RunningProfile(
                            profile_id=profile_id,
                            context=context,
                            display=display,
                            ws_port=ws_port,
                            cdp_port=cdp_port,
                            browser=browser,
                            playwright=pw,
                            process=process,
                            runtime_artifacts=runtime_artifacts,
                        )
                    )
                except Exception as exc:
                    logger.warning("Error cleaning failed launch runtime for %s: %s", profile_id, exc)
            await self.vnc.stop_vnc(display)
            raise

    async def _on_browser_closed(self, profile_id: str):
        """Called when browser exits (crash, user closed via VNC, or stop())."""
        async with self._lock:
            running = self.running.pop(profile_id, None)

        if running:
            logger.info("Browser closed for profile %s, cleaning up", profile_id)
            await self._stop_browser_runtime(running)
            await self.vnc.stop_vnc(running.display)

    async def _launch_browser_via_cdp(
        self,
        *,
        profile: dict[str, Any],
        display: int,
        cdp_port: int,
        proxy: str | None,
        extra_args: list[str],
    ) -> tuple[asyncio.subprocess.Process, Any, Any, Any, dict[str, Any], list[str]]:
        """Start CloakBrowser directly, then attach Playwright over CDP."""
        from cloakbrowser.browser import (
            _import_async_playwright,
            _resolve_backend,
            _resolve_proxy_config,
            _resolve_webrtc_args,
            build_args,
            ensure_binary,
            maybe_resolve_geoip,
        )

        timezone = profile.get("timezone") or None
        locale = profile.get("locale") or None
        binary_path = ensure_binary()
        runtime_artifacts: list[str] = []
        recorded_proxy_value = proxy

        timezone, locale, exit_ip = maybe_resolve_geoip(
            bool(profile.get("geoip", False)),
            proxy,
            timezone,
            locale,
        )
        proxy_kwargs, proxy_extra_args = _resolve_proxy_config(proxy)
        if proxy and not proxy_extra_args:
            proxy_server = proxy
            proxy_settings = proxy_kwargs.get("proxy") if isinstance(proxy_kwargs, dict) else None
            proxy_username = ""
            proxy_password = ""
            if isinstance(proxy_settings, dict):
                proxy_server = str(proxy_settings.get("server") or proxy)
                proxy_username = str(proxy_settings.get("username") or "")
                proxy_password = str(proxy_settings.get("password") or "")
            proxy_extra_args = [f"--proxy-server={proxy_server}"]
            recorded_proxy_value = proxy_server
            if proxy_username or proxy_password:
                extension_dir = self._ensure_proxy_auth_extension(
                    profile["id"],
                    proxy_username,
                    proxy_password,
                )
                runtime_artifacts.append(extension_dir)

        launch_args = [
            arg
            for arg in (_resolve_webrtc_args(extra_args, proxy) or [])
            if not str(arg).startswith("--remote-debugging-port=")
        ]
        for runtime_arg in ("--disable-dev-shm-usage", "--no-sandbox"):
            if runtime_arg not in launch_args:
                launch_args.append(runtime_arg)
        if exit_ip and not any(str(arg).startswith("--fingerprint-webrtc-ip=") for arg in launch_args):
            launch_args.append(f"--fingerprint-webrtc-ip={exit_ip}")

        chrome_args = build_args(
            True,
            launch_args + proxy_extra_args,
            timezone=timezone,
            locale=locale,
            headless=bool(profile.get("headless", False)),
        )
        if profile.get("user_agent"):
            chrome_args.append(f"--user-agent={profile['user_agent']}")
        width = profile.get("screen_width", 1920)
        height = profile.get("screen_height", 1080)
        chrome_args.append(f"--window-size={width},{height}")
        if profile.get("color_scheme") == "dark":
            chrome_args.append("--force-dark-mode")
        for artifact in runtime_artifacts:
            chrome_args.append(f"--load-extension={artifact}")

        user_data_dir = os.fspath(profile["user_data_dir"])
        browser_env = {
            **os.environ,
            "DISPLAY": f":{display}",
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "MESA_LOADER_DRIVER_OVERRIDE": "llvmpipe",
        }
        launch_invocation_args = [
            *chrome_args,
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={user_data_dir}",
            "about:blank",
        ]
        launch_record = {
            "launchPath": "direct_binary_cdp_attach",
            "binaryPath": binary_path,
            "proxyTransport": "command_line" if proxy else "none",
            "args": list(launch_invocation_args),
            "proxyInput": proxy,
            "normalizedProxy": recorded_proxy_value,
            "envSummary": {
                "DISPLAY": browser_env["DISPLAY"],
                "LIBGL_ALWAYS_SOFTWARE": browser_env["LIBGL_ALWAYS_SOFTWARE"],
                "MESA_LOADER_DRIVER_OVERRIDE": browser_env["MESA_LOADER_DRIVER_OVERRIDE"],
            },
        }

        log_path = f"/tmp/cloakbrowser-{profile['id']}.log"
        browser_log = open(log_path, "ab")
        try:
            process = await asyncio.create_subprocess_exec(
                binary_path,
                *launch_invocation_args,
                env=browser_env,
                stdout=browser_log,
                stderr=browser_log,
                start_new_session=True,
            )
        finally:
            browser_log.close()

        cdp_url = f"http://127.0.0.1:{cdp_port}"
        last_error: Exception | None = None
        for _ in range(60):
            if process.returncode is not None:
                break
            try:
                import urllib.request

                with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=1):
                    last_error = None
                    break
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(1)
        else:
            last_error = RuntimeError("Timed out waiting for local CDP endpoint")

        if process.returncode is not None or last_error is not None:
            await self._terminate_process(process)
            self._cleanup_runtime_artifacts(runtime_artifacts)
            raise RuntimeError(
                f"Manual CloakBrowser launch failed for {profile['id']}: {last_error or process.returncode}"
            )

        pw = None
        try:
            async_playwright = _import_async_playwright(_resolve_backend(None))
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise RuntimeError("Connected browser returned no default context")
            context = browser.contexts[0]
            if profile.get("humanize"):
                from cloakbrowser.human import patch_context_async
                from cloakbrowser.human.config import resolve_config

                cfg = resolve_config(profile.get("human_preset", "default"), None)
                patch_context_async(context, cfg)
            return process, pw, browser, context, launch_record, runtime_artifacts
        except Exception:
            self._cleanup_runtime_artifacts(runtime_artifacts)
            await self._terminate_process(process)
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    logger.warning("Failed to stop Playwright after CDP attach failure", exc_info=True)
            raise

    def _ensure_proxy_auth_extension(self, profile_id: str, username: str, password: str) -> str:
        extension_dir = Path(f"/tmp/cloakbrowser-proxy-auth-{profile_id}")
        extension_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": "CloakBrowser Proxy Auth",
            "version": "1.0.0",
            "manifest_version": 3,
            "permissions": ["webRequest", "webRequestAuthProvider"],
            "host_permissions": ["<all_urls>"],
            "background": {"service_worker": "background.js"},
        }
        background = (
            f"const USERNAME = {json.dumps(username)};\n"
            f"const PASSWORD = {json.dumps(password)};\n"
            "chrome.webRequest.onAuthRequired.addListener(\n"
            "  (details, callback) => {\n"
            "    if (!details.isProxy) {\n"
            "      callback({});\n"
            "      return;\n"
            "    }\n"
            "    callback({authCredentials: {username: USERNAME, password: PASSWORD}});\n"
            "  },\n"
            "  {urls: ['<all_urls>']},\n"
            "  ['asyncBlocking']\n"
            ");\n"
        )
        (extension_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        (extension_dir / "background.js").write_text(background, encoding="utf-8")
        return str(extension_dir)

    def _cleanup_runtime_artifacts(self, artifact_paths: list[str]) -> None:
        for raw_path in artifact_paths:
            if not raw_path:
                continue
            path = Path(raw_path)
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to clean runtime artifact %s", raw_path, exc_info=True)

    async def _terminate_process(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
        except Exception:
            logger.warning("Failed to kill browser subprocess", exc_info=True)

    async def _stop_browser_runtime(self, running: RunningProfile) -> None:
        if running.browser is not None:
            try:
                await running.browser.close()
            except Exception as exc:
                logger.warning("Error closing browser session for %s: %s", running.profile_id, exc)
        elif running.context is not None:
            try:
                await running.context.close()
            except Exception as exc:
                logger.warning("Error closing context for %s: %s", running.profile_id, exc)

        await self._terminate_process(running.process)

        if running.playwright is not None:
            try:
                await running.playwright.stop()
            except Exception as exc:
                logger.warning("Error stopping Playwright for %s: %s", running.profile_id, exc)
        self._cleanup_runtime_artifacts(getattr(running, "runtime_artifacts", []))

    async def _check_stealth_integrity(
        self,
        *,
        context: Any,
        profile: dict[str, Any],
        requested_args: list[str],
        launch_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify that the launched CloakBrowser surface did not expose automation signals."""
        import re
        import shlex
        from urllib.parse import unquote
        from urllib.parse import urlparse

        errors: list[str] = []
        warnings: list[str] = []
        mismatch_details: list[dict[str, Any]] = []
        observed: dict[str, Any] = {}
        capability_snapshot: dict[str, Any] = {"available": False}
        command_line: dict[str, Any] = {
            "available": False,
            "source": "chrome://version",
            "arguments": [],
        }
        cloakbrowser_version: str | None

        def dedupe(values: list[str]) -> list[str]:
            seen: set[str] = set()
            output: list[str] = []
            for value in values:
                if value in seen:
                    continue
                seen.add(value)
                output.append(value)
            return output

        def add_mismatch(
            code: str,
            stage: str,
            field: str,
            message: str,
            *,
            expected: Any = None,
            observed_value: Any = None,
        ) -> None:
            mismatch_details.append(
                {
                    "code": code,
                    "stage": stage,
                    "field": field,
                    "message": message,
                    "expected": expected,
                    "observed": observed_value,
                }
            )

        def normalize_locale(value: Any) -> str | None:
            if value is None:
                return None
            normalized = str(value).strip()
            if not normalized:
                return None
            return normalized.lower()

        def normalize_timezone(value: Any) -> str | None:
            if value is None:
                return None
            normalized = str(value).strip()
            if not normalized:
                return None
            return normalized

        def parse_feature_values(args: list[str], flag: str) -> set[str]:
            values: set[str] = set()
            for index, arg in enumerate(args):
                arg_text = str(arg)
                if arg_text == flag and index + 1 < len(args):
                    raw_value = str(args[index + 1])
                elif arg_text.startswith(f"{flag}="):
                    raw_value = arg_text.split("=", 1)[1]
                else:
                    continue
                for feature in raw_value.split(","):
                    feature_text = feature.strip()
                    if feature_text:
                        values.add(feature_text)
            return values

        def launch_args_request_webauthn_disable(args: list[str]) -> bool:
            chrome_features = parse_feature_values(args, "--disable-features")
            blink_features = parse_feature_values(args, "--disable-blink-features")
            return (
                "--disable-webauthn" in args
                or bool(
                    chrome_features.intersection(
                        {
                            "WebAuthentication",
                            "WebAuthnUI",
                            "WebAuthnPlatformAuthenticator",
                            "WebAuthnCrossDevice",
                            "WebAuthnExtensions",
                        }
                    )
                )
                or "WebAuthentication" in blink_features
            )

        def launch_args_disable_webauthn(args: list[str]) -> bool:
            chrome_features = parse_feature_values(args, "--disable-features")
            blink_features = parse_feature_values(args, "--disable-blink-features")
            return (
                "--disable-webauthn" in args
                and {
                    "WebAuthentication",
                    "WebAuthnUI",
                    "WebAuthnPlatformAuthenticator",
                    "WebAuthnCrossDevice",
                    "WebAuthnExtensions",
                }.issubset(chrome_features)
                and "WebAuthentication" in blink_features
            )

        def command_line_switch_value(args: list[str], flag: str) -> str | None:
            for index, arg in enumerate(args):
                arg_text = str(arg)
                if arg_text == flag and index + 1 < len(args):
                    return str(args[index + 1])
                if arg_text.startswith(f"{flag}="):
                    return arg_text.split("=", 1)[1]
            return None

        def normalize_proxy_for_audit(value: Any) -> str | None:
            if value is None:
                return None
            raw_text = str(value).strip()
            if not raw_text:
                return None
            normalized = _normalize_proxy(raw_text)
            parsed = urlparse(normalized)
            if not parsed.scheme or not parsed.hostname or not parsed.port:
                return normalized
            auth = ""
            if parsed.username is not None:
                auth = unquote(parsed.username)
                if parsed.password is not None:
                    auth += f":{unquote(parsed.password)}"
                auth += "@"
            return f"{parsed.scheme.lower()}://{auth}{parsed.hostname.lower()}:{parsed.port}"

        def mask_proxy_for_audit(value: Any) -> str | None:
            normalized = normalize_proxy_for_audit(value)
            if not normalized:
                return None
            parsed = urlparse(normalized)
            auth = ""
            if parsed.username is not None:
                auth = f"{unquote(parsed.username)}:***@"
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{auth}{host}{port}"

        def mask_command_line_args(args: list[str]) -> list[str]:
            output: list[str] = []
            redact_next_proxy = False
            for arg in args:
                arg_text = str(arg)
                if redact_next_proxy:
                    output.append(mask_proxy_for_audit(arg_text) or "<redacted>")
                    redact_next_proxy = False
                    continue
                for flag in ("--proxy-server", "--proxy-pac-url"):
                    prefix = f"{flag}="
                    if arg_text == flag:
                        output.append(flag)
                        redact_next_proxy = True
                        break
                    if arg_text.startswith(prefix):
                        proxy_value = arg_text.split("=", 1)[1]
                        output.append(f"{flag}={mask_proxy_for_audit(proxy_value) or '<redacted>'}")
                        break
                else:
                    output.append(arg_text)
            return output

        def extract_command_line(body_text: Any) -> dict[str, Any]:
            if not isinstance(body_text, str) or not body_text.strip():
                return {
                    "available": False,
                    "source": "chrome://version",
                    "arguments": [],
                    "error": "chrome://version body text is empty",
                }
            match = re.search(r"(?:^|\n)Command Line\s+([^\n]+)", body_text)
            if not match:
                return {
                    "available": False,
                    "source": "chrome://version",
                    "arguments": [],
                    "error": "chrome://version command line row missing",
                }
            raw = match.group(1).strip()
            try:
                tokens = shlex.split(raw)
            except ValueError:
                tokens = raw.split()
            if not tokens:
                return {
                    "available": False,
                    "source": "chrome://version",
                    "raw": raw,
                    "arguments": [],
                    "error": "chrome://version command line row had no tokens",
                }
            executable_path = tokens[0]
            arguments = [str(token) for token in tokens[1:]]
            return {
                "available": True,
                "source": "chrome://version",
                "raw": raw,
                "executable_path": executable_path,
                "arguments": arguments,
                "proxy_server": (
                    command_line_switch_value(arguments, "--proxy-server")
                    or command_line_switch_value(arguments, "--proxy-pac-url")
                ),
            }

        try:
            cloakbrowser_version = importlib.metadata.version("cloakbrowser")
        except importlib.metadata.PackageNotFoundError:
            cloakbrowser_version = None
            warnings.append("cloakbrowser package version unavailable")

        page = None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
        except Exception as exc:
            add_mismatch(
                "BROWSER_LAUNCH_FACT_AUDIT_FAILED",
                "observed_js",
                "page",
                "unable to access a browser page for launch fact audit",
                observed_value=str(exc),
            )

        if page is not None:
            try:
                observed = await page.evaluate("""() => ({
                    webdriver: navigator.webdriver,
                    userAgent: navigator.userAgent,
                    language: navigator.language,
                    languages: Array.from(navigator.languages || []),
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    screen: {
                        width: window.screen.width,
                        height: window.screen.height,
                        availWidth: window.screen.availWidth,
                        availHeight: window.screen.availHeight
                    },
                    inner: { width: window.innerWidth, height: window.innerHeight },
                    outer: { width: window.outerWidth, height: window.outerHeight },
                    devicePixelRatio: window.devicePixelRatio
                })""")
            except Exception as exc:
                add_mismatch(
                    "BROWSER_LAUNCH_FACT_AUDIT_FAILED",
                    "observed_js",
                    "surface_observed",
                    "unable to evaluate browser stealth JavaScript",
                    observed_value=str(exc),
                )

            try:
                capability_snapshot = await page.evaluate("""async () => {
                    const globalValue = globalThis;
                    const ctor = globalValue.PublicKeyCredential;
                    const snapshot = {
                        available: true,
                        publicKeyCredentialPresent: typeof ctor === "function",
                        publicKeyCredentialGetClientCapabilitiesPresent:
                            typeof ctor?.getClientCapabilities === "function",
                        navigatorCredentialsCreatePresent:
                            typeof navigator.credentials?.create === "function",
                        navigatorCredentialsGetPresent:
                            typeof navigator.credentials?.get === "function",
                        publicKeyCredentialClientCapabilities: null,
                    };
                    if (typeof ctor?.getClientCapabilities === "function") {
                        try {
                            const capabilityMap = await ctor.getClientCapabilities();
                            snapshot.publicKeyCredentialClientCapabilities = Object.fromEntries(
                                Object.entries(capabilityMap).filter(([, value]) => typeof value === "boolean")
                            );
                        } catch (error) {
                            snapshot.error = String(error);
                        }
                    }
                    return snapshot;
                }""")
            except Exception as exc:
                capability_snapshot = {"available": False, "error": str(exc)}

        version_page = None
        try:
            version_page = await context.new_page()
            await version_page.goto("chrome://version/", wait_until="domcontentloaded")
            body_text = await version_page.evaluate("() => document.body ? document.body.innerText : ''")
            command_line = extract_command_line(body_text)
        except Exception as exc:
            version_error = str(exc)
            if page is not None:
                try:
                    cdp_session = await context.new_cdp_session(page)
                    cdp_command_line = await cdp_session.send("Browser.getBrowserCommandLine")
                    raw_args = [str(arg) for arg in (cdp_command_line.get("arguments") or [])]
                    executable_path = raw_args[0] if raw_args and not raw_args[0].startswith("--") else None
                    arguments = raw_args[1:] if executable_path else raw_args
                    command_line = {
                        "available": True,
                        "source": "cdp",
                        "executable_path": executable_path,
                        "arguments": arguments,
                        "proxy_server": (
                            command_line_switch_value(arguments, "--proxy-server")
                            or command_line_switch_value(arguments, "--proxy-pac-url")
                        ),
                    }
                    warnings.append("chrome://version command line unavailable; used Browser.getBrowserCommandLine fallback")
                except Exception as cdp_exc:
                    command_line = {
                        "available": False,
                        "source": "unavailable",
                        "arguments": [],
                        "error": f"chrome://version: {version_error}; cdp: {cdp_exc}",
                    }
            else:
                command_line = {
                    "available": False,
                    "source": "unavailable",
                    "arguments": [],
                    "error": f"chrome://version: {version_error}; no page available for CDP fallback",
                }
        finally:
            if version_page is not None and version_page is not page:
                try:
                    await version_page.close()
                except Exception:
                    logger.debug("Failed to close chrome://version page", exc_info=True)

        webdriver = observed.get("webdriver")
        if webdriver is not False:
            add_mismatch(
                "BROWSER_LAUNCH_FACT_AUDIT_FAILED",
                "observed_js",
                "webdriver",
                "navigator.webdriver expected false",
                expected=False,
                observed_value=webdriver,
            )

        if not profile.get("geoip"):
            expected_locale = profile.get("locale")
            if expected_locale and normalize_locale(observed.get("language")) != normalize_locale(expected_locale):
                add_mismatch(
                    "BROWSER_LOCALE_MISMATCH",
                    "observed_js",
                    "language",
                    "navigator.language does not match requested locale",
                    expected=expected_locale,
                    observed_value=observed.get("language"),
                )
            expected_timezone = profile.get("timezone")
            if expected_timezone and observed.get("timezone") != expected_timezone:
                add_mismatch(
                    "BROWSER_TIMEZONE_MISMATCH",
                    "observed_js",
                    "timezone",
                    "observed timezone does not match requested timezone",
                    expected=expected_timezone,
                    observed_value=observed.get("timezone"),
                )

        screen = observed.get("screen") if isinstance(observed.get("screen"), dict) else {}
        expected_screen_width = profile.get("screen_width")
        expected_screen_height = profile.get("screen_height")
        if expected_screen_width and screen.get("width") != expected_screen_width:
            add_mismatch(
                "BROWSER_SCREEN_MISMATCH",
                "observed_js",
                "screen.width",
                "observed screen width does not match requested width",
                expected=expected_screen_width,
                observed_value=screen.get("width"),
            )
        if expected_screen_height and screen.get("height") != expected_screen_height:
            add_mismatch(
                "BROWSER_SCREEN_MISMATCH",
                "observed_js",
                "screen.height",
                "observed screen height does not match requested height",
                expected=expected_screen_height,
                observed_value=screen.get("height"),
            )

        recorded_launch = dict(launch_record or {})
        recorded_args = [str(arg) for arg in (recorded_launch.get("args") or requested_args or [])]
        expected_proxy = normalize_proxy_for_audit(
            recorded_launch.get("normalizedProxy")
            or recorded_launch.get("proxyInput")
            or profile.get("proxy")
        )
        recorded_proxy_transport = str(recorded_launch.get("proxyTransport") or ("sdk_kwarg" if expected_proxy else "none"))
        recorded_proxy_arg = (
            command_line_switch_value(recorded_args, "--proxy-server")
            or command_line_switch_value(recorded_args, "--proxy-pac-url")
        )
        observed_command_line_args = [str(arg) for arg in (command_line.get("arguments") or [])]
        observed_command_line_proxy = normalize_proxy_for_audit(command_line.get("proxy_server"))

        if command_line.get("available") is not True:
            add_mismatch(
                "BROWSER_LAUNCH_FACT_AUDIT_FAILED",
                "observed_command_line",
                "command_line",
                "browser command line audit is unavailable",
                expected="command line evidence",
                observed_value=command_line.get("error"),
            )
        elif any(
            arg == "--enable-automation" or arg.startswith("--enable-automation=")
            for arg in observed_command_line_args
        ):
            add_mismatch(
                "BROWSER_LAUNCH_ARGS_MISMATCH",
                "observed_command_line",
                "arguments",
                "browser command line contains --enable-automation",
                observed_value=mask_command_line_args(observed_command_line_args),
            )

        if expected_proxy:
            if recorded_proxy_transport == "command_line":
                normalized_recorded_proxy = normalize_proxy_for_audit(recorded_proxy_arg)
                if normalized_recorded_proxy != expected_proxy:
                    add_mismatch(
                        "BROWSER_PROXY_NOT_EFFECTIVE",
                        "recorded_launch",
                        "proxy",
                        "recorded direct-launch proxy args do not match the expected proxy",
                        expected=mask_proxy_for_audit(expected_proxy),
                        observed_value=mask_proxy_for_audit(normalized_recorded_proxy),
                    )
            else:
                normalized_recorded_proxy = normalize_proxy_for_audit(
                    recorded_launch.get("normalizedProxy") or recorded_launch.get("proxyInput")
                )
                if normalized_recorded_proxy != expected_proxy:
                    add_mismatch(
                        "BROWSER_PROXY_NOT_EFFECTIVE",
                        "recorded_launch",
                        "proxy",
                        "recorded proxy input does not match the expected proxy",
                        expected=mask_proxy_for_audit(expected_proxy),
                        observed_value=mask_proxy_for_audit(normalized_recorded_proxy),
                    )
            if observed_command_line_proxy != expected_proxy:
                add_mismatch(
                    "BROWSER_PROXY_NOT_EFFECTIVE",
                    "observed_command_line",
                    "proxy",
                    "observed browser command line proxy does not match the expected proxy",
                    expected=mask_proxy_for_audit(expected_proxy),
                    observed_value=mask_proxy_for_audit(observed_command_line_proxy),
                )

        explicit_disable_webauthn = launch_args_request_webauthn_disable(recorded_args)
        if explicit_disable_webauthn:
            if not launch_args_disable_webauthn(recorded_args):
                add_mismatch(
                    "BROWSER_LAUNCH_ARGS_MISMATCH",
                    "recorded_launch",
                    "arguments",
                    "recorded launch args do not include the full explicit WebAuthn disable policy",
                    expected=[
                        "--disable-webauthn",
                        "--disable-features=WebAuthentication,WebAuthnUI,WebAuthnPlatformAuthenticator,WebAuthnCrossDevice,WebAuthnExtensions",
                        "--disable-blink-features=WebAuthentication",
                    ],
                    observed_value=mask_command_line_args(recorded_args),
                )
            if not launch_args_disable_webauthn(observed_command_line_args):
                add_mismatch(
                    "BROWSER_LAUNCH_ARGS_MISMATCH",
                    "observed_command_line",
                    "arguments",
                    "observed browser command line does not include the full explicit WebAuthn disable policy",
                    expected=[
                        "--disable-webauthn",
                        "--disable-features=WebAuthentication,WebAuthnUI,WebAuthnPlatformAuthenticator,WebAuthnCrossDevice,WebAuthnExtensions",
                        "--disable-blink-features=WebAuthentication",
                    ],
                    observed_value=mask_command_line_args(observed_command_line_args),
                )
            if capability_snapshot.get("available") is not True:
                add_mismatch(
                    "BROWSER_CAPABILITY_POLICY_MISMATCH",
                    "observed_capability",
                    "capability_snapshot",
                    "browser capability snapshot is unavailable for explicit WebAuthn disable validation",
                    expected="PublicKeyCredential unavailable",
                    observed_value=capability_snapshot.get("error"),
                )
            else:
                if capability_snapshot.get("publicKeyCredentialPresent") is not False:
                    add_mismatch(
                        "BROWSER_CAPABILITY_POLICY_MISMATCH",
                        "observed_capability",
                        "PublicKeyCredential",
                        "PublicKeyCredential should be unavailable when explicit WebAuthn disable is requested",
                        expected=False,
                        observed_value=capability_snapshot.get("publicKeyCredentialPresent"),
                    )
                if capability_snapshot.get("navigatorCredentialsCreatePresent") is not False:
                    add_mismatch(
                        "BROWSER_CAPABILITY_POLICY_MISMATCH",
                        "observed_capability",
                        "navigator.credentials.create",
                        "navigator.credentials.create should be unavailable when explicit WebAuthn disable is requested",
                        expected=False,
                        observed_value=capability_snapshot.get("navigatorCredentialsCreatePresent"),
                    )
                if capability_snapshot.get("navigatorCredentialsGetPresent") is not False:
                    add_mismatch(
                        "BROWSER_CAPABILITY_POLICY_MISMATCH",
                        "observed_capability",
                        "navigator.credentials.get",
                        "navigator.credentials.get should be unavailable when explicit WebAuthn disable is requested",
                        expected=False,
                        observed_value=capability_snapshot.get("navigatorCredentialsGetPresent"),
                    )

        errors = dedupe([str(item["code"]) for item in mismatch_details])
        warnings = dedupe(warnings)
        launch_fact_audit = {
            "passed": not mismatch_details,
            "expectedFacts": {
                "proxyMasked": mask_proxy_for_audit(expected_proxy),
                "locale": profile.get("locale"),
                "timezone": profile.get("timezone"),
                "screenWidth": profile.get("screen_width"),
                "screenHeight": profile.get("screen_height"),
                "geoip": bool(profile.get("geoip", False)),
                "explicitDisableWebAuthn": explicit_disable_webauthn,
            },
            "recordedLaunch": {
                "launchPath": recorded_launch.get("launchPath") or "playwright_launch_persistent_context_async",
                "proxyTransport": recorded_proxy_transport,
                "args": mask_command_line_args(recorded_args),
                "proxyInputMasked": mask_proxy_for_audit(recorded_launch.get("proxyInput")),
                "normalizedProxyMasked": mask_proxy_for_audit(recorded_launch.get("normalizedProxy")),
                "envSummary": recorded_launch.get("envSummary") if isinstance(recorded_launch.get("envSummary"), dict) else {},
            },
            "observedFacts": {
                "commandLine": {
                    "available": command_line.get("available") is True,
                    "source": command_line.get("source"),
                    "executablePath": command_line.get("executable_path"),
                    "arguments": mask_command_line_args(observed_command_line_args),
                    "proxyMasked": mask_proxy_for_audit(command_line.get("proxy_server")),
                    "error": command_line.get("error"),
                },
                "jsSnapshot": observed,
                "capabilitySnapshot": capability_snapshot,
            },
            "mismatches": mismatch_details,
            "providerDiagnostics": {
                "launchPath": recorded_launch.get("launchPath") or "playwright_launch_persistent_context_async",
                "commandLineSource": command_line.get("source"),
            },
        }

        return {
            "passed": not errors,
            "checked_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "cloakbrowser_version": cloakbrowser_version,
            "errors": errors,
            "warnings": warnings,
            "observed": observed,
            "command_line": {
                "available": command_line.get("available") is True,
                "source": command_line.get("source"),
                "executable_path": command_line.get("executable_path"),
                "arguments": mask_command_line_args(observed_command_line_args),
                "proxy_masked": mask_proxy_for_audit(command_line.get("proxy_server")),
                "error": command_line.get("error"),
            },
            "requested_args": requested_args,
            "launchFactAudit": launch_fact_audit,
        }

    async def stop(self, profile_id: str):
        """Stop a running browser instance."""
        # Pop before close so _on_browser_closed() finds nothing to clean up
        async with self._lock:
            running = self.running.pop(profile_id, None)

        if not running:
            return

        logger.info("Stopping profile %s", profile_id)

        await self._stop_browser_runtime(running)
        await self.vnc.stop_vnc(running.display)

    def get_status(self, profile_id: str) -> dict[str, Any]:
        """Get running status for a profile."""
        running = self.running.get(profile_id)
        if running:
            stealth_integrity = getattr(running, "stealth_integrity", None)
            if not isinstance(stealth_integrity, dict):
                stealth_integrity = None
            launched_at = getattr(running, "launched_at", None)
            if not isinstance(launched_at, str):
                launched_at = None
            return {
                "status": "running",
                "vnc_ws_port": running.ws_port,
                "display": f":{running.display}",
                "cdp_url": f"/api/profiles/{profile_id}/cdp",
                "stealth_integrity": stealth_integrity,
                "launched_at": launched_at,
            }
        return {
            "status": "stopped",
            "vnc_ws_port": None,
            "display": None,
            "cdp_url": None,
            "stealth_integrity": None,
            "launched_at": None,
        }

    async def cleanup_all(self):
        """Stop all running profiles. Called on shutdown."""
        async with self._lock:
            profile_ids = list(self.running.keys())

        for pid in profile_ids:
            await self.stop(pid)

        await self.vnc.cleanup_all()

    async def cleanup_stale(self):
        """Kill orphan processes from previous container runs."""
        await self.vnc.cleanup_stale()

    async def auto_launch_all(self):
        """Launch all profiles with auto_launch=True. Called on startup."""
        from . import database as db

        profiles = db.list_profiles()
        auto_profiles = [p for p in profiles if p.get("auto_launch")]
        if not auto_profiles:
            logger.info("No profiles configured for auto-launch")
            return

        logger.info("Auto-launching %d profile(s)...", len(auto_profiles))
        for profile in auto_profiles:
            try:
                await asyncio.wait_for(self.launch(profile), timeout=60)
                logger.info("Auto-launched profile %s (%s)", profile["name"], profile["id"])
            except Exception as exc:
                logger.error(
                    "Auto-launch failed for profile %s (%s): %s",
                    profile["name"], profile["id"], exc,
                )
        logger.info("Auto-launch complete: %d running", len(self.running))

    def _allocate_cdp_port(self) -> int:
        """Find a free CDP port using a rotating counter to avoid TIME_WAIT collisions."""
        for _ in range(CDP_PORT_RANGE):
            port = self._next_cdp_port
            self._next_cdp_port = BASE_CDP_PORT + (
                (self._next_cdp_port + 1 - BASE_CDP_PORT) % CDP_PORT_RANGE
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise ValueError("No free CDP ports available in range %d-%d" % (BASE_CDP_PORT, BASE_CDP_PORT + CDP_PORT_RANGE - 1))

    def _build_fingerprint_args(self, profile: dict[str, Any]) -> list[str]:
        """Build extra Chromium args from profile fingerprint settings."""
        args: list[str] = [
            "--disable-infobars",
            "--test-type",  # suppress "unsupported flag: --no-sandbox" bad flags warning
            "--use-angle=swiftshader",  # software GL for VNC (no GPU in container)
        ]

        seed = profile.get("fingerprint_seed")
        if seed is not None:
            args.append(f"--fingerprint={seed}")

        p = profile.get("platform")
        if p:
            # Map our "macos" to binary's "macos"
            args.append(f"--fingerprint-platform={p}")

        vendor = profile.get("gpu_vendor")
        if vendor:
            args.append(f"--fingerprint-gpu-vendor={vendor}")

        renderer = profile.get("gpu_renderer")
        if renderer:
            args.append(f"--fingerprint-gpu-renderer={renderer}")

        hw = profile.get("hardware_concurrency")
        if hw is not None:
            args.append(f"--fingerprint-hardware-concurrency={hw}")

        sw = profile.get("screen_width")
        sh = profile.get("screen_height")
        if sw:
            args.append(f"--fingerprint-screen-width={sw}")
        if sh:
            args.append(f"--fingerprint-screen-height={sh}")

        return args
