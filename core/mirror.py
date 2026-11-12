# -*- coding: utf-8 -*-
"""实时投屏 + 反向控制（scrcpy v2.7 协议的纯 Python 客户端）。

借鉴 GameScrcpy / scrcpy 的架构：
  1. adb push scrcpy-server 到设备，app_process 启动；
  2. 设备端 MediaCodec 硬件编码 H.264，经 adb forward 的 localabstract
     socket 推流到电脑（tunnel_forward 模式）；
  3. 电脑端 PyAV 解码出 RGB 帧，写入单槽 latest_frame() 供界面按节拍取；
  4. 鼠标/键盘操作打包成控制消息走 control socket 注回设备，
     由 InputManager 注入，无需 root。

本模块不依赖 Qt：GUI 用回调/信号桥接，CLI 也能直接复用。
协议细节见 scrcpy v2.7 源码：Streamer / ControlMessageReader / DesktopConnection。
"""

from __future__ import annotations

import os
import random
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

from .adb import Adb, AdbError

SERVER_VERSION = "2.7"
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"
DEVICE_RECORD_PATH = "/data/local/tmp/adbcase_mirror.h264"
SERVER_JAR_NAME = f"scrcpy-server-v{SERVER_VERSION}.jar"
SERVER_JAR_URL = (
    f"https://github.com/Genymobile/scrcpy/releases/download/v{SERVER_VERSION}/{SERVER_JAR_NAME}"
)

# ---- Android 常量（KeyEvent / MotionEvent）----
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

KEYCODE_HOME = 3
KEYCODE_BACK = 4
KEYCODE_POWER = 26
KEYCODE_VOLUME_UP = 24
KEYCODE_VOLUME_DOWN = 25
KEYCODE_MENU = 82
KEYCODE_ENTER = 66
KEYCODE_DPAD_UP = 19
KEYCODE_DPAD_DOWN = 20
KEYCODE_DPAD_LEFT = 21
KEYCODE_DPAD_RIGHT = 22
KEYCODE_DPAD_CENTER = 23
KEYCODE_APP_SWITCH = 187
# 物理键盘注入用（A2）：文本编辑 / 导航键
KEYCODE_TAB = 61
KEYCODE_DEL = 67            # 退格 Backspace
KEYCODE_FORWARD_DEL = 112   # 删除 Delete
KEYCODE_ESCAPE = 111
KEYCODE_MOVE_HOME = 122
KEYCODE_MOVE_END = 123
KEYCODE_PAGE_UP = 92
KEYCODE_PAGE_DOWN = 93
KEYCODE_INSERT = 124

# 控制消息类型（ControlMessage.java）
TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_BACK_OR_SCREEN_ON = 4

# scrcpy 用 -1 作为鼠标指针 id
POINTER_ID_MOUSE = -1

_CODEC_NAMES = {0x68323634: "h264", 0x68323635: "hevc", 0x00617631: "av1"}

# 音频编码 id（AudioCodec.java）；0=设备禁用音频，1=配置错误（Streamer.writeDisableStream）
_AUDIO_CODECS = {0x6F707573: "opus", 0x00616163: "aac",
                 0x666C6163: "flac", 0x00726177: "raw"}


class MirrorError(RuntimeError):
    pass


def has_decoder() -> bool:
    """PyAV 是否可用（投屏解码依赖它；控制消息打包不需要）。"""
    try:
        import av  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------
# 控制消息打包（纯函数，便于离线自检）
# 全部大端；Position = x(i32) y(i32) screenW(u16) screenH(u16)
# --------------------------------------------------------------------------
def _pack_position(x: int, y: int, w: int, h: int) -> bytes:
    return struct.pack(">iiHH", int(x), int(y), int(w), int(h))


