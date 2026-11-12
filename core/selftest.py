# -*- coding: utf-8 -*-
"""内置自检：不依赖真机，用合成 getevent 流跑通完整链路。"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .adb import AdbError
from .cases import (CaseError, expand_steps, load_case, new_case, render_params,
                    save_case, validate_case)
from .config import DEFAULTS
from .draft import DraftWriter
from .image_baseline import (DEFAULT_IGNORE_STATUS_BAR, HAS_PILLOW,
                             ShotCapture, compare_images, inject_baselines)
from .listener import RawEvent, iter_text, parse_line
from .runner import Runner
from .translate import Translator

DEMO = """\
[     100.000000] /dev/input/event4: EV_ABS       ABS_MT_TRACKING_ID   00000001
[     100.000000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_X    0000012c
[     100.000000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_Y    00000096
[     100.000000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
[     100.080000] /dev/input/event4: EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[     100.080000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
[     101.000000] /dev/input/event4: EV_ABS       ABS_MT_TRACKING_ID   00000002
[     101.000000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_X    00000384
[     101.000000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_Y    000001f4
[     101.000000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
[     101.100000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_Y    00000064
[     101.100000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
[     101.200000] /dev/input/event4: EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[     101.200000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
[     102.000000] /dev/input/event3: EV_KEY       KEY_BACK             00000001
[     102.010000] /dev/input/event3: EV_SYN       SYN_REPORT           00000000
[     103.000000] /dev/input/event3: 0001         009e                 00000001
"""

# 两次快速点击，用于验证 double_tap 合并后截图序号的保留
DBL = """\
[     100.000000] /dev/input/event4: EV_ABS       ABS_MT_TRACKING_ID   00000001
[     100.000000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_X    0000012c
[     100.000000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_Y    00000096
[     100.000000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
[     100.060000] /dev/input/event4: EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[     100.060000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
[     100.200000] /dev/input/event4: EV_ABS       ABS_MT_TRACKING_ID   00000002
[     100.200000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_X    0000012c
[     100.200000] /dev/input/event4: EV_ABS       ABS_MT_POSITION_Y    00000096
[     100.200000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
[     100.260000] /dev/input/event4: EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[     100.260000] /dev/input/event4: EV_SYN       SYN_REPORT           00000000
"""

PROFILE = {
    "serial": "SELFTEST", "model": "VirtualHeadUnit",
    "size": [1920, 720], "rotation": 0,
    "touch_abs": [1920, 720], "abs_source": "getevent",
    "inputs": [
        {"node": "/dev/input/event4", "name": "demo_ts", "is_touch": True,
         "abs_x_max": 1920, "abs_y_max": 720, "keys": []},
        {"node": "/dev/input/event3", "name": "demo_keys", "is_touch": False,
         "abs_x_max": 0, "abs_y_max": 0, "keys": ["KEY_BACK", "KEY_HOME"]},
    ],
}


class DummyAdb:
    """dry-run 下 runner 不会真正调用 ADB 方法，占位即可。"""

    def __getattr__(self, _name):
        def _noop(*_a, **_kw):
            return ""
        return _noop


def _check(cond: bool, label: str, results: list, detail: str = "") -> None:
    results.append((bool(cond), label, detail))


def run_selftest() -> tuple[bool, list[tuple[bool, str, str]]]:
    results: list[tuple[bool, str, str]] = []
    cfg = dict(DEFAULTS)

    # 1. 双格式行解析
    events = list(iter_text(DEMO))
    _check(len(events) == 17, "getevent 流解析（label + hex 混排）", results,
           f"解析到 {len(events)} 条事件")
    hex_ev = parse_line("[ 103.000000] /dev/input/event3: 0001 009e 00000001")
    _check(hex_ev is not None and hex_ev.kind == "EV_KEY" and hex_ev.code == "KEY_BACK",
           "hex 模式 KEY_BACK 解码", results, str(hex_ev))
    rel = parse_line("[ 100.080000] /dev/input/event4: EV_ABS ABS_MT_TRACKING_ID ffffffff")
    _check(rel is not None and rel.value == -1, "抬手事件 ffffffff -> -1", results, str(rel))

    # 2. 事件 -> 语义动作
    tr = Translator(cfg, PROFILE)
    tr.feed(events)
    steps = tr.finish()
    actions = [s["action"] for s in steps]
    _check("tap" in actions, "生成 tap 动作", results, str(actions))
    _check("swipe" in actions, "生成 swipe 动作", results, str(actions))
    _check(actions.count("keyevent") == 2, "生成 keyevent 动作（label + hex 各一）",
           results, str(actions))
    _check("wait" in actions, "动作间隙生成 wait", results, str(actions))
    tap = next(s for s in steps if s["action"] == "tap")
    _check(abs(tap["at"][0] - 0.1563) < 0.002 and abs(tap["at"][1] - 0.2083) < 0.002,
           "坐标归一化正确 (300/1920, 150/720)", results, str(tap["at"]))
    swipe = next(s for s in steps if s["action"] == "swipe")
    _check(all(0 <= v <= 1 for v in swipe["from"] + swipe["to"]),
           "swipe 起止点均在 0~1", results, f"{swipe['from']} -> {swipe['to']}")
    key = next(s for s in steps if s["action"] == "keyevent")
    _check(key["key"] == "KEYCODE_BACK", "KEY_BACK -> KEYCODE_BACK", results, key["key"])

    # 2b. 混合来源：投屏合成事件是视频像素，与设备面板事件共用一条时间轴
    _mx = Translator(cfg, PROFILE)
    _mx.node_abs["mirror"] = (639.0, 359.0)
    _t0 = 500.0

    def _tap_events(node: str, x: int, y: int, ts: float) -> list:
        return [RawEvent(ts, node, "EV_ABS", "ABS_MT_TRACKING_ID", 1),
                RawEvent(ts, node, "EV_ABS", "ABS_MT_POSITION_X", x),
                RawEvent(ts, node, "EV_ABS", "ABS_MT_POSITION_Y", y),
                RawEvent(ts, node, "EV_SYN", "SYN_REPORT", 0),
                RawEvent(ts + 0.06, node, "EV_ABS", "ABS_MT_TRACKING_ID", -1)]

    _mx.feed(_tap_events("mirror", 320, 180, _t0))
    _mx.feed(_tap_events("/dev/input/event4", 960, 360, _t0 + 5.0))
    _mixed = [s for s in _mx.finish() if s["action"] == "tap"]
    _check(len(_mixed) == 2
           and abs(_mixed[0]["at"][0] - 320 / 639) < 0.002
           and abs(_mixed[0]["at"][1] - 180 / 359) < 0.002
           and abs(_mixed[1]["at"][0] - 0.5) < 0.002
           and abs(_mixed[1]["at"][1] - 0.5) < 0.002,
           "投屏事件按视频像素、设备事件按面板尺度各自归一化", results,
           str([s["at"] for s in _mixed]))
    _sw = Translator({**cfg, "swap_xy": True}, PROFILE)
    _sw.node_abs["mirror"] = (639.0, 359.0)
    _sw.feed(_tap_events("mirror", 320, 180, _t0))
    _sw_tap = [s for s in _sw.finish() if s["action"] == "tap"]
    _check(bool(_sw_tap) and abs(_sw_tap[0]["at"][0] - 320 / 639) < 0.002,
           "投屏坐标已是屏幕坐标系，不适用面板 swap_xy", results, str(_sw_tap))

    # 3. 用例文件读写 + 校验
    with tempfile.TemporaryDirectory(prefix="adbcase_") as td:
        td = Path(td)
        case = new_case("自检用例", PROFILE, steps, tags=["selftest"],
                        params={"pkg": "com.demo.hvac"})
        case["steps"].insert(0, {"action": "launch", "package": "${pkg}", "desc": "启动应用"})
        case["steps"].append({"action": "assert_text", "text": "空调", "desc": "断言界面"})
        case["steps"].append({"action": "repeat", "count": 3,
                              "steps": [{"action": "wait", "ms": 10}], "desc": "循环"})

        yml = save_case(case, td / "demo.yaml", "yaml")
        jsn = save_case(case, td / "demo.json", "json")
        loaded_y, loaded_j = load_case(yml), load_case(jsn)
        _check(loaded_y["steps"] == loaded_j["steps"], "YAML / JSON 双格式读写一致",
               results, f"{yml.name} / {jsn.name}")
        _check(yml.read_text(encoding="utf-8").startswith("version: 1"),
               "YAML 产物人类可读（键不排序）", results)

        errs = validate_case(loaded_y)
        _check(not errs, "用例结构校验通过", results, "; ".join(errs))

        # 4. 参数化 + include/repeat 展开
        rendered = render_params(loaded_y, {"pkg": "com.real.hvac"})
        _check(rendered["steps"][0]["package"] == "com.real.hvac",
               "${pkg} 参数替换（CLI --param 覆盖）", results,
               rendered["steps"][0]["package"])
        flat = expand_steps(rendered, td)
        _check(len([s for s in flat if s["action"] == "wait"]) >= 3,
               "repeat 展开为多个步骤", results, f"展开后 {len(flat)} 步")
        _check(all(s["action"] not in ("repeat", "include") for s in flat),
               "展开后无嵌套控制步骤", results)

        # 5. 回放（dry-run，不连设备）
        runner = Runner(DummyAdb(), cfg, dict(PROFILE), dry_run=True)
        report = runner.run(loaded_y, params={"pkg": "com.real.hvac"}, save_report=False)
        _check(report.failed == 0, "dry-run 回放无失败步骤", results,
               f"通过 {report.passed}/{len(report.steps)}")
        cmds = " ".join(s["command"] for s in report.steps)
        _check("input tap" in cmds and "input keyevent KEYCODE_BACK" in cmds,
               "回放命令生成正确", results, cmds[:120])
        _check("monkey -p com.real.hvac" in cmds, "参数化值进入回放命令", results)

        # 6. 逐步截图回调、_shot 透传与基线断言注入
        seq_calls: list[tuple[float, str]] = []

        def on_action(ts, action):
            seq_calls.append((ts, action["action"]))
            return len(seq_calls)

        tr2 = Translator(cfg, PROFILE, on_action=on_action)
        tr2.feed(events)
        steps2 = tr2.finish()
        marked = [s for s in steps2 if "_shot" in s]
        _check(len(seq_calls) == 4, "每个语义动作触发一次截图回调", results,
               str([a for _, a in seq_calls]))
        _check(len(marked) == 4, "_shot 序号透传到 finish()", results,
               str([s["_shot"] for s in marked]))

        shots = {s["_shot"]: f"demo_shots/step_{s['_shot']:04d}.png" for s in marked}
        clone = lambda: [dict(s) for s in steps2]  # noqa: E731
        all_steps = inject_baselines(clone(), shots, policy="all", threshold=0.03,
                                     ignore_regions=[list(DEFAULT_IGNORE_STATUS_BAR)],
                                     wait_before_ms=200)
        _check(sum(1 for s in all_steps if s["action"] == "assert_image") == 4,
               "policy=all 每步生成 assert_image", results, f"{len(all_steps)} 步")
        _check(all("_shot" not in s for s in all_steps),
               "内部字段 _shot 不写进用例", results)
        _check(all(s.get("ignore_regions") and s.get("wait_before_ms") == 200
                   for s in all_steps if s["action"] == "assert_image"),
               "阈值/忽略区/等待参数写入断言步骤", results)
        img_yml = save_case(new_case("基线用例", PROFILE, all_steps), td / "baseline.yaml", "yaml")
        _check(not validate_case(load_case(img_yml)),
               "含基线的用例存盘/加载/校验通过", results,
               "; ".join(validate_case(load_case(img_yml))))
        yml_text = img_yml.read_text(encoding="utf-8")
        _check("&id" not in yml_text and "*id" not in yml_text,
               "基线用例 YAML 无共享对象锚点（每步可独立改）", results)
        _check(sum(1 for s in inject_baselines(clone(), shots, policy="last")
                   if s["action"] == "assert_image") == 1,
               "policy=last 只校验最终界面", results)
        none_steps = inject_baselines(clone(), shots, policy="none")
        _check(not any(s["action"] == "assert_image" for s in none_steps)
               and all("_shot" not in s for s in none_steps),
               "policy=none 只存图不断言", results)

        calls: list[str] = []

        def on_dbl(_ts, action):
            calls.append(action["action"])
            return len(calls)

        tr3 = Translator(cfg, PROFILE, on_action=on_dbl)
        tr3.feed(list(iter_text(DBL)))
        dbl = [s for s in tr3.finish() if s["action"] == "double_tap"]
        _check(len(dbl) == 1 and dbl[0].get("_shot") == 2,
               "double_tap 合并保留最后一次截图基线", results, str(dbl))

        # 7. 截图落盘（sync 模式，不接触真机）
        class CapAdb(DummyAdb):
            def screencap(self, dest):
                p = Path(dest)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
                return True

        cap = ShotCapture(CapAdb(), td, "cap_shots", delay_ms=0, mode="sync")
        s1, s2 = cap.submit(1.0), cap.submit(2.0)
        cap_result = cap.close()
        _check(s1 == 1 and s2 == 2 and cap_result == {
            1: "cap_shots/step_0001.png", 2: "cap_shots/step_0002.png"},
            "ShotCapture 序号与相对用例路径", results, str(cap_result))
        _check((td / "cap_shots/step_0001.png").exists(),
               "截图文件按序号落盘", results)

        cap_async = ShotCapture(CapAdb(), td, "async_shots", delay_ms=0, mode="async")
        for _ in range(3):
            cap_async.submit()
        async_result = cap_async.close()
        _check(len(async_result) == 3 and cap_async.stat.saved == 3,
               "async 截图队列全部消费完成", results, str(async_result))

        # 8. assert_image 用例校验
        img_case = new_case("图像用例", PROFILE, [
            {"action": "assert_image", "image": "shots/b.png", "threshold": 0.02,
             "pixel_tolerance": 24, "ignore_regions": [list(DEFAULT_IGNORE_STATUS_BAR)]},
        ])
        _check(not validate_case(img_case), "assert_image 用例校验通过", results,
               "; ".join(validate_case(img_case)))
        bad_case = new_case("非法用例", PROFILE, [
            {"action": "assert_image", "image": "x.png", "threshold": 1.5}])
        _check(bool(validate_case(bad_case)), "非法 threshold 被校验拦截", results)

        # 9. 图像比对 + 回放集成（Pillow）
        if HAS_PILLOW:
            from PIL import Image

            idir = td / "img"
            idir.mkdir(exist_ok=True)
            base = idir / "base.png"
            Image.new("RGB", (80, 60), (10, 20, 30)).save(base)
            Image.new("RGB", (80, 60), (10, 20, 30)).save(idir / "same.png")
            changed = Image.new("RGB", (80, 60), (10, 20, 30))
            changed.paste((200, 0, 0), (0, 0, 20, 15))
            changed.save(idir / "changed.png")
            Image.new("RGB", (160, 120), (10, 20, 30)).save(idir / "big.png")

            r1 = compare_images(base, idir / "same.png", threshold=0.02)
            _check(r1.get("ok") and r1["diff_ratio"] == 0,
                   "相同图片比对通过", results, str(r1.get("diff_ratio")))
            r2 = compare_images(base, idir / "changed.png", threshold=0.01,
                                diff_path=idir / "diff.png")
            _check(not r2.get("ok") and r2["diff_ratio"] > 0,
                   "界面差异被检出并生成差异图", results, f"diff={r2.get('diff_ratio')}")
            _check((idir / "diff.png").exists(), "差异图已保存", results)
            _check(compare_images(base, idir / "changed.png", threshold=0.5).get("ok"),
                   "放宽阈值后通过", results)
            _check(compare_images(base, idir / "changed.png", threshold=0.01,
                                  ignore_regions=[[0, 0, 0.3, 0.3]]).get("ok"),
                   "ignore_regions 屏蔽差异区域", results)
            r5 = compare_images(base, idir / "big.png", threshold=0.02)
            _check(r5.get("ok") and r5.get("resized"),
                   "跨分辨率自动缩放比对", results)

            shots_dir = td / "shots"
            shots_dir.mkdir(exist_ok=True)
            bl = shots_dir / "b.png"
            Image.new("RGB", (80, 60), (10, 20, 30)).save(bl)
            case_img = new_case("图像回放", PROFILE, [
                {"action": "assert_image", "image": "shots/b.png",
                 "threshold": 0.02, "wait_before_ms": 0},
            ])

            class ShotAdb(DummyAdb):
                color = (10, 20, 30)

                def screencap(self, dest):
                    p = Path(dest)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", (80, 60), self.color).save(p)
                    return True

            sa = ShotAdb()
            rep1 = Runner(sa, cfg, dict(PROFILE), report_dir=td / "reports",
                          case_dir=td).run(case_img, save_report=False)
            _check(rep1.failed == 0, "回放中基线比对通过", results)

            sa.color = (200, 0, 0)
            rep2 = Runner(sa, cfg, dict(PROFILE), report_dir=td / "reports",
                          case_dir=td).run(case_img, save_report=False)
            _check(rep2.failed == 1, "界面变化被基线断言拦截", results)

            rep3 = Runner(sa, cfg, dict(PROFILE), report_dir=td / "reports",
                          case_dir=td, update_baseline=True).run(case_img,
                                                                save_report=False)
            _check(rep3.failed == 0, "--update-baseline 刷新基线不报错", results)
            with Image.open(bl) as im:
                _check(im.getpixel((0, 0)) == (200, 0, 0),
                       "基线文件被当前屏幕实际覆盖", results)
        else:
            results.append((True, "图像比对（未装 Pillow，跳过）", ""))

        # 10. 借鉴 GameScrcpy：时间量化 / at_ms / snapshot / format / 原子写
        fresh = tr.finish()
        _check(all("at_ms" in s for s in fresh), "每步带 at_ms 相对时间", results)
        _check(all(s["at_ms"] % 10 == 0 for s in fresh), "毫秒值量化到 10ms", results)
        _check(all(s["duration_ms"] % 10 == 0 for s in fresh if "duration_ms" in s),
               "duration_ms 同步量化", results)
        _check(load_case(yml).get("format") == "adbcase.case",
               "format 字段写入用例", results)
        bad_fmt = dict(load_case(yml))
        bad_fmt["format"] = "something.else"
        _check(any("format" in e for e in validate_case(bad_fmt)),
               "非法 format 被严格校验拦截", results)
        before = len(seq_calls)
        s1, s2 = tr2.snapshot(), tr2.snapshot()
        _check(s1 == s2 and len(seq_calls) == before,
               "snapshot 可反复调用且不触发截图回调", results)
        _check(not list(td.glob("*.tmp")), "原子写无 .tmp 残留", results)

        # 11. until 条件等待 / 动作重试 / 步骤级失败策略 / 时间轴 / 掉线恢复
        class CondAdb(DummyAdb):
            def __init__(self):
                self.hits = 0

            def uiautomator_dump(self):
                self.hits += 1
                return "<node text='READY'/>" if self.hits >= 3 else "<node text='LOADING'/>"

        ca = CondAdb()
        cond_case = new_case("条件等待", PROFILE, [
            {"action": "wait_until",
             "until": {"text": "READY", "timeout_ms": 1000, "interval_ms": 10}}])
        rep = Runner(ca, cfg, dict(PROFILE), report_dir=td / "reports",
                     case_dir=td).run(cond_case, save_report=False)
        _check(rep.failed == 0 and ca.hits >= 3,
               "until 轮询直到条件满足（不靠固定延时）", results, f"hits={ca.hits}")

        class RetryAdb(DummyAdb):
            def __init__(self):
                self.taps = 0

            def input_tap(self, x, y):
                self.taps += 1
                return ""

            def uiautomator_dump(self):
                return "READY" if self.taps >= 2 else "LOADING"

        ra = RetryAdb()
        retry_case = new_case("动作重试", PROFILE, [
            {"action": "tap", "at": [0.5, 0.5],
             "until": {"text": "READY", "timeout_ms": 60, "interval_ms": 10, "retry": 2}}])
        rep = Runner(ra, cfg, dict(PROFILE), report_dir=td / "reports",
                     case_dir=td).run(retry_case, save_report=False)
        _check(rep.failed == 0 and ra.taps == 2,
               "until 超时自动重做动作", results, f"taps={ra.taps}")

        class FlakyAdb(DummyAdb):
            def __init__(self):
                self.calls = 0

            def input_keyevent(self, key):
                self.calls += 1
                if self.calls < 3:
                    raise AdbError("boom")
                return ""

        fa = FlakyAdb()
        flaky_case = new_case("步骤级重试", PROFILE, [
            {"action": "keyevent", "key": "KEYCODE_BACK",
             "on_fail": "retry", "on_fail_retry": 3}])
        rep = Runner(fa, cfg, dict(PROFILE), report_dir=td / "reports",
                     case_dir=td).run(flaky_case, save_report=False)
        _check(rep.failed == 0 and fa.calls == 3,
               "on_fail=retry 重试到成功", results, f"calls={fa.calls}")

        class FailAdb(DummyAdb):
            def input_keyevent(self, key):
                raise AdbError("boom")

        skip_case = new_case("跳过失败", PROFILE, [
            {"action": "keyevent", "key": "KEYCODE_BACK", "on_fail": "skip"},
            {"action": "wait", "ms": 10}])
        rep = Runner(FailAdb(), cfg, dict(PROFILE), report_dir=td / "reports",
                     case_dir=td).run(skip_case, save_report=False)
        _check(rep.failed == 1 and len(rep.steps) == 2,
               "on_fail=skip 失败后继续执行", results)

        tl_case = new_case("时间轴", PROFILE, [
            {"action": "keyevent", "key": "KEYCODE_BACK", "at_ms": 300},
            {"action": "wait", "ms": 200, "at_ms": 200},
            {"action": "tap", "at": [0.5, 0.5], "at_ms": 100}])
        rep = Runner(DummyAdb(), cfg, dict(PROFILE), dry_run=True, timeline=True,
                     report_dir=td / "reports", case_dir=td).run(tl_case, save_report=False)
        acts = [s["action"] for s in rep.steps]
        _check(acts == ["tap", "wait", "keyevent"],
               "timeline 按 at_ms 稳定排序", results, str(acts))
        _check("timeline" in rep.steps[1]["command"],
               "timeline 忽略 wait 的额外 sleep", results)

        class RecoverAdb(DummyAdb):
            def __init__(self):
                self.calls = 0

            def input_keyevent(self, key):
                self.calls += 1
                if self.calls == 1:
                    raise AdbError("device not found")
                return ""

            def shell(self, *args):
                return "ADBCASE_OK"

        rc = RecoverAdb()
        rec_case = new_case("掉线恢复", PROFILE,
                            [{"action": "keyevent", "key": "KEYCODE_BACK"}])
        rep = Runner(rc, dict(cfg, reconnect_attempts=1, reconnect_backoff_ms=1),
                     dict(PROFILE), report_dir=td / "reports",
                     case_dir=td).run(rec_case, save_report=False)
        _check(rep.failed == 0 and rc.calls == 2,
               "疑似掉线时探活并自动重试", results, f"calls={rc.calls}")

        bad_until = new_case("非法until", PROFILE, [
            {"action": "tap", "at": [0.5, 0.5], "until": {"bogus": 1}}])
        _check(any("until" in e for e in validate_case(bad_until)),
               "非法 until 被校验拦截", results)
        bad_fields = new_case("非法字段", PROFILE, [
            {"action": "wait", "ms": 10, "on_fail": "whatever", "at_ms": -1}])
        errs = validate_case(bad_fields)
        _check(any("on_fail" in e for e in errs) and any("at_ms" in e for e in errs),
               "非法 on_fail / at_ms 被校验拦截", results)

        draft = DraftWriter(tr2, PROFILE, td / "d.yaml", "yaml",
                            1.0, "d", [], {})
        _check(draft.path.name == "d.draft.yaml", "草稿文件命名正确", results)
        draft.stop(remove=False)
        draft2 = DraftWriter(tr2, PROFILE, td / "d2.yaml", "yaml",
                             1.0, "d2", [], {})
        draft2.write_once()
        _check(draft2.path.exists()
               and not validate_case(load_case(draft2.path)),
               "草稿原子写入且结构可校验", results,
               "; ".join(validate_case(load_case(draft2.path))) if draft2.path.exists() else "文件未生成")
        draft2.stop(remove=False)

        # 12. GUI 依赖的无 Qt 能力：可停止监听 + 回放回调/中止
        from .listener import EventStreamController
        ctl = EventStreamController()
        _check(not ctl.stopped, "EventStreamController 初始未停止", results)
        ctl.stop()
        _check(ctl.stopped, "EventStreamController.stop 生效", results)

        # 多节点监听：AOSP 的 getevent 只接受一个 device，多传会打印 usage 直接退出
        # （CET-AL00 实测），所以必须每个节点各起一个进程
        from . import listener as _lst

        class _FakeProc:
            def __init__(self):
                self.stdout = iter(())
                self.terminated = False

            def poll(self):
                return None if not self.terminated else 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        class _FakeAdb:
            prefix = ["adb"]

        _calls: list[str] = []
        _procs: list[_FakeProc] = []
        _orig_open = _lst._open_getevent

        def _fake_open(_adb, node, _hex):
            _calls.append(node)
            p = _FakeProc()
            # 每个节点吐一条合法事件：验证多进程输出能被合并解析
            p.stdout = iter([f"[ 100.000000] {node}: EV_ABS ABS_MT_POSITION_X 00000010\n"])
            _procs.append(p)
            return p

        _nodes = ["/dev/input/event5", "/dev/input/event2", "/dev/input/event1"]
        _ctl2 = EventStreamController()
        try:
            _lst._open_getevent = _fake_open
            _merged = list(_lst.iter_live(_FakeAdb(), _nodes, controller=_ctl2))
        finally:
            _lst._open_getevent = _orig_open
        _check(sorted(_calls) == sorted(_nodes) and len(_calls) == len(_nodes),
               "多输入节点各自单独一个 getevent 进程（单 device 参数）", results,
               str(_calls))
        _check(len(_merged) == len(_nodes)
               and {e.node for e in _merged} == set(_nodes)
               and all(e.code == "ABS_MT_POSITION_X" for e in _merged),
               "多进程事件合并解析（每节点都能产出事件）", results,
               f"events={len(_merged)}")

        # 停止时要能终止所有进程（多进程时漏杀会留一堆 adb 进程）
        _p1, _p2 = _FakeProc(), _FakeProc()
        _ctl3 = EventStreamController()
        _ctl3.attach(_p1)
        _ctl3.attach(_p2)
        _ctl3.stop()
        _check(_p1.terminated and _p2.terminated and _ctl3.proc is _p1,
               "停止监听会终止全部 getevent 进程", results,
               f"{_p1.terminated}/{_p2.terminated}")

        captured: list[dict] = []
        logs: list[str] = []
        cb_case = new_case("回调用例", PROFILE, [
            {"action": "wait", "ms": 10}, {"action": "keyevent", "key": "KEYCODE_BACK"}])
        rep = Runner(DummyAdb(), cfg, dict(PROFILE), dry_run=True,
                     log=logs.append, on_step=captured.append,
                     report_dir=td / "reports", case_dir=td).run(cb_case, save_report=False)
        _check(len(captured) == len(rep.steps) and any("[回放]" in x for x in logs),
               "Runner 的 log / on_step 回调可用", results,
               f"steps={len(captured)} logs={len(logs)}")
        stopped = Runner(DummyAdb(), cfg, dict(PROFILE), dry_run=True,
                         log=logs.append, stop_callback=lambda: True,
                         report_dir=td / "reports", case_dir=td).run(cb_case, save_report=False)
        _check(len(stopped.steps) == 0, "Runner.stop_callback 立即中止", results)

        # 13. 自检发现的问题回归：非法 YAML / 尺寸兜底 / 参数容错 / 快速停止
        (td / "broken.yaml").write_text("version: 1\nsteps: [\n", encoding="utf-8")
        try:
            load_case(td / "broken.yaml")
            bad_yaml_ok = False
        except CaseError as exc:
            bad_yaml_ok = "YAML 解析失败" in str(exc)
        except Exception:
            bad_yaml_ok = False
        _check(bad_yaml_ok, "非法 YAML 包装为 CaseError（CLI 不再抛 traceback）", results)

        zero_case = new_case("零尺寸", {**PROFILE, "size": [0, 0]},
                             [{"action": "tap", "at": [0.5, 0.5]}])
        rep = Runner(DummyAdb(), cfg, {**PROFILE, "size": [0, 0]}, dry_run=True,
                     report_dir=td / "reports", case_dir=td).run(zero_case, save_report=False)
        cmd = rep.steps[0]["command"] if rep.steps else ""
        _check(rep.failed == 0 and "input tap 0 0" not in cmd,
               "device.size 为 0 时回退默认分辨率", results, cmd)

        none_case = new_case("空时长", PROFILE, [
            {"action": "swipe", "from": [0.1, 0.1], "to": [0.9, 0.9], "duration_ms": None},
            {"action": "wait", "ms": None}])
        rep = Runner(DummyAdb(), cfg, dict(PROFILE), dry_run=True,
                     report_dir=td / "reports", case_dir=td).run(none_case, save_report=False)
        _check(rep.failed == 0 and (rep.steps[0]["command"] if rep.steps else "").endswith(" 300"),
               "duration_ms / ms 为 None 时用默认值不崩溃", results,
               str(rep.steps[0]["command"] if rep.steps else ""))

        slow = new_case("慢等待", PROFILE, [{"action": "wait", "ms": 1000},
                                            {"action": "wait", "ms": 1000}])
        t0 = time.perf_counter()
        rep = Runner(DummyAdb(), cfg, dict(PROFILE), report_dir=td / "reports",
                     case_dir=td,
                     stop_callback=lambda: time.perf_counter() - t0 > 0.2
                     ).run(slow, save_report=False)
        elapsed = time.perf_counter() - t0
        _check(elapsed < 0.6, "wait 期间停止能快速响应（分片 sleep）", results,
               f"{elapsed:.2f}s / {len(rep.steps)} 步")

        mixed = {"version": 1, "format": "adbcase.case", "name": "mixed",
                 "steps": ["不是对象", {"action": "wait", "ms": 10}]}
        _check(len(expand_steps(mixed)) == 1 and len(validate_case(mixed)) == 1,
               "非对象步骤：校验报错且展开不崩溃", results)

        # 14. 投屏协议（scrcpy v2.7）：控制消息打包与缓冲读取，不连设备
        import socket as _socket
        import struct as _struct
        from . import mirror as _mir
        touch = _mir.pack_touch(_mir.ACTION_DOWN, 100, 200, 1280, 720)
        _check(len(touch) == 32 and touch[0] == 2 and touch[1] == 0,
               "投屏触摸消息 32 字节（type/action 在前）", results, str(len(touch)))
        _check(_struct.unpack(">iiHH", touch[10:22]) == (100, 200, 1280, 720),
               "投屏触摸坐标与屏幕尺寸字段", results)
        up = _mir.pack_touch(_mir.ACTION_UP, 100, 200, 1280, 720)
        _check(_struct.unpack(">H", up[22:24])[0] == 0, "投屏抬手压力为 0", results)
        key = _mir.pack_keycode(_mir.ACTION_DOWN, _mir.KEYCODE_BACK)
        _check(len(key) == 14 and _struct.unpack(">i", key[2:6])[0] == 4,
               "投屏按键消息 14 字节", results)
        scr = _mir.pack_scroll(10, 20, 1280, 720, 0.0, -1.0)
        _check(len(scr) == 21 and _struct.unpack(">h", scr[15:17])[0] == -0x8000,
               "投屏滚动 i16 定点数（-1.0 -> -0x8000）", results)
        _check(_mir.pack_text("hi") == bytes([1, 0, 0, 0, 2]) + b"hi",
               "投屏文本注入消息", results)

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

        rd = _mir._SockReader(_ChunkSock([b"h" * 5, _socket.timeout(), b"t" * 7]),
                              lambda: False)
        _check(rd.read(12) == b"h" * 5 + b"t" * 7,
               "投屏帧头分片+超时不丢字节（防流失步）", results)
        _check(_mir.find_server_jar(DEFAULTS, allow_download=False).is_file(),
               "scrcpy-server jar 就位（vendor/）", results)

        # 音频通道连接顺序：服务端等三条通道都 accept 才写 codec id，
        # 先读 codec id 再连控制会互等死锁（真机曾现）
        import threading as _threading
        import time as _time
        _srv = _socket.socket()
        _srv.bind(("127.0.0.1", 0))
        _srv.listen(3)
        _srv_port = _srv.getsockname()[1]

        def _fake_scrcpy_server():
            video = _srv.accept()[0]
            video.sendall(b"\x00")  # 视频 dummy byte：accept 后立即写
            audio = _srv.accept()[0]
            control = _srv.accept()[0]
            # 三条通道都 accept 完才写音频 codec id（与真实服务端一致）
            audio.sendall(_struct.pack(">I", 0x6F707573))
            _time.sleep(0.5)
            for c in (video, audio, control):
                c.close()

        _t = _threading.Thread(target=_fake_scrcpy_server, daemon=True)
        _t.start()
        _m = object.__new__(_mir.ScrcpyMirror)
        _m._stopped = False
        _m._proc = None
        _m._port = _srv_port
        _m.audio = True
        _m._video_sock = _m._audio_sock = _m._ctrl_sock = None
        _m._audio_codec_id = 0
        from collections import deque as _deque
        _m._server_log = _deque()
        try:
            _m._connect_sockets()
            _ok = (_m._audio_codec_id == 0x6F707573 and _m._video_sock is not None
                   and _m._audio_sock is not None and _m._ctrl_sock is not None)
        except Exception:
            _ok = False
        finally:
            for _s in (_m._video_sock, _m._audio_sock, _m._ctrl_sock):
                if _s is not None:
                    _s.close()
            _srv.close()
            _t.join(2)
        _check(_ok, "音频通道先连控制再读 codec id（防死锁）", results)

        # opus 解码输出 fltp 平面浮点，必须转 s16 交错才能喂声卡
        if _mir.has_decoder():
            import av as _av
            _f = _av.AudioFrame(format="fltp", layout="stereo", samples=480)
            _f.sample_rate = 48000
            _m2 = object.__new__(_mir.ScrcpyMirror)
            _m2._audio_resampler = None
            _m2._audio_resampler_key = None
            _outs = _m2._to_s16_frames(_f)
            _arr = _outs[0].to_ndarray()
            _check(_arr.dtype.name == "int16" and _arr.shape == (1, 960),
                   "音频 fltp->s16 交错转换（PyAV 19 兼容）", results,
                   f"{ _arr.dtype} {_arr.shape}")

        # 外挂官方 scrcpy：命令行拼装纯函数 + 未部署检测
        cmd = _mir.build_external_cmd(r"E:\tools\scrcpy\scrcpy.exe", "SER",
                                      max_size=1920, bit_rate=16_000_000,
                                      max_fps=60, audio=False, adb_path="adb/x.exe")
        _check(cmd[0].endswith("scrcpy.exe") and
               [cmd[i + 1] for i, x in enumerate(cmd) if x == "--serial"] == ["SER"] and
               "1920" in cmd and "16000000" in cmd and "60" in cmd and
               cmd[-1] == "--no-audio" and
               "--adb" not in cmd and "--adb-path" not in cmd,
               "外挂 scrcpy 命令行拼装（v2.7 选项集，adb_path 不进命令行）", results,
               " ".join(cmd))
        cmd2 = _mir.build_external_cmd("scrcpy.exe")
        _check("--no-audio" not in cmd2 and "--serial" not in cmd2,
               "外挂 scrcpy 默认带音频、无序列号", results)
        _check(_mir.find_external_scrcpy({"mirror_external_scrcpy":
                                         r"Z:\nope\scrcpy.exe"}) is None,
               "外挂 scrcpy 指定路径不存在返回 None", results)

        # 15. 控件透视：uiautomator XML 解析 / 命中 / 信息文案，不连设备
        from . import inspector as _insp
        _check(_insp.parse_bounds("[10,20][30,40]") == (10, 20, 30, 40),
               "透视 bounds 解析", results)
        _check(_insp.parse_bounds("[0,0][0,0]") is None
               and _insp.parse_bounds("乱码") is None
               and _insp.parse_bounds("") is None,
               "透视非法/零面积 bounds 返回 None", results)
        _xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
                '<hierarchy rotation="0">'
                '<node index="0" class="android.widget.FrameLayout" '
                'package="com.demo" bounds="[0,0][1080,1920]" enabled="true">'
                '<node index="0" class="android.widget.Button" '
                'resource-id="com.demo:id/ok" text="确定" content-desc="确认按钮" '
                'package="com.demo" bounds="[100,200][300,280]" '
                'clickable="true" enabled="true"/>'
                '<node index="1" class="android.view.View" '
                'bounds="[0,0][0,0]"/>'
                '</node></hierarchy>')
        _nodes = _insp.parse_hierarchy(_xml)
        _check(len(_nodes) == 2 and _nodes[1]["class"] == "Button"
               and _nodes[1]["resource_id"] == "com.demo:id/ok"
               and _nodes[1]["text"] == "确定" and _nodes[1]["clickable"] is True
               and _nodes[1]["depth"] == 1 and _nodes[1]["bounds"] == (100, 200, 300, 280),
               "透视控件树展平（零面积节点跳过，属性/层级齐全）", results,
               str(_nodes))
        _check(len(_insp.parse_hierarchy("UI hierchary dumped to: /sdcard/x.xml\n"
                                         + _xml + "\n杂散尾巴")) == 2,
               "透视 dump 输出带前后杂散行仍可解析", results)
        _check(_insp.hit_node(_nodes, 150, 240) is _nodes[1]
               and _insp.hit_node(_nodes, 900, 1000) is _nodes[0]
               and _insp.hit_node(_nodes, 2000, 2000) is None,
               "透视命中取最小面积控件（重叠优先叶子）", results)
        _info = _insp.node_info(_nodes[1])
        _check(all(s in _info for s in ("Button", "com.demo:id/ok", "确定",
                                        "确认按钮", "[100,200][300,280]", "可点击")),
               "透视悬浮信息含类名/id/文本/bounds/交互属性", results, _info)

        class _InspAdb:
            def uiautomator_dump(self):
                return _xml

        class _EmptyAdb:
            def uiautomator_dump(self):
                return ""

        _check(len(_insp.fetch_nodes(_InspAdb())) == 2, "透视 fetch_nodes 走 adb dump", results)
        try:
            _insp.fetch_nodes(_EmptyAdb())
            _empty_raised = False
        except ValueError:
            _empty_raised = True
        _check(_empty_raised, "透视空 dump 报错而非静默返回空列表", results)

        # dump 失败时旧文件还在，直接 cat 会拿陈旧控件树：必须验成功标志
        from .adb import Adb as _Adb

        class _DumpFailAdb(_Adb):
            def __init__(self):
                self.cat_called = False

            def shell(self, *tokens, **_kw):
                if tokens and tokens[0] == "cat":
                    self.cat_called = True
                    return "<hierarchy>stale</hierarchy>"
                return "ERROR: could not get idle state."

        class _DumpOkAdb(_Adb):
            def shell(self, *tokens, **_kw):
                if tokens and tokens[0] == "cat":
                    return _xml
                return "UI hierchary dumped to: /sdcard/window_dump.xml"

        _dfa = _DumpFailAdb()
        try:
            _dfa.uiautomator_dump()
            _dump_rejected = False
        except AdbError:
            _dump_rejected = True
        _check(_dump_rejected and not _dfa.cat_called,
               "dump 失败拒收陈旧文件（不 cat 旧 dump）", results)
        _check(len(_insp.parse_hierarchy(_DumpOkAdb().uiautomator_dump())) == 2,
               "dump 成功标志校验后正常返回控件树", results)

        class _FlakyDumpAdb(DummyAdb):
            def __init__(self):
                self.dumps = 0

            def uiautomator_dump(self):
                self.dumps += 1
                if self.dumps < 3:
                    raise AdbError("could not get idle state")
                return "<node text='READY'/>"

        _fda = _FlakyDumpAdb()
        _flaky_case = new_case("dump抖动", PROFILE, [
            {"action": "wait_until",
             "until": {"text": "READY", "timeout_ms": 1000, "interval_ms": 10}}])
        rep = Runner(_fda, cfg, dict(PROFILE), report_dir=td / "reports",
                     case_dir=td).run(_flaky_case, save_report=False)
        _check(rep.failed == 0 and _fda.dumps >= 3,
               "until 轮询容忍瞬时 dump 失败（不判死整个等待）", results,
               f"dumps={_fda.dumps} failed={rep.failed}")

        class _RaiseAdb:
            def uiautomator_dump(self):
                raise AdbError("dump 被拒绝")

        try:
            _insp.fetch_nodes(_RaiseAdb())
            _prop = False
        except AdbError:
            _prop = True
        _check(_prop, "透视 fetch 传导 adb 错误（GUI 可展示失败原因）", results)

        # 16. 控件透视增强：不可见噪声过滤 / 同级取叶子 / 逻辑屏纠正 / 重试与回退
        _xml2 = ('<hierarchy rotation="1">'
                 '<node class="android.widget.FrameLayout" bounds="[0,0][1920,1080]">'
                 '<node class="android.view.View" visible-to-user="false" '
                 'resource-id="com.demo:id/ghost" bounds="[10,10][500,500]"/>'
                 '<node class="android.widget.LinearLayout" bounds="[300,300][600,420]">'
                 '<node class="android.widget.Button" resource-id="com.demo:id/ok" '
                 'text="确定" bounds="[300,300][600,420]" clickable="true"/>'
                 '</node></node></hierarchy>')
        _n2 = _insp.parse_hierarchy(_xml2)
        _check(len(_n2) == 3 and not any(n["resource_id"].endswith("ghost") for n in _n2),
               "透视默认丢弃 visible-to-user=false 噪声节点", results, str(_n2))
        _btn2 = [n for n in _n2 if n["class"] == "Button"][0]
        _check(_insp.hit_node(_n2, 400, 350) is _btn2,
               "透视同 bounds 嵌套取最深的叶子控件", results)
        _check(_btn2["parent"] is not None
               and _btn2["path"].endswith("LinearLayout/Button")
               and _btn2["sel"] == '//*[@resource-id="com.demo:id/ok"]'
               and _insp.node_label(_btn2) == "Button#ok",
               "透视保存父索引/层级路径/定位表达式/徽章文案", results, str(_btn2))
        _check(_insp.logical_screen_size((1920, 1080), (1080, 1920)) == (1920, 1080)
               and _insp.logical_screen_size((1920, 1080), (1920, 1080)) == (1920, 1080)
               and _insp.logical_screen_size((0, 0), (1080, 2400)) == (1080, 2400),
               "透视横屏逻辑屏尺寸按视频比例纠正", results)
        _check([n["class"] for n in _insp.filter_nodes(_n2, "com.demo:id/ok")] == ["Button"]
               and len(_insp.filter_nodes(_n2, "")) == len(_n2),
               "透视控件树筛选（id 命中 / 空串返回全部）", results)

        _focus = _insp.parse_focus(
            "  mCurrentFocus=Window{8f8f8f8 u0 com.ime/com.ime.ImeWindow}\n"
            "  mFocusedApp=ActivityRecord{63f4b2c u0 com.demo/.MainActivity t42}\n")
        _check(_focus["package"] == "com.demo"
               and _focus["activity"] == "com.demo.MainActivity"
               and _focus["window"] == "com.ime.ImeWindow",
               "前台应用解析：mFocusedApp 优先、相对 Activity 名补全包名",
               results, str(_focus))
        _check(_insp.parse_focus("mCurrentFocus=Window{1 u0 android/android.app.AlertActivity}")
               ["activity"] == "android.app.AlertActivity"
               and _insp.parse_focus("mCurrentFocus=null")["package"] == ""
               and _insp.parse_focus("")["activity"] == "",
               "前台应用解析：单字包名 / null / 空串不报错", results)
        _check(_insp.foreground_package(
                   [{"package": "com.a"}, {"package": "com.b"}, {"package": "com.a"}]) == "com.a"
               and _insp.foreground_package([]) == ""
               and _insp.foreground_package([{"package": ""}]) == "",
               "控件树包名兜底取出现最多的 package", results)

        class _RetryAdb:
            def __init__(self, fails=2):
                self.calls, self.fails = [], fails

            def uiautomator_dump(self, *, compressed=False):
                self.calls.append(compressed)
                if len(self.calls) <= self.fails:
                    raise AdbError("ERROR: could not get idle state.")
                return _xml

        _ra = _RetryAdb()
        _notes: list[str] = []
        _check(len(_insp.fetch_nodes(_ra, retry_delay=0, on_retry=_notes.append)) == 2
               and _ra.calls == [False, True, True] and len(_notes) == 2,
               "透视瞬时失败自动重试并改用 --compressed", results, str(_ra.calls))

        class _HardAdb:
            def __init__(self):
                self.calls = 0

            def uiautomator_dump(self, *, compressed=False):
                self.calls += 1
                raise AdbError("uiautomator dump 失败（锁屏/安全界面可能禁止 dump）: x")

        _ha = _HardAdb()
        try:
            _insp.fetch_nodes(_ha)
            _hard = False
        except AdbError:
            _hard = True
        _check(_hard and _ha.calls == 1, "透视锁屏/拒绝等硬失败不重试", results,
               f"calls={_ha.calls}")

        class _U2Adb:
            serial = "SELFTEST"

            def uiautomator_dump(self, *, compressed=False):
                return _xml

        _u2_backup = _insp._u2_dump
        try:
            def _no_u2(_adb):
                raise ImportError("no uiautomator2")
            _insp._u2_dump = _no_u2
            _check(len(_insp.fetch_nodes(_U2Adb(), use_u2=True)) == 2,
                   "透视可选 u2 数据源不可用时回退 adb dump", results)
        finally:
            _insp._u2_dump = _u2_backup

        class _StderrDumpAdb(_Adb):
            def __init__(self):
                self.calls: list[list[str]] = []

            def shell(self, *tokens, **_kw):
                self.calls.append(list(tokens))
                if tokens and tokens[0] == "cat":
                    return _xml
                return "uiautomator: UI hierchary dumped to: /sdcard/window_dump.xml"

        _sd = _StderrDumpAdb()
        _sd_nodes = _insp.parse_hierarchy(_sd.uiautomator_dump(compressed=True))
        _dump_tokens = _sd.calls[0] if _sd.calls else []
        _check(len(_sd_nodes) == 2
               and "--compressed" in _dump_tokens and "2>&1" in _dump_tokens,
               "compressed 参数与 stderr 合并下发到设备（厂商差异容错）", results,
               str(_sd.calls))

        # 17. 探测能力：getevent 解析 / getevent 单项失效 / wm size 回退 / 整体降级
        from . import probe as _probe_mod
        from .adb import Adb as _Adb3

        _GETEVENT = (
            "add device 1: /dev/input/event4\n"
            '  name:     "input_mt_wrapper"\n'
            "    Events:\n"
            "      ABS (0003): ABS_MT_POSITION_X     : value 0, min 0, max 1920, fuzz 0\n"
            "                  ABS_MT_POSITION_Y     : value 0, min 0, max 720, fuzz 0\n"
            "                  ABS_MT_TRACKING_ID    : value 0, min 0, max 65535, fuzz 0\n"
            "add device 2: /dev/input/event3\n"
            '  name:     "gpio-keys"\n'
            "    Events:\n"
            "      KEY (0001): KEY_BACK                KEY_HOME\n"
            "add device 3: /dev/input/event5\n"
            '  name:     "pen_touch"\n'
            "    Events:\n"
            "      ABS (0003): ABS_MT_POSITION_X     : value 0, min 0, max 1080, fuzz 0\n"
            "                  ABS_MT_POSITION_Y     : value 0, min 0, max 1920, fuzz 0\n")
        _devs = _probe_mod._parse_getevent_info(_GETEVENT)
        _check([d["node"] for d in _devs] == ["/dev/input/event4", "/dev/input/event3",
                                              "/dev/input/event5"]
               and _devs[0]["abs_x_max"] == 1920 and _devs[0]["is_touch"]
               and _devs[1]["keys"] == ["KEY_BACK", "KEY_HOME"] and not _devs[1]["is_touch"],
               "探测 getevent 能力表解析（触摸上限 / 按键 / 非触摸）", results, str(_devs))
        _touch = _probe_mod.pick_touch_device(_devs)
        _check(_touch is not None and _touch["node"] == "/dev/input/event4",
               "探测主触摸设备优先 wrapper（多触摸不重复录）", results,
               str(_touch and _touch.get("name")))

        _noise = [
            {"node": "/dev/input/event1", "name": "fingerprint", "is_touch": False,
             "keys": ["KEY_F1"]},
            {"node": "/dev/input/event2", "name": "tof8801", "is_touch": False,
             "keys": []},
            {"node": "/dev/input/event7", "name": "gpio-keys", "is_touch": False,
             "keys": ["KEY_HOME"]},
        ]
        _check(_probe_mod.nodes_of(_noise) == ["/dev/input/event7"],
               "探测监听节点排除指纹/距离感应等无关设备", results,
               str(_probe_mod.nodes_of(_noise)))

        class _ProbeAdb:
            """只实现探测用到的方法，完全离线。"""

            def __init__(self, *, fail_wm=False, fail_getevent=False):
                self.fail_wm, self.fail_getevent = fail_wm, fail_getevent
                self.getevent_flags: list[str] = []

            def resolve_serial(self):
                return "PROBE0"

            def facts(self):
                out = {"size": [1920, 720], "density": 240, "rotation": 1,
                       "model": "VirtualHeadUnit", "release": "14", "sdk": "34",
                       "errors": [], "batched": True}
                if self.fail_wm:
                    out.update(size=[0, 0], errors=[
                        "屏幕尺寸: 无法获取屏幕尺寸：wm size / dumpsys display / screencap 均无有效输出"])
                return out

            def getevent_info(self):
                if self.fail_getevent:
                    raise AdbError("getevent 无输出：设备可能未授权 shell 读取 /dev/input。")
                return _GETEVENT

        _pa = _ProbeAdb()
        _prof = _probe_mod.probe(_pa)
        _check(_prof["serial"] == "PROBE0" and tuple(_prof["size"]) == (1920, 720)
               and _prof["touch_abs"] == [1920, 720]
               and _prof["abs_source"].startswith("getevent:input_mt_wrapper")
               and _prof["touch_nodes"] == ["input_mt_wrapper", "pen_touch"]
               and _prof["key_nodes"] == ["gpio-keys"]
               and _prof["record_nodes"] == ["/dev/input/event4", "/dev/input/event3"]
               and _prof["rotation"] == 1 and _prof["density"] == 240
               and _prof["errors"] == [] and _prof["model"] == "VirtualHeadUnit",
               "探测能力档案字段完整（屏幕/触摸/按键/型号）", results, str(_prof))
        _check(_prof["coord_ratio"] == [1.0, 1.0] and not _prof["coord_warning"],
               "探测触摸上限与屏幕比例一致（不误报 swap_xy 警告）", results,
               str(_prof["coord_ratio"]))

        _broken = _probe_mod.probe(_ProbeAdb(fail_wm=True, fail_getevent=True))
        _check(tuple(_broken["size"]) == (0, 0) and _broken["touch_abs"] == [0, 0]
               and _broken["abs_source"] == "none" and _broken["record_nodes"] == []
               and len(_broken["errors"]) == 2 and _broken["density"] == 240,
               "探测能力单项失效不再整体崩（错误收集 + 能力降级）", results,
               str(_broken["errors"]))

        # ---- 批量事实读取：5 次 adb 往返 -> 1 次 ----
        class _BatchAdb(_Adb3):
            def __init__(self, raw, *, singles=None, boom=False):
                self.timeout = 15
                self.serial = "BATCH0"
                self.raw = raw
                self.singles = singles or {}
                self.boom = boom
                self.calls: list[str] = []

            def shell_raw(self, script, **_kw):
                self.calls.append("raw")
                if self.boom:
                    raise AdbError("adb 命令超时")
                return self.raw

            def shell(self, *tokens, **_kw):
                key = " ".join(str(t) for t in tokens)
                self.calls.append(key)
                return self.singles.get(key, "")

        _batch_raw = ("__ADBCASE_SIZE__\nPhysical size: 1088x2400\nOverride size: 2400x1088\n"
                      "__ADBCASE_DENSITY__\nPhysical density: 520\nOverride density: 420\n"
                      "__ADBCASE_ROT__\n  SurfaceOrientation: 3\n"
                      "__ADBCASE_PROP__\nCET-AL00\n12\n31\n__ADBCASE_END__\n")
        _ba = _BatchAdb(_batch_raw)
        _bf = _ba.facts()
        _check(_bf["size"] == [2400, 1088] and _bf["density"] == 420
               and _bf["rotation"] == 3 and _bf["model"] == "CET-AL00"
               and _bf["sdk"] == "31" and _bf["errors"] == [] and _bf["batched"]
               and _ba.calls == ["raw"],
               "探测批量读取：一次 shell 取全尺寸/密度/旋转/型号", results, str(_ba.calls))

        _fb = _BatchAdb("__ADBCASE_END__\n", singles={
            "wm size": "Physical size: 1080x1920\n",
            "wm density": "Physical density: 160\n",
            "settings get system user_rotation": "2\n",
        })
        _ff = _fb.facts()
        _check(_ff["size"] == [1080, 1920] and _ff["density"] == 160
               and _ff["rotation"] == 2 and not _ff["batched"]
               and _ff["errors"] == [] and len(_fb.calls) > 1,
               "批量段落缺失时逐项回退（行为与旧实现一致）", results, str(_fb.calls))

        _eb = _BatchAdb("", boom=True, singles={
            "wm size": "Physical size: 720x1280\n",
            "wm density": "Physical density: 320\n",
        })
        _ef = _eb.facts()
        _check(_ef["size"] == [720, 1280] and _ef["density"] == 320
               and any(e.startswith("批量读取") for e in _ef["errors"]),
               "批量 shell 超时仍能逐项取回设备事实", results, str(_ef["errors"]))

        # ---- resolve_serial：指定序列号时 ping 优先，少一次 devices 往返 ----
        class _SerialAdb(_Adb3):
            def __init__(self, *, ping_ok=True, online=None):
                self.serial = "A1"
                self.ping_ok = ping_ok
                self.online = online if online is not None else [{"serial": "A1"}]
                self.calls: list[str] = []

            def shell(self, *tokens, **_kw):
                self.calls.append(" ".join(str(t) for t in tokens))
                return "ADBCASE_OK\n" if self.ping_ok else ""

            def devices(self):
                self.calls.append("devices")
                return self.online

        _sa = _SerialAdb()
        _check(_sa.resolve_serial() == "A1" and _sa.calls == ["echo ADBCASE_OK"],
               "已指定序列号时 ping 优先，省掉一次 devices 往返", results, str(_sa.calls))

        _sf = _SerialAdb(ping_ok=False, online=[{"serial": "B2"}])
        _serr = ""
        try:
            _sf.resolve_serial()
        except AdbError as exc:
            _serr = str(exc)
        _check("不在线" in _serr and _sf.calls[-1] == "devices",
               "ping 失败且序列号不在线时给出当前在线列表", results, _serr)

        class _NoWmAdb(_Adb3):
            def __init__(self):
                self.calls: list[list[str]] = []

            def shell(self, *tokens, **_kw):
                self.calls.append(list(tokens))
                if tokens[:2] == ("wm", "size"):
                    return "/system/bin/sh: wm: not found"
                if tokens[:2] == ("dumpsys", "display"):
                    return "  DisplayDeviceInfo{... cur=1920x1080 app=1920x1080}\n"
                return ""

        _nw = _NoWmAdb()
        _check(_nw.wm_size() == (1920, 1080),
               "wm size 不可用时回退 dumpsys display", results, str(_nw.calls))

        class _OverrideAdb(_Adb3):
            def __init__(self):
                pass

            def shell(self, *tokens, **_kw):
                if tokens[:2] == ("wm", "size"):
                    return "Physical size: 1080x1920\nOverride size: 1920x1080\n"
                return ""

        _check(_OverrideAdb().wm_size() == (1920, 1080),
               "wm size 同时报物理/Override 时优先 Override", results)

        class _RotAdb(_Adb3):
            def __init__(self):
                pass

            def shell_raw(self, script, **_kw):
                return ""

            def shell(self, *tokens, **_kw):
                if tokens[:2] == ("settings", "get"):
                    return "1\n"
                return ""

        _check(_RotAdb().rotation() == 1, "旋转读取回退到 settings user_rotation", results)

        class _GeteventFlagAdb(_Adb3):
            def __init__(self):
                self.flags: list[str] = []

            def shell(self, *tokens, **_kw):
                flag = tokens[1] if len(tokens) > 1 else ""
                self.flags.append(flag)
                if flag == "-lp":
                    raise AdbError("adb 命令超时: getevent -lp")
                return _GETEVENT

        _gf = _GeteventFlagAdb()
        _check("/dev/input/event4" in _gf.getevent_info() and _gf.flags == ["-lp", "-p"],
               "getevent -lp 失败仍回退 -p（不再一步失败）", results, str(_gf.flags))

        class _AllFailGeAdb(_Adb3):
            def __init__(self):
                self.calls = 0

            def shell(self, *tokens, **_kw):
                self.calls += 1
                return "getevent: No such file or directory"

        _af = _AllFailGeAdb()
        try:
            _af.getevent_info()
            _ge_raised = False
        except AdbError:
            _ge_raised = True
        _check(_ge_raised and _af.calls == 3,
               "getevent 全部参数无输出时报错并带上最后输出", results,
               f"calls={_af.calls}")

        # 18. 控件断言（assert_widget）：选择器生成 / 判定 / 用例校验 / 回放
        _AX = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" package="com.demo" bounds="[0,0][1080,1920]">
    <node class="android.widget.TextView" resource-id="com.demo:id/mode" text="制冷"
          package="com.demo" bounds="[40,300][400,380]" />
    <node class="android.widget.Button" text="确定" content-desc="ok" package="com.demo"
          bounds="[40,500][300,580]" />
    <node class="android.widget.Button" text="确定" package="com.demo"
          bounds="[340,500][600,580]" />
    <node class="android.widget.Switch" resource-id="com.demo:id/power" checked="true"
          enabled="false" package="com.demo" bounds="[800,300][1000,380]" />
  </node>
</hierarchy>"""
        _an = _insp.parse_hierarchy(_AX)
        _by_id = {n.get("resource_id"): n for n in _an if n.get("resource_id")}
        _mode = _by_id.get("com.demo:id/mode")
        _power = _by_id.get("com.demo:id/power")
        _oks = [n for n in _an if n.get("text") == "确定"]
        _check(len(_an) == 5 and _mode is not None and _power is not None and len(_oks) == 2,
               "断言用控件树解析（含 checked/enabled）", results, str(len(_an)))
        _sel_mode = _insp.selector_from_node(_an, _mode)
        _check(_sel_mode == {"id": "com.demo:id/mode"},
               "选择器优先取 id（不把断言目标文本写进定位）", results, str(_sel_mode))
        _sel_desc = _insp.selector_from_node(_an, _oks[0])
        _check(_sel_desc.get("desc") == "ok", "无 id 时用描述定位", results, str(_sel_desc))
        _sel_txt = _insp.selector_from_node(_an, _oks[1])
        _check(_sel_txt.get("text") == "确定" and _sel_txt.get("index") == 2,
               "同名控件补 index（取第 2 个）", results, str(_sel_txt))
        _check(_insp.selector_label(_sel_txt).endswith("第2个"),
               "选择器摘要带“第几个”", results, _insp.selector_label(_sel_txt))

        _ok, _msg = _insp.check_widget(_an, _sel_mode, {"text": "制冷"})
        _check(_ok, "控件文本等于判定通过", results, _msg)
        _bad, _msg = _insp.check_widget(_an, _sel_mode, {"text": "制热"})
        _check(not _bad and "制热" in _msg and "制冷" in _msg,
               "控件值不符时区分“找到了但值不对”", results, _msg)
        _miss, _msg = _insp.check_widget(_an, {"id": "com.demo:id/nope"}, {"exists": True})
        _check(not _miss and "未找到控件" in _msg, "控件不存在时报找不到", results, _msg)
        _check(_insp.check_widget(_an, {"id": "com.demo:id/nope"}, {"exists": False})[0],
               "断言控件不存在（未命中即通过）", results)
        _check(not _insp.check_widget(_an, {"desc": "ok"}, {"exists": False})[0],
               "断言不存在但控件还在时报错", results)
        _check(not _insp.check_widget(_an, {"id": "com.demo:id/mode", "index": 2},
                                      {"exists": True})[0],
               "index 超出命中数目时报找不到", results)
        _check(not _insp.check_widget(_an, {"text": "确定", "index": 3},
                                      {"exists": False})[0],
               "断言不存在不看 index（同名控件还剩 2 个就不能算没了）", results)
        _check(_insp.check_widget(_an, {"id": "com.demo:id/power"}, {"checked": True})[0]
               and not _insp.check_widget(_an, {"id": "com.demo:id/power"},
                                          {"checked": False})[0],
               "checked 断言按 dump 属性判定", results)
        _check(_insp.check_widget(_an, {"id": "com.demo:id/power"}, {"enabled": False})[0]
               and not _insp.check_widget(_an, {"id": "com.demo:id/power"},
                                          {"enabled": True})[0],
               "enabled 断言按 dump 属性判定", results)
        _check(_insp.check_widget(_an, {"desc": "ok"}, {"contains": "o"})[0],
               "contains 同时看文本与描述", results)

        _wcase = new_case("控件断言用例", PROFILE, [
            {"action": "assert_widget", "selector": {"id": "com.demo:id/mode"},
             "expect": {"text": "制冷"}}])
        _check(not validate_case(_wcase), "assert_widget 用例校验通过", results,
               "; ".join(validate_case(_wcase)))
        for _label, _step in (
                ("空选择器", {"selector": {"index": 2}, "expect": {"exists": True}}),
                ("空期望", {"selector": {"id": "a"}, "expect": {}}),
                ("choice 类型错", {"selector": {"id": "a"},
                                 "expect": {"exists": "yes"}}),
                ("index 为 bool", {"selector": {"id": "a", "index": True},
                                   "expect": {"exists": True}})):
            _check(bool(validate_case(new_case("坏断言", PROFILE,
                                              [{"action": "assert_widget", **_step}]))),
                   f"非法控件断言被校验拦截（{_label}）", results)

        class _WidgetAdb(DummyAdb):
            def __init__(self, xml):
                self.xml = xml
                self.dumps = 0

            def uiautomator_dump(self, **_kw):
                self.dumps += 1
                return self.xml

        _wa = _WidgetAdb(_AX)
        _rep_w = Runner(_wa, cfg, dict(PROFILE), report_dir=td / "reports",
                        case_dir=td).run(_wcase, save_report=False)
        _check(_rep_w.failed == 0 and _wa.dumps == 1,
               "回放执行 assert_widget（dump 一次即判定）", results,
               f"failed={_rep_w.failed} dumps={_wa.dumps}")
        _cmd_w = _rep_w.steps[0]["command"] if _rep_w.steps else ""
        _check("assert_widget" in _cmd_w and "com.demo:id/mode" in _cmd_w,
               "回放命令日志写明断言目标", results, _cmd_w)
        _fail_case = new_case("失败断言", PROFILE, [
            {"action": "assert_widget", "selector": {"id": "com.demo:id/mode"},
             "expect": {"text": "制热"}}])
        _rep_f = Runner(_WidgetAdb(_AX), cfg, dict(PROFILE), report_dir=td / "reports",
                        case_dir=td).run(_fail_case, save_report=False)
        _check(_rep_f.failed == 1 and "制热" in str(_rep_f.steps[0].get("message") or ""),
               "控件值不符记 FAIL 并说明期望/实际", results,
               str(_rep_f.steps[0].get("message")))

    return all(r[0] for r in results), results
