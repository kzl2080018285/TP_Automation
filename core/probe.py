# -*- coding: utf-8 -*-
"""设备与输入节点运行时探测。

原则：绝不把 /dev/input/eventN 写死在代码或用例里。
录制时记录「设备名 + 能力」，回放时重新探测并按名字匹配当前节点。
"""

from __future__ import annotations

import re

from .adb import Adb, AdbError

_BLOCK = re.compile(r"(?:add device \d+|Device \d+)\s*:\s*(/dev/input/event\d+)", re.I)
_NAME = re.compile(r"\bname\s*:\s*\"([^\"]+)\"", re.I)
_ABS_X = re.compile(r"(ABS_MT_POSITION_X|ABS_X|0035)\b[^:]*:\s*[^,]*,\s*min\s*(\d+)\s*,\s*max\s*(\d+)", re.I)
_ABS_Y = re.compile(r"(ABS_MT_POSITION_Y|ABS_Y|0036)\b[^:]*:\s*[^,]*,\s*min\s*(\d+)\s*,\s*max\s*(\d+)", re.I)
_KEY_TOKEN = re.compile(r"\bKEY_[A-Z0-9_]+\b")


def pick_touch_device(inputs: list[dict]) -> dict | None:
    """从多个触摸设备中挑主屏。

    真机上常有 input_mt_wrapper + 触控笔 + 手势层同时上报，
    全监听会把一次点击录成两三个动作，所以只认一个主设备。
    """
    cand = [d for d in inputs
            if d.get("is_touch") and d.get("abs_x_max", 0) > 0 and d.get("abs_y_max", 0) > 0]
    if not cand:
        return None

    def score(d: dict):
        name = (d.get("name") or "").lower()
        preferred = any(k in name for k in
                        ("wrapper", "touchscreen", "touch_screen", "main_ts", "panel"))
        return (0 if preferred else 1, -int(d["abs_x_max"]))

    return sorted(cand, key=score)[0]


def _parse_getevent_info(raw: str) -> list[dict]:
    devices: list[dict] = []
    matches = list(_BLOCK.finditer(raw))
    for i, m in enumerate(matches):
        chunk = raw[m.start(): matches[i + 1].start() if i + 1 < len(matches) else len(raw)]
        name_m = _NAME.search(chunk)
        name = name_m.group(1) if name_m else ""
        x_m, y_m = _ABS_X.search(chunk), _ABS_Y.search(chunk)
        max_x = int(x_m.group(3)) if x_m else 0
        max_y = int(y_m.group(3)) if y_m else 0
        keys = sorted(set(_KEY_TOKEN.findall(chunk)))
        has_mt = "ABS_MT_TRACKING_ID" in chunk or "ABS_MT_POSITION_X" in chunk or "0039" in chunk
        has_abs = bool(x_m and y_m)
        devices.append({
            "node": m.group(1),
            "name": name,
            "is_touch": has_abs or has_mt or "BTN_TOUCH" in chunk,
            "abs_x_max": max_x,
            "abs_y_max": max_y,
            "keys": keys,
        })
    return devices


def nodes_of(inputs: list[dict]) -> list[str]:
    """从能力表挑选监听节点：主触摸设备 + 真实硬按键设备。

    只监听 1 个主触摸设备（避免 wrapper / 触控笔重复上报同一次点击）；
    明显无关的节点（指纹、耳机插孔、马达、距离感应）排除。
    """
    touch = pick_touch_device(inputs)
    wanted: list[str] = [touch["node"]] if touch else []

    skip = ("headset", "button jack", "haptics", "tof", "fingerprint", "sensor")
    for d in inputs:
        name = (d.get("name") or "").lower()
        if d.get("keys") and not any(s in name for s in skip):
            if d["node"] not in wanted:
                wanted.append(d["node"])
    return wanted


