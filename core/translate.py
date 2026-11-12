# -*- coding: utf-8 -*-
"""原始输入事件 -> 语义动作。

把 getevent 的一堆 EV_ABS/EV_SYN 帧还原成 tap / long_press / swipe /
double_tap / keyevent / wait，坐标归一化为 0~1，写进用例后可直接手改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
import copy

from .listener import RawEvent
from .probe import touch_abs_of


def _r(v: float, n: int = 4) -> float:
    return round(min(1.0, max(0.0, v)), n)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


@dataclass
class Slot:
    tracking_id: int
    t_down: float
    x: float = 0.0
    y: float = 0.0
    points: list[tuple[float, float]] = field(default_factory=list)
    released: bool = False
    last_t: float = 0.0

    @property
    def start(self) -> tuple[float, float]:
        return self.points[0] if self.points else (self.x, self.y)

    @property
    def end(self) -> tuple[float, float]:
        return self.points[-1] if self.points else (self.x, self.y)


@dataclass
class Frame:
    active_slot: int = 0
    slots: dict[int, Slot] = field(default_factory=dict)


class Translator:
    def __init__(self, cfg: dict, profile: dict, on_action=None):
        """on_action(ts, action) -> 截图序号或 None；用于录制期逐步抓屏。"""
        self.cfg = cfg
        self.profile = profile
        self.on_action = on_action
        abs_x, abs_y = touch_abs_of(profile)
        self.abs_x, self.abs_y = float(max(1, abs_x)), float(max(1, abs_y))
        self.swap_xy = bool(cfg.get("swap_xy", False))
        # 不同来源的坐标量纲不同：设备面板常是屏幕的整数倍（如 9 倍），
        # 投屏合成事件则是视频像素。按事件来源分别归一化，两类事件才能
        # 录进同一条时间轴（只需登记“这个来源的 abs 上限是多少”）。
        self.node_abs: dict[str, tuple[float, float]] = {}
        self.frames: dict[str, Frame] = {}
        self.emitted: list[tuple[float, int, dict]] = []  # (起始时间, 序号, 动作)
        self.raw: list[dict] = []
        self._seq = 0
        self._first_ts: float | None = None
        # 借鉴 GameScrcpy RecordingConvert.cpp:90 的时间量化：
        # 所有毫秒值取整到 step（默认 10ms），用例更干净、回放更确定
        self.quantize_ms = int(cfg.get("time_quantize_ms", 10))
        self.record_at_ms = bool(cfg.get("record_at_ms", True))

    def _quant(self, ms: float) -> int:
        q = self.quantize_ms
        return int(ms) if q <= 1 else int(round(float(ms) / q) * q)

    # ---- 坐标 ----
    def _norm(self, x: float, y: float, node: str | None = None) -> tuple[float, float]:
        spec = self.node_abs.get(node) if node else None
        if spec is not None:
            # 自带屏幕坐标系的来源（投屏合成事件）：按它自己的尺度归一化，不做 swap
            return _r(x / max(1e-6, spec[0])), _r(y / max(1e-6, spec[1]))
        if self.swap_xy:
            x, y = y, x
        return _r(x / self.abs_x), _r(y / self.abs_y)

    # ---- 动作产出 ----
    def _emit(self, ts: float, action: dict) -> None:
        if self.on_action is not None:
            seq = self.on_action(ts, action)
            if seq is not None:
                action["_shot"] = seq
        if self._first_ts is not None:
            action["_at_ms"] = self._quant((ts - self._first_ts) * 1000)
        self.emitted.append((ts, self._seq, action))
        self._seq += 1

    def _keycode(self, key: str) -> tuple[str, bool]:
        aliases = self.cfg.get("key_aliases") or {}
        if key in aliases:
            return aliases[key], True
        guessed = "KEYCODE_" + key[4:] if key.startswith("KEY_") else key
        return guessed, False

    def _settle(self, node: str, slot_id: int, slot: Slot, ts: float) -> None:
        if not slot.points:
            slot.points.append((slot.x, slot.y))
        duration_ms = self._quant(max(0.0, (ts - slot.t_down)) * 1000)
        start, end = slot.start, slot.end
        norm_start = self._norm(start[0], start[1], node)
        norm_end = self._norm(end[0], end[1], node)
        travel = _dist(norm_start, norm_end)
        slop = float(self.cfg.get("tap_slop", 0.02))
        long_ms = int(self.cfg.get("long_press_ms", 500))

        if travel < slop and duration_ms < long_ms:
            nx, ny = norm_end
            self._emit(slot.t_down, {
                "action": "tap",
                "at": [nx, ny],
                "duration_ms": duration_ms,
                "desc": f"点击 ({nx}, {ny})",
            })
        elif travel < slop:
            nx, ny = norm_end
            self._emit(slot.t_down, {
                "action": "long_press",
                "at": [nx, ny],
                "duration_ms": duration_ms,
                "desc": f"长按 ({nx}, {ny}) {duration_ms}ms",
            })
        else:
            fx, fy = norm_start
            tx, ty = norm_end
            step: dict = {
                "action": "swipe",
                "from": [fx, fy],
                "to": [tx, ty],
                "duration_ms": max(1, duration_ms),
                "desc": f"滑动 ({fx}, {fy}) -> ({tx}, {ty})",
            }
            max_pts = int(self.cfg.get("swipe_sample_points", 12))
            if max_pts > 1 and len(slot.points) > 2:
                idx = [round(i * (len(slot.points) - 1) / (max_pts - 1))
                       for i in range(max_pts)]
                seen, path = set(), []
                for i in idx:
                    if i in seen:
                        continue
                    seen.add(i)
                    p = self._norm(slot.points[i][0], slot.points[i][1], node)
                    path.append([p[0], p[1]])
                step["path"] = path
            self._emit(slot.t_down, step)

    def inject_text(self, text: str, ts: float) -> None:
        """投屏物理键盘注入的可打印文本 -> text 动作。

        设备 getevent 抓不到 scrcpy 的 INJECT_TEXT，只能由 GUI 侧显式补录；
        走与按键相同的 _emit 流水线（时间量化 / 截图 / at_ms 一致）。
        """
        if not text:
            return
        self._emit(ts, {"action": "text", "text": text,
                        "desc": f"输入文本 {text!r}"})

    def feed(self, events: Iterable[RawEvent]) -> None:
        for ev in events:
            self._feed_one(ev)

    def _feed_one(self, ev: RawEvent) -> None:
        if self._first_ts is None:
            self._first_ts = ev.ts
        if self.cfg.get("keep_raw_events"):
            self.raw.append(ev.as_dict())

        fr = self.frames.setdefault(ev.node, Frame())

        if ev.kind == "EV_ABS":
            if ev.code == "ABS_MT_SLOT":
                fr.active_slot = max(0, ev.value)
                return
            slot = fr.slots.get(fr.active_slot)
            if ev.code == "ABS_MT_TRACKING_ID":
                if ev.value < 0:  # 抬手
                    if slot:
                        self._settle(ev.node, fr.active_slot, slot, ev.ts)
                        fr.slots.pop(fr.active_slot, None)
                else:
                    fr.slots[fr.active_slot] = Slot(tracking_id=ev.value, t_down=ev.ts,
                                                    last_t=ev.ts)
                return
            if slot is None:
                # 单点协议（ABS_X/ABS_Y）没有 TRACKING_ID，按需补一个 slot
                slot = fr.slots.setdefault(0, Slot(tracking_id=0, t_down=ev.ts, last_t=ev.ts))
            if ev.code in ("ABS_MT_POSITION_X", "ABS_X"):
                slot.x = float(ev.value)
            elif ev.code in ("ABS_MT_POSITION_Y", "ABS_Y"):
                slot.y = float(ev.value)
            slot.last_t = ev.ts

        elif ev.kind == "EV_SYN":
            if ev.code == "SYN_REPORT":
                for s in fr.slots.values():
                    pt = (s.x, s.y)  # 原始内核坐标，归一化统一在 _settle 完成
                    if not s.points or _dist(s.points[-1], pt) >= 2.0:
                        s.points.append(pt)

        elif ev.kind == "EV_KEY":
            if ev.code == "BTN_TOUCH" and ev.value == 0:
                for sid, s in list(fr.slots.items()):
                    self._settle(ev.node, sid, s, ev.ts)
                    fr.slots.pop(sid, None)
            elif ev.code.startswith("KEY_") and ev.value == 1:
                keycode, known = self._keycode(ev.code)
                action = {"action": "keyevent", "key": keycode,
                          "desc": f"按键 {keycode}（原始 {ev.code}）"}
                if not known:
                    action["note"] = "键名由 Linux KEY_* 推断，若不生效请改成设备上正确的 KEYCODE"
                self._emit(ev.ts, action)

    def _build_steps(self) -> list[dict]:
        """把已结算的动作整理成用例步骤。

        深拷贝动作，保证可以反复调用（录制草稿会周期性做快照），
        不会污染 emitted 里的 _shot / _at_ms 内部字段。
        """
        items = sorted(self.emitted, key=lambda x: (x[0], x[1]))
        first = self._first_ts or (items[0][0] if items else 0.0)

        # 双击合并：相邻两次 tap、时间接近、位置接近
        gap_ms = int(self.cfg.get("double_tap_gap_ms", 320))
        slop = float(self.cfg.get("tap_slop", 0.02))
        merged: list[list] = []
        for ts, seq, action in items:
            if (gap_ms > 0 and merged
                    and action["action"] == "tap"
                    and merged[-1][2]["action"] == "tap"
                    and (ts - merged[-1][0]) * 1000 <= gap_ms
                    and _dist(tuple(merged[-1][2]["at"]), tuple(action["at"])) < slop):
                prev = merged[-1][2]
                combined = {"action": "double_tap", "at": prev["at"],
                            "desc": f"双击 {tuple(prev['at'])}",
                            "_at_ms": prev.get("_at_ms", action.get("_at_ms", 0))}
                # 合并后保留「后一次」的截图基线，它代表最终界面状态
                shot = action.get("_shot", prev.get("_shot"))
                if shot is not None:
                    combined["_shot"] = shot
                merged[-1][2] = combined
            else:
                merged.append([ts, seq, action])

        # 动作之间的空隙补 wait；借鉴 RecordingConvert.cpp:152-183 的 sleep(delta) 保时序
        threshold = int(self.cfg.get("wait_threshold_ms", 150))
        max_wait = int(self.cfg.get("max_wait_ms", 15000))
        steps: list[dict] = []
        prev_t: float | None = None
        for ts, _, src in merged:
            at = self._quant((ts - first) * 1000)
            if prev_t is not None:
                delta = self._quant((ts - prev_t) * 1000)
                if delta > threshold:
                    wait_ms = min(delta, max_wait)
                    wait: dict = {"action": "wait", "ms": wait_ms,
                                  "desc": f"等待 {wait_ms}ms"}
                    # wait 的 at_ms = 下一个动作发生的时刻（时间轴回放时据此对齐）
                    if self.record_at_ms:
                        wait["at_ms"] = at
                    steps.append(wait)
            a = copy.deepcopy(src)
            at_ms = int(a.pop("_at_ms", at))
            if self.record_at_ms:
                a["at_ms"] = at_ms
            if "duration_ms" in a:
                a["duration_ms"] = max(1, self._quant(a["duration_ms"]))
            steps.append(a)
            prev_t = ts
        return steps

    def snapshot(self) -> list[dict]:
        """录制中可安全调用的中间快照（不结算仍按着的手指、不触发截图回调）。"""
        return self._build_steps()

    def finish(self) -> list[dict]:
        # 录制被中断时，把仍按着的点也结算掉
        for node, fr in self.frames.items():
            for sid, s in list(fr.slots.items()):
                if s.points:
                    self._settle(node, sid, s, s.last_t or s.t_down)
                    fr.slots.pop(sid, None)
        return self._build_steps()
