# -*- coding: utf-8 -*-
"""测试用例文件：YAML / JSON 双格式、结构校验、参数化、include / repeat 展开。

用例是普通文本文件，录制产物可以直接手工改：
  - 坐标是 0~1 归一化值，换分辨率自动换算
  - params + ${key} 做参数化
  - include 复用公共流程，repeat 做循环
  - assert_* 步骤做结果校验
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    import yaml
    HAS_YAML = True
except ImportError:  # 零依赖兜底：仍可用 .json 用例
    yaml = None
    HAS_YAML = False

CASE_FORMAT = "adbcase.case"

KNOWN_ACTIONS = {
    "launch", "tap", "double_tap", "long_press", "swipe", "keyevent", "text",
    "wait", "wait_until", "assert_text", "assert_not_text", "assert_activity",
    "assert_widget", "assert_image", "screenshot", "repeat", "include", "shell",
    "comment",
}
REQUIRED = {
    "launch": ("package",),
    "tap": ("at",), "double_tap": ("at",), "long_press": ("at",),
    "swipe": ("from", "to"),
    "keyevent": ("key",), "text": ("text",),
    "wait": ("ms",),
    "wait_until": ("until",),
    "assert_text": ("text",), "assert_not_text": ("text",),
    "assert_activity": ("contains",),
    "assert_widget": ("selector", "expect"),
    "assert_image": ("image",),
    "screenshot": ("path",),
    "repeat": ("count", "steps"),
    "include": ("file",),
    "shell": ("cmd",),
}
VAR = re.compile(r"\$\{([^}]+)\}")
UNTIL_KEYS = {"text", "not_text", "activity", "image", "not_image"}
ON_FAIL_VALUES = {"retry", "skip", "abort", "continue", "stop"}
# 控件断言（assert_widget）：选择器字段与断言项
WIDGET_SELECTOR_KEYS = ("id", "class", "text", "desc", "package")
WIDGET_EXPECT_KEYS = ("exists", "text", "contains", "checked", "enabled")


class CaseError(RuntimeError):
    pass


def new_case(name: str, profile: dict, steps: list[dict], *,
             tags: list[str] | None = None, params: dict | None = None,
             raw_events: list[dict] | None = None) -> dict:
    case: dict[str, Any] = {
        "version": 1,
        "format": CASE_FORMAT,
        "name": name,
        "tags": tags or [],
        # 录制时的设备档案只作参考；回放时会重新探测目标设备
        "device": {
            "model": profile.get("model", ""),
            "serial": profile.get("serial", ""),
            "size": profile.get("size", [0, 0]),
            "rotation": profile.get("rotation", 0),
            "touch_abs": profile.get("touch_abs", [0, 0]),
            "abs_source": profile.get("abs_source", ""),
            "coord_ratio": profile.get("coord_ratio", []),
            "inputs": profile.get("inputs", []),
        },
        "params": params or {},
        "steps": steps,
    }
    if raw_events:
        case["raw_events"] = raw_events
    return case


def detect_format(path: str | Path) -> str:
    return "json" if str(path).lower().endswith(".json") else "yaml"


def save_case(case: dict, path: str | Path, fmt: str | None = None) -> Path:
    p = Path(path)
    fmt = (fmt or detect_format(p)).lower()
    if fmt not in ("yaml", "json"):
        raise CaseError(f"不支持的格式: {fmt}")
    if fmt == "yaml" and not HAS_YAML:
        raise CaseError("未安装 PyYAML：请 pip install pyyaml，或改用 --format json")
    p.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        content = json.dumps(case, indent=2, ensure_ascii=False) + "\n"
    else:
        content = yaml.safe_dump(case, sort_keys=False, allow_unicode=True,
                                 default_flow_style=None, width=120)
    # 借鉴 GameScrcpy Recorder.cpp:160 writeFileAtomically：
    # 先写临时文件再原子替换，录制中途断电/崩溃不会留下半个用例
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return p


def load_case(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise CaseError(f"用例文件不存在: {p}")
    text = p.read_text(encoding="utf-8")
    try:
        if detect_format(p) == "json":
            case = json.loads(text)
        else:
            if not HAS_YAML:
                raise CaseError("未安装 PyYAML：请 pip install pyyaml，或把用例转成 .json（convert 命令）")
            case = yaml.safe_load(text)
    except CaseError:
        raise
    except json.JSONDecodeError as exc:
        raise CaseError(f"JSON 解析失败 {p} 第{exc.lineno}行第{exc.colno}列: {exc.msg}") from exc
    except Exception as exc:
        # PyYAML 的 ParserError/ScannerError 不是 ValueError，必须显式包装成
        # CaseError，否则 CLI 会直接抛 traceback
        if HAS_YAML and isinstance(exc, yaml.YAMLError):
            mark = getattr(exc, "problem_mark", None)
            where = f" 第{mark.line + 1}行第{mark.column + 1}列" if mark is not None else ""
            problem = getattr(exc, "problem", None) or str(exc)
            raise CaseError(f"YAML 解析失败 {p}{where}: {problem}") from exc
        raise
    if not isinstance(case, dict):
        raise CaseError(f"用例根节点必须是对象: {p}")
    return case


def validate_case(case: dict) -> list[str]:
    """返回问题列表；空列表表示校验通过。"""
    errors: list[str] = []
    if case.get("version") not in (1, None):
        errors.append(f"不支持的 version: {case.get('version')}")
    fmt = case.get("format")
    if fmt is not None and fmt != CASE_FORMAT:
        errors.append(f"不支持的 format: {fmt!r}（应为 {CASE_FORMAT}；旧用例可省略该字段）")
    if not case.get("name"):
        errors.append("缺少 name")
    steps = case.get("steps")
    if not isinstance(steps, list):
        return errors + ["steps 必须是列表"]

    def _check(items: list, where: str) -> None:
        for i, s in enumerate(items):
            tag = f"{where}[{i}]"
            if not isinstance(s, dict):
                errors.append(f"{tag} 不是对象")
                continue
            act = s.get("action")
            if act not in KNOWN_ACTIONS:
                errors.append(f"{tag} 未知 action: {act!r}")
                continue
            for key in REQUIRED.get(act, ()):
                if key not in s:
                    errors.append(f"{tag} ({act}) 缺少字段 {key}")
            for key in ("at", "from", "to"):
                v = s.get(key)
                if v is not None:
                    if not (isinstance(v, (list, tuple)) and len(v) == 2
                            and all(isinstance(x, (int, float)) for x in v)):
                        errors.append(f"{tag} {key} 应为 [x, y] 两个数字")
                    elif not all(0.0 <= float(x) <= 1.0 for x in v):
                        errors.append(f"{tag} {key} 坐标应为 0~1 归一化值")
            if "at_ms" in s and (not isinstance(s["at_ms"], int) or s["at_ms"] < 0):
                errors.append(f"{tag} at_ms 应为非负整数（录制相对时间）")
            if "on_fail" in s and s["on_fail"] not in ON_FAIL_VALUES:
                errors.append(f"{tag} on_fail 应为 {sorted(ON_FAIL_VALUES)}")
            if "until" in s or act == "wait_until":
                u = s.get("until")
                if not isinstance(u, dict):
                    errors.append(f"{tag} until 必须是对象")
                else:
                    if not any(k in u for k in UNTIL_KEYS):
                        errors.append(f"{tag} until 至少需要一个条件 {sorted(UNTIL_KEYS)}")
                    for k in ("timeout_ms", "interval_ms", "retry"):
                        if k in u and (not isinstance(u[k], int) or u[k] < 0):
                            errors.append(f"{tag} until.{k} 应为非负整数")
            if act == "repeat":
                inner = s.get("steps")
                if not isinstance(inner, list):
                    errors.append(f"{tag} repeat.steps 必须是列表")
                else:
                    _check(inner, f"{tag}.steps")
                if not isinstance(s.get("count"), int) or s["count"] < 1:
                    errors.append(f"{tag} repeat.count 应为正整数")
            if act == "assert_image":
                if not isinstance(s.get("image"), str) or not s["image"].strip():
                    errors.append(f"{tag} assert_image.image 应为相对路径字符串")
                th = s.get("threshold", 0.02)
                if not isinstance(th, (int, float)) or not 0 <= float(th) <= 1:
                    errors.append(f"{tag} assert_image.threshold 应为 0~1")
                for key in ("region",):
                    if key in s and not (isinstance(s[key], list) and len(s[key]) == 4):
                        errors.append(f"{tag} {key} 应为 [x1,y1,x2,y2]")
                for r in s.get("ignore_regions", []) or []:
                    if not (isinstance(r, list) and len(r) == 4):
                        errors.append(f"{tag} ignore_regions 每项应为 [x1,y1,x2,y2]")
            if act == "assert_widget":
                sel = s.get("selector")
                if not isinstance(sel, dict) or not any(
                        isinstance(sel.get(k), str) and sel[k].strip()
                        for k in WIDGET_SELECTOR_KEYS):
                    errors.append(f"{tag} assert_widget.selector 至少需要一个定位字段 "
                                  f"{WIDGET_SELECTOR_KEYS}")
                elif "index" in sel and (not isinstance(sel["index"], int)
                                         or isinstance(sel["index"], bool)
                                         or sel["index"] < 1):
                    # bool 是 int 的子类：index: true 不能当成 1 放过
                    errors.append(f"{tag} assert_widget.selector.index 应为正整数")
                exp = s.get("expect")
                if not isinstance(exp, dict) or not any(k in exp for k in WIDGET_EXPECT_KEYS):
                    errors.append(f"{tag} assert_widget.expect 至少需要一个断言项 "
                                  f"{WIDGET_EXPECT_KEYS}")
                else:
                    for k in ("exists", "checked", "enabled"):
                        if k in exp and not isinstance(exp[k], bool):
                            errors.append(f"{tag} assert_widget.expect.{k} 应为布尔值")
                    for k in ("text", "contains"):
                        if k in exp and not isinstance(exp[k], str):
                            errors.append(f"{tag} assert_widget.expect.{k} 应为字符串")
    _check(steps, "steps")
    return errors


def render_params(case: dict, overrides: dict | None = None) -> dict:
    """把 ${key} / ${params.key} 替换为 params 与命令行 --param 的值。"""
    values: dict[str, Any] = {}
    values.update(case.get("params") or {})
    values.update(overrides or {})

    def lookup(key: str) -> Any:
        key = key.strip()
        if key.startswith("params."):
            key = key[7:]
        if key in values:
            return values[key]
        cur: Any = values
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                raise CaseError(f"未定义的参数: ${{{key}}}")
        return cur

    def walk(obj: Any) -> Any:
        if isinstance(obj, str):
            def sub(m):
                val = lookup(m.group(1))
                return str(val)
            return VAR.sub(sub, obj)
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return obj

    out = copy.deepcopy(case)
    out["steps"] = walk(case.get("steps", []))
    out["device"] = walk(case.get("device", {}))
    return out


def expand_steps(case: dict, base_dir: str | Path | None = None,
                 *, _depth: int = 0) -> list[dict]:
    """把 include / repeat 展开成一维步骤序列，便于回放与报告。"""
    if _depth > 5:
        raise CaseError("include 嵌套超过 5 层，可能存在循环引用")
    base = Path(base_dir) if base_dir else Path.cwd()
    out: list[dict] = []
    for step in case.get("steps", []):
        if not isinstance(step, dict):
            continue  # 非对象步骤由 validate_case 报错，展开时跳过避免崩
        act = step.get("action")
        if act == "comment":
            continue
        if act == "include":
            child_path = (base / step["file"]).resolve()
            child = load_case(child_path)
            child = render_params(child, {**(case.get("params") or {}),
                                          **(step.get("with") or {})})
            for s in expand_steps(child, child_path.parent, _depth=_depth + 1):
                s.setdefault("_source", str(child_path.name))
                out.append(s)
        elif act == "repeat":
            count = int(step["count"])
            inner = expand_steps({"steps": step["steps"]}, base, _depth=_depth + 1)
            for i in range(count):
                for s in inner:
                    c = copy.deepcopy(s)
                    c["_loop"] = i + 1
                    out.append(c)
        else:
            out.append(copy.deepcopy(step))
    return out


def convert(path: str | Path, target: str) -> Path:
    case = load_case(path)
    src = Path(path)
    dst = src.with_suffix(".json" if target == "json" else ".yaml")
    if dst == src:
        raise CaseError("源与目标格式相同")
    return save_case(case, dst, target)
