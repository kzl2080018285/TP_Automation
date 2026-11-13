# -*- coding: utf-8 -*-
"""设备 UI 控件透视：uiautomator dump XML -> 扁平控件列表 + 命中判定。

GUI「控件透视」开关用它在投屏画面上叠加全部控件轮廓，
鼠标悬浮时高亮命中控件并展示基本信息（参考 Chrome DevTools Inspect）。
纯解析/纯函数，不依赖 Qt 与真机，可离线自检。
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 布尔属性：dump 里是 "true"/"false" 字符串
_BOOL_KEYS = ("clickable", "long_clickable", "scrollable", "checkable",
              "checked", "enabled", "focusable", "focused", "selected")

# dump 瞬时失败（界面还在动画、系统忙）值得重试；锁屏/权限拒绝不值得
_TRANSIENT_HINTS = ("idle", "timeout", "timed out", "超时", "busy")


def parse_bounds(s: str) -> tuple[int, int, int, int] | None:
    """'[x1,y1][x2,y2]' -> (x1,y1,x2,y2)；非法/零面积返回 None。"""
    m = _BOUNDS_RE.search(s or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _selector(full_class: str, rid: str, text: str, desc: str, nth: int) -> str:
    """给录制/定位用的首选选择器：id > text > desc > class(序号)。"""
    if rid:
        return f'//*[@resource-id="{rid}"]'
    if text:
        return f'//*[@text="{text}"]'
    if desc:
        return f'//*[@content-desc="{desc}"]'
    if not full_class:
        return ""
    return f'//*[@class="{full_class}"]' + (f"[{nth}]" if nth > 1 else "")


def parse_hierarchy(xml_text: str, *, skip_invisible: bool = True) -> list[dict]:
    """把 uiautomator dump 的 XML 展平成控件列表（文档序，父先子后）。

    部分设备 dump/cat 输出前后带提示行或杂散字符，只截取
    <hierarchy>…</hierarchy> 段再解析，避免整段 fromstring 失败。

    skip_invisible 丢弃 visible-to-user="false" 的节点：WebView/视频播放器
    里常有成百上千个不可见占位控件，既画不出来也点不到，纯属噪声。
    """
    text = xml_text or ""
    start = text.find("<hierarchy")
    end = text.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise ValueError("uiautomator dump 输出不含 <hierarchy>（设备不支持或屏幕异常）")
    root = ET.fromstring(text[start:end + len("</hierarchy>")])
    nodes: list[dict] = []

    def walk_children(el, parent_idx, path_parts):
        step_count: dict[str, int] = {}
        for ch in el:
            if ch.tag != "node":
                # hierarchy 等非控件包装层不占层级，继续下钻
                walk_children(ch, parent_idx, path_parts)
                continue
            full_class = ch.get("class", "")
            short_cls = full_class.rsplit(".", 1)[-1] or full_class
            step_count[short_cls] = step_count.get(short_cls, 0) + 1
            nth = step_count[short_cls]
            step = short_cls if nth == 1 else f"{short_cls}[{nth}]"
            cur_path = path_parts + [step]
            visible = ch.get("visible-to-user", "true") != "false"
            bounds = parse_bounds(ch.get("bounds", ""))
            idx = None
            if bounds is not None and (visible or not skip_invisible):
                rid = ch.get("resource-id", "")
                txt = ch.get("text", "")
                desc = ch.get("content-desc", "")
                x1, y1, x2, y2 = bounds
                node = {
                    "class": short_cls,
                    "full_class": full_class,
                    "resource_id": rid,
                    "text": txt,
                    "desc": desc,
                    "package": ch.get("package", ""),
                    "bounds": bounds,
                    "area": (x2 - x1) * (y2 - y1),
                    "depth": nodes[parent_idx]["depth"] + 1 if parent_idx is not None else 0,
                    "parent": parent_idx,
                    "path": "/".join(cur_path),
                    "sel": _selector(full_class, rid, txt, desc, nth),
                    "visible": visible,
                }
                for k in _BOOL_KEYS:
                    node[k] = ch.get(k, "false") == "true"
                idx = len(nodes)
                nodes.append(node)
            walk_children(ch, idx if idx is not None else parent_idx, cur_path)

    walk_children(root, None, [])
    return nodes


def hit_node(nodes: list[dict], x: float, y: float) -> dict | None:
    """命中包含该点的最小面积控件。

    bounds 完全相同的嵌套包装层（FrameLayout 套 Button 很常见）按层级取最深：
    悬浮要看到的是用户真正能点的叶子控件，而不是外层容器。
    """
    best, best_key = None, None
    for n in nodes:
        x1, y1, x2, y2 = n["bounds"]
        if x1 <= x < x2 and y1 <= y < y2:
            key = (n.get("area") or (x2 - x1) * (y2 - y1), -int(n.get("depth", 0)))
            if best_key is None or key < best_key:
                best, best_key = n, key
    return best


def node_matches(n: dict, query: str) -> bool:
    """控件树筛选：类名/id/文本/描述/路径 任意字段包含查询串（不区分大小写）。"""
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = " ".join((n.get("class", ""), n.get("full_class", ""),
                    n.get("resource_id", ""), n.get("text", ""),
                    n.get("desc", ""), n.get("path", ""))).lower()
    return q in hay


def filter_nodes(nodes: list[dict], query: str) -> list[dict]:
    return [n for n in nodes if node_matches(n, query)]


# --------------------------------------------------------------------------
# 控件断言：投屏右键与用例编辑共用同一套选择器
# --------------------------------------------------------------------------
SELECTOR_KEYS = ("id", "class", "text", "desc", "package")
EXPECT_KEYS = ("exists", "text", "contains", "checked", "enabled")

# 选择器字段 -> dump 节点字段（class 单独处理：短类名/完整类名都认）
_SEL_FIELDS = {"id": "resource_id", "class": "class", "text": "text",
               "desc": "desc", "package": "package"}


def match_selector(n: dict, sel: dict) -> bool:
    """控件是否命中选择器：写了的字段必须全部相等，空字段不参与。"""
    if not sel:
        return True
    for key, field in _SEL_FIELDS.items():
        want = sel.get(key)
        if want in (None, ""):
            continue
        got = n.get(field) or ""
        if key == "class":
            if got != want and (n.get("full_class") or "") != want:
                return False
        elif got != want:
            return False
    return True


def find_nodes(nodes: list[dict], sel: dict) -> list[dict]:
    return [n for n in nodes or [] if match_selector(n, sel)]


def selector_from_node(nodes: list[dict], node: dict) -> dict:
    """命中控件 -> 断言选择器：优先 id > 描述 > 文本 > 类名。

    文本优先级放低：它可能正是要断言的值，写进选择器后控件文本一变，
    断言只会报「找不到控件」而不是「文本不对」，定位不到问题。
    同条件命中多个时补 index（第几个），避免断言到同名兄弟控件上。
    """
    if not node:
        return {}
    cls = node.get("class") or ""
    cands: list[dict] = []
    if node.get("resource_id"):
        cands.append({"id": node["resource_id"]})
    if node.get("desc"):
        cands.append({"desc": node["desc"]})
    if node.get("text"):
        cands.append({"text": node["text"], **({"class": cls} if cls else {})})
    if cls:
        cands.append({"class": cls})
    for sel in cands:
        hits = find_nodes(nodes, sel)
        pos = next((i for i, n in enumerate(hits) if n is node), -1)
        if pos < 0:
            continue
        out = dict(sel)
        if len(hits) > 1:
            out["index"] = pos + 1
        return out
    return {"class": cls, "index": 1} if cls else {}


def selector_label(sel: dict) -> str:
    """选择器的人类可读摘要（日志 / 断言消息）。"""
    if not isinstance(sel, dict) or not sel:
        return "任意控件"
    parts = [f"{caption}={_short(str(sel[key]), 40)}"
             for key, caption in (("id", "id"), ("class", "类名"), ("text", "文本"),
                                  ("desc", "描述"), ("package", "包名"))
             if sel.get(key)]
    if sel.get("index"):
        parts.append(f"第{int(sel['index'])}个")
    return " ".join(parts) or "任意控件"


def check_widget(nodes: list[dict], selector: dict, expect: dict) -> tuple[bool, str]:
    """控件断言：exists / text / contains / checked / enabled。

    返回 (通过, 说明)；失败说明直接写进回放报告，要能一眼看出是
    「没找到控件」还是「找到了但值不对」。contains 同时看 text 与 desc
    （同一段文案在不同 App 里可能落在任一字段）。
    """
    sel = selector or {}
    exp = expect or {}
    label = selector_label(sel)
    hits = find_nodes(nodes, sel)
    try:
        idx = max(1, int(sel.get("index") or 1))
    except (TypeError, ValueError):
        idx = 1
    if not exp.get("exists", True):
        # 「控件不存在」不看 index：只要还有任何命中就算失败。
        # 否则同名控件的第 1 个还在、只是第 2 个没了也会误判通过
        if hits:
            return False, f"控件仍存在: {label}（命中 {len(hits)} 个）"
        return True, f"控件已不存在: {label}"
    if len(hits) < idx:
        return False, f"未找到控件: {label}（命中 {len(hits)} 个，需要第 {idx} 个）"
    n = hits[idx - 1]
    problems: list[str] = []
    if "text" in exp:
        want, got = str(exp["text"]), n.get("text") or ""
        if got != want:
            problems.append(f"文本应为 {want!r}，实际 {got!r}")
    if "contains" in exp:
        want = str(exp["contains"])
        got = f"{n.get('text') or ''} {n.get('desc') or ''}".strip()
        if want not in got:
            problems.append(f"文本未包含 {want!r}，实际 {got!r}")
    if "checked" in exp and bool(n.get("checked")) != bool(exp["checked"]):
        problems.append("应" + ("已勾选" if exp["checked"] else "未勾选")
                        + "，实际" + ("已勾选" if n.get("checked") else "未勾选"))
    if "enabled" in exp and bool(n.get("enabled", True)) != bool(exp["enabled"]):
        problems.append("应" + ("可用" if exp["enabled"] else "禁用")
                        + "，实际" + ("可用" if n.get("enabled", True) else "禁用"))
    if problems:
        return False, f"控件不满足断言（{label}）：" + "；".join(problems)
    return True, f"控件断言通过: {label}" + (f" text={n.get('text')!r}"
                                            if n.get("text") else "")


# dumpsys window 里的焦点行：
#   mCurrentFocus=Window{8f8f8f8 u0 com.demo/com.demo.MainActivity}
#   mFocusedApp=ActivityRecord{63f4b2c u0 com.demo/.MainActivity t42}
# 包名可能是单词（android），Activity 可能带 $内部类；从首个 `名字/名字` 处截。
_FOCUS_RE = re.compile(r"([A-Za-z][\w.]*)/([\w.$]+)")


def parse_focus(text: str) -> dict:
    """解析 dumpsys window 的前台信息 -> 应用包名 + 界面（Activity）。

    mFocusedApp（ActivityRecord）代表真正的界面，mCurrentFocus 常是输入法/
    系统弹窗的窗口，所以前者优先、后者兜底。相对 Activity 名（.MainActivity）
    按包名补全，复制出来可直接拼 `am start -n 包名/Activity`。

    返回 {"package", "activity", "window"}，取不到的字段是空串。
    """
    out = {"package": "", "activity": "", "window": ""}
    for line in (text or "").splitlines():
        if "mFocusedApp=" in line:
            key = "activity"
        elif "mCurrentFocus=" in line:
            key = "window"
        else:
            continue
        m = _FOCUS_RE.search(line)
        if not m:
            continue   # mCurrentFocus=null / 无窗口名
        pkg, act = m.group(1), m.group(2)
        if act.startswith("."):
            act = pkg + act
        if key == "activity":
            out["activity"] = act
            out["package"] = pkg
        else:
            out["window"] = act
            if not out["package"]:
                out["package"] = pkg
    if not out["activity"]:
        out["activity"] = out["window"]   # 无 ActivityRecord 的 ROM 退回窗口名
    return out


def foreground_package(nodes: list[dict]) -> str:
    """控件树里出现最多的 package：dump 拿不到 dumpsys 时的包名兜底。"""
    counts: dict[str, int] = {}
    for n in nodes or []:
        p = n.get("package") or ""
        if p:
            counts[p] = counts.get(p, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def node_label(n: dict) -> str:
    """徽章文案：类名#id 或 类名 "文本"，用于树列表与信息卡标题。"""
    cls = n.get("class") or "?"
    rid = n.get("resource_id") or ""
    if rid:
        return f"{cls}#{rid.rsplit('/', 1)[-1]}"
    txt = (n.get("text") or n.get("desc") or "").strip()
    if txt:
        return f'{cls} "{_short(txt, 16)}"'
    return cls


