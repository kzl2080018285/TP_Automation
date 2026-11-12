# -*- coding: utf-8 -*-
"""getevent 实时流解析。

支持两种输出格式（自动识别）：
  label : [ 53890.813990] /dev/input/event4: EV_ABS  ABS_MT_POSITION_X  000001be
  hex   : [ 53890.813990] /dev/input/event4: 0003    0035              000001be
hex -> 符号名的映射是 Linux input 协议常量，不是设备相关的硬编码表。
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Iterable, Iterator

LINE = re.compile(
    r"^\s*\[\s*(\d+\.\d+)\]\s+(/dev/input/event\d+)\s*:\s*"
    r"(\S+)\s+(\S+)\s+([0-9a-fA-F]+)\s*$"
)

EV_TYPES = {0x00: "EV_SYN", 0x01: "EV_KEY", 0x02: "EV_REL", 0x03: "EV_ABS",
            0x04: "EV_MSC", 0x05: "EV_SW", 0x11: "EV_LED", 0x12: "EV_SND"}
ABS_CODES = {
    0x00: "ABS_X", 0x01: "ABS_Y", 0x02: "ABS_Z", 0x18: "ABS_PRESSURE",
    0x2F: "ABS_MT_SLOT", 0x30: "ABS_MT_TOUCH_MAJOR", 0x31: "ABS_MT_TOUCH_MINOR",
    0x32: "ABS_MT_WIDTH_MAJOR", 0x33: "ABS_MT_WIDTH_MINOR", 0x34: "ABS_MT_ORIENTATION",
    0x35: "ABS_MT_POSITION_X", 0x36: "ABS_MT_POSITION_Y", 0x37: "ABS_MT_TOOL_TYPE",
    0x38: "ABS_MT_BLOB_ID", 0x39: "ABS_MT_TRACKING_ID", 0x3A: "ABS_MT_PRESSURE",
    0x3B: "ABS_MT_DISTANCE", 0x3C: "ABS_MT_TOOL_X", 0x3D: "ABS_MT_TOOL_Y",
}
SYN_CODES = {0x00: "SYN_REPORT", 0x01: "SYN_CONFIG", 0x02: "SYN_MT_REPORT"}
BTN_CODES = {
    0x00: "BTN_MISC", 0x01: "BTN_0", 0x110: "BTN_LEFT", 0x14A: "BTN_TOUCH",
    0x14B: "BTN_STYLUS", 0x14C: "BTN_STYLUS2", 0x14D: "BTN_TOOL_DOUBLETAP",
    0x14E: "BTN_TOOL_TRIPLETAP",
}
KEY_CODES = {
    0x001: "KEY_ESC", 0x01C: "KEY_ENTER", 0x066: "KEY_HOME", 0x067: "KEY_DPAD_UP",
    0x069: "KEY_DPAD_LEFT", 0x06A: "KEY_DPAD_RIGHT", 0x06C: "KEY_DPAD_DOWN",
    0x06D: "KEY_VOLUMEUP", 0x06E: "KEY_VOLUMEDOWN", 0x072: "KEY_VOLUMEDOWN",
    0x073: "KEY_VOLUMEUP", 0x074: "KEY_POWER", 0x08B: "KEY_MENU", 0x09E: "KEY_BACK",
    0x0D9: "KEY_SEARCH", 0x0E2: "KEY_MEDIA_PLAY_PAUSE", 0x0E3: "KEY_MEDIA_STOP",
    0x0E4: "KEY_MEDIA_PREVIOUS", 0x0E5: "KEY_MEDIA_NEXT", 0x0E6: "KEY_MEDIA_REWIND",
    0x0E7: "KEY_MEDIA_FAST_FORWARD", 0x0E8: "KEY_MUTE", 0x160: "KEY_DPAD_CENTER",
    0x1A0: "KEY_CAMERA", 0x1A2: "KEY_SOUND", 0x1A4: "KEY_BRIGHTNESS_UP",
    0x1A5: "KEY_BRIGHTNESS_DOWN",
}
ERROR_HINTS = ("usage:", "getevent:", "not found", "unknown option", "invalid option",
               "permission denied", "no such file", "unrecognized", "bad option")


class GeteventModeError(RuntimeError):
    """设备不支持 -l 或无法读取 /dev/input，需要改用 hex 模式 / 检查权限。"""


class EventStreamController:
    """给 GUI 用的停止开关。

    在 Windows 上 `for line in proc.stdout` 会一直阻塞，单纯置标志没用；
    stop() 同时终止 adb 客户端进程，令管道 EOF，生成器自然收尾并走 finally。
    多节点监听会起多个进程，stop() 一并终止（procs 列表）。
    """

    def __init__(self):
        self._stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.procs: list[subprocess.Popen] = []

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def attach(self, proc: subprocess.Popen) -> None:
        """登记子进程：已 stop 时立刻终止，避免 stop 与启动竞争时漏杀。"""
        self.procs.append(proc)
        if self.proc is None:
            self.proc = proc
        if self._stop.is_set() and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        for p in self.procs or ([self.proc] if self.proc is not None else []):
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                except OSError:
                    pass


@dataclass(frozen=True)
class RawEvent:
    ts: float
    node: str
    kind: str
    code: str
    value: int

    def as_dict(self) -> dict:
        return {"ts": self.ts, "node": self.node, "kind": self.kind,
                "code": self.code, "value": self.value}


def _signed(hex_str: str) -> int:
    v = int(hex_str, 16)
    return v - 0x100000000 if v >= 0x80000000 else v


def parse_line(line: str) -> RawEvent | None:
    m = LINE.match(line.rstrip("\r\n"))
    if not m:
        return None
    ts, node, a, b, val = m.groups()
    value = _signed(val)

    if a.startswith("EV_"):
        kind, code = a, b
    else:
        try:
            tnum, cnum = int(a, 16), int(b, 16)
        except ValueError:
            return None
        kind = EV_TYPES.get(tnum, f"EV_0X{tnum:02X}")
        if tnum == 0x01:
            code = KEY_CODES.get(cnum)
            if code is None:
                code = BTN_CODES.get(cnum, f"KEY_0X{cnum:04X}")
        elif tnum == 0x03:
            code = ABS_CODES.get(cnum, f"ABS_0X{cnum:04X}")
        elif tnum == 0x00:
            code = SYN_CODES.get(cnum, "SYN_REPORT")
        else:
            code = f"0X{cnum:04X}"
    return RawEvent(float(ts), node, kind, code, value)


def iter_text(text: str | Iterable[str]) -> Iterator[RawEvent]:
    """从静态文本/行序列解析，供自检和离线转换使用。"""
    lines = text.splitlines() if isinstance(text, str) else text
    for line in lines:
        ev = parse_line(line)
        if ev:
            yield ev


def _open_getevent(adb, node: str | None, hex_mode: bool) -> subprocess.Popen:
    """起一个 getevent 进程（每个进程只带一个 device，见 iter_live 说明）。"""
    args = ["getevent", "-tt"] + ([] if hex_mode else ["-l"])
    if node:
        args.append(node)
    cmd = adb.prefix + ["shell", *args]
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )


def _iter_one(adb, node: str | None, *, hex_mode: bool,
              controller: EventStreamController | None) -> Iterator[RawEvent]:
    """单进程监听：设备不支持/权限不足时立刻报 GeteventModeError。"""
    proc = _open_getevent(adb, node, hex_mode)
    if controller is not None:
        controller.attach(proc)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if controller is not None and controller.stopped:
                break
            ev = parse_line(line)
            if ev:
                yield ev
            elif any(h in line.lower() for h in ERROR_HINTS):
                args = ["getevent", "-tt"] + ([] if hex_mode else ["-l"])
                if node:
                    args.append(node)
                raise GeteventModeError(
                    f"getevent 启动异常: {line.strip()}\n"
                    f"命令: {' '.join(adb.prefix + ['shell', *args])}\n"
                    f"可试 --event-mode hex（老设备不支持 -l），或检查 root / SELinux 权限。"
                )
    except (OSError, ValueError):
        # 主动 stop() 终止进程时管道可能抛 I/O 错误，属于正常收尾
        if controller is None or not controller.stopped:
            raise
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def iter_live(adb, nodes: list[str] | None = None, *, hex_mode: bool = False,
              controller: EventStreamController | None = None) -> Iterator[RawEvent]:
    """启动 adb shell getevent -tt [-l]，实时产出事件；生成器关闭时自动终止子进程。

    注意：AOSP 的 getevent 只接受【一个】device 参数，传多个会直接打印
    usage 退出（CET-AL00 实测）。所以多节点时必须每个节点各起一个进程，
    再用队列合并输出；部分节点不可用（权限/不存在）时跳过，全失败才报错。
    """
    nodes = [str(n) for n in (nodes or []) if n]
    if len(nodes) <= 1:
        yield from _iter_one(adb, nodes[0] if nodes else None,
                             hex_mode=hex_mode, controller=controller)
        return

    import queue

    q: queue.Queue = queue.Queue()
    procs: list[subprocess.Popen] = []
    lock = threading.Lock()
    stop_evt = threading.Event()

    def pump(node: str) -> None:
        try:
            proc = _open_getevent(adb, node, hex_mode)
            with lock:
                procs.append(proc)
            if controller is not None:
                controller.attach(proc)
            assert proc.stdout is not None
            for line in proc.stdout:
                if stop_evt.is_set() or (controller is not None and controller.stopped):
                    break
                if any(h in line.lower() for h in ERROR_HINTS):
                    q.put(("fail", f"{node}: {line.strip()}"))
                    break
                q.put(("line", line))
        except Exception as exc:   # noqa: BLE001
            q.put(("fail", f"{node}: {exc}"))
        finally:
            q.put(("done", node))

    threads = [threading.Thread(target=pump, args=(n,), daemon=True,
                                name="getevent" + n.rsplit("/", 1)[-1]) for n in nodes]
    for t in threads:
        t.start()
    try:
        remaining = len(threads)
        fails: list[str] = []
        while remaining > 0:
            kind, payload = q.get()
            if kind == "done":
                remaining -= 1
            elif kind == "fail":
                fails.append(payload)
            else:
                ev = parse_line(payload)
                if ev:
                    yield ev
        if fails and len(fails) >= len(nodes):
            raise GeteventModeError(
                "getevent 启动异常（所有节点都不可用）:\n  " + "\n  ".join(fails)
                + "\n可试 --event-mode hex，或检查 root / SELinux 权限。"
            )
    finally:
        stop_evt.set()
        with lock:
            for p in procs:
                if p.poll() is None:
                    p.terminate()
        for p in procs:
            try:
                p.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    p.kill()
                except OSError:
                    pass
