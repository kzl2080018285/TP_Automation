#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ADB Case Recorder —— 独立命令行工具（不依赖皮卡皮卡）。

  python adbcase.py devices
  python adbcase.py -s <序列号> record --out cases/hvac.yaml
  python adbcase.py -s <序列号> record --out cases/hvac.yaml --screenshots
  python adbcase.py -s <序列号> replay cases/hvac.yaml --repeat 3
  python adbcase.py -s <序列号> replay cases/hvac.yaml --timeline
  python adbcase.py -s <序列号> replay cases/hvac.yaml --update-baseline
  python adbcase.py replay cases/hvac.yaml --dry-run
  python adbcase.py mirror                    # 实时投屏 + 鼠标控制（需 PySide6/av）
  python adbcase.py selftest
  python adbcase.py gui                     # 需要 PySide6（见 adbcase_gui.py 说明）

--screenshots 打开后，每个动作后自动抓一张图，并按 --baseline-policy
生成 assert_image 基线断言；回放时按阈值比对，UI 改版用 --update-baseline 一键刷新。
用例里 at_ms 是相对录制时刻，--timeline 按它确定性回放；
until 让步骤等条件成立而非死等，超时可自动重做动作。

录下来的用例是普通 YAML/JSON，坐标 0~1 归一化、支持 ${参数}、
include 复用、repeat 循环、assert_* 断言，随便用编辑器改。

录制时建议同时开投屏（python adbcase.py mirror）或车机实屏，方便确认自己点在哪；
投屏窗口里鼠标点击/拖拽可直接操作设备，还能把鼠标操作录成用例。
录制只产出动作骨架，断言与参数化请在用例里手工补齐。
本机没插设备也能用 replay --dry-run 预演整份用例。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import __version__  # noqa: E402
from core.adb import Adb, AdbError  # noqa: E402
from core.cases import (CaseError, convert, detect_format, expand_steps,  # noqa: E402
                        load_case, new_case, save_case, validate_case)
from core.config import load_config, write_default_config  # noqa: E402
from core.draft import DraftWriter  # noqa: E402
from core.image_baseline import (DEFAULT_IGNORE_STATUS_BAR, HAS_PILLOW,  # noqa: E402
                                 ShotCapture, inject_baselines)
from core.listener import GeteventModeError, iter_live  # noqa: E402
from core.probe import probe, resolve_nodes  # noqa: E402
from core.runner import Runner  # noqa: E402
from core.selftest import run_selftest  # noqa: E402
from core.translate import Translator  # noqa: E402


class _OfflineAdb:
    """dry-run 不需要真实设备，占位避免误连 adb。"""

    def __getattr__(self, _name):
        def _noop(*_a, **_kw):
            return ""
        return _noop


# 草稿写入统一由 core.draft.DraftWriter 提供（CLI 与 GUI 共用）