def node_info(n: dict) -> str:
    """悬浮信息卡文案：类名 / id / 文本 / 描述 / bounds / 交互属性 / 路径。"""
    lines = [n.get("class") or n.get("full_class") or "?"]
    if n.get("full_class") and n.get("full_class") != n.get("class"):
        lines.append(n["full_class"])
    if n.get("resource_id"):
        lines.append(f"id: {n['resource_id']}")
    if n.get("text"):
        lines.append(f"text: {_short(n['text'], 120)}")
    if n.get("desc"):
        lines.append(f"desc: {_short(n['desc'], 120)}")
    x1, y1, x2, y2 = n["bounds"]
    lines.append(f"bounds: [{x1},{y1}][{x2},{y2}]  {x2 - x1}x{y2 - y1}")
    flags = [label for label, key in
             (("可点击", "clickable"), ("可长按", "long_clickable"),
              ("可滚动", "scrollable"), ("可勾选", "checkable"))
             if n.get(key)]
    if n.get("checked"):
        flags.append("已勾选")
    if not n.get("enabled", True):
        flags.append("已禁用")
    if n.get("focused"):
        flags.append("已聚焦")
    if flags:
        lines.append(" · ".join(flags))
    if n.get("path"):
        lines.append(f"path: {_short(n['path'], 72)}")
    if n.get("sel"):
        lines.append(f"sel: {_short(n['sel'], 72)}")
    return "\n".join(lines)


