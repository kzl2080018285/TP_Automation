# -*- coding: utf-8 -*-
"""录制期逐步截图 + 图片基线断言。

- ShotCapture: 按语义动作序号抓屏（异步不阻塞 getevent 事件流，或同步精确模式）
- inject_baselines: 把截图转成用例里的 assert_image 步骤（all / last / none）
- compare_images: Pillow 像素差异比对，支持区域、忽略区、阈值与差异图
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageDraw
    HAS_PILLOW = True
except ImportError:
    Image = ImageChops = ImageDraw = None
    HAS_PILLOW = False


def save_rgb(arr, dest) -> bool:
    """rgb24 ndarray 写 PNG（投屏帧截图链路）；环境缺失或数据异常返回 False。"""
    if not HAS_PILLOW or arr is None or getattr(arr, "ndim", 0) != 3:
        return False
    try:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(arr).save(dest)
        return True
    except Exception:
        return False


DEFAULT_THRESHOLD = 0.02       # 允许的差异像素占比
DEFAULT_PIXEL_TOLERANCE = 24   # 单像素灰度差超过它才算变化
DEFAULT_IGNORE_STATUS_BAR = [0.0, 0.0, 1.0, 0.06]


# --------------------------------------------------------------------------
# 录制期截图
# --------------------------------------------------------------------------
@dataclass
class ShotStat:
    total: int = 0
    saved: int = 0
    failed: int = 0


class ShotCapture:
    """每个语义动作触发一次截图；文件名与动作序号一一对应。

    frame_provider 可选（借鉴 GameScrcpy vision 直接拿画面帧）：
    投屏开启时取最新解码帧，比 screencap 快一个量级且不干扰事件流；
    取不到帧（未投屏/尺寸不符）时自动回退 screencap。
    """

    def __init__(self, adb, base_dir: str | Path, shots_dir_name: str, *,
                 delay_ms: int = 250, mode: str = "async", max_wait_s: float = 10,
                 frame_provider=None):
        self.adb = adb
        self.frame_provider = frame_provider
        self.base_dir = Path(base_dir)
        self.dir_name = shots_dir_name
        self.out_dir = self.base_dir / shots_dir_name
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.delay_ms = max(0, int(delay_ms))
        self.mode = mode if mode in ("async", "sync") else "async"
        self.max_wait_s = max_wait_s
        self.results: dict[int, str] = {}
        self.stat = ShotStat()
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self._q: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        if self.mode == "async":
            self._q = queue.Queue()
            self._thread = threading.Thread(target=self._worker, daemon=True,
                                            name="adbcase-shot")
            self._thread.start()

    def submit(self, ts: float = 0.0) -> int | None:
        """返回一个截图序号；调用方把它挂到对应动作上。

        已 close 后返回 None：录制收尾时 finish() 可能再结算一次未完成手势，
        那次不该再产生截图请求。
        """
        with self._lock:
            if self._closed:
                return None
            self._seq += 1
            seq = self._seq
        self.stat.total += 1
        if self.mode == "sync":
            self._capture(seq)
        else:
            assert self._q is not None
            self._q.put(seq)
        return seq

    def _capture(self, seq: int) -> None:
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000.0)
        rel = f"{self.dir_name}/step_{seq:04d}.png"
        path = self.out_dir / f"step_{seq:04d}.png"
        ok = False
        if self.frame_provider is not None:
            try:
                arr = self.frame_provider()
            except Exception:
                arr = None
            if arr is not None:
                # 送进 PIL 前先拷贝：单槽帧是只读引用，下游可能改写缓冲
                ok = save_rgb(getattr(arr, "copy", lambda: arr)(), path)
        if not ok:
            ok = bool(self.adb.screencap(path))
        with self._lock:
            if ok:
                self.results[seq] = rel
                self.stat.saved += 1
            else:
                self.stat.failed += 1

    def _worker(self) -> None:
        assert self._q is not None
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                break
            try:
                self._capture(item)
            except Exception:  # 单张失败不影响录制主流程
                with self._lock:
                    self.stat.failed += 1
            finally:
                self._q.task_done()

    def close(self) -> dict[int, str]:
        """停止 worker 并等待队列清空，返回 {序号: 相对用例目录的路径}。"""
        with self._lock:
            self._closed = True
        if self._q is not None:
            self._q.put(None)
            if self._thread is not None:
                self._thread.join(timeout=self.max_wait_s)
        with self._lock:
            return dict(self.results)

    @property
    def pending(self) -> int:
        return self._q.qsize() if self._q is not None else 0


# --------------------------------------------------------------------------
# 用例里注入 assert_image 步骤
# --------------------------------------------------------------------------
def inject_baselines(steps: list[dict], shots: dict[int, str], *,
                     policy: str = "all", threshold: float = DEFAULT_THRESHOLD,
                     pixel_tolerance: int = DEFAULT_PIXEL_TOLERANCE,
                     ignore_regions: list | None = None,
                     wait_before_ms: int = 250,
                     desc_prefix: str = "界面基线") -> list[dict]:
    """把带 _shot 序号的动作后面插入 assert_image。

    policy: all=每步 / last=仅最后一步 / none=只保留截图不生成断言
    """
    entries = [(i, s["_shot"]) for i, s in enumerate(steps) if "_shot" in s]
    for s in steps:
        s.pop("_shot", None)
    if policy not in ("all", "last") or not shots:
        return steps

    targets = entries if policy == "all" else entries[-1:]
    inserts: dict[int, dict] = {}
    for i, seq in targets:
        rel = shots.get(seq)
        if not rel:
            continue
        inserts[i] = {
            "action": "assert_image",
            "image": rel,
            "threshold": threshold,
            "pixel_tolerance": pixel_tolerance,
            "wait_before_ms": wait_before_ms,
            "desc": f"{desc_prefix} step_{seq:04d}",
        }
        if ignore_regions:
            # 每步独立拷贝：避免 PyYAML 把共用列表序列化成 &id001 锚点/别名，
            # 也保证用户能单独修改某一步的忽略区域
            inserts[i]["ignore_regions"] = [list(r) for r in ignore_regions]
        # 继承被校验动作的相对时刻，时间轴回放排序时断言仍紧跟该动作
        if "at_ms" in steps[i]:
            inserts[i]["at_ms"] = steps[i]["at_ms"]

    out: list[dict] = []
    for i, s in enumerate(steps):
        out.append(s)
        if i in inserts:
            out.append(inserts[i])
    return out


# --------------------------------------------------------------------------
# 图像比对
# --------------------------------------------------------------------------
def _rect(region, W: int, H: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = [float(v) for v in region]
    return (max(0, min(W, int(round(x1 * W)))),
            max(0, min(H, int(round(y1 * H)))),
            max(0, min(W, int(round(x2 * W)))),
            max(0, min(H, int(round(y2 * H)))))


def compare_images(baseline: str | Path, current: str | Path, *,
                   threshold: float = DEFAULT_THRESHOLD,
                   pixel_tolerance: int = DEFAULT_PIXEL_TOLERANCE,
                   region: list | None = None,
                   ignore_regions: list | None = None,
                   diff_path: str | Path | None = None) -> dict:
    """返回 {ok, diff_ratio, changed_pixels, total_pixels, ...}。

    当前截图尺寸与基线不同时自动缩放到基线尺寸，便于跨分辨率回放。
    """
    if not HAS_PILLOW:
        return {"ok": False, "error": "图像比对需要 Pillow：pip install Pillow",
                "diff_ratio": 1.0}
    bpath, cpath = Path(baseline), Path(current)
    if not bpath.exists():
        return {"ok": False, "error": f"基线图片不存在: {bpath}", "diff_ratio": 1.0}
    if not cpath.exists():
        return {"ok": False, "error": f"当前截图不存在: {cpath}", "diff_ratio": 1.0}

    with Image.open(bpath) as im:
        base = im.convert("RGB")
    with Image.open(cpath) as im:
        cur = im.convert("RGB")
    orig_size = list(cur.size)
    W, H = base.size
    if cur.size != base.size:
        cur = cur.resize(base.size, Image.LANCZOS)

    mask = Image.new("L", (W, H), 0 if region else 255)
    draw = ImageDraw.Draw(mask)
    if region:
        draw.rectangle(_rect(region, W, H), fill=255)
    for r in (ignore_regions or []):
        draw.rectangle(_rect(r, W, H), fill=0)
    total = mask.histogram()[255] or 1

    diff = ImageChops.difference(base, cur).convert("L")
    diff = ImageChops.multiply(diff, mask)
    hist = diff.histogram()
    changed = sum(hist[max(0, int(pixel_tolerance)) + 1:])
    ratio = changed / total
    ok = ratio <= float(threshold)

    saved_diff = ""
    if diff_path is not None:
        p = Path(diff_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        hi = diff.point(lambda v: 255 if v > int(pixel_tolerance) else 0)
        overlay = cur.copy()
        overlay.paste(Image.new("RGB", (W, H), (255, 40, 40)), (0, 0), hi)
        overlay.save(p)
        saved_diff = str(p)

    return {
        "ok": ok,
        "diff_ratio": round(ratio, 6),
        "threshold": float(threshold),
        "changed_pixels": changed,
        "total_pixels": total,
        "baseline_size": [W, H],
        "current_size": orig_size,
        "resized": orig_size != [W, H],
        "diff_image": saved_diff,
    }
