# -*- coding: utf-8 -*-
"""回放引擎：用例 -> ADB 命令 -> 结果报告。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .adb import Adb, AdbError
from .cases import expand_steps, render_params
from .image_baseline import compare_images
from .inspector import check_widget, parse_hierarchy, selector_label
from .probe import probe

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    Image = None
    HAS_PILLOW = False


@dataclass
class StepResult:
    index: int
    action: str
    desc: str = ""
    ok: bool = True
    command: str = ""
    elapsed_ms: int = 0
    message: str = ""
    source: str = ""
    loop: int = 0


@dataclass
class ReplayReport:
    case: str
    dry_run: bool
    device: dict = field(default_factory=dict)
    started: str = ""
    finished: str = ""
    steps: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s["ok"])

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if not s["ok"])

    def to_dict(self) -> dict:
        return {**{k: v for k, v in asdict(self).items() if k != "steps"},
                "passed": self.passed, "failed": self.failed,
                "total": len(self.steps), "steps": self.steps}


class Runner:
    def __init__(self, adb: Adb, cfg: dict, profile: dict | None = None, *,
                 dry_run: bool = False, speed: float | None = None,
                 on_error: str | None = None, report_dir: str | Path | None = None,
                 case_dir: str | Path | None = None,
                 update_baseline: bool = False, timeline: bool = False,
                 frame_provider=None,
                 log=None, on_step=None, stop_callback=None):
        self.adb = adb
        self.cfg = cfg
        self.profile = profile or ({} if dry_run else probe(adb))
        self.dry_run = dry_run
        self.speed = float(speed if speed is not None else cfg.get("speed", 1.0)) or 1.0
        self.on_error = on_error or cfg.get("on_error", "stop")
        self.report_dir = Path(report_dir) if report_dir else Path(__file__).resolve().parent.parent / "reports"
        self.case_dir = Path(case_dir) if case_dir else Path.cwd()
        self.follow_path = bool(cfg.get("follow_path", False))
        self.scale = cfg.get("scale", "stretch")
        self.update_baseline = update_baseline
        # 借鉴 GameScrcpy Replay.cpp: 相对时间轴 + 稳定排序的确定性回放
        self.timeline = timeline
        self.until_timeout_ms = int(cfg.get("until_timeout_ms", 5000))
        self.until_interval_ms = int(cfg.get("until_interval_ms", 250))
        self.on_fail_retry = int(cfg.get("on_fail_retry", 2))
        self.reconnect_attempts = int(cfg.get("reconnect_attempts", 3))
        self.reconnect_backoff_ms = int(cfg.get("reconnect_backoff_ms", 250))
        # 借鉴 GameScrcpy vision：投屏开启时直接取最新解码帧做图像比对，
        # 免掉 screencap 的秒级开销；callable() -> ndarray[H,W,3] | None，
        # 返回 None 时自动回退 screencap
        self.frame_provider = frame_provider
        # GUI 可注入：日志回调、每步结果回调、中止判定；CLI 不传时行为不变
        self._emit_log = log
        self.on_step = on_step
        self.stop_callback = stop_callback

    def _log(self, msg: str = "") -> None:
        if self._emit_log is not None:
            self._emit_log(msg)
        else:
            print(msg)

    # ---- 坐标 ----
    @staticmethod
    def _dim(value, fallback: int) -> int:
        """尺寸字段容错：0 / None / "1920" / 非法值都退回 fallback，避免除零和 tap 0 0。"""
        try:
            v = int(float(value if value is not None else 0))
        except (TypeError, ValueError):
            v = 0
        return v if v > 0 else fallback

    def _denorm(self, point) -> tuple[int, int]:
        try:
            nx, ny = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            raise ValueError(f"坐标应为 [x, y] 两个 0~1 数字: {point!r}") from exc
        size = self.profile.get("size") or []
        W = self._dim(size[0] if len(size) > 0 else 0, 1080)
        H = self._dim(size[1] if len(size) > 1 else 0, 1920)
        if self.scale == "fit":
            src = self.profile.get("source_size") or []
            sw = self._dim(src[0] if len(src) > 0 else 0, W)
            sh = self._dim(src[1] if len(src) > 1 else 0, H)
            k = min(W / sw, H / sh)
            ox, oy = (W - sw * k) / 2, (H - sh * k) / 2
            x, y = nx * sw * k + ox, ny * sh * k + oy
        else:
            x, y = nx * W, ny * H
        return max(0, min(W - 1, int(round(x)))), max(0, min(H - 1, int(round(y))))

    def _sleep(self, ms: float) -> None:
        """分片 sleep：等待期间响应停止，点「停止」不会卡到整个 wait 结束。"""
        if self.dry_run or ms <= 0:
            return
        remain = ms / 1000.0 / self.speed
        while remain > 0:
            if self.stop_callback is not None and self.stop_callback():
                return
            chunk = 0.1 if remain > 0.1 else remain
            time.sleep(chunk)
            remain -= chunk

    @staticmethod
    def _as_int(value, default: int = 0) -> int:
        """None/空串走默认值；其它非法值让 int() 抛错，由步骤级 try 捕获记 FAIL。"""
        if value is None or value == "":
            return int(default)
        return int(value)

    def _grab_frame(self, dest: Path) -> bool:
        """当前画面写 PNG：优先投屏最新解码帧（实时），否则 screencap。"""
        if self.frame_provider is not None and HAS_PILLOW:
            try:
                arr = self.frame_provider()
            except Exception:
                arr = None
            if arr is not None and getattr(arr, "ndim", 0) == 3:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(arr.copy()).save(dest)   # 单槽帧只读，先拷贝
                    return True
                except Exception:
                    pass    # 帧异常不阻断，退回 screencap
        return self.adb.screencap(dest)

    # ---- 单步动作执行（返回命令列表，供 until 重试时复用） ----
    def _perform(self, step: dict, res: StepResult) -> list[str]:
        act = step["action"]
        adb = self.adb
        cmds: list[str] = []

        def call(fn, cmd_str: str, *args, **kwargs):
            cmds.append(cmd_str)
            if not self.dry_run:
                fn(*args, **kwargs)

        if act == "launch":
            pkg = step["package"]
            activity = step.get("activity", "")
            cmd = f"am start -n {pkg}/{activity}" if activity else \
                  f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1"
            call(adb.start_app, cmd, pkg, activity)
        elif act in ("tap", "double_tap"):
            x, y = self._denorm(step["at"])
            n = 2 if act == "double_tap" else 1
            for _ in range(n):
                call(adb.input_tap, f"input tap {x} {y}", x, y)
                self._sleep(100)
        elif act == "long_press":
            x, y = self._denorm(step["at"])
            dur = self._as_int(step.get("duration_ms", 600), 600) or 600
            call(adb.input_swipe, f"input swipe {x} {y} {x} {y} {dur}", x, y, x, y, dur)
        elif act == "swipe":
            x1, y1 = self._denorm(step["from"])
            x2, y2 = self._denorm(step["to"])
            dur = max(1, self._as_int(step.get("duration_ms", 300), 300))
            path = step.get("path") or []
            if self.follow_path and len(path) > 2:
                seg = max(1, int(dur / (len(path) - 1)))
                pts = [self._denorm(p) for p in path]
                pts[0], pts[-1] = (x1, y1), (x2, y2)
                for a, b in zip(pts, pts[1:]):
                    call(adb.input_swipe, f"input swipe {a[0]} {a[1]} {b[0]} {b[1]} {seg}",
                         a[0], a[1], b[0], b[1], seg)
            else:
                call(adb.input_swipe, f"input swipe {x1} {y1} {x2} {y2} {dur}",
                     x1, y1, x2, y2, dur)
        elif act == "keyevent":
            key = str(step["key"])
            call(adb.input_keyevent, f"input keyevent {key}", key)
        elif act == "text":
            txt = str(step["text"])
            call(adb.input_text, f'input text "{txt}"', txt)
        elif act == "wait":
            ms = self._as_int(step.get("ms"), 0)
            cmds.append(f"sleep {ms}ms (speed x{self.speed})")
            self._sleep(ms)
        elif act == "wait_until":
            # 只等条件、不做设备动作；条件由 _execute 的 until 统一处理
            cmds.append("wait_until")
        elif act == "shell":
            cmd = str(step["cmd"])
            cmds.append(f"shell: {cmd}")
            if not self.dry_run:
                adb.shell_raw(cmd, check=bool(step.get("check", False)))
        elif act == "screenshot":
            p = Path(step["path"])
            if not p.is_absolute():
                p = self.report_dir / p
            cmds.append(f"screencap -p > {p}")
            if not self.dry_run and not adb.screencap(p):
                raise AdbError(f"截图失败: {p}")
        elif act in ("assert_text", "assert_not_text"):
            text = str(step["text"])
            cmds.append(f"uiautomator dump | {act} '{text}'")
            if not self.dry_run:
                dump = adb.uiautomator_dump()
                hit = text in dump
                if act == "assert_text" and not hit:
                    raise AssertionError(f"界面未找到文本: {text}")
                if act == "assert_not_text" and hit:
                    raise AssertionError(f"界面仍存在文本: {text}")
        elif act == "assert_widget":
            sel = step.get("selector") or {}
            exp = step.get("expect") or {}
            want = ", ".join(f"{k}={v}" for k, v in exp.items())
            cmds.append(f"uiautomator dump | assert_widget {selector_label(sel)} [{want}]")
            if not self.dry_run:
                nodes = parse_hierarchy(adb.uiautomator_dump())
                ok, msg = check_widget(nodes, sel, exp)
                if not ok:
                    raise AssertionError(msg)
                res.message = msg
        elif act == "assert_image":
            rel = str(step["image"])
            base = Path(rel)
            if not base.is_absolute():
                base = self.case_dir / base
            th = float(step.get("threshold", 0.02) or 0.02)
            tol = self._as_int(step.get("pixel_tolerance", 24), 24)
            wait_ms = self._as_int(step.get("wait_before_ms", 0), 0)
            cmds.append(f"assert_image {base.name} (threshold={th}, tol={tol})")
            if not self.dry_run:
                self._sleep(wait_ms)
                if self.update_baseline:
                    base.parent.mkdir(parents=True, exist_ok=True)
                    if not self._grab_frame(base):
                        raise AdbError(f"刷新基线失败，截图未成功: {base}")
                    res.message = f"已刷新基线 {base.name}"
                    cmds.append(f"UPDATE BASELINE -> {base}")
                else:
                    self.report_dir.mkdir(parents=True, exist_ok=True)
                    cur = self.report_dir / f"_current_{res.index:04d}.png"
                    diff = self.report_dir / f"diff_{res.index:04d}.png"
                    if not self._grab_frame(cur):
                        raise AdbError("当前截图获取失败，无法做基线比对")
                    result = compare_images(
                        base, cur, threshold=th, pixel_tolerance=tol,
                        region=step.get("region"),
                        ignore_regions=step.get("ignore_regions"),
                        diff_path=diff,
                    )
                    if result.get("error"):
                        raise AssertionError(result["error"])
                    ratio = result["diff_ratio"]
                    if not result["ok"]:
                        raise AssertionError(
                            f"界面差异 {ratio:.2%} > 阈值 {th:.2%}；"
                            f"差异图 {result.get('diff_image', '')}"
                        )
                    res.message = f"差异 {ratio:.2%}" + ("（已缩放）" if result["resized"] else "")
        elif act == "assert_activity":
            frag = str(step["contains"])
            cmds.append(f"dumpsys window | assert contains '{frag}'")
            if not self.dry_run:
                focus = adb.current_focus()
                if frag not in focus:
                    raise AssertionError(f"当前焦点不含 {frag!r}: {focus.strip()[:200]}")
        else:
            raise AdbError(f"回放不支持的 action: {act}")
        return cmds

    # ---- until：逐帧轮询条件，超时重做动作 ----
    def _check_condition(self, cond: dict, res: StepResult) -> bool:
        """借鉴 GameScrcpy Action.until() / vision.waitFor：状态来自查询而非固定延时。"""
        needs_dump = "text" in cond or "not_text" in cond
        try:
            dump = self.adb.uiautomator_dump() if needs_dump else ""
        except AdbError:
            # 动画期 dump 常报 could not get idle state：视为本轮未命中
            # 继续轮询到超时，而不是把整个等待判死
            dump = ""
        results: list[bool] = []
        if "text" in cond:
            results.append(str(cond["text"]) in dump)
        if "not_text" in cond:
            results.append(str(cond["not_text"]) not in dump)
        if "activity" in cond:
            results.append(str(cond["activity"]) in (self.adb.current_focus() or ""))
        if "image" in cond or "not_image" in cond:
            neg = "not_image" in cond
            rel = str(cond.get("image") or cond.get("not_image"))
            base = Path(rel)
            if not base.is_absolute():
                base = self.case_dir / base
            self.report_dir.mkdir(parents=True, exist_ok=True)
            cur = self.report_dir / f"_until_{res.index:04d}.png"
            if not self._grab_frame(cur):
                raise AdbError("until 条件检查时截图失败")
            r = compare_images(
                base, cur,
                threshold=float(cond.get("threshold", 0.02)),
                pixel_tolerance=self._as_int(cond.get("pixel_tolerance", 24), 24),
                region=cond.get("region"),
                ignore_regions=cond.get("ignore_regions"),
            )
            if r.get("error"):
                raise AssertionError(r["error"])
            results.append((not r["ok"]) if neg else r["ok"])
        return all(results) if results else True

    def _wait_until(self, cond: dict, res: StepResult) -> tuple[bool, str]:
        if self.dry_run:
            return True, "dry-run"
        timeout = self._as_int(cond.get("timeout_ms", self.until_timeout_ms), self.until_timeout_ms)
        interval = max(10, self._as_int(cond.get("interval_ms", self.until_interval_ms), self.until_interval_ms))
        deadline = time.monotonic() + timeout / 1000.0 / self.speed
        t0 = time.monotonic()
        while True:
            # 停止时要能立刻退出轮询，而不是等满 timeout
            if self.stop_callback is not None and self.stop_callback():
                return False, "用户中止"
            try:
                if self._check_condition(cond, res):
                    return True, f"{int((time.monotonic() - t0) * 1000)}ms"
            except (AssertionError, AdbError) as exc:
                return False, str(exc)
            if time.monotonic() >= deadline:
                return False, f"timeout {timeout}ms"
            time.sleep(interval / 1000.0 / self.speed)

    def _execute(self, step: dict, res: StepResult) -> None:
        cond = step.get("until")
        if not cond:
            res.command = " ; ".join(self._perform(step, res))
            return
        retry = self._as_int(cond.get("retry", 0), 0)
        cmds: list[str] = []
        for attempt in range(retry + 1):
            cmds = self._perform(step, res)
            ok, detail = self._wait_until(cond, res)
            if ok:
                res.message = (res.message + " | " if res.message else "") + f"until OK {detail}"
                res.command = " ; ".join(cmds)
                return
            if detail == "用户中止":
                res.command = " ; ".join(cmds)
                return
            cmds.append(f"until attempt {attempt + 1}: {detail}")
            if attempt < retry:
                res.message = f"until 超时，重做动作 {attempt + 1}/{retry}"
        raise AssertionError(f"until 条件未满足（重试 {retry} 次）: {cond}")

    # ---- 连接健壮性：失败时探活 + 指数退避等待恢复 ----
    @staticmethod
    def _looks_disconnected(msg: str) -> bool:
        keys = ("device not found", "offline", "no devices", "unauthorized",
                "cannot connect", "connection reset", "closed")
        return any(k in (msg or "").lower() for k in keys)

    def _recover_connection(self) -> bool:
        for i in range(max(0, self.reconnect_attempts)):
            time.sleep(min(self.reconnect_backoff_ms * (2 ** i), 2000) / 1000.0)
            try:
                if "ADBCASE_OK" in (self.adb.shell("echo", "ADBCASE_OK") or ""):
                    return True
            except Exception:
                pass
        return False

    # ---- 主流程 ----
    def run(self, case: dict, *, params: dict | None = None,
            from_step: int = 1, to_step: int | None = None,
            save_report: bool = True) -> ReplayReport:
        rendered = render_params(case, params)
        steps = expand_steps(rendered, self.case_dir)
        if self.timeline:
            # 借鉴 GameScrcpy Session.cpp:580 的 stable_sort：
            # 人工编辑打乱顺序后，时间轴模式仍按录制相对时刻确定性重放
            steps = sorted(steps, key=lambda s: s.get("at_ms")
                           if isinstance(s.get("at_ms"), int) else 0)
        total = len(steps)
        start_i = max(1, from_step)
        end_i = min(total, to_step or total)
        if total == 0 or start_i > total or end_i < start_i:
            self._log(f"[WARN] 步骤范围 {start_i}~{end_i} 超出用例总步数 {total}，"
                      f"本次不会执行任何步骤")

        # 目标设备尺寸写进 profile，fit 模式需要录制尺寸做等比换算
        self.profile.setdefault("source_size", (case.get("device") or {}).get("size") or self.profile.get("size"))

        report = ReplayReport(
            case=case.get("name", "unnamed"),
            dry_run=self.dry_run,
            device={k: self.profile.get(k) for k in ("serial", "model", "size", "rotation")},
            started=datetime.now().isoformat(timespec="seconds"),
        )
        self._log(f"[回放] 用例: {report.case} | 步骤 {start_i}~{end_i}/{total} | "
              f"设备: {report.device.get('serial')} {report.device.get('size')} | "
              f"{'更新基线' if self.update_baseline else 'DRY-RUN' if self.dry_run else '时间轴回放' if self.timeline else '实机执行'}")
        t_start = time.monotonic()

        def once(i: int, step: dict) -> StepResult:
            res = StepResult(index=i, action=step.get("action", "?"),
                             desc=step.get("desc", ""), source=step.get("_source", ""),
                             loop=step.get("_loop", 0))
            t0 = time.perf_counter()
            try:
                self._execute(step, res)
            except (AdbError, AssertionError, KeyError, ValueError, TypeError, OSError) as exc:
                res.ok = False
                res.message = str(exc)
            res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return res

        for i, step in enumerate(steps, start=1):
            if self.stop_callback is not None and self.stop_callback():
                self._log("  ... 用户中止回放")
                break
            if i < start_i or i > end_i:
                continue

            # 时间轴模式：按录制时的相对 at_ms 对齐，wait 步骤不再额外 sleep
            if self.timeline:
                at = step.get("at_ms")
                if isinstance(at, int) and not self.dry_run:
                    delay = (t_start + at / 1000.0 / self.speed) - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                if step.get("action") == "wait":
                    res = StepResult(index=i, action="wait", desc=step.get("desc", ""),
                                     command=f"timeline at_ms={at}")
                    report.steps.append(asdict(res))
                    if self.on_step is not None:
                        self.on_step(asdict(res))
                    self._log(f"  [OK  ] #{i:03d} {'wait':<16} {res.desc[:34]:<34} {res.command}")
                    continue

            res = once(i, step)

            # 疑似掉线：探活 + 指数退避等待恢复，恢复后自动重试该步
            if not res.ok and not self.dry_run and self._looks_disconnected(res.message):
                if self._recover_connection():
                    res = once(i, step)

            # 步骤级 on_fail 覆盖全局 on_error
            policy = str(step.get("on_fail") or self.on_error)
            if not res.ok and policy == "retry":
                for _ in range(max(0, self._as_int(step.get("on_fail_retry", self.on_fail_retry), self.on_fail_retry))):
                    res = once(i, step)
                    if res.ok:
                        break

            report.steps.append(asdict(res))
            if self.on_step is not None:
                self.on_step(asdict(res))
            flag = "OK  " if res.ok else "FAIL"
            extra = f" | {res.message}" if res.message else ""
            self._log(f"  [{flag}] #{i:03d} {res.action:<16} {res.desc[:34]:<34} "
                  f"{res.command[:60]}{extra}")
            if not res.ok and policy in ("stop", "abort", "retry"):
                self._log(f"  ... 已按 on_fail={policy} 中止回放")
                break

        report.finished = datetime.now().isoformat(timespec="seconds")
        self._log(f"[结果] 通过 {report.passed} / 失败 {report.failed} / 共 {len(report.steps)} 步")
        if save_report:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in report.case)[:40]
            out = self.report_dir / f"replay_{safe}_{stamp}.json"
            out.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            self._log(f"[报告] {out}")
        return report