def probe(adb: Adb) -> dict:
    """返回可序列化、可直接写进用例头部的设备档案。

    尺寸/密度/旋转/型号由 adb.facts() 一次 shell 批量取回（单项缺失自动回退），
    输入节点单独用 getevent 读：整个探测只花 2 次 adb 往返。
    每一步单独容错：定制 ROM 上 wm size / dumpsys / getevent 经常单项失效，
    旧版任一项抛错就整体失败，界面上只看到“探测中…”卡死、像按钮失效。
    单项失败记进 errors，能力表仍尽量填完并返回。
    """
    serial = adb.resolve_serial()
    errors: list[str] = []

    try:
        facts = adb.facts()
    except AdbError as exc:          # facts 内部已逐项容错，这里是最后一道兜底
        facts = {}
        errors.append(f"设备信息: {exc}")
    size = [int(v) for v in (facts.get("size") or (0, 0))]
    density = int(facts.get("density") or 0)
    rotation = int(facts.get("rotation") or 0)
    getprop = [str(facts.get("model") or ""), str(facts.get("release") or ""),
               str(facts.get("sdk") or "")]
    errors.extend(str(e) for e in (facts.get("errors") or []))

    try:
        raw_info = adb.getevent_info()
        inputs = _parse_getevent_info(raw_info)
        info_error = "" if inputs else "getevent 输出未解析到输入设备"
    except AdbError as exc:
        inputs, info_error = [], str(exc)
    if info_error:
        errors.append(f"输入节点: {info_error}")

    touch_nodes = [d["name"] or d["node"] for d in inputs if d["is_touch"]]
    key_nodes = [d["name"] or d["node"] for d in inputs if d["keys"]]

    touch = pick_touch_device(inputs)
    if touch:
        touch_abs = [touch["abs_x_max"], touch["abs_y_max"]]
        abs_source = f"getevent:{touch['name'] or touch['node']}"
    elif size[0] > 0 and size[1] > 0:
        touch_abs = [max(1, size[0] - 1), max(1, size[1] - 1)]
        abs_source = "wm_size_fallback"
    else:
        # 尺寸和 getevent 都拿不到时才真的没辙：留空由回放侧默认值兜底
        touch_abs = [0, 0]
        abs_source = "none"

    # 内核上报的坐标上限常是屏幕像素的整数倍（如 9 倍），比例一致即可；
    # 横纵比例不一致通常意味着面板坐标系与屏幕不一致，需要人工确认。
    ratio = [round(touch_abs[0] / max(1, size[0]), 3),
             round(touch_abs[1] / max(1, size[1]), 3)]
    coord_warning = abs(ratio[0] - ratio[1]) > 0.05 * max(ratio) if min(ratio) > 0 else False

    return {
        "serial": serial,
        "model": getprop[0],
        "android_release": getprop[1],
        "sdk": getprop[2],
        "size": size,
        "density": density,
        "rotation": rotation,
        "touch_abs": touch_abs,
        "abs_source": abs_source,
        "coord_ratio": ratio,
        "coord_warning": coord_warning,
        "inputs": inputs,
        "touch_nodes": touch_nodes,
        "key_nodes": key_nodes,
        "record_nodes": nodes_of(inputs),
        "info_error": info_error,
        "errors": errors,
        "facts_batched": bool(facts.get("batched")),
    }


def resolve_nodes(adb: Adb, profile: dict, *, refresh: bool = False) -> list[str]:
    """确定录制要监听的节点。

    用例里存的是设备名，重启/换机导致 eventN 漂移时能按名字重新对上。
    """
    now = probe(adb) if refresh or not profile.get("inputs") else profile
    return nodes_of(now.get("inputs", []))


def touch_abs_of(profile: dict) -> list[int]:
    t = profile.get("touch_abs") or [0, 0]
    if t[0] > 0 and t[1] > 0:
        return [int(t[0]), int(t[1])]
    size = profile.get("size") or [1080, 1920]
    return [max(1, int(size[0]) - 1), max(1, int(size[1]) - 1)]
