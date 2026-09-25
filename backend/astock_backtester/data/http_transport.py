from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from threading import local as thread_local
from time import monotonic
from typing import Any

import requests

logger = logging.getLogger(__name__)

TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

# User agents are defined once here so they can be upgraded in a single place.
# MINIMAL_USER_AGENT intentionally omits platform details for endpoints that
# serve plain quote payloads; BROWSER_USER_AGENT is a full Chrome string for
# HTML pages that render server-side content per browser.
MINIMAL_USER_AGENT = "Mozilla/5.0"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def create_scraping_session() -> requests.Session:
    """Create a ``requests.Session`` for public-market scraping.

    Policy: ``trust_env=False`` so per-machine proxy/credential environment
    variables (``http_proxy``, ``HTTPS_PROXY``, ``NO_PROXY``, ... ) never
    hijack upstream market requests.  This matches the explicit
    ``proxies={}`` override used by the HTTP daily-bars adapter.
    """
    session = requests.Session()
    session.trust_env = False
    return session


_thread_local = thread_local()


def scraping_session() -> requests.Session:
    """Return this thread's proxy-immune scraping session.

    Provider dataclasses must default their ``requester`` to :func:`scraping_get`
    rather than to ``requests.get``.  ``requests.get`` builds a throw-away
    session with ``trust_env=True``, so it adopts ``HTTP_PROXY``/``HTTPS_PROXY``
    from the machine environment; a system proxy such as Clash Verge then
    terminates TLS to domestic market hosts (``SSL: UNEXPECTED_EOF_WHILE_READING``)
    and every upstream read on that path fails.  One session per thread keeps the
    connection reuse without sharing a ``Session`` across the provider thread
    pools.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = create_scraping_session()
        _thread_local.session = session
    return session


def scraping_get(url: str, **kwargs: Any) -> Any:
    """``requests.get`` equivalent that never reads proxy environment variables."""
    return scraping_session().get(url, **kwargs)


def _curl_get(url: str, **kwargs: Any) -> Any:
    from curl_cffi import requests as curl_requests

    kwargs.setdefault("impersonate", "chrome")
    return curl_requests.get(url, **curl_verify_kwargs(), **kwargs)


_ca_bundle_cache: str | None = None


def curl_ca_bundle() -> str | None:
    """Return a CA bundle path that libcurl can actually load, or ``None``.

    libcurl (and therefore ``curl_cffi``) fails with
    ``curl: (77) error setting certificate verify locations`` when the CA path
    contains non-ASCII characters — which is the case for any Windows machine
    whose user profile is not ASCII, because certifi ships inside the Python
    installation under ``C:\\Users\\<用户名>\\...``.  That silently disabled the
    entire ``curl_cffi`` fallback transport on such machines.

    The fix has two steps, cheapest first:

    1. Ask Windows for the 8.3 short path of the certifi bundle — it is pure
       ASCII and needs no copying or extra directories;
    2. otherwise copy the bundle next to the project's tooling / into
       ``%ProgramData%``, whose paths are ASCII by construction.

    Any failure returns ``None`` so callers keep libcurl's default lookup
    instead of breaking an already-working setup.
    """
    global _ca_bundle_cache
    if _ca_bundle_cache is not None:
        return _ca_bundle_cache or None
    try:
        import certifi

        source = Path(certifi.where())
        if str(source).isascii():
            _ca_bundle_cache = str(source)
            return _ca_bundle_cache
        short = _windows_short_path(str(source))
        if short and short.isascii():
            _ca_bundle_cache = short
            return _ca_bundle_cache
        target = _first_ascii_candidate(_ascii_ca_candidates())
        if target is None:
            # 没有任何 ASCII 落点：保持 libcurl 默认查找，并把"不可用"缓存下来。
            _ca_bundle_cache = ""
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.stat().st_size != source.stat().st_size:
                shutil.copyfile(source, target)
            _ca_bundle_cache = str(target)
    except Exception:  # noqa: BLE001 - CA relocation must never break requests
        logger.debug("curl CA bundle relocation skipped", exc_info=True)
        _ca_bundle_cache = ""
    return _ca_bundle_cache or None


def _first_ascii_candidate(targets: list[Path]) -> Path | None:
    """First candidate whose absolute path is pure ASCII (libcurl's constraint).

    非 ASCII 路径上 libcurl 直接报 error 77，把 CA 复制过去等于没修——所以
    路径不纯 ASCII 的候选必须整条跳过，而不是"挑最后一个凑数"。
    """
    for target in targets:
        if str(target).isascii():
            return target
    return None


def _windows_short_path(path: str) -> str | None:
    """Windows 8.3 short path (ASCII when the volume supports it)."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(4096)
        length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, 4096)
    except Exception:  # noqa: BLE001
        return None
    return buffer.value if length and buffer.value else None


