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

    if phase == "布局" and zone == "边上":
        return Scenario(
            template="开局边上太急",
            title="开局别着急冲边",
            slogan="先站稳，再出拳。",
            problem="这手太急着打架，自己的地基还没站稳。",
            fix="先把自己的棋连好、补厚，再去碰对手。",
            action="下一盘前30手，只要想冲断，先问自己“我这块稳了吗？”",
        )
    if phase == "布局" and zone == "中腹":
        return Scenario(
            template="开局中间漂移",
            title="开局先下角边",
            slogan="角边是地基，中间是屋顶。",
            problem="太早跑到中间，角和边的大场没先拿。",
            fix="先占角边要点，等形势清楚再进中腹。",
            action="前30手优先角边，除非中腹有必救棋。",
        )
    if phase == "中盘" and zone == "边上":
        return Scenario(
            template="边上次序错误",
            title="边上先连回再冲断",
            slogan="先连回，再断人。",
            problem="边上次序走反了，容易被对手借力。",
            fix="先把自己的断点补掉，再考虑冲断对手。",
            action="边上接触战先看两件事：我有断点吗？我有气吗？",
        )
    if phase == "中盘" and zone == "中腹":
        return Scenario(
            template="中腹判断漂",
            title="中腹先看强弱",
            slogan="先看谁弱，再想吃子。",
            problem="中腹下得太飘，没先判断强弱和急所。",
            fix="先处理弱棋，再走看起来“很大”的棋。",
            action="每次想吃子前，先说出“我方最弱那块在哪”。",
        )
    if phase == "中盘" and context == "优势失误":
        return Scenario(
            template="领先还要硬杀",
            title="领先局面不乱战",
            slogan="领先不冒险，稳稳把家搬。",
            problem="本来已经不错了，这手把局面又下复杂了。",
            fix="领先时先补薄、先守空、先抢先手官子。",
            action="一觉得“我领先”，就先找最稳的一手。",
        )
    if phase == "官子":
        return Scenario(
            template="领先官子乱下",
            title="官子要稳收",
            slogan="领先收官子，不找新战事。",
            problem="官子阶段还在找战斗，容易把优势送掉。",
            fix="优先先手官子和收大官子，减少复杂变化。",
            action="官子只做一件事：每手尽量先手、尽量大。",
        )
    if context == "均势失误":
        return Scenario(
            template="均势胜负手误判",
            title="均势先抢最大点",
            slogan="均势不逞强，先手最值钱。",
            problem="胜负接近时，这手没有走在最大点上。",
            fix="先比两点价值，再下手。",
            action="落子前先想：我这手比对手最大点更大吗？",
        )
    if context == "逆风失误":
        return Scenario(
            template="逆风一手翻盘梦",
            title="逆风别赌命",
            slogan="逆风先追分，不赌一步。",
            problem="落后时太想一手翻盘，风险过大。",
            fix="先抢最大点，连续追分，慢慢缩小差距。",
            action="落后时优先“先手大官子 + 补弱”。",
        )
    return Scenario(
        template="综合判断偏差",
        title="先稳后战",
        slogan="不连错，就是赢。",
        problem="这手没有走在当前最急的位置。",
        fix="优先补弱、守空、抢先手，再考虑进攻。",
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