def _out_setup():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _common(p: argparse.ArgumentParser, *, suppress: bool) -> None:
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("-s", "--serial", default=d, help="设备序列号；多设备时必填")
    p.add_argument("--adb", default=d, help="adb 可执行文件路径（也可在配置文件/ADB_PATH 中设置）")
    p.add_argument("--config", default=d, help=f"配置文件路径（默认 {ROOT / 'adb_case_config.json'}）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adbcase",
        description="录制 Android/车机操作 -> 生成可编辑测试用例 -> 回放",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"adbcase {__version__}")
    _common(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("devices", help="列出在线设备", parents=[])
    _common(sp, suppress=True)

    sp = sub.add_parser("probe", help="探测设备屏幕与输入节点能力（不硬编码 eventN）")
    _common(sp, suppress=True)
    sp.add_argument("--json", dest="json_out", help="把设备档案另存为 JSON")

    sp = sub.add_parser("record", help="监听设备操作并生成用例")
    _common(sp, suppress=True)
    sp.add_argument("--out", "-o", required=True, help="用例输出路径 (.yaml / .json)")
    sp.add_argument("--name", help="用例名，默认取文件名")
    sp.add_argument("--format", dest="fmt", choices=["yaml", "json"],
                    help="输出格式，默认按扩展名，扩展名非 yaml/json 时取配置 default_format")
    sp.add_argument("--tag", action="append", default=[], help="用例标签，可重复")
    sp.add_argument("--param", action="append", default=[], metavar="k=v",
                    help="写入 params，可重复 --param 或逗号分隔多个 k=v")
    sp.add_argument("--nodes", nargs="*", help="手动指定 /dev/input/eventN（默认自动探测）")
    sp.add_argument("--event-mode", choices=["auto", "label", "hex"], default="auto",
                    help="getevent 输出模式；老设备不支持 -l 时用 hex")
    sp.add_argument("--keep-raw", action="store_true", help="用例里附原始事件流，便于排查")
    sp.add_argument("--screenshot", action="store_true", help="录制开始时截一张图")

    # —— 逐步截图 + 图片基线断言开关 ——
    shots = sp.add_argument_group("逐步截图与图片基线")
    shots.add_argument("--screenshots", action="store_true",
                       help="打开：每个动作后自动截图，并按策略生成 assert_image 基线断言")
    shots.add_argument("--no-screenshots", action="store_true",
                       help="关闭：覆盖配置文件里的 screenshots=true")
    shots.add_argument("--baseline-policy", choices=["all", "last", "none"],
                       help="all=每步断言(默认) / last=只校验最终界面 / none=只存图不断言")
    shots.add_argument("--shot-delay", type=int, help="动作后延迟多少毫秒抓图（默认 250）")
    shots.add_argument("--shot-mode", choices=["async", "sync"],
                       help="async=不阻塞事件流(默认) / sync=逐步精确但稍慢")
    shots.add_argument("--shot-threshold", type=float, help="允许差异像素占比 0~1（默认 0.02）")
    shots.add_argument("--shot-tolerance", type=int, help="单像素灰度差阈值（默认 24）")
    shots.add_argument("--include-status-bar", action="store_true",
                       help="不忽略顶部状态栏（默认忽略，避免时间/电量导致误报）")
    shots.add_argument("--shot-dir", help="截图目录名（默认 <用例名>_shots，相对用例文件）")

    drafts = sp.add_argument_group("录制草稿（崩溃安全）")
    drafts.add_argument("--draft-interval", type=float,
                        help="每多少秒原子重写一次草稿用例（默认 5s）")
    drafts.add_argument("--no-draft", action="store_true", help="关闭周期性草稿写入")

    sp = sub.add_parser("replay", help="回放用例")
    _common(sp, suppress=True)
    sp.add_argument("case", help="用例文件 (.yaml / .json)")
    sp.add_argument("--repeat", type=int, default=1, help="整份用例重复次数")
    sp.add_argument("--dry-run", action="store_true", help="只打印将执行的 ADB 命令，不连设备")
    sp.add_argument("--speed", type=float, help="时间缩放，2=两倍速，0.5=半速")
    sp.add_argument("--param", action="append", default=[], metavar="k=v",
                    help="覆盖 params，可重复 --param 或逗号分隔多个 k=v")
    sp.add_argument("--from-step", type=int, default=1, help="从第 N 步开始（按展开后序号）")
    sp.add_argument("--to-step", type=int, help="到第 N 步结束")
    sp.add_argument("--on-error", choices=["stop", "continue"], help="失败后继续还是中止")
    sp.add_argument("--timeline", action="store_true",
                    help="按用例里的 at_ms 相对时间轴确定性回放（忽略 wait 的额外等待；"
                         "repeat 展开的步骤共享 at_ms，精确时序请手工调）")
    sp.add_argument("--report-dir", help="报告与截图输出目录")
    sp.add_argument("--no-report", action="store_true", help="不写 JSON 报告")
    sp.add_argument("--update-baseline", action="store_true",
                    help="UI 改版后刷新基线：遇到 assert_image 时用当前屏幕覆盖基线图，不做比对")
    sp.add_argument("--force", action="store_true", help="用例校验有问题也继续")

    sp = sub.add_parser("validate", help="校验用例结构")
    sp.add_argument("cases", nargs="+")

    sp = sub.add_parser("convert", help="YAML <-> JSON 互转")
    sp.add_argument("case")
    sp.add_argument("--to", choices=["yaml", "json"], required=True)

    sp = sub.add_parser("selftest", help="不连设备的全链路自检")

    sp = sub.add_parser("gui", help="启动 Qt 界面（需要 PySide6）")

    sp = sub.add_parser("mirror", help="实时投屏 + 鼠标点击/滑动控制（需要 PySide6 与 av）")
    _common(sp, suppress=True)
    sp.add_argument("--max-size", type=int, help="视频长边上限，0=原始分辨率")
    sp.add_argument("--bit-rate", type=int, help="H.264 码率 bps")
    sp.add_argument("--max-fps", type=int, help="帧率上限")
    sp.add_argument("--no-audio", action="store_true", help="不转发设备声音到电脑（默认转发）")
    sp.add_argument("--external", action="store_true",
                    help="直接启动官方 scrcpy 高性能客户端（部署在 E:\\tools\\scrcpy）")

    sp = sub.add_parser("init-config", help="生成默认配置文件")
    sp.add_argument("--force", action="store_true", help="覆盖已有配置")
    return p


def _params(pairs: list[str]) -> dict:
    out = {}
    for item in pairs:
        for part in item.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError(f"--param 需要 k=v 格式: {part}")
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _make_adb(args, cfg) -> Adb:
    return Adb(binary=getattr(args, "adb", None), serial=getattr(args, "serial", None), cfg=cfg)


def cmd_devices(args, cfg) -> int:
    adb = _make_adb(args, cfg)
    try:
        items = adb.devices()
    except AdbError as exc:
        print(f"[FAIL] {exc}")
        return 1
    if not items:
        print("没有在线设备（检查 USB 调试授权 / adb connect）。")
        return 0
    print(f"{'序列号':<28} {'状态':<10} 信息")
    for d in items:
        print(f"{d['serial']:<28} {d['state']:<10} "
              f"{' '.join(f'{k}={v}' for k, v in d.items() if k not in ('serial', 'state'))}")
    return 0


def cmd_probe(args, cfg) -> int:
    info = probe(_make_adb(args, cfg))
    print(f"设备      : {info['serial']}  {info['model']}  Android {info['android_release']} (SDK {info['sdk']})")
    print(f"屏幕      : {info['size'][0]}x{info['size'][1]}  density={info['density']}  rotation={info['rotation']}")
    print(f"触摸坐标  : {info['touch_abs']}  来源={info['abs_source']}  "
          f"与屏幕比={info.get('coord_ratio')}")
    if info.get("coord_warning"):
        print("[WARN] 触摸坐标与屏幕的横纵比例不一致，可能面板坐标系与屏幕不同；"
              "建议先手工点一下核对，必要时在配置里设 swap_xy=true 或用 --nodes 指定主触摸设备。")
    print(f"输入设备  : 共 {len(info['inputs'])} 个")
    for d in info["inputs"]:
        kind = "触摸" if d["is_touch"] else ("按键" if d["keys"] else "其他")
        print(f"  {d['node']:<22} {kind:<4} {d['name']:<28} "
              f"abs=({d['abs_x_max']},{d['abs_y_max']}) keys={','.join(d['keys'][:6])}")
    print(f"监听节点  : {', '.join(info.get('record_nodes') or []) or '（无，请手工指定）'}")
    if info["info_error"]:
        print(f"[WARN] {info['info_error']}")
    for err in info.get("errors") or []:
        print(f"[WARN] 探测受限: {err}")
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"已保存: {p}")
    return 0