def _ascii_ca_candidates() -> list[Path]:
    """Candidate CA destinations whose absolute paths are usually ASCII."""
    candidates: list[Path] = []
    program_data = os.environ.get("ProgramData")
    if program_data:
        candidates.append(Path(program_data) / "astock-ca" / "cacert.pem")
    # 项目内 .tools 是构建/工具产物的既有归属地，退一步再用它。
    candidates.append(Path(__file__).resolve().parents[3] / ".tools" / "ca" / "cacert.pem")
    return candidates


def curl_verify_kwargs() -> dict[str, Any]:
    """``{"verify": <ascii ca>}`` for curl_cffi calls, or ``{}`` when unavailable.

    Every ``curl_cffi`` call site must spread this in — sharing one helper is
    what keeps the workaround from being re-implemented per provider.
    """
    bundle = curl_ca_bundle()
    return {"verify": bundle} if bundle else {}


def _attempt_timeout(timeout: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("public HTTP request budget exhausted")
    return min(timeout, remaining)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is not None and response.status_code in TRANSIENT_STATUS_CODES
    return isinstance(exc, (requests.ConnectionError, requests.Timeout, TimeoutError, OSError))


def should_allow_alternate_transport(
    requester: Callable[..., Any],
    override: bool | None = None,
) -> bool:
    """Determine whether a provider should fall back to curl_cffi transport.

    Previously this logic was duplicated as ``_allow_public_alternate_transport``
    on both ``MarketNewsProvider`` and ``RealtimeMarketProvider``.  Centralising
    it here keeps the policy in one place while leaving the per-provider
    override knob intact.  :func:`scraping_get` counts as a public requester
    because it is the ``trust_env=False`` stand-in for ``requests.get``.
    """
    if override is not None:
        return override
    return requester in (requests.get, scraping_get)


def resilient_get(
    requester: Callable[..., Any],
    url: str,
    *,
    timeout: float,
    source: str,
    diagnostics: list[str] | None = None,
    retries: int = 1,
    deadline: float | None = None,
    alternate_requester: Callable[..., Any] | None = None,
    allow_alternate: bool = False,
    **kwargs: Any,
) -> Any:
    diagnostics = diagnostics if diagnostics is not None else []
    attempts = max(1, retries + 1)
    primary_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = requester(url, timeout=_attempt_timeout(timeout, deadline), **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:
            primary_error = exc
            diagnostics.append(f"{source} primary attempt {attempt}/{attempts} failed: {exc}")
            if not _is_transient(exc) or attempt == attempts:
                break

    if allow_alternate:
        alternate = alternate_requester or _curl_get
        try:
            response = alternate(url, timeout=_attempt_timeout(timeout, deadline), **kwargs)
            response.raise_for_status()
            diagnostics.append(f"{source} alternate transport used after primary failure.")
            return response
        except Exception as exc:
            diagnostics.append(f"{source} alternate transport failed: {exc}")
            raise exc from primary_error

    if primary_error is not None:
        raise primary_error
    raise RuntimeError(f"{source} request failed without an error")