def logical_screen_size(video_size, wm_size) -> tuple[int, int]:
    """控件 bounds 所在的逻辑屏尺寸。

    uiautomator 的 bounds 用的是「当前旋转后」的显示坐标，而不少 ROM 的
    wm size 始终报竖屏物理尺寸：两者长宽比不一致时按视频帧（与显示同源、
    等比缩放）纠正，避免横屏时轮廓整体压扁/错位。
    """
    try:
        vw, vh = int(video_size[0]), int(video_size[1])
    except (TypeError, ValueError, IndexError):
        vw = vh = 0
    try:
        ww, wh = int(wm_size[0]), int(wm_size[1])
    except (TypeError, ValueError, IndexError):
        ww = wh = 0
    if ww <= 0 or wh <= 0:
        return (vw, vh) if vw > 0 and vh > 0 else (0, 0)
    if vw <= 0 or vh <= 0:
        return ww, wh

    def ratio(a, b):
        return a / b if b else 0.0

    direct = abs(ratio(vw, vh) - ratio(ww, wh))
    swapped = abs(ratio(vw, vh) - ratio(wh, ww))
    return (wh, ww) if swapped + 1e-3 < direct else (ww, wh)


def _parse_or_raise(xml_text: str) -> list[dict]:
    nodes = parse_hierarchy(xml_text)
    if not nodes:
        raise ValueError("uiautomator dump 未解析到控件（锁屏/安全界面可能禁止 dump）")
    return nodes


