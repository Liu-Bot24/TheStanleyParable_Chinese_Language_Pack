from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SOURCE = DATA / "source"
BATCHES = DATA / "batches"
RAW = DATA / "gemini_raw"
DIST = ROOT / "dist"

BLOCK_SIZE = 8192
HEADER_SIZE = 24
ENTRY_SIZE = 12

LANG_FILES = [
    "basemodui_english.txt",
    "gameui_english.txt",
    "valve_english.txt",
    "chat_english.txt",
]

SCHEME_FILES = [
    "clientscheme.res",
    "sourcescheme.res",
    "basemodui_scheme.res",
]

UI_RESOURCE_FILES = [
    "ui/basemodui/mainmenu_tsp.res",
    "ui/basemodui/options.res",
    "ui/basemodui/extrasdialog.res",
]

CJK_FONT_NAME = "Noto Sans SC"

EXTRA_LANG_TOKENS = {
    "basemodui_english.txt": {
        "TSP_Menu_Credits": "制作名单",
        "TSP_Extras_Achievement": "成就",
        "TSP_Extras_Saves": "存档",
    },
}

FONT_FILES = [
    {
        "name": "NotoSansSC-VF.ttf",
        "url": "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
    },
]

FONT_LICENSE_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/OFL.txt"


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if b"\x00" in raw[:128]:
        return raw.decode("utf-16le")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin1")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def crc_token(token: str) -> int:
    return zlib.crc32(token.lower().encode("latin1")) & 0xFFFFFFFF


