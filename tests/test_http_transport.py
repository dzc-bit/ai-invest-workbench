"""curl_cffi 备用传输的 CA 修复守卫。

libcurl 在非 ASCII 的 CA 路径上直接报 ``curl: (77) error setting certificate
verify locations``——任何用户名含中文的 Windows 都会因此整条 curl_cffi 备用
传输静默失效。这里锁两件事：ASCII 路径直接复用；非 ASCII 路径能解析出可加载
的 ASCII 替代（短路径或复制到 ASCII 目录），且不破坏 TLS 校验。
"""

from __future__ import annotations

import logging
import sys
import types

import pytest
from astock_backtester.data import http_transport


def _fake_certifi(where_result: str) -> types.ModuleType:
    module = types.ModuleType("certifi")
    module.where = lambda: where_result  # type: ignore[method-assign]
    return module


def test_curl_ca_bundle_reuses_ascii_certifi_path(monkeypatch):
    monkeypatch.setitem(sys.modules, "certifi", _fake_certifi(r"C:\Python313\certifi\cacert.pem"))
    monkeypatch.setattr(http_transport, "_ca_bundle_cache", None)

    assert http_transport.curl_ca_bundle() == r"C:\Python313\certifi\cacert.pem"
    assert http_transport.curl_verify_kwargs() == {"verify": r"C:\Python313\certifi\cacert.pem"}


def test_first_ascii_candidate_skips_non_ascii_paths():
    """libcurl 的约束是"路径必须 ASCII"：非 ASCII 候选要整条跳过，不能凑数。"""
    candidates = [
        http_transport.Path(r"C:\Users\中文用户\AppData\Local\Temp\astock-ca\cacert.pem"),
        http_transport.Path(r"C:\ProgramData\astock-ca\cacert.pem"),
    ]
    assert http_transport._first_ascii_candidate(candidates) == candidates[1]
    assert http_transport._first_ascii_candidate(candidates[:1]) is None


def test_curl_ca_bundle_relocates_non_ascii_certifi_path(monkeypatch, tmp_path):
    non_ascii_source = tmp_path / "证书目录" / "cacert.pem"
    non_ascii_source.parent.mkdir(parents=True)
    non_ascii_source.write_text("FAKE-CA", encoding="utf-8")
    assert not str(non_ascii_source).isascii()

    target = tmp_path / "ascii-ca" / "cacert.pem"
    monkeypatch.setitem(sys.modules, "certifi", _fake_certifi(str(non_ascii_source)))
    # 短路径在测试环境不可控（部分卷未启用 8.3），这里关掉走"复制"分支；
    # 候选选择逻辑单独由 test_first_ascii_candidate 锁定。
    monkeypatch.setattr(http_transport, "_windows_short_path", lambda _path: None)
    monkeypatch.setattr(http_transport, "_first_ascii_candidate", lambda _targets: target)
    monkeypatch.setattr(http_transport, "_ca_bundle_cache", None)

    resolved = http_transport.curl_ca_bundle()

    assert resolved == str(target)
    assert target.exists() and target.read_text(encoding="utf-8") == "FAKE-CA"
    assert http_transport.curl_verify_kwargs() == {"verify": resolved}


def test_curl_ca_bundle_failure_keeps_default_lookup(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "certifi", _fake_certifi(str(tmp_path / "不存在目录" / "中文" / "cacert.pem")))
    monkeypatch.setattr(http_transport, "_windows_short_path", lambda _path: None)
    monkeypatch.setattr(http_transport, "_first_ascii_candidate", lambda _targets: None)
    monkeypatch.setattr(http_transport, "_ca_bundle_cache", None)

    assert http_transport.curl_ca_bundle() is None
    assert http_transport.curl_verify_kwargs() == {}


def _fake_clock():
    """Deterministic monotonic clock whose sleep advances it (no real waiting)."""
    now = [0.0]

    def clock() -> float:
        return now[0]

    def advance(seconds: float) -> None:
        now[0] += seconds

    return clock, advance


def test_host_throttle_spaces_requests_to_the_same_host():
    """同一 host 的两次出站请求必须相隔 >= min_interval，不同 host 各有各的额度。"""
    throttle_cls = getattr(http_transport, "HostThrottle", None)
    assert isinstance(throttle_cls, type), "http_transport must own the per-host throttle policy"

    clock, advance = _fake_clock()
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        advance(seconds)

    throttle = throttle_cls(0.1, clock=clock, sleep=fake_sleep)

    throttle.wait("https://push2.eastmoney.com/api/qt/stock/get")
    assert slept == []

    throttle.wait("https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get")
    assert slept == [0.1]

    # 不同 host 有自己的额度，不互相等待
    throttle.wait("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get")
    assert slept == [0.1]

    throttle.wait("https://push2.eastmoney.com/api/qt/stock/get")
    assert slept == [0.1, 0.1]


def test_host_throttle_is_bypassed_when_interval_is_zero():
    """min_interval <= 0 时完全旁路（注入式构造的测试不能被限速拖慢）。"""
    throttle_cls = getattr(http_transport, "HostThrottle", None)
    assert isinstance(throttle_cls, type), "http_transport must own the per-host throttle policy"

    slept: list[float] = []
    throttle = throttle_cls(0, clock=lambda: 0.0, sleep=slept.append)

    throttle.wait("https://qt.gtimg.cn/q=sh600519")
    throttle.wait("https://qt.gtimg.cn/q=sz000001")

    assert slept == []


def test_resilient_get_warns_once_when_alternate_transport_fails(caplog, monkeypatch):
    """备用传输第一次失败要一次性可见告警，之后静默；diagnostics 与异常语义不变。

    全离线：主/备传输都用注入的假 callable，绝不打真实网络。
    """
    monkeypatch.setattr(http_transport, "_alternate_transport_failed_once", False)

    def primary_requester(url, **kwargs):
        raise ConnectionResetError("primary transport reset")

    def alternate_requester(url, **kwargs):
        raise TimeoutError("curl_cffi connect timeout")

    diagnostics: list[str] = []
    call_kwargs = {
        "timeout": 1.0,
        "source": "unit-test",
        "diagnostics": diagnostics,
        "retries": 0,
        "alternate_requester": alternate_requester,
        "allow_alternate": True,
    }

    def warnings() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == http_transport.logger.name and record.levelno >= logging.WARNING
        ]

    with caplog.at_level(logging.WARNING, logger=http_transport.logger.name):
        with pytest.raises(TimeoutError, match="curl_cffi connect timeout"):
            http_transport.resilient_get(primary_requester, "https://example.invalid/quote", **call_kwargs)

        assert len(warnings()) == 1, "备用传输首次失败必须打一条 warning"
        message = warnings()[0]
        assert "备用传输" in message
        assert "curl_cffi" in message
        assert "仅主传输" in message
        assert "TimeoutError" in message

        with pytest.raises(TimeoutError, match="curl_cffi connect timeout"):
            http_transport.resilient_get(primary_requester, "https://example.invalid/quote", **call_kwargs)

        assert len(warnings()) == 1, "第二次及以后不得重复告警（全市场补齐会刷屏）"

    # 异常照常抛出、diagnostics 语义不改：每次请求都带主/备失败记录。
    assert sum(1 for entry in diagnostics if "primary attempt" in entry) == 2
    assert sum(1 for entry in diagnostics if "alternate transport failed" in entry) == 2
