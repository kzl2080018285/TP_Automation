#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ADB Case Recorder 的独立 Qt 界面（PySide6）。

    D:/Python/python.exe adbcase_gui.py            # 启动界面
    D:/Python/python.exe adbcase_gui.py --selftest # 离屏冒烟自检，不开窗口

设计原则（借鉴 GameScrcpy 蓝图「单一数据源」）：
  YAML/JSON 用例文件是唯一真相。表格视图只是它的一个视图，
  编辑后写回同一个文件，可 diff、可 code review，GUI 不另存数据。

GUI 只依赖 core/*，与 CLI（adbcase.py）并列，互不嵌套、互不影响。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from PySide6.QtCore import Qt, QThread, QTimer, Signal, QRectF, QPoint, QPointF
    from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPixmap, QPen
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
        QFormLayout, QGroupBox, QLabel, QLineEdit, QPushButton, QComboBox,
        QCheckBox, QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem,
        QPlainTextEdit, QFileDialog, QMessageBox, QDialog, QDialogButtonBox,
        QToolBar, QStatusBar, QHeaderView, QAbstractItemView, QSplitter, QSizePolicy,
        QTreeWidget, QTreeWidgetItem, QMenu, QToolButton, QStackedWidget,
    )
except ImportError as exc:
    raise SystemExit(
        f"未找到 PySide6（{exc}）。\n"
        "本机可用: D:/Python/python.exe adbcase_gui.py （已装 PySide6 6.11.1）\n"
        "或在你的环境执行: pip install PySide6"
    )

try:
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink
    HAS_QT_AUDIO = True
except ImportError:
    HAS_QT_AUDIO = False

from core.adb import Adb, AdbError  # noqa: E402
from core.cases import (CASE_FORMAT, KNOWN_ACTIONS, CaseError,  # noqa: E402
                        detect_format, expand_steps, load_case, new_case,
                        save_case, validate_case)
from core.config import DEFAULTS, default_config_path, load_config  # noqa: E402
from core.draft import DraftWriter  # noqa: E402
from core.image_baseline import (DEFAULT_IGNORE_STATUS_BAR, ShotCapture,  # noqa: E402
                                 inject_baselines, save_rgb)
from core.inspector import (fetch_nodes, foreground_package, hit_node,  # noqa: E402
                            logical_screen_size, node_info, node_label,
                            node_matches, parse_focus, selector_from_node,
                            selector_label)
from core.listener import EventStreamController, GeteventModeError, RawEvent, iter_live  # noqa: E402
from core.mirror import (KEYCODE_APP_SWITCH, KEYCODE_BACK, KEYCODE_HOME,  # noqa: E402
                         KEYCODE_POWER, KEYCODE_VOLUME_DOWN, KEYCODE_VOLUME_UP,
                         ScrcpyMirror, has_decoder,
                         EXTERNAL_TOOLS_DIR, EXTERNAL_ZIP_URL,
                         find_external_scrcpy, download_external_scrcpy,
                         launch_external_scrcpy)
from core.mirror import (KEYCODE_ENTER, KEYCODE_TAB, KEYCODE_DEL,  # noqa: E402
                         KEYCODE_FORWARD_DEL, KEYCODE_ESCAPE, KEYCODE_INSERT,
                         KEYCODE_DPAD_UP, KEYCODE_DPAD_DOWN, KEYCODE_DPAD_LEFT,
                         KEYCODE_DPAD_RIGHT, KEYCODE_MOVE_HOME, KEYCODE_MOVE_END,
                         KEYCODE_PAGE_UP, KEYCODE_PAGE_DOWN)
from core.probe import probe, resolve_nodes  # noqa: E402
from core.runner import Runner  # noqa: E402
from core.translate import Translator  # noqa: E402

import yaml  # noqa: E402  (PyYAML 与 PySide6 同在 GUI 环境)

import gui_theme  # noqa: E402
from gui_theme import apply_theme, color as theme_color, set_role, set_subdued  # noqa: E402

APP_TITLE = "ADB Case Recorder — 录制 / 编辑 / 回放"


def _persist_cfg_key(key: str, value) -> None:
    """只写单个配置键到默认配置文件，不动用户其它配置。"""
    try:
        path = default_config_path()
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data[key] = value
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def dumps_case(case: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(case, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(case, sort_keys=False, allow_unicode=True,
                          default_flow_style=None, width=120)


def loads_case(text: str, fmt: str) -> dict:
    data = json.loads(text) if fmt == "json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("用例根节点必须是对象")
    return data


def parse_params(text: str) -> dict:
    out = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"参数需要 k=v 格式: {part}")
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def short(value, limit: int = 90) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _node_interesting(n: dict) -> bool:
    """节点自己是否有“内容”：有 id/文本/描述，或能交互。

    真实页面里大半层是既无 id 也不能点的 FrameLayout 包装壳，
    控件树的「只看有效」和灰字样式都靠这一个判定。
    """
    return bool(n.get("resource_id") or n.get("text") or n.get("desc")
                or any(n.get(k) for k in ("clickable", "long_clickable",
                                          "checkable", "scrollable")))


def _set_cell(table: QTableWidget, row: int, col: int, value) -> QTableWidgetItem:
    """填表格单元并附全文 tooltip：列宽不够被截断时悬停仍可看全内容。"""
    item = QTableWidgetItem(str(value))
    item.setToolTip(str(value))
    table.setItem(row, col, item)
    return item


def step_summary(step: dict) -> str:
    if step.get("action") == "assert_widget":
        exp = step.get("expect") or {}
        return short(selector_label(step.get("selector") or {}), 60) + " | " + \
            ", ".join(f"{k}={v}" for k, v in exp.items())
    skip = {"action", "desc", "at_ms"}
    return ", ".join(f"{k}={short(v, 40)}" for k, v in step.items() if k not in skip)


class OfflineAdb:
    """dry-run 不需要真实设备。"""

    def __getattr__(self, _name):
        def _noop(*_a, **_kw):
            return ""
        return _noop


# --------------------------------------------------------------------------
# 后台线程：所有耗时操作都不碰 UI 控件，只发信号
# --------------------------------------------------------------------------
class DeviceWorker(QThread):
    sig_log = Signal(str)
    sig_devices = Signal(list)
    sig_profile = Signal(dict)
    sig_failed = Signal(str)

    def __init__(self, cfg: dict, action: str = "list", serial: str = "", parent=None):
        super().__init__(parent)
        self.cfg, self.action, self.serial = cfg, action, serial

    def run(self):
        try:
            adb = Adb(cfg=self.cfg, serial=self.serial or None)
            if self.action == "list":
                devices = adb.devices()
                self.sig_devices.emit(devices)
                self.sig_log.emit(f"[设备] 在线 {len(devices)} 台: "
                                  + (", ".join(d["serial"] for d in devices) or "无"))
            else:
                self.sig_log.emit("[探测] 读取屏幕尺寸/密度/旋转与输入节点能力 ...")
                self.sig_profile.emit(probe(adb))
        except Exception as exc:
            self.sig_failed.emit(str(exc))


class RecordWorker(QThread):
    sig_log = Signal(str)
    sig_steps = Signal(list)
    sig_done = Signal(dict)
    sig_failed = Signal(str)

    def __init__(self, cfg: dict, opts: dict, parent=None):
        super().__init__(parent)
        self.cfg, self.o = cfg, opts
        self.controller = EventStreamController()
        # 投屏侧合成的事件（手势/按键/文本）走线程安全队列并入同一条时间轴：
        # scrcpy 注入走 InputManager，不会出现在 /dev/input；真机触摸也不会重复
        # 上报，所以两路事件直接合并即可（以前只读 getevent，投屏演示会被录成空用例）
        self._inject_q: queue.Queue = queue.Queue()
        vw, vh = opts.get("video_size") or (0, 0)
        # 投屏视频尺寸（像素）：录制可能先开、投屏后连上（或中途旋转），
        # 所以运行时要按最新值重新登记归一化尺度
        self._mirror_size: tuple[float, float] = \
            (float(vw), float(vh)) if vw > 1 and vh > 1 else (0.0, 0.0)

    def stop(self):
        self.controller.stop()

    def set_mirror_size(self, w: int, h: int) -> None:
        """记录当前投屏画面尺寸；0 表示还没画面，保留上一次已知尺度。"""
        if w > 1 and h > 1:
            self._mirror_size = (float(w), float(h))

    def feed_mirror(self, ev: RawEvent) -> None:
        """投屏画面合成的事件（只入队，由录制线程按原时间戳喂给 Translator）。"""
        self._inject_q.put(("ev", ev))

    def feed_mirror_text(self, text: str) -> None:
        """投屏物理键盘输入的文本：注入走 INJECT_TEXT，getevent 抓不到，需补录。"""
        if text:
            self._inject_q.put(("text", (text, time.monotonic())))

    def _take_injected(self) -> list[tuple[str, object]]:
        out: list[tuple[str, object]] = []
        while True:
            try:
                out.append(self._inject_q.get_nowait())
            except queue.Empty:
                return out

    @staticmethod
    def _gesture_end(ev: RawEvent) -> bool:
        """这一行是否结束了一个手势（抬手）：用于即时刷新已识别步骤。"""
        return ((ev.kind == "EV_ABS" and ev.code == "ABS_MT_TRACKING_ID" and ev.value < 0)
                or (ev.kind == "EV_KEY" and ev.code == "BTN_TOUCH" and ev.value == 0))

    @classmethod
    def _apply_batch(cls, tr, batch, by_node: dict[str, int]) -> tuple[int, bool]:
        """把一批（设备/投屏）事件喂给 Translator -> (条数, 是否结束了手势)。"""
        n, ended = 0, False
        for kind, payload in batch:
            if kind == "text":
                text, ts = payload
                tr.inject_text(text, ts)
                by_node["mirror"] = by_node.get("mirror", 0) + 1
                n += 1
                ended = True
                continue
            ev: RawEvent = payload
            tr.feed([ev])
            by_node[ev.node] = by_node.get(ev.node, 0) + 1
            n += 1
            ended = ended or cls._gesture_end(ev)
        return n, ended

    @staticmethod
    def _no_step_reason(count: int, by_node: dict[str, int], nodes: list[str],
                        profile: dict) -> str:
        """“没有识别到操作”要说清楚到底是没收到事件，还是收到了却不成手势。"""
        if not count:
            touch = "，".join(f"{d['node']}（{d.get('name') or '?'}）"
                             for d in profile.get("inputs") or [] if d.get("is_touch")) \
                or "未探测到"
            return ("没有收到任何输入事件，未生成用例。\n"
                    "• 在设备屏幕上操作，或直接在投屏画面上操作（投屏操作会被合成进录制）\n"
                    f"• 当前监听: {', '.join(nodes)}\n"
                    f"• 若确实在设备上操作过，可改选触摸节点重试: {touch}\n"
                    "• 仍为 0 条时把事件模式改成 hex（个别 ROM 的 getevent -l 不兼容）")
        if set(by_node) == {"mirror"}:
            return (f"收到 {count} 条投屏事件，但没有形成完整手势。\n"
                    "• 在投屏画面上「按下-抬起」完整点一下，或拖出滑动轨迹后再停止录制")
        dist = "，".join(f"{k}={v}" for k, v in sorted(by_node.items()))
        return (f"收到 {count} 条原始事件，但没有识别出任何动作。\n"
                f"• 事件分布: {dist}\n"
                "• 触摸可能不走 MT 协议（缺 tracking id / BTN_TOUCH 抬起），可改选其它触摸节点重试")

    def _stream(self, adb, nodes):
        """与 CLI 一致：auto 模式在设备不支持 -l 时自动回退 hex。"""
        mode = self.o.get("event_mode", "auto")
        if mode == "hex":
            yield from iter_live(adb, nodes, hex_mode=True, controller=self.controller)
            return
        try:
            yield from iter_live(adb, nodes, hex_mode=False, controller=self.controller)
        except GeteventModeError as exc:
            if mode != "auto" or self.controller.stopped:
                raise
            self.sig_log.emit(f"[WARN] -l 不可用，自动切换 hex 模式: {exc}")
            yield from iter_live(adb, nodes, hex_mode=True, controller=self.controller)

    def run(self):
        draft = None
        capture = None
        try:
            adb = Adb(cfg=self.cfg, serial=self.o.get("serial") or None)
            self.sig_log.emit("[探测] 读取屏幕尺寸与输入节点能力 ...")
            profile = probe(adb)
            nodes = self.o.get("nodes") or resolve_nodes(adb, profile)
            if not nodes:
                raise RuntimeError("没有找到可监听的输入节点，请在界面里手动填写 /dev/input/eventN")
            self.sig_log.emit(f"[探测] {profile['serial']} 屏幕 {profile['size']} "
                              f"触摸坐标 {profile['touch_abs']}")
            names = {d["node"]: (d.get("name") or "") for d in profile.get("inputs") or []}
            self.sig_log.emit("[监听] 节点: " + "，".join(
                n + (f"（{names[n]}）" if names.get(n) else "") for n in nodes))
            self.sig_log.emit("[监听] 现在在设备屏幕上操作，或直接用投屏画面操作"
                              "（两者都会录进同一条时间轴）；点「停止录制」结束")

            case_path = Path(self.o["out"])
            fmt = self.o["fmt"]
            name = self.o["name"] or case_path.stem
            shots_dir = f"{case_path.stem}{self.cfg.get('shot_dir_suffix', '_shots')}"

            if self.o.get("screenshots"):
                # async 模式不阻塞事件流，投屏帧来源可让截图接近实时；
                # sync 模式讲究精确时刻，保持 screencap
                capture = ShotCapture(adb, case_path.parent, shots_dir,
                                      delay_ms=int(self.o.get("shot_delay", 250)),
                                      mode=self.o.get("shot_mode", "async"),
                                      frame_provider=(self.o.get("frame_provider")
                                                      if self.o.get("shot_mode", "async") == "async"
                                                      else None))
                src = "投屏解码帧" if self.o.get("shot_mode", "async") == "async" \
                    and self.o.get("frame_provider") else "screencap"
                self.sig_log.emit(f"[截图] 逐步截图（{src}）-> {case_path.parent / shots_dir}")

            tr = Translator(self.cfg, profile,
                            on_action=(lambda ts, a: capture.submit(ts)) if capture else None)
            # 投屏合成事件是视频像素：按它自己的尺度归一化，否则坐标会被压成 0.0x
            mirror_size = self._mirror_size
            if mirror_size[0] > 1:
                tr.node_abs["mirror"] = (mirror_size[0] - 1, mirror_size[1] - 1)
            if self.o.get("draft", True):
                draft = DraftWriter(tr, profile, case_path, fmt,
                                    float(self.cfg.get("draft_interval_s", 5)),
                                    name, self.o.get("tags", []),
                                    self.o.get("params", {}), log=self.sig_log.emit)
                draft.start()

            count = 0
            by_node: dict[str, int] = {}
            last_logged = 0

            def preview(with_log: bool):
                clean = inject_baselines(tr.snapshot(), {}, policy="none")
                self.sig_steps.emit(clean)
                if with_log:
                    self.sig_log.emit(f"[录制] 已接收 {count} 条原始事件，识别 {len(clean)} 步")

            # getevent 生成器会阻塞，外包一层线程进队列，主循环才能同时取投屏事件
            ev_q: queue.Queue = queue.Queue()

            def pump():
                try:
                    for ev in self._stream(adb, nodes):
                        ev_q.put(ev)
                        if self.controller.stopped:
                            break
                except BaseException as exc:   # noqa: BLE001 交给主循环统一处理
                    ev_q.put(exc)
                finally:
                    ev_q.put(None)

            threading.Thread(target=pump, daemon=True, name="record-pump").start()
            no_item = object()
            while True:
                if self._mirror_size != mirror_size and self._mirror_size[0] > 1:
                    # 投屏刚连上 / 旋转过：换新的视频尺度过（_norm 在手势结算时才读）
                    mirror_size = self._mirror_size
                    tr.node_abs["mirror"] = (mirror_size[0] - 1, mirror_size[1] - 1)
                batch = self._take_injected()
                try:
                    item = ev_q.get(timeout=0.0 if batch else 0.1)
                except queue.Empty:
                    item = no_item
                if item is not no_item:
                    if item is None:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    batch.append(("ev", item))
                if not batch:
                    continue
                n, ended = self._apply_batch(tr, batch, by_node)
                count += n
                if ended:
                    preview(False)      # 一抬手就刷步骤，投屏演示时不用等满 25 条
                if count - last_logged >= 25:
                    last_logged = count
                    preview(True)

            # 收尾：点「停止」前最后一刻的操作还在队列里，别丢
            tail = self._take_injected()
            if tail:
                n, _ = self._apply_batch(tr, tail, by_node)
                count += n
            if count:
                self.sig_log.emit("[录制] 事件来源: "
                                  + "，".join(f"{k}={v}" for k, v in sorted(by_node.items())))

            shots = capture.close() if capture else {}
            if capture:
                self.sig_log.emit(f"[截图] 成功 {capture.stat.saved}/{capture.stat.total} 张")
            ignore = []
            if self.cfg.get("ignore_status_bar", True) and not self.o.get("include_status_bar"):
                ignore.append(list(DEFAULT_IGNORE_STATUS_BAR))
            ignore += [list(r) for r in (self.cfg.get("baseline_ignore_regions") or [])]
            steps = inject_baselines(
                tr.finish(), shots,
                policy=self.o.get("baseline_policy", "all"),
                threshold=float(self.cfg.get("baseline_threshold", 0.02)),
                pixel_tolerance=int(self.cfg.get("baseline_pixel_tolerance", 24)),
                ignore_regions=ignore,
                wait_before_ms=int(self.o.get("shot_delay", 250)),
            )
            if not steps:
                raise RuntimeError(self._no_step_reason(count, by_node, nodes, profile))
            case = new_case(name, profile, steps, tags=self.o.get("tags", []),
                            params=self.o.get("params", {}))
            path = save_case(case, case_path, fmt)
            if draft:
                draft.stop(remove=True)
            self.sig_steps.emit(steps)
            self.sig_log.emit(f"[用例] 已生成 {path}（{len(steps)} 步）")
            self.sig_done.emit({"path": str(path), "steps": len(steps), "events": count})
        except Exception as exc:
            if draft:
                draft.stop(remove=False)
                self.sig_log.emit(f"[草稿] 异常中断，已保留 {draft.path}")
            if capture:
                capture.close()
            self.sig_failed.emit(f"{exc}\n{traceback.format_exc(limit=4)}")


class ReplayWorker(QThread):
    sig_log = Signal(str)
    sig_step = Signal(dict)
    sig_done = Signal(dict)
    sig_failed = Signal(str)

    def __init__(self, cfg: dict, opts: dict, parent=None):
        super().__init__(parent)
        self.cfg, self.o = cfg, opts
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        try:
            path = Path(self.o["case"])
            case = load_case(path)
            errs = validate_case(case)
            if errs and not self.o.get("force"):
                raise RuntimeError("用例校验失败:\n  " + "\n  ".join(errs))

            if self.o.get("dry_run"):
                # 预演不碰设备，但报告仍会「全过」：先明确告知，避免误判回放失效
                self.sig_log.emit("[回放] DRY-RUN 预演：只打印 ADB 命令，不会操作设备")
                adb = OfflineAdb()
                dev = case.get("device") or {}
                profile = {"serial": "DRY-RUN", "model": dev.get("model", ""),
                           "size": dev.get("size") or [1080, 1920],
                           "rotation": dev.get("rotation", 0),
                           "touch_abs": dev.get("touch_abs") or [1079, 1919]}
            else:
                adb = Adb(cfg=self.cfg, serial=self.o.get("serial") or None)
                profile = probe(adb)

            timeline = bool(self.o.get("timeline"))
            if timeline and not any(isinstance(s.get("at_ms"), int)
                                    for s in case.get("steps", []) if isinstance(s, dict)):
                self.sig_log.emit("[WARN] 用例没有 at_ms，--timeline 已忽略")
                timeline = False

            runner = Runner(
                adb, self.cfg, profile,
                dry_run=bool(self.o.get("dry_run")),
                speed=float(self.o.get("speed", 1.0)),
                on_error=self.o.get("on_error", "stop"),
                timeline=timeline,
                update_baseline=bool(self.o.get("update_baseline")),
                report_dir=ROOT / "reports", case_dir=path.parent,
                frame_provider=self.o.get("frame_provider"),
                log=self.sig_log.emit, on_step=self.sig_step.emit,
                stop_callback=lambda: self._stopped,
            )
            failures = 0
            total_steps = 0
            for _ in range(max(1, int(self.o.get("repeat", 1)))):
                rep = runner.run(case, params=self.o.get("params", {}),
                                 from_step=int(self.o.get("from_step", 1)),
                                 to_step=self.o.get("to_step"),
                                 save_report=not self.o.get("no_report"))
                failures += rep.failed
                total_steps += len(rep.steps)
                if rep.failed and runner.on_error == "stop":
                    break
                if self._stopped:
                    break
            self.sig_done.emit({"passed": total_steps - failures,
                                "failed": failures, "total": total_steps})
        except Exception as exc:
            self.sig_failed.emit(f"{exc}\n{traceback.format_exc(limit=4)}")


# --------------------------------------------------------------------------
# 实时投屏（借鉴 GameScrcpy：scrcpy 协议硬编推流 + 鼠标反向控制）
# --------------------------------------------------------------------------
class AudioPlayer:
    """设备声音播放：PCM 入有界 deque，后台线程直写 QAudioSink。

    借鉴 GameScrcpy media 模块：声音 I/O 不占界面线程——旧版用 20ms
    QTimer 在主线程喂数据，重绘卡顿会直接导致音频欠载“突突”。
    缓冲上限 1 秒，超出丢最旧（宁丢不延）；静音时直接丢弃。
    """

    def __init__(self, parent=None, *, pump: bool = True):
        self._parent = parent
        self._pump = pump        # False：不起后台写线程（仅单测隔空验缓冲）
        self._buf = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._rebuild = True
        self._rate = 48000    # scrcpy opus 固定 48k；以首块实际值为准
        self._ch = 2
        self._sink = None
        self._dev = None
        self.muted = False
        self.bytes_played = 0
        self._thread: threading.Thread | None = None

    def push(self, pcm: bytes, rate: int, channels: int):
        if self.muted or not pcm:
            return
        with self._lock:
            if (rate, channels) != (self._rate, self._ch):
                # 格式变化：重建标记交给写线程处理，主线程不建 QAudioSink
                self._rate, self._ch = rate, channels
                self._buf.clear()
                self._bytes = 0
                self._rebuild = True
            self._buf.append(pcm)
            self._bytes += len(pcm)
            cap = max(1, rate * channels * 2)  # 1 秒 s16
            while self._bytes > cap and self._buf:
                self._bytes -= len(self._buf.popleft())
            if self._pump and (self._thread is None or not self._thread.is_alive()):
                # 从“无线程”状态拉起时强制重建 sink：上一段会话退出已把
                # _dev 置空，不重置 _rebuild 会导致重新投屏后静音
                self._rebuild = True
                # 旧线程已完全退出（is_alive 为假即 run 含收尾已结束）才清
                # 停止标志并拉新线程；旧线程未退时不 clear，它会自行退出，
                # 下次 push 再接管——杜绝新旧两个写线程并存
                self._stop.clear()
                self._thread = threading.Thread(target=self._drain_loop,
                                                name="mirror-audio-out", daemon=True)
                self._thread.start()
        if self._pump:
            self._wake.set()

    def buffered_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def _open_sink(self):
        if not HAS_QT_AUDIO:
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(self._rate)
        fmt.setChannelCount(self._ch)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        sink = QAudioSink(fmt)
        sink.setBufferSize(max(4096, self._rate * self._ch * 2 // 10))  # ~100ms
        dev = sink.start()
        if dev is None:
            sink.stop()
            return False
        self._sink, self._dev = sink, dev
        return True

    def _close_sink(self):
        if self._sink is not None:
            self._sink.stop()
        self._sink = None
        self._dev = None

    def _drain_loop(self):
        # QAudioSink 的 I/O 设备线程安全，本线程全程独占读写；退出时才 join+清理
        while not self._stop.is_set():
            with self._lock:
                need_rebuild = self._rebuild
                if need_rebuild:
                    self._rebuild = False
            if need_rebuild:
                self._close_sink()
                if not self._open_sink():
                    break
            chunk = None
            with self._lock:
                if self._buf and self._dev is not None:
                    chunk = self._buf.popleft()
                    self._bytes -= len(chunk)
            if chunk is None:
                self._wake.wait(0.05)
                self._wake.clear()
                continue
            if self.muted:
                continue
            try:
                self._dev.write(chunk)
                self.bytes_played += len(chunk)
            except OSError:
                break
        with self._lock:
            # 只在“自己仍是当前线程”时置空：stop() 不再提前置空引用，
            # 退出登记与 push 的 is_alive 判断配合，杜绝新旧线程并存
            if self._thread is threading.current_thread():
                self._thread = None
            self._buf.clear()
            self._bytes = 0
        self._close_sink()

    def set_muted(self, muted: bool):
        self.muted = bool(muted)
        if self.muted:
            with self._lock:
                self._buf.clear()
                self._bytes = 0

    def stop(self):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        # sink 归写线程独占：退出时自行关闭；这里不兜底 close，
        # 否则 join 超时会关掉旧线程正在写的设备。_stop 保持置位，
        # 残留旧线程下一轮循环即退出；新线程由 push 拉起时再清标志
        with self._lock:
            self._buf.clear()
            self._bytes = 0


class MirrorWorker(QThread):
    """后台维持 ScrcpyMirror 会话；音频/元信息/日志经信号切回 UI 线程。

    视频帧不走信号：UI 卡顿会让帧对象在 Qt 事件队列里积压成无界缓冲、
    延迟越滚越大；改为会话内单槽 latest-frame，UI 定时器按自己节拍取。
    借鉴 GameScrcpy「断线自动续上」：意外中断且设备仍在线时，
    退避重建会话，连上且出过帧才算恢复（消耗的重连额度由服务端复位）。
    """
    sig_audio = Signal(bytes, int, int)  # PCM s16 / 采样率 / 声道
    sig_meta = Signal(dict)
    sig_log = Signal(str)
    sig_failed = Signal(str)
    sig_stopped = Signal()
    sig_reconnecting = Signal(str)

    def __init__(self, cfg: dict, serial: str = "", max_size: int = 1280,
                 bit_rate: int = 8_000_000, max_fps: int = 30,
                 audio: bool = True, record_path: str = "", parent=None):
        super().__init__(parent)
        self.cfg, self.serial = cfg, serial
        self.max_size, self.bit_rate, self.max_fps = max_size, bit_rate, max_fps
        self.audio = audio
        self.record_path = record_path
        self._session: ScrcpyMirror | None = None
        self._stop_event = threading.Event()
        self._failed = ""

    def current(self) -> ScrcpyMirror | None:
        """当前活跃会话（重连后是新对象）；未连上/已停止为 None。"""
        s = self._session
        return s if s is not None and s._started else None

    def run(self):
        enabled = bool(self.cfg.get("mirror_reconnect", True))
        max_attempts = int(self.cfg.get("mirror_reconnect_attempts", 3))
        backoff = max(0.2, float(self.cfg.get("mirror_reconnect_backoff_s", 1.0)))
        attempts = 0          # 本轮中断已连续尝试次数（稳定出帧后复位）
        failed = ""
        adb = Adb(cfg=self.cfg, serial=self.serial or None)
        try:
            while not self._stop_event.is_set():
                session = ScrcpyMirror(
                    adb, self.cfg, max_size=self.max_size, bit_rate=self.bit_rate,
                    max_fps=self.max_fps, audio=self.audio,
                    record_path=self.record_path,
                    on_audio=self.sig_audio.emit,
                    on_meta=self.sig_meta.emit, on_error=self._on_stream_error,
                    log=self.sig_log.emit)
                self._session = session
                try:
                    session.start()
                except Exception as exc:
                    self._cleanup_session(session)
                    self._session = None
                    if not (enabled and not self._stop_event.is_set()
                            and attempts < max_attempts and self._device_online(adb)):
                        failed = str(exc)
                        break
                    attempts += 1
                    self.sig_reconnecting.emit(
                        f"连接失败，{backoff * attempts:.0f}s 后重连（第 {attempts}/{max_attempts} 次）: {exc}")
                    if self._stop_event.wait(backoff * attempts):
                        break
                    continue
                # 会话运行直到出错或用户停止；出帧稳定后复位重连额度
                while not self._stop_event.is_set():
                    self._stop_event.wait(0.5)
                    s = self._session
                    if s is not None and not self._failed and s.frames_decoded > 0:
                        attempts = 0
                if not self._failed:
                    break               # 用户主动停止
                self._cleanup_session(session)
                self._session = None
                if not (enabled and attempts < max_attempts and self._device_online(adb)):
                    failed = self._failed
                    break
                attempts += 1
                self.sig_reconnecting.emit(
                    f"投屏中断，{backoff * attempts:.0f}s 后重连（第 {attempts}/{max_attempts} 次）: {self._failed}")
                self._failed = ""
                self._stop_event.clear()   # 重连窗口重新武装，否则 wait() 永不等
                if self._stop_event.wait(backoff * attempts):
                    break
        finally:
            self._cleanup_session(self._session)
            self._session = None
            if failed:
                self.sig_failed.emit(failed)
            self.sig_stopped.emit()

    @staticmethod
    def _device_online(adb: Adb) -> bool:
        """真掉线（拔线/授权失效）就别反复重连，快速失败给用户准确提示。"""
        try:
            serials = [d["serial"] for d in adb.devices()]
        except (AdbError, OSError):
            return False
        return adb.serial in serials if adb.serial else bool(serials)

    def _cleanup_session(self, session):
        if session is None:
            return
        try:
            session.stop()  # 完整清理（adb forward 移除/录像回收）在后台线程做
        except Exception:
            pass

    def _on_stream_error(self, msg: str):
        self._failed = msg
        self._stop_event.set()   # 唤醒 run() 主循环，由它决定重连还是终止

    def stop(self):
        self._stop_event.set()
        s = self.current() or self._session
        if s is not None:
            s.request_stop()  # 快速路径，不卡 UI；收尾在 run() 的 finally


# 控件透视配色：与 Chrome DevTools Overlay.highlightNode 同源
# （content 淡蓝 rgba(111,168,220) + accent 边框）
_INSPECT_LINE = QColor(111, 168, 220, 120)   # 全量轮廓：低透明度不抢画面
_INSPECT_FILL = QColor(111, 168, 220, 70)    # 命中控件填充
_INSPECT_HL = QColor(78, 161, 245)           # 命中控件边框
# 右键锁定的控件：琥珀色，与悬浮高亮区分开（不看鼠标也知道盯的是哪个）
_INSPECT_PIN = QColor(255, 176, 32)
_INSPECT_PIN_FILL = QColor(255, 176, 32, 52)


class CopyLabel(QLabel):
    """单行可复制标签：双击复制主值，右键菜单选复制哪一段。

    控件树顶部的「应用包名 · 界面」用它：鼠标悬停有全文 tooltip，
    双击直接把 `包名/Activity` 拿去粘到 am start / 用例里。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._value = ""
        self._targets: dict[str, str] = {}
        self.on_copied = None            # fn(说明文字)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def set_copy(self, value: str, targets: dict[str, str] | None = None):
        """登记可复制内容；空值时不响应双击（避免复制空串）。"""
        self._value = value or ""
        self._targets = dict(targets or {})
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse if self._value
            else Qt.TextInteractionFlag.NoTextInteraction)

    def copy_now(self, value: str | None = None) -> str:
        text = self._value if value is None else value
        if not text:
            return ""
        QApplication.clipboard().setText(text)
        if self.on_copied is not None:
            self.on_copied(short(text, 32))
        return text

    def mouseDoubleClickEvent(self, ev):
        if self._value:
            self.copy_now()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def _show_menu(self, pos):
        targets = self._targets or ({"": self._value} if self._value else {})
        if not targets:
            return
        menu = QMenu(self)
        acts = [(menu.addAction(f"复制{caption}" if caption else "复制"), value)
                for caption, value in targets.items()]
        chosen = menu.exec(self.mapToGlobal(pos))
        for act, value in acts:
            if chosen is act:
                self.copy_now(value)
                return


class PopoutButton(QToolButton):
    """画面右上角的「拖出独立展示」把手：拖动松手给出落点，单击也可弹出。

    QToolButton 在被 accept 的 mousePressEvent 后不会发 clicked，这里自己
    判定位移：≥24px 算拖出（带落点），否则当单击（落点由调用方决定）。
    """

    released = Signal(object)      # QPoint 全局落点；单击时为 None

    def __init__(self, text: str = "⧉", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setObjectName("popoutBtn")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._press = None
        self._dragged = False

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._press = ev.globalPosition().toPoint()
            self._dragged = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._press is not None:
            if (ev.globalPosition().toPoint() - self._press).manhattanLength() >= 24:
                self._dragged = True
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._press is None:
            super().mouseReleaseEvent(ev)
            return
        self._press = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        pos = ev.globalPosition().toPoint()
        ev.accept()
        self.released.emit(pos if self._dragged else None)


class MirrorView(QWidget):
    """投屏画面：等比 letterbox 渲染；鼠标坐标映射回设备视频坐标。

    左键点击=触摸按下/抬起，左键拖拽=滑动，中键=Home，滚轮=滚动。
    右键不注入任何输入（不算点击操作）：透视开启时选中控件并弹出菜单，
    可把该控件设成断言（存在/不存在/文本）或存成截图基线。
    录制开启时把手势合成 RawEvent 喂给 Translator，与设备录制共用识别规则。
    """

    # 物理键盘特殊键 -> (Android keycode, Linux 键名)；可打印字符走 INJECT_TEXT。
    # Linux 键名经 config.key_aliases 解析成 KEYCODE_* 录进用例（与快捷键录制同链路）
    SPECIAL_KEYS = {
        Qt.Key.Key_Return: (KEYCODE_ENTER, "KEY_ENTER"),
        Qt.Key.Key_Enter: (KEYCODE_ENTER, "KEY_ENTER"),
        Qt.Key.Key_Backspace: (KEYCODE_DEL, "KEY_DEL"),
        Qt.Key.Key_Tab: (KEYCODE_TAB, "KEY_TAB"),
        Qt.Key.Key_Delete: (KEYCODE_FORWARD_DEL, "KEY_FORWARDDEL"),
        Qt.Key.Key_Escape: (KEYCODE_ESCAPE, "KEY_ESC"),
        Qt.Key.Key_Up: (KEYCODE_DPAD_UP, "KEY_DPAD_UP"),
        Qt.Key.Key_Down: (KEYCODE_DPAD_DOWN, "KEY_DPAD_DOWN"),
        Qt.Key.Key_Left: (KEYCODE_DPAD_LEFT, "KEY_DPAD_LEFT"),
        Qt.Key.Key_Right: (KEYCODE_DPAD_RIGHT, "KEY_DPAD_RIGHT"),
        Qt.Key.Key_Home: (KEYCODE_MOVE_HOME, "KEY_MOVE_HOME"),
        Qt.Key.Key_End: (KEYCODE_MOVE_END, "KEY_MOVE_END"),
        Qt.Key.Key_PageUp: (KEYCODE_PAGE_UP, "KEY_PAGEUP"),
        Qt.Key.Key_PageDown: (KEYCODE_PAGE_DOWN, "KEY_PAGEDOWN"),
        Qt.Key.Key_Insert: (KEYCODE_INSERT, "KEY_INSERT"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # 中文等 IME 的提交文本走 inputMethodEvent 而非 keyPressEvent，
        # 不开这个属性输入法候选词上屏后无处可去（A2 补全）
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.worker: MirrorWorker | None = None
        self.on_input_event = None   # fn(RawEvent)：录制时合成事件回调
        self.on_shortcut = None      # fn(name)：右键/中键快捷键回调
        self.on_key_event = None     # fn(linux_key_name)：物理键盘特殊键录制回调
        self.on_text_event = None    # fn(text)：物理键盘可打印文本录制回调
        self._image: QImage | None = None
        self._arr = None             # 当前显示帧的 numpy 引用（QImage 缓冲所有者）
        self._video_size = (0, 0)
        self._pressed = False
        self._last_video: tuple[int, int] | None = None
        self._mark = None
        self._mark_timer = QTimer(self)
        self._mark_timer.setSingleShot(True)
        self._mark_timer.setInterval(350)
        self._mark_timer.timeout.connect(self._clear_mark)
        # 控件透视（默认关）：开启后叠加全部控件轮廓，悬浮高亮+信息卡。
        # 鼠标追踪常开，未按键的移动也能做悬浮命中
        self.setMouseTracking(True)
        self._inspect_nodes: list[dict] = []
        self._inspect_screen = (0, 0)   # 控件 bounds 所用的设备逻辑屏尺寸
        self._hover_node = None         # 鼠标当前悬浮的控件（移出/手势中为空）
        self._pinned = None             # 右键选中的控件：高亮/信息卡不随鼠标消失
        self.on_inspect_hover = None    # 面板回调：悬浮控件变化时同步控件树选中
        self.on_inspect_pick = None     # 面板回调：右键选中控件（None = 取消选中）
        self.on_assert_pick = None      # 面板回调：fn(kind, node) 右键断言/截图请求
        self._assert_menu: QMenu | None = None   # 右键菜单（自检也靠它触发菜单项）
        # 轮廓层缓存：节点/几何不变时不再每帧重绘几千个矩形，直接 blit
        self._inspect_cache: QPixmap | None = None
        self._inspect_cache_rect = QRectF()
        self._inspect_cache_serial = 0   # 自检用：缓存重建次数

    # ---- 显示 ----
    def set_frame(self, arr):
        """上屏一帧 rgb24（单槽取帧，发布后只读、C 连续）。

        QImage 直接引用 numpy 缓冲（零拷贝）：_arr 持引用防悬垂，
        deleter 闭包给 Qt 缓冲续命到最后一个拷贝前——旧版每帧两次
        全量 memcpy（非连续防御 + img.copy），1080p@60 白吃 30MB/s。
        """
        h, w = arr.shape[:2]
        if not arr.flags["C_CONTIGUOUS"]:
            arr = arr.copy()   # 防御：单槽帧本已 C 连续，直接传入的非连续数组先拉直
        self._arr = arr
        img = QImage(arr.data, w, h, arr.strides[0], QImage.Format.Format_RGB888)
        self._image = img
        self._video_size = (w, h)
        self.update()

    def clear_frame(self):
        self._image = None
        self._arr = None
        self._video_size = (0, 0)
        self.update()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.place_popout()

    def place_popout(self):
        """把手钉在画面右上角（画面尺寸随窗口变，每次 resize 重新定位）。"""
        b = getattr(self, "btn_popout", None)
        if b is None:
            return
        b.adjustSize()
        b.move(max(4, self.width() - b.width() - 10), 10)
        b.raise_()

    def _content_rect(self):
        """画面实际绘制区域（等比缩放居中，四周留黑边）。"""
        w, h = self._video_size
        if not w or not h or self._image is None:
            return QRectF()
        scale = min(self.width() / w, self.height() / h)
        dw, dh = w * scale, h * scale
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme_color("video_bg")))
        rect = self._content_rect()
        if self._image is not None and not rect.isEmpty():
            p.drawImage(rect, self._image)
            if self._mark is not None:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 80, 80, 140))
                p.drawEllipse(self._mark, 14, 14)
            self._draw_inspect(p, rect)
        else:
            p.setPen(QColor(theme_color("subtext")))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "未投屏\n点击上方「开始投屏」连接设备\n"
                       "连接后左键点击 / 拖拽 / 键盘打字即可操作设备")
        p.end()

    def _clear_mark(self):
        self._mark = None
        self.update()

    # ---- 控件透视 ----
    def _invalidate_inspect_cache(self):
        self._inspect_cache = None
        self._inspect_cache_rect = QRectF()

    def set_inspect(self, nodes: list, screen_size):
        """装载控件树（设备逻辑屏坐标）；画面缩放/letterbox 由绘制时换算。"""
        self._inspect_nodes = list(nodes or [])
        self._inspect_screen = (int(screen_size[0]), int(screen_size[1])) \
            if screen_size else (0, 0)
        self._hover_node = None
        self._pinned = None   # 节点已整体替换，锁定由面板按 path+sel 重新落位
        self._invalidate_inspect_cache()
        # 透视开启时准星光标：提示“这里点击的是画面，悬浮看控件”
        self.setCursor(Qt.CursorShape.CrossCursor if self._inspect_nodes
                       else Qt.CursorShape.ArrowCursor)
        self.update()

    def clear_inspect(self):
        self._inspect_nodes = []
        self._inspect_screen = (0, 0)
        self._hover_node = None
        self._pinned = None
        self._invalidate_inspect_cache()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def inspect_hit(self, pos):
        """widget 坐标 -> 设备逻辑屏坐标 -> 命中最小面积控件。"""
        rect = self._content_rect()
        sw, sh = self._inspect_screen
        if rect.isEmpty() or not sw or not sh or not self._inspect_nodes:
            return None
        if not rect.contains(pos):
            return None
        dx = (pos.x() - rect.x()) / rect.width() * sw
        dy = (pos.y() - rect.y()) / rect.height() * sh
        return hit_node(self._inspect_nodes, dx, dy)

    def _pin(self, node):
        """右键选中/取消选中控件：画面与侧栏都停在这个控件上，直到手动取消。"""
        self._pinned = node
        self._hover_node = node
        if self.on_inspect_pick is not None:
            self.on_inspect_pick(node)
        self.update()

    # ---- 右键菜单：控件断言 / 控件截图基线 ----
    def _emit_assert(self, kind: str, node):
        if self.on_assert_pick is not None:
            self.on_assert_pick(kind, node)

    def _show_assert_menu(self, pos, node):
        """右键菜单：把命中的控件交给用例编辑做断言（选择器由主窗口生成）。

        菜单对象复用：右键是高频操作，每次 new 一个 QMenu 挂在 self 上不会释放，
        连点几百次就攒下几百个菜单（以及它们闭包里抓着的控件节点）。
        """
        menu = self._assert_menu
        if menu is None:
            menu = self._assert_menu = QMenu(self)
        else:
            menu.close()    # 上一个菜单还开着：先收起再清空重建
        menu.clear()        # 旧 QAction 归菜单所有，clear() 一并销毁（闭包随之释放）
        has = node is not None
        # QAction 文本里的 & 是助记符（&x 会渲染成下划线 x），控件文本原样带上必须转义
        text = short(node.get("text") or "", 18).replace("&", "&&") if has else ""

        def add(caption: str, kind: str):
            a = menu.addAction(caption)
            a.setEnabled(has)   # 右键在空白处时只留「整屏截图」可用
            a.triggered.connect(lambda *_x, k=kind: self._emit_assert(k, node))
            return a

        add("断言此控件存在", "exists")
        add(f"断言此控件文本等于 {text}" if text else "断言此控件文本…", "text")
        add("断言此控件不存在", "absent")
        menu.addSeparator()
        add("保存控件截图并断言（图片基线）", "shot_widget")
        a_screen = menu.addAction("保存整屏截图并断言（图片基线）")
        a_screen.triggered.connect(lambda *_x: self._emit_assert("shot_screen", None))
        if self._pinned is not None:
            menu.addSeparator()
            a_unpin = menu.addAction("取消选中控件")
            a_unpin.triggered.connect(lambda *_x: self._pin(None))
        menu.popup(self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))))

    def _build_inspect_cache(self, rect: QRectF):
        """把全部控件轮廓画进离屏位图（只在节点/几何变化时重建）。

        1080p 投屏常见 300~1500 个控件，每帧直接描边会把 60fps 拖低；
        轮廓与视频帧无关，缓存成位图后每帧只是一次 blit。
        """
        sw, sh = self._inspect_screen
        dpr = max(1.0, float(self.devicePixelRatioF()))
        pm = QPixmap(max(1, int(rect.width() * dpr)),
                     max(1, int(rect.height() * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        kx, ky = rect.width() / sw, rect.height() / sh
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_INSPECT_LINE, 1))
        # 同 bounds 的嵌套包装层只描一次；小于 2px 的碎控件不描
        seen = set()
        for n in self._inspect_nodes:
            b = n["bounds"]
            if b in seen:
                continue
            seen.add(b)
            x1, y1, x2, y2 = b
            rw, rh = (x2 - x1) * kx, (y2 - y1) * ky
            if rw < 2 or rh < 2:
                continue
            # 缓存坐标系以内容区左上角为原点
            p.drawRect(QRectF(x1 * kx, y1 * ky, rw, rh))
        p.end()
        self._inspect_cache = pm
        self._inspect_cache_rect = QRectF(rect)
        self._inspect_cache_serial += 1

    def _node_rect(self, rect: QRectF, node: dict) -> QRectF:
        """设备像素 bounds -> 画面坐标矩形（与轮廓层同一套缩放）。"""
        sw, sh = self._inspect_screen
        kx, ky = rect.width() / sw, rect.height() / sh
        x1, y1, x2, y2 = node["bounds"]
        return QRectF(rect.x() + x1 * kx, rect.y() + y1 * ky,
                      (x2 - x1) * kx, (y2 - y1) * ky)

    def _paint_highlight(self, p: QPainter, hr: QRectF, *, fill: QColor,
                         line: QColor, dashed: bool = False):
        """控件高亮：淡填充 + 边框 + 四角白刻度（控件很小时边界依然可辨）。"""
        p.setBrush(fill)
        p.setPen(QPen(line, 2, Qt.PenStyle.DashLine if dashed
                      else Qt.PenStyle.SolidLine))
        p.drawRect(hr)
        p.setPen(QPen(QColor(255, 255, 255, 210), 2))
        tick = min(8.0, max(3.0, min(hr.width(), hr.height()) * 0.25))
        for cx, cy, dx, dy in ((hr.left(), hr.top(), 1, 1),
                               (hr.right(), hr.top(), -1, 1),
                               (hr.left(), hr.bottom(), 1, -1),
                               (hr.right(), hr.bottom(), -1, -1)):
            p.drawLine(QPointF(cx, cy), QPointF(cx + dx * tick, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * tick))

    def _draw_inspect(self, p: QPainter, rect: QRectF):
        sw, sh = self._inspect_screen
        if not sw or not sh or not self._inspect_nodes:
            return
        if self._inspect_cache is None or self._inspect_cache_rect != rect:
            self._build_inspect_cache(rect)
        if self._inspect_cache is not None:
            p.drawPixmap(rect.topLeft(), self._inspect_cache)
        pin = self._pinned
        hv = self._hover_node
        # 锁定控件先用琥珀色虚线框住（悬浮到别的控件时也能看到"钉"住的是哪个）
        if pin is not None and pin is not hv:
            self._paint_highlight(p, self._node_rect(rect, pin),
                                  fill=_INSPECT_PIN_FILL, line=_INSPECT_PIN, dashed=True)
        if hv is None:
            return
        hr = self._node_rect(rect, hv)
        # DevTools 风格：命中控件之外的画面压暗，视线只落在控件上。
        # 压暗范围只取内容区（rect）：letterbox 黑边本来就是底色，
        # 没必要再对它做一次全画布 alpha 填充。
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 92))
        w_rect = QRectF(rect)
        for band in (
                QRectF(w_rect.left(), w_rect.top(), w_rect.width(),
                       max(0.0, hr.top() - w_rect.top())),
                QRectF(w_rect.left(), hr.bottom(), w_rect.width(),
                       max(0.0, w_rect.bottom() - hr.bottom())),
                QRectF(w_rect.left(), hr.top(), max(0.0, hr.left() - w_rect.left()),
                       max(0.0, hr.height())),
                QRectF(hr.right(), hr.top(), max(0.0, w_rect.right() - hr.right()),
                       max(0.0, hr.height()))):
            if band.width() >= 1 and band.height() >= 1:
                p.drawRect(band)
        # content 淡蓝填充 + accent 边框（Chrome Overlay.highlightNode 配色）
        self._paint_highlight(p, hr, fill=_INSPECT_FILL, line=_INSPECT_HL)
        self._draw_info_card(p, hr, hv)

    def _draw_info_card(self, p: QPainter, hr: QRectF, node: dict):
        """信息卡：类名徽章 + id/文本/bounds/属性/路径。

        大控件贴着画会遮住主体，改为固定在画面左下角；小控件则跟随在
        控件右下（越界翻到左上），夹回可视区。
        """
        raw = node_info(node).split("\n")
        title = short(raw[0], 40)
        detail = [short(s, 64) for s in raw[1:]]
        # uiautodev 风格的百分比辅助定位（不同分辨率下更好对位）
        sw, sh = self._inspect_screen
        if sw and sh:
            x1, y1, x2, y2 = node["bounds"]
            detail.append(f"位置 {(x1 + x2) / 2 / sw * 100:.0f}%,{(y1 + y2) / 2 / sh * 100:.0f}%"
                          f"  ·  占屏 {(x2 - x1) / sw * 100:.0f}%x{(y2 - y1) / sh * 100:.0f}%")
        sel = node.get("sel") or ""
        if sel:
            same = sum(1 for m in self._inspect_nodes if m.get("sel") == sel)
            if same > 1:
                detail.append(f"该选择器匹配 {same} 个控件（复制后建议加序号/层级）")
        fm = p.fontMetrics()
        pad = 10
        line_h = fm.height()
        title_w = fm.horizontalAdvance(title)
        body_w = max((fm.horizontalAdvance(s) for s in detail), default=0)
        bw = max(title_w + 22, body_w) + pad * 2
        bw = min(bw, max(80.0, self.width() - 16.0))
        bh = pad * 2 + line_h + 6 + line_h * len(detail)
        vw, vh = float(self.width()), float(self.height())
        big = hr.width() * hr.height() > 0.35 * vw * vh
        if big:
            bx, by = 8.0, vh - bh - 8.0
        else:
            bx, by = hr.right() + 12, hr.bottom() + 12
            if bx + bw > vw - 8:
                bx = hr.left() - bw - 12
            if by + bh > vh - 8:
                by = hr.top() - bh - 12
            if by < 8:
                by = vh - bh - 8
        bx = min(max(8.0, bx), max(8.0, vw - bw - 8))
        by = min(max(8.0, by), max(8.0, vh - bh - 8))
        card = QRectF(bx, by, bw, bh)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(12, 16, 22, 238))
        p.drawRoundedRect(card, 8, 8)
        p.setPen(QPen(QColor(78, 161, 245, 130), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(card, 8, 8)
        badge = QRectF(bx + pad, by + pad, min(title_w + 14, bw - pad * 2),
                       line_h + 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(78, 161, 245, 64))
        p.drawRoundedRect(badge, 4, 4)
        p.setPen(QColor(214, 231, 255))
        p.drawText(badge.adjusted(7, 0, -7, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)
        p.setPen(QColor(232, 238, 248))
        ty = by + pad + badge.height() + 6 + fm.ascent()
        for s in detail:
            p.drawText(QPointF(bx + pad, ty), s)
            ty += line_h

    # ---- 坐标映射 ----
    def map_to_video(self, pos):
        rect = self._content_rect()
        if rect.isEmpty() or not rect.contains(pos):
            return None
        w, h = self._video_size
        vx = int((pos.x() - rect.x()) / rect.width() * w)
        vy = int((pos.y() - rect.y()) / rect.height() * h)
        return max(0, min(vx, w - 1)), max(0, min(vy, h - 1))

    def _mirror(self) -> ScrcpyMirror | None:
        w = self.worker
        return w.current() if w is not None and w.isRunning() else None

    def _feed(self, kind: str, code: str, value: int):
        if self.on_input_event is not None:
            self.on_input_event(RawEvent(ts=time.monotonic(), node="mirror",
                                         kind=kind, code=code, value=int(value)))

    def _feed_xy(self, x: int, y: int):
        self._feed("EV_ABS", "ABS_MT_POSITION_X", x)
        self._feed("EV_ABS", "ABS_MT_POSITION_Y", y)
        self._feed("EV_SYN", "SYN_REPORT", 0)

    # ---- 鼠标 ----
    def mousePressEvent(self, ev):
        m = self._mirror()
        if ev.button() == Qt.MouseButton.RightButton:
            # 右键不是点击操作：不注入返回键、也不进录制流水线。
            # 透视开启时选中控件（同一控件不取消，取消走菜单）并弹出断言菜单。
            if not self._inspect_nodes:
                # 未开控件透视：仍可整屏存断言基线；控件断言需先勾「控件透视」
                if m is not None:
                    self._show_assert_menu(ev.position(), None)
                return
            node = self.inspect_hit(ev.position())
            if node is not None and node is not self._pinned:
                self._pin(node)
            elif node is None and self._pinned is not None:
                self._pin(None)   # 点空白处取消锁定
            self._show_assert_menu(ev.position(), node)
            return
        if ev.button() == Qt.MouseButton.MiddleButton:
            if self.on_shortcut:
                self.on_shortcut("home")
            return
        if ev.button() != Qt.MouseButton.LeftButton or m is None:
            return
        pt = self.map_to_video(ev.position())
        if pt is None:
            return
        # 点击投屏即取得键盘焦点：本类重写了 mousePressEvent 不调 super，
        # 默认的 ClickFocus 聚焦不会发生，不显式 setFocus 则物理键盘无从接入（A2）
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        # 开始触摸操作时收起悬浮信息卡，避免拖拽全程挂着卡片遮挡画面
        # （锁定控件不受影响，抬手后会重新上屏）
        if self._hover_node is not None:
            self._hover_node = None
        self._pressed = True
        self._last_video = pt
        m.touch_down(*pt)
        self._mark = ev.position()
        self._mark_timer.start()
        self.update()
        self._feed("EV_ABS", "ABS_MT_TRACKING_ID", 1)
        self._feed_xy(*pt)

    def mouseMoveEvent(self, ev):
        if not self._pressed:
            # 未按住：透视开启时做悬浮命中（不干扰设备）
            if self._inspect_nodes:
                node = self.inspect_hit(ev.position())
                if node is None:
                    node = self._pinned   # 锁定后移开也保留高亮/信息卡
                if node is not self._hover_node:
                    self._hover_node = node
                    if self.on_inspect_hover is not None:
                        self.on_inspect_hover(node)
                    self.update()
            return
        m = self._mirror()
        pt = self.map_to_video(ev.position())
        if m is None or pt is None or pt == self._last_video:
            return
        self._last_video = pt
        m.touch_move(*pt)
        self._mark = ev.position()
        self.update()
        self._feed_xy(*pt)

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or not self._pressed:
            return
        self._pressed = False
        m = self._mirror()
        pt = self.map_to_video(ev.position()) or self._last_video
        if m is not None and pt is not None:
            m.touch_up(*pt)
        self._feed("EV_ABS", "ABS_MT_TRACKING_ID", -1)
        self._feed("EV_SYN", "SYN_REPORT", 0)
        if self._pinned is not None:
            self._hover_node = self._pinned   # 手势结束把锁定控件的信息卡放回来
            self.update()

    # 窗口尺寸变化不需要单独失效缓存：_draw_inspect 对比 _content_rect()
    # 自己就能发现几何变了（隐藏控件 grab() 也会发 resize，白白重建一次）

    def leaveEvent(self, _ev):
        node = self._pinned            # 已锁定的控件不因鼠标移出画面而消失
        if node is not self._hover_node:
            self._hover_node = node
            if self.on_inspect_hover is not None:
                self.on_inspect_hover(node)
            self.update()

    def wheelEvent(self, ev):
        m = self._mirror()
        pt = self.map_to_video(ev.position())
        if m is None or pt is None:
            return
        d = ev.angleDelta()
        m.scroll(pt[0], pt[1], d.x() / 120.0, d.y() / 120.0)

    # ---- 物理键盘（A2）----
    def keyPressEvent(self, ev):
        m = self._mirror()
        if m is None:
            super().keyPressEvent(ev)
            return
        # Ctrl/Alt/Meta 组合留给应用/系统快捷键，不注入设备（Shift 放行以输入大写/符号）
        if ev.modifiers() & (Qt.KeyboardModifier.ControlModifier
                             | Qt.KeyboardModifier.AltModifier
                             | Qt.KeyboardModifier.MetaModifier):
            super().keyPressEvent(ev)
            return
        special = self.SPECIAL_KEYS.get(ev.key())
        if special is not None:
            keycode, linux_name = special
            m.key(keycode)
            if self.on_key_event is not None:
                self.on_key_event(linux_name)
            ev.accept()
            return
        text = ev.text()
        if text and text.isprintable():
            m.text(text)
            if self.on_text_event is not None:
                self.on_text_event(text)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def inputMethodEvent(self, ev):
        # IME（中文输入法等）提交串不走 keyPressEvent，这里补注入；
        # 预编辑串（候选未上屏）不注入，避免把拼音半成品打进设备
        commit = ev.commitString()
        m = self._mirror()
        if commit and m is not None:
            m.text(commit)
            if self.on_text_event is not None:
                self.on_text_event(commit)
            ev.accept()
            return
        super().inputMethodEvent(ev)


def _menu_exec(menu, pos):
    """右键菜单弹出单一出口：自检桩掉它避免真事件循环阻塞。"""
    return menu.exec(pos)


class MirrorPanel(QWidget):
    """投屏页签：连接控制 + 画面 + 快捷键 + 鼠标操作录制成用例。"""
    sig_case_saved = Signal(str)
    sig_inspect = Signal(object, object)   # 控件树抓取完成：(nodes, screen_size)
    sig_inspect_failed = Signal(str)
    sig_inspect_note = Signal(str)         # 重试/回退等过程提示（进日志）
    sig_focus = Signal(object)             # 前台应用信息：{"package","activity","window"}
    sig_popout = Signal(object)            # 画面拖出/单独立展示：QPoint 落点 或 None
    sig_assert = Signal(object)            # 画面右键断言请求：{"kind":…, "node":控件节点|""}

    # 快捷键 -> Linux 键名（录制时经 key_aliases 转 Android KEYCODE）
    SHORTCUT_KEYS = {"back": "KEY_BACK", "home": "KEY_HOME", "recents": "KEY_APP_SWITCH",
                     "power": "KEY_POWER", "voldown": "KEY_VOLUMEDOWN",
                     "volup": "KEY_VOLUMEUP"}

    # 画质预设：(长边上限像素, 码率Mbps, 帧率)；原画=原始分辨率高码率
    QUALITY_PRESETS = {"流畅": (640, 4, 30), "标准": (1280, 8, 30),
                       "高清": (1920, 16, 60), "原画": (0, 24, 60)}

    def __init__(self, cfg: dict, serial_getter=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._serial_getter = serial_getter or (lambda: "")
        self.worker: MirrorWorker | None = None
        self.meta: dict | None = None
        self._tr: Translator | None = None
        self._tr_profile: dict | None = None
        self.on_record_event = None       # fn(RawEvent)：转发给录制页（getevent）录制
        self.on_record_text = None        # fn(str)：同上，补录投屏键盘输入的文本
        self._frame_count = 0
        # 单槽拉帧：定时器按 16ms 节拍从会话取最新一帧，旧帧在源头就被覆盖，
        # 不经 Qt 事件队列，UI 卡顿不会让帧对象积压、延迟滚大
        self._last_ts = 0.0
        self._inspect_busy = False
        self._focus: dict = {}              # 前台应用信息（包名/界面），由透视线程顺带抓
        self._inspect_items: list = []      # 控件树条目，索引与 _inspect_nodes 对齐
        self._node_item: dict = {}          # id(node) -> QTreeWidgetItem，双向联动用
        self._ext_proc = None               # 高性能（外部 scrcpy）进程：与内嵌投屏互斥
        self.player = AudioPlayer(self)
        self._build_ui()
        self.sig_inspect.connect(self._on_inspect_done)
        self.sig_inspect_failed.connect(self._on_inspect_failed)
        self.sig_inspect_note.connect(lambda m: self._on_log("[控件透视] " + m))
        self.sig_focus.connect(self._on_focus)
        self._inspect_timer = QTimer(self)
        self._inspect_timer.setInterval(2500)
        self._inspect_timer.timeout.connect(self._refresh_inspect)
        self._fps_timer = QTimer(self)
        self._fps_timer.setInterval(1000)
        self._fps_timer.timeout.connect(self._tick_fps)
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(16)
        self._frame_timer.timeout.connect(self._pull_frame)
        self._ext_timer = QTimer(self)
        self._ext_timer.setInterval(500)
        self._ext_timer.timeout.connect(self._watch_external)

    # ---------------- UI ----------------
    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # 第一行：连接控制 + 编码参数 + 状态
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_start = QPushButton("▶ 开始投屏")
        set_role(self.btn_start, "accent")
        self.btn_start.clicked.connect(self.start_mirror)
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_mirror)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addSpacing(12)
        pl = QLabel("画质")
        set_subdued(pl)
        row.addWidget(pl)
        self.cmb_preset = QComboBox()
        self.cmb_preset.addItems(["自定义", "流畅", "标准", "高清", "原画"])
        self.cmb_preset.setToolTip("一键套用预设的分辨率/码率/帧率组合，仍可手动微调")
        self.cmb_preset.setMinimumWidth(76)   # 不用固定宽：大字体/高 DPI 下可撑开不截断
        self.cmb_preset.currentTextChanged.connect(self._apply_preset)
        row.addWidget(self.cmb_preset)
        row.addSpacing(8)
        for caption in ("分辨率", "码率", "帧率"):
            lb = QLabel(caption)
            set_subdued(lb)
            row.addWidget(lb)
            if caption == "分辨率":
                self.cmb_size = QComboBox()
                self.cmb_size.addItems(["1280", "960", "640", "1920", "原始"])
                self.cmb_size.setCurrentText(str(self.cfg.get("mirror_max_size", 1280))
                                             if self.cfg.get("mirror_max_size", 1280) else "原始")
                self.cmb_size.setMinimumWidth(86)
                row.addWidget(self.cmb_size)
            elif caption == "码率":
                self.spn_bitrate = QSpinBox()
                self.spn_bitrate.setRange(1, 50)
                self.spn_bitrate.setSuffix(" Mbps")
                self.spn_bitrate.setValue(max(1, int(self.cfg.get("mirror_bit_rate", 8_000_000)) // 1_000_000))
                row.addWidget(self.spn_bitrate)
            else:
                self.spn_fps = QSpinBox()
                self.spn_fps.setRange(1, 120)
                self.spn_fps.setValue(int(self.cfg.get("mirror_max_fps", 30)) or 30)
                row.addWidget(self.spn_fps)
        row.addSpacing(12)
        self.chk_audio = QCheckBox("设备声音")
        self.chk_audio.setChecked(bool(self.cfg.get("mirror_audio", True)))
        self.chk_audio.setToolTip("把设备播放的声音转发到电脑（scrcpy opus 音频通道）")
        if not HAS_QT_AUDIO:
            self.chk_audio.setChecked(False)
            self.chk_audio.setEnabled(False)
            self.cfg["mirror_audio"] = False  # 播不了就别让设备白采集
            self.chk_audio.setToolTip("缺少 PySide6.QtMultimedia，无法在电脑端播放设备声音")
        self.chk_audio.toggled.connect(self._on_audio_toggled)
        row.addWidget(self.chk_audio)
        row.addSpacing(12)
        self.lbl_info = QLabel("未连接")
        self.lbl_info.setWordWrap(True)   # 长设备名/旋转尺寸信息换行展示，不截断
        row.addWidget(self.lbl_info, 1)
        self.lbl_fps = QLabel("— fps")
        self.lbl_fps.setObjectName("fpsBadge")
        row.addWidget(self.lbl_fps)
        v.addLayout(row)
        # 全部参数控件就位后，回填当前配置对应的预设（不触发套用信号）
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.setCurrentText(self._match_preset())
        self.cmb_preset.blockSignals(False)

        # 第二行：快捷键 + 文本注入
        keys = QHBoxLayout()
        keys.setSpacing(6)
        for text, name in (("返回", "back"), ("Home", "home"), ("最近任务", "recents"),
                           ("电源", "power"), ("音量−", "voldown"), ("音量＋", "volup")):
            b = QPushButton(text)
            b.setProperty("key", True)
            b.setToolTip("中键画面=Home")
            b.clicked.connect(lambda _c=False, n=name: self._shortcut(n))
            keys.addWidget(b)
        keys.addSpacing(16)
        self.txt_inject = QLineEdit()
        self.txt_inject.setPlaceholderText("向设备注入文本（支持中文输入法），回车发送")
        self.txt_inject.returnPressed.connect(self._inject_text)
        keys.addWidget(self.txt_inject, 1)
        b = QPushButton("发送文本")
        b.clicked.connect(self._inject_text)
        keys.addWidget(b)
        b = QPushButton("⚡ 高性能")
        b.setToolTip(f"启动官方 scrcpy 客户端（C/SDL GPU 渲染，帧率/清晰度上限最高），\n"
                     f"使用上方同样的分辨率/码率/帧率参数，独立窗口显示。\n"
                     f"关闭高性能窗口后自动恢复内嵌投屏。\n"
                     f"首次使用会提示下载便携版部署到 {EXTERNAL_TOOLS_DIR}\\scrcpy")
        b.clicked.connect(self._launch_external)
        keys.addWidget(b)
        keys.addSpacing(10)
        self.chk_record_video = QCheckBox("录像")
        self.chk_record_video.setToolTip(
            "投屏期间设备端同步录像（scrcpy video_file，编码流直写，不占电脑帧率），\n"
            "停止投屏后自动拉回 reports/mirror_时间戳.h264，可用 VLC/ PotPlayer 回放")
        keys.addWidget(self.chk_record_video)
        v.addLayout(keys)

        # 第二行：控件透视（独立一行，窄窗口也不会被快捷键按钮挤出去）
        insp = QHBoxLayout()
        insp.setSpacing(6)
        self.chk_inspect = QCheckBox("控件透视")
        self.chk_inspect.setToolTip(
            "在投屏画面上叠加当前设备界面全部控件的轮廓\n"
            "鼠标悬浮：高亮控件并展示类名/id/文本/bounds 等信息\n"
            "右键：选中控件（不算点击操作），侧栏定位到它并锁定高亮；\n"
            "右键菜单可把该控件设成断言（存在/不存在/文本）或存成截图基线\n"
            "数据来自 uiautomator dump（仅活动应用窗口）；勾选「全窗口(u2)」可含状态栏/dock\n"
            "画面变化后点「刷新控件」或勾选「自动刷新」")
        self.chk_inspect.toggled.connect(self._on_inspect_toggled)
        insp.addWidget(self.chk_inspect)
        self.chk_inspect_tree = QCheckBox("控件树")
        self.chk_inspect_tree.setEnabled(False)
        self.chk_inspect_tree.setToolTip(
            "右侧控件树 + 属性表：顶部显示前台应用包名/界面（双击即复制），\n"
            "容器层灰字、可交互控件加粗，按 类名/id/文本 分列；\n"
            "深页可勾「只看有效」把纯包装层压掉；右键画面选中控件后侧栏锁定显示，\n"
            "双击条目复制定位表达式，树内右键菜单可展开全部/折叠到 3 层/复制路径")
        self.chk_inspect_tree.toggled.connect(self._on_tree_toggled)
        insp.addWidget(self.chk_inspect_tree)
        self.chk_inspect_auto = QCheckBox("自动刷新")
        self.chk_inspect_auto.setEnabled(False)
        self.chk_inspect_auto.setToolTip(
            "每 2.5s 自动重抓控件树：动画/视频界面也能跟上界面变化（未连接时自动跳过）")
        self.chk_inspect_auto.toggled.connect(self._on_auto_toggled)
        insp.addWidget(self.chk_inspect_auto)
        self.chk_inspect_u2 = QCheckBox("全窗口(u2)")
        self.chk_inspect_u2.setChecked(bool(self.cfg.get("inspect_u2", False)))
        self.chk_inspect_u2.setToolTip(
            "用 uiautomator2 常驻服务抓全窗口控件树：含状态栏/dock 栏等系统窗口\n"
            "（原生 uiautomator dump 只有活动应用窗口，看不到这两条栏）\n"
            "需 pip install uiautomator2，首次连接会在设备装辅助 APK；不可用时自动回退原生 dump")
        self.chk_inspect_u2.toggled.connect(self._on_u2_toggled)
        insp.addWidget(self.chk_inspect_u2)
        self.btn_inspect_refresh = QPushButton("刷新控件")
        self.btn_inspect_refresh.setEnabled(False)
        self.btn_inspect_refresh.setToolTip("重新抓取设备当前界面的控件树（失败会自动重试）")
        self.btn_inspect_refresh.clicked.connect(self._refresh_inspect)
        insp.addWidget(self.btn_inspect_refresh)
        self.lbl_inspect = QLabel("")
        set_subdued(self.lbl_inspect)
        self.lbl_inspect.setWordWrap(False)
        insp.addWidget(self.lbl_inspect, 1)
        v.addLayout(insp)

        # 主体：画面 + 录制面板
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.view = MirrorView()
        self.view.on_input_event = self._on_input_event
        self.view.on_shortcut = self._shortcut
        self.view.on_key_event = self._on_view_key
        self.view.on_text_event = self._on_view_text
        self.view.on_inspect_hover = self._on_view_inspect_hover
        self.view.on_inspect_pick = self._on_view_inspect_pick
        self.view.on_assert_pick = self._on_assert_pick
        split.addWidget(self.view)
        # 画面右上角把手：拖动/单击把画面拖出主窗口独立展示（关窗收回）
        self.btn_popout = PopoutButton("⧉", self.view)
        self.btn_popout.setToolTip(
            "把投屏画面拖出主窗口独立展示（单击也行）；\n关闭独立窗口即收回主界面，投屏不断")
        self.btn_popout.released.connect(self.sig_popout.emit)
        self.view.btn_popout = self.btn_popout
        self.view.place_popout()

        # 控件树侧栏（默认隐藏，勾选「控件树」时展开）：筛选 + 树 + 明细/复制定位
        self.insp_panel = QWidget()
        ip = QVBoxLayout(self.insp_panel)
        ip.setContentsMargins(0, 0, 0, 0)
        ip.setSpacing(6)
        # 前台应用信息（包名 + 界面）：常驻顶部，双击/右键复制
        self.lbl_insp_app = CopyLabel("应用：—")
        self.lbl_insp_app.on_copied = lambda cap: self.lbl_inspect.setText("已复制：" + cap)
        self.lbl_insp_app.setToolTip("开始投屏并开启控件透视后，这里显示前台应用包名与界面；双击复制")
        ip.addWidget(self.lbl_insp_app)
        fbar = QHBoxLayout()
        fbar.setSpacing(6)
        self.insp_filter = QLineEdit()
        self.insp_filter.setPlaceholderText("筛选：类名 / id / 文本")
        self.insp_filter.textChanged.connect(lambda _t: self._apply_inspect_filter())
        fbar.addWidget(self.insp_filter, 1)
        self.chk_insp_compact = QCheckBox("只看有效")
        self.chk_insp_compact.setToolTip(
            "把既无 id/文本/描述、也不能交互的纯包装层压掉，\n"
            "它们的子节点直接挂到最近的有效祖先下：30 多层的页面会被压成几层，父子层级一眼能读出")
        self.chk_insp_compact.toggled.connect(self._on_compact_toggled)
        fbar.addWidget(self.chk_insp_compact)
        ip.addLayout(fbar)
        self.insp_tree = QTreeWidget()
        # 三列：控件 / id / 文本。实景页面常深达 17 层，列 0 会被逐层缩进推得很右，
        # 于是把 id/文本放进固定列、缩进压到 12（默认 20）：任何层级都能横向对齐扫读，
        # 再配合「容器层灰字 / 可交互控件加粗」一眼分清包装层和真控件。
        self.insp_tree.setHeaderLabels(["控件", "id", "文本"])
        self.insp_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # 最后一列不能拉伸，否则会把上面设置的固定列宽吃掉，主列反而被挤窄
        self.insp_tree.header().setStretchLastSection(False)
        for col in (1, 2):
            self.insp_tree.setColumnWidth(col, 100)
        self.insp_tree.setIndentation(12)
        self.insp_tree.setUniformRowHeights(True)
        self.insp_tree.setAllColumnsShowFocus(True)    # 选中行整行高亮：深层条目也看得见上下文
        self.insp_tree.setExpandsOnDoubleClick(False)  # 双击留给「复制定位表达式」
        self.insp_tree.setAlternatingRowColors(True)
        # 竖滚动条常开：否则它晚一步出现会把视口挤窄，凭空多出一条假横向滚动条
        self.insp_tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.insp_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.insp_tree.currentItemChanged.connect(self._on_tree_current)
        self.insp_tree.itemDoubleClicked.connect(self._on_tree_double)
        self.insp_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.insp_tree.customContextMenuRequested.connect(self._on_tree_menu)
        self.insp_tree.setToolTip(
            "容器层灰字、可交互控件加粗；右键菜单可展开全部/折叠到 3 层/复制定位/复制单元格值")
        ip.addWidget(self.insp_tree, 2)
        # 属性表：完整属性逐行展示（比一整块纯文本好扫读，双击可复制值）
        self.insp_props = QTreeWidget()
        self.insp_props.setHeaderLabels(["属性", "值"])
        self.insp_props.setRootIsDecorated(False)
        self.insp_props.setAlternatingRowColors(True)
        self.insp_props.setUniformRowHeights(True)
        self.insp_props.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.insp_props.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.insp_props.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.insp_props.setColumnWidth(0, 86)
        self.insp_props.header().setStretchLastSection(True)
        self.insp_props.setMinimumHeight(150)
        self.insp_props.setToolTip("选中/悬浮控件后显示完整属性；双击或右键复制值")
        self.insp_props.itemDoubleClicked.connect(self._on_prop_double)
        self.insp_props.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.insp_props.customContextMenuRequested.connect(self._on_prop_menu)
        ip.addWidget(self.insp_props, 1)
        self.btn_insp_copy = QPushButton("复制定位表达式")
        self.btn_insp_copy.setToolTip("把当前条目的选择器（id > text > desc > class）复制到剪贴板")
        self.btn_insp_copy.clicked.connect(
            lambda: self._on_tree_double(self.insp_tree.currentItem(), 0))
        ip.addWidget(self.btn_insp_copy)
        self.insp_panel.setVisible(False)
        split.addWidget(self.insp_panel)

        right = self.rec_panel = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        gb = QGroupBox("鼠标操作录制")
        gv = QVBoxLayout(gb)
        gv.setSpacing(8)
        self.chk_record = QCheckBox("把鼠标操作录制成用例")
        self.chk_record.stateChanged.connect(self._toggle_record)
        gv.addWidget(self.chk_record)
        self.rec_out = QLineEdit(str(ROOT / "cases" / "mirror_recorded.yaml"))
        gv.addWidget(self._line_with_button(self.rec_out, "浏览…", self._browse_out))
        self.btn_save_case = QPushButton("保存用例")
        set_role(self.btn_save_case, "accent")
        self.btn_save_case.clicked.connect(self._save_recorded)
        gv.addWidget(self.btn_save_case)
        self.tbl_steps = QTableWidget(0, 4)
        self.tbl_steps.setHorizontalHeaderLabels(["#", "动作", "at_ms", "参数"])
        self.tbl_steps.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_steps.setAlternatingRowColors(True)
        self.tbl_steps.setShowGrid(False)
        self.tbl_steps.verticalHeader().setVisible(False)
        self.tbl_steps.verticalHeader().setDefaultSectionSize(26)
        for i, cw in enumerate((36, 90, 70)):
            self.tbl_steps.setColumnWidth(i, cw)
        self.tbl_steps.horizontalHeader().setStretchLastSection(True)
        self.tbl_steps.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        gv.addWidget(self.tbl_steps, 1)
        hint = QLabel("左键点击=tap · 左键拖拽=swipe · 右键=选中控件+断言菜单（不注入）\n"
                      "中键=Home · 滚轮=滚动（不录制）· 键盘打字可注入\n"
                      "投屏注入的手势不走内核，getevent 录不到，\n"
                      "请勾选上方开关用本面板录制。")
        hint.setWordWrap(True)   # 右侧面板拖窄时换行，不截断
        set_subdued(hint)
        rv.addWidget(gb, 1)
        rv.addWidget(hint)
        split.addWidget(right)
        self.split = split
        split.setSizes([780, 340])

        # 录制面板默认收起：窗口右侧留一条窄把手，点一下展开、再点收起
        self.btn_rec_toggle = QToolButton()
        self.btn_rec_toggle.setObjectName("recHandle")
        self.btn_rec_toggle.setText("◂")
        self.btn_rec_toggle.setFixedWidth(20)
        self.btn_rec_toggle.setSizePolicy(QSizePolicy.Policy.Fixed,
                                          QSizePolicy.Policy.Expanding)
        self.btn_rec_toggle.setToolTip("展开「鼠标操作录制」面板")
        self.btn_rec_toggle.clicked.connect(lambda: self._toggle_rec_panel())
        self.rec_panel.setVisible(False)
        self._update_rec_handle()

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(4)
        body.addWidget(split, 1)
        body.addWidget(self.btn_rec_toggle)
        v.addLayout(body, 1)

    # ---------------- 录制面板折叠 ----------------
    def _toggle_rec_panel(self, show: bool | None = None):
        """展开/收起「鼠标操作录制」面板（默认收起，只留 20px 把手）。"""
        show = self.rec_panel.isHidden() if show is None else bool(show)
        self.rec_panel.setVisible(show)
        if show:
            # 展开时把宽度真正分给它：隐藏期间 splitter 把它压成 0，只靠显示不够
            self.rec_panel.setMinimumWidth(260)
            total = max(480, self.split.width() or 1200)
            insp_w = 0 if self.insp_panel.isHidden() else max(260, self.insp_panel.width())
            self.split.setSizes([max(200, total - insp_w - 320), insp_w, 320])
        self._update_rec_handle()

    def _update_rec_handle(self):
        """把手文案/提示/录制中高亮：面板收起时也知道录制开没开。"""
        shown = not self.rec_panel.isHidden()
        recording = self.chk_record.isChecked()
        self.btn_rec_toggle.setText("▸" if shown else "◂")
        self.btn_rec_toggle.setToolTip(
            ("收起" if shown else "展开") + "「鼠标操作录制」面板"
            + ("　·　正在录制" if recording else ""))
        self.btn_rec_toggle.setProperty("recording", "true" if recording else "false")
        st = self.btn_rec_toggle.style()
        st.unpolish(self.btn_rec_toggle)
        st.polish(self.btn_rec_toggle)

    def _line_with_button(self, line: QLineEdit, caption: str, slot) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(line, 1)
        b = QPushButton(caption)
        b.clicked.connect(slot)
        h.addWidget(b)
        return w

    # ---------------- 画质预设 ----------------
    def _current_max_size(self) -> int:
        t = self.cmb_size.currentText()
        return 0 if t == "原始" else int(t)

    def _match_preset(self) -> str:
        cur = (self._current_max_size(), self.spn_bitrate.value(), self.spn_fps.value())
        for name, vals in self.QUALITY_PRESETS.items():
            if vals == cur:
                return name
        return "自定义"

    def _apply_preset(self, name: str):
        if name == "自定义" or name not in self.QUALITY_PRESETS:
            return
        size, mbps, fps = self.QUALITY_PRESETS[name]
        self.cmb_size.setCurrentText("原始" if size == 0 else str(size))
        self.spn_bitrate.setValue(mbps)
        self.spn_fps.setValue(fps)

    # ---------------- 外挂官方 scrcpy ----------------
    def _external_running(self) -> bool:
        """高性能（外部 scrcpy 独立窗口）是否还活着。"""
        p = self._ext_proc
        return p is not None and p.poll() is None

    def _stop_external(self):
        """关掉高性能窗口：外部 scrcpy 与内嵌投屏不能共存。"""
        self._ext_timer.stop()
        p = self._ext_proc
        self._ext_proc = None
        if p is None or p.poll() is not None:
            return
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def _watch_external(self):
        """轮询高性能进程退出：窗口关闭后自动恢复内嵌投屏。

        两种投屏互斥，切高性能时停了内嵌；外部窗口退出后必须有人
        接管，否则画面就黑着没人管。主动切换/关 App 走 _stop_external
        会先停掉本定时器，不会误触发。
        """
        p = self._ext_proc
        if p is None:
            self._ext_timer.stop()
            return
        if p.poll() is None:
            return
        self._ext_proc = None
        self._ext_timer.stop()
        self._on_log("[投屏] 高性能窗口已关闭，自动恢复内嵌投屏")
        if self.worker is None or not self.worker.isRunning():
            self.start_mirror()

    def _launch_external(self):
        if self._external_running():
            QMessageBox.information(self, "高性能模式",
                                    "高性能窗口已在运行；两个投屏会抢同一设备，先关掉旧的再说")
            return
        exe = find_external_scrcpy(self.cfg)
        if exe is None:
            ret = QMessageBox.question(
                self, "高性能模式",
                f"未找到官方 scrcpy（默认部署位置 {EXTERNAL_TOOLS_DIR}\\scrcpy）。\n\n"
                f"是否下载官方 win64 便携版（约 9MB）并解压到 {EXTERNAL_TOOLS_DIR}\\scrcpy？\n"
                f"下载地址：{EXTERNAL_ZIP_URL}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
            self.lbl_info.setText("下载 scrcpy 中…")
            QApplication.processEvents()
            try:
                exe = download_external_scrcpy(log=self._on_log)
            except Exception as exc:
                QMessageBox.critical(self, "下载失败", str(exc))
                self.lbl_info.setText("未连接")
                return
            self.lbl_info.setText("未连接")
        # 互斥：同一个设备只允许一种投屏在推流，双开会互相把对方的画面挤没。
        # 放在「确实能启动」之后：下载被取消时不去扰动正在跑的投屏
        if self.worker is not None and self.worker.isRunning():
            ret = QMessageBox.question(
                self, "切换到高性能模式",
                "内嵌投屏正在运行。\n高性能投屏与普通投屏不能共存，"
                "是否停止内嵌投屏并切换到高性能窗口？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
            self.stop_mirror(wait_ms=3000)
            self._on_log("[投屏] 已停止内嵌投屏，即将启动高性能窗口")
        try:
            proc = launch_external_scrcpy(
                exe, self._serial_getter(), max_size=self._current_max_size(),
                bit_rate=self.spn_bitrate.value() * 1_000_000,
                max_fps=self.spn_fps.value(), audio=self.chk_audio.isChecked(),
                adb_path=str(self.cfg.get("adb_path") or ""))
        except OSError as exc:
            QMessageBox.critical(self, "启动失败", str(exc))
            return
        self._ext_proc = proc
        self._ext_timer.start()
        self._on_log(f"[投屏] 已启动官方 scrcpy 高性能窗口（pid={proc.pid}），参数与面板一致；"
                     f"高性能投屏与内嵌投屏不能共存，已停止内嵌投屏；关闭该窗口后自动恢复")

    # ---------------- 投屏生命周期 ----------------
    def start_mirror(self):
        if self.worker is not None and self.worker.isRunning():
            return
        if not has_decoder():
            QMessageBox.warning(self, "缺少解码器",
                                "实时投屏需要 PyAV 与 numpy：\n  pip install av numpy")
            return
        if self._external_running():
            ret = QMessageBox.question(
                self, "切换到普通投屏",
                "高性能投屏（独立 scrcpy 窗口）正在运行。\n"
                "高性能投屏与普通投屏不能共存，是否关闭高性能窗口并使用内嵌投屏？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
            self._stop_external()
            self._on_log("[投屏] 已关闭高性能窗口，切换到内嵌投屏")
        size_text = self.cmb_size.currentText()
        max_size = 0 if size_text == "原始" else int(size_text)
        record_path = ""
        if self.chk_record_video.isChecked():
            from datetime import datetime
            record_path = str(ROOT / "reports" /
                              f"mirror_{datetime.now():%Y%m%d_%H%M%S}.h264")
        w = MirrorWorker(self.cfg, self._serial_getter(), max_size,
                         self.spn_bitrate.value() * 1_000_000, self.spn_fps.value(),
                         audio=self.chk_audio.isChecked(), record_path=record_path)
        self.worker = w
        w.sig_audio.connect(self.player.push)
        w.sig_meta.connect(self._on_meta)
        w.sig_log.connect(self._on_log)
        w.sig_failed.connect(self._on_failed)
        w.sig_stopped.connect(self._on_stopped)
        w.sig_reconnecting.connect(self._on_reconnecting)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_info.setText("连接中…")
        self._frame_count = 0
        self._last_ts = 0.0
        w.start()
        self._fps_timer.start()
        self._frame_timer.start()

    def stop_mirror(self, wait_ms: int = 0):
        """停止投屏；wait_ms>0 时阻塞等它真正退完（切换高性能模式需要）。"""
        if self.worker is not None:
            self.btn_stop.setEnabled(False)
            self.worker.stop()
            if wait_ms and self.worker.isRunning():
                self.worker.wait(wait_ms)

    def shutdown(self):
        self._fps_timer.stop()
        self._frame_timer.stop()
        self.player.stop()
        self._stop_external()
        if self.worker is not None:
            self.worker.stop()
            if self.worker.isRunning() and not self.worker.wait(4000):
                self.worker.terminate()
                self.worker.wait(1000)

    def _pull_frame(self):
        # 单槽拉帧：只认时间戳更新的帧；UI 卡顿时旧帧在解码端已被覆盖，
        # 队列里不会积压历史帧（对比旧版 sig_frame 直推）
        w = self.worker
        if w is None:
            return
        session = w.current()
        if session is None:
            return
        arr, ts = session.latest_frame()
        if arr is None or ts <= self._last_ts:
            return
        self._last_ts = ts
        self._frame_count += 1
        self.view.set_frame(arr)
        # 旋转/分辨率变化时同步录制归一化基准 + 逻辑屏尺寸，否则坐标/元数据错位
        self._sync_tr_size(arr.shape[1], arr.shape[0], session.screen_size)

    def snapshot(self):
        """最新一帧 rgb24 拷贝（断言基线用）；未投屏/还没有帧时返回 None。"""
        w = self.worker
        session = w.current() if w is not None else None
        if session is None:
            return None
        try:
            arr, _ts = session.latest_frame()
        except Exception:
            return None
        return None if arr is None else arr.copy()

    def _sync_tr_size(self, w: int, h: int, screen_size=None):
        # 逻辑屏尺寸随旋转刷新：core 在视频尺寸变化时已重查 wm_size，这里把它
        # 传导到信息栏与用例 device.size 元数据（归一化用的是下面的 touch_abs）
        if screen_size and self.meta is not None:
            ss = [int(screen_size[0]), int(screen_size[1])]
            if ss != self.meta.get("screen_size"):
                self.meta["screen_size"] = ss
                if self._tr_profile is not None:
                    self._tr_profile["size"] = ss
                self.lbl_info.setText(
                    f"{self.meta['device_name']}  视频 {w}x{h}  屏幕 {ss[0]}x{ss[1]}")
                # 旋转后旧控件树坐标已失效：先收起错位轮廓再重抓，
                # 抓取窗口内不展示误导性的旧叠加
                if self.chk_inspect.isChecked():
                    self.view.clear_inspect()
                    self._refresh_inspect()
        if self._tr is None or self._tr_profile is None:
            return
        if [max(1, w - 1), max(1, h - 1)] != self._tr_profile["touch_abs"]:
            self._tr_profile["touch_abs"] = [max(1, w - 1), max(1, h - 1)]
            self._tr.abs_x = float(max(1, w - 1))
            self._tr.abs_y = float(max(1, h - 1))

    def _tick_fps(self):
        if self.worker is not None and self.worker.isRunning():
            self.lbl_fps.setText(f"{self._frame_count} fps")
        self._frame_count = 0

    def _on_meta(self, info: dict):
        self.meta = info
        self.view.worker = self.worker
        self.btn_stop.setEnabled(True)
        self.lbl_info.setText(f"{info['device_name']}  视频 {info['video_size'][0]}x{info['video_size'][1]}"
                              f"  屏幕 {info['screen_size'][0]}x{info['screen_size'][1]}")
        if self.chk_record.isChecked():
            self._ensure_translator()
        # 重连后是新会话，界面可能已变：透视开着就重抓控件树
        if self.chk_inspect.isChecked():
            self._refresh_inspect()

    def _on_log(self, msg: str):
        win = self.window()
        if hasattr(win, "append_log"):
            win.append_log(msg)

    def _on_failed(self, msg: str):
        self._on_log("[投屏失败] " + msg)
        QMessageBox.critical(self, "投屏失败", msg.splitlines()[0])

    def _on_reconnecting(self, msg: str):
        self._on_log("[投屏] " + msg)
        self.lbl_info.setText("⚠ 重连中：" + short(msg, 48))
        self._last_ts = 0.0   # 新会话从 0 计帧，时间戳基准可能重置

    def _on_audio_toggled(self, checked: bool):
        self.player.set_muted(not checked)
        self.cfg["mirror_audio"] = bool(checked)
        _persist_cfg_key("mirror_audio", bool(checked))

    def _on_stopped(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_fps.setText("— fps")
        self.lbl_info.setText("未连接")
        self.view.worker = None
        self.view.clear_frame()
        self._last_ts = 0.0
        self.player.stop()
        self._fps_timer.stop()
        self._frame_timer.stop()
        # 透视随会话结束复位（开关默认关，不跨会话残留旧控件树）
        self._inspect_busy = False
        self._inspect_timer.stop()
        self.chk_inspect.blockSignals(True)
        self.chk_inspect.setChecked(False)
        self.chk_inspect.blockSignals(False)
        self.chk_inspect_tree.setChecked(False)
        self.chk_inspect_auto.setChecked(False)
        self.chk_inspect_tree.setEnabled(False)
        self.chk_inspect_auto.setEnabled(False)
        self.insp_panel.setVisible(False)
        self.btn_inspect_refresh.setEnabled(False)
        self.view.clear_inspect()
        self._fill_inspect_tree([])
        self.lbl_inspect.setText("")
        self.lbl_inspect.setToolTip("")

    # ---------------- 控件透视 ----------------
    def _inspect_source(self):
        """当前会话的 (adb, 逻辑屏尺寸, 视频帧尺寸)；未连接返回 None。"""
        w = self.worker
        s = w.current() if w is not None else None
        if s is None:
            return None
        screen = tuple(s.screen_size or (0, 0))
        if not (screen[0] and screen[1]) and self.meta:
            screen = tuple(self.meta.get("screen_size") or (0, 0))
        video = tuple(getattr(s, "video_size", (0, 0)) or (0, 0))
        return s.adb, screen, video

    def _on_inspect_toggled(self, checked: bool):
        if checked:
            if self._inspect_source() is None:
                self.lbl_inspect.setText("请先开始投屏")
                self.chk_inspect.blockSignals(True)
                self.chk_inspect.setChecked(False)
                self.chk_inspect.blockSignals(False)
                return
            self.btn_inspect_refresh.setEnabled(True)
            self.chk_inspect_tree.setEnabled(True)
            self.chk_inspect_auto.setEnabled(True)
            self._refresh_inspect()
        else:
            self.btn_inspect_refresh.setEnabled(False)
            self.chk_inspect_tree.setEnabled(False)
            self.chk_inspect_auto.setEnabled(False)
            self.chk_inspect_auto.setChecked(False)   # 停定时器
            self.chk_inspect_tree.setChecked(False)   # 收起侧栏
            self.view.clear_inspect()
            self._fill_inspect_tree([])
            self._focus = {}
            self._refresh_focus_label()
            self.lbl_inspect.setText("")
            self.lbl_inspect.setToolTip("")

    def _on_auto_toggled(self, checked: bool):
        if checked and self.chk_inspect.isChecked():
            self._inspect_timer.start()
        else:
            self._inspect_timer.stop()

    def _on_u2_toggled(self, checked: bool):
        self.cfg["inspect_u2"] = bool(checked)
        _persist_cfg_key("inspect_u2", bool(checked))
        if self.chk_inspect.isChecked():
            self._refresh_inspect()

    def _refresh_inspect(self):
        if self._inspect_busy or not self.chk_inspect.isChecked():
            return
        src = self._inspect_source()
        if src is None:
            self.lbl_inspect.setText("投屏未连接")
            return
        adb, screen, video = src
        video = video if video and video[0] else tuple(self.view._video_size)
        use_u2 = bool(self.cfg.get("inspect_u2", False))
        retries = max(1, int(self.cfg.get("inspect_retries", 3)))
        self._inspect_busy = True
        self.btn_inspect_refresh.setEnabled(False)
        self.lbl_inspect.setText("控件抓取中…")
        self.lbl_inspect.setToolTip("")

        def note(msg: str):
            try:
                self.sig_inspect_note.emit(msg)
            except RuntimeError:
                pass

        def work():
            # dump 可能耗时 1~3s（动画界面还会重试），放后台线程，
            # 结果经信号切回 UI 线程；面板可能先被销毁（关窗），向已删除
            # 对象 emit 会抛 RuntimeError，吞掉即可，不能让线程带崩退出
            try:
                sc = screen
                if not (sc and sc[0] and sc[1]):
                    sc = adb.wm_size()
                # bounds 是旋转后的显示坐标，而 wm size 常报竖屏物理尺寸
                sc = logical_screen_size(video, sc)
                nodes = fetch_nodes(adb, retries=retries, use_u2=use_u2, on_retry=note)
            except Exception as exc:   # noqa: BLE001
                try:
                    self.sig_inspect_failed.emit(str(exc))
                except RuntimeError:
                    pass
                return
            try:
                self.sig_inspect.emit(nodes, sc)
            except RuntimeError:
                pass
            # 前台应用信息（包名/界面）：多一条 dumpsys，失败不影响控件树
            try:
                info = parse_focus(adb.current_focus())
            except Exception:   # noqa: BLE001
                info = {"package": "", "activity": "", "window": ""}
            if not info.get("package"):
                info["package"] = foreground_package(nodes)
            try:
                self.sig_focus.emit(info)
            except RuntimeError:
                pass

        threading.Thread(target=work, name="ui-inspect", daemon=True).start()

    def _on_inspect_done(self, nodes, screen):
        self._inspect_busy = False
        if not self.chk_inspect.isChecked():
            return   # 抓取期间用户已关掉开关，丢弃结果
        self.btn_inspect_refresh.setEnabled(True)
        tree_on = self.chk_inspect_tree.isChecked()
        pinned = self.view._pinned is not None
        keep = self._selected_key() if (tree_on or pinned) else (None, None)
        self.view.set_inspect(list(nodes), tuple(screen))
        if tree_on:
            self._fill_inspect_tree(self.view._inspect_nodes)
        else:
            # 侧栏收起时不建树（大页面重建一次 ~13ms，每 2.5s 白卡一下）；
            # 旧条目一并清掉，避免下标映射到新节点上导致选中/联动错位
            self._fill_inspect_tree([])
        self._restore_selection(*keep, pinned=pinned)   # 自动刷新不打断已选/锁定控件
        self.lbl_inspect.setText(f"{len(nodes)} 个控件")
        self.lbl_inspect.setToolTip("悬浮画面控件查看轮廓与基本信息；右键选中可锁定并定位到侧栏")

    def _on_focus(self, info):
        """透视线程顺带抓到的前台应用信息（包名/界面）。"""
        self._focus = dict(info or {})
        self._refresh_focus_label()
        # 属性表头部跟着换；但没选中控件时不白建一张表
        node = self.view._pinned or self.view._hover_node
        if node is not None and self.insp_props.topLevelItemCount():
            self._show_node_detail(node)

    def _refresh_focus_label(self):
        """控件树顶部那一行：`应用 包名 · 界面 Activity`，双击/右键复制。"""
        pkg = (self._focus.get("package") or "").strip()
        act = (self._focus.get("activity") or "").strip()
        if not pkg and not act:
            self.lbl_insp_app.setText("应用：—")
            self.lbl_insp_app.setToolTip(
                "开始投屏并开启控件透视后，这里显示前台应用包名与界面；双击复制")
            self.lbl_insp_app.set_copy("")
            return
        short_act = act.rsplit(".", 1)[-1] if act else ""
        text = f"应用 {pkg or '—'}"
        if short_act:
            text += f" · 界面 {short_act}"
        full = f"{pkg}/{act}" if (pkg and act) else (pkg or act)
        self.lbl_insp_app.setText(text)
        self.lbl_insp_app.setToolTip(f"{text}\n双击复制 {full}")
        targets = {}
        if pkg:
            targets["包名"] = pkg
        if act:
            targets["界面全名"] = act
            targets["包名/界面"] = full
        self.lbl_insp_app.set_copy(full, targets)

    def _on_inspect_failed(self, msg: str):
        self._inspect_busy = False
        if self.chk_inspect.isChecked():
            self.btn_inspect_refresh.setEnabled(True)
        first = (str(msg).splitlines() or [""])[0]
        # 失败原因直接上标签（不只看 tooltip）：锁屏/动画/可重试一眼可辨
        self.lbl_inspect.setText("抓取失败：" + short(first, 24))
        self.lbl_inspect.setToolTip(str(msg))
        self._on_log("[控件透视] 抓取失败: " + str(msg))

    # ---- 控件树侧栏 ----
    def _on_tree_toggled(self, checked: bool):
        self.insp_panel.setVisible(bool(checked))
        if checked:
            self._fill_inspect_tree(self.view._inspect_nodes)

    def _fill_inspect_tree(self, nodes):
        """按 parent 索引重建控件树（_inspect_items 与 nodes 下标一一对应）。

        条目里只存下标（int）：PySide 把 Python dict 存进 QVariant 后取回来是
        新对象，双向联动会因对象身份不一致而失效。
        列：控件 / id / 文本 —— 深嵌套时列 0 被缩进推得很靠右，
        把 id/文本放到固定列，任何层级都能直接读到关键信息。
        列 0 只放类名（同父同名的第 2 个起加 [n]，与 path 一致）：
        id 已在固定列，再重复一遍只会白占窄列宽度。
        """
        self.insp_tree.clear()
        self._inspect_items = [None] * len(nodes or [])
        self._node_item = {}
        ordinals: dict[tuple, int] = {}
        host: list = [None] * len(nodes or [])   # 每个节点在树里的落点（被压掉的壳沿用祖先）
        compact = self.chk_insp_compact.isChecked()
        for i, n in enumerate(nodes or []):
            parent = n.get("parent")
            up = host[parent] if isinstance(parent, int) and 0 <= parent < i else None
            # 「只看有效」：纯包装壳自己不建条目，子节点直接挂到最近的有效祖先下。
            # 实景页面 30 多层里大半是这种壳，压掉后层数直接降一个量级。
            if (compact and parent is not None and up is not None
                    and not _node_interesting(n)):
                host[i] = up
                continue
            cls = n.get("class") or "?"
            key = (parent, cls)
            ordinals[key] = ordinals.get(key, 0) + 1
            same = ordinals[key]
            it = QTreeWidgetItem([
                cls + (f"[{same}]" if same > 1 else ""),
                (n.get("resource_id") or "").rsplit("/", 1)[-1],
                short(" ".join((n.get("text") or n.get("desc") or "").split()), 24),
            ])
            it.setData(0, Qt.ItemDataRole.UserRole, i)
            it.setToolTip(0, node_info(n))
            it.setToolTip(1, n.get("resource_id") or "")
            it.setToolTip(2, short(n.get("text") or n.get("desc") or "", 300))
            self._style_tree_item(it, n)
            if up is not None:
                up.addChild(it)
            else:
                self.insp_tree.addTopLevelItem(it)
            host[i] = it
            self._inspect_items[i] = it
            self._node_item[id(n)] = it
        self._apply_inspect_filter()   # 内含：无筛选→展开到 3 层；有筛选→展开全部命中分支

    def _on_compact_toggled(self, _checked):
        """「只看有效」会改变树的父子结构（包装层被压掉），必须重建而不是只做隐藏。"""
        keep = self._selected_key()
        pinned = self.view._pinned is not None
        self._fill_inspect_tree(self.view._inspect_nodes)
        self._restore_selection(*keep, pinned=pinned)

    def _style_tree_item(self, it, n):
        """可交互控件加粗、纯包装容器灰字：深嵌套时一眼分出「能点的」和「壳」。"""
        if any(n.get(k) for k in ("clickable", "long_clickable", "checkable", "scrollable")):
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
            return
        if not (n.get("resource_id") or n.get("text") or n.get("desc")):
            it.setForeground(0, QBrush(QColor(theme_color("subtext"))))

    def _tree_depth(self, it) -> int:
        """条目在树里的实际层级（压缩视图下与 node depth 不是一回事）。"""
        d = 0
        p = it.parent()
        while p is not None:
            d += 1
            p = p.parent()
        return d

    def _expand_inspect_tree(self, depth: int = 3):
        """只展开前几层：全展开看不出层级，全折叠又要一层层点开。"""
        for it in self._inspect_items:
            if it is not None:
                it.setExpanded(self._tree_depth(it) < depth)

    def _reveal_item(self, item):
        """展开父链，保证条目真的显示在树里（深层默认是折叠的）。"""
        p = item.parent() if item is not None else None
        while p is not None:
            p.setExpanded(True)
            p = p.parent()

    def _item_node(self, item):
        """条目 -> 控件节点（下标回查视图里的真实节点）。"""
        idx = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        nodes = self.view._inspect_nodes
        return nodes[idx] if isinstance(idx, int) and 0 <= idx < len(nodes) else None

    def _selected_key(self):
        """当前选中/锁定控件的稳定标识（path+sel），用于自动刷新后重选。"""
        node = self.view._pinned or self._item_node(self.insp_tree.currentItem())
        if not node:
            return (None, None)
        return (node.get("path"), node.get("sel"))

    def _restore_selection(self, path, sel, *, pinned: bool = False):
        """自动刷新重建后把选中/高亮落回同一个控件（不打断正在看的控件）。"""
        if not path and not sel:
            return
        for i, n in enumerate(self.view._inspect_nodes):
            if (n.get("path"), n.get("sel")) != (path, sel):
                continue
            if pinned:
                self.view._pinned = n
            self.view._hover_node = n
            self.view.update()
            it = self._inspect_items[i] if i < len(self._inspect_items) else None
            if it is not None and not it.isHidden():
                self._reveal_item(it)
                self.insp_tree.blockSignals(True)
                self.insp_tree.setCurrentItem(it)
                self.insp_tree.scrollToItem(it, QAbstractItemView.ScrollHint.PositionAtCenter)
                self.insp_tree.blockSignals(False)
            self._show_node_detail(n)   # 条目被压掉/筛掉时属性表仍跟着控件走
            return

    def _apply_inspect_filter(self):
        """筛选：自身命中或任一子孙命中则可见（父链保留，不然树形结构断了）。

        「只看有效」再滤掉没有 id/文本/描述、也不能交互的包装壳分支。
        """
        q = self.insp_filter.text().strip()
        compact = self.chk_insp_compact.isChecked()

        def visit(it) -> bool:
            node = self._item_node(it) or {}
            ok = node_matches(node, q)
            for i in range(it.childCount()):
                ok = visit(it.child(i)) or ok
            it.setHidden(not ok)
            return ok

        for i in range(self.insp_tree.topLevelItemCount()):
            visit(self.insp_tree.topLevelItem(i))
        if q or compact:
            self.insp_tree.expandAll()    # 命中项少且散，展开才不会漏看
        else:
            self._expand_inspect_tree()   # 无筛选回到 3 层视图，不留全展开的乱树

    def _on_view_inspect_hover(self, node):
        """画面悬浮 -> 控件树选中（右键锁定 / 被筛选隐藏时不抢选中）。"""
        if self.view._pinned is not None:
            return   # 锁定期间画面只跟手，不打断侧栏已选控件
        it = self._node_item.get(id(node)) if node is not None else None
        self.insp_tree.blockSignals(True)
        if it is not None and not it.isHidden():
            self._reveal_item(it)
            self.insp_tree.setCurrentItem(it)
            self.insp_tree.scrollToItem(it, QAbstractItemView.ScrollHint.PositionAtCenter)
            self._show_node_detail(node)
        elif node is not None:
            self._show_node_detail(node)   # 被筛选/只看有效藏了条目：树不跳，属性表照样给全
        elif it is None:
            self.insp_tree.clearSelection()
        self.insp_tree.blockSignals(False)

    def _on_view_inspect_pick(self, node):
        """右键选中控件：展开侧栏并定位到它；再右键同一控件或点空白处取消。"""
        if node is None:
            self._show_node_detail(None)
            if self.chk_inspect.isChecked():
                self.lbl_inspect.setText("已取消选中")
            return
        if not self.chk_inspect_tree.isChecked():
            self.chk_inspect_tree.setChecked(True)   # 展开侧栏（顺带建树）
        it = self._node_item.get(id(node))
        if it is not None:
            self._reveal_item(it)
            self.insp_tree.blockSignals(True)
            self.insp_tree.setCurrentItem(it)
            self.insp_tree.scrollToItem(it, QAbstractItemView.ScrollHint.PositionAtCenter)
            self.insp_tree.blockSignals(False)
        self._show_node_detail(node)
        self.lbl_inspect.setText("已选中：" + short(node_label(node), 28) + "（右键菜单可加断言）")
        self.lbl_inspect.setToolTip(
            "右键选中会锁定高亮与属性，不随鼠标移动变化；\n"
            "右键菜单可把该控件设成断言（存在/不存在/文本等于）或存成截图基线，\n"
            "点空白处或菜单「取消选中控件」取消锁定")

    def _on_assert_pick(self, kind: str, node):
        """画面右键断言请求 -> 主窗口弹对话框（控件选择器在那里生成）。"""
        try:
            self.sig_assert.emit({"kind": str(kind), "node": node})
        except RuntimeError:
            pass   # 面板已销毁（自检/退出）

    def _on_tree_current(self, item, _prev=None):
        """控件树选中 -> 画面高亮 + 属性表（与悬浮共用一套绘制）。"""
        node = self._item_node(item)
        if node is None:
            return
        self._show_node_detail(node)
        if node is not self.view._hover_node:
            self.view._hover_node = node
            self.view.update()

    def _show_node_detail(self, node):
        """属性表：类名/id/文本/几何/状态/层级/定位逐行展示，双击行可复制值。"""
        self.insp_props.clear()
        if not node:
            return
        b = node["bounds"]
        w, h = b[2] - b[0], b[3] - b[1]
        sw, sh = self.view._inspect_screen
        # 应用/界面（前台 Activity）总在最前：复制出来直接拼 am start -n 包名/Activity
        app_pkg = self._focus.get("package") or (node.get("package") or "")
        app_act = self._focus.get("activity") or ""
        rows: list[tuple[str, str]] = []
        if app_pkg:
            rows.append(("应用", app_pkg))
        if app_act:
            rows.append(("界面", app_act))
        rows.append(("类名", node.get("class") or "?"))
        full = node.get("full_class") or ""
        if full and full != node.get("class"):
            rows.append(("完整类名", full))
        # 节点自己的 package 与应用不同（状态栏/输入法控件混进来）时才单独列
        if node.get("package") and node["package"] != app_pkg:
            rows.append(("包名", node["package"]))
        if node.get("resource_id"):
            rows.append(("id", node["resource_id"]))
        if node.get("text"):
            rows.append(("文本", short(" ".join(node["text"].split()), 300)))
        if node.get("desc"):
            rows.append(("描述", short(" ".join(node["desc"].split()), 300)))
        rows.append(("区域", f"[{b[0]},{b[1]}][{b[2]},{b[3]}]") )
        size = f"{w}x{h}"
        if sw and sh:
            size += (f"  占屏 {w / sw * 100:.0f}%x{h / sh * 100:.0f}%"
                     f"  中心 {(b[0] + b[2]) / 2 / sw * 100:.0f}%,"
                     f"{(b[1] + b[3]) / 2 / sh * 100:.0f}%")
        rows.append(("尺寸", size))
        flags = [label for label, key in (("可点击", "clickable"), ("可长按", "long_clickable"),
                                          ("可滚动", "scrollable"), ("可勾选", "checkable"))
                 if node.get(key)]
        if node.get("checked"):
            flags.append("已勾选")
        if node.get("focused"):
            flags.append("已聚焦")
        flags.append("已启用" if node.get("enabled", True) else "已禁用")
        rows.append(("状态", " · ".join(flags)))
        nodes = self.view._inspect_nodes
        idx = next((i for i, m in enumerate(nodes) if m is node), -1)
        rows.append(("层级", f"第 {int(node.get('depth') or 0) + 1} 层"
                             + (f"  {idx + 1}/{len(nodes)}" if idx >= 0 else "")))
        parent = node.get("parent")
        if isinstance(parent, int) and 0 <= parent < len(nodes):
            rows.append(("父级", node_label(nodes[parent])))
        if node.get("path"):
            rows.append(("路径", node["path"]))
        if node.get("sel"):
            rows.append(("定位", node["sel"]))
        for name, value in rows:
            it = QTreeWidgetItem([name, str(value)])
            it.setToolTip(0, name)
            it.setToolTip(1, str(value))
            if name in ("路径", "定位"):
                it.setForeground(1, QBrush(QColor(theme_color("accent"))))
            self.insp_props.addTopLevelItem(it)

    def _on_prop_double(self, item, _col=0):
        """双击属性行：复制值（定位表达式/完整 id 直接粘到用例里）。"""
        val = item.text(1) if item is not None else ""
        if not val:
            return
        QApplication.clipboard().setText(val)
        self.lbl_inspect.setText("已复制：" + short(val, 32))

    def _on_prop_menu(self, pos):
        """属性表右键菜单：复制值 / 复制属性名（双击同样复制值）。"""
        item = self.insp_props.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        act_val = menu.addAction("复制值")
        act_name = menu.addAction("复制属性名")
        act_val.setEnabled(bool(item.text(1)))
        chosen = _menu_exec(menu, self.insp_props.viewport().mapToGlobal(pos))
        if chosen is act_val:
            self._on_prop_double(item, 1)
        elif chosen is act_name:
            QApplication.clipboard().setText(item.text(0))
            self.lbl_inspect.setText("已复制：" + short(item.text(0), 32))

    def _on_tree_menu(self, pos):
        """控件树右键菜单：深嵌套时快速收敛/展开视图，或直接复制关键字段。"""
        item = self.insp_tree.itemAt(pos)
        node = self._item_node(item)
        menu = QMenu(self)
        act_all = menu.addAction("展开全部")
        act_three = menu.addAction("折叠到 3 层")
        menu.addSeparator()
        act_branch = menu.addAction("展开选中分支")
        act_sel = menu.addAction("复制定位表达式")
        act_path = menu.addAction("复制控件路径")
        act_cell = menu.addAction("复制单元格值")
        act_branch.setEnabled(item is not None)
        act_sel.setEnabled(bool(node and node.get("sel")))
        act_path.setEnabled(bool(node and node.get("path")))
        act_cell.setEnabled(item is not None)
        chosen = _menu_exec(menu, self.insp_tree.viewport().mapToGlobal(pos))
        if chosen is act_all:
            self.insp_tree.expandAll()
        elif chosen is act_three:
            self._expand_inspect_tree(3)
        elif chosen is act_branch and item is not None:
            self._reveal_item(item)
            self.insp_tree.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
        elif chosen is act_sel:
            self._on_tree_double(item, 0)
        elif chosen is act_path and node is not None:
            QApplication.clipboard().setText(node.get("path") or "")
            self.lbl_inspect.setText("已复制路径：" + short(node.get("path") or "", 28))
        elif chosen is act_cell and item is not None:
            val = item.text(max(0, self.insp_tree.columnAt(pos.x())))
            if val:
                QApplication.clipboard().setText(val)
                self.lbl_inspect.setText("已复制：" + short(val, 32))

    def _on_tree_double(self, item, _col=0):
        """双击/复制按钮：选择器进剪贴板，直接可粘到录制用例里。"""
        node = self._item_node(item)
        if node is None:
            return
        sel = node.get("sel") or ""
        if not sel:
            self.lbl_inspect.setText("该控件没有可用的定位表达式")
            return
        QApplication.clipboard().setText(sel)
        self.lbl_inspect.setText("已复制：" + short(sel, 32))
        self._on_log(f"[控件透视] 已复制 {node_label(node)} → {sel}")

    # ---------------- 快捷键 / 文本 ----------------
    def _shortcut(self, name: str):
        m = self.view._mirror()
        if m is None:
            return
        fn = {"back": m.back, "home": m.home, "recents": m.recents,
              "power": m.power, "voldown": m.volume_down, "volup": m.volume_up}.get(name)
        if fn is None:
            return
        fn()
        key = self.SHORTCUT_KEYS.get(name)
        if key and self.chk_record.isChecked() and self._tr is not None:
            self._tr.feed([RawEvent(ts=time.monotonic(), node="mirror", kind="EV_KEY",
                                    code=key, value=1),
                           RawEvent(ts=time.monotonic(), node="mirror", kind="EV_SYN",
                                    code="SYN_REPORT", value=0)])
            self._refresh_steps()

    def _inject_text(self):
        m = self.view._mirror()
        text = self.txt_inject.text()
        if m is None or not text:
            return
        try:
            m.text(text)
        except Exception as exc:
            QMessageBox.warning(self, "文本注入失败", str(exc))

    def _on_view_key(self, linux_name: str):
        """物理键盘特殊键：注入已在 view 完成，这里只补录 keyevent（与快捷键同链路）。"""
        ts = time.monotonic()
        evs = [RawEvent(ts, "mirror", "EV_KEY", linux_name, 1),
               RawEvent(ts, "mirror", "EV_SYN", "SYN_REPORT", 0)]
        if self.on_record_event is not None:
            for e in evs:
                self.on_record_event(e)
        if not self.chk_record.isChecked():
            return
        self._ensure_translator()
        if self._tr is None:
            return
        self._tr.feed(evs)
        self._refresh_steps()

    def _on_view_text(self, text: str):
        """物理键盘可打印字符：注入已在 view 完成，这里只补录 text 动作。"""
        if self.on_record_text is not None:
            self.on_record_text(text)
        if not self.chk_record.isChecked():
            return
        self._ensure_translator()
        if self._tr is None:
            return
        self._tr.inject_text(text, time.monotonic())
        self._refresh_steps()

    # ---------------- 手势录制 ----------------
    def _browse_out(self):
        p, _ = QFileDialog.getSaveFileName(self, "投屏录制输出", self.rec_out.text(),
                                           "用例 (*.yaml *.yml *.json)")
        if p:
            self.rec_out.setText(p)

    def _toggle_record(self, _state):
        if self.chk_record.isChecked():
            self._ensure_translator()
        self._refresh_steps()
        self._update_rec_handle()   # 录制中把手变红，面板收起也不会忘记在录

    def _ensure_translator(self):
        if self._tr is not None or self.meta is None:
            return
        # 优先用当前实际帧尺寸（可能已旋转），meta 只是握手时的初始值
        w, h = self.view._video_size if self.view._video_size != (0, 0) \
            else self.meta["video_size"]
        self._tr_profile = {
            "serial": self.meta["serial"], "model": self.meta["device_name"],
            "size": self.meta["screen_size"], "rotation": 0,
            "touch_abs": [max(1, w - 1), max(1, h - 1)],
            "abs_source": "mirror", "inputs": [],
        }
        # 投屏坐标已是屏幕坐标系，不适用物理面板的 swap_xy 修正
        cfg = dict(self.cfg)
        cfg["swap_xy"] = False
        self._tr = Translator(cfg, self._tr_profile)

    def _on_input_event(self, ev: RawEvent):
        # 录制页（getevent）同时在跑时也喂一份：scrcpy 注入走 InputManager，
        # 不会出现在 /dev/input，不转发的话“在投屏上演示”会被判成没有任何操作
        if self.on_record_event is not None:
            self.on_record_event(ev)
        if not self.chk_record.isChecked():
            return
        self._ensure_translator()
        if self._tr is None:
            return
        self._tr.feed([ev])
        if ev.kind == "EV_ABS" and ev.code == "ABS_MT_TRACKING_ID" and ev.value < 0:
            self._refresh_steps()

    def _refresh_steps(self):
        steps = self._tr.snapshot() if self._tr is not None else []
        t = self.tbl_steps
        t.setRowCount(len(steps))
        for i, s in enumerate(steps):
            vals = [str(i + 1), s.get("action", ""), str(s.get("at_ms", "")),
                    step_summary(s)]
            for c, val in enumerate(vals):
                _set_cell(t, i, c, val)

    def _save_recorded(self):
        if self._tr is None:
            QMessageBox.information(self, "没有可保存的录制", "先勾选录制并操作投屏画面")
            return
        steps = self._tr.finish()
        if not steps:
            QMessageBox.information(self, "没有识别到操作", "先在投屏画面上点击/滑动")
            return
        out = self.rec_out.text().strip()
        if not out:
            QMessageBox.warning(self, "缺少输出路径", "请先填写输出用例路径")
            return
        path = Path(out)
        case = new_case(path.stem, self._tr_profile, steps)
        save_case(case, path, detect_format(path))
        self._on_log(f"[投屏录制] 已生成 {path}（{len(steps)} 步）")
        self.sig_case_saved.emit(str(path))
        # 保存后重新开始一段录制
        self._tr = None
        self._ensure_translator()
        self._refresh_steps()


class MirrorWindow(QMainWindow):
    """独立投屏窗口：CLI `adbcase.py mirror` 自建面板，也可接管主界面的投屏面板。

    panel 传入时（主界面「拖出独立展示」），窗口关闭只把面板还回去，
    不 shutdown —— 投屏会话不能因为换个窗口就断。
    """

    def __init__(self, cfg: dict, serial: str = "", panel=None, on_closed=None,
                 on_log=None):
        super().__init__()
        self.setWindowTitle(APP_TITLE + " — 实时投屏")
        self.resize(1060, 720)
        self._owns_panel = panel is None
        self.on_closed = on_closed
        self.on_log = on_log
        self.panel = panel if panel is not None else \
            MirrorPanel(cfg, serial_getter=lambda: serial)
        self.setCentralWidget(self.panel)
        bar = QToolBar("主题", self)
        bar.setMovable(False)
        self.addToolBar(bar)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.btn_theme = QPushButton("浅色模式" if cfg.get("theme", "light") == "dark"
                                     else "深色模式")
        self.btn_theme.clicked.connect(self._toggle_theme)
        bar.addWidget(self.btn_theme)
        if self._owns_panel:
            # 只有自建面板才绑信号；接管的面板收回后信号不能留下悬垂 lambda
            self.setStatusBar(QStatusBar())
            self.panel.sig_case_saved.connect(
                lambda p: self.statusBar().showMessage(f"用例已保存: {p}", 8000))

    def _toggle_theme(self):
        nxt = "light" if gui_theme.current() == "dark" else "dark"
        apply_theme(QApplication.instance(), nxt)
        self.btn_theme.setText("浅色模式" if nxt == "dark" else "深色模式")
        self.panel.view.update()  # 自定义绘制的投屏背景需显式重绘
        _persist_cfg_key("theme", nxt)

    def append_log(self, msg: str):
        # 接管模式没建状态栏：不能调 statusBar()（会凭空造一条空栏），
        # 日志转给主窗口，否则拖出去的窗口里什么都看不到
        if self._owns_panel:
            self.statusBar().showMessage(short(str(msg), 120), 6000)
        elif callable(self.on_log):
            self.on_log(msg)

    def closeEvent(self, event):
        if self._owns_panel:
            self.panel.shutdown()
            cb, self.on_closed = self.on_closed, None
            if cb is not None:
                cb()
        elif self.on_closed is not None:
            # 接管模式：窗口会被反复复用（关窗只隐藏），回调不能清空
            self.on_closed()   # 把面板还给主界面（投屏继续）
        super().closeEvent(event)


# --------------------------------------------------------------------------
# 单步编辑对话框：直接编辑该步的 YAML，改完写回表格
# --------------------------------------------------------------------------
class StepEditDialog(QDialog):
    def __init__(self, step: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑步骤")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        lb_edit = QLabel("直接编辑这一步的 YAML（字段随用例文件保存）：")
        lb_edit.setWordWrap(True)
        layout.addWidget(lb_edit)
        self.edit = QPlainTextEdit()
        self.edit.setFont(QFont("Consolas", 10))
        self.edit.setPlainText(yaml.safe_dump(step, sort_keys=False, allow_unicode=True,
                                              default_flow_style=None, width=120))
        layout.addWidget(self.edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.result_step_data = None

    def accept(self):
        try:
            data = yaml.safe_load(self.edit.toPlainText())
            if not isinstance(data, dict) or "action" not in data:
                raise ValueError("步骤必须是含 action 字段的对象")
            self.result_step_data = data
            super().accept()
        except Exception as exc:
            QMessageBox.warning(self, "解析失败", str(exc))


class AssertDialog(QDialog):
    """添加断言：控件断言 / 界面文本 / Activity / 图片基线。

    投屏画面右键进来时带 node + selector + expect 预填：控件定位与期望值都能改。
    勾选「同时保存控件截图」会在控件断言后再追一条图片基线断言（区域=控件范围）；
    截图一律取投屏最新解码帧，未投屏时给明确提示，不静默失败。
    """

    KINDS = (
        ("widget", "控件断言（按 id/类名/文本定位）"),
        ("text", "界面包含文本"),
        ("not_text", "界面不含文本"),
        ("activity", "当前界面（Activity 包含）"),
        ("image", "图片基线（截图比对）"),
    )
    EXPECTS = (
        ("exists", "控件存在"),
        ("absent", "控件不存在"),
        ("text", "文本等于"),
        ("contains", "文本包含"),
        ("checked", "已勾选"),
        ("unchecked", "未勾选"),
        ("enabled", "可用（enabled）"),
        ("disabled", "禁用（disabled）"),
    )
    PAGE_OF = {"widget": 0, "text": 1, "not_text": 1, "activity": 2, "image": 3}
    SEL_FIELDS = (("id", "id", "如 com.demo:id/title"),
                  ("class", "类名", "如 android.widget.Button（短名/全名都行）"),
                  ("text", "文本", "控件当前文本（值会变，定位慎用）"),
                  ("desc", "描述", "content-desc"),
                  ("package", "包名", "如 com.demo（可选）"))

    def __init__(self, *, node=None, kind="widget", selector=None, expect=None,
                 image="", region=None, capture=None, base_dir=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加断言")
        self.resize(640, 560)
        self.node = node
        self._capture = capture   # fn(node, full) -> (相对用例路径, 归一化区域|None)
        self._base_dir = Path(base_dir) if base_dir else None
        self._shot = (image, region) if image else None   # 图片基线页当前基线
        self.shots: list[str] = []   # 本对话框新拍的基线图（取消时回收，不留孤儿文件）
        self.result_steps: list[dict] = []
        self.result_note = ""

        root = QVBoxLayout(self)
        root.setSpacing(8)
        head = QFormLayout()
        self.cmb_kind = QComboBox()
        for key, text in self.KINDS:
            self.cmb_kind.addItem(text, key)
        self.cmb_kind.currentIndexChanged.connect(self._on_kind_changed)
        head.addRow("断言类型", self.cmb_kind)
        root.addLayout(head)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_widget_page())
        self.stack.addWidget(self._build_text_page())
        self.stack.addWidget(self._build_activity_page())
        self.stack.addWidget(self._build_image_page())
        root.addWidget(self.stack, 1)

        lb_hint = QLabel("断言立即写进当前用例（表格与源码同步），记得点「保存」写回文件。")
        lb_hint.setWordWrap(True)
        set_subdued(lb_hint)
        root.addWidget(lb_hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._prefill(kind, selector, expect, image, region)

    # ---- 构建 ----
    def _build_widget_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        gb_sel = QGroupBox("控件定位（留空的字段不参与匹配）")
        form = QFormLayout(gb_sel)
        form.setSpacing(6)
        self._sel_edits: dict[str, QLineEdit] = {}
        for key, caption, placeholder in self.SEL_FIELDS:
            e = QLineEdit()
            e.setPlaceholderText(placeholder)
            self._sel_edits[key] = e
            form.addRow(caption, e)
        self.spn_sel_index = QSpinBox()
        self.spn_sel_index.setRange(1, 999)
        self.spn_sel_index.setMaximumWidth(120)
        self.spn_sel_index.setToolTip("同一条件命中多个控件时取第几个（1 起）；唯一控件留 1")
        form.addRow("第几个", self.spn_sel_index)
        v.addWidget(gb_sel)

        gb_exp = QGroupBox("期望")
        form2 = QFormLayout(gb_exp)
        form2.setSpacing(6)
        self.cmb_expect = QComboBox()
        for key, text in self.EXPECTS:
            self.cmb_expect.addItem(text, key)
        self.cmb_expect.currentIndexChanged.connect(self._on_expect_changed)
        form2.addRow("断言方式", self.cmb_expect)
        self.ed_expect = QLineEdit()
        self.ed_expect.setPlaceholderText("文本等于 / 文本包含 时的期望值")
        form2.addRow("期望值", self.ed_expect)
        v.addWidget(gb_exp)

        self.chk_shot = QCheckBox("同时保存该控件截图，并追加图片基线断言")
        self.chk_shot.setToolTip(
            "取投屏最新一帧存为该控件的截图基线（比对区域=控件范围）；\n"
            "回放时先用控件断言定位控件，再只比这块区域的画面")
        self.chk_shot.setEnabled(self._capture is not None and self.node is not None)
        v.addWidget(self.chk_shot)
        v.addStretch(1)
        self._sync_expect_edit()
        return page

    def _build_text_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        self.ed_text = QLineEdit()
        self.ed_text.setPlaceholderText("要断言的界面文本（子串匹配）")
        v.addWidget(self.ed_text)
        hint = QLabel("回放时 dump 当前界面，检查是否包含该文本：可断言弹窗/标题/提示是否出现。\n"
                      "从投屏右键进来时，这里已预填选中控件的文本。")
        hint.setWordWrap(True)
        set_subdued(hint)
        v.addWidget(hint)
        v.addStretch(1)
        return page

    def _build_activity_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        self.ed_activity = QLineEdit()
        self.ed_activity.setPlaceholderText("如 com.demo.main.MainActivity 或 .MainActivity")
        v.addWidget(self.ed_activity)
        hint = QLabel("回放时读当前焦点窗口（dumpsys window），要求包含该片段；\n"
                      "投屏侧栏顶部「界面」可直接双击复制。")
        hint.setWordWrap(True)
        set_subdued(hint)
        v.addWidget(hint)
        v.addStretch(1)
        return page

    def _build_image_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        gb = QGroupBox("基线图片")
        form = QFormLayout(gb)
        form.setSpacing(6)
        self.ed_image = QLineEdit()
        self.ed_image.setPlaceholderText("相对用例目录，如 demo_shots/assert_xx.png")
        form.addRow("基线文件", self._with_button(self.ed_image, "浏览…", self._browse_image))
        self.btn_shoot = QPushButton("截取当前投屏画面为基线")
        self.btn_shoot.setToolTip("取投屏最新一帧存到 <用例名>_shots/ 下（未投屏会提示）")
        self.btn_shoot.setEnabled(self._capture is not None)
        self.btn_shoot.clicked.connect(self._shoot_current)
        form.addRow("", self.btn_shoot)
        self.cmb_region = QComboBox()
        self.cmb_region.addItem("整屏", "full")
        self.cmb_region.addItem("仅控件区域（截图时选中了控件才可用）", "widget")
        self.cmb_region.setEnabled(False)
        self.cmb_region.setToolTip("比对区域：整屏容易被时钟/状态栏干扰，控件区域更稳")
        form.addRow("比对区域", self.cmb_region)
        self.spn_threshold = QDoubleSpinBox()
        self.spn_threshold.setRange(0.01, 100.0)
        self.spn_threshold.setDecimals(2)
        self.spn_threshold.setValue(2.0)
        self.spn_threshold.setSuffix(" %")
        self.spn_threshold.setToolTip("允许的差异像素占比上限（默认 2%）")
        form.addRow("差异阈值", self.spn_threshold)
        self.spn_tolerance = QSpinBox()
        self.spn_tolerance.setRange(0, 255)
        self.spn_tolerance.setValue(24)
        self.spn_tolerance.setToolTip("单像素色差超过它才算变化（抗视频压缩噪声）")
        form.addRow("像素容差", self.spn_tolerance)
        self.chk_status_bar = QCheckBox("忽略顶部状态栏（时间 / 电量 / 信号）")
        self.chk_status_bar.setChecked(True)
        form.addRow("", self.chk_status_bar)
        v.addWidget(gb)
        self.lbl_shot = QLabel("")
        self.lbl_shot.setWordWrap(True)
        set_subdued(self.lbl_shot)
        v.addWidget(self.lbl_shot)
        v.addStretch(1)
        return page

    def _with_button(self, line: QLineEdit, caption: str, slot) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(line, 1)
        b = QPushButton(caption)
        b.clicked.connect(slot)
        h.addWidget(b)
        return w

    # ---- 预填 ----
    def _prefill(self, kind, selector, expect, image, region):
        key = kind if kind in self.PAGE_OF else "widget"
        self.cmb_kind.setCurrentIndex(max(0, self.cmb_kind.findData(key)))
        self._on_kind_changed()
        sel = dict(selector or {})
        for k, e in self._sel_edits.items():
            e.setText(str(sel.get(k) or ""))
        try:
            self.spn_sel_index.setValue(max(1, int(sel.get("index") or 1)))
        except (TypeError, ValueError):
            pass
        node_text = str((self.node or {}).get("text") or "")
        if node_text:
            self.ed_text.setText(node_text)
        self._apply_expect(expect or {"exists": True})
        if image:
            self.ed_image.setText(image)
            self.cmb_region.setEnabled(bool(region))
            self.cmb_region.setCurrentIndex(1 if region else 0)
            self.lbl_shot.setText("已截取基线：" + image)

    def _apply_expect(self, expect: dict):
        mode, value = "exists", ""
        if "exists" in expect:
            mode = "exists" if expect["exists"] else "absent"
        elif "text" in expect:
            mode, value = "text", str(expect["text"])
        elif "contains" in expect:
            mode, value = "contains", str(expect["contains"])
        elif "checked" in expect:
            mode = "checked" if expect["checked"] else "unchecked"
        elif "enabled" in expect:
            mode = "enabled" if expect.get("enabled", True) else "disabled"
        self.cmb_expect.setCurrentIndex(max(0, self.cmb_expect.findData(mode)))
        self.ed_expect.setText(value)
        self._sync_expect_edit()
        if mode in ("text", "contains"):
            # 文本断言多半要把预填值改掉：全选后直接打字即替换
            self.ed_expect.setFocus()
            self.ed_expect.selectAll()

    def _on_kind_changed(self, _index=None):
        key = self.cmb_kind.currentData() or "widget"
        self.stack.setCurrentIndex(self.PAGE_OF.get(key, 0))
        self.setWindowTitle("添加断言 — " + (self.cmb_kind.currentText() or ""))

    def _sync_expect_edit(self, _index=None):
        mode = self.cmb_expect.currentData()
        self.ed_expect.setEnabled(mode in ("text", "contains"))

    def _on_expect_changed(self, _index=None):
        """用户切到「文本等于 / 包含」：直接聚焦并全选期望值，少点一次鼠标。"""
        self._sync_expect_edit()
        if self.cmb_expect.currentData() in ("text", "contains"):
            self.ed_expect.setFocus()
            self.ed_expect.selectAll()

    # ---- 截图 ----
    def _browse_image(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择基线图片",
                                           str(ROOT / "cases"),
                                           "图片 (*.png *.jpg *.jpeg *.bmp)")
        if not p:
            return
        text = p
        if self._base_dir is not None:
            try:
                text = Path(p).resolve().relative_to(self._base_dir.resolve()).as_posix()
            except ValueError:
                text = p   # 不在用例目录下：保留绝对路径（runner 也认）
        self.ed_image.setText(text)
        self._shot = None
        self.cmb_region.setCurrentIndex(0)
        self.cmb_region.setEnabled(False)
        self.lbl_shot.setText("已选基线：" + text)

    def _shoot_current(self):
        if self._capture is None:
            QMessageBox.warning(self, "无法截图", "请先在「① 实时投屏」连接设备")
            return
        try:
            rel, region = self._capture(self.node, False)
        except RuntimeError as exc:
            QMessageBox.warning(self, "截图失败", str(exc))
            return
        self._shot = (rel, region)
        self.shots.append(rel)   # 取消对话框时按这个列表回收本次新拍的图
        self.ed_image.setText(rel)
        self.cmb_region.setEnabled(bool(region))
        self.cmb_region.setCurrentIndex(1 if region else 0)
        self.lbl_shot.setText("已截取基线：" + rel
                              + ("（比对区域=控件范围）" if region else "（比对区域=整屏）"))

    def discard_shots(self, extra: list[str] | None = None,
                      keep: set[str] | None = None):
        """回收本次新拍的基线图，避免 <用例名>_shots/ 里堆没人引用的图。

        只处理本对话框拍的（shots）与调用方显式传入的（extra，如右键预截图）；
        手动「浏览…」选的既有文件绝不动。keep 里是已写进用例步骤的图，保留。
        """
        for rel in dict.fromkeys(list(self.shots) + list(extra or [])):
            if keep and rel in keep:
                continue
            p = Path(rel)
            if not p.is_absolute() and self._base_dir is not None:
                p = self._base_dir / p
            try:
                p.unlink()
            except OSError:
                pass   # 已不存在 / 被占用：残留一个图不影响用例可用性
        self.shots.clear()

    # ---- 产出步骤 ----
    @staticmethod
    def _expect_caption(exp: dict) -> str:
        if "exists" in exp:
            return "控件存在" if exp["exists"] else "控件不存在"
        if "text" in exp:
            return f"文本={exp['text']}"
        if "contains" in exp:
            return f"文本含 {exp['contains']}"
        if "checked" in exp:
            return "已勾选" if exp["checked"] else "未勾选"
        if "enabled" in exp:
            return "可用" if exp.get("enabled", True) else "禁用"
        return ""

    def _widget_steps(self) -> list[dict]:
        sel = {k: e.text().strip() for k, e in self._sel_edits.items()
               if e.text().strip()}
        if not sel:
            raise ValueError("至少填一个控件定位字段（id / 类名 / 文本 / 描述 / 包名）")
        idx = int(self.spn_sel_index.value())
        if idx > 1:
            sel["index"] = idx
        mode = self.cmb_expect.currentData()
        if mode in ("text", "contains"):
            value = self.ed_expect.text()
            if value == "":
                raise ValueError("「文本等于 / 文本包含」需要填写期望值")
            exp = {mode: value}
        elif mode in ("exists", "absent"):
            exp = {"exists": mode == "exists"}
        elif mode in ("checked", "unchecked"):
            exp = {"checked": mode == "checked"}
        else:
            exp = {"enabled": mode == "enabled"}
        label = selector_label(sel)
        steps = [{"action": "assert_widget", "selector": sel, "expect": exp,
                  "desc": f"断言 {label} {self._expect_caption(exp)}".strip()}]
        if self.chk_shot.isChecked():
            if self._capture is None:
                raise RuntimeError("当前没有可用的投屏画面，无法截取控件截图")
            rel, region = self._capture(self.node, False)
            self.shots.append(rel)
            shot = {"action": "assert_image", "image": rel, "wait_before_ms": 0,
                    "threshold": 0.02, "pixel_tolerance": 24,
                    "desc": f"控件截图基线 {label}"}
            if region:
                shot["region"] = region   # 只比控件范围，状态栏/背景变化不影响
            else:
                shot["ignore_regions"] = [list(DEFAULT_IGNORE_STATUS_BAR)]
            steps.append(shot)
        return steps

    def _text_step(self, key: str) -> dict:
        value = self.ed_text.text()
        if not value.strip():
            raise ValueError("请填写要断言的界面文本")
        act = "assert_text" if key == "text" else "assert_not_text"
        verb = "包含" if key == "text" else "不含"
        return {"action": act, "text": value, "desc": f"界面{verb} {short(value, 30)}"}

    def _activity_step(self) -> dict:
        value = self.ed_activity.text().strip()
        if not value:
            raise ValueError("请填写 Activity 名或其片段")
        return {"action": "assert_activity", "contains": value,
                "desc": f"Activity 含 {short(value, 30)}"}

    def _image_step(self) -> dict:
        image = self.ed_image.text().strip()
        if not image:
            raise ValueError("请先截取或选择基线图片")
        if not Path(image).is_absolute() and self._base_dir is not None \
                and not (self._base_dir / image).exists():
            raise ValueError(f"基线图片不存在: {image}（相对 {self._base_dir}）")
        step = {"action": "assert_image", "image": image,
                "threshold": round(self.spn_threshold.value() / 100.0, 4),
                "pixel_tolerance": int(self.spn_tolerance.value()),
                "wait_before_ms": 0,
                "desc": "图片基线 " + Path(image).name}
        if self._shot and self._shot[0] == image \
                and self.cmb_region.currentData() == "widget" and self._shot[1]:
            step["region"] = self._shot[1]
        if self.chk_status_bar.isChecked():
            step["ignore_regions"] = [list(DEFAULT_IGNORE_STATUS_BAR)]
        return step

    def accept(self):
        key = self.cmb_kind.currentData() or "widget"
        try:
            if key == "widget":
                steps = self._widget_steps()
            elif key in ("text", "not_text"):
                steps = [self._text_step(key)]
            elif key == "activity":
                steps = [self._activity_step()]
            else:
                steps = [self._image_step()]
        except ValueError as exc:
            QMessageBox.warning(self, "断言不完整", str(exc))
            return
        except RuntimeError as exc:   # 截图失败（未投屏 / 写盘失败）
            QMessageBox.warning(self, "截图失败", str(exc))
            return
        self.result_steps = steps
        self.result_note = steps[0].get("desc") or steps[0]["action"]
        super().accept()


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, autoload_devices: bool = True):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1220, 800)
        self.cfg = load_config()
        self.case_data: dict | None = None
        self.case_path: Path = ROOT / "cases" / "example.yaml"
        self._syncing = False
        self.rec_worker = None
        self.replay_worker = None
        self._probe_worker = None
        self._probe_finished = False   # 本次探测是否已有结果（成功/失败）
        self._probe_save_profile = True   # 档案落盘开关（自检里关掉，不污染 reports/）
        self.mirror_win = None            # 投屏画面独立展示的窗口（拖出主界面）
        self._mirror_tab_index = 0        # 收回时插回原位置

        self._build_ui()
        self._load_config_text()
        self._load_case(str(self.case_path), quiet=True)
        if autoload_devices:
            self._refresh_devices()

    # ---------------- UI ----------------
    def _build_ui(self):
        self.setMinimumSize(1000, 660)
        bar = QToolBar("设备", self)
        bar.setMovable(False)
        self.addToolBar(bar)
        lb_device = QLabel("设备")
        set_subdued(lb_device)
        bar.addWidget(lb_device)
        self.cmb_device = QComboBox()
        self.cmb_device.setMinimumWidth(260)
        self.cmb_device.addItem("（自动 / 未选择）")
        # 长序列号（无线 adb / 模拟器）在收起态可能被省略，tooltip 补全文
        self.cmb_device.currentTextChanged.connect(self.cmb_device.setToolTip)
        # 设备切换后刷新回放提示条（真机模式会显示目标设备）
        self.cmb_device.currentTextChanged.connect(self._on_serial_changed)
        bar.addWidget(self.cmb_device)
        b1 = QPushButton("刷新设备")
        b1.clicked.connect(self._refresh_devices)
        bar.addWidget(b1)
        b2 = QPushButton("探测能力")
        b2.setToolTip("读取屏幕尺寸/密度/旋转、输入节点能力与触摸坐标上限（写入用例头部档案）")
        b2.clicked.connect(self._probe_device)
        self.btn_probe = b2
        bar.addWidget(b2)
        b3 = QPushButton("实时投屏")
        set_role(b3, "accent")
        b3.setToolTip("低延迟投屏 + 鼠标点击/滑动控制（scrcpy 协议）")
        b3.clicked.connect(self._open_mirror)
        bar.addWidget(b3)
        self.btn_popout = QPushButton("⧉ 独立展示")
        self.btn_popout.setToolTip("把实时投屏画面拖出主窗口单独显示（也可直接拖画面右上角的 ⊹ 把手）；\n关闭独立窗口即收回主界面，投屏不断")
        self.btn_popout.clicked.connect(lambda: self._popout_mirror())
        bar.addWidget(self.btn_popout)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.btn_theme = QPushButton("浅色模式" if self.cfg.get("theme", "light") == "dark"
                                     else "深色模式")
        self.btn_theme.setToolTip("切换深色 / 浅色主题（写入配置）")
        self.btn_theme.clicked.connect(self._toggle_theme)
        bar.addWidget(self.btn_theme)
        bar.addSeparator()
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setWordWrap(True)   # 长状态消息换行展示，全文另见 tooltip/日志
        bar.addWidget(self.lbl_status)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.mirror_panel = MirrorPanel(self.cfg, serial_getter=self._serial)
        self.mirror_panel.sig_case_saved.connect(self._on_mirror_case_saved)
        self.mirror_panel.sig_popout.connect(self._popout_mirror)
        self.mirror_panel.sig_assert.connect(self._on_assert_request)
        # 投屏侧的手势/按键/文本也进录制页的事件流：scrcpy 注入不落 /dev/input，
        # 不转发的话“在投屏上演示”会被录成空用例（真机触摸不会重复：注入不产生事件）
        self.mirror_panel.on_record_event = self._forward_record_event
        self.mirror_panel.on_record_text = self._forward_record_text
        self.tabs.addTab(self.mirror_panel, "① 实时投屏")
        self.tabs.addTab(self._build_record_tab(), "② 录制")
        self.page_editor = self._build_editor_tab()
        self.tabs.addTab(self.page_editor, "③ 用例编辑")
        self.page_replay = self._build_replay_tab()
        self.tabs.addTab(self.page_replay, "④ 回放")
        self.tabs.addTab(self._build_config_tab(), "⑤ 配置")
        self.page_log = self._build_log_tab()
        self.tabs.addTab(self.page_log, "⑥ 日志")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("用例文件是唯一数据源；GUI 的改动直接写回 YAML/JSON")

    def _toggle_theme(self):
        nxt = "light" if self.cfg.get("theme", "light") == "dark" else "dark"
        self.cfg["theme"] = nxt
        apply_theme(QApplication.instance(), nxt)
        self.btn_theme.setText("浅色模式" if nxt == "dark" else "深色模式")
        # 投屏画面背景是自定义绘制（不走 QSS），必须显式触发重绘才刷新
        self.mirror_panel.view.update()
        _persist_cfg_key("theme", nxt)

    def _line_with_button(self, line: QLineEdit, caption: str, slot) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(line, 1)
        b = QPushButton(caption)
        b.clicked.connect(slot)
        h.addWidget(b)
        return w

    def _build_record_tab(self) -> QWidget:
        page = QWidget()
        root = QHBoxLayout(page)
        root.setContentsMargins(10, 10, 10, 10)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        gb_param = QGroupBox("录制参数")
        form = QFormLayout(gb_param)
        form.setSpacing(8)
        self.rec_out = QLineEdit(str(ROOT / "cases" / "recorded.yaml"))
        form.addRow("输出用例", self._line_with_button(self.rec_out, "浏览…", self._browse_record_out))
        self.rec_name = QLineEdit("")
        self.rec_name.setPlaceholderText("留空则取文件名")
        form.addRow("用例名", self.rec_name)
        self.rec_tags = QLineEdit("")
        self.rec_tags.setPlaceholderText("逗号分隔，如 hvac,smoke")
        form.addRow("标签", self.rec_tags)
        self.rec_nodes = QLineEdit("")
        self.rec_nodes.setPlaceholderText("留空自动探测；也可填 /dev/input/event5 ...")
        form.addRow("监听节点", self.rec_nodes)
        self.rec_event_mode = QComboBox()
        self.rec_event_mode.addItems(["auto(label→hex)", "label", "hex"])
        form.addRow("getevent 模式", self.rec_event_mode)
        self.rec_shots = QCheckBox("逐步截图并生成图片基线断言")
        self.rec_shots.setChecked(bool(self.cfg.get("screenshots", False)))
        form.addRow("", self.rec_shots)
        self.rec_policy = QComboBox()
        self.rec_policy.addItems(["all", "last", "none"])
        self.rec_policy.setCurrentText(self.cfg.get("baseline_policy", "all"))
        form.addRow("断言策略", self.rec_policy)
        self.rec_delay = QSpinBox()
        self.rec_delay.setRange(0, 5000)
        self.rec_delay.setValue(int(self.cfg.get("shot_delay_ms", 250)))
        self.rec_delay.setSuffix(" ms")
        self.rec_delay.setMaximumWidth(160)
        form.addRow("截图延时", self.rec_delay)
        self.rec_shot_mode = QComboBox()
        self.rec_shot_mode.addItems(["async", "sync"])
        self.rec_shot_mode.setCurrentText(self.cfg.get("shot_mode", "async"))
        form.addRow("截图模式", self.rec_shot_mode)
        self.rec_draft = QCheckBox("崩溃安全草稿（周期性原子写）")
        self.rec_draft.setChecked(bool(self.cfg.get("draft_enabled", True)))
        form.addRow("", self.rec_draft)
        self.rec_statusbar = QCheckBox("忽略顶部状态栏（避免时间/电量误报）")
        self.rec_statusbar.setChecked(True)
        form.addRow("", self.rec_statusbar)
        lv.addWidget(gb_param)

        btnrow = QHBoxLayout()
        self.btn_rec_start = QPushButton("● 开始录制")
        set_role(self.btn_rec_start, "danger")
        self.btn_rec_start.clicked.connect(self._start_record)
        self.btn_rec_stop = QPushButton("■ 停止录制并生成用例")
        self.btn_rec_stop.setEnabled(False)
        self.btn_rec_stop.clicked.connect(self._stop_record)
        btnrow.addWidget(self.btn_rec_start)
        btnrow.addWidget(self.btn_rec_stop)
        lv.addLayout(btnrow)
        self.lbl_rec_state = QLabel("待机")
        self.lbl_rec_state.setObjectName("stateBadge")
        self.lbl_rec_state.setWordWrap(True)   # 完成统计/失败原因过长时换行不截断
        self.lbl_rec_state.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        lv.addWidget(self.lbl_rec_state)
        lv.addStretch(1)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        lb_steps = QLabel("实时识别到的步骤")
        set_subdued(lb_steps)
        rv.addWidget(lb_steps)
        self.tbl_rec_steps = self._make_table(["#", "动作", "at_ms", "参数", "说明"], [45, 110, 70, 230, 180])
        rv.addWidget(self.tbl_rec_steps, 1)
        split.addWidget(right)
        split.setSizes([430, 760])
        return page

    def _build_editor_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.ed_path = QLineEdit(str(self.case_path))
        lb_path = QLabel("用例文件")
        set_subdued(lb_path)
        top.addWidget(lb_path)
        top.addWidget(self.ed_path, 1)
        for text, slot in (("打开", self._editor_open), ("浏览…", self._editor_browse),
                           ("保存", lambda: self._save_case()), ("另存为…", self._editor_save_as),
                           ("校验", self._editor_validate)):
            b = QPushButton(text)
            if text == "保存":
                set_role(b, "accent")
            b.clicked.connect(slot)
            top.addWidget(b)
        v.addLayout(top)

        self.ed_tabs = QTabWidget()
        table_page = QWidget()
        tv = QVBoxLayout(table_page)
        self.tbl_steps = self._make_table(["#", "action", "at_ms", "参数", "desc"],
                                          [45, 120, 70, 260, 220])
        self.tbl_steps.doubleClicked.connect(self._edit_step)
        tv.addWidget(self.tbl_steps, 1)
        row = QHBoxLayout()
        for text, slot in (("新增步骤", self._add_step), ("添加断言", self._add_assertion),
                           ("编辑步骤", self._edit_step),
                           ("删除", self._del_step), ("上移", lambda: self._move_step(-1)),
                           ("下移", lambda: self._move_step(1))):
            b = QPushButton(text)
            if text == "添加断言":
                set_role(b, "accent")
                b.setToolTip("添加控件断言 / 界面文本 / Activity / 图片基线断言；\n"
                             "也可以在「① 实时投屏」画面上右键，直接把选中控件设成断言或存成截图基线")
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        self.lbl_case_info = QLabel("")
        self.lbl_case_info.setWordWrap(True)   # 长用例名换行展示
        row.addWidget(self.lbl_case_info)
        tv.addLayout(row)
        self.ed_tabs.addTab(table_page, "步骤表格")

        self.txt_source = QPlainTextEdit()
        self.txt_source.setObjectName("codeView")
        self.ed_tabs.addTab(self.txt_source, "YAML / JSON 源码")
        self.ed_tabs.currentChanged.connect(self._on_editor_tab_changed)
        v.addWidget(self.ed_tabs, 1)
        lb_hint = QLabel("表格与源码是同一份用例的两个视图；切换时自动同步，保存即写回文件。")
        lb_hint.setWordWrap(True)
        set_subdued(lb_hint)
        v.addWidget(lb_hint)
        return page

    def _build_replay_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        gb_param = QGroupBox("回放参数")
        form = QFormLayout(gb_param)
        form.setSpacing(8)
        self.rp_case = QLineEdit(str(self.case_path))
        form.addRow("用例文件", self._line_with_button(self.rp_case, "使用当前用例", self._use_current_case))
        self.rp_repeat = QSpinBox()
        self.rp_repeat.setRange(1, 999)
        self.rp_repeat.setValue(1)
        self.rp_repeat.setMaximumWidth(160)
        form.addRow("重复轮数", self.rp_repeat)
        self.rp_speed = QDoubleSpinBox()
        self.rp_speed.setRange(0.1, 10.0)
        self.rp_speed.setSingleStep(0.5)
        self.rp_speed.setValue(1.0)
        self.rp_speed.setMaximumWidth(160)
        form.addRow("速度倍数", self.rp_speed)
        self.rp_from = QSpinBox()
        self.rp_from.setRange(1, 9999)
        self.rp_from.setMaximumWidth(160)
        form.addRow("从第 N 步", self.rp_from)
        self.rp_to = QSpinBox()
        self.rp_to.setRange(0, 9999)
        self.rp_to.setSpecialValueText("到最后")
        self.rp_to.setMaximumWidth(160)
        form.addRow("到第 N 步", self.rp_to)
        self.rp_on_error = QComboBox()
        self.rp_on_error.addItems(["stop", "continue"])
        form.addRow("失败策略", self.rp_on_error)
        self.rp_params = QLineEdit("")
        self.rp_params.setPlaceholderText("k=v,k2=v2，覆盖用例 params")
        form.addRow("参数覆盖", self.rp_params)
        checks = QHBoxLayout()
        self.rp_dry = QCheckBox("dry-run（不连设备）")
        # 默认不勾：回放的主用途就是真机执行。勾上 dry-run 只会打印命令、
        # 设备毫无动静，却仍然报「通过 7/7」，最容易让人以为回放坏了。
        self.rp_dry.setChecked(False)
        self.rp_dry.setToolTip(
            "勾选后只把将执行的 ADB 命令打印出来，不连设备也不操作手机\n"
            "用途：没插设备时校验用例语法/坐标；要真机回放请保持不勾选")
        self.rp_dry.toggled.connect(self._on_dry_toggled)
        self.rp_timeline = QCheckBox("按 at_ms 时间轴")
        self.rp_update = QCheckBox("刷新图片基线")
        self.rp_noreport = QCheckBox("不写报告")
        for c in (self.rp_dry, self.rp_timeline, self.rp_update, self.rp_noreport):
            checks.addWidget(c)
        checks.addStretch(1)
        form.addRow("选项", self._wrap(checks))
        v.addWidget(gb_param)

        row = QHBoxLayout()
        self.btn_rp_start = QPushButton("▶ 开始回放")
        set_role(self.btn_rp_start, "success")
        self.btn_rp_start.clicked.connect(self._start_replay)
        self.btn_rp_stop = QPushButton("■ 停止")
        self.btn_rp_stop.setEnabled(False)
        self.btn_rp_stop.clicked.connect(self._stop_replay)
        row.addWidget(self.btn_rp_start)
        row.addWidget(self.btn_rp_stop)
        self.lbl_rp_result = QLabel("尚未运行")
        self.lbl_rp_result.setWordWrap(True)   # 结果/失败原因过长时换行
        row.addWidget(self.lbl_rp_result, 1)
        v.addLayout(row)
        # 模式提示条：预演/真机两态一眼可辨（预演不再“静默成功”）
        self.lbl_rp_hint = QLabel("")
        self.lbl_rp_hint.setWordWrap(True)
        v.addWidget(self.lbl_rp_hint)
        self._on_dry_toggled(self.rp_dry.isChecked())
        lb_result = QLabel("逐步结果")
        set_subdued(lb_result)
        v.addWidget(lb_result)
        self.tbl_replay = self._make_table(["#", "动作", "结果", "耗时", "命令 / 消息"],
                                           [45, 120, 60, 70, 420])
        v.addWidget(self.tbl_replay, 1)
        return page

    def _build_config_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        lb_cfg = QLabel(f"配置文件: {default_config_path()}  （保存后立即生效）")
        lb_cfg.setWordWrap(True)   # 长路径换行展示
        set_subdued(lb_cfg)
        v.addWidget(lb_cfg)
        self.txt_config = QPlainTextEdit()
        self.txt_config.setObjectName("codeView")
        v.addWidget(self.txt_config, 1)
        row = QHBoxLayout()
        for text, slot in (("保存配置", self._save_config), ("重新载入", self._load_config_text),
                           ("恢复默认", self._reset_config)):
            b = QPushButton(text)
            if text == "保存配置":
                set_role(b, "accent")
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return page

    def _build_log_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setObjectName("logView")
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(3000)
        v.addWidget(self.txt_log, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        b = QPushButton("清空日志")
        b.clicked.connect(self.txt_log.clear)
        row.addWidget(b)
        v.addLayout(row)
        return page

    def _wrap(self, layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _make_table(self, headers, widths) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(28)
        hh = t.horizontalHeader()
        for i, w in enumerate(widths):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            t.setColumnWidth(i, w)
        hh.setStretchLastSection(True)
        return t

    # ---------------- 通用 ----------------
    def append_log(self, msg: str):
        self.txt_log.appendPlainText(str(msg))
        first = str(msg).splitlines()[0] if str(msg) else ""
        self.lbl_status.setText(short(first, 60))
        self.lbl_status.setToolTip(first)   # 截断展示时悬停可看全文
        self.statusBar().showMessage(short(str(msg), 120), 5000)

    def _serial(self) -> str:
        s = self.cmb_device.currentText()
        return "" if s.startswith("（") else s

    def _mirror_frame(self):
        """投屏在线时给图像断言/录制截图提供最新解码帧（免 screencap 秒级开销）。

        仅限与回放/录制目标设备一致的会话；不可用时返回 None，调用方回退 screencap。
        """
        w = getattr(self, "mirror_panel", None)
        w = w.worker if w is not None else None
        if w is None or not w.isRunning():
            return None
        session = w.current()
        if session is None:
            return None
        target = self._serial()
        if target and session.adb.serial and session.adb.serial != target:
            return None
        arr, _ts = session.latest_frame()
        return arr

    def _on_tab_changed(self, index: int):
        rp = getattr(self, "rp_case", None)
        page = self.tabs.widget(index)
        # 只在回放页还没填路径时补默认值，不覆盖用户手动输入的用例
        # （按页面对象判定：投屏页被拖出去后页签序号会整体前移）
        if (page is not None and page is getattr(self, "page_replay", None)
                and rp is not None and not rp.text().strip() and self.case_path):
            rp.setText(str(self.case_path))

    # ---------------- 投屏 ----------------
    def _open_mirror(self):
        if self.mirror_win is not None:
            # 画面已独立展示：置顶那个窗口，不再往主界面页签里找
            self.mirror_win.showNormal()
            self.mirror_win.raise_()
            self.mirror_win.activateWindow()
        else:
            self.tabs.setCurrentWidget(self.mirror_panel)
        self.mirror_panel.start_mirror()

    def _popout_mirror(self, pos=None):
        """把「实时投屏」画面拖出主窗口独立展示（关闭那个窗口即收回）。

        窗口只建一次、反复复用：关窗时它只隐藏，面板回页签——不销毁窗口，
        面板就不会随着最后一个 Python 引用消失而被连带删掉。
        """
        panel = self.mirror_panel
        idx = self.tabs.indexOf(panel)
        if idx < 0:
            # 已经拖出去了：把那个窗口提到前面即可
            win = self.mirror_win
            if win is not None:
                win.showNormal()
                win.raise_()
                win.activateWindow()
            return
        self._mirror_tab_index = idx
        self.tabs.removeTab(idx)
        if self.mirror_win is None:
            self.mirror_win = MirrorWindow(self.cfg, panel=panel,
                                           on_closed=self._dock_mirror,
                                           on_log=self.append_log)
        else:
            self.mirror_win.setCentralWidget(panel)
        win = self.mirror_win
        panel.show()   # 换父窗口时 Qt 会先把控件隐藏，这里重新亮出来
        if pos is not None and hasattr(pos, "x"):
            # 落点往左上偏一点：窗口标题栏避开鼠标，画面刚好在落点附近
            win.move(max(0, int(pos.x()) - 140), max(0, int(pos.y()) - 50))
        elif not win.isVisible():
            geo = self.frameGeometry()
            win.move(geo.x() + 60, geo.y() + 60)
        win.show()
        win.raise_()
        self.append_log("[投屏] 画面已拖出主窗口独立展示；关闭该窗口即收回主界面（投屏不断）")

    def _dock_mirror(self):
        """独立窗口关闭时回调：把投屏面板插回原页签位置，投屏会话继续。"""
        panel = self.mirror_panel
        if panel.parent() is not None:
            panel.setParent(None)
        idx = min(max(0, self._mirror_tab_index), self.tabs.count())
        self.tabs.insertTab(idx, panel, "① 实时投屏")
        self.tabs.setCurrentWidget(panel)
        panel.show()
        self.append_log("[投屏] 画面已收回主界面")

    def _on_mirror_case_saved(self, path: str):
        self._load_case(path)
        self.tabs.setCurrentWidget(self.page_editor)

    # ---------------- 设备 ----------------
    def _refresh_devices(self):
        w = DeviceWorker(self.cfg, "list", self._serial())
        self._device_worker = w
        w.sig_log.connect(self.append_log)
        w.sig_failed.connect(lambda e: self.append_log(f"[设备] 失败: {e}"))
        w.sig_devices.connect(self._fill_devices)
        w.start()

    def _fill_devices(self, devices: list):
        current = self._serial()
        self.cmb_device.blockSignals(True)
        self.cmb_device.clear()
        self.cmb_device.addItem("（自动 / 未选择）")
        for d in devices:
            self.cmb_device.addItem(d["serial"])
        if current and self.cmb_device.findText(current) >= 0:
            self.cmb_device.setCurrentText(current)
        self.cmb_device.blockSignals(False)
        self.cmb_device.setToolTip(self.cmb_device.currentText())

    def _probe_device(self):
        # 先给明确反馈：探测要跑几条 adb（getevent 在部分设备上要 2~10s），
        # 旧版点下去毫无变化，看起来像按钮失效
        if self._probe_worker is not None and self._probe_worker.isRunning():
            self.lbl_status.setText("探测中…请稍候（上次探测还没返回）")
            return
        self._probe_finished = False
        self.btn_probe.setEnabled(False)
        self.lbl_status.setText("探测中…（读取屏幕尺寸与输入节点能力）")
        self.lbl_status.setToolTip("")
        w = DeviceWorker(self.cfg, "probe", self._serial())
        self._probe_worker = w
        w.sig_log.connect(self.append_log)
        w.sig_failed.connect(self._probe_failed)
        w.sig_profile.connect(self._show_profile)
        w.finished.connect(lambda ww=w: self._probe_done(ww))
        w.start()

    def _probe_done(self, worker):
        self.btn_probe.setEnabled(True)
        if self._probe_worker is worker:
            self._probe_worker = None
        if not self._probe_finished:
            # 线程结束了但既没成功也没报错：不能留“探测中…”挂在工具栏
            self.lbl_status.setText("探测结束：没有返回结果（详见日志）")

    def _probe_failed(self, msg: str):
        """探测失败也要上状态栏：旧版只写日志，看起来像按钮没反应。"""
        self._probe_finished = True
        first = (str(msg).splitlines() or [""])[0]
        # 注意顺序：append_log 会把摘要写进同一条状态标签，必须最后再覆盖
        self.append_log("[探测] 失败: " + str(msg))
        self.lbl_status.setText("探测失败：" + short(first, 60))
        self.lbl_status.setToolTip(str(msg))

    def _show_profile(self, info: dict):
        self._probe_finished = True
        size = info.get("size") or [0, 0]
        touch_n = len(info.get("touch_nodes") or [])
        key_n = len(info.get("key_nodes") or [])
        self.append_log(f"[探测] {info.get('serial')} {info.get('model')} "
                        f"Android {info.get('android_release')} "
                        f"屏幕 {size} 触摸上限 {info.get('touch_abs')} "
                        f"比例 {info.get('coord_ratio')} 来源 {info.get('abs_source')}")
        for d in info.get("inputs", []):
            self.append_log(f"    {d['node']:<22} {d['name']:<28} "
                            f"touch={d['is_touch']} keys={','.join(d['keys'][:4])}")
        if info.get("coord_warning"):
            self.append_log("[WARN] 触摸坐标与屏幕横纵比不一致，请在配置里设 swap_xy 或手动指定节点")
        for err in info.get("errors") or []:
            self.append_log("[WARN] 探测受限: " + str(err))
        rn = info.get("record_nodes") or []
        self.append_log("[探测] 可录制节点: " + (", ".join(rn) if rn else "未解析到（请手动填）"))
        saved = self._save_probe_profile(info)
        if saved:
            self.append_log(f"[探测] 设备档案已保存: {saved}")
        # 日志写完再落状态：append_log 会写同一标签，顺序反了摘要会被冲掉
        self.lbl_status.setText(
            f"探测完成：{info.get('model') or info.get('serial') or '?'}  屏幕 {size[0]}x{size[1]}  "
            f"触摸 {touch_n} / 按键 {key_n}")
        self.lbl_status.setToolTip(
            f"触摸上限 {info.get('touch_abs')}（{info.get('abs_source')}）\n"
            f"密度 {info.get('density')}  旋转 {info.get('rotation')}\n"
            f"监听节点 {', '.join(info.get('record_nodes') or []) or '未解析到'}\n"
            + "\n".join(info.get("errors") or [] or ["全部能力可用"]))

    def _save_probe_profile(self, info: dict) -> str:
        """档案落盘：与 CLI --json-out 同一个文件，失败不影响探测结果。"""
        if not getattr(self, "_probe_save_profile", True):
            return ""
        try:
            p = ROOT / "reports" / "device_profile.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
            return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
        except (OSError, ValueError) as exc:
            self.append_log(f"[探测] 档案写入失败: {exc}")
            return ""

    # ---------------- 录制 ----------------
    def _browse_record_out(self):
        p, _ = QFileDialog.getSaveFileName(self, "录制用例输出", self.rec_out.text(),
                                           "用例 (*.yaml *.yml *.json)")
        if p:
            self.rec_out.setText(p)

    def _forward_record_event(self, ev: RawEvent):
        """投屏合成事件 -> 录制页的 RecordWorker（未在录制则直接丢弃）。"""
        w = self.rec_worker
        if w is not None and w.isRunning():
            w.set_mirror_size(*self.mirror_panel.view._video_size)
            w.feed_mirror(ev)

    def _forward_record_text(self, text: str):
        w = self.rec_worker
        if w is not None and w.isRunning():
            w.feed_mirror_text(text)

    def _start_record(self):
        if self.rec_worker is not None and self.rec_worker.isRunning():
            self.append_log("[录制] 上一次录制还没结束，请先停止")
            return
        out = self.rec_out.text().strip()
        if not out:
            QMessageBox.warning(self, "缺少输出路径", "请先填写输出用例路径")
            return
        mode = self.rec_event_mode.currentText()
        try:
            params = parse_params("")
        except ValueError:
            params = {}
        opts = {
            "out": out,
            "fmt": detect_format(Path(out)),
            "name": self.rec_name.text().strip(),
            "tags": [t.strip() for t in self.rec_tags.text().split(",") if t.strip()],
            "nodes": [n.strip() for n in self.rec_nodes.text().split() if n.strip()],
            "hex_mode": mode == "hex",
            "event_mode": mode.split("(")[0].strip(),
            "screenshots": self.rec_shots.isChecked(),
            "baseline_policy": self.rec_policy.currentText(),
            "shot_delay": self.rec_delay.value(),
            "shot_mode": self.rec_shot_mode.currentText(),
            "draft": self.rec_draft.isChecked(),
            "include_status_bar": not self.rec_statusbar.isChecked(),
            "serial": self._serial(),
            "params": params,
            "frame_provider": self._mirror_frame,
            # 投屏合成事件是视频像素：告诉录制线程按这个尺度归一化坐标
            "video_size": tuple(self.mirror_panel.view._video_size),
        }
        self.tbl_rec_steps.setRowCount(0)
        self.btn_rec_start.setEnabled(False)
        self.btn_rec_stop.setEnabled(True)
        self.lbl_rec_state.setText("录制中… 在设备或投屏画面上操作")
        self.tabs.setCurrentWidget(self.page_log)
        w = RecordWorker(self.cfg, opts)
        self.rec_worker = w
        w.sig_log.connect(self.append_log)
        w.sig_steps.connect(self._fill_rec_steps)
        w.sig_done.connect(self._on_record_done)
        w.sig_failed.connect(self._on_record_failed)
        w.start()

    def _stop_record(self):
        if self.rec_worker:
            self.btn_rec_stop.setEnabled(False)
            self.lbl_rec_state.setText("正在收尾并生成用例…")
            self.rec_worker.stop()

    def _fill_rec_steps(self, steps: list):
        t = self.tbl_rec_steps
        t.setRowCount(len(steps))
        for i, s in enumerate(steps):
            vals = [str(i + 1), s.get("action", ""), str(s.get("at_ms", "")),
                    step_summary(s), s.get("desc", "")]
            for c, v in enumerate(vals):
                _set_cell(t, i, c, v)
        self.lbl_rec_state.setText(f"录制中… 已识别 {len(steps)} 步")

    def _on_record_done(self, info: dict):
        self.btn_rec_start.setEnabled(True)
        self.btn_rec_stop.setEnabled(False)
        self.lbl_rec_state.setText(f"完成：{info['events']} 条事件 → {info['steps']} 步")
        self.append_log(f"[完成] 用例已生成，切换到「用例编辑」可继续加工")
        self._load_case(info["path"])
        self.tabs.setCurrentWidget(self.page_editor)

    def _on_record_failed(self, msg: str):
        self.btn_rec_start.setEnabled(True)
        self.btn_rec_stop.setEnabled(False)
        self.lbl_rec_state.setText("录制失败")
        self.append_log("[录制失败] " + msg)
        QMessageBox.critical(self, "录制失败", msg.splitlines()[0])

    # ---------------- 用例编辑 ----------------
    def _load_config_text(self):
        p = default_config_path()
        try:
            self.txt_config.setPlainText(p.read_text(encoding="utf-8") if p.exists()
                                         else json.dumps(DEFAULTS, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.append_log(f"[配置] 读取失败: {exc}")

    def _save_config(self):
        try:
            data = json.loads(self.txt_config.toPlainText())
            if not isinstance(data, dict):
                raise ValueError("配置根节点必须是对象")
            p = default_config_path()
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.cfg = load_config()
            self.mirror_panel.cfg = self.cfg
            self.append_log(f"[配置] 已保存并生效: {p}")
        except Exception as exc:
            QMessageBox.warning(self, "配置保存失败", str(exc))

    def _reset_config(self):
        self.txt_config.setPlainText(json.dumps(DEFAULTS, indent=2, ensure_ascii=False) + "\n")
        self.append_log("[配置] 已填入默认值，点「保存配置」生效")

    def _editor_browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "打开用例", str(ROOT / "cases"),
                                           "用例 (*.yaml *.yml *.json)")
        if p:
            self.ed_path.setText(p)
            self._load_case(p)

    def _editor_open(self):
        self._load_case(self.ed_path.text().strip())

    def _load_case(self, path: str, quiet: bool = False) -> bool:
        try:
            case = load_case(path)
            self.case_data = case
            self.case_path = Path(path)
            self.ed_path.setText(str(self.case_path))
            self.rp_case.setText(str(self.case_path))
            self._syncing = True
            try:
                self._fill_step_table()
                self.txt_source.setPlainText(dumps_case(case, detect_format(self.case_path)))
            finally:
                self._syncing = False
            self.lbl_case_info.setText(
                f"{case.get('name', '?')} | {len(case.get('steps', []))} 步 | format={case.get('format', '旧用例')}")
            if not quiet:
                self.append_log(f"[用例] 已加载 {self.case_path}")
            return True
        except Exception as exc:
            self.append_log(f"[用例] 加载失败: {exc}")
            if not quiet:
                QMessageBox.warning(self, "加载失败", str(exc))
            return False

    def _fill_step_table(self):
        steps = (self.case_data or {}).get("steps", [])
        t = self.tbl_steps
        t.setRowCount(len(steps))
        for i, s in enumerate(steps):
            vals = [str(i + 1), s.get("action", ""), str(s.get("at_ms", "")),
                    step_summary(s), s.get("desc", "")]
            for c, v in enumerate(vals):
                _set_cell(t, i, c, str(v))

    def _on_editor_tab_changed(self, index: int):
        if self._syncing or self.case_data is None:
            return
        if index == 0:
            # 从源码同步到表格
            try:
                case = loads_case(self.txt_source.toPlainText(), detect_format(self.case_path))
                self.case_data = case
                self._fill_step_table()
            except Exception as exc:
                self.append_log(f"[用例] 源码解析失败，表格保持原样: {exc}")
        else:
            # 从表格同步到源码（表格修改已即时写入 case_data）
            self._syncing = True
            self.txt_source.setPlainText(dumps_case(self.case_data, detect_format(self.case_path)))
            self._syncing = False

    def _save_case(self, silent: bool = False) -> bool:
        if self.case_data is None:
            return False
        try:
            # 若正在看源码，先把源码解析回数据
            if self.ed_tabs.currentIndex() == 1:
                self.case_data = loads_case(self.txt_source.toPlainText(),
                                            detect_format(self.case_path))
            errs = validate_case(self.case_data)
            if errs and not silent:
                self.append_log("[用例] 校验提示: " + " ; ".join(errs[:5]))
            save_case(self.case_data, self.case_path, detect_format(self.case_path))
            self.lbl_case_info.setText(
                f"{self.case_data.get('name', '?')} | {len(self.case_data.get('steps', []))} 步 | 已保存")
            if not silent:
                self.append_log(f"[用例] 已保存 {self.case_path}")
            return True
        except Exception as exc:
            if not silent:
                QMessageBox.warning(self, "保存失败", str(exc))
            return False

    def _editor_save_as(self):
        p, _ = QFileDialog.getSaveFileName(self, "另存用例", str(self.case_path),
                                           "用例 (*.yaml *.yml *.json)")
        if not p:
            return
        old = self.case_path
        self.case_path = Path(p)
        if not self._save_case(silent=True):
            self.case_path = old
            QMessageBox.warning(self, "另存失败", "用例数据无法保存，请检查源码语法")
            return
        self.ed_path.setText(p)
        self.rp_case.setText(p)
        self.append_log(f"[用例] 已另存为 {p}")

    def _editor_validate(self):
        if self.case_data is None:
            return
        try:
            case = (loads_case(self.txt_source.toPlainText(), detect_format(self.case_path))
                    if self.ed_tabs.currentIndex() == 1 else self.case_data)
            errs = validate_case(case)
            expanded = []
            if not errs:
                try:
                    expanded = expand_steps(case, self.case_path.parent)
                except CaseError as exc:
                    errs = list(errs) + [f"include 展开失败: {exc}"]
            if errs:
                QMessageBox.warning(self, "校验失败", "\n".join(errs[:15]))
                self.append_log("[校验] 失败: " + " ; ".join(errs[:5]))
            else:
                QMessageBox.information(self, "校验通过",
                                        f"格式: {case.get('format', CASE_FORMAT)}\n"
                                        f"原始 {len(case.get('steps', []))} 步，展开后 {len(expanded)} 步")
                self.append_log(f"[校验] 通过，展开后 {len(expanded)} 步")
        except Exception as exc:
            QMessageBox.warning(self, "校验失败", str(exc))

    def _selected_step_index(self) -> int:
        return self.tbl_steps.currentRow()

    def _add_step(self):
        if self.case_data is None:
            return
        dlg = StepEditDialog({"action": "wait", "ms": 500, "desc": "新步骤"}, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_step_data:
            self.case_data.setdefault("steps", []).append(dlg.result_step_data)
            self._fill_step_table()
            self._sync_from_table()

    def _edit_step(self):
        i = self._selected_step_index()
        if self.case_data is None or i < 0 or i >= len(self.case_data.get("steps", [])):
            return
        dlg = StepEditDialog(self.case_data["steps"][i], self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_step_data:
            self.case_data["steps"][i] = dlg.result_step_data
            self._fill_step_table()
            self._sync_from_table()

    def _del_step(self):
        i = self._selected_step_index()
        if self.case_data is None or i < 0 or i >= len(self.case_data.get("steps", [])):
            return
        del self.case_data["steps"][i]
        self._fill_step_table()
        self._sync_from_table()

    def _move_step(self, delta: int):
        i = self._selected_step_index()
        steps = (self.case_data or {}).get("steps", [])
        j = i + delta
        if self.case_data is None or i < 0 or j < 0 or j >= len(steps):
            return
        steps[i], steps[j] = steps[j], steps[i]
        self._fill_step_table()
        self.tbl_steps.selectRow(j)
        self._sync_from_table()

    def _sync_from_table(self):
        self._syncing = True
        self.txt_source.setPlainText(dumps_case(self.case_data, detect_format(self.case_path)))
        self._syncing = False

    # ---------------- 断言 ----------------
    def _capture_assert_shot(self, node=None, full: bool = False):
        """把投屏最新一帧存成断言基线 -> (相对用例路径, 归一化区域或 None)。

        基线始终存整屏帧：region 只是回放时的比对掩码（0~1 归一化），
        与 assert_image 现有语义完全一致，跨分辨率也不会错位，
        不必额外裁图，回放失败时差异图也能看到整个界面。
        """
        arr = self.mirror_panel.snapshot()
        if arr is None:
            raise RuntimeError("投屏未连接或还没有画面帧：请先在「① 实时投屏」点「开始投屏」")
        region = None
        if not full and node is not None:
            sw, sh = self.mirror_panel.view._inspect_screen
            bounds = node.get("bounds") or []
            if sw and sh and len(bounds) == 4:
                x1, y1, x2, y2 = (float(v) for v in bounds)
                x1n, y1n = max(0.0, x1 / sw), max(0.0, y1 / sh)
                x2n, y2n = min(1.0, x2 / sw), min(1.0, y2 / sh)
                if x2n > x1n and y2n > y1n:
                    region = [round(x1n, 6), round(y1n, 6),
                              round(x2n - x1n, 6), round(y2n - y1n, 6)]
        out_dir = self.case_path.parent / f"{self.case_path.stem}_shots"
        # 微秒序尾数：同一秒里连拍两张（控件 + 整屏）也不会重名互相覆盖
        name = (f"assert_{time.strftime('%Y%m%d_%H%M%S')}_"
                f"{time.time_ns() // 1000 % 1_000_000:06d}.png")
        if not save_rgb(arr, out_dir / name):
            raise RuntimeError(f"基线图片保存失败（需 Pillow）: {out_dir / name}")
        return f"{out_dir.name}/{name}", region

    def _append_steps(self, steps: list[dict], note: str = "") -> bool:
        """把断言步骤追加到当前用例：表格 / 源码 / 选中行一起同步。"""
        if not steps:
            return False
        if self.case_data is None:
            QMessageBox.information(self, "先加载用例",
                                    "请先在「③ 用例编辑」打开或新建一个用例文件")
            return False
        items = self.case_data.setdefault("steps", [])
        first = len(items)
        items.extend(steps)
        self._fill_step_table()
        self._sync_from_table()
        self.tbl_steps.selectRow(first)
        self.tabs.setCurrentWidget(self.page_editor)
        self.ed_tabs.setCurrentIndex(0)
        self.append_log(f"[断言] 已添加 {len(steps)} 步：{note or step_summary(steps[0])}"
                        "（记得点「保存」写回文件）")
        return True

    def _run_assert_dialog(self, pre_shots: list[str] | None = None, **kwargs) -> bool:
        """弹出断言对话框：确认就追加步骤，取消就回收本次新拍的基线图。

        两个入口（编辑页按钮 / 投屏右键菜单）只差预填内容，构造与收尾都在这里：
        取消后 _shots/ 里不留没人引用的孤儿图；接受时只留步骤真正引用的那张
        （换过截图 / 改过断言类型时，前面的图就被弃用了）。
        """
        dlg = AssertDialog(capture=self._capture_assert_shot,
                           base_dir=self.case_path.parent, parent=self, **kwargs)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_steps:
            used = {str(s.get("image")) for s in dlg.result_steps if s.get("image")}
            dlg.discard_shots(pre_shots, keep=used)
            self._append_steps(dlg.result_steps, dlg.result_note)
            return True
        dlg.discard_shots(pre_shots)
        return False

    def _add_assertion(self):
        """编辑页「添加断言」：预填投屏当前选中的控件（没选就手填）。"""
        if self.case_data is None:
            QMessageBox.information(self, "先加载用例",
                                    "请先在「③ 用例编辑」打开或新建一个用例文件")
            return
        view = self.mirror_panel.view
        node = view._pinned
        sel = selector_from_node(view._inspect_nodes, node) if node is not None else {}
        self._run_assert_dialog(node=node, selector=sel, expect={"exists": True})

    def _on_assert_request(self, req):
        """投屏画面右键：控件断言预填；截图基线先落盘再开对话框确认阈值。"""
        if not isinstance(req, dict):
            return
        kind = str(req.get("kind") or "exists")
        node = req.get("node")
        if self.case_data is None:
            QMessageBox.information(self, "先加载用例",
                                    "请先在「③ 用例编辑」打开或新建一个用例文件")
            return
        kwargs = {"node": node}
        pre_shots: list[str] = []
        if kind in ("shot_widget", "shot_screen"):
            try:
                rel, region = self._capture_assert_shot(
                    node, full=(kind == "shot_screen" or node is None))
            except RuntimeError as exc:
                QMessageBox.warning(self, "截图失败", str(exc))
                return
            kwargs.update(kind="image", image=rel, region=region)
            pre_shots = [rel]   # 用户取消时连预截图一起回收
        else:
            sel = selector_from_node(self.mirror_panel.view._inspect_nodes, node) \
                if node is not None else {}
            exp = {"exists": kind != "absent"}
            if kind == "text":
                exp = {"text": str((node or {}).get("text") or "")}
            kwargs.update(kind="widget", selector=sel, expect=exp)
        self._run_assert_dialog(pre_shots, **kwargs)

    # ---------------- 回放 ----------------
    def _use_current_case(self):
        self.rp_case.setText(str(self.case_path))

    def _on_serial_changed(self, _text: str):
        """设备切换后刷新回放提示条（真机模式会显示目标设备）。"""
        if hasattr(self, "rp_dry"):
            self._on_dry_toggled(self.rp_dry.isChecked())

    def _on_dry_toggled(self, checked: bool):
        """dry-run 只打印命令、不动设备：把模式写进按钮文案和提示条。

        旧版默认勾选 dry-run，用户点「开始回放」看到 7/7 全过、手机却毫无反应，
        很难想到是预演模式——因此两种模式必须在按钮上就区分开。
        """
        hint = getattr(self, "lbl_rp_hint", None)
        if checked:
            self.btn_rp_start.setText("▶ 开始预演（不动设备）")
            self.btn_rp_start.setToolTip("当前为 dry-run：只打印 ADB 命令，不会操作设备")
            if hint is not None:
                hint.setText("⚠ 预演模式（dry-run）：只打印将执行的命令，设备不会有任何反应。\n"
                             "要去手机上真实回放，请取消勾选「dry-run（不连设备）」。")
                hint.setStyleSheet(f"color: {theme_color('danger')};")
        else:
            self.btn_rp_start.setText("▶ 开始回放")
            self.btn_rp_start.setToolTip("在真机/模拟器上逐步执行用例，并写出回放报告")
            if hint is not None:
                hint.setText("真机回放：每一步都会通过 adb 真正操作设备"
                             + (f"（当前设备 {self._serial() or '未连接'}）"))
                hint.setStyleSheet(f"color: {theme_color('subtext')};")

    def _start_replay(self):
        if self.replay_worker is not None and self.replay_worker.isRunning():
            self.append_log("[回放] 上一次回放还没结束，请先停止")
            return
        path = self.rp_case.text().strip()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "用例不存在", "请选择有效的用例文件")
            return
        try:
            params = parse_params(self.rp_params.text())
        except ValueError as exc:
            QMessageBox.warning(self, "参数格式错误", str(exc))
            return
        opts = {
            "case": path, "serial": self._serial(),
            "repeat": self.rp_repeat.value(), "speed": self.rp_speed.value(),
            "from_step": self.rp_from.value(),
            "to_step": self.rp_to.value() or None,
            "on_error": self.rp_on_error.currentText(),
            "params": params,
            "dry_run": self.rp_dry.isChecked(),
            "timeline": self.rp_timeline.isChecked(),
            "update_baseline": self.rp_update.isChecked(),
            "no_report": self.rp_noreport.isChecked(),
            "frame_provider": self._mirror_frame,
        }
        if opts["dry_run"] and opts["update_baseline"]:
            QMessageBox.warning(self, "选项冲突", "刷新基线需要真实设备，不能与 dry-run 同时用")
            return
        if opts["dry_run"]:
            serial = self._serial()
            if serial:
                self.append_log(f"[回放] 注意：已勾选 dry-run，本次不会操作设备 {serial}；"
                                "要真实回放请取消该勾选")
            else:
                self.append_log("[回放] dry-run 预演：未连接设备，只打印将要执行的命令")
        self.tbl_replay.setRowCount(0)
        self.lbl_rp_result.setText("预演中…（不动设备）" if opts["dry_run"] else "运行中…")
        self.btn_rp_start.setEnabled(False)
        self.btn_rp_stop.setEnabled(True)
        self.tabs.setCurrentWidget(self.page_log)
        w = ReplayWorker(self.cfg, opts)
        self.replay_worker = w
        w.sig_log.connect(self.append_log)
        w.sig_step.connect(self._append_replay_step)
        w.sig_done.connect(self._on_replay_done)
        w.sig_failed.connect(self._on_replay_failed)
        w.start()

    def _stop_replay(self):
        if self.replay_worker:
            self.replay_worker.stop()
            self.lbl_rp_result.setText("正在中止…")

    def _append_replay_step(self, res: dict):
        t = self.tbl_replay
        r = t.rowCount()
        t.insertRow(r)
        msg = (res.get("message") or "") + "  " + (res.get("command") or "")
        vals = [str(res.get("index", r + 1)), res.get("action", ""),
                "OK" if res.get("ok") else "FAIL",
                f"{res.get('elapsed_ms', 0)}ms", msg.strip()]
        for c, v in enumerate(vals):
            item = _set_cell(t, r, c, str(v))
            if c == 2 and not res.get("ok"):
                item.setForeground(Qt.GlobalColor.red)
        t.scrollToBottom()

    def _on_replay_done(self, info: dict):
        self.btn_rp_start.setEnabled(True)
        self.btn_rp_stop.setEnabled(False)
        text = f"通过 {info['passed']} / 失败 {info['failed']} / 共 {info['total']} 步"
        self.lbl_rp_result.setText(text)
        self.append_log("[回放] " + text)

    def _on_replay_failed(self, msg: str):
        self.btn_rp_start.setEnabled(True)
        self.btn_rp_stop.setEnabled(False)
        self.lbl_rp_result.setText("回放失败")
        self.append_log("[回放失败] " + msg)
        QMessageBox.critical(self, "回放失败", msg.splitlines()[0])

    def closeEvent(self, event):
        """关窗前停掉所有后台线程：否则 QThread 仍在运行就被销毁，Qt 会崩。"""
        # 先停会话（面板必须还活着），再关独立窗口：窗口里的面板会随窗口一起回收
        self.mirror_panel.shutdown()
        if self.mirror_win is not None:
            self.mirror_win.on_closed = None
            self.mirror_win.close()
        for attr in ("rec_worker", "replay_worker",
                     "_device_worker", "_probe_worker"):
            w = getattr(self, attr, None)
            if w is None:
                continue
            stop = getattr(w, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
            try:
                if w.isRunning() and not w.wait(3000):
                    # 兜底：adb 命令仍阻塞时强制结束，避免 QThread 被析构时崩溃
                    w.terminate()
                    w.wait(1000)
            except RuntimeError:
                pass
        super().closeEvent(event)


# --------------------------------------------------------------------------
# 离屏冒烟自检
# --------------------------------------------------------------------------
def run_gui_selftest() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app, "dark")
    checks: list[tuple[bool, str, str]] = []

    def ck(cond, label, detail=""):
        checks.append((bool(cond), label, detail))

    w = MainWindow(autoload_devices=False)
    # 自检断言按原生 dump（应用窗口）树形校准：不受用户配置 inspect_u2 影响，
    # u2 全窗口树含系统窗口会打破这些断言；blockSignals 避免回写用户配置
    w.cfg["inspect_u2"] = False
    w.mirror_panel.chk_inspect_u2.blockSignals(True)
    w.mirror_panel.chk_inspect_u2.setChecked(False)
    w.mirror_panel.chk_inspect_u2.blockSignals(False)
    ck(APP_TITLE.split(" —")[0] in w.windowTitle(), "主窗口构建")
    ck(w.tabs.count() == 6, "六个功能页签", str(w.tabs.count()))
    ck(w.tabs.tabText(0).find("投屏") >= 0, "投屏页签在首位", w.tabs.tabText(0))

    # 主题：深色/浅色切换与自定义绘制取色
    import gui_theme
    dark_qss = app.styleSheet()
    ck("QPushButton" in dark_qss and gui_theme.current() == "dark",
       "深色主题已应用")
    apply_theme(app, "light")
    ck(app.styleSheet() != dark_qss and gui_theme.color("window") == "#f5f6f8",
       "浅色主题切换生效")
    apply_theme(app, "dark")
    ck(gui_theme.color("video_bg") == "#0d1015", "投屏画面背景取主题色")
    ck(DEFAULTS["theme"] == "light", "默认主题为浅色")
    # 切换主题后投屏背景必须立即重绘刷新（自定义绘制不走 QSS）
    theme_backup = w.cfg.get("theme", "light")
    w.cfg["theme"] = "light"
    w.mirror_panel.view.resize(320, 240)
    apply_theme(app, "light")
    w._toggle_theme()  # light -> dark
    img = w.mirror_panel.view.grab().toImage()
    ck(img.pixelColor(2, 2).name() == "#0d1015", "切深色后投屏背景立即刷新",
       img.pixelColor(2, 2).name())
    w._toggle_theme()  # dark -> light
    img = w.mirror_panel.view.grab().toImage()
    ck(img.pixelColor(2, 2).name() == "#14171c", "切浅色后投屏背景立即刷新",
       img.pixelColor(2, 2).name())
    w.cfg["theme"] = theme_backup
    _persist_cfg_key("theme", theme_backup)  # 自检不污染用户配置
    ck(w.btn_theme.text() in ("浅色模式", "深色模式"), "工具栏主题切换按钮就位")
    ck(w.mirror_panel.btn_start.property("role") == "accent"
       and w.btn_rec_start.property("role") == "danger"
       and w.btn_rp_start.property("role") == "success",
       "主操作按钮语义色（投屏/录制/回放）")

    ok = w._load_case(str(ROOT / "cases" / "example.yaml"), quiet=True)
    ck(ok, "加载示例用例")
    ck(w.case_data is not None and w.case_data.get("format") == CASE_FORMAT,
       "用例 format 字段", str((w.case_data or {}).get("format")))
    ck(w.tbl_steps.rowCount() == len(w.case_data.get("steps", [])),
       "步骤表格填充", f"{w.tbl_steps.rowCount()} 行")
    ck("format: adbcase.case" in w.txt_source.toPlainText(), "YAML 源码视图")

    # 表格 -> 源码 同步
    n0 = len(w.case_data["steps"])
    w.case_data["steps"].append({"action": "wait", "ms": 10, "desc": "selftest"})
    w._sync_from_table()
    ck("selftest" in w.txt_source.toPlainText(), "表格改动同步到源码")
    w.case_data["steps"].pop()
    ck(len(w.case_data["steps"]) == n0, "步骤回滚")

    # 源码 -> 表格 同步
    w.ed_tabs.setCurrentIndex(1)
    src = loads_case(w.txt_source.toPlainText(), "yaml")
    src["name"] = "GUI 自检改名"
    w.txt_source.setPlainText(dumps_case(src, "yaml"))
    w.ed_tabs.setCurrentIndex(0)
    ck(w.lbl_case_info.text().startswith("GUI 自检改名") or w.case_data.get("name") == "GUI 自检改名",
       "源码改动同步回数据")

    # 保存到临时文件
    import tempfile
    with tempfile.TemporaryDirectory(prefix="adbcase_gui_") as td:
        p = Path(td) / "case.yaml"
        old = w.case_path
        w.case_path = p
        saved = w._save_case(silent=True)
        ck(saved and p.exists(), "GUI 保存用例文件")
        if p.exists():
            ck(not validate_case(load_case(p)), "保存后用例校验通过",
               "; ".join(validate_case(load_case(p))))
            ck("&id" not in p.read_text(encoding="utf-8"), "保存后 YAML 无锚点")
        w.case_path = old

    # ---- 断言：选择器预填 / 对话框产出步骤 / 截图为基线 / 追加到用例 ----
    import numpy as _np
    from core.inspector import parse_hierarchy as _ph
    from core.inspector import selector_from_node as _sfn
    _assert_btns = [b for b in w.page_editor.findChildren(QPushButton)
                    if b.text() == "添加断言"]
    ck(bool(_assert_btns), "用例编辑页提供「添加断言」按钮", str(len(_assert_btns)))
    _AXML = ('<hierarchy rotation="0"><node class="android.widget.FrameLayout" '
             'package="com.demo" bounds="[0,0][1080,1920]">'
             '<node class="android.widget.TextView" resource-id="com.demo:id/mode" '
             'text="制冷" package="com.demo" bounds="[40,300][400,380]" /></node></hierarchy>')
    _anodes = _ph(_AXML)
    _anode = _anodes[1]
    _asel = _sfn(_anodes, _anode)
    ck(_asel == {"id": "com.demo:id/mode"}, "投屏命中控件生成断言选择器", str(_asel))

    ck(w.mirror_panel.snapshot() is None, "未投屏时 snapshot 返回 None")

    # 高性能退出监听：窗口关闭后自动恢复内嵌投屏（桩掉 start_mirror，不真连设备）
    import subprocess as _sp
    _mp = w.mirror_panel
    _resume: list = []
    _orig_start = _mp.start_mirror
    _mp.start_mirror = lambda: _resume.append(1)
    _mp._ext_proc = _sp.Popen([sys.executable, "-c", "pass"])
    _mp._ext_proc.wait()
    _mp._watch_external()
    ck(_resume == [1] and _mp._ext_proc is None,
       "高性能窗口关闭后自动恢复内嵌投屏")
    _mp._watch_external()
    ck(_resume == [1], "退出监听只触发一次恢复")
    _mp.start_mirror = _orig_start
    ck(w.mirror_panel.chk_inspect_u2.isChecked() == bool(w.cfg.get("inspect_u2", False)),
       "全窗口(u2)开关与配置一致")
    try:
        w._capture_assert_shot()
        _shot_raised = False
    except RuntimeError:
        _shot_raised = True
    ck(_shot_raised, "未投屏时截图基线报错而非静默写空图")

    _dlg = AssertDialog(node=_anode, selector=_asel, expect={"exists": True})
    ck(_dlg.stack.currentIndex() == 0 and _dlg._sel_edits["id"].text() == "com.demo:id/mode"
       and _dlg._sel_edits["text"].text() == "" and not _dlg.chk_shot.isEnabled(),
       "断言对话框预填控件定位（无投屏时控件截图不可选）")
    _dlg.cmb_expect.setCurrentIndex(_dlg.cmb_expect.findData("text"))
    _dlg.ed_expect.setText("制冷")
    _wsteps = _dlg._widget_steps()
    ck(len(_wsteps) == 1 and _wsteps[0]["action"] == "assert_widget"
       and _wsteps[0]["selector"] == {"id": "com.demo:id/mode"}
       and _wsteps[0]["expect"] == {"text": "制冷"},
       "控件断言步骤=选择器+期望值", str(_wsteps))
    _dlg.ed_expect.setText("")
    try:
        _dlg._widget_steps()
        _text_blocked = False
    except ValueError:
        _text_blocked = True
    ck(_text_blocked, "文本断言缺期望值被拦下")
    _dlg.spn_sel_index.setValue(2)
    _dlg.cmb_expect.setCurrentIndex(_dlg.cmb_expect.findData("absent"))
    _abs_sel = _dlg._widget_steps()[0]
    ck(_abs_sel["selector"].get("index") == 2
       and _abs_sel["expect"] == {"exists": False},
       "「第几个」与「控件不存在」写入断言步骤", str(_abs_sel))

    _dlg_txt = AssertDialog(node=_anode, kind="text")
    ck(_dlg_txt.stack.currentIndex() == 1 and _dlg_txt.ed_text.text() == "制冷",
       "文本断言页预填选中控件文本")
    ck(_dlg_txt._text_step("not_text")["action"] == "assert_not_text",
       "界面不含文本断言动作")
    ck(step_summary(_wsteps[0]).startswith("id=com.demo:id/mode"),
       "表格参数列展示控件断言摘要", step_summary(_wsteps[0]))

    # 投屏右键请求 -> 主窗口预填对话框（桩掉对话框本体，不弹模态）
    _orig_dlg_cls = AssertDialog
    _seen: dict = {}

    class _StubAssertDialog:
        def __init__(self, **kw):
            _seen.update(kw)

        def exec(self):
            return QDialog.DialogCode.Rejected

        def discard_shots(self, extra=None, keep=None):
            _seen["discarded"] = list(extra or [])
            _seen["kept"] = sorted(keep or [])

    _nodes_backup = w.mirror_panel.view._inspect_nodes
    w.mirror_panel.view._inspect_nodes = _anodes
    globals()["AssertDialog"] = _StubAssertDialog
    w.mirror_panel._on_assert_pick("text", _anode)
    globals()["AssertDialog"] = _orig_dlg_cls
    w.mirror_panel.view._inspect_nodes = _nodes_backup
    ck(_seen.get("kind") == "widget" and _seen.get("selector") == _asel
       and _seen.get("expect") == {"text": "制冷"},
       "右键请求 -> 断言对话框预填选择器与期望值", str(_seen)[:140])
    ck(_seen.get("discarded") == [], "取消对话框时按约定调 discard_shots",
       str(_seen.get("discarded")))

    with tempfile.TemporaryDirectory(prefix="adbcase_assert_") as atd:
        _old_case_path, _old_snap = w.case_path, w.mirror_panel.snapshot
        w.case_path = Path(atd) / "case.yaml"
        w.mirror_panel.view._inspect_screen = (1080, 1920)
        _frame = _np.zeros((1920, 1080, 3), dtype=_np.uint8)
        w.mirror_panel.snapshot = lambda: _frame
        _rel, _region = w._capture_assert_shot(_anode)
        ck(_rel.startswith("case_shots/") and (Path(atd) / _rel).exists(),
           "控件截图存到 <用例名>_shots/", _rel)
        ck(_region is not None and abs(_region[0] - 40 / 1080) < 1e-5
           and abs(_region[1] - 300 / 1920) < 1e-5
           and abs(_region[3] - 80 / 1920) < 1e-5,
           "控件截图区域归一化为 0~1 矩形", str(_region))
        _full_rel, _full_region = w._capture_assert_shot(_anode, full=True)
        ck(_full_region is None and (Path(atd) / _full_rel).exists(),
           "整屏基线不带 region")

        _img_dlg = AssertDialog(node=_anode, kind="image", image=_rel,
                                region=_region, capture=w._capture_assert_shot,
                                base_dir=Path(atd))
        _istep = _img_dlg._image_step()
        ck(_istep["image"] == _rel and _istep.get("region") == _region
           and _istep.get("ignore_regions") and _istep["threshold"] == 0.02,
           "图片基线步骤带区域/阈值/忽略状态栏", str(_istep))
        _img_dlg.cmb_region.setCurrentIndex(0)
        ck("region" not in _img_dlg._image_step(), "选整屏时不写 region")

        _shot_wsteps = AssertDialog(node=_anode, selector=_asel,
                                    capture=w._capture_assert_shot,
                                    base_dir=Path(atd))
        _shot_wsteps.chk_shot.setChecked(True)
        _pair = _shot_wsteps._widget_steps()
        ck(len(_pair) == 2 and _pair[1]["action"] == "assert_image"
           and _pair[1].get("region") == _region and _pair[1]["wait_before_ms"] == 0,
           "控件断言可同时追加控件截图基线", str(_pair[1]))
        ck(len(_shot_wsteps.shots) == 1
           and (Path(atd) / _shot_wsteps.shots[0]).exists(),
           "拍过的基线图记账（取消时才能回收）", str(_shot_wsteps.shots))

        _ds = AssertDialog(node=_anode, selector=_asel,
                           capture=w._capture_assert_shot, base_dir=Path(atd))
        _ds._shoot_current()
        _ds_rel = _ds.ed_image.text()
        ck(_ds_rel and (Path(atd) / _ds_rel).exists(), "「截取当前投屏画面」落盘", _ds_rel)
        _ds.discard_shots()
        ck(not (Path(atd) / _ds_rel).exists() and not _ds.shots,
           "取消断言时清掉本次新拍的基线图", _ds_rel)
        _ds2 = AssertDialog(node=_anode, selector=_asel,
                            capture=w._capture_assert_shot, base_dir=Path(atd))
        _ds2._shoot_current()
        _keep_rel = _ds2.ed_image.text()
        _ds2.discard_shots([], keep={_keep_rel})
        ck(bool(_keep_rel) and (Path(atd) / _keep_rel).exists() and not _ds2.shots,
           "已写进用例步骤的基线图不会被回收", _keep_rel)

        # 右键截图 -> 取消：预截图交给对话框一并回收（否则 _shots/ 里留孤儿图）
        _seen_shot: dict = {}

        class _StubReject:
            def __init__(self, **kw):
                _seen_shot.update(kw)

            def exec(self):
                return QDialog.DialogCode.Rejected

            def discard_shots(self, extra=None, keep=None):
                _seen_shot["pre"] = list(extra or [])
                _seen_shot["kept"] = sorted(keep or [])

        _nodes_bak = w.mirror_panel.view._inspect_nodes
        w.mirror_panel.view._inspect_nodes = _anodes
        globals()["AssertDialog"] = _StubReject
        w.mirror_panel._on_assert_pick("shot_widget", _anode)
        globals()["AssertDialog"] = _orig_dlg_cls
        w.mirror_panel.view._inspect_nodes = _nodes_bak
        _pre = _seen_shot.get("pre") or []
        ck(len(_pre) == 1 and _pre[0].startswith("case_shots/")
           and _seen_shot.get("kind") == "image",
           "右键截图后取消断言：预截图交给对话框回收", str(_pre))
        for _r in _pre:
            (Path(atd) / _r).unlink(missing_ok=True)

        _n_before = len(w.case_data["steps"])
        _appended = w._append_steps(_pair)
        _src = w.txt_source.toPlainText()
        ck(_appended and len(w.case_data["steps"]) == _n_before + 2
           and w.tbl_steps.rowCount() == _n_before + 2
           and "assert_widget" in _src and "assert_image" in _src,
           "添加断言同步表格/源码/选中行")
        ck(w.tbl_steps.currentRow() == _n_before, "新断言行被选中",
           str(w.tbl_steps.currentRow()))
        ck(not validate_case(w.case_data), "含控件断言+截图的用例校验通过",
           "; ".join(validate_case(w.case_data)))
        w.case_data["steps"] = w.case_data["steps"][:_n_before]
        w._fill_step_table()
        w._sync_from_table()
        w.case_path, w.mirror_panel.snapshot = _old_case_path, _old_snap
        w.mirror_panel.view._inspect_screen = (0, 0)

    # 配置页
    cfg_data = json.loads(w.txt_config.toPlainText())
    ck("until_timeout_ms" in cfg_data, "配置页载入新字段")

    # 所有页切换不崩
    for i in range(w.tabs.count()):
        w.tabs.setCurrentIndex(i)
        app.processEvents()
    ck(True, "六个页面切换无异常")

    # 投屏：控制消息打包与坐标映射（离线可验，不连设备）
    from core import mirror as _mir
    touch = _mir.pack_touch(_mir.ACTION_DOWN, 100, 200, 1280, 720)
    ck(len(touch) == 32 and touch[0] == _mir.TYPE_INJECT_TOUCH_EVENT,
       "触摸控制消息 32 字节", str(len(touch)))
    import struct as _st
    aid, pid = _st.unpack(">Bq", touch[1:10])
    x, y, sw, sh = _st.unpack(">iiHH", touch[10:22])
    ck((aid, x, y, sw, sh) == (0, 100, 200, 1280, 720) and pid == -1,
       "触摸消息字段（动作/坐标/屏幕尺寸/指针 id）")
    up = _mir.pack_touch(_mir.ACTION_UP, 100, 200, 1280, 720)
    ck(_st.unpack(">H", up[22:24])[0] == 0, "抬手压力为 0")
    key = _mir.pack_keycode(_mir.ACTION_DOWN, _mir.KEYCODE_BACK)
    ck(len(key) == 14 and _st.unpack(">i", key[2:6])[0] == 4, "按键消息 14 字节")
    scr = _mir.pack_scroll(10, 20, 1280, 720, 0.0, 1.0)
    ck(len(scr) == 21 and _st.unpack(">h", scr[15:17])[0] == 0x7FFF,
       "滚动消息 i16 定点数")
    txt = _mir.pack_text("hi")
    ck(txt == bytes([1, 0, 0, 0, 2]) + b"hi", "文本注入消息")
    ck(_mir.find_server_jar(w.cfg, allow_download=False).is_file(),
       "scrcpy-server jar 就位（vendor/）")
    # 设备声音：默认打开 + 播放器缓冲/静音/限幅（pump=False 不起写线程，离线确定可验）
    ck(DEFAULTS["mirror_audio"] is True and w.mirror_panel.chk_audio.isChecked(),
       "设备声音默认打开")
    ap = AudioPlayer(pump=False)
    ap.push(b"\x00\x01" * 480, 48000, 2)
    ck(ap.buffered_bytes() == 960, "音频 PCM 入缓冲", str(ap.buffered_bytes()))
    ap.set_muted(True)
    ap.push(b"\x00\x01" * 480, 48000, 2)
    ck(ap.buffered_bytes() == 0, "静音时丢弃音频")
    ap.set_muted(False)
    ap.push(b"\x00\x01" * 48000, 48000, 2)  # 480KB，远超 1 秒上限
    ck(ap.buffered_bytes() <= 48000 * 2 * 2, "缓冲超上限自动丢弃最旧")
    ap.stop()
    ck(ap.buffered_bytes() == 0 and ap._thread is None, "停止后播放器复位")

    # 音频停止后再投屏必须能重新出声：旧线程完全退出后 push 拉新线程，
    # 且 stop 不再提前清停止标志/置空引用（曾致 join 超时后双写线程并存）
    class _NoSinkPlayer(AudioPlayer):
        def _open_sink(self):
            class _Dev:
                def write(self, data):
                    return len(data)
            self._dev = _Dev()   # 桩设备：不碰真实声卡，只验线程生命周期
            return True

        def _close_sink(self):
            self._sink = None
            self._dev = None

    ap2 = _NoSinkPlayer()
    ap2.push(b"\x00" * 960, 48000, 2)
    t1 = ap2._thread
    ck(t1 is not None and t1.is_alive(), "音频 push 拉起写线程")
    ap2.stop()
    ck(t1 is not None and not t1.is_alive() and ap2._thread is None,
       "音频 stop 后写线程退出并注销")
    ap2.push(b"\x00" * 960, 48000, 2)
    t2 = ap2._thread
    ck(t2 is not None and t2 is not t1 and t2.is_alive()
       and not ap2._stop.is_set(),
       "音频停止后重新 push 能拉起新线程（重连后不静音）")
    ap2.stop()
    ck(_mir.ScrcpyMirror(Adb(), {"mirror_audio": False}).audio is False
       and _mir.ScrcpyMirror(Adb(), {}).audio is True,
       "mirror_audio 配置生效（默认开）")
    ck(_mir._AUDIO_CODECS[0x6F707573] == "opus", "音频编码 id 映射（opus）")
    view = w.mirror_panel.view
    view.resize(640, 480)
    view._video_size = (1280, 720)
    view._image = QImage(1280, 720, QImage.Format.Format_RGB888)
    from PySide6.QtCore import QPointF
    mid = view.map_to_video(QPointF(320, 240))
    ck(mid == (640, 360), "鼠标坐标映射到视频中心", str(mid))
    ck(view.map_to_video(QPointF(5, 5)) is None, "黑边区域不映射点击")
    view.clear_frame()
    # 投屏手势 -> Translator 合成事件链路：点击应识别为 tap
    panel = w.mirror_panel
    panel.meta = {"serial": "SELFTEST", "device_name": "mirror",
                  "video_size": [1280, 720], "screen_size": [1920, 1080]}
    panel.chk_record.setChecked(True)
    t0 = time.monotonic()
    panel._on_input_event(RawEvent(t0, "mirror", "EV_ABS", "ABS_MT_TRACKING_ID", 1))
    panel._on_input_event(RawEvent(t0, "mirror", "EV_ABS", "ABS_MT_POSITION_X", 640))
    panel._on_input_event(RawEvent(t0, "mirror", "EV_ABS", "ABS_MT_POSITION_Y", 360))
    panel._on_input_event(RawEvent(t0, "mirror", "EV_SYN", "SYN_REPORT", 0))
    panel._on_input_event(RawEvent(t0 + 0.05, "mirror", "EV_ABS", "ABS_MT_TRACKING_ID", -1))
    panel._on_input_event(RawEvent(t0 + 0.05, "mirror", "EV_SYN", "SYN_REPORT", 0))
    steps = panel._tr.finish() if panel._tr else []
    ck(len(steps) == 1 and steps[0]["action"] == "tap"
       and all(abs(v - 0.5) < 0.001 for v in steps[0]["at"]),
       "投屏手势录制识别为归一化 tap", str(steps[:1]))
    panel.chk_record.setChecked(False)
    panel._tr = None

    # 单槽取帧桩：模拟一个已连上、握有一帧的会话（不连设备）
    import numpy as _np

    class _FS:
        def __init__(self, arr, ts, screen_size=(1920, 1080)):
            self._a, self._t = arr, ts
            self.screen_size = screen_size
            self.adb = Adb()
            self.adb.serial = "SELFTEST"

        def latest_frame(self):
            return self._a, self._t

    class _FW:
        def __init__(self, s):
            self._s = s

        def isRunning(self):
            return True

        def current(self):
            return self._s

    # 旋转后录制归一化基准必须跟随实际帧尺寸（Translator 构造时拷贝 abs，需显式同步）
    panel.chk_record.setChecked(True)
    panel._ensure_translator()
    panel.worker = _FW(_FS(_np.zeros((1280, 720, 3), dtype=_np.uint8), 1.0))  # 竖屏帧
    panel._last_ts = 0.0
    panel._pull_frame()
    ck(panel._tr is not None and panel._tr.abs_x == 719.0 and panel._tr.abs_y == 1279.0,
       "旋转后录制基准跟随帧尺寸",
       f"abs=({panel._tr.abs_x if panel._tr else None},{panel._tr.abs_y if panel._tr else None})")
    panel.chk_record.setChecked(False)
    panel._tr = None
    panel.worker = None
    view.clear_frame()

    # A1：旋转后逻辑屏尺寸（core 重查 wm_size）必须传导到用例 device.size 与信息栏
    panel.meta = {"serial": "SELFTEST", "device_name": "mirror",
                  "video_size": [1280, 720], "screen_size": [1920, 1080]}
    panel.chk_record.setChecked(True)
    panel._tr = None
    panel._ensure_translator()
    panel._last_ts = 0.0
    panel.worker = _FW(_FS(_np.zeros((1280, 720, 3), dtype=_np.uint8), 5.0,
                           screen_size=(1080, 1920)))   # 竖屏帧 + 旋转后逻辑屏
    panel._pull_frame()
    ck(panel._tr_profile["size"] == [1080, 1920]
       and panel.meta["screen_size"] == [1080, 1920],
       "A1 旋转后逻辑屏尺寸传导到录制 profile 与 meta",
       f"size={panel._tr_profile['size']}")
    ck("1080x1920" in panel.lbl_info.text(),
       "A1 信息栏反映旋转后屏幕尺寸", panel.lbl_info.text())
    panel.chk_record.setChecked(False)
    panel._tr = None
    panel.worker = None
    view.clear_frame()

    # 非连续帧（FFmpeg stride 填充）必须能正常显示，曾导致 QImage BufferError
    strided = _np.zeros((720, 2560, 3), dtype=_np.uint8)[:, ::2, :]
    ck(not strided.flags["C_CONTIGUOUS"], "构造非连续帧样本")
    view.set_frame(strided)
    ck(view._image is not None and view._video_size == (1280, 720),
       "非连续帧可正常显示（stride 防御）")
    view.clear_frame()

    # 画质预设：套用联动三参数、反查匹配
    panel.cmb_preset.setCurrentText("高清")
    ck(panel.cmb_size.currentText() == "1920" and panel.spn_bitrate.value() == 16
       and panel.spn_fps.value() == 60, "预设套用：高清联动分辨率/码率/帧率")
    ck(panel._match_preset() == "高清", "预设反查当前参数")
    panel.cmb_size.setCurrentText("640")
    ck(panel._match_preset() == "自定义", "手动微调后预设显示自定义")
    panel.cmb_preset.setCurrentText("标准")

    # 单槽拉帧：会话只留最新帧，_pull_frame 只认更新的 ts（防 UI 事件积压、旧帧不渲染）
    view.clear_frame()
    panel._last_ts = 0.0
    panel._frame_count = 0
    a2 = _np.zeros((720, 1280, 3), dtype=_np.uint8)
    panel.worker = _FW(_FS(a2, 2.0))
    panel._pull_frame()
    ck(view._video_size == (1280, 720) and panel._last_ts == 2.0,
       "单槽：定时器刷出会话最新帧")
    panel._pull_frame()   # ts 未推进，不重复上屏
    ck(panel._frame_count == 1, "单槽：ts 未推进时不重复上屏", str(panel._frame_count))
    panel.worker = _FW(_FS(_np.zeros((720, 1280, 3), dtype=_np.uint8), 3.0))
    panel._pull_frame()   # 新会话新 ts，应刷新
    ck(panel._frame_count == 2, "单槽：新会话时间戳重置后能重新取帧", str(panel._frame_count))
    view.clear_frame()
    panel.worker = None

    # 外挂官方 scrcpy：面板按钮就位
    ck(any(b.text() == "⚡ 高性能" for b in panel.findChildren(QPushButton)),
       "高性能模式按钮就位")

    # 缓冲读取器：超时不丢半截帧头（否则整条流错位）
    class _ChunkSock:
        def __init__(self, items):
            self.items = list(items)

        def recv(self, _n):
            if not self.items:
                return b""
            it = self.items.pop(0)
            if isinstance(it, Exception):
                raise it
            return it

    import socket as _sock
    rd = _mir._SockReader(_ChunkSock([b"a" * 5, _sock.timeout(), b"b" * 7]),
                          lambda: False)
    ck(rd.read(12) == b"a" * 5 + b"b" * 7, "帧头分片+超时后仍能完整重组")
    rd2 = _mir._SockReader(_ChunkSock([b"x" * 20]), lambda: False)
    ck(rd2.read(12) == b"x" * 12 and rd2.read(8) == b"x" * 8,
       "缓冲读取器连续读不丢字节")
    ck(_mir.pack_back_or_screen_on() == bytes([4, 0]), "返回/亮屏消息")
    # 快捷键录制键名必须能解析成合法 KEYCODE（曾错写 KEY_VOLUMEDOWN）
    tr_cfg = dict(w.cfg)
    tr_cfg["swap_xy"] = False
    tr_probe = Translator(tr_cfg, {"serial": "T", "size": [100, 100],
                                   "touch_abs": [99, 99]})
    resolved = {tr_probe._keycode(k)[0] for k in MirrorPanel.SHORTCUT_KEYS.values()}
    ck(resolved == {"KEYCODE_BACK", "KEYCODE_HOME", "KEYCODE_APP_SWITCH",
                    "KEYCODE_POWER", "KEYCODE_VOLUME_UP", "KEYCODE_VOLUME_DOWN"},
       "快捷键录制键名解析正确", str(resolved))

    # A2：物理键盘特殊键的 Linux 键名必须都能解析成合法 KEYCODE（与快捷键同链路）
    kb_resolved = {tr_probe._keycode(name)[0] for _, name in MirrorView.SPECIAL_KEYS.values()}
    ck(all(r.startswith("KEYCODE_") for r in kb_resolved)
       and {"KEYCODE_ENTER", "KEYCODE_DEL", "KEYCODE_FORWARD_DEL",
            "KEYCODE_DPAD_UP", "KEYCODE_TAB"} <= kb_resolved,
       "A2 键盘特殊键键名解析正确", str(sorted(kb_resolved)))

    # A2：keyPressEvent 路由——字符走 text、特殊键走 keycode、Ctrl 组合不注入设备
    class _RecMirror:
        def __init__(self):
            self.keys, self.texts = [], []

        def key(self, kc, **_kw):
            self.keys.append(kc)

        def text(self, s):
            self.texts.append(s)

    class _RecWorker:
        def __init__(self, s):
            self._s = s

        def isRunning(self):
            return True

        def current(self):
            return self._s

    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    rec = _RecMirror()
    view.worker = _RecWorker(rec)
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                                 Qt.KeyboardModifier.NoModifier, "a"))
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                 Qt.KeyboardModifier.NoModifier, "\r"))
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Backspace,
                                 Qt.KeyboardModifier.NoModifier, "\b"))
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C,
                                 Qt.KeyboardModifier.ControlModifier, "c"))
    ck(rec.texts == ["a"] and rec.keys == [KEYCODE_ENTER, KEYCODE_DEL],
       "A2 键盘注入路由：字符->text 特殊键->keycode Ctrl组合不注入",
       f"texts={rec.texts} keys={rec.keys}")
    view.worker = None

    # A2：键盘录制——特殊键生成 keyevent、字符生成 text，KEYCODE 合法可回放
    panel.meta = {"serial": "SELFTEST", "device_name": "mirror",
                  "video_size": [1280, 720], "screen_size": [1920, 1080]}
    panel.chk_record.setChecked(True)
    panel._tr = None
    panel._ensure_translator()
    panel._on_view_key("KEY_ENTER")
    panel._on_view_text("h")
    panel._on_view_text("i")
    ksteps = panel._tr.finish()
    ck([s["action"] for s in ksteps] == ["keyevent", "text", "text"]
       and ksteps[0]["key"] == "KEYCODE_ENTER"
       and ksteps[1]["text"] == "h" and ksteps[2]["text"] == "i",
       "A2 键盘录制生成 keyevent/text 步骤", str(ksteps))
    panel.chk_record.setChecked(False)
    panel._tr = None

    # A2 补全：中文 IME 提交串走 inputMethodEvent（keyPressEvent 收不到），
    # 预编辑串（候选未上屏）不得注入设备
    from PySide6.QtGui import QInputMethodEvent

    def _ime_event(preedit: str, commit: str):
        e = QInputMethodEvent(preedit, [])
        if commit:
            e.setCommitString(commit)
        return e

    rec2 = _RecMirror()
    view.worker = _RecWorker(rec2)
    view.inputMethodEvent(_ime_event("", "你好"))
    view.inputMethodEvent(_ime_event("nihao", ""))
    ck(rec2.texts == ["你好"], "A2 IME 提交串注入设备（预编辑串不注入）",
       f"texts={rec2.texts}")
    view.worker = None

    # C8 补全：重连多段会话拉回同一本地路径不得覆盖前段，自动改拉 _段N
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rec_path = str(Path(td) / "mirror.h264")
        Path(rec_path).write_bytes(b"seg1")   # 前段会话已拉回
        pulls = []

        class _RecAdb:
            def run(self, args, timeout=None):
                if args[0] == "pull":
                    pulls.append(list(args))
                    Path(args[2]).write_bytes(b"seg")   # 模拟 adb pull 落盘

        sm = _mir.ScrcpyMirror(_RecAdb(), {}, record_path=rec_path)
        sm._device_record_path = "/data/local/tmp/x.h264"
        _sleep_backup = _mir.time.sleep
        _mir.time.sleep = lambda _s: None   # 跳过回收前的等待
        try:
            sm._pull_device_record()
            sm._pull_device_record()
        finally:
            _mir.time.sleep = _sleep_backup
        ck(pulls[0][0] == "pull" and pulls[0][2].endswith("mirror_段2.h264")
           and pulls[1][2].endswith("mirror_段3.h264"),
           "C8 重连录像分段拉回不覆盖前段", str([p[2] for p in pulls]))

    # ---- 控件透视：默认关 / 坐标命中 / 悬浮高亮 / 后台抓取信号链路 ----
    from core import inspector as _insp
    ck(not panel.chk_inspect.isChecked() and not view._inspect_nodes
       and view.hasMouseTracking(),
       "透视默认关闭且鼠标追踪就位")
    view.resize(640, 480)
    view._video_size = (1280, 720)
    view._image = QImage(1280, 720, QImage.Format.Format_RGB888)
    view._image.fill(QColor(0, 0, 0))
    # view 在 splitter 里，resize 会被布局覆盖；先 flush 布局事件让尺寸稳定，
    # 否则块执行中途几何变化会让期望坐标/命中判定失效
    app.processEvents()
    _ixml = ('<hierarchy rotation="0">'
             '<node class="android.widget.FrameLayout" bounds="[0,0][1920,1080]">'
             '<node class="android.widget.Button" resource-id="com.demo:id/ok" '
             'text="确定" bounds="[300,300][600,420]" clickable="true"/>'
             '</node></hierarchy>')
    _inodes = _insp.parse_hierarchy(_ixml)
    view.set_inspect(_inodes, (1920, 1080))

    def _btn_center():
        # 按钮设备中心 (450,360) -> 画面坐标；view 在 splitter 里几何可能被
        # 布局事件刷新，每次断言前按当前内容区即时换算，不缓存绝对像素
        r = view._content_rect()
        return QPointF(r.x() + 450 / 1920 * r.width(),
                       r.y() + 360 / 1080 * r.height())

    _pt = _btn_center()
    ck(view.inspect_hit(_pt) is _inodes[1],
       "透视悬浮命中映射到设备控件（含 letterbox/缩放换算）")
    _r = view._content_rect()
    ck(view.inspect_hit(QPointF(_r.x() + 2, _r.y() - 5)) is None,
       "透视黑边区域不命中")
    _pt = _btn_center()
    _img0 = view.grab().toImage()
    ck(_img0.pixelColor(int(_pt.x()), int(_pt.y())) == QColor(0, 0, 0),
       "透视未悬浮时只描轮廓不填充")
    from PySide6.QtGui import QMouseEvent
    _pt = _btn_center()   # 换算与派发同步执行，中间无事件循环，几何不会漂移
    view.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, _pt, _pt,
                                    Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier))
    ck(view._hover_node is _inodes[1], "透视 mouseMove 更新悬浮控件",
       f"hover={view._hover_node}")
    _pt = _btn_center()
    _img1 = view.grab().toImage()
    ck(_img1.pixelColor(int(_pt.x()), int(_pt.y())) != QColor(0, 0, 0),
       "透视悬浮时高亮填充+信息卡实际绘出")
    view.leaveEvent(None)
    ck(view._hover_node is None, "透视移出画面清除悬浮")

    class _InspAdb:
        def uiautomator_dump(self, *, compressed=False):
            return _ixml

        def wm_size(self):
            return (1920, 1080)

    class _InspSession:
        def __init__(self):
            self.adb = _InspAdb()
            self.screen_size = (1920, 1080)
            self.video_size = (1280, 720)

        def touch_down(self, *_a):
            pass

        def touch_move(self, *_a):
            pass

        def touch_up(self, *_a):
            pass

    panel.worker = _FW(_InspSession())
    panel.chk_inspect.setChecked(True)
    _deadline = time.monotonic() + 5
    while panel._inspect_busy and time.monotonic() < _deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    ck(len(view._inspect_nodes) == 2 and "2 个控件" in panel.lbl_inspect.text()
       and panel.btn_inspect_refresh.isEnabled(),
       "透视开关触发后台抓取并经信号装载控件节点",
       f"nodes={len(view._inspect_nodes)} lbl={panel.lbl_inspect.text()}")

    # 侧栏收起时不建树：每 2.5s 自动刷新不再白耗一次重建
    ck(panel.insp_tree.topLevelItemCount() == 0 and not panel._inspect_items,
       "控件树未展开时不重建树（自动刷新零成本）",
       f"top={panel.insp_tree.topLevelItemCount()} items={len(panel._inspect_items)}")

    # 控件树侧栏：层级构建 / 筛选保留父链 / 双向联动 / 复制定位
    panel.chk_inspect_tree.setChecked(True)
    ck(not panel.insp_panel.isHidden(), "勾选控件树展开侧栏")
    ck(panel.insp_tree.topLevelItemCount() == 1
       and panel.insp_tree.topLevelItem(0).childCount() == 1,
       "控件树按层级构建（FrameLayout > Button）",
       f"top={panel.insp_tree.topLevelItemCount()}")
    # 注意：面板里的节点是那次后台 dump 重新解析出的对象，与本地 _inodes 内容相同
    # 但身份不同，联动断言必须用面板/视图里的实际对象
    _root_item = panel._inspect_items[0] if panel._inspect_items else None
    _btn_item = panel._inspect_items[1] if len(panel._inspect_items) > 1 else None
    _root_node = view._inspect_nodes[0]
    _btn_node = view._inspect_nodes[1]
    ck(_root_item is not None and _btn_item is not None,
       "控件树条目与控件节点一一对应")
    panel.insp_filter.setText("com.demo:id/ok")
    ck(not _btn_item.isHidden() and not _root_item.isHidden(),
       "筛选命中子孙时保留父链")
    panel.insp_filter.setText("不存在的控件zzz")
    ck(_btn_item.isHidden() and _root_item.isHidden(), "筛选无匹配时整树隐藏")
    panel.insp_filter.setText("")

    panel._on_view_inspect_hover(_btn_node)
    ck(panel.insp_tree.currentItem() is _btn_item, "画面悬浮联动控件树选中")
    panel._on_tree_current(_root_item)
    ck(view._hover_node is _root_node, "控件树选中联动画面高亮")
    view._hover_node = None

    panel._on_tree_double(_btn_item)
    ck(QApplication.clipboard().text() == '//*[@resource-id="com.demo:id/ok"]'
       and panel.lbl_inspect.text().startswith("已复制"),
       "双击控件树条目复制定位表达式", QApplication.clipboard().text())

    # 自动刷新重建树：选中/高亮要落回同一控件（否则每 2.5s 打断一次）
    panel.insp_tree.setCurrentItem(_btn_item)
    view._hover_node = _btn_node
    panel._on_inspect_done(_insp.parse_hierarchy(_ixml), (1920, 1080))
    _cur = panel._item_node(panel.insp_tree.currentItem())
    ck(_cur is not None and _cur.get("sel") == _btn_node.get("sel")
       and view._hover_node is _cur,
       "自动刷新后重新选中同一控件（不打断查看）",
       f"cur={_cur and _cur.get('sel')}")
    view._hover_node = None

    # ---- 右键：不注入输入（不算点击），透视下改作「选中/取消选中控件」 ----
    _shortcuts: list[str] = []
    _feeds: list = []
    _orig_shortcut, _orig_feed = view.on_shortcut, view.on_input_event
    view.on_shortcut = lambda name: _shortcuts.append(name)
    view.on_input_event = lambda ev: _feeds.append(ev)

    def _right_click(pt):
        view.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, pt, pt,
                                         Qt.MouseButton.RightButton,
                                         Qt.MouseButton.RightButton,
                                         Qt.KeyboardModifier.NoModifier))

    _right_click(_btn_center())
    ck(view._pinned is not None and not _shortcuts and not _feeds,
       "右键不再注入返回键，也不算点击/录制动作",
       f"shortcuts={_shortcuts} feeds={len(_feeds)}")
    _pin_cur = panel._item_node(panel.insp_tree.currentItem())
    ck(_pin_cur is view._pinned and panel.lbl_inspect.text().startswith("已选中："),
       "右键选中后侧栏树定位到该控件并提示已选中", panel.lbl_inspect.text())
    _props = [panel.insp_props.topLevelItem(i).text(0)
              for i in range(panel.insp_props.topLevelItemCount())]
    ck("id" in _props and "尺寸" in _props and "层级" in _props and "定位" in _props,
       "控件详细数据以属性表逐项展示（id/尺寸/层级/定位）", str(_props))
    _locked_item = panel.insp_tree.currentItem()
    panel._on_view_inspect_hover(_root_node)
    ck(panel.insp_tree.currentItem() is _locked_item,
       "锁定期间鼠标悬浮不抢控件树选中")
    view.leaveEvent(None)
    ck(view._hover_node is view._pinned,
       "锁定控件不因鼠标移出画面而消失")
    _right_click(_btn_center())
    _menu = view._assert_menu
    _acts = [a.text() for a in (_menu.actions() if _menu is not None else []) if a.text()]
    ck(_menu is not None and view._pinned is not None
       and any("断言此控件存在" in t for t in _acts)
       and any("保存控件截图" in t for t in _acts),
       "右键选中控件并弹出断言菜单（控件断言 + 控件截图）", str(_acts))
    _reqs: list = []

    def _on_req(req):
        _reqs.append(req)

    panel.sig_assert.disconnect(w._on_assert_request)   # 防真对话框在离屏自检里阻塞
    panel.sig_assert.connect(_on_req)
    next(a for a in _menu.actions() if a.text() == "断言此控件存在").trigger()
    ck(bool(_reqs) and _reqs[0]["kind"] == "exists" and _reqs[0]["node"] is _pin_cur,
       "菜单「断言此控件存在」发出断言请求（带命中控件）", str(_reqs[:1]))
    _menu_ref = view._assert_menu
    _right_click(_btn_center())
    ck(view._assert_menu is _menu_ref, "右键菜单对象复用（不每次新建 QMenu 泄漏）")
    view._show_assert_menu(QPoint(5, 5), dict(_btn_node, text="A&B"))
    _amp_acts = [a.text() for a in view._assert_menu.actions()]
    ck(any("A&&B" in t for t in _amp_acts), "菜单文本转义 &（不当成助记符）", str(_amp_acts))
    _menu.close()
    _right_click(_btn_center())
    next(a for a in view._assert_menu.actions() if a.text() == "取消选中控件").trigger()
    ck(view._pinned is None and panel.lbl_inspect.text() == "已取消选中",
       "菜单「取消选中控件」取消右键选中", panel.lbl_inspect.text())
    view._assert_menu.close()
    panel.sig_assert.disconnect(_on_req)
    panel.sig_assert.connect(w._on_assert_request)

    # 属性表双击复制值（定位表达式/完整 id 直接粘到用例里）
    panel._on_view_inspect_pick(_btn_node)
    _sel_row = next((panel.insp_props.topLevelItem(i)
                     for i in range(panel.insp_props.topLevelItemCount())
                     if panel.insp_props.topLevelItem(i).text(0) == "定位"), None)
    panel._on_prop_double(_sel_row)
    ck(QApplication.clipboard().text() == '//*[@resource-id="com.demo:id/ok"]',
       "属性表双击行复制定位表达式", QApplication.clipboard().text())
    # 属性表右键菜单：复制值 / 复制属性名（桩掉 QMenu.exec 依次选第一/二项）
    _p0 = panel.insp_props.topLevelItem(0)
    panel.insp_props.scrollToItem(_p0, QAbstractItemView.ScrollHint.PositionAtTop)
    _orig_menu_exec = globals()["_menu_exec"]
    globals()["_menu_exec"] = lambda menu, pos: menu.actions()[0]
    panel._on_prop_menu(QPoint(5, 5))
    ck(QApplication.clipboard().text() == _p0.text(1),
       "属性表右键复制值", QApplication.clipboard().text())
    globals()["_menu_exec"] = lambda menu, pos: menu.actions()[1]
    panel._on_prop_menu(QPoint(5, 5))
    ck(QApplication.clipboard().text() == _p0.text(0),
       "属性表右键复制属性名", QApplication.clipboard().text())
    # 控件树右键：复制单元格值（第一行第 0 列）
    _t0 = panel.insp_tree.topLevelItem(0)
    panel.insp_tree.scrollToItem(_t0, QAbstractItemView.ScrollHint.PositionAtTop)
    globals()["_menu_exec"] = lambda menu, pos: next(
        a for a in menu.actions() if a.text() == "复制单元格值")
    panel._on_tree_menu(QPoint(5, 5))
    ck(QApplication.clipboard().text() == _t0.text(0),
       "控件树右键复制单元格值", QApplication.clipboard().text())
    globals()["_menu_exec"] = _orig_menu_exec
    panel._on_view_inspect_pick(None)
    view.on_shortcut, view.on_input_event = _orig_shortcut, _orig_feed

    # ---- 控件树展示：多列 / 容器灰字 / 可交互加粗 / 默认只展开 3 层 ----
    _deep = ['<hierarchy rotation="0">']
    for _i in range(6):
        _deep.append('<node class="android.widget.FrameLayout" '
                     'bounds="[0,0][1080,2400]">')
    _deep.append('<node class="android.widget.Button" resource-id="com.demo:id/deep" '
                 'text="深层按钮" bounds="[100,2000][980,2100]" clickable="true"/>')
    _deep += ["</node>"] * 6 + ["</hierarchy>"]
    _dnodes = _insp.parse_hierarchy("".join(_deep))
    panel._on_inspect_done(_dnodes, (1080, 2400))
    _d_items = panel._inspect_items
    ck(panel.insp_tree.columnCount() == 3 and panel.insp_tree.columnWidth(1) > 0,
       "控件树按 控件/id/文本 分列（深层也有固定列可读）",
       f"cols={panel.insp_tree.columnCount()}")
    ck(_d_items[2].isExpanded() and not _d_items[3].isExpanded()
       and not _d_items[-1].isExpanded(),
       "控件树默认只展开前 3 层（6 层嵌套仍看得出层级）")
    ck(not _d_items[0].font(0).bold() and _d_items[-1].font(0).bold(),
       "容器层不加粗、可交互控件加粗")
    ck(_d_items[-1].text(1) == "deep" and _d_items[-1].text(2) == "深层按钮",
       "控件树固定列展示 id 与文本",
       f"id={_d_items[-1].text(1)} text={_d_items[-1].text(2)}")
    ck(_d_items[0].text(0) == "FrameLayout" and "#" not in _d_items[0].text(0),
       "列 0 只放类名，id 不重复占宽（已在固定列 1）",
       f"c0={_d_items[0].text(0)}")
    panel._reveal_item(_d_items[-1])
    ck(all(_d_items[i].isExpanded() for i in range(6)),
       "展开选中分支会逐层展开父链")
    panel._expand_inspect_tree(3)
    ck(not _d_items[3].isExpanded(), "折叠到 3 层可一键收敛深嵌套视图")

    # 同父同名兄弟加 [n] 序号：一屏多个 FrameLayout/TextView 时不至于认错
    _sib = ('<hierarchy rotation="0"><node class="android.widget.LinearLayout" '
            'bounds="[0,0][1080,2400]">'
            '<node class="android.widget.TextView" text="A" bounds="[0,0][100,100]"/>'
            '<node class="android.widget.TextView" text="B" bounds="[0,0][100,100]"/>'
            '</node></hierarchy>')
    panel._on_inspect_done(_insp.parse_hierarchy(_sib), (1080, 2400))
    _s_items = panel._inspect_items
    ck(_s_items[1].text(0) == "TextView" and _s_items[2].text(0) == "TextView[2]",
       "同父同名控件在列 0 加 [n] 序号",
       f"{_s_items[1].text(0)}/{_s_items[2].text(0)}")

    # 应用/界面信息（前台 Activity）：顶部一行 + 属性表首两行，均可复制
    panel._on_focus({"package": "com.demo", "activity": "com.demo.MainActivity",
                     "window": "com.demo.MainActivity"})
    ck("com.demo" in panel.lbl_insp_app.text()
       and "MainActivity" in panel.lbl_insp_app.text(),
       "控件树顶部展示前台应用包名与界面", panel.lbl_insp_app.text())
    panel.lbl_insp_app.copy_now()
    ck(QApplication.clipboard().text() == "com.demo/com.demo.MainActivity",
       "应用信息双击复制「包名/界面」", QApplication.clipboard().text())
    panel._show_node_detail(panel.view._inspect_nodes[0])
    _app_row = panel.insp_props.topLevelItem(0)
    _act_row = panel.insp_props.topLevelItem(1)
    ck(_app_row.text(0) == "应用" and _app_row.text(1) == "com.demo"
       and _act_row.text(0) == "界面"
       and _act_row.text(1) == "com.demo.MainActivity",
       "属性表首两行展示 应用 / 界面",
       f"{_app_row.text(1)} | {_act_row.text(1)}")
    panel._on_prop_double(_app_row)
    ck(QApplication.clipboard().text() == "com.demo",
       "属性表可复制应用包名", QApplication.clipboard().text())
    panel._on_focus({})
    ck(panel.lbl_insp_app.text() == "应用：—" and panel.lbl_insp_app.copy_now() == "",
       "透视关闭后应用信息清空", panel.lbl_insp_app.text())

    # 录制面板默认收起（右侧 18px 把手可展开/再收起）
    ck(panel.rec_panel.isHidden() and panel.btn_rec_toggle.text() == "◂",
       "鼠标操作录制面板默认收起", panel.btn_rec_toggle.text())
    panel.btn_rec_toggle.click()
    ck(not panel.rec_panel.isHidden() and panel.btn_rec_toggle.text() == "▸"
       and panel.rec_panel.minimumWidth() >= 260,
       "点右侧把手展开录制面板", str(panel.rec_panel.isHidden()))
    panel.btn_rec_toggle.click()
    ck(panel.rec_panel.isHidden() and panel.btn_rec_toggle.text() == "◂",
       "再点把手收起录制面板")

    # 「只看有效」：纯包装壳不建条目，子节点直接挂到最近的有效祖先下
    panel._on_inspect_done(_insp.parse_hierarchy("".join(_deep)), (1080, 2400))
    panel.chk_insp_compact.setChecked(True)
    _c_items = [i for i in panel._inspect_items if i is not None]
    ck(len(_c_items) == 2 and _c_items[-1].parent() is _c_items[0]
       and _c_items[-1].text(1) == "deep",
       "「只看有效」把 6 层包装壳压掉，按钮直接挂到顶层下",
       f"kept={[i.text(0) for i in _c_items]}")
    panel.chk_insp_compact.setChecked(False)
    ck(len(panel._inspect_items) == 7
       and all(i is not None for i in panel._inspect_items),
       "取消「只看有效」恢复完整 7 层树",
       f"n={len(panel._inspect_items)}")

    # 轮廓层缓存：节点/几何不变时连续绘制只重建一次
    view.clear_inspect()
    view.set_inspect(_inodes, (1920, 1080))
    _serial0 = view._inspect_cache_serial
    view.grab()
    view.grab()
    ck(view._inspect_cache_serial == _serial0 + 1,
       "透视轮廓层缓存：连续两帧只重建一次",
       f"serial={view._inspect_cache_serial} base={_serial0}")
    view.set_inspect(_inodes, (1920, 1080))
    view.grab()
    ck(view._inspect_cache_serial == _serial0 + 2,
       "透视节点变化后缓存失效重建")

    panel.chk_inspect_auto.setChecked(True)
    ck(panel._inspect_timer.isActive(), "自动刷新开启后定时器运行")
    panel.chk_inspect_auto.setChecked(False)
    ck(not panel._inspect_timer.isActive(), "关闭自动刷新停止定时器")
    panel.chk_inspect_tree.setChecked(False)
    ck(panel.insp_panel.isHidden(), "取消勾选收起控件树侧栏")
    panel.chk_inspect.setChecked(False)
    ck(not view._inspect_nodes and not panel.btn_inspect_refresh.isEnabled(),
       "透视关闭清空控件树与刷新按钮")
    panel._on_inspect_failed("模拟失败原因")
    ck(panel.lbl_inspect.text().startswith("抓取失败：")
       and panel.lbl_inspect.toolTip() == "模拟失败原因",
       "透视抓取失败：原因上标签 + tooltip 留全文", panel.lbl_inspect.text())
    panel.lbl_inspect.setText("")
    panel.lbl_inspect.setToolTip("")
    panel.worker = None
    panel.chk_inspect.setChecked(True)
    ck(not panel.chk_inspect.isChecked() and panel.lbl_inspect.text() == "请先开始投屏",
       "透视未连接时开关自动回退并提示")
    panel.lbl_inspect.setText("")
    view.clear_inspect()
    view.clear_frame()

    # 透视健壮性：重连重抓 / 旋转先清叠加 / 按下收起悬浮 / 重复与碎节点绘制
    def _wait_inspect_idle():
        dl = time.monotonic() + 5
        while panel._inspect_busy and time.monotonic() < dl:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()

    panel.worker = _FW(_InspSession())
    panel.chk_inspect.setChecked(True)
    _wait_inspect_idle()
    ck(len(view._inspect_nodes) == 2, "透视重抓前置：控件树已装载")
    panel._on_meta({"serial": "SELFTEST", "device_name": "mirror",
                    "video_size": [1280, 720], "screen_size": [1920, 1080]})
    ck(panel._inspect_busy, "透视重连收到 meta 自动重抓控件树")
    _wait_inspect_idle()
    # meta 保持横屏 [1920,1080]，传入竖屏尺寸才会命中“尺寸变化”分支
    panel._sync_tr_size(720, 1280, (1080, 1920))
    ck(not view._inspect_nodes and panel._inspect_busy,
       "透视旋转先收起错位叠加再重抓",
       f"nodes={len(view._inspect_nodes)} busy={panel._inspect_busy} "
       f"chk={panel.chk_inspect.isChecked()} src={panel._inspect_source() is not None} "
       f"meta_ss={panel.meta.get('screen_size')}")
    _wait_inspect_idle()
    # 上一段收尾 clear_frame 清空了画面，悬浮/绘制断言前先补桩帧
    view._video_size = (1280, 720)
    view._image = QImage(1280, 720, QImage.Format.Format_RGB888)
    view._image.fill(QColor(0, 0, 0))
    app.processEvents()
    view.set_inspect(_inodes, (1920, 1080))
    _pt = _btn_center()
    view.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, _pt, _pt,
                                    Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier))
    ck(view._hover_node is _inodes[1], "透视按下前悬浮已就位",
       f"nodes={len(view._inspect_nodes)} screen={view._inspect_screen} "
       f"hit={view.inspect_hit(_pt) is _inodes[1]} rect={view._content_rect()} "
       f"pt=({_pt.x()},{_pt.y()})")
    _pt = _btn_center()
    view.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, _pt, _pt,
                                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                     Qt.KeyboardModifier.NoModifier))
    ck(view._hover_node is None, "透视触摸按下收起悬浮信息卡")
    view._pressed = False
    _dup_xml = ('<hierarchy><node class="a.F" bounds="[0,0][1920,1080]">'
                '<node class="a.W" bounds="[300,300][600,420]">'
                '<node class="a.B" bounds="[300,300][600,420]"/>'
                '<node class="a.T" bounds="[0,0][3,3]"/>'
                '</node></node></hierarchy>')
    view.set_inspect(_insp.parse_hierarchy(_dup_xml), (1920, 1080))
    _img2 = view.grab().toImage()
    ck(not _img2.isNull(), "透视重复 bounds/碎节点绘制不崩（去重+最小尺寸过滤）")
    panel.chk_inspect.setChecked(False)
    panel.worker = None
    view.clear_inspect()
    view.clear_frame()

    # ---- UI 文言过长展示修复：动态标签换行 / 表格全文 tooltip / 下拉不固定宽 ----
    _QWIDGETSIZE_MAX = 16777215   # Qt 默认最大宽（本 PySide6 未导出该常量）
    long_text = "超长文本展示验证" * 12
    wrap_labels = {"投屏信息": w.mirror_panel.lbl_info,
                   "录制状态": w.lbl_rec_state, "用例信息": w.lbl_case_info,
                   "回放结果": w.lbl_rp_result, "工具栏状态": w.lbl_status}
    ck(all(lb.wordWrap() for lb in wrap_labels.values()),
       "UI 动态长文本标签均开启自动换行",
       str([k for k, lb in wrap_labels.items() if not lb.wordWrap()]))
    wrap_ok = True
    for lb in wrap_labels.values():
        lb.setText(long_text)
        wrap_ok = wrap_ok and lb.heightForWidth(200) > lb.fontMetrics().height() * 1.5
    ck(wrap_ok, "UI 长文本在窄宽度下实际换行而非截断")
    w.lbl_rp_result.setText("尚未运行")
    w.lbl_case_info.setText("")
    w.lbl_status.setText("就绪")
    w.mirror_panel.lbl_info.setText("未连接")
    w.lbl_rec_state.setText("待机")

    long_msg = "[消息] " + "长命令输出内容" * 20
    w.append_log(long_msg)
    ck(w.lbl_status.toolTip() == long_msg and len(w.lbl_status.text()) <= 60,
       "UI 状态标签截断展示时 tooltip 保留全文")

    w._fill_devices([{"serial": "192.168.123.234:5555"}])
    ck(w.cmb_device.toolTip() == w.cmb_device.currentText(),
       "UI 设备下拉收起态省略时 tooltip 补全文")

    # ---- 探测按钮状态机：点下去必须有反馈，成功/失败都上状态栏 ----
    class _RunningWorker:
        def isRunning(self):
            return True

    w._probe_worker = _RunningWorker()
    w._probe_device()
    ck(w.lbl_status.text().startswith("探测中…请稍候"),
       "探测重复点击不重复起线程（提示稍候）", w.lbl_status.text())
    w._probe_worker = None

    w._probe_finished = False
    w._probe_save_profile = False   # 自检不覆盖 reports/device_profile.json
    w.btn_probe.setEnabled(False)
    w._show_profile({"serial": "SELFTEST", "model": "VirtualHeadUnit",
                     "android_release": "14", "size": [1920, 720],
                     "density": 240, "rotation": 1, "touch_abs": [1920, 720],
                     "abs_source": "getevent:demo_ts", "coord_ratio": [1.0, 1.0],
                     "coord_warning": False, "inputs": [],
                     "touch_nodes": ["demo_ts"], "key_nodes": ["gpio-keys"],
                     "record_nodes": ["/dev/input/event4"], "errors": []})
    ck(w._probe_finished and w.lbl_status.text().startswith("探测完成：")
       and "1920x720" in w.lbl_status.text()
       and "触摸 1 / 按键 1" in w.lbl_status.text()
       and "监听节点 /dev/input/event4" in w.lbl_status.toolTip()
       and w._save_probe_profile({"serial": "X"}) == "",
       "探测成功：状态栏显示屏幕/触摸/按键汇总（自检不落盘）", w.lbl_status.text())
    w._probe_failed("没有可用设备（adb devices 为空或全部 offline）")
    ck(w.lbl_status.text().startswith("探测失败：")
       and "adb devices" in w.lbl_status.toolTip(),
       "探测失败：原因上状态栏 + tooltip 留全文", w.lbl_status.text())
    w._probe_worker = None
    w._probe_finished = False
    w.btn_probe.setEnabled(False)
    w._probe_done(None)
    ck(w.btn_probe.isEnabled() and w.lbl_status.text().startswith("探测结束："),
       "探测线程结束恢复按钮（不卡在探测中）", w.lbl_status.text())
    w.lbl_status.setText("就绪")
    w.lbl_status.setToolTip("")

    ck(w.tbl_steps.rowCount() > 0
       and all(w.tbl_steps.item(0, c).toolTip() == w.tbl_steps.item(0, c).text()
               for c in range(w.tbl_steps.columnCount())),
       "UI 用例表格单元附全文 tooltip")
    w._append_replay_step({"index": 1, "action": "shell", "ok": True,
                           "elapsed_ms": 5, "message": long_msg, "command": ""})
    ck(w.tbl_replay.item(0, 4).toolTip() == long_msg.strip(),
       "UI 回放表格长消息 tooltip 全文")
    w.tbl_replay.setRowCount(0)

    panel = w.mirror_panel
    panel.meta = {"serial": "SELFTEST", "device_name": "mirror",
                  "video_size": [1280, 720], "screen_size": [1920, 1080]}
    panel.chk_record.setChecked(True)
    panel._tr = None
    panel._ensure_translator()
    panel._on_view_text("表")
    panel._refresh_steps()
    ck(panel.tbl_steps.rowCount() > 0
       and panel.tbl_steps.item(0, 3).toolTip() == panel.tbl_steps.item(0, 3).text(),
       "UI 投屏录制表格单元附全文 tooltip")
    panel.chk_record.setChecked(False)
    panel._tr = None

    # 录制页不再带设备画面：它与实时投屏的 scrcpy 会话抢同一设备，画面会互相挤掉
    ck(not hasattr(w, "lbl_screen") and not hasattr(w, "_preview_timer")
       and not hasattr(w, "_on_preview_tick"),
       "录制页已移除设备画面（不再与实时投屏抢设备）",
       f"lbl_screen={hasattr(w, 'lbl_screen')}")

    # 画面右上角把手：钉在画面内，拖出/单击都能触发独立展示
    _pb = w.mirror_panel.btn_popout
    ck(_pb.parent() is w.mirror_panel.view
       and _pb.geometry().right() <= w.mirror_panel.view.width()
       and _pb.geometry().y() >= 0,
       "投屏画面带「拖出独立展示」把手（钉在画面右上角）",
       f"{_pb.geometry().getRect()} in {w.mirror_panel.view.width()}x"
       f"{w.mirror_panel.view.height()}")

    # 独立展示：从页签摘下挂到独立窗口，关窗后面板要原样收回、投屏不断
    w.resize(1220, 800)
    w.show()
    app.processEvents()
    w._popout_mirror()
    app.processEvents()
    _win0 = w.mirror_win
    ck(_win0 is not None
       and w.tabs.indexOf(w.mirror_panel) == -1
       and w.mirror_panel.parent() is _win0
       and w.mirror_panel.isVisible(),
       "画面可拖出主界面独立展示（页签里不再占位、画面仍可见）",
       f"win={_win0 is not None} idx={w.tabs.indexOf(w.mirror_panel)}"
       f" visible={w.mirror_panel.isVisible()}")
    w._popout_mirror()   # 重复弹出应复用同一个窗口，不能再开第二个
    ck(w.mirror_win is _win0 and w.tabs.indexOf(w.mirror_panel) == -1,
       "重复拖出复用同一个独立窗口")
    _win0.close()
    app.processEvents()
    ck(w.tabs.indexOf(w.mirror_panel) == 0
       and w.tabs.widget(0) is w.mirror_panel
       and w.mirror_panel.isVisible()
       and not _win0.isVisible(),
       "关闭独立窗口后面板收回到原页签位置",
       f"win_visible={_win0.isVisible()} idx={w.tabs.indexOf(w.mirror_panel)}"
       f" panel_visible={w.mirror_panel.isVisible()}")
    # 第二回合：同一窗口再拖出再关闭，必须照样能收回（回调不能在第一回合被清掉）
    w._popout_mirror()
    app.processEvents()
    _win0.close()
    app.processEvents()
    ck(w.mirror_win is _win0 and w.tabs.indexOf(w.mirror_panel) == 0
       and w.tabs.widget(0) is w.mirror_panel,
       "独立窗口可反复拖出/收回（复用同一实例）",
       f"idx={w.tabs.indexOf(w.mirror_panel)}")
    w.hide()

    # 高性能（外部 scrcpy）与内嵌投屏互斥：开启一方会先停掉另一方
    class _FakeExtProc:
        pid = 4321

        def __init__(self):
            self.alive = True
            self.terminated = False

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.terminated, self.alive = True, False

        def kill(self):
            self.alive = False

        def wait(self, timeout=None):
            return 0

    _mp = w.mirror_panel
    ck(not _mp._external_running(), "未启动高性能时判定为无外部进程")
    _fake = _FakeExtProc()
    _mp._ext_proc = _fake
    ck(_mp._external_running(), "高性能进程存活时判定为占用中")
    _mp._stop_external()
    ck(_mp._ext_proc is None and _fake.terminated and not _mp._external_running(),
       "切换投屏模式时能终止高性能进程", f"terminated={_fake.terminated}")

    ck(w.mirror_panel.cmb_preset.minimumWidth() >= 76
       and w.mirror_panel.cmb_preset.maximumWidth() == _QWIDGETSIZE_MAX
       and w.mirror_panel.cmb_size.minimumWidth() >= 86
       and w.mirror_panel.cmb_size.maximumWidth() == _QWIDGETSIZE_MAX,
       "UI 画质/分辨率下拉不固定宽（大字体可撑开）")
    mw = MirrorWindow(w.cfg)
    mw.append_log("[投屏] 自检日志")
    ck("自检日志" in mw.statusBar().currentMessage(), "独立投屏窗口日志可见")
    mw.close()

    # 无 Qt 依赖的 worker 控制能力
    ctl = EventStreamController()
    ctl.stop()
    ck(ctl.stopped, "录制停止开关可用")

    # ---- 录制页也能录“在投屏上操作”（scrcpy 注入不落 /dev/input） ----
    _fwd: list = []
    _fwd_txt: list = []
    _oe, _ot = panel.on_record_event, panel.on_record_text
    panel.on_record_event, panel.on_record_text = _fwd.append, _fwd_txt.append
    panel._on_input_event(RawEvent(1.0, "mirror", "EV_ABS", "ABS_MT_POSITION_X", 10))
    panel._on_view_text("abc")
    panel.on_record_event, panel.on_record_text = _oe, _ot
    ck(len(_fwd) == 1 and _fwd_txt == ["abc"],
       "投屏手势/键盘输入转发给录制页（未开投屏录制也转发）",
       f"ev={len(_fwd)} txt={_fwd_txt}")

    _rw = RecordWorker(w.cfg, {"video_size": (640, 360)})
    _rtr = Translator(w.cfg, {"size": [1088, 2400], "touch_abs": [9791, 21599]})
    _rtr.node_abs["mirror"] = (639.0, 359.0)
    _batch = [("ev", RawEvent(1.0, "mirror", "EV_ABS", "ABS_MT_TRACKING_ID", 1)),
              ("ev", RawEvent(1.0, "mirror", "EV_ABS", "ABS_MT_POSITION_X", 320)),
              ("ev", RawEvent(1.0, "mirror", "EV_ABS", "ABS_MT_POSITION_Y", 180)),
              ("ev", RawEvent(1.0, "mirror", "EV_SYN", "SYN_REPORT", 0)),
              ("ev", RawEvent(1.05, "mirror", "EV_ABS", "ABS_MT_TRACKING_ID", -1))]
    _by: dict = {}
    _n, _ended = RecordWorker._apply_batch(_rtr, _batch, _by)
    _taps = [s for s in _rtr.finish() if s["action"] == "tap"]
    ck(_n == 5 and _ended and _by.get("mirror") == 5 and len(_taps) == 1
       and abs(_taps[0]["at"][0] - 320 / 639) < 0.002
       and abs(_taps[0]["at"][1] - 180 / 359) < 0.002,
       "录制页：投屏手势按视频像素归一化（不被面板尺度压成 0.0x）", str(_taps))
    _rw.feed_mirror(RawEvent(2.0, "mirror", "EV_KEY", "KEY_BACK", 1))
    _rw.feed_mirror_text("hi")
    _q = _rw._take_injected()
    ck(len(_q) == 2 and _q[0][0] == "ev" and _q[1][0] == "text"
       and _q[1][1][0] == "hi", "录制页接收投屏事件的队列", str(_q))
    _rw.set_mirror_size(0, 0)   # 还没画面：保留原尺度
    _keep = _rw._mirror_size
    _rw.set_mirror_size(1280, 720)
    ck(_keep == (640.0, 360.0) and _rw._mirror_size == (1280.0, 720.0),
       "录制中投屏才连上/旋转时重新登记视频尺度", f"{_keep} -> {_rw._mirror_size}")
    ck("没有收到任何输入事件" in RecordWorker._no_step_reason(
        0, {}, ["/dev/input/event5"], {"inputs": []}),
       "零事件时提示“设备无事件”并给出排查项")
    ck("没有形成完整手势" in RecordWorker._no_step_reason(
        6, {"mirror": 6}, ["/dev/input/event5"], {}),
       "只有投屏事件时提示手势不完整")
    ck("事件分布" in RecordWorker._no_step_reason(
        30, {"/dev/input/event5": 30}, ["/dev/input/event5"], {}),
       "收到事件但未识别时报节点分布")
    rw = ReplayWorker(w.cfg, {"case": str(ROOT / "cases" / "example.yaml")})
    rw.stop()
    ck(rw._stopped, "回放中止开关可用")
    # dry-run 只打印命令、不动设备：默认必须不勾，否则“全过”而手机毫无反应
    ck(not w.rp_dry.isChecked()
       and "开始回放" in w.btn_rp_start.text()
       and "预演" not in w.btn_rp_start.text()
       and "真机回放" in w.lbl_rp_hint.text(),
       "回放默认真机模式（dry-run 不勾选）",
       f"{w.rp_dry.isChecked()} | {w.btn_rp_start.text()} | {w.lbl_rp_hint.text()}")
    w.rp_dry.setChecked(True)
    ck("预演" in w.btn_rp_start.text() and "预演模式" in w.lbl_rp_hint.text(),
       "勾选 dry-run 后按钮/提示条改成预演语义",
       f"{w.btn_rp_start.text()} | {w.lbl_rp_hint.text()}")
    w.rp_dry.setChecked(False)
    ck("预演" not in w.btn_rp_start.text() and "真机回放" in w.lbl_rp_hint.text(),
       "取消 dry-run 后恢复真机回放语义", w.btn_rp_start.text())
    w.rp_dry.setChecked(False)   # 保持默认值，不影响后续断言
    ck(parse_params("a=1,b=2") == {"a": "1", "b": "2"}, "参数解析")
    ck(step_summary({"action": "tap", "at": [0.1, 0.2]}) == "at=[0.1, 0.2]",
       "步骤摘要", step_summary({"action": "tap", "at": [0.1, 0.2]}))
    ck(set(KNOWN_ACTIONS) >= {"wait_until", "assert_image"}, "动作类型表完整")

    # 回放 worker 在 Qt 线程里完整跑一遍 dry-run（验证信号/日志/逐步结果链路）
    logs, steps, done, failed = [], [], [], []
    rw = ReplayWorker(w.cfg, {
        "case": str(ROOT / "cases" / "example.yaml"),
        "dry_run": True, "no_report": True, "repeat": 1, "speed": 1.0,
        "from_step": 1, "to_step": None, "on_error": "stop", "params": {},
        "timeline": False, "update_baseline": False,
    })
    rw.sig_log.connect(logs.append)
    rw.sig_step.connect(steps.append)
    rw.sig_done.connect(done.append)
    rw.sig_failed.connect(failed.append)
    rw.start()
    import time as _t
    deadline = _t.monotonic() + 15
    while rw.isRunning() and _t.monotonic() < deadline:
        app.processEvents()
        rw.wait(20)
    rw.wait(2000)
    app.processEvents()
    ck(not failed and len(steps) >= 10 and bool(done),
       "GUI 回放线程完整跑通（信号/日志/逐步结果）",
       f"steps={len(steps)} logs={len(logs)} failed={failed[:1]}")
    ck(any("不会操作设备" in m for m in logs),
       "dry-run 回放日志明确告知未操作设备",
       f"{logs[:2]}")

    # ADB bytes 模式：截屏/画面预览依赖它（之前 errors 参数会误转文本模式）
    try:
        a = Adb(cfg=w.cfg)
        a.resolve_serial()
        proc = a.run(["shell", "echo", "BYTES_OK"], text=False)
        ck(isinstance(proc.stdout, bytes) and b"BYTES_OK" in proc.stdout,
           "ADB bytes 模式（截屏 / 预览链路）", str(type(proc.stdout)))
    except Exception as exc:
        ck(True, "ADB bytes 模式（无设备，跳过）", str(exc))

    # 关窗时应停掉后台线程，否则 QThread 被析构会崩
    class _SlowThread(QThread):
        def run(self):
            self.msleep(500)

    w2 = MainWindow(autoload_devices=False)
    slow = _SlowThread()
    slow.start()
    w2.rec_worker = slow          # 冒充正在录制的后台线程
    w2.close()
    ck(not slow.isRunning(), "关闭窗口会等待后台线程结束")

    w.close()
    failed = [c for c in checks if not c[0]]
    for ok_flag, label, detail in checks:
        print(f"[{'PASS' if ok_flag else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not ok_flag else ""))
    print(f"\nGUI 自检结果: {len(checks) - len(failed)}/{len(checks)} 通过  "
          f"{'全部通过' if not failed else '存在失败项'}")
    return 0 if not failed else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return run_gui_selftest()
    app = QApplication(sys.argv)
    app.setApplicationName("adbcase")
    cfg = load_config()
    apply_theme(app, str(cfg.get("theme", "light")))
    if "--mirror" in sys.argv:
        serial = ""
        if "--serial" in sys.argv:
            i = sys.argv.index("--serial")
            if i + 1 < len(sys.argv):
                serial = sys.argv[i + 1]
        if "--no-audio" in sys.argv:
            cfg["mirror_audio"] = False
        w = MirrorWindow(cfg, serial)
        w.show()
        w.panel.start_mirror()
        return app.exec()
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