def _u2_dump(adb) -> str:
    """可选数据源：uiautomator2 常驻服务（单次 200~500ms，不等 idle）。

    u2 服务端遍历全部 accessibility 窗口根（getWindowRoots），状态栏/dock
    等系统窗口也在树里；原生 uiautomator dump 只有活动应用窗口。
    未安装 uiautomator2 时抛 ImportError，由调用方回退 adb dump。
    """
    import uiautomator2 as u2
    serial = getattr(adb, "serial", "") or ""
    dev = _U2_DEVICES.get(serial)
    if dev is None:
        dev = u2.connect(serial) if serial else u2.connect()
        _U2_DEVICES[serial] = dev
    try:
        return dev.dump_hierarchy()
    except Exception:
        _U2_DEVICES.pop(serial, None)   # 掉线/服务死掉不缓存坏连接
        raise


_U2_DEVICES: dict = {}


def dump_xml(adb, *, use_u2: bool = False) -> str:
    """原始控件树 XML：use_u2 时优先 u2 全窗口（含状态栏/dock），
    未开启/不可用回退原生 adb dump（仅活动窗口）。回放断言与透视共用。"""
    if use_u2:
        try:
            return _u2_dump(adb)
        except Exception:   # noqa: BLE001  未安装/连接失败都回退 adb
            pass
    return adb.uiautomator_dump()


def _is_transient(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _TRANSIENT_HINTS)


def fetch_nodes(adb, *, retries: int = 3, retry_delay: float = 0.6,
                use_u2: bool = False, on_retry=None) -> list[dict]:
    """抓当前界面控件树：u2 直连(可选) > adb dump，瞬时失败自动重试。

    「could not get idle state」是动画/视频界面的常态，单次失败就报死会让
    透视看起来完全不可用；第一次失败后改用 --compressed（丢掉非关键节点、
    更快返回）再试，仍然失败才把最后一次错误抛给调用方。
    """
    last: BaseException | None = None
    if use_u2:
        try:
            return _parse_or_raise(_u2_dump(adb))
        except Exception as exc:   # noqa: BLE001  未安装/连接失败都回退 adb
            last = exc
            if on_retry:
                on_retry(f"uiautomator2 直连不可用，回退 adb dump：{exc}")

    attempts = max(1, int(retries))
    for i in range(attempts):
        try:
            if i == 0:
                xml = adb.uiautomator_dump()
            else:
                try:
                    xml = adb.uiautomator_dump(compressed=True)
                except TypeError:   # 桩/旧实现没有 compressed 参数
                    xml = adb.uiautomator_dump()
            return _parse_or_raise(xml)
        except Exception as exc:   # noqa: BLE001
            last = exc
            if i + 1 >= attempts or not _is_transient(exc):
                raise
            if on_retry:
                on_retry(f"控件抓取第 {i + 1} 次失败（{_short(str(exc), 60)}），重试中…")
            time.sleep(max(0.0, retry_delay) * (i + 1))
    raise last if last is not None else ValueError("控件抓取失败")
