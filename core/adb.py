# -*- coding: utf-8 -*-
"""ADB 封装：二进制定位、设备选择、shell 执行与实时流。"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

from .config import DEFAULTS


def _common_adb_paths(cfg: dict) -> list[str]:
    roots = list(cfg.get("adb_search_roots") or [])
    env_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if env_home:
        roots.append(env_home)
    roots += [
        os.path.expanduser("~/AppData/Local/Android/Sdk"),
        os.path.expanduser("~/Android/Sdk"),
        "C:/platform-tools",
        "D:/platform-tools",
        "E:/platform-tools",
        "C:/Users/Public/Android/Sdk",
    ]
    out = []
    for r in roots:
        if not r:
            continue
        r = str(r).replace("\\", "/")
        out.append(f"{r}/platform-tools/adb.exe")
        out.append(f"{r}/platform-tools/adb")
        out.append(f"{r}/adb.exe")
    return out


def find_adb(explicit: str | None = None, cfg: dict | None = None) -> str:
    """按 显式参数 > 配置 > 环境变量 > PATH > 常见 SDK 路径 的顺序定位 adb。

    显式/配置指定的路径不存在时直接报错，不再静默回退到另一个 adb——
    否则用户指定的版本会被悄悄换掉，排查起来非常费劲。
    """
    cfg = cfg or DEFAULTS
    for label, value in (("--adb", explicit), ("配置里的 adb_path", cfg.get("adb_path"))):
        if not value:
            continue
        raw = str(value).strip().strip('"')
        p = Path(raw)
        if p.is_file():
            return str(p)
        if os.sep not in raw and "/" not in raw:
            found = shutil.which(raw)
            if found:
                return found
        raise FileNotFoundError(f"{label} 指定的 adb 不存在或不可执行: {value}")

    candidates = [
        os.environ.get("ADB_PATH"),
        os.environ.get("ADB"),
        shutil.which("adb"),
        *_common_adb_paths(cfg),
    ]
    for c in candidates:
        if not c:
            continue
        c = str(c).strip().strip('"')
        p = Path(c)
        if p.is_file():
            return str(p)
        if os.sep not in c and "/" not in c:
            found = shutil.which(c)
            if found:
                return found
    raise FileNotFoundError(
        "未找到 adb。请用 --adb 指定路径，或在 adb_case_config.json 的 adb_path / "
        "adb_search_roots 中配置，也可设置环境变量 ADB_PATH。"
    )


class AdbError(RuntimeError):
    pass


# ---- 设备事实批量读取 ----
# 每条独立命令都是一次 adb 进程往返（USB 实测 60~100ms），探测要读
# 尺寸/密度/旋转/型号/API 五项，串行执行就是 400~500ms。设备端 shell
# 本身支持一次执行多行，这里用标记分段合成一条命令，只花一次往返。
_FACTS_SCRIPT = (
    "echo __ADBCASE_SIZE__; wm size; "
    "echo __ADBCASE_DENSITY__; wm density; "
    "echo __ADBCASE_ROT__; dumpsys input | grep -m1 SurfaceOrientation; "
    "echo __ADBCASE_PROP__; getprop ro.product.model; "
    "getprop ro.build.version.release; getprop ro.build.version.sdk; "
    "echo __ADBCASE_END__"
)
_SIZE_RE = re.compile(r"(\d{2,5})\s*[xX]\s*(\d{2,5})")
_ROT_RE = re.compile(r"SurfaceOrientation:\s*(\d+)")
_MARK = "__ADBCASE_"


def _split_facts(raw: str) -> dict[str, str]:
    """按 __ADBCASE_xxx__ 标记切段；缺标记的段为空，由调用方走单项回退。"""
    out: dict[str, str] = {}
    key = ""
    for line in (raw or "").splitlines():
        s = line.strip()
        if s.startswith(_MARK) and s.endswith("__"):
            key = s[len(_MARK):-2]
            out.setdefault(key, "")
            continue
        if key:
            out[key] = f"{out[key]}\n{line}" if out[key] else line
    return out


def _parse_wm_size(text: str) -> tuple[int, int] | None:
    """Override 优先，其次 Physical；都没有返回 None。"""
    physical = None
    for line in (text or "").splitlines():
        low = line.lower()
        if "size:" not in low:
            continue
        m = _SIZE_RE.search(line.split(":", 1)[1])
        if not m:
            continue
        size = (int(m.group(1)), int(m.group(2)))
        if "override" in low:
            return size
        physical = physical or size
    return physical


def _parse_density(text: str) -> int:
    """倒序取第一条 density 行：有 Override 时取 Override。"""
    for line in reversed((text or "").splitlines()):
        if "density:" not in line.lower():
            continue
        val = line.split(":", 1)[1].strip()
        try:
            return int(val)
        except ValueError:
            continue
    return 0


def _parse_rotation(text: str) -> int | None:
    m = _ROT_RE.search(text or "")
    return int(m.group(1)) % 4 if m else None


class Adb:
    def __init__(self, binary: str | None = None, serial: str | None = None,
                 cfg: dict | None = None, timeout: float | None = None):
        self.cfg = cfg or DEFAULTS
        self.binary = find_adb(binary, self.cfg)
        self.serial = serial or self.cfg.get("serial") or os.environ.get("ANDROID_SERIAL") or ""
        self.timeout = timeout or float(self.cfg.get("adb_timeout_s", 15))

    @property
    def prefix(self) -> list[str]:
        return [self.binary, "-s", self.serial] if self.serial else [self.binary]

    def run(self, args: list[str], *, check: bool = False, timeout: float | None = None,
            text: bool = True) -> subprocess.CompletedProcess:
        cmd = self.prefix + list(args)
        kwargs = {"capture_output": True, "timeout": timeout or self.timeout}
        if text:
            # 只在文本模式传 encoding/errors；bytes 模式下传 errors 会让
            # subprocess 误判为 text_mode=True，把 PNG 二进制解码成 str
            kwargs.update(text=True, encoding="utf-8", errors="replace")
        else:
            kwargs["text"] = False
        try:
            proc = subprocess.run(cmd, **kwargs)
        except FileNotFoundError as exc:
            raise AdbError(f"adb 无法启动: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 命令超时: {' '.join(cmd)}") from exc
        if check and proc.returncode != 0:
            raise AdbError(
                f"adb 命令失败 (code={proc.returncode}): {' '.join(cmd)}\n"
                f"stdout: {(proc.stdout or '').strip()}\nstderr: {(proc.stderr or '').strip()}"
            )
        return proc

    def shell(self, *tokens: str, check: bool = False,
              timeout: float | None = None) -> str:
        return self.run(["shell", *[str(t) for t in tokens]], check=check, timeout=timeout).stdout or ""

    def shell_raw(self, script: str, *, check: bool = False,
                  timeout: float | None = None) -> str:
        """把整段脚本作为一个参数交给 adb shell，便于处理引号与管道。"""
        return self.run(["shell", script], check=check, timeout=timeout).stdout or ""

    # ---- 设备 ----
    def devices(self) -> list[dict]:
        out = self.run(["devices", "-l"]).stdout or ""
        items = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*") or "offline" in line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
                items.append({"serial": parts[0], "state": parts[1], **props})
        return items

    def _ping(self, timeout: float = 8) -> bool:
        """设备 shell 是否真的可用（adb devices 显示 connected 并不保证）。"""
        try:
            return "ADBCASE_OK" in self.shell("echo", "ADBCASE_OK", timeout=timeout)
        except AdbError:
            return False

    def resolve_serial(self, requested: str | None = None) -> str:
        """确认设备可用。

        已指定序列号时先直接 ping：成功就省掉一次 `adb devices -l` 往返
        （实测 60~100ms）。只有 ping 失败才列设备，用来给出「不在线 /
        当前在线有哪些」的准确提示。
        """
        if requested:
            self.serial = requested
        if self.serial and self._ping():
            return self.serial

        serials = [d["serial"] for d in self.devices()]
        if self.serial:
            if self.serial not in serials:
                raise AdbError(f"指定的设备 {self.serial} 不在线。当前在线: "
                               f"{', '.join(serials) or '无'}")
            raise AdbError(
                f"设备 {self.serial} 在列表中显示 connected，但 shell 无响应"
                f"（车机 adbd 状态不一致的典型表现）。\n"
                f"建议: adb -s {self.serial} reconnect 或重启 adbd 后重试。"
            )
        if not serials:
            raise AdbError("没有可用设备（adb devices 为空或全部 offline），请检查 USB 调试授权。")
        if len(serials) > 1:
            raise AdbError(f"检测到多台设备 {serials}，请用 -s/--serial 指定，或在配置中设置 serial。")
        self.serial = serials[0]
        if not self._ping():
            raise AdbError(
                f"设备 {self.serial} 在列表中显示为 connected，但 shell 无响应"
                f"（常见车机 adbd 状态不一致）。\n"
                f"建议: adb disconnect && adb connect <ip> 或 adb -s {self.serial} "
                f"reconnect / 重启 adbd 后重试。"
            )
        return self.serial

    # ---- 显示信息 ----
    def wm_size(self) -> tuple[int, int]:
        """屏幕逻辑分辨率：Override > Physical > dumpsys/screencap 逐级回退。

        部分车机/定制 ROM 没有 wm 命令，或 wm 被截断输出，旧实现直接抛错
        让整个「探测能力」失败；这里把回退做全，探测至少能拿到可用尺寸。
        """
        try:
            out = self.shell("wm", "size")
        except AdbError:
            out = ""
        size = _parse_wm_size(out)
        return size or self.display_size()

    def display_size(self) -> tuple[int, int]:
        """wm size 不可用时的回退：dumpsys 显示信息 > screencap PNG 头。"""
        for args in (("dumpsys", "display"), ("dumpsys", "window", "displays")):
            try:
                out = self.shell(*args)
            except AdbError:
                continue
            for line in out.splitlines():
                low = line.lower()
                if not any(k in low for k in ("cur=", "real=", "init=", "displaydeviceinfo")):
                    continue
                m = _SIZE_RE.search(line)
                if m and int(m.group(1)) >= 200 and int(m.group(2)) >= 200:
                    return int(m.group(1)), int(m.group(2))
        try:
            proc = self.run(["exec-out", "screencap", "-p"], text=False, timeout=20)
            data = proc.stdout if isinstance(proc.stdout, bytes) else b""
            i = data.find(b"IHDR")   # PNG: 8 字节签名 + 4 长度 + IHDR + 宽高(各 4 字节大端)
            if i > 0 and len(data) >= i + 12:
                w, h = struct.unpack(">II", data[i + 4:i + 12])
                if w and h:
                    return int(w), int(h)
        except AdbError:
            pass
        raise AdbError("无法获取屏幕尺寸：wm size / dumpsys display / screencap 均无有效输出")

    def wm_density(self) -> int:
        return _parse_density(self.shell("wm", "density"))

    def rotation(self) -> int:
        """当前显示旋转 0/1/2/3：dumpsys 优先，settings 兜底。"""
        try:
            out = self.shell_raw("dumpsys input | grep -m1 SurfaceOrientation")
        except AdbError:
            out = ""
        rot = _parse_rotation(out)
        if rot is not None:
            return rot
        for script, pat in (("dumpsys window displays", r"mRotation=ROTATION_(\d+)"),
                            ("dumpsys display", r"mCurrentOrientation=(\d+)")):
            try:
                out2 = self.shell_raw(script)
            except AdbError:
                continue
            m2 = re.search(pat, out2)
            if m2:
                return int(m2.group(1)) % 4
        try:
            rot = self.shell("settings", "get", "system", "user_rotation").strip()
        except AdbError:
            rot = ""
        return int(rot) % 4 if rot.isdigit() else 0

    def facts(self) -> dict:
        """一次 shell 取回 尺寸/密度/旋转/型号/API，缺失项逐项回退。

        批量脚本里某段缺失（定制 ROM 无 wm、grep 被裁剪、dumpsys 被限流）时，
        对应字段退回原来的单项实现，能力表照样填满——行为不变，但常见路径
        从 5 次 adb 往返压到 1 次。
        """
        errors: list[str] = []
        try:
            raw = self.shell_raw(_FACTS_SCRIPT, timeout=max(self.timeout, 20))
        except AdbError as exc:
            raw = ""
            errors.append(f"批量读取: {exc}")
        sec = _split_facts(raw)

        try:
            size = _parse_wm_size(sec.get("SIZE", ""))
            if not size:
                size = self.wm_size()        # 单项回退：dumpsys display / screencap
            if int(size[0]) <= 0 or int(size[1]) <= 0:
                raise AdbError(f"wm size 返回非法尺寸 {list(size)}")
            size = [int(size[0]), int(size[1])]
        except AdbError as exc:
            size = [0, 0]
            errors.append(f"屏幕尺寸: {exc}")

        density = _parse_density(sec.get("DENSITY", ""))
        if not density:
            try:
                density = self.wm_density()
            except AdbError as exc:
                errors.append(f"屏幕密度: {exc}")

        rotation = _parse_rotation(sec.get("ROT", ""))
        if rotation is None:
            try:
                rotation = self.rotation()
            except AdbError as exc:
                rotation = 0
                errors.append(f"显示旋转: {exc}")

        props = [x.strip() for x in (sec.get("PROP", "") or "").splitlines() if x.strip()]
        if len(props) < 3:
            try:
                props = [x.strip() for x in self.shell_raw(
                    "getprop ro.product.model; getprop ro.build.version.release; "
                    "getprop ro.build.version.sdk").splitlines() if x.strip()]
            except AdbError as exc:
                errors.append(f"型号信息: {exc}")
        props = (props + ["", "", ""])[:3]

        return {"size": size, "density": density, "rotation": rotation,
                "model": props[0], "release": props[1], "sdk": props[2],
                "errors": errors, "batched": bool(sec.get("SIZE") or sec.get("PROP"))}

    # ---- 输入能力 ----
    def getevent_info(self) -> str:
        """运行时读取输入设备能力表，绝不硬编码 /dev/input/eventN。

        每个参数单独 try：部分设备 `getevent -lp` 超时/报错但 `-p` 可用，
        旧实现第一个参数抛错就整条探测链路失败，界面上看起来像按钮没反应。
        """
        last = ""
        for flag in ("-lp", "-p", "-il"):
            try:
                out = self.shell("getevent", flag, timeout=10)
            except AdbError as exc:
                last = str(exc)
                continue
            if "/dev/input/event" in out:
                return out
            last = out.strip()[:200] or last
        raise AdbError("getevent 无输出：设备可能未授权 shell 读取 /dev/input。"
                       + (f" 最后输出: {last}" if last else ""))

    def screencap(self, dest: str | os.PathLike) -> bool:
        proc = self.run(["exec-out", "screencap", "-p"], timeout=30, text=False)
        data = proc.stdout if isinstance(proc.stdout, bytes) else b""
        if not data or proc.returncode != 0:
            return False
        # 部分设备输出 CRLF，修正 PNG 头
        if data.startswith(b"\r\n"):
            data = data.replace(b"\r\n", b"\n", 1)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(data)
        return True

    def uiautomator_dump(self, *, compressed: bool = False) -> str:
        args = ["uiautomator", "dump"]
        if compressed:
            # 丢掉非关键节点：节点少一个量级，动画界面更不容易卡在等 idle
            args.append("--compressed")
        # 未尾 2>&1 在设备端合并 stderr：部分厂商把成功提示写 stderr，
        # 只看 stdout 会把成功当失败（一次 dump 白跑）
        args += ["/sdcard/window_dump.xml", "2>&1"]
        out = self.shell(*args)
        # dump 失败（锁屏/应用禁止）时旧文件还在，直接 cat 会拿到上一帧
        # 界面的陈旧控件树：断言假通过、透视展示错位，必须先验成功标志
        if "dumped to" not in out.lower():
            raise AdbError("uiautomator dump 失败（锁屏/安全界面可能禁止 dump）: "
                           f"{out.strip()[:200]}")
        return self.shell("cat", "/sdcard/window_dump.xml")

    def current_focus(self) -> str:
        return self.shell_raw("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")

    def input_tap(self, x: int, y: int) -> str:
        return self.shell("input", "tap", str(x), str(y), check=True)

    def input_swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> str:
        return self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2),
                          str(max(1, duration_ms)), check=True)

    def input_keyevent(self, keycode: str) -> str:
        return self.shell("input", "keyevent", keycode, check=True)

    def input_text(self, text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace(" ", "%s")
        return self.shell_raw(f'input text "{escaped}"', check=True)

    def start_app(self, package: str, activity: str = "") -> str:
        if activity:
            return self.shell("am", "start", "-n", f"{package}/{activity}", check=True)
        return self.shell("monkey", "-p", package, "-c",
                          "android.intent.category.LAUNCHER", "1", check=True)
