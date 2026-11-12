# -*- coding: utf-8 -*-
"""配置加载：所有可调参数集中在这里，运行逻辑不写死设备/阈值。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    # ADB 可执行文件；留空表示自动探测（环境变量 / PATH / 常见 SDK 路径）
    "adb_path": "",
    "serial": "",
    "adb_timeout_s": 15,

    # 事件 -> 语义动作的判定阈值
    "tap_slop": 0.02,             # 归一化坐标下的位移阈值，小于它算点击
    "long_press_ms": 500,         # 按住超过该时长算长按
    "double_tap_gap_ms": 320,     # 两次 tap 间距小于该值且位置接近，合并为 double_tap
    "wait_threshold_ms": 150,     # 动作间空隙超过该值插入 wait 步骤
    "max_wait_ms": 15000,         # 超过该空隙视为人为停顿，截断为该值
    "swipe_sample_points": 12,    # swipe 轨迹最多保留的采样点数（0 = 不保留轨迹）
    # 借鉴 GameScrcpy：时间量化、相对时间轴、崩溃安全草稿
    "time_quantize_ms": 10,       # 毫秒值取整步长（0/1 = 不量化）
    "record_at_ms": True,         # 每个步骤写入相对录制起点的 at_ms
    "draft_enabled": True,        # 录制中周期写草稿，崩溃/Ctrl+C 也不丢已录步骤
    "draft_interval_s": 5,

    # 录制时监听的输入节点；留空 = 运行时自动探测触摸屏 + 按键设备
    "record_nodes": [],
    # 额外监听的原始事件（例如车机方控、旋钮），即便无法语义化也会写入 raw 参考
    "keep_raw_events": False,

    # 录制期逐步截图 + 图片基线断言（CLI --screenshots 可临时打开）
    "screenshots": False,
    "shot_mode": "async",              # async=不阻塞事件流 / sync=每步精确但会短暂阻塞
    "shot_delay_ms": 250,              # 动作后等多久抓图，等界面动画稳定
    "shot_dir_suffix": "_shots",
    "baseline_policy": "all",          # all=每步生成断言 / last=仅末步 / none=只存图不断言
    "baseline_threshold": 0.02,        # 允许的差异像素占比
    "baseline_pixel_tolerance": 24,    # 单像素灰度差阈值
    "ignore_status_bar": True,         # 默认忽略顶部 6% 状态栏（时间/电量会变）
    "baseline_ignore_regions": [],     # 额外忽略区域 [[x1,y1,x2,y2], ...]，归一化

    # 回放
    "scale": "stretch",           # stretch | fit | raw
    "speed": 1.0,                # 时间缩放，>1 加速
    "on_error": "stop",           # stop | continue（步骤级 on_fail 可覆盖）
    # 借鉴 GameScrcpy：until 条件等待、步骤级重试、断线退避重连
    "until_timeout_ms": 5000,
    "until_interval_ms": 250,
    "on_fail_retry": 2,
    "reconnect_attempts": 3,
    "reconnect_backoff_ms": 250,
    "default_format": "yaml",     # yaml | json
    "follow_path": False,         # 回放 swipe 时按录制轨迹 path 分段注入
    "swap_xy": False,             # 触摸面板与屏幕坐标系横纵互换时开启
    "key_aliases": {              # Linux KEY_* -> Android KEYCODE_*（可自行扩充）
        "KEY_BACK": "KEYCODE_BACK",
        "KEY_HOME": "KEYCODE_HOME",
        "KEY_MENU": "KEYCODE_MENU",
        "KEY_POWER": "KEYCODE_POWER",
        "KEY_SEARCH": "KEYCODE_SEARCH",
        "KEY_VOLUMEUP": "KEYCODE_VOLUME_UP",
        "KEY_VOLUMEDOWN": "KEYCODE_VOLUME_DOWN",
        "KEY_CAMERA": "KEYCODE_CAMERA",
        "KEY_ENTER": "KEYCODE_ENTER",
        "KEY_DPAD_UP": "KEYCODE_DPAD_UP",
        "KEY_DPAD_DOWN": "KEYCODE_DPAD_DOWN",
        "KEY_DPAD_LEFT": "KEYCODE_DPAD_LEFT",
        "KEY_DPAD_RIGHT": "KEYCODE_DPAD_RIGHT",
        "KEY_DPAD_CENTER": "KEYCODE_DPAD_CENTER",
        "KEY_MEDIA_PLAY_PAUSE": "KEYCODE_MEDIA_PLAY_PAUSE",
        "KEY_MEDIA_NEXT": "KEYCODE_MEDIA_NEXT",
        "KEY_MEDIA_PREVIOUS": "KEYCODE_MEDIA_PREVIOUS",
        # 物理键盘（投屏 A2）：文本编辑 / 导航键
        "KEY_TAB": "KEYCODE_TAB",
        "KEY_DEL": "KEYCODE_DEL",
        "KEY_FORWARDDEL": "KEYCODE_FORWARD_DEL",
        "KEY_ESC": "KEYCODE_ESCAPE",
        "KEY_MOVE_HOME": "KEYCODE_MOVE_HOME",
        "KEY_MOVE_END": "KEYCODE_MOVE_END",
        "KEY_PAGEUP": "KEYCODE_PAGE_UP",
        "KEY_PAGEDOWN": "KEYCODE_PAGE_DOWN",
        "KEY_INSERT": "KEYCODE_INSERT",
    },
    # ADB 自动探测时额外扫描的根目录
    "adb_search_roots": [],

    # 实时投屏（借鉴 GameScrcpy：scrcpy 协议硬编推流 + 控制通道反向注入）
    "mirror_max_size": 1280,        # 视频长边上限，0=原始分辨率（越大越清晰但延迟越高）
    "mirror_bit_rate": 8000000,     # H.264 码率 bps
    "mirror_max_fps": 30,           # 帧率上限，0=不限
    "mirror_server_jar": "",        # scrcpy-server jar 路径；留空用 vendor/ 自带的
    "mirror_auto_download": True,   # vendor/ 缺失时自动从 GitHub 下载
    "mirror_audio": True,           # 设备声音转发到电脑播放（scrcpy opus 音频通道）
    "mirror_external_scrcpy": "",   # 官方 scrcpy.exe 路径；留空用 E:\tools\scrcpy\scrcpy.exe
    # 借鉴 GameScrcpy：USB 抖动/无线瞬断后自动重建会话，而不是直接报错停止
    "mirror_reconnect": True,       # 断线自动重连开关
    "mirror_reconnect_attempts": 3,  # 一次意外中断后最多连续重连几次（连上并出过帧才算恢复）
    "mirror_reconnect_backoff_s": 1.0,  # 重连退避基数（秒），第 n 次等 n*基数

    # 控件透视（uiautomator dump）
    "inspect_retries": 3,          # 动画期 could not get idle state 时的重试次数
    "inspect_u2": False,           # True=优先 uiautomator2 常驻服务（需 pip install uiautomator2）

    # 界面主题：light=浅色（默认） / dark=深色
    "theme": "light",
}


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "adb_case_config.json"


def load_config(path: str | os.PathLike | None = None) -> dict:
    """读取配置文件并与默认值合并；文件不存在时返回纯默认值。"""
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else default_config_path()
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                for k, v in user.items():
                    if k == "key_aliases" and isinstance(v, dict):
                        cfg["key_aliases"].update(v)
                    else:
                        cfg[k] = v
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"配置文件解析失败: {p} -> {exc}")
    return cfg


def write_default_config(path: str | os.PathLike | None = None, force: bool = False) -> Path:
    p = Path(path) if path else default_config_path()
    if p.exists() and not force:
        return p
    p.write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p
