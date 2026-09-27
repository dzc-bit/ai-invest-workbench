"""协作取消：一次请求的停止信号与在途轮次登记。

停止不是杀线程、也不是只断开前端。worker 只在**安全边界**查询令牌——模型轮次
之间、模型流的 token 之间、工具批次里每个工具启动之前、寻优的每个组合之间——
已经开始的写入跑到边界再退出，会话保存与会话锁释放都发生在退出之后。因此令牌
必须比 worker 活得久一点，也不能被两个轮次共用。
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock
from typing import Any


class TurnCancelled(Exception):
    """worker 在安全边界发现已取消时抛出的控制流异常（不是错误）。"""


class CancelToken:
    """一次请求的取消信号。``Event`` 意味着幂等：重复 cancel 无副作用。"""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TurnCancelled


class TurnRegistry:
    """``key -> 当前轮次令牌``。同一把键上同时只允许一个轮次（会话锁已保证），
    所以登记用键而不是用请求 id；worker 结束（含保存）后才摘除自己那一份。"""

    def __init__(self) -> None:
        self._tokens: dict[str, CancelToken] = {}
        self._lock = Lock()

    def register(self, key: str) -> tuple[CancelToken, Callable[[], None]]:
        token = CancelToken()
        with self._lock:
            self._tokens[key] = token

        def release() -> None:
            with self._lock:
                # 只摘自己：后一个轮次可能已经覆盖了同一个键。
                if self._tokens.get(key) is token:
                    del self._tokens[key]

        return token, release

    def cancel(self, key: str) -> bool:
        """返回是否真的有一个在途轮次被标记为取消。"""
        with self._lock:
            token: Any = self._tokens.get(key)
        if token is None:
            return False
        token.cancel()
        return True

    def active_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._tokens)
