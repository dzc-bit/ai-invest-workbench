"""curl_cffi 备用传输的 CA 修复守卫。

libcurl 在非 ASCII 的 CA 路径上直接报 ``curl: (77) error setting certificate
verify locations``——任何用户名含中文的 Windows 都会因此整条 curl_cffi 备用
传输静默失效。这里锁两件事：ASCII 路径直接复用；非 ASCII 路径能解析出可加载
的 ASCII 替代（短路径或复制到 ASCII 目录），且不破坏 TLS 校验。
"""

from __future__ import annotations

import sys
import types

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
