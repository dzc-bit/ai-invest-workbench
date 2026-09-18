---
description: 启动本地数据服务 sidecar 并探测全部关键接口
---

对本地数据服务做一轮接口探针（参考 `AGENTS.md` §11）。步骤：

1. 用 Python `subprocess.Popen([...])` 参数数组启动 sidecar（路径含空格和中文，禁止拼未转义字符串）：

```python
import json, subprocess, sys, time
import urllib.request

proc = subprocess.Popen(
    [
        r".venv\Scripts\python.exe",  # 或项目内 Python：.tools\python-build\Scripts\python.exe
        "-m", "astock_backtester.service",
        "--host", "127.0.0.1",
        "--port", "9101",
        "--cache-dir", r"D:\New project 6\.astock-cache",
    ],
    cwd=r"D:\New project 6",
)
time.sleep(3)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 回环不走系统代理
base = "http://127.0.0.1:9101"

for path in ["/ping", "/health", "/market/finance", "/ai/status", "/diagnostics/sources"]:
    with opener.open(base + path, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    print(path, "->", resp.status, str(payload)[:120])

# /run/backtest/stream 是 NDJSON：逐行读，断言最后一条 type == result
request = urllib.request.Request(
    base + "/run/backtest/stream",
    data=json.dumps({"strategy": {...}, "settings": {...}}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with opener.open(request, timeout=60) as resp:
    events = [json.loads(line) for line in resp.read().decode("utf-8").splitlines() if line.strip()]
assert events[-1]["type"] == "result", events[-1]
print("backtest stream OK, last event keys:", sorted(events[-1]["result"].keys()))

proc.terminate()
```

2. 探针覆盖清单：`GET /ping`、`GET /health`、`GET /market/finance`、`GET /ai/status`、`GET /diagnostics/sources`、`POST /run/backtest/stream`（NDJSON 逐行解析）。若 AI 已配置（`运行产物/AI配置/ai-config.json` 存在），追加 `POST /ai/conditions/parse`（`{"text": "收盘价站上20日均线"}`）与 `POST /ai/insight/oneshot`（`{"scene": "results_overview", "context": {}}`）。
3. NDJSON 接口不能当普通 JSON 判断；断言最后一条事件的 `type`。
4. 探针脚本跑完即删，不落地长期 `.py`/`.ps1`/`.json`。