def _stream(adb: Adb, nodes: list[str], mode: str):
    if mode == "hex":
        yield from iter_live(adb, nodes, hex_mode=True)
        return
    try:
        yield from iter_live(adb, nodes, hex_mode=False)
    except GeteventModeError as exc:
        if mode != "auto":
            raise
        print(f"[WARN] -l 模式不可用，自动切换 hex 模式。原因: {exc}")
        yield from iter_live(adb, nodes, hex_mode=True)


def cmd_record(args, cfg) -> int:
    adb = _make_adb(args, cfg)
    print("[探测] 读取屏幕尺寸与输入节点能力 ...")
    profile = probe(adb)
    nodes = args.nodes or cfg.get("record_nodes") or resolve_nodes(adb, profile)
    if not nodes:
        print("[FAIL] 没有找到可监听的输入节点；可用 --nodes /dev/input/eventN 手动指定。")
        return 1
    print(f"[探测] {profile['serial']} 屏幕 {profile['size'][0]}x{profile['size'][1]}  "
          f"触摸坐标上限 {profile['touch_abs']} (比例 {profile.get('coord_ratio')})")
    if profile.get("coord_warning"):
        print("[WARN] 触摸坐标与屏幕横纵比例不一致，回放可能偏移；可在配置里设 swap_xy 或用 --nodes 校准。")
    print(f"[监听] 节点: {', '.join(nodes)}  （只取 1 个主触摸设备，避免重复记录同一次点击）")
    if args.screenshot:
        p = ROOT / "reports" / "record_start.png"
        if adb.screencap(p):
            print(f"[截图] {p}")

    shots_on = bool(cfg.get("screenshots")) or args.screenshots
    if args.no_screenshots:
        shots_on = False
    policy = args.baseline_policy or cfg.get("baseline_policy", "all")
    delay = args.shot_delay if args.shot_delay is not None else int(cfg.get("shot_delay_ms", 250))
    mode = args.shot_mode or cfg.get("shot_mode", "async")
    threshold = (args.shot_threshold if args.shot_threshold is not None
                 else float(cfg.get("baseline_threshold", 0.02)))
    tolerance = (args.shot_tolerance if args.shot_tolerance is not None
                 else int(cfg.get("baseline_pixel_tolerance", 24)))
    ignore: list[list] = []
    if cfg.get("ignore_status_bar", True) and not args.include_status_bar:
        ignore.append(list(DEFAULT_IGNORE_STATUS_BAR))
    ignore += [list(r) for r in (cfg.get("baseline_ignore_regions") or [])]
    case_path = Path(args.out).resolve()
    shots_dir_name = args.shot_dir or f"{case_path.stem}{cfg.get('shot_dir_suffix', '_shots')}"

    capture = None
    if shots_on:
        capture = ShotCapture(adb, case_path.parent, shots_dir_name,
                              delay_ms=delay, mode=mode)
        print(f"[截图] 逐步截图已开启 -> {case_path.parent / shots_dir_name}  "
              f"mode={mode} delay={delay}ms 断言策略={policy}")
        if not HAS_PILLOW:
            print("[WARN] 未安装 Pillow：截图不受影响，但回放无法比对，请 pip install Pillow")
    print("[监听] 现在请在设备上操作，按 Ctrl+C 结束并生成用例 ...\n")

    tr = Translator(cfg, profile,
                    on_action=(lambda ts, action: capture.submit(ts)) if capture else None)
    name = args.name or case_path.stem
    fmt = args.fmt or (cfg.get("default_format")
                       if case_path.suffix.lower() not in (".yaml", ".yml", ".json")
                       else detect_format(case_path))
    draft = None
    if cfg.get("draft_enabled", True) and not args.no_draft:
        interval = (args.draft_interval if args.draft_interval is not None
                    else float(cfg.get("draft_interval_s", 5)))
        if interval > 0:
            draft = DraftWriter(tr, profile, case_path, fmt, interval, name,
                                args.tag, _params(args.param), log=print)
            draft.start()
    count = 0
    try:
        for ev in _stream(adb, nodes, args.event_mode):
            tr.feed([ev])
            count += 1
            if count % 50 == 0:
                print(f"\r  已接收 {count} 条原始事件 ...", end="", flush=True)
    except (KeyboardInterrupt, GeteventModeError) as exc:
        if isinstance(exc, GeteventModeError):
            print(f"\n[WARN] {exc}")
    except AdbError as exc:
        print(f"\n[FAIL] {exc}")
        if draft:
            draft.stop(remove=False)
            print(f"[草稿] 已保留中断前的用例: {draft.path}")
        return 1
    print(f"\n[结束] 共 {count} 条原始事件")
    shots: dict[int, str] = {}
    if capture:
        if capture.pending:
            print(f"[截图] 等待剩余 {capture.pending} 张截图写入 ...")
        shots = capture.close()
        print(f"[截图] 成功 {capture.stat.saved}/{capture.stat.total} 张，失败 {capture.stat.failed} 张"
              + ("；失败步骤不生成断言" if capture.stat.failed else ""))

    steps = tr.finish()
    steps = inject_baselines(steps, shots, policy=policy, threshold=threshold,
                             pixel_tolerance=tolerance, ignore_regions=ignore,
                             wait_before_ms=delay)
    if not steps:
        print("[WARN] 没有识别到任何操作，未生成用例。")
        if draft:
            draft.stop(remove=True)
        return 1
    case = new_case(name, profile, steps, tags=args.tag, params=_params(args.param),
                    raw_events=tr.raw if cfg.get("keep_raw_events") or args.keep_raw else None)
    path = save_case(case, args.out, fmt)
    print(f"[用例] {path}  （共 {len(steps)} 步）")
    for i, s in enumerate(steps[:12], 1):
        print(f"  {i:>3}. {s['action']:<13} {s.get('desc', '')}")
    if len(steps) > 12:
        print(f"  ... 其余 {len(steps) - 12} 步")
    n_img = sum(1 for s in steps if s["action"] == "assert_image")
    if n_img:
        print(f"[基线] 生成 {n_img} 条 assert_image（阈值 {threshold:.0%}、像素容差 {tolerance}、"
              f"忽略区域 {len(ignore)} 个）")
    print("\n可直接用编辑器改这个用例：坐标是 0~1 归一化值，"
          "可加 ${参数}、include、repeat、assert_* 断言与 desc。")
    print("录制只生成动作骨架，断言的 region / threshold 都可在用例里逐条调整；"
          f"无设备时可先 python adbcase.py replay {path} --dry-run 预演。")
    if n_img:
        print(f"UI 改版后刷新基线: python adbcase.py -s {profile['serial']} "
              f"replay {path} --update-baseline")
    print(f"回放: python adbcase.py -s {profile['serial']} replay {path} --repeat 3")
    if draft:
        draft.stop(remove=True)
    return 0