def unescape_value(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def escape_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace('"', '\\"')


def game_mod_dir(game_dir: Path) -> Path:
    direct = game_dir / "thestanleyparable"
    if (direct / "gameinfo.txt").exists():
        return direct
    if (game_dir / "gameinfo.txt").exists():
        return game_dir
    raise FileNotFoundError(f"找不到 thestanleyparable\\gameinfo.txt: {game_dir}")


def parse_dat(path: Path) -> list[dict]:
    blob = path.read_bytes()
    magic, version, block_count, block_size, count, data_offset = struct.unpack_from("<4sIIIII", blob, 0)
    if magic != b"VCCD" or version != 1 or block_size != BLOCK_SIZE:
        raise ValueError(f"不支持的 Source caption dat: {path}")
    rows = []
    for i in range(count):
        crc, block, rel, length = struct.unpack_from("<IIHH", blob, HEADER_SIZE + i * ENTRY_SIZE)
        start = data_offset + block * BLOCK_SIZE + rel
        raw = blob[start:start + length]
        if raw.endswith(b"\x00\x00"):
            raw = raw[:-2]
        rows.append({"crc": crc, "block": block, "offset": rel, "length": length, "text": raw.decode("utf-16le")})
    return rows


def compile_dat(tokens: dict[str, str]) -> bytes:
    items = sorted(tokens.items(), key=lambda kv: kv[0].lower())
    padding = 512 - ((HEADER_SIZE + len(items) * ENTRY_SIZE) % 512)
    data_offset = HEADER_SIZE + len(items) * ENTRY_SIZE + padding

    header = bytearray()
    header += b"VCCD"
    header += struct.pack("<I", 1)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", BLOCK_SIZE)
    header += struct.pack("<I", len(items))
    header += struct.pack("<I", data_offset)

    directory = bytearray()
    blocks: list[bytes] = []
    current = bytearray()
    block_index = 0
    for token, text in items:
        encoded = text.encode("utf-16le") + b"\x00\x00"
        if len(encoded) >= BLOCK_SIZE:
            raise ValueError(f"字幕过长，无法放入单个 block: {token}")
        if len(current) + len(encoded) >= BLOCK_SIZE:
            blocks.append(bytes(current).ljust(BLOCK_SIZE, b"\x00"))
            current = bytearray()
            block_index += 1
        rel = len(current)
        current += encoded
        directory += struct.pack("<IIHH", crc_token(token), block_index, rel, len(encoded))
    if current or not blocks:
        blocks.append(bytes(current).ljust(BLOCK_SIZE, b"\x00"))
    header[8:12] = struct.pack("<I", len(blocks))
    return bytes(header) + bytes(directory) + (b"\x00" * padding) + b"".join(blocks)


def parse_lang(path: Path) -> list[dict]:
    text = read_text(path)
    rows = []
    pattern = re.compile(r'^\s*"([^"]+)"\s+"((?:\\.|[^"])*)"', re.MULTILINE)
    for match in pattern.finditer(text):
        key = match.group(1)
        if key.lower() == "language":
            continue
        rows.append({
            "key": key,
            "source": unescape_value(match.group(2)),
            "line": text.count("\n", 0, match.start()) + 1,
        })
    return rows


def scene_tokens(mod_dir: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for path in (mod_dir / "scenes").rglob("*.vcd"):
        seen: set[str] = set()
        text = read_text(path)
        rel = path.relative_to(mod_dir).as_posix()
        for match in re.finditer(r'(?:event\s+speak|param)\s+"([^"]+)"', text):
            token = match.group(1)
            if token not in seen:
                result[token].append(rel)
                seen.add(token)
    return result


def sound_tokens(mod_dir: Path) -> dict[int, list[str]]:
    result: dict[int, list[str]] = defaultdict(list)
    for path in (mod_dir / "scripts").rglob("*.txt"):
        text = read_text(path)
        for match in re.finditer(r'(?m)^"([^"]+)"\s*\{', text):
            token = match.group(1)
            if not token or any(ord(ch) > 127 for ch in token):
                continue
            crc = crc_token(token)
            if token not in result[crc]:
                result[crc].append(token)
    return result


def source_caption_tokens(mod_dir: Path, dat_name: str, txt_name: str | None = None) -> dict[str, str]:
    resource = mod_dir / "resource"
    entries = parse_dat(resource / dat_name)
    if txt_name:
        by_crc = {crc_token(row["key"]): row["key"] for row in parse_lang(resource / txt_name)}
        return {by_crc[row["crc"]]: row["text"] for row in entries if row["crc"] in by_crc}

    scene_by_token = scene_tokens(mod_dir)
    by_crc = {crc_token(token): token for token in scene_by_token}
    sounds = sound_tokens(mod_dir)
    out: dict[str, str] = {}
    missing = []
    for row in entries:
        token = by_crc.get(row["crc"])
        if token is None:
            candidates = sounds.get(row["crc"], [])
            token = candidates[0] if len(candidates) == 1 else None
        if token is None:
            missing.append(row["crc"])
        else:
            out[token] = row["text"]
    if missing:
        raise RuntimeError(f"{dat_name} 有 {len(missing)} 个 CRC 未匹配")
    return out


def command_extract(args: argparse.Namespace) -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    SOURCE.mkdir(parents=True, exist_ok=True)
    mod_dir = game_mod_dir(Path(args.game_dir).resolve())
    resource = mod_dir / "resource"

    scene_by_token = scene_tokens(mod_dir)
    units: list[dict] = []

    subtitles = source_caption_tokens(mod_dir, "subtitles_english.dat")
    for token, source in sorted(subtitles.items(), key=lambda kv: kv[0].lower()):
        units.append({
            "id": f"caption:subtitles:{token}",
            "kind": "narration",
            "file": "subtitles_english.dat",
            "target_file": "subtitles_schinese.txt",
            "token": token,
            "scene_files": scene_by_token.get(token, []),
            "source": source,
            "target": "",
        })

    closecaption = source_caption_tokens(mod_dir, "closecaption_english.dat", "closecaption_english.txt")
    for token, source in sorted(closecaption.items(), key=lambda kv: kv[0].lower()):
        units.append({
            "id": f"caption:closecaption:{token}",
            "kind": "sfx",
            "file": "closecaption_english.dat",
            "target_file": "closecaption_schinese.txt",
            "token": token,
            "source": source,
            "target": "",
        })

    for name in LANG_FILES:
        path = resource / name
        if not path.exists():
            continue
        shutil.copy2(path, SOURCE / name)
        for row in parse_lang(path):
            units.append({
                "id": f"ui:{name}:{row['line']}:{row['key']}",
                "kind": "ui",
                "file": name,
                "target_file": name.replace("_english", "_schinese"),
                "key": row["key"],
                "line": row["line"],
                "source": row["source"],
                "target": "",
            })

    for name in SCHEME_FILES:
        path = resource / name
        if path.exists():
            shutil.copy2(path, SOURCE / name)

    for name in UI_RESOURCE_FILES:
        path = resource / name
        if path.exists():
            target = SOURCE / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    write_jsonl(DATA / "units.jsonl", units)
    if not (DATA / "translations.jsonl").exists():
        write_jsonl(DATA / "translations.jsonl", units)
    (DATA / "inventory.json").write_text(json.dumps({
        "game_dir": str(mod_dir.parent),
        "mod_dir": str(mod_dir),
        "units": len(units),
        "narration": len(subtitles),
        "sfx": len(closecaption),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # Binary round-trip verification: the reconstructed English captions must be exact.
    compiled_subtitles = compile_dat(subtitles)
    compiled_close = compile_dat(closecaption)
    if compiled_subtitles != (resource / "subtitles_english.dat").read_bytes():
        raise RuntimeError("subtitles_english.dat 往返编译不一致")
    if compiled_close != (resource / "closecaption_english.dat").read_bytes():
        raise RuntimeError("closecaption_english.dat 往返编译不一致")

    print(json.dumps({"extracted": len(units), "narration": len(subtitles), "sfx": len(closecaption)}, ensure_ascii=False))
    return 0


def route_group(unit: dict) -> tuple[str, str, str]:
    if unit["kind"] == "ui":
        if unit["file"] == "basemodui_english.txt":
            return "ui_main_menu_options", "medium", "主菜单、暂停菜单、存档/读档、音视频和控制选项。"
        return "ui_engine_common", "medium", "Source 引擎通用 UI、聊天、保存提示和系统文本。"
    if unit["kind"] == "sfx":
        return "closed_caption_sfx", "loose", "非对白音效字幕，保留 <sfx>/<len>/<norepeat> 等标签。"

    token = unit["token"].split(".", 1)[1].lower()
    token = re.sub(r"[_-]\d+$", "", token)
    route_patterns = [
        ("narration_zending_happiness_loop", "tight", "幸福房/Zending 路线，旁白恳求史丹利留在美景中并抗拒重启。", r"^(1[a-k]|2[a-e]|3[a-d]|4[a-c]|5a|6_|zen)"),
        ("narration_core_left_route", "tight", "左门主线：开场、会议室、老板办公室、精神控制设施、自由/倒计时结局。", r"^(intro|meeting_room|two_doors$|staircase|boss|underground|monitor|freedom|countdown|death_machine|escape_hall|maintenance)"),
        ("narration_right_door_apartment", "tight", "右门路线：员工休息室、货梯、电话、公寓、选择批判和错误结局。", r"^(two_doors_right|lounge|cargolift|inc|incright|incleft|incend|phone|apartment|wife|choicepsa)"),
        ("narration_confusion_adventure_line", "tight", "困惑结局和冒险线，包括路线表、重启和旁白迷失。", r"^(con|controls)"),
        ("narration_gags_secrets_achievements", "tight", "扫帚间、430 成就、严肃房间、窗外/越界、办公室变体等隐藏笑话。", r"^(417|ach|broomcloset|lockedoffice|windowgag|seriousroom|zaxis|idle|office)"),
        ("narration_games_playtest_baby", "tight", "游戏/试玩路线：Minecraft/Portal 式转场、婴儿游戏、红蓝门服从测试和反馈讽刺。", r"^(playtest|playtestmc|playtestbaby|playtestp|playtestfeedback|playtestfinale|baby|redblue)"),
        ("narration_museum_dream_female", "tight", "博物馆/梦境/女性旁白相关内容。", r"^(femnarr|dream)"),
    ]
    for group, tightness, description, pattern in route_patterns:
        if re.search(pattern, token):
            return group, tightness, description
    return "narration_misc", "medium", "未归入主要路线的旁白，按旁白通用语气处理。"


def command_group(args: argparse.Namespace) -> int:
    units = read_jsonl(DATA / "units.jsonl")
    BATCHES.mkdir(parents=True, exist_ok=True)
    for old in BATCHES.glob("*.json"):
        old.unlink()

    grouped: dict[str, dict] = {}
    for index, unit in enumerate(units):
        group, tightness, description = route_group(unit)
        grouped.setdefault(group, {"group": group, "tightness": tightness, "description": description, "rows": []})
        row = dict(unit)
        row["sequence"] = index
        grouped[group]["rows"].append(row)

    batches = []
    for group in sorted(grouped):
        rows = grouped[group]["rows"]
        size = 70 if grouped[group]["tightness"] == "tight" else 90
        for part, start in enumerate(range(0, len(rows), size), start=1):
            chunk = rows[start:start + size]
            package_id = group if len(rows) <= size else f"{group}_part{part:02d}"
            batch = {
                "package_id": package_id,
                "group": group,
                "tightness": grouped[group]["tightness"],
                "description": grouped[group]["description"],
                "rows": chunk,
            }
            (BATCHES / f"{package_id}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
            batches.append({"package_id": package_id, "group": group, "rows": len(chunk), "tightness": batch["tightness"]})

    report = {
        "principle": "按同一剧情路线/同类 UI/同类音效划分；剧情旁白优先保持上下文连续。",
        "groups": [
            {"group": g, "rows": len(v["rows"]), "tightness": v["tightness"], "description": v["description"]}
            for g, v in sorted(grouped.items())
        ],
        "batches": batches,
    }
    (DATA / "grouping.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"groups": len(grouped), "batches": len(batches)}, ensure_ascii=False))
    return 0


def background_text() -> str:
    return """《The Stanley Parable》是一款第一人称元叙事游戏。玩家扮演办公室职员 Stanley/史丹利，旁白以第三人称讲述“史丹利会怎么做”，但玩家经常违抗旁白，从而进入不同结局。核心主题是选择、自由意志、玩家控制权、叙事脚本、游戏规则和第四面墙。

翻译目标：中文要自然、准确、有文学感。旁白语气偏英式、戏剧化、一本正经地荒谬，常在优雅叙述、讽刺、恼火、哀求、崩溃之间切换。不要直译成谷歌翻译腔。

术语：Stanley=史丹利；The Stanley Parable=史丹利的寓言；Narrator=旁白；Mind Control Facility=精神控制设施；meeting room=会议室；boss's office=老板办公室；Broom Closet=扫帚间；Adventure Line=冒险线；Serious Room=严肃房间。

技术约束：保留所有 Source 标签和变量，如 <sfx>、<len:5>、<norepeat:5>、<clr:255,0,0>、%s1、$W、$M、$D。音效字幕保持 [描述] 风格，只翻译声音描述。UI 尽量短。
"""


def parse_gemini_json(text: str) -> dict:
    clean = "\n".join(line for line in text.splitlines() if not line.startswith("Warning:") and not line.startswith("Ripgrep is not available.")).strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean)
    start, end = clean.find("{"), clean.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"Gemini 输出中没有 JSON: {clean[:500]}")
    return json.loads(clean[start:end + 1])


def required_markers(text: str) -> set[str]:
    out = set(re.findall(r"</?[A-Za-z][A-Za-z0-9_:-]*(?:\s*:[^>]*)?>", text))
    out.update(re.findall(r"%![A-Za-z0-9_]+!%|%s\d+|%[A-Za-z]|\$[A-Za-z]", text))
    return out


def gemini_command() -> list[str]:
    exe = shutil.which("gemini.cmd") or shutil.which("gemini")
    if not exe:
        raise FileNotFoundError("找不到 Gemini CLI。请确认 gemini 在 PATH 中。")
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/c", exe]
    return [exe]


def run_gemini(prompt: str, timeout: int, model: str = "") -> str:
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "xterm-256color"
    # Do not pass -m. Project .gemini/settings.json selects CLI Auto (Gemini 3).
    # Clear model env overrides so the CLI's project-level model routing wins.
    env.pop("GEMINI_MODEL", None)
    cmd = gemini_command()
    if model:
        cmd += ["-m", model]
    proc = subprocess.run(
        cmd + ["--approval-mode", "plan", "--skip-trust", "-p", "不要调用工具，不要读写文件。只根据 stdin 做翻译，并且只输出严格 JSON。"],
        input=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout)
    return proc.stdout


def command_translate(args: argparse.Namespace) -> int:
    translations_path = DATA / "translations.jsonl"
    rows = read_jsonl(translations_path)
    by_id = {row["id"]: row for row in rows}
    RAW.mkdir(parents=True, exist_ok=True)

    batch_paths = sorted(BATCHES.glob("*.json"))
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        batch_paths = [p for p in batch_paths if p.stem in wanted]
    if args.limit:
        batch_paths = batch_paths[:args.limit]

    for path in batch_paths:
        batch = json.loads(path.read_text(encoding="utf-8"))
        ids = [row["id"] for row in batch["rows"]]
        if not args.overwrite and all(by_id[i].get("target") or by_id[i].get("source") == "" for i in ids):
            print(f"skip {batch['package_id']}")
            continue
        payload = []
        for row in batch["rows"]:
            payload.append({
                "id": row["id"],
                "kind": row["kind"],
                "token_or_key": row.get("token") or row.get("key"),
                "scene_files": row.get("scene_files", []),
                "source": row["source"],
            })
        prompt = f"""你是专业游戏本地化译者。请翻译《The Stanley Parable》文本。

只输出 JSON：{{"translations":[{{"id":"原样保留","target":"简体中文译文"}}]}}
每个输入 id 必须输出一条。不要解释。

背景和术语：
{background_text()}

当前批次：{batch['package_id']}
关联度：{batch['tightness']}
上下文：{batch['description']}

待翻译：
{json.dumps(payload, ensure_ascii=False)}
"""
        parsed = None
        last_error = None
        for attempt in range(1, args.retries + 1):
            raw = run_gemini(prompt if last_error is None else prompt + f"\n上次错误：{last_error}\n请修正后重新输出完整 JSON。", args.timeout)
            (RAW / f"{batch['package_id']}.attempt{attempt}.txt").write_text(raw, encoding="utf-8")
            try:
                data = parse_gemini_json(raw)
                translations = {item["id"]: item["target"] for item in data["translations"]}
                missing = set(ids) - set(translations)
                if missing:
                    raise ValueError(f"缺少 id: {sorted(missing)[:5]}")
                problems = []
                for row in batch["rows"]:
                    src = row["source"]
                    tgt = translations[row["id"]]
                    if src and not tgt.strip():
                        problems.append(f"{row['id']} 空译文")
                    lost = required_markers(src) - required_markers(tgt)
                    if lost:
                        problems.append(f"{row['id']} 丢失标记 {sorted(lost)}")
                if problems:
                    raise ValueError("; ".join(problems[:8]))
                parsed = translations
                break
            except Exception as exc:
                last_error = str(exc)
                print(f"{batch['package_id']} attempt {attempt} failed: {last_error}", file=sys.stderr)
                time.sleep(attempt * 2)
        if parsed is None:
            raise RuntimeError(f"Gemini 翻译失败: {batch['package_id']} / {last_error}")
        for row_id, target in parsed.items():
            by_id[row_id]["target"] = target
        write_jsonl(translations_path, rows)
        print(f"translated {batch['package_id']} ({len(ids)} rows)")
    return 0


def estimated_width(text: str) -> float:
    width = 0.0
    for ch in text:
        code = ord(ch)
        if ch in "\n\r\t":
            continue
        if code >= 0x4E00 and code <= 0x9FFF:
            width += 1.0
        elif code > 0x3000:
            width += 0.9
        elif ch.isupper():
            width += 0.62
        elif ch in "il.,:;!|' ":
            width += 0.28
        else:
            width += 0.5
    return width


def audit_rows(rows: list[dict]) -> dict:
    problems: list[dict] = []
    for row in rows:
        source = row.get("source") or ""
        target = row.get("target") or ""
        if source and not target.strip():
            problems.append({"severity": "fatal", "id": row["id"], "type": "missing", "message": "缺少译文"})
            continue
        lost = required_markers(source) - required_markers(target)
        if lost:
            problems.append({"severity": "fatal", "id": row["id"], "type": "markers", "message": f"丢失标记 {sorted(lost)}"})
        letters = re.findall(r"[A-Za-z]{4,}", target)
        if row["kind"] != "sfx" and len(letters) >= 3 and not re.search(r"[\u4e00-\u9fff]", target):
            problems.append({"severity": "warn", "id": row["id"], "type": "english_leftover", "message": "疑似未翻译或英文残留过多"})
        if row["kind"] == "ui" and source and target:
            src_width = max(estimated_width(source), 1.0)
            tgt_width = estimated_width(target)
            if tgt_width > src_width * 1.25 and tgt_width > 14:
                problems.append({
                    "severity": "warn",
                    "id": row["id"],
                    "type": "ui_width",
                    "message": f"UI 译文估算宽度偏长 {tgt_width:.1f}/{src_width:.1f}",
                })
    return {
        "rows": len(rows),
        "fatal": sum(1 for p in problems if p["severity"] == "fatal"),
        "warn": sum(1 for p in problems if p["severity"] == "warn"),
        "problems": problems,
    }


def command_audit(args: argparse.Namespace) -> int:
    rows = read_jsonl(DATA / "translations.jsonl")
    report = audit_rows(rows)
    reports = ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 汉化审计报告",
        "",
        f"- 总条目：{report['rows']}",
        f"- 严重问题：{report['fatal']}",
        f"- 警告：{report['warn']}",
        "",
    ]
    for problem in report["problems"][:200]:
        lines.append(f"- [{problem['severity']}] {problem['type']} {problem['id']}: {problem['message']}")
    (reports / "audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"rows": report["rows"], "fatal": report["fatal"], "warn": report["warn"]}, ensure_ascii=False))
    return 1 if report["fatal"] else 0


def command_review(args: argparse.Namespace) -> int:
    rows = read_jsonl(DATA / "translations.jsonl")
    by_id = {row["id"]: row for row in rows}
    review_dir = DATA / "gemini_review"
    review_dir.mkdir(parents=True, exist_ok=True)

    batch_paths = sorted(BATCHES.glob("*.json"))
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        batch_paths = [p for p in batch_paths if p.stem in wanted]
    if args.limit:
        batch_paths = batch_paths[:args.limit]

    total_corrections = 0
    for path in batch_paths:
        batch = json.loads(path.read_text(encoding="utf-8"))
        payload = []
        for src_row in batch["rows"]:
            row = by_id[src_row["id"]]
            if row.get("source") and not row.get("target"):
                raise RuntimeError(f"审校前仍有未翻译条目: {row['id']}")
            payload.append({
                "id": row["id"],
                "kind": row["kind"],
                "token_or_key": row.get("token") or row.get("key"),
                "scene_files": row.get("scene_files", []),
                "source": row["source"],
                "target": row.get("target", ""),
                "ui_width_risk": row["kind"] == "ui" and estimated_width(row.get("target", "")) > max(estimated_width(row["source"]) * 1.25, 14),
            })
        prompt = f"""你是《The Stanley Parable》简体中文本地化审校。请逐条核对英文原文和中文译文，只修正确实有问题的条目。

只输出 JSON：{{"corrections":[{{"id":"原样保留","target":"修正后的简体中文","reason":"一句话说明"}}]}}
如果整批都没问题，输出 {{"corrections":[]}}。不要解释。

审校标准：
1. 必须忠于原文含义、剧情上下文和旁白语气；旁白要自然、有讽刺感和戏剧感，不要翻译腔。
2. UI 文字要短，优先能放进原按钮/菜单；必要时用中文常见短词。
3. 保留所有 Source 标签和变量，如 <sfx>、<len:5>、<norepeat:5>、%s1、$W。
4. 不要把专名改乱：Stanley=史丹利，Narrator=旁白，Mind Control Facility=精神控制设施。

背景和术语：
{background_text()}

当前批次：{batch['package_id']}
关联度：{batch['tightness']}
上下文：{batch['description']}

待审校：
{json.dumps(payload, ensure_ascii=False)}
"""
        parsed = None
        last_error = None
        for attempt in range(1, args.retries + 1):
            raw = run_gemini(
                prompt if last_error is None else prompt + f"\n上次错误：{last_error}\n请修正后重新输出完整 JSON。",
                args.timeout,
                args.model,
            )
            (review_dir / f"{batch['package_id']}.attempt{attempt}.txt").write_text(raw, encoding="utf-8")
            try:
                data = parse_gemini_json(raw)
                corrections = data.get("corrections", [])
                if not isinstance(corrections, list):
                    raise ValueError("corrections 不是数组")
                valid: list[dict] = []
                ids = {row["id"] for row in payload}
                for item in corrections:
                    row_id = item["id"]
                    if row_id not in ids:
                        raise ValueError(f"未知 id: {row_id}")
                    target = item["target"]
                    source = by_id[row_id]["source"]
                    lost = required_markers(source) - required_markers(target)
                    if lost:
                        raise ValueError(f"{row_id} 修正后丢失标记 {sorted(lost)}")
                    valid.append({"id": row_id, "target": target, "reason": item.get("reason", "")})
                parsed = valid
                break
            except Exception as exc:
                last_error = str(exc)
                print(f"review {batch['package_id']} attempt {attempt} failed: {last_error}", file=sys.stderr)
                time.sleep(attempt * 2)
        if parsed is None:
            raise RuntimeError(f"Gemini 审校失败: {batch['package_id']} / {last_error}")
        if parsed:
            for item in parsed:
                by_id[item["id"]]["target"] = item["target"]
            total_corrections += len(parsed)
            write_jsonl(DATA / "translations.jsonl", rows)
        print(f"reviewed {batch['package_id']} corrections={len(parsed)}")
    print(json.dumps({"corrections": total_corrections}, ensure_ascii=False))
    return 0


def caption_text(tokens: dict[str, str]) -> str:
    lines = ['"lang"', "{", '\t"Language" "schinese"', '\t"Tokens"', "\t{"]
    for token, text in sorted(tokens.items(), key=lambda kv: kv[0].lower()):
        lines.append(f'\t\t"{token}" "{escape_value(text)}"')
    lines += ["\t}", "}", ""]
    return "\r\n".join(lines)


def add_extra_lang_tokens(text: str, source_file: str) -> str:
    extra = EXTRA_LANG_TOKENS.get(source_file, {})
    if not extra:
        return text
    existing = set(re.findall(r'^\s*"([^"]+)"\s+"', text, flags=re.MULTILINE))
    additions = [
        f'\t\t"{key}" "{escape_value(value)}"'
        for key, value in extra.items()
        if key not in existing
    ]
    if not additions:
        return text
    match = re.search(r'"Tokens"\s*\{', text, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"{source_file} 缺少 Tokens block")
    open_index = text.find("{", match.start())
    close_index = matching_brace(text, open_index)
    insert = "\r\n" + "\r\n".join(additions) + "\r\n"
    return text[:close_index] + insert + text[close_index:]


def replace_lang(source: str, source_file: str, targets: dict[str, str]) -> str:
    def repl(match: re.Match) -> str:
        prefix, key, value, suffix = match.group(1), match.group(2), match.group(3), match.group(4)
        if key.lower() == "language":
            return f'{prefix}schinese{suffix}'
        line = source.count("\n", 0, match.start()) + 1
        row_id = f"ui:{source_file}:{line}:{key}"
        target = targets.get(row_id)
        if not target:
            return match.group(0)
        return f"{prefix}{escape_value(target)}{suffix}"
    replaced = re.sub(r'^(\s*"([^"]+)"\s+")((?:\\.|[^"])*)(".*)$', repl, source, flags=re.MULTILINE)
    return add_extra_lang_tokens(replaced, source_file)


def download_file(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if len(data) < 1024:
        raise RuntimeError(f"下载内容异常: {url}")
    path.write_bytes(data)


def command_fonts(args: argparse.Namespace) -> int:
    font_dir = ROOT / "assets" / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)
    for item in FONT_FILES:
        path = font_dir / item["name"]
        if args.force or not path.exists():
            print(f"download {item['name']}")
            download_file(item["url"], path)
    license_path = font_dir / "OFL.txt"
    if args.force or not license_path.exists():
        download_file(FONT_LICENSE_URL, license_path)
    print(json.dumps({"fonts": [item["name"] for item in FONT_FILES], "dir": str(font_dir)}, ensure_ascii=False))
    return 0


def matching_brace(text: str, open_index: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for i in range(open_index, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("找不到匹配的大括号")


def add_custom_fonts(text: str) -> str:
    if "NotoSansSC-VF.ttf" in text:
        return text
    match = re.search(r"CustomFontFiles(?:\s*\[[^\]]+\])?\s*\{", text)
    if not match:
        insert = '\n\tCustomFontFiles\n\t{\n\t\t"90"\t\t"resource/NotoSansSC-VF.ttf"\n\t}\n'
        close = text.rfind("}")
        if close < 0:
            raise ValueError("scheme 文件缺少结尾大括号")
        return text[:close] + insert + text[close:]
    open_index = text.find("{", match.start())
    close_index = matching_brace(text, open_index)
    insert = '\n\t\t"90"\t\t"resource/NotoSansSC-VF.ttf"'
    return text[:close_index] + insert + "\n" + text[close_index:]


def replace_named_blocks(text: str, block_name: str, transform) -> str:
    pattern = re.compile(r'(?m)^(\s*)"' + re.escape(block_name) + r'"(?:\s*\[[^\]]+\])?\s*\{')
    out: list[str] = []
    pos = 0
    while True:
        match = pattern.search(text, pos)
        if not match:
            out.append(text[pos:])
            return "".join(out)
        open_index = text.find("{", match.start())
        close_index = matching_brace(text, open_index)
        out.append(text[pos:match.start()])
        block = text[match.start():close_index + 1]
        out.append(transform(block))
        pos = close_index + 1


def patch_font_block(block: str, tall: str | None = None, family: str = CJK_FONT_NAME) -> str:
    if '"name"' not in block:
        return block
    block = re.sub(r'("name"\s+)"[^"]+"', rf'\1"{family}"', block)
    if tall is not None:
        block = re.sub(r'("tall"\s+)"[^"]+"', rf'\1"{tall}"', block)
    block = re.sub(r'("range"\s+")0x[0-9A-Fa-f]+\s+0x[0-9A-Fa-f]+(".*)', r'\g<1>0x0000 0xFFFF\2', block)
    return block


def patch_specific_font_blocks(text: str, specs: dict[str, dict[str, str]]) -> str:
    for block_name, options in specs.items():
        text = replace_named_blocks(
            text,
            block_name,
            lambda block, opts=options: patch_font_block(block, **opts),
        )
    return text


def patch_common_font_names(text: str) -> str:
    common_names = [
        "Verdana",
        "Arial",
        "Tahoma",
        "Consolas",
        "Courier New",
        "Lucida Console",
        "Lucida Grande",
        "Trebuchet MS",
        "Impact",
        "PT Sans",
        "Trade Gothic Bold",
        "UniversLTStd-Cn",
        "UniversLTStd-BoldCn",
    ]
    pattern = r'("name"\s+")(' + "|".join(re.escape(name) for name in common_names) + r')(")'
    text = re.sub(pattern, rf"\1{CJK_FONT_NAME}\3", text)
    return re.sub(r'("range"\s+")0x0000\s+0x0(?:07F|0FF|17F)(".*)', r"\g<1>0x0000 0xFFFF\2", text)


def patch_scheme_text(text: str, name: str) -> str:
    text = patch_common_font_names(text)
    if name == "basemodui_scheme.res":
        text = re.sub(r'(?m)^(\s*Dialog\.TitleFont\s+)"[^"]+"', r'\1"DialogTitle"', text)
        text = patch_specific_font_blocks(text, {
            "MainMenuItem": {"tall": "34"},
            "MainMenuItemSmall": {"tall": "28"},
            "MainMenuItemSmaller": {"tall": "22"},
            "MainMenuHeader2": {"tall": "24"},
            "DialogTitle": {"tall": "28"},
            "DialogButton": {"tall": "18"},
            "ConfirmationText": {"tall": "18"},
            "CloseCaption_Normal": {"tall": "20"},
            "CloseCaption_Italic": {"tall": "20"},
            "CloseCaption_Bold": {"tall": "20"},
            "CloseCaption_BoldItalic": {"tall": "20"},
            "CloseCaption_Console": {"tall": "20"},
        })
    return add_custom_fonts(text)


def copy_fonts_to(out: Path) -> None:
    font_dir = ROOT / "assets" / "fonts"
    for item in FONT_FILES:
        src = font_dir / item["name"]
        if not src.exists():
            raise FileNotFoundError(f"缺少字体文件，请先运行 fonts: {src}")
        shutil.copy2(src, out / item["name"])
    license_path = font_dir / "OFL.txt"
    if license_path.exists():
        shutil.copy2(license_path, out / "NotoSansSC-OFL.txt")


def patch_ui_resource(text: str, name: str) -> str:
    normalized = name.replace("\\", "/").lower()
    if normalized == "ui/basemodui/mainmenu_tsp.res":
        text = re.sub(r'("labelText"\s+)"Credits"', r'\1"#TSP_Menu_Credits"', text)
    elif normalized == "ui/basemodui/options.res":
        text = re.sub(r'("labelText"\s+)"Extras"', r'\1"#L4D360UI_MainMenu_Extras"', text)
    elif normalized == "ui/basemodui/extrasdialog.res":
        text = re.sub(r'("labelText"\s+)"Achievement"', r'\1"#TSP_Extras_Achievement"', text)
        text = re.sub(r'("labelText"\s+)"Saves"', r'\1"#TSP_Extras_Saves"', text)
    return text


def command_build(args: argparse.Namespace) -> int:
    rows = read_jsonl(DATA / "translations.jsonl")
    missing = [row["id"] for row in rows if row["source"] and not row.get("target")]
    if missing and not args.allow_untranslated:
        raise RuntimeError(f"还有 {len(missing)} 条未翻译，例如 {missing[:5]}")

    targets = {row["id"]: (row.get("target") or row["source"]) for row in rows}
    out = DIST / "thestanleyparable" / "resource"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    for target_file in ["subtitles_schinese.txt", "closecaption_schinese.txt"]:
        tokens = {}
        for row in rows:
            if row.get("target_file") == target_file:
                tokens[row["token"]] = targets[row["id"]]
        (out / target_file).write_text(caption_text(tokens), encoding="utf-16", newline="")
        (out / target_file.replace(".txt", ".dat")).write_bytes(compile_dat(tokens))

    for name in LANG_FILES:
        src = SOURCE / name
        if not src.exists():
            continue
        translated = replace_lang(read_text(src), name, targets)
        schinese = name.replace("_english", "_schinese")
        (out / schinese).write_text(translated, encoding="utf-16", newline="")
        (out / name).write_text(translated, encoding="utf-16", newline="")

    for caption_file in ["subtitles", "closecaption"]:
        for ext in [".txt", ".dat"]:
            schinese = out / f"{caption_file}_schinese{ext}"
            english = out / f"{caption_file}_english{ext}"
            if schinese.exists():
                shutil.copy2(schinese, english)

    copy_fonts_to(out)

    for name in SCHEME_FILES:
        src = SOURCE / name
        if src.exists():
            (out / name).write_text(patch_scheme_text(read_text(src), name), encoding="utf-8", newline="")

    for name in UI_RESOURCE_FILES:
        src = SOURCE / name
        if not src.exists():
            raise FileNotFoundError(f"缺少 UI 源资源: {src}")
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patch_ui_resource(read_text(src), name), encoding="utf-8", newline="")
    print(f"built {out}")
    return 0


def command_install(args: argparse.Namespace) -> int:
    mod_dir = game_mod_dir(Path(args.game_dir).resolve())
    out = DIST / "thestanleyparable"
    files = sorted(p for p in out.rglob("*") if p.is_file())
    for src in files:
        rel = src.relative_to(out)
        dest = mod_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    print(json.dumps({"installed": [str((mod_dir / p.relative_to(out))) for p in files]}, ensure_ascii=False, indent=2))
    return 0


PACKAGE_README = '''史丹利的寓言 简体中文汉化包

安装：
把压缩包里的 thestanleyparable 文件夹直接解压/覆盖到 The Stanley Parable 游戏根目录。

恢复：
在 Steam 里对游戏执行“验证游戏文件完整性”，或删除游戏后重新下载。

说明：
- 本包会覆盖英文资源文件以保证原版游戏默认启动即可显示中文，同时保留 schinese 副本。
- 字体使用 Noto Sans SC，随包按 SIL Open Font License 附带。
'''


def command_package(args: argparse.Namespace) -> int:
    src = DIST / "thestanleyparable"
    if not src.exists():
        raise FileNotFoundError("缺少 dist\\thestanleyparable，请先运行 build")
    release_root = ROOT / "release"
    release_root.mkdir(parents=True, exist_ok=True)
    version = args.version or datetime.now().strftime("%Y%m%d_%H%M%S")
    package_dir = release_root / f"TheStanleyParable_CN_Overlay_{version}"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    payload_mod = package_dir / "thestanleyparable"
    shutil.copytree(src, payload_mod)

    files = sorted(str(p.relative_to(package_dir)).replace("\\", "/") for p in payload_mod.rglob("*") if p.is_file())
    (payload_mod / "resource" / "cn_patch_manifest.json").write_text(json.dumps({
        "name": "The Stanley Parable Simplified Chinese Patch",
        "version": version,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "overlayFiles": files,
        "font": CJK_FONT_NAME,
        "install": "Extract this archive into the game root and overwrite thestanleyparable files.",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (package_dir / "README.txt").write_text(PACKAGE_README, encoding="utf-8", newline="\r\n")

    archive_base = str(package_dir)
    archive = shutil.make_archive(archive_base, "zip", root_dir=package_dir)
    print(json.dumps({"package_dir": str(package_dir), "archive": archive, "files": len(files)}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fonts")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=command_fonts)
    p = sub.add_parser("extract")
    p.add_argument("game_dir")
    p.set_defaults(func=command_extract)
    p = sub.add_parser("group")
    p.set_defaults(func=command_group)
    p = sub.add_parser("translate")
    p.add_argument("--only", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=command_translate)
    p = sub.add_parser("review")
    p.add_argument("--only", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--model", default="gemini-3.1-pro-preview")
    p.set_defaults(func=command_review)
    p = sub.add_parser("audit")
    p.set_defaults(func=command_audit)
    p = sub.add_parser("build")
    p.add_argument("--allow-untranslated", action="store_true")
    p.set_defaults(func=command_build)
    p = sub.add_parser("install")
    p.add_argument("game_dir")
    p.set_defaults(func=command_install)
    p = sub.add_parser("package")
    p.add_argument("--version", default="")
    p.set_defaults(func=command_package)
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