def pack_touch(action: int, x: int, y: int, w: int, h: int, *,
               pointer_id: int = POINTER_ID_MOUSE, pressure: float | None = None,
               buttons: int | None = None) -> bytes:
    """注入触摸。pressure 缺省：按下/移动=1.0，抬起=0.0（u16 定点数）。"""
    if pressure is None:
        pressure = 0.0 if action == ACTION_UP else 1.0
    if buttons is None:
        buttons = 0 if action == ACTION_UP else 1
    p = 0xFFFF if pressure >= 1.0 else max(0, int(pressure * 0x10000))
    action_button = 1 if action == ACTION_DOWN else 0
    return (struct.pack(">BBq", TYPE_INJECT_TOUCH_EVENT, action, pointer_id)
            + _pack_position(x, y, w, h)
            + struct.pack(">Hii", p, action_button, buttons))


def pack_keycode(action: int, keycode: int, repeat: int = 0, meta: int = 0) -> bytes:
    return struct.pack(">BBiii", TYPE_INJECT_KEYCODE, action, int(keycode),
                       int(repeat), int(meta))


def pack_text(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > 300:
        raise MirrorError("注入文本过长（服务端上限 300 字节）")
    return struct.pack(">BI", TYPE_INJECT_TEXT, len(data)) + data


def _i16_fixed(v: float) -> int:
    """i16 定点数（Binary.floatToI16FixedPoint）：1.0 -> 0x7FFF。"""
    if v >= 1.0:
        return 0x7FFF
    if v <= -1.0:
        return -0x8000
    return int(v * 0x8000)


def pack_scroll(x: int, y: int, w: int, h: int, hscroll: float, vscroll: float) -> bytes:
    return (struct.pack(">B", TYPE_INJECT_SCROLL_EVENT)
            + _pack_position(x, y, w, h)
            + struct.pack(">hhi", _i16_fixed(hscroll), _i16_fixed(vscroll), 0))


def pack_back_or_screen_on(action: int = ACTION_DOWN) -> bytes:
    return struct.pack(">BB", TYPE_BACK_OR_SCREEN_ON, action)


# --------------------------------------------------------------------------
# server jar 定位
# --------------------------------------------------------------------------
def find_server_jar(cfg: dict | None = None, *, allow_download: bool = True) -> Path:
    """显式配置 > vendor/ 自带 > 自动下载到 vendor/。"""
    cfg = cfg or {}
    explicit = str(cfg.get("mirror_server_jar") or "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise MirrorError(f"配置 mirror_server_jar 指定的文件不存在: {explicit}")
    vendor = Path(__file__).resolve().parent.parent / "vendor" / SERVER_JAR_NAME
    if vendor.is_file():
        return vendor
    if not allow_download or not cfg.get("mirror_auto_download", True):
        raise MirrorError(
            f"缺少 {SERVER_JAR_NAME}。请手动下载放入 vendor/ 目录：\n  {SERVER_JAR_URL}\n"
            "或在配置 mirror_server_jar 里指定已有路径（scrcpy 安装目录里也有）。"
        )
    vendor.parent.mkdir(parents=True, exist_ok=True)
    tmp = vendor.with_suffix(".jar.part")
    try:
        urllib.request.urlretrieve(SERVER_JAR_URL, tmp)
        os.replace(tmp, vendor)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise MirrorError(
            f"自动下载 {SERVER_JAR_NAME} 失败（{exc}）。\n请手动下载放入 vendor/：\n  {SERVER_JAR_URL}"
        ) from exc
    return vendor


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# 外挂官方 scrcpy（C/SDL 客户端，GPU 渲染 + 硬解码，帧率/清晰度上限最高）
# 用途：Python 端解码跟不上高码率高分辨率时，一键切官方客户端看同等参数效果。
# 部署约定：便携版解压到 E:\tools\scrcpy（可用 cfg mirror_external_scrcpy 指定）。
# --------------------------------------------------------------------------
EXTERNAL_TOOLS_DIR = r"E:\tools"
EXTERNAL_ZIP_URL = (
    f"https://github.com/Genymobile/scrcpy/releases/download/"
    f"v{SERVER_VERSION}/scrcpy-win64-v{SERVER_VERSION}.zip"
)


def find_external_scrcpy(cfg: dict | None = None) -> Path | None:
    """返回 scrcpy.exe 路径；未部署时 None（不自动下载，由调用方决定）。"""
    cfg = cfg or {}
    explicit = str(cfg.get("mirror_external_scrcpy") or "").strip()
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    exe = Path(EXTERNAL_TOOLS_DIR) / "scrcpy" / "scrcpy.exe"
    return exe if exe.is_file() else None


def download_external_scrcpy(dest_dir: str = EXTERNAL_TOOLS_DIR, log=None) -> Path:
    r"""下载官方 win64 便携版并解压到 dest_dir\scrcpy，返回 scrcpy.exe。"""
    import shutil
    import zipfile
    log = log or (lambda _m: None)
    dest = Path(dest_dir) / "scrcpy"
    exe = dest / "scrcpy.exe"
    if exe.is_file():
        return exe
    dest.mkdir(parents=True, exist_ok=True)
    tmp = Path(dest_dir) / f"scrcpy-win64-v{SERVER_VERSION}.zip.part"
    log(f"[投屏] 下载官方 scrcpy 便携版 -> {dest}")
    try:
        urllib.request.urlretrieve(EXTERNAL_ZIP_URL, tmp)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(dest)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise MirrorError(
            f"下载官方 scrcpy 失败（{exc}）。可手动下载解压到 {dest}：\n  {EXTERNAL_ZIP_URL}"
        ) from exc
    tmp.unlink(missing_ok=True)
    if not exe.is_file():
        # zip 内层带 scrcpy-win64-vX.Y 目录，展平到 dest 根目录
        for sub in dest.iterdir():
            if sub.is_dir() and (sub / "scrcpy.exe").is_file():
                for f in sub.iterdir():
                    shutil.move(str(f), str(dest / f.name))
                sub.rmdir()
                break
    if not exe.is_file():
        raise MirrorError(f"解压后未找到 {exe}")
    log(f"[投屏] 官方 scrcpy 已部署: {exe}")
    return exe


def build_external_cmd(exe: Path | str, serial: str = "", *, max_size: int = 0,
                       bit_rate: int = 0, max_fps: int = 0, audio: bool = True,
                       adb_path: str = "") -> list[str]:
    """拼官方客户端参数（纯函数，便于离线自检）。

    adb_path 不生成命令行参数（v2.7 没有 --adb/--adb-path 选项），
    由 launch_external_scrcpy 通过 ADB 环境变量传递；未设时
    scrcpy 优先用同目录自带的 adb.exe。
    """
    cmd = [str(exe), "--window-title", "scrcpy 高性能模式"]
    if serial:
        cmd += ["--serial", serial]
    if max_size:
        cmd += ["--max-size", str(int(max_size))]
    if bit_rate:
        cmd += ["--video-bit-rate", str(int(bit_rate))]
    if max_fps:
        cmd += ["--max-fps", str(int(max_fps))]
    if not audio:
        cmd += ["--no-audio"]
    return cmd


def launch_external_scrcpy(exe: Path | str, serial: str = "", *,
                           adb_path: str = "", **kw) -> subprocess.Popen:
    """启动独立的高性能投屏窗口（与内嵌投屏互不干扰）。"""
    cmd = build_external_cmd(exe, serial, adb_path=adb_path, **kw)
    env = None
    if adb_path:
        import os
        env = dict(os.environ, ADB=str(adb_path))
    return subprocess.Popen(cmd, cwd=str(Path(exe).parent), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("投屏连接被设备端关闭")
        buf.extend(chunk)
    return bytes(buf)


class _SockReader:
    """带缓冲的流读取器：超时不丢已读的部分字节。

    帧头/帧体可能分多次 recv 到达；若超时后丢弃半截帧头，
    下次读取会从帧头中间开始，整条流永久错位。缓冲保留partial数据，
    超时只意味着“设备暂时没推帧”（静态画面属正常）。
    """

    def __init__(self, sock: socket.socket, is_stopped):
        self._sock = sock
        self._is_stopped = is_stopped
        self._buf = bytearray()

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            if self._is_stopped():
                raise ConnectionError("投屏已停止")
            try:
                chunk = self._sock.recv(n - len(self._buf))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("投屏连接被设备端关闭")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


# --------------------------------------------------------------------------
# 投屏会话
# --------------------------------------------------------------------------
class ScrcpyMirror:
    """一台设备一个会话。start() 阻塞到首帧握手完成，stop() 幂等。

    回调都在后台线程触发，GUI 侧请用信号切回主线程：
      on_audio(pcm: bytes, rate, channels)  每解码出一块音频（s16 交错）
      on_meta(info: dict)            握手成功：device_name/video_size/screen_size/serial
      on_error(msg: str)             运行中意外中断（只触发一次）
      log(msg: str)                  运行日志

    视频帧不再走回调/信号跨线程（UI 卡顿会让帧对象在 Qt 事件队列里
    积压成无界缓冲、延迟滚大），改为单槽 latest-frame：解码线程只留
    最新一帧，旧帧就地释放，消费方用自己的节拍取帧。
    """

    def __init__(self, adb: Adb, cfg: dict | None = None, *,
                 max_size: int = 0, bit_rate: int = 8_000_000, max_fps: int = 30,
                 audio: bool | None = None, record_path: str | None = None,
                 on_audio=None, on_meta=None, on_error=None, log=None):
        self.adb = adb
        self.cfg = cfg or {}
        self.max_size = int(max_size or self.cfg.get("mirror_max_size", 1280))
        self.bit_rate = int(bit_rate or self.cfg.get("mirror_bit_rate", 8_000_000))
        self.max_fps = int(max_fps or self.cfg.get("mirror_max_fps", 30))
        self.audio = bool(self.cfg.get("mirror_audio", True) if audio is None else audio)
        # 非空时设备端同步录像（scrcpy video_file，编码分流不占电脑 CPU），
        # 会话清理时 adb pull 回该路径
        self.record_path = str(record_path) if record_path else ""
        self.on_audio = on_audio
        self.on_meta = on_meta
        self.on_error = on_error
        self.log = log or (lambda _m: None)

        self.device_name = ""
        self.video_size: tuple[int, int] = (0, 0)
        self.screen_size: tuple[int, int] = (0, 0)

        self._stopped = False
        self._started = False
        self._cleaned = False
        self._stop_logged = False
        self._error_reported = False
        self._video_sock: socket.socket | None = None
        self._audio_sock: socket.socket | None = None
        self._audio_codec_id = 0
        self._ctrl_sock: socket.socket | None = None
        self._proc: subprocess.Popen | None = None
        self._port = 0
        self._scid = 0
        self._ctrl_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._server_log: deque = deque(maxlen=200)
        self._codec = None
        self._audio_resampler = None
        self._audio_resampler_key = None
        self.frames_decoded = 0
        self.audio_frames = 0
        self._frame_lock = threading.Lock()
        self._latest_frame = None          # ndarray[H,W,3] rgb24（C 连续）
        self._latest_frame_ts = 0.0
        self._device_record_path = ""

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._started:
            return
        if not has_decoder():
            raise MirrorError("投屏解码需要 PyAV：pip install av numpy")
        import av  # noqa: F401  提前暴露导入错误

        self.adb.serial = self.adb.resolve_serial()
        jar = find_server_jar(self.cfg)
        self.log(f"[投屏] 推送 {jar.name} -> {DEVICE_SERVER_PATH}")
        self.adb.run(["push", str(jar), DEVICE_SERVER_PATH], check=True, timeout=60)

        self._scid = random.randint(0, 0x7FFFFFFF)
        self._port = _free_port()
        socket_name = f"scrcpy_{self._scid:08x}"
        self.adb.run(["forward", f"tcp:{self._port}", f"localabstract:{socket_name}"],
                     check=True)
        opts = [
            f"scid={self._scid:08x}", "log_level=warn", "tunnel_forward=true",
            "video=true", f"audio={'true' if self.audio else 'false'}",
            "control=true", "video_codec=h264",
            f"max_size={self.max_size}", f"video_bit_rate={self.bit_rate}",
            f"max_fps={self.max_fps}", "clipboard_autosync=false",
            "send_device_meta=true", "send_frame_meta=true",
            "send_dummy_byte=true", "send_codec_meta=true",
        ]
        if self.audio:
            # 与 scrcpy 客户端默认一致：opus / 设备播放声音 / 128kbps
            opts += ["audio_codec=opus", "audio_source=output", "audio_bit_rate=128000"]
        if self.record_path:
            # 设备端直写编码流（借鉴 scrcpy --record）：不重编码不占电脑 CPU，
            # 会话结束后 adb pull 回本地
            self._device_record_path = f"{DEVICE_RECORD_PATH[:-5]}_{self._scid:08x}.h264"
            opts.append(f"video_file={self._device_record_path}")
        cmd = self.adb.prefix + [
            "shell", f"CLASSPATH={DEVICE_SERVER_PATH}", "app_process", "/",
            "com.genymobile.scrcpy.Server", SERVER_VERSION, *opts,
        ]
        self.log(f"[投屏] 启动设备端服务（{self.max_size or '原始'}p / "
                 f"{self.bit_rate // 1000}kbps / {self.max_fps}fps）")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
        # 立即开始收集服务端输出：启动失败时诊断信息才不会丢
        t = threading.Thread(target=self._drain_stderr, name="mirror-stderr", daemon=True)
        t.start()
        self._threads.append(t)
        try:
            self._connect_sockets()
            self._handshake()
        except Exception:
            self.stop()
            raise
        self._started = True
        t = threading.Thread(target=self._video_loop, name="mirror-video", daemon=True)
        t.start()
        self._threads.append(t)
        if self._audio_sock is not None:
            t = threading.Thread(target=self._audio_loop, name="mirror-audio", daemon=True)
            t.start()
            self._threads.append(t)
        info = {
            "device_name": self.device_name,
            "video_size": list(self.video_size),
            "screen_size": list(self.screen_size),
            "serial": self.adb.serial,
        }
        self.log(f"[投屏] 已连接 {self.device_name} 视频 {self.video_size[0]}x{self.video_size[1]} "
                 f"屏幕 {self.screen_size[0]}x{self.screen_size[1]}")
        if self.on_meta:
            self.on_meta(info)

    def _connect_sockets(self) -> None:
        # tunnel_forward：设备端先 accept 视频再 accept 控制，顺序不能反。
        # adb 守护层可能在设备端 LocalServerSocket 就绪前就接受 TCP 连接，
        # 随后立刻断开；所以 dummy byte 读失败也要当作“还没就绪”重试。
        deadline = time.monotonic() + 15
        last_err: Exception | None = None
        while time.monotonic() < deadline and not self._stopped:
            if self._proc is not None and self._proc.poll() is not None:
                raise MirrorError("设备端 scrcpy-server 启动失败: "
                                  + self._server_stderr()[-800:])
            sock = None
            try:
                sock = socket.create_connection(("127.0.0.1", self._port), timeout=2)
                sock.settimeout(3)
                _read_exact(sock, 1)  # dummy byte：确认设备端真的活着
                self._video_sock = sock
                break
            except (OSError, ConnectionError) as exc:
                last_err = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                time.sleep(0.25)
        if self._video_sock is None:
            detail = self._server_stderr()[-800:]
            raise MirrorError(f"连接投屏通道超时: {last_err}"
                              + (f"\n设备端输出: {detail}" if detail else ""))
        self._video_sock.settimeout(5)
        if self.audio:
            # 顺序：视频 → 音频 → 控制（DesktopConnection.open）。
            # 关键：服务端要等三条通道都 accept 完才开始写音频 codec id，
            # 所以必须先连上控制 socket 再读 codec id，否则双方互等死锁，
            # 超时后关音频 socket 重试还会导致服务端 fatal 退出。
            deadline_a = time.monotonic() + 10
            last_a: Exception | None = None
            while time.monotonic() < deadline_a and not self._stopped:
                a_sock = c_sock = None
                try:
                    a_sock = socket.create_connection(("127.0.0.1", self._port), timeout=2)
                    c_sock = socket.create_connection(("127.0.0.1", self._port), timeout=2)
                    a_sock.settimeout(3)
                    self._audio_codec_id = struct.unpack(">I", _read_exact(a_sock, 4))[0]
                    self._audio_sock = a_sock
                    self._ctrl_sock = c_sock
                    break
                except (OSError, ConnectionError) as exc:
                    last_a = exc
                    for s in (a_sock, c_sock):
                        if s is not None:
                            try:
                                s.close()
                            except OSError:
                                pass
                    time.sleep(0.25)
            if self._audio_sock is None:
                raise MirrorError(f"连接音频通道超时: {last_a}")
            self._audio_sock.settimeout(1)
        else:
            self._ctrl_sock = socket.create_connection(("127.0.0.1", self._port), timeout=5)
        self._ctrl_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _server_stderr(self) -> str:
        """服务端已产生的输出（后台线程收集，Windows 上不能用 select 读管道）。"""
        return "\n".join(self._server_log).strip()

    def _handshake(self) -> None:
        assert self._video_sock is not None
        name = _read_exact(self._video_sock, 64)
        self.device_name = name.split(b"\x00", 1)[0].decode("utf-8", "replace")
        codec_id, w, h = struct.unpack(">III", _read_exact(self._video_sock, 12))
        codec_name = _CODEC_NAMES.get(codec_id)
        if codec_name is None:
            raise MirrorError(f"未知视频编码 id: {codec_id:#x}")
        if w == 0 or h == 0:
            raise MirrorError("设备端返回了 0 尺寸视频流")
        self.video_size = (w, h)
        try:
            self.screen_size = self.adb.wm_size()
        except AdbError:
            self.screen_size = (w, h)

        import av
        self._codec = av.CodecContext.create(codec_name, "r")
        # 低延迟：不做帧级多线程缓冲，解出一帧立刻显示
        self._codec.thread_type = "NONE"
        self._codec.options = {"flags": "low_delay"}
        self._video_sock.settimeout(1)

    def request_stop(self) -> None:
        """快速停止：只关连接/杀进程，可在 UI 线程调用，不阻塞等 adb。"""
        if self._stopped:
            return
        self._stopped = True
        for s in (self._video_sock, self._audio_sock, self._ctrl_sock):
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    def stop(self) -> None:
        """完整清理（含 adb forward 移除与线程等待），请在后台线程调用。"""
        self.request_stop()
        if self._cleaned:
            return
        self._cleaned = True
        if self._proc is not None:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except OSError:
                    pass
            except OSError:
                pass
        # 服务退出时才收尾 video_file：等它写完再 pull（给 1s 缓冲落盘）
        if self._device_record_path:
            self._pull_device_record()
        if self._port:
            try:
                self.adb.run(["forward", "--remove", f"tcp:{self._port}"], timeout=5)
            except (AdbError, OSError):
                pass
        for t in self._threads:
            t.join(timeout=2)
        if not self._stop_logged:
            self._stop_logged = True
            self.log("[投屏] 已停止")

    def _pull_device_record(self) -> None:
        """把设备端录像拉回 record_path，成功后删除设备上的临时文件。

        断线重连会产生多段会话，各自拉回同一本地路径会覆盖前段；
        目标已存在时自动改拉 _段N 分段文件，保留全部录像。
        """
        time.sleep(1.0)
        dest = self.record_path
        if os.path.exists(dest):
            stem, ext = os.path.splitext(dest)
            n = 2
            while os.path.exists(f"{stem}_段{n}{ext}"):
                n += 1
            dest = f"{stem}_段{n}{ext}"
        try:
            self.adb.run(["pull", self._device_record_path, dest],
                         timeout=120)
            self.log(f"[投屏] 录像已保存: {dest}")
            try:
                self.adb.run(["shell", "rm", "-f", self._device_record_path],
                             timeout=10)
            except (AdbError, OSError):
                pass
        except (AdbError, OSError, subprocess.TimeoutExpired) as exc:
            self.log(f"[投屏] 录像拉取失败（{exc}），设备端保留 {self._device_record_path}")

    def _report_error(self, msg: str) -> None:
        if self._stopped or self._error_reported:
            return
        self._error_reported = True
        if self.on_error is not None:
            self.on_error(msg)

    # ---- 视频流 ----
    def latest_frame(self):
        """取最新解码帧：(ndarray[H,W,3] rgb24, monotonic_ts)；无帧时 (None, 0.0)。

        数组发布后从不改写（发布即换新对象），返回的是只读引用，
        调用方不得就地修改；要送进会改缓冲的下游（PIL 等）先自行 copy。
        消费方：GUI 刷帧定时器（零拷贝）、图像断言（免 screencap 的实时截图）。
        """
        with self._frame_lock:
            if self._latest_frame is None:
                return None, 0.0
            view = self._latest_frame
            view.flags.writeable = False
            return view, self._latest_frame_ts

    def _publish_frame(self, arr) -> None:
        with self._frame_lock:
            self._latest_frame = arr
            self._latest_frame_ts = time.monotonic()

    def _video_loop(self) -> None:
        assert self._video_sock is not None and self._codec is not None
        reader = _SockReader(self._video_sock, lambda: self._stopped)
        try:
            while not self._stopped:
                # v2.7 帧头 12 字节：PTS/flags(8) + size(4)（display 尺寸仅开流时发一次）
                pts_flags, size = struct.unpack(">QI", reader.read(12))
                if size > 64 * 1024 * 1024:
                    raise MirrorError(f"异常帧大小 {size}，流可能已损坏")
                data = reader.read(size)
                # bit63=config(SPS/PPS) bit62=关键帧；都直接喂给解析器
                for packet in self._codec.parse(data):
                    for frame in self._codec.decode(packet):
                        self.frames_decoded += 1
                        # 旋转/分辨率变化时跟随实际帧尺寸，控制坐标才不会错位
                        if (frame.width, frame.height) != self.video_size:
                            self.video_size = (frame.width, frame.height)
                            # 逻辑屏尺寸同步刷新：wm_size 只在握手取过一次，
                            # 不重查则录制归一化基准按旧方向换算出偏差坐标。
                            # 旋转是低频事件，这里多一次 adb 调用可接受
                            try:
                                self.screen_size = self.adb.wm_size()
                            except AdbError:
                                pass
                            self.log(f"[投屏] 视频尺寸变为 {frame.width}x{frame.height}")
                        arr = frame.to_ndarray(format="rgb24")
                        # FFmpeg 行stride有对齐填充时 to_ndarray 返回非连续视图，
                        # 单槽只存 C 连续版本，消费方（QImage/断言）可直接引用
                        if not arr.flags["C_CONTIGUOUS"]:
                            arr = arr.copy()
                        self._publish_frame(arr)
        except Exception as exc:
            if not self._stopped:
                self.log(f"[投屏] 视频流中断: {exc}")
                self._report_error(f"视频流中断: {exc}")

    # ---- 音频流 ----
    def _audio_loop(self) -> None:
        assert self._audio_sock is not None
        reader = _SockReader(self._audio_sock, lambda: self._stopped)
        codec_id = self._audio_codec_id
        if codec_id == 0:
            self.log("[投屏] 设备不支持音频采集，仅投视频")
            return
        if codec_id == 1:
            self.log("[投屏] 设备端音频配置错误，仅投视频")
            return
        name = _AUDIO_CODECS.get(codec_id)
        if name is None:
            self.log(f"[投屏] 未知音频编码 id: {codec_id:#x}，仅投视频")
            return
        self.log(f"[投屏] 音频通道已连接（{name}）")
        codec = None
        try:
            while not self._stopped:
                pts_flags, size = struct.unpack(">QI", reader.read(12))
                if size > 16 * 1024 * 1024:
                    raise MirrorError(f"异常音频包大小 {size}")
                data = reader.read(size)
                if pts_flags & (1 << 63):
                    # config 包：服务端已切好 OpusHead/AudioSpecificConfig/FLAC streaminfo
                    if name != "raw":
                        import av
                        codec = av.CodecContext.create(name, "r")
                        codec.extradata = data
                    continue
                if name == "raw":
                    self._emit_audio(data, 48000, 2)
                    continue
                if codec is None:
                    continue
                for packet in codec.parse(data):
                    for frame in codec.decode(packet):
                        for out in self._to_s16_frames(frame):
                            arr = out.to_ndarray()
                            if not arr.flags["C_CONTIGUOUS"]:
                                arr = arr.copy()
                            self._emit_audio(arr.tobytes(), out.sample_rate,
                                             out.layout.nb_channels)
        except Exception as exc:
            # 音频中断不影响视频，不触发 on_error
            if not self._stopped:
                self.log(f"[投屏] 音频流中断（视频不受影响）: {exc}")

    def _to_s16_frames(self, frame):
        """解码帧统一转 s16 交错（opus 原生输出 fltp 平面浮点）。

        旧版 PyAV 用 frame.reformat；19+ 移除了该方法，改用 AudioResampler。
        """
        if frame.format.name == "s16":
            return [frame]
        reformat = getattr(frame, "reformat", None)
        if reformat is not None:
            return [reformat(format="s16")]
        import av
        key = (frame.format.name, str(frame.layout), frame.sample_rate)
        if self._audio_resampler is None or self._audio_resampler_key != key:
            self._audio_resampler = av.AudioResampler(
                format="s16", layout=frame.layout, rate=frame.sample_rate)
            self._audio_resampler_key = key
        return self._audio_resampler.resample(frame)

    def _emit_audio(self, pcm: bytes, rate: int, channels: int) -> None:
        self.audio_frames += 1
        if self.on_audio is not None:
            self.on_audio(pcm, int(rate), int(channels))

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._server_log.append(line)
                if self._started and not self._stopped:
                    self.log(f"[server] {line}")
        if not self._stopped:
            code = proc.poll()
            self.log(f"[投屏] 设备端服务已退出 (code={code})")
            self._report_error(f"设备端投屏服务意外退出 (code={code})，请检查 USB 连接")

    # ---- 反向控制 ----
    def _send(self, data: bytes) -> None:
        if self._stopped or self._ctrl_sock is None:
            return
        with self._ctrl_lock:
            try:
                self._ctrl_sock.sendall(data)
            except OSError as exc:
                if not self._stopped:
                    self.log(f"[投屏] 控制通道发送失败: {exc}")

    def _pos(self, x: int, y: int) -> tuple[int, int, int, int]:
        w, h = self.video_size
        return (max(0, min(int(x), w - 1)), max(0, min(int(y), h - 1)), w, h)

    def touch(self, action: int, x: int, y: int) -> None:
        self._send(pack_touch(action, *self._pos(x, y)))

    def touch_down(self, x: int, y: int) -> None:
        self.touch(ACTION_DOWN, x, y)

    def touch_move(self, x: int, y: int) -> None:
        self.touch(ACTION_MOVE, x, y)

    def touch_up(self, x: int, y: int) -> None:
        self.touch(ACTION_UP, x, y)

    def tap(self, x: int, y: int) -> None:
        self.touch_down(x, y)
        self.touch_up(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        """程序化滑动（GUI 拖拽由 down/move/up 自然组成，不走这里）。"""
        def _run():
            steps = max(8, int(duration_ms / 16))
            self.touch_down(x1, y1)
            for i in range(1, steps + 1):
                if self._stopped:
                    break
                t = i / steps
                self.touch_move(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
                time.sleep(duration_ms / 1000 / steps)
            self.touch_up(x2, y2)
        threading.Thread(target=_run, name="mirror-swipe", daemon=True).start()

    def key(self, keycode: int, *, repeat: int = 0, meta: int = 0) -> None:
        self._send(pack_keycode(ACTION_DOWN, keycode, repeat, meta))
        self._send(pack_keycode(ACTION_UP, keycode, repeat, meta))

    def back(self) -> None:
        # BACK_OR_SCREEN_ON：亮屏时等价返回键，灭屏时先点亮屏幕
        self._send(pack_back_or_screen_on(ACTION_DOWN))
        self._send(pack_back_or_screen_on(ACTION_UP))

    def home(self) -> None:
        self.key(KEYCODE_HOME)

    def recents(self) -> None:
        self.key(KEYCODE_APP_SWITCH)

    def power(self) -> None:
        self.key(KEYCODE_POWER)

    def volume_up(self) -> None:
        self.key(KEYCODE_VOLUME_UP)

    def volume_down(self) -> None:
        self.key(KEYCODE_VOLUME_DOWN)

    def scroll(self, x: int, y: int, hscroll: float, vscroll: float) -> None:
        self._send(pack_scroll(*self._pos(x, y), hscroll, vscroll))

    def text(self, content: str) -> None:
        self._send(pack_text(content))