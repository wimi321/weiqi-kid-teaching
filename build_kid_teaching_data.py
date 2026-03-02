#!/usr/bin/env python3
"""Build kid-friendly interactive teaching data from critical turning points."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from go_review import GameRecord, parse_sgf


ROOT = Path("/Users/haoc/Developer/wq20260301")
CRITICAL_JSON = ROOT / "review_output_mgqp_full28" / "critical_turning_points.json"
SGF_DIR = ROOT / "data" / "mgqp_raw" / "mgqp"
OUT_JSON = ROOT / "review_output_mgqp_full28" / "kid_teaching_data.json"

LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


@dataclass
class Scenario:
    template: str
    title: str
    slogan: str
    problem: str
    fix: str
    action: str


def gtp_to_xy(move: str, size: int) -> Optional[Tuple[int, int]]:
    m = move.strip().upper()
    if not m or m == "PASS":
        return None
    if m[0] not in LETTERS:
        return None
    try:
        row = int(m[1:])
    except ValueError:
        return None
    x = LETTERS.index(m[0])
    y = size - row
    if x < 0 or y < 0 or x >= size or y >= size:
        return None
    return x, y


def neighbors(x: int, y: int, size: int) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < size and 0 <= ny < size:
            out.append((nx, ny))
    return out


def collect_group(
    board: Dict[Tuple[int, int], str], start: Tuple[int, int], size: int
) -> Tuple[List[Tuple[int, int]], int]:
    color = board.get(start)
    if color is None:
        return [], 0
    stack = [start]
    seen = {start}
    stones: List[Tuple[int, int]] = []
    liberties = 0
    liberty_seen = set()
    while stack:
        p = stack.pop()
        stones.append(p)
        for n in neighbors(p[0], p[1], size):
            c = board.get(n)
            if c is None:
                if n not in liberty_seen:
                    liberty_seen.add(n)
                    liberties += 1
            elif c == color and n not in seen:
                seen.add(n)
                stack.append(n)
    return stones, liberties


def apply_move(
    board: Dict[Tuple[int, int], str], color: str, move: str, size: int
) -> None:
    xy = gtp_to_xy(move, size)
    if xy is None:
        return
    x, y = xy
    board[(x, y)] = color
    opp = "W" if color == "B" else "B"

    # Capture adjacent opponent groups with no liberties.
    for n in neighbors(x, y, size):
        if board.get(n) != opp:
            continue
        grp, libs = collect_group(board, n, size)
        if libs == 0:
            for s in grp:
                board.pop(s, None)

    # Handle self-capture edge case.
    grp, libs = collect_group(board, (x, y), size)
    if libs == 0:
        for s in grp:
            board.pop(s, None)


def build_board_before(game: GameRecord, move_no: int) -> Dict[Tuple[int, int], str]:
    board: Dict[Tuple[int, int], str] = {}
    for c, move in game.initial_stones:
        xy = gtp_to_xy(move, game.size)
        if xy is not None:
            board[xy] = c
    for i, m in enumerate(game.moves, start=1):
        if i >= move_no:
            break
        apply_move(board, m.color, m.gtp_coord, game.size)
    return board


def board_position_hash(board: Dict[Tuple[int, int], str], size: int) -> str:
    stones = [
        f"{x},{y},{c}"
        for (x, y), c in sorted(board.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    ]
    raw = f"{size}|{';'.join(stones)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def point_position_hash(
    point: Dict[str, object],
    sgf_map: Dict[str, Path],
    game_cache: Dict[str, GameRecord],
    position_cache: Dict[Tuple[str, int], str],
) -> str:
    game_name = str(point.get("game", ""))
    try:
        move_no = int(point.get("move_no", 0))
    except (TypeError, ValueError):
        move_no = 0
    cache_key = (game_name, move_no)
    if cache_key in position_cache:
        return position_cache[cache_key]

    sgf_path = sgf_map.get(game_name)
    if sgf_path is None:
        fallback = f"missing:{game_name}:{move_no}"
        position_cache[cache_key] = fallback
        return fallback

    game = game_cache.get(game_name)
    if game is None:
        game = parse_sgf(sgf_path)
        game_cache[game_name] = game
    board = build_board_before(game, move_no)
    h = board_position_hash(board, game.size)
    position_cache[cache_key] = h
    return h


def make_scenario(point: Dict[str, object]) -> Scenario:
    phase = str(point.get("phase", "中盘"))
    zone = str(point.get("zone", "中腹"))
    context = str(point.get("context", "均势失误"))

    def to_float(v: object, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def normalize_move(v: object) -> str:
        move = str(v or "PASS").strip().upper()
        return move if move else "PASS"

    def move_label(move: str) -> str:
        return "停一手" if move == "PASS" else move

    def parse_gtp(move: str) -> Optional[Tuple[int, int]]:
        if move == "PASS":
            return None
        if len(move) < 2 or move[0] not in LETTERS:
            return None
        try:
            row = int(move[1:])
        except ValueError:
            return None
        return LETTERS.index(move[0]), row

    def severity_text(drop: float) -> str:
        if drop >= 0.70:
            return "这步太关键了！"
        if drop >= 0.62:
            return "这一步非常可惜！"
        if drop >= 0.54:
            return "这一步很可惜！"
        if drop >= 0.47:
            return "这手有点伤。"
        return "这手还能更稳。"

    def context_text(before_wr: float, ctx: str) -> str:
        if ctx == "优势失误" or before_wr >= 0.65:
            return "你当时本来是优势局面"
        if ctx == "均势失误" or 0.47 <= before_wr <= 0.55:
            return "你当时在五五开的胜负处"
        if ctx == "逆风失误" or before_wr <= 0.40:
            return "你当时在追分局面"
        if before_wr >= 0.55:
            return "你当时局面略优"
        return "你当时局面接近"

    def move_gap_text(actual_mv: str, best_mv: str) -> str:
        a = parse_gtp(actual_mv)
        b = parse_gtp(best_mv)
        if a is None or b is None:
            return "这手和推荐点的思路差异比较大。"
        dist = abs(a[0] - b[0]) + abs(a[1] - b[1])
        if dist <= 2:
            return "落点离推荐点很近，主要差在次序和时机。"
        if dist <= 6:
            return "方向有些偏，先后手价值被对手拿走。"
        return "方向偏得比较远，把关键大点让给了对手。"

    actual = normalize_move(point.get("actual"))
    best = normalize_move(point.get("best"))
    pv_raw = point.get("pv", [])
    pv = [
        normalize_move(m)
        for m in pv_raw
        if isinstance(m, str) and normalize_move(m)
    ]
    drop = to_float(point.get("winrate_drop", 0.0), 0.0)
    drop_pct = round(drop * 100, 1)
    before_wr = to_float(point.get("before_winrate", 0.5), 0.5)

    severity = severity_text(drop)
    side = context_text(before_wr, context)
    compare_line = (
        f"实战下了{move_label(actual)}，KataGo更推荐{move_label(best)}，"
        f"这手胜率掉了{drop_pct:.1f}%。"
    )
    if actual == best:
        compare_line = f"KataGo认为这手效率偏低，胜率仍掉了{drop_pct:.1f}%。"
    pv_line = ""
    if pv:
        seq = " -> ".join(move_label(m) for m in pv[:3])
        suffix = " ..." if len(pv) > 3 else ""
        pv_line = f"主线参考：{seq}{suffix}。"
    gap = move_gap_text(actual, best)

    if phase == "布局" and zone == "边上":
        return Scenario(
            template="布局边上稳形",
            title="开局边上先稳形再扩张",
            slogan="开局占边要稳，优势别扩张",
            problem=f"{severity}{side}，{compare_line}开局边上先求稳形和连络，别急着把战线拉长。{gap}",
            fix=f"先把边线要点和己方联络走厚，再考虑压迫和扩张。优先考虑{move_label(best)}。{pv_line}",
            action=f"前30手下边上时，先做两问：我这块稳吗？有断点吗？然后再决定是否进攻。",
        )
    if phase == "布局" and zone == "中腹":
        return Scenario(
            template="布局中腹稳重",
            title="开局中腹别先开战",
            slogan="开局占中要稳重，别急着战斗",
            problem=f"{severity}{side}，{compare_line}布局阶段中腹价值要靠角边支撑，太早开战容易两头落空。{gap}",
            fix=f"先拿角边要点、搭好外势，再进中腹发力。推荐先走{move_label(best)}。{pv_line}",
            action="前30手想下中腹前，先确认角边还有没有更大的点。",
        )
    if phase == "中盘" and zone == "边上":
        return Scenario(
            template="中盘边上形状",
            title="边上作战先看形和气",
            slogan="边上作战看清形状，优势别冲动",
            problem=f"{severity}{side}，{compare_line}中盘边上接触战最怕形状变薄、气紧被反打。{gap}",
            fix=f"先补断点、抢要害气，再决定冲断或强杀。此题先手应考虑{move_label(best)}。{pv_line}",
            action="边上接触战固定三问：我的断点在哪？双方气数谁紧？我有没有退路？",
        )
    if phase == "中盘" and zone == "中腹":
        return Scenario(
            template="中盘中腹强弱",
            title="中腹开战先判强弱",
            slogan="中腹战斗先判强弱，优势别浪战",
            problem=f"{severity}{side}，{compare_line}中腹一旦乱战，强弱判断错了就会连锁崩塌。{gap}",
            fix=f"先安定弱棋，再利用厚势发力；该简化就简化。此处更稳的是{move_label(best)}。{pv_line}",
            action="每次想在中腹动手前，先说出盘上最弱的一块棋，再决定是否开战。",
        )
    if phase == "官子":
        return Scenario(
            template="官子稳收",
            title="官子先收再战",
            slogan="官子阶段稳稳收，优势别找事",
            problem=f"{severity}{side}，{compare_line}官子阶段比的是目数和先后手，不是继续找复杂战斗。{gap}",
            fix=f"先手官子和大官子优先，把可兑现的目数先收进口袋。推荐先走{move_label(best)}。{pv_line}",
            action="官子每手先估目数，再看能否保持先手；没有把握时优先稳收。",
        )
    if context == "均势失误":
        return Scenario(
            template="均势最大点",
            title="均势先抢最大点",
            slogan="均势不逞强，先手最值钱",
            problem=f"{severity}{side}，{compare_line}均势阶段每一手都在比价值和先后手。{gap}",
            fix=f"先比较双方最大点再落子，避免情绪手。此题建议先走{move_label(best)}。{pv_line}",
            action="落子前先说出“对手下一手最想下哪里”，再看自己这手是否更大。",
        )
    if context == "逆风失误":
        return Scenario(
            template="逆风追分",
            title="逆风先追分别豪赌",
            slogan="逆风先追分，不赌一步",
            problem=f"{severity}{side}，{compare_line}逆风时硬拼一步翻盘，通常会把形势继续拉开。{gap}",
            fix=f"先拿稳定分、保持先手，连续追分比豪赌有效。优先考虑{move_label(best)}。{pv_line}",
            action="逆风局每手目标是“缩小差距”，不是“一手翻盘”。",
        )
    return Scenario(
        template="综合判断",
        title="先稳后战",
        slogan="先把棋走厚，再谈攻击",
        problem=f"{severity}{side}，{compare_line}这手没有走在当前最急的位置。{gap}",
        fix=f"优先补弱、守空、抢先手，再考虑进攻。此题更稳的是{move_label(best)}。{pv_line}",
        action="每手先做三问：稳吗？大吗？先手吗？",
    )


def find_sgf_files() -> Dict[str, Path]:
    files = {}
    for p in SGF_DIR.rglob("*.sgf"):
        files[p.name] = p
    return files


def select_points(
    points: List[Dict[str, object]], sgf_map: Dict[str, Path], target: int = 12
) -> List[Dict[str, object]]:
    # Keep key teaching points diverse across phase/zone/context and games.
    out: List[Dict[str, object]] = []
    used_game: Dict[str, int] = {}
    used_key = set()
    used_template = set()
    used_position = set()
    game_cache: Dict[str, GameRecord] = {}
    position_cache: Dict[Tuple[str, int], str] = {}

    def key_of(p: Dict[str, object]) -> Tuple[str, str, str]:
        return (
            str(p.get("phase", "")),
            str(p.get("zone", "")),
            str(p.get("context", "")),
        )

    def position_key_of(p: Dict[str, object]) -> Tuple[str, str, str]:
        return (
            point_position_hash(p, sgf_map, game_cache, position_cache),
            str(p.get("best", "")).strip().upper(),
            str(p.get("color", "")).strip().upper(),
        )

    for p in points:
        bw = float(p.get("before_winrate", 0.5))
        if not (0.2 <= bw <= 0.85):
            continue
        game = str(p.get("game", ""))
        if used_game.get(game, 0) >= 2:
            continue
        template = make_scenario(p).template
        if template in used_template:
            continue
        k = key_of(p)
        if k in used_key:
            continue
        position_key = position_key_of(p)
        if position_key in used_position:
            continue
        out.append(p)
        used_game[game] = used_game.get(game, 0) + 1
        used_key.add(k)
        used_template.add(template)
        used_position.add(position_key)
        if len(out) >= target:
            return out

    for p in points:
        if len(out) >= target:
            break
        game = str(p.get("game", ""))
        if used_game.get(game, 0) >= 2:
            continue
        bw = float(p.get("before_winrate", 0.5))
        if not (0.2 <= bw <= 0.85):
            continue
        position_key = position_key_of(p)
        if position_key in used_position:
            continue
        out.append(p)
        used_game[game] = used_game.get(game, 0) + 1
        used_position.add(position_key)
    return out


def main() -> int:
    points = json.loads(CRITICAL_JSON.read_text(encoding="utf-8"))
    points = sorted(points, key=lambda x: float(x.get("winrate_drop", 0.0)), reverse=True)
    sgf_map = find_sgf_files()
    selected = select_points(points, sgf_map, target=12)

    examples = []
    for idx, p in enumerate(selected, start=1):
        game_name = str(p["game"])
        sgf_path = sgf_map.get(game_name)
        if sgf_path is None:
            continue
        game = parse_sgf(sgf_path)
        move_no = int(p["move_no"])
        board = build_board_before(game, move_no)
        stones = [
            {"x": x, "y": y, "c": c}
            for (x, y), c in sorted(board.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        ]
        scenario = make_scenario(p)
        actual = str(p.get("actual", "pass"))
        best = str(p.get("best", "pass"))
        pv_raw = p.get("pv", [])
        pv = [
            str(m).strip().upper()
            for m in pv_raw
            if isinstance(m, str) and str(m).strip()
        ]
        pv_xy = []
        for m in pv:
            xy = gtp_to_xy(m, game.size)
            if xy is not None:
                pv_xy.append(xy)
        examples.append(
            {
                "id": idx,
                "game": game_name,
                "title": scenario.title,
                "template": scenario.template,
                "slogan": scenario.slogan,
                "problem": scenario.problem,
                "fix": scenario.fix,
                "action": scenario.action,
                "phase": p.get("phase", ""),
                "zone": p.get("zone", ""),
                "context": p.get("context", ""),
                "move_no": move_no,
                "to_play": p.get("color", ""),
                "before_winrate_pct": round(float(p.get("before_winrate", 0.0)) * 100, 1),
                "drop_pct": round(float(p.get("winrate_drop", 0.0)) * 100, 1),
                "cluster_size": int(p.get("cluster_size", 1)),
                "actual": actual,
                "best": best,
                "pv": pv,
                "pv_xy": pv_xy,
                "actual_xy": gtp_to_xy(actual, game.size),
                "best_xy": gtp_to_xy(best, game.size),
                "board_size": game.size,
                "stones": stones,
                "quiz_prompt": "你会下红点还是绿点？点一下棋盘试试！",
            }
        )

    payload = {
        "student_id": "芒果25437",
        "source_games": 28,
        "teaching_examples": len(examples),
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "examples": examples,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] wrote {OUT_JSON} with {len(examples)} examples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