def cmd_replay(args, cfg) -> int:
    path = Path(args.case)
    try:
        case = load_case(path)
    except (CaseError, OSError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    errs = validate_case(case)
    if errs:
        print(f"[校验] 发现 {len(errs)} 个问题:")
        for e in errs[:20]:
            print(f"  - {e}")
        if not args.force:
            print("用 --force 可强制回放。")
            return 1

    timeline = args.timeline
    if timeline and not any(isinstance(s.get("at_ms"), int)
                            for s in case.get("steps", []) if isinstance(s, dict)):
        print("[WARN] 用例没有 at_ms 相对时间轴，--timeline 已忽略（请用新版本重新录制）")
        timeline = False

    dry = args.dry_run
    if dry and args.update_baseline:
        print("[FAIL] --update-baseline 需要真实设备抓图，不能与 --dry-run 同时使用。")
        return 1
    if args.update_baseline:
        n_img = sum(1 for s in case.get("steps", []) if s.get("action") == "assert_image")
        print(f"[基线] 将用当前界面覆盖 {n_img} 条 assert_image 的基线图（不做比对）")
    if dry:
        adb = _OfflineAdb()
        dev = case.get("device") or {}
        profile = {"serial": "DRY-RUN", "model": dev.get("model", ""),
                   "size": dev.get("size") or [1080, 1920],
                   "rotation": dev.get("rotation", 0),
                   "touch_abs": dev.get("touch_abs") or [1079, 1919]}
    else:
        adb = _make_adb(args, cfg)
        try:
            profile = probe(adb)
        except AdbError as exc:
            print(f"[FAIL] {exc}")
            return 1

    runner = Runner(adb, cfg, profile, dry_run=dry, speed=args.speed,
                    on_error=args.on_error,
                    report_dir=args.report_dir or ROOT / "reports",
                    case_dir=path.parent,
                    update_baseline=args.update_baseline,
                    timeline=timeline)
    total_failed = 0
    for n in range(1, max(1, args.repeat) + 1):
        if args.repeat > 1:
            print(f"\n===== 第 {n}/{args.repeat} 轮 =====")
        rep = runner.run(case, params=_params(args.param),
                         from_step=args.from_step, to_step=args.to_step,
                         save_report=not args.no_report)
        total_failed += rep.failed
        if rep.failed and runner.on_error == "stop":
            break
    return 1 if total_failed else 0


def cmd_validate(args, cfg) -> int:
    bad = 0
    for f in args.cases:
        try:
            case = load_case(f)
            errs = validate_case(case)
            n = len(expand_steps(case, Path(f).parent)) if not errs else 0
        except (CaseError, OSError, ValueError) as exc:
            errs, n = [str(exc)], 0
        if errs:
            bad += 1
            print(f"[FAIL] {f}")
            for e in errs:
                print(f"       - {e}")
        else:
            print(f"[PASS] {f}  用例名={case.get('name')}  展开后 {n} 步")
    return 1 if bad else 0


def cmd_gui(args, cfg) -> int:
    gui = ROOT / "adbcase_gui.py"
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("当前 Python 没有 PySide6，无法启动 Qt 界面。请用装了 PySide6 的解释器执行：")
        print(f"  <python> {gui}")
        print("本机可用: D:/Python/python.exe adbcase_gui.py；或 pip install PySide6")
        return 2
    import runpy
    sys.argv = [str(gui)]
    runpy.run_path(str(gui), run_name="__main__")
    return 0


def cmd_mirror(args, cfg) -> int:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("投屏窗口需要 PySide6：pip install PySide6 av numpy")
        return 2
    from core.mirror import has_decoder
    if not has_decoder():
        print("投屏解码需要 PyAV：pip install av numpy")
        return 2
    if getattr(args, "max_size", None) is not None:
        cfg["mirror_max_size"] = args.max_size
    if getattr(args, "bit_rate", None) is not None:
        cfg["mirror_bit_rate"] = args.bit_rate
    if getattr(args, "max_fps", None) is not None:
        cfg["mirror_max_fps"] = args.max_fps
    if getattr(args, "adb", None):
        cfg["adb_path"] = args.adb
    if getattr(args, "no_audio", False):
        cfg["mirror_audio"] = False
    if getattr(args, "external", False):
        from core.mirror import (find_external_scrcpy, download_external_scrcpy,
                                 EXTERNAL_TOOLS_DIR, EXTERNAL_ZIP_URL)
        exe = find_external_scrcpy(cfg)
        if exe is None:
            print(f"未找到官方 scrcpy，自动下载便携版到 {EXTERNAL_TOOLS_DIR}\\scrcpy …\n"
                  f"  来源: {EXTERNAL_ZIP_URL}")
            exe = download_external_scrcpy(log=print)
        from core.mirror import launch_external_scrcpy
        proc = launch_external_scrcpy(
            exe, args.serial or "", max_size=cfg.get("mirror_max_size", 1280) or 0,
            bit_rate=cfg.get("mirror_bit_rate", 8_000_000),
            max_fps=cfg.get("mirror_max_fps", 30) or 0,
            audio=bool(cfg.get("mirror_audio", True)),
            adb_path=cfg.get("adb_path", ""))
        print(f"已启动官方 scrcpy（pid={proc.pid}），关闭窗口即结束")
        return proc.wait()
    import runpy
    argv = [str(ROOT / "adbcase_gui.py"), "--mirror"]
    if getattr(args, "serial", None):
        argv += ["--serial", args.serial]
    if getattr(args, "no_audio", False):
        argv += ["--no-audio"]
    sys.argv = argv
    runpy.run_path(str(ROOT / "adbcase_gui.py"), run_name="__main__")
    return 0


def cmd_selftest(args, cfg) -> int:
    ok, results = run_selftest()
    for passed, label, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not passed else ""))
    print(f"\n自检结果: {sum(1 for r in results if r[0])}/{len(results)} 通过  "
          f"{'全部通过' if ok else '存在失败项'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _out_setup()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(getattr(args, "config", None))
    except SystemExit as exc:
        print(f"[FAIL] {exc}")
        return 1

    handlers = {
        "devices": cmd_devices, "probe": cmd_probe, "record": cmd_record,
        "replay": cmd_replay, "validate": cmd_validate, "selftest": cmd_selftest,
    }
    try:
        if args.cmd == "init-config":
            p = write_default_config(force=args.force)
            print(f"配置文件: {p}")
            return 0
        if args.cmd == "convert":
            print(f"已生成: {convert(args.case, args.to)}")
            return 0
        if args.cmd == "gui":
            return cmd_gui(args, cfg)
        if args.cmd == "mirror":
            return cmd_mirror(args, cfg)
        return handlers[args.cmd](args, cfg)
    except (AdbError, CaseError, OSError, ValueError) as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
