# -*- coding: utf-8 -*-
"""录制草稿：周期性原子写入中间用例（CLI 与 GUI 共用，无 Qt 依赖）。

借鉴 GameScrcpy Recorder.cpp:105-171 的 rewriteEvery + writeFileAtomically：
长录制中途崩溃/断电，仍能保留最后一次已识别的步骤。
"""

from __future__ import annotations

import threading
from pathlib import Path

from .cases import new_case, save_case
from .image_baseline import inject_baselines


class DraftWriter:
    def __init__(self, translator, profile: dict, out_path: str | Path,
                 fmt: str, interval_s: float, name: str,
                 tags: list | None = None, params: dict | None = None,
                 log=None):
        self.tr = translator
        self.profile = profile
        out = Path(out_path)
        self.path = out.with_name(out.stem + ".draft" + out.suffix)
        self.fmt = fmt
        self.interval = max(1.0, float(interval_s))
        self.name = name
        self.tags = tags or []
        self.params = params or {}
        self._log = log
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="adbcase-draft")

    def start(self) -> None:
        self._thread.start()
        if self._log:
            self._log(f"[草稿] 每 {self.interval:g}s 原子写入 {self.path}（正常结束后删除）")

    def write_once(self) -> None:
        # snapshot 不结算仍按着的手指、不触发截图回调，可安全并发调用
        steps = inject_baselines(self.tr.snapshot(), {}, policy="none")
        case = new_case(self.name + "（录制中草稿）", self.profile, steps,
                        tags=self.tags, params=self.params)
        save_case(case, self.path, self.fmt)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.write_once()
            except Exception:
                pass  # 草稿失败绝不影响主录制

    def stop(self, *, remove: bool = True) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        if remove:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
