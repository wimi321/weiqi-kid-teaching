#!/usr/bin/env python3
"""
Batch Go (Weiqi) review pipeline:
SGF files -> KataGo analysis -> issue mining -> optional LLM coaching report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PHASES: Tuple[Tuple[str, int], ...] = (
    ("布局", 50),
    ("中盘", 200),
    ("官子", 10**9),
)


SEVERITY_THRESHOLDS: Tuple[Tuple[str, float], ...] = (
    ("严重", 0.15),
    ("较大", 0.08),
    ("一般", 0.04),
)

CONTEXT_LABELS: Tuple[str, ...] = ("优势失误", "均势失误", "逆风失误", "未知局势")
ZONE_LABELS: Tuple[str, ...] = ("角部", "边上", "中腹", "未知")


LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


@dataclass
class MoveRecord:
    color: str
    sgf_coord: str
    gtp_coord: str


@dataclass
class GameRecord:
    path: str
    black: str
    white: str
    result: str
    date: str
    size: int
    komi: float
    initial_stones: List[Tuple[str, str]]
    moves: List[MoveRecord]


@dataclass
class MoveIssue:
    move_no: int
    color: str
    actual: str
    best: str
    pv: List[str]
    winrate_drop: float
    score_drop: Optional[float]
    phase: str
    severity: str
    before_winrate: Optional[float]
    context: str
    zone: str


@dataclass
class GameReview:
    game_path: str
    black: str
    white: str
    result: str
    date: str
    student_color: str
    student_moves: int
    issue_count: int
    issue_rate: float
    severe_count: int
    major_count: int
    minor_count: int
    avg_drop: float
    phase_move_counts: Dict[str, int]
    phase_issue_counts: Dict[str, int]
    context_issue_counts: Dict[str, int]
    zone_issue_counts: Dict[str, int]
    issues: List[MoveIssue]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="围棋复盘流水线：SGF + KataGo + 可选LLM建议"
    )
    parser.add_argument("--sgf-dir", required=True, type=Path, help="SGF目录")
    parser.add_argument(
        "--out-dir", default=Path("review_output"), type=Path, help="输出目录"
    )
    parser.add_argument(
        "--recent-days",
        default=30,
        type=int,
        help="按文件修改时间筛选最近N天（<=0表示不过滤）",
    )
    parser.add_argument("--max-games", default=30, type=int, help="最多分析对局数")

    parser.add_argument("--katago-bin", required=True, type=Path, help="KataGo可执行文件")
    parser.add_argument("--katago-config", required=True, type=Path, help="KataGo配置文件")
    parser.add_argument("--katago-model", required=True, type=Path, help="KataGo模型文件")
    parser.add_argument(
        "--analysis-threads",
        default=0,
        type=int,
        help="KataGo分析线程（仅当配置文件未设置numAnalysisThreads时生效，0表示不传）",
    )
    parser.add_argument("--rules", default="Chinese", help="规则，如 Chinese / Japanese")
    parser.add_argument("--max-visits", default=500, type=int, help="每步最大访问数")
    parser.add_argument("--timeout-sec", default=30, type=int, help="单次请求超时秒数")

    parser.add_argument(
        "--student-color",
        choices=["auto", "B", "W", "both"],
        default="auto",
        help="学生执子颜色。auto需配合--player-name",
    )
    parser.add_argument("--player-name", default="", help="学生在SGF中的名字（PB/PW匹配）")
    parser.add_argument(
        "--min-winrate-drop",
        default=0.04,
        type=float,
        help="记为问题手的最小胜率下降（0-1）",
    )

    parser.add_argument(
        "--llm-provider",
        choices=["none", "openai"],
        default="none",
        help="是否启用LLM生成建议",
    )
    parser.add_argument("--llm-model", default="gpt-4.1-mini", help="LLM模型名")
    parser.add_argument("--llm-base-url", default="https://api.openai.com/v1", help="LLM API地址")
    parser.add_argument(
        "--openai-api-key",
        default="",
        help="OpenAI API Key（不填则读取OPENAI_API_KEY）",
    )
    return parser.parse_args()


def severity_for_drop(drop: float) -> str:
    for label, threshold in SEVERITY_THRESHOLDS:
        if drop >= threshold:
            return label
    return "轻微"


def phase_for_move(move_no: int) -> str:
    for phase, bound in PHASES:
        if move_no <= bound:
            return phase
    return "未知"


def context_for_before_wr(before_wr: Optional[float]) -> str:
    if before_wr is None:
        return "未知局势"
    if before_wr >= 0.65:
        return "优势失误"
    if before_wr <= 0.35:
        return "逆风失误"
    return "均势失误"


def gtp_to_xy(move: str, size: int) -> Optional[Tuple[int, int]]:
    m = move.strip().upper()
    if not m or m == "PASS":
        return None
    letter = m[0]
    if letter not in LETTERS:
        return None
    try:
        row = int(m[1:])
    except ValueError:
        return None
    x = LETTERS.index(letter)
    y = size - row
    if x < 0 or y < 0 or x >= size or y >= size:
        return None
    return x, y


def zone_for_gtp(move: str, size: int) -> str:
    xy = gtp_to_xy(move, size)
    if xy is None:
        return "未知"
    x, y = xy
    near_left = x <= 2
    near_right = x >= size - 3
    near_top = y <= 2
    near_bottom = y >= size - 3
    if (near_left or near_right) and (near_top or near_bottom):
        return "角部"
    if near_left or near_right or near_top or near_bottom:
        return "边上"
    return "中腹"


def safe_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def parse_komi(raw: Any) -> float:
    text = str(raw).strip()
    val = safe_float(text, 7.5)
    # Fox SGF often stores komi like "375" to represent 3.75.
    if abs(val) > 50:
        val = val / 100.0
    # KataGo analysis mode requires integer or half-integer komi.
    val = round(val * 2.0) / 2.0
    return val


def list_recent_sgfs(sgf_dir: Path, recent_days: int, max_games: int) -> List[Path]:
    files = [p for p in sgf_dir.rglob("*.sgf") if p.is_file()]
    if recent_days > 0:
        cutoff = time.time() - recent_days * 86400
        recent = [p for p in files if p.stat().st_mtime >= cutoff]
        if recent:
            files = recent
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if max_games > 0:
        files = files[:max_games]
    return files


PROP_RE = re.compile(r"([A-Za-z]+)((?:\[(?:\\.|[^\]])*\])+)")
VAL_RE = re.compile(r"\[((?:\\.|[^\]])*)\]")


def unescape_sgf_value(value: str) -> str:
    value = value.replace("\\]", "]")
    value = value.replace("\\\\", "\\")
    value = value.replace("\\n", "\n")
    return value.strip()


def split_main_nodes(sgf_text: str) -> List[str]:
    nodes: List[str] = []
    depth = 0
    in_bracket = False
    escaped = False
    node_start: Optional[int] = None

    for i, ch in enumerate(sgf_text):
        if in_bracket:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "]":
                in_bracket = False
            continue

        if ch == "[":
            in_bracket = True
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            if depth == 1 and node_start is not None:
                nodes.append(sgf_text[node_start:i])
                node_start = None
            depth -= 1
            continue
        if ch == ";" and depth == 1:
            if node_start is not None:
                nodes.append(sgf_text[node_start:i])
            node_start = i + 1
    if node_start is not None:
        nodes.append(sgf_text[node_start:])
    return nodes


def parse_props(node_text: str) -> Dict[str, List[str]]:
    props: Dict[str, List[str]] = {}
    for match in PROP_RE.finditer(node_text):
        key = match.group(1)
        block = match.group(2)
        values = [unescape_sgf_value(v) for v in VAL_RE.findall(block)]
        props.setdefault(key, []).extend(values)
    return props


def sgf_to_gtp(coord: str, size: int) -> str:
    c = coord.strip().lower()
    if c == "" or c == "tt":
        return "pass"
    if len(c) < 2:
        return "pass"

    x = ord(c[0]) - ord("a")
    y = ord(c[1]) - ord("a")
    if x < 0 or y < 0 or x >= size or y >= size:
        return "pass"
    if x >= len(LETTERS):
        return "pass"
    return f"{LETTERS[x]}{size - y}"


def parse_sgf(path: Path) -> GameRecord:
    text = path.read_text(encoding="utf-8", errors="ignore")
    nodes = split_main_nodes(text)
    if not nodes:
        raise ValueError(f"无法解析主线节点: {path}")

    root = parse_props(nodes[0])
    size_raw = (root.get("SZ", ["19"])[0] or "19").split(":")[0]
    size = int(safe_float(size_raw, 19))
    komi = parse_komi(root.get("KM", ["7.5"])[0])

    initial_stones: List[Tuple[str, str]] = []
    for c in root.get("AB", []):
        initial_stones.append(("B", sgf_to_gtp(c, size)))
    for c in root.get("AW", []):
        initial_stones.append(("W", sgf_to_gtp(c, size)))
    initial_stones = [stone for stone in initial_stones if stone[1] != "pass"]

    moves: List[MoveRecord] = []
    for node in nodes[1:]:
        props = parse_props(node)
        if "B" in props:
            sgf_coord = props["B"][0]
            moves.append(MoveRecord("B", sgf_coord, sgf_to_gtp(sgf_coord, size)))
        elif "W" in props:
            sgf_coord = props["W"][0]
            moves.append(MoveRecord("W", sgf_coord, sgf_to_gtp(sgf_coord, size)))

    return GameRecord(
        path=str(path.resolve()),
        black=(root.get("PB", ["Black"])[0] or "Black"),
        white=(root.get("PW", ["White"])[0] or "White"),
        result=(root.get("RE", [""])[0] or ""),
        date=(root.get("DT", [""])[0] or ""),
        size=size,
        komi=komi,
        initial_stones=initial_stones,
        moves=moves,
    )


def infer_student_color(
    game: GameRecord, forced_mode: str, player_name: str
) -> str:
    if forced_mode in {"B", "W", "both"}:
        return forced_mode
    if player_name:
        target = player_name.strip().lower()
        if game.black.strip().lower() == target:
            return "B"
        if game.white.strip().lower() == target:
            return "W"
    return "both"


class KataGoAnalyzer:
    def __init__(
        self,
        bin_path: Path,
        config_path: Path,
        model_path: Path,
        analysis_threads: int,
        max_visits: int,
        rules: str,
        timeout_sec: int,
    ) -> None:
        cmd = [
            str(bin_path),
            "analysis",
            "-config",
            str(config_path),
            "-model",
            str(model_path),
        ]
        if analysis_threads > 0:
            cmd.extend(["-analysis-threads", str(analysis_threads)])
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self.proc.stdin is None or self.proc.stdout is None or self.proc.stderr is None:
            raise RuntimeError("KataGo管道初始化失败")

        self.max_visits = max_visits
        self.rules = rules
        self.timeout_sec = timeout_sec
        self._next_id = 1
        self._stderr_tail: List[str] = []
        self._stderr_lock = threading.Lock()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            with self._stderr_lock:
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > 120:
                    self._stderr_tail = self._stderr_tail[-120:]

    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail[-20:])

    def analyze_position(
        self,
        size: int,
        komi: float,
        moves: List[List[str]],
        initial_stones: List[Tuple[str, str]],
    ) -> Dict[str, Any]:
        request_id = str(self._next_id)
        self._next_id += 1

        req: Dict[str, Any] = {
            "id": request_id,
            "rules": self.rules,
            "komi": komi,
            "boardXSize": size,
            "boardYSize": size,
            "maxVisits": self.max_visits,
            "moves": moves,
        }
        if initial_stones:
            req["initialStones"] = [[c, m] for c, m in initial_stones]

        payload = json.dumps(req, ensure_ascii=False)
        assert self.proc.stdin is not None
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()

        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        deadline = time.time() + self.timeout_sec
        while True:
            if self.proc.poll() is not None:
                tail = self.stderr_tail()
                raise RuntimeError(
                    f"KataGo提前退出，exit={self.proc.returncode}\n{tail}"
                )

            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError(
                    f"KataGo分析超时（{self.timeout_sec}s）。\n最近stderr:\n{self.stderr_tail()}"
                )
            ready, _, _ = select.select([fd], [], [], remain)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if str(data.get("id")) != request_id:
                continue
            if data.get("isDuringSearch"):
                continue
            if "error" in data:
                raise RuntimeError(f"KataGo返回错误: {data['error']}")
            return data

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)

    def __enter__(self) -> "KataGoAnalyzer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def analyze_game(
    game: GameRecord,
    student_color: str,
    min_drop: float,
    analyzer: KataGoAnalyzer,
) -> GameReview:
    prefix_moves: List[List[str]] = []
    issues: List[MoveIssue] = []

    phase_move_counts = {phase: 0 for phase, _ in PHASES}
    phase_issue_counts = {phase: 0 for phase, _ in PHASES}
    context_issue_counts = {label: 0 for label in CONTEXT_LABELS}
    zone_issue_counts = {label: 0 for label in ZONE_LABELS}

    student_moves = 0

    for idx, move in enumerate(game.moves, start=1):
        to_analyze = student_color == "both" or move.color == student_color
        phase = phase_for_move(idx)
        if to_analyze:
            phase_move_counts[phase] += 1
            if move.gtp_coord != "pass":
                student_moves += 1

        if not to_analyze:
            prefix_moves.append([move.color, move.gtp_coord])
            continue
        if move.gtp_coord == "pass":
            prefix_moves.append([move.color, move.gtp_coord])
            continue

        before = analyzer.analyze_position(
            size=game.size,
            komi=game.komi,
            moves=prefix_moves,
            initial_stones=game.initial_stones,
        )
        root_before = before.get("rootInfo", {})
        rbw = root_before.get("winrate")
        before_wr = float(rbw) if isinstance(rbw, (int, float)) else None
        move_infos = before.get("moveInfos", [])
        if not move_infos:
            prefix_moves.append([move.color, move.gtp_coord])
            continue

        best = move_infos[0]
        best_move = best.get("move", "pass")
        best_wr = best.get("winrate")
        best_lead = best.get("scoreLead")
        if not isinstance(best_wr, (int, float)):
            prefix_moves.append([move.color, move.gtp_coord])
            continue

        actual_info = next(
            (info for info in move_infos if info.get("move") == move.gtp_coord),
            None,
        )
        actual_wr: Optional[float] = None
        actual_lead: Optional[float] = None

        if actual_info is not None:
            aw = actual_info.get("winrate")
            if isinstance(aw, (int, float)):
                actual_wr = float(aw)
            al = actual_info.get("scoreLead")
            if isinstance(al, (int, float)):
                actual_lead = float(al)
        else:
            after = analyzer.analyze_position(
                size=game.size,
                komi=game.komi,
                moves=prefix_moves + [[move.color, move.gtp_coord]],
                initial_stones=game.initial_stones,
            )
            root_after = after.get("rootInfo", {})
            rw = root_after.get("winrate")
            if isinstance(rw, (int, float)):
                actual_wr = 1.0 - float(rw)
            rl = root_after.get("scoreLead")
            if isinstance(rl, (int, float)):
                actual_lead = -float(rl)

        if actual_wr is None:
            prefix_moves.append([move.color, move.gtp_coord])
            continue

        drop = float(best_wr) - float(actual_wr)
        if drop >= min_drop:
            score_drop: Optional[float] = None
            if isinstance(best_lead, (int, float)) and actual_lead is not None:
                score_drop = float(best_lead) - actual_lead

            context = context_for_before_wr(before_wr)
            zone = zone_for_gtp(move.gtp_coord, game.size)
            issue = MoveIssue(
                move_no=idx,
                color=move.color,
                actual=move.gtp_coord,
                best=best_move,
                pv=(best.get("pv", [])[:8] if isinstance(best.get("pv"), list) else []),
                winrate_drop=drop,
                score_drop=score_drop,
                phase=phase,
                severity=severity_for_drop(drop),
                before_winrate=before_wr,
                context=context,
                zone=zone,
            )
            issues.append(issue)
            phase_issue_counts[phase] += 1
            context_issue_counts[context] = context_issue_counts.get(context, 0) + 1
            zone_issue_counts[zone] = zone_issue_counts.get(zone, 0) + 1

        prefix_moves.append([move.color, move.gtp_coord])

    severe = sum(1 for i in issues if i.severity == "严重")
    major = sum(1 for i in issues if i.severity == "较大")
    minor = sum(1 for i in issues if i.severity == "一般")

    issue_count = len(issues)
    issue_rate = issue_count / max(student_moves, 1)
    avg_drop = statistics.mean([i.winrate_drop for i in issues]) if issues else 0.0

    return GameReview(
        game_path=game.path,
        black=game.black,
        white=game.white,
        result=game.result,
        date=game.date,
        student_color=student_color,
        student_moves=student_moves,
        issue_count=issue_count,
        issue_rate=issue_rate,
        severe_count=severe,
        major_count=major,
        minor_count=minor,
        avg_drop=avg_drop,
        phase_move_counts=phase_move_counts,
        phase_issue_counts=phase_issue_counts,
        context_issue_counts=context_issue_counts,
        zone_issue_counts=zone_issue_counts,
        issues=issues,
    )


def aggregate_reviews(reviews: List[GameReview]) -> Dict[str, Any]:
    total_games = len(reviews)
    total_moves = sum(r.student_moves for r in reviews)
    total_issues = sum(r.issue_count for r in reviews)
    severe_total = sum(r.severe_count for r in reviews)
    major_total = sum(r.major_count for r in reviews)
    minor_total = sum(r.minor_count for r in reviews)

    phase_stats: Dict[str, Dict[str, float]] = {}
    for phase, _ in PHASES:
        phase_moves = sum(r.phase_move_counts.get(phase, 0) for r in reviews)
        phase_issues = sum(r.phase_issue_counts.get(phase, 0) for r in reviews)
        issue_rate = phase_issues / max(phase_moves, 1)
        phase_stats[phase] = {
            "moves": phase_moves,
            "issues": phase_issues,
            "issue_rate": issue_rate,
        }

    context_counts: Dict[str, int] = {label: 0 for label in CONTEXT_LABELS}
    zone_counts: Dict[str, int] = {label: 0 for label in ZONE_LABELS}
    for review in reviews:
        for label, n in review.context_issue_counts.items():
            context_counts[label] = context_counts.get(label, 0) + int(n)
        for label, n in review.zone_issue_counts.items():
            zone_counts[label] = zone_counts.get(label, 0) + int(n)

    context_stats: Dict[str, Dict[str, float]] = {}
    for label, n in context_counts.items():
        context_stats[label] = {
            "issues": n,
            "share": n / max(total_issues, 1),
        }

    zone_stats: Dict[str, Dict[str, float]] = {}
    for label, n in zone_counts.items():
        zone_stats[label] = {
            "issues": n,
            "share": n / max(total_issues, 1),
        }

    all_issues: List[Tuple[GameReview, MoveIssue]] = []
    for review in reviews:
        for issue in review.issues:
            all_issues.append((review, issue))
    all_issues.sort(key=lambda x: x[1].winrate_drop, reverse=True)

    raw_top_issues = [
        {
            "game": Path(review.game_path).name,
            "move_no": issue.move_no,
            "actual": issue.actual,
            "best": issue.best,
            "phase": issue.phase,
            "severity": issue.severity,
            "winrate_drop": issue.winrate_drop,
            "score_drop": issue.score_drop,
            "before_winrate": issue.before_winrate,
            "context": issue.context,
            "zone": issue.zone,
            "pv": issue.pv,
        }
        for review, issue in all_issues[:80]
    ]

    # Merge consecutive mistake bursts within each game into a single turning point.
    turning_points: List[Dict[str, Any]] = []
    by_game: Dict[str, List[MoveIssue]] = {}
    for review in reviews:
        game_name = Path(review.game_path).name
        by_game[game_name] = sorted(review.issues, key=lambda i: i.move_no)
    for game_name, issues in by_game.items():
        if not issues:
            continue
        cluster: List[MoveIssue] = [issues[0]]
        for issue in issues[1:]:
            if issue.move_no - cluster[-1].move_no <= 4:
                cluster.append(issue)
            else:
                pivot = max(cluster, key=lambda i: i.winrate_drop)
                turning_points.append(
                    {
                        "game": game_name,
                        "move_no": pivot.move_no,
                        "actual": pivot.actual,
                        "best": pivot.best,
                        "phase": pivot.phase,
                        "severity": pivot.severity,
                        "winrate_drop": pivot.winrate_drop,
                        "score_drop": pivot.score_drop,
                        "before_winrate": pivot.before_winrate,
                        "context": pivot.context,
                        "zone": pivot.zone,
                        "pv": pivot.pv,
                        "cluster_size": len(cluster),
                    }
                )
                cluster = [issue]
        pivot = max(cluster, key=lambda i: i.winrate_drop)
        turning_points.append(
            {
                "game": game_name,
                "move_no": pivot.move_no,
                "actual": pivot.actual,
                "best": pivot.best,
                "phase": pivot.phase,
                "severity": pivot.severity,
                "winrate_drop": pivot.winrate_drop,
                "score_drop": pivot.score_drop,
                "before_winrate": pivot.before_winrate,
                "context": pivot.context,
                "zone": pivot.zone,
                "pv": pivot.pv,
                "cluster_size": len(cluster),
            }
        )
    turning_points.sort(key=lambda x: x["winrate_drop"], reverse=True)
    turning_points = turning_points[:20]

    weakness_signals: List[str] = []
    if total_issues > 0:
        phase_order = sorted(
            phase_stats.items(), key=lambda kv: kv[1]["issue_rate"], reverse=True
        )
        top_phase, top_phase_stat = phase_order[0]
        if top_phase_stat["issue_rate"] >= 0.20:
            weakness_signals.append(
                f"{top_phase}阶段问题手率较高（{top_phase_stat['issue_rate']*100:.1f}%）"
            )

        severe_rate = severe_total / total_issues
        if severe_rate >= 0.25:
            weakness_signals.append(
                f"严重失误占比较高（{severe_rate*100:.1f}%），需优先降低单步大亏损"
            )

        if phase_stats["官子"]["issue_rate"] >= 0.15:
            weakness_signals.append("官子稳定性偏弱，后半盘容易被逆转")
        if phase_stats["布局"]["issue_rate"] >= 0.15:
            weakness_signals.append("布局阶段方向感有待加强")
        if phase_stats["中盘"]["issue_rate"] >= 0.18:
            weakness_signals.append("中盘战斗判断波动较大")

        if context_stats["优势失误"]["share"] >= 0.35:
            weakness_signals.append("领先后守成稳定性不足，优势局面有回送倾向")
        if context_stats["均势失误"]["share"] >= 0.45:
            weakness_signals.append("均势关键点判断波动较大，胜负手把握不足")
        if context_stats["逆风失误"]["share"] >= 0.35:
            weakness_signals.append("逆风局韧性不足，追赶阶段容易继续扩大损失")

        if zone_stats["边上"]["share"] >= 0.40:
            weakness_signals.append("边上攻防转换处理偏弱，易出现薄味或次序问题")
        if zone_stats["角部"]["share"] >= 0.35:
            weakness_signals.append("角部定型与定式后续衔接有改进空间")

    summary = {
        "overall": {
            "games": total_games,
            "student_moves": total_moves,
            "issues": total_issues,
            "issue_rate": total_issues / max(total_moves, 1),
            "severe": severe_total,
            "major": major_total,
            "minor": minor_total,
        },
        "phase_stats": phase_stats,
        "context_stats": context_stats,
        "zone_stats": zone_stats,
        "weakness_signals": weakness_signals,
        "top_issues": raw_top_issues[:20],
        "turning_points": turning_points,
    }
    return summary


def fallback_advice(summary: Dict[str, Any]) -> str:
    overall = summary["overall"]
    phases = summary["phase_stats"]
    contexts = summary.get("context_stats", {})
    zones = summary.get("zone_stats", {})
    weakness = summary["weakness_signals"]

    lines: List[str] = []
    lines.append("### 核心问题")
    if weakness:
        for w in weakness[:4]:
            lines.append(f"- {w}")
    else:
        lines.append("- 样本中未出现单一明显短板，建议继续按阶段复盘。")

    lines.append("")
    lines.append("### 两周训练计划（每天60分钟）")
    lines.append("- 20分钟：死活/手筋（优先做边角攻防和弃取判断题）。")
    lines.append("- 20分钟：布局方向 + 中盘计算题（每题先写下候选点与理由）。")
    lines.append("- 20分钟：官子价值计算与收束（每盘复盘至少核对5个官子次序）。")
    if contexts.get("优势失误", {}).get("share", 0.0) >= 0.35:
        lines.append("- 每天加10分钟：领先简化训练（优先守空/补薄，不主动开新战场）。")
    if contexts.get("逆风失误", {}).get("share", 0.0) >= 0.35:
        lines.append("- 每天加10分钟：逆风追赶训练（先抢最大官子与先手，不赌一步翻盘）。")
    if zones.get("边上", {}).get("share", 0.0) >= 0.40:
        lines.append("- 每天加10分钟：边上定型专题（厚薄判断、先后手次序、连接与腾挪）。")

    lines.append("")
    lines.append("### 对局复盘清单")
    lines.append("- 每盘只抓3个关键转折点（去除连续崩盘重复手），记录“当时想法 vs KataGo推荐”。")
    lines.append("- 对每个问题手补1个“可执行替代原则”，如下次优先补厚/先手。")
    lines.append("- 次日快速复盘前一天错误点，检查是否重复。")

    lines.append("")
    lines.append("### 量化目标")
    lines.append(
        f"- 将问题手率从 {overall['issue_rate']*100:.1f}% 降到 {(overall['issue_rate']*100*0.8):.1f}% 左右。"
    )
    lines.append(f"- 将严重失误次数从 {overall['severe']} 次减少至少 30%。")
    lines.append(
        f"- 当前阶段问题率：布局 {phases['布局']['issue_rate']*100:.1f}% / 中盘 {phases['中盘']['issue_rate']*100:.1f}% / 官子 {phases['官子']['issue_rate']*100:.1f}%。"
    )
    if contexts:
        lines.append(
            f"- 局势失误结构：优势 {contexts.get('优势失误', {}).get('share', 0.0)*100:.1f}% / 均势 {contexts.get('均势失误', {}).get('share', 0.0)*100:.1f}% / 逆风 {contexts.get('逆风失误', {}).get('share', 0.0)*100:.1f}%。"
        )
    return "\n".join(lines)


def generate_llm_advice(summary: Dict[str, Any], args: argparse.Namespace) -> Optional[str]:
    if args.llm_provider != "openai":
        return None

    api_key = args.openai_api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None

    system_prompt = (
        "你是一名围棋职业教练。请基于KataGo统计结果，用中文输出："
        "1)核心问题(最多4条) 2)两周训练计划(可执行, 按天) "
        "3)赛后复盘模板 4)下一阶段量化目标。"
    )
    user_payload = {
        "overall": summary["overall"],
        "phase_stats": summary["phase_stats"],
        "context_stats": summary.get("context_stats", {}),
        "zone_stats": summary.get("zone_stats", {}),
        "weakness_signals": summary["weakness_signals"],
        "turning_points": summary.get("turning_points", [])[:12],
    }
    user_prompt = (
        "以下是学生最近一段时间野狐棋谱的KataGo统计，请给出教学建议：\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )

    body = {
        "model": args.llm_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    url = args.llm_base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        choices = data.get("choices", [])
        if not choices:
            return None
        msg = choices[0].get("message", {})
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        return None
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None


def render_markdown_report(
    reviews: List[GameReview],
    summary: Dict[str, Any],
    coaching: str,
) -> str:
    overall = summary["overall"]
    phase_stats = summary["phase_stats"]
    context_stats = summary.get("context_stats", {})
    zone_stats = summary.get("zone_stats", {})
    top_issues = summary.get("top_issues", [])
    turning_points = summary.get("turning_points", [])
    weakness = summary["weakness_signals"]

    lines: List[str] = []
    lines.append("# 围棋复盘报告（KataGo + LLM）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 分析对局：{overall['games']} 局")
    lines.append(f"- 学生落子：{overall['student_moves']} 手")
    lines.append(
        f"- 问题手：{overall['issues']} 手（{overall['issue_rate']*100:.1f}%）"
    )
    lines.append(
        f"- 严重/较大/一般：{overall['severe']}/{overall['major']}/{overall['minor']}"
    )
    lines.append("")

    lines.append("## 阶段统计")
    for phase in ["布局", "中盘", "官子"]:
        stat = phase_stats[phase]
        lines.append(
            f"- {phase}：{int(stat['issues'])}/{int(stat['moves'])} "
            f"（问题率 {stat['issue_rate']*100:.1f}%）"
        )
    lines.append("")

    if context_stats:
        lines.append("## 局势上下文分布")
        for key in ["优势失误", "均势失误", "逆风失误", "未知局势"]:
            stat = context_stats.get(key, {"issues": 0, "share": 0.0})
            lines.append(
                f"- {key}：{int(stat['issues'])} 手（占比 {stat['share']*100:.1f}%）"
            )
        lines.append("")

    if zone_stats:
        lines.append("## 落子区域分布")
        for key in ["角部", "边上", "中腹", "未知"]:
            stat = zone_stats.get(key, {"issues": 0, "share": 0.0})
            lines.append(
                f"- {key}：{int(stat['issues'])} 手（占比 {stat['share']*100:.1f}%）"
            )
        lines.append("")

    lines.append("## 主要风险信号")
    if weakness:
        for item in weakness:
            lines.append(f"- {item}")
    else:
        lines.append("- 暂未出现明显单一短板。")
    lines.append("")

    lines.append("## 关键转折点（去重后 Top 10）")
    source = turning_points if turning_points else top_issues
    if source:
        for item in source[:10]:
            drop_pct = item["winrate_drop"] * 100
            if item["score_drop"] is None:
                lead = "N/A"
            else:
                lead = f"{item['score_drop']:+.2f}"
            context = item.get("context", "未知局势")
            zone = item.get("zone", "未知")
            lines.append(
                f"- {item['game']} 手数{item['move_no']} "
                f"{item['actual']} -> 推荐 {item['best']} | "
                f"胜率损失 {drop_pct:.1f}% | 目数变化 {lead} | {context}/{zone}"
            )
    else:
        lines.append("- 无达到阈值的问题手。")
    lines.append("")

    lines.append("## 分局摘要")
    for review in reviews:
        lines.append(
            f"- {Path(review.game_path).name} | "
            f"{review.black} vs {review.white} | "
            f"问题手 {review.issue_count}/{review.student_moves} "
            f"（{review.issue_rate*100:.1f}%）"
        )
    lines.append("")

    lines.append("## 教学建议")
    lines.append(coaching.strip())
    lines.append("")
    return "\n".join(lines)


def write_outputs(
    reviews: List[GameReview],
    summary: Dict[str, Any],
    report_md: str,
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    games_dir = out_dir / "games"
    games_dir.mkdir(parents=True, exist_ok=True)

    for review in reviews:
        file_name = Path(review.game_path).stem + ".json"
        payload = asdict(review)
        payload["issues"] = [asdict(i) for i in review.issues]
        (games_dir / file_name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "student_review_report.md").write_text(report_md, encoding="utf-8")


def validate_args(args: argparse.Namespace) -> None:
    if not args.sgf_dir.exists():
        raise FileNotFoundError(f"SGF目录不存在: {args.sgf_dir}")
    for p in [args.katago_bin, args.katago_config, args.katago_model]:
        if not p.exists():
            raise FileNotFoundError(f"路径不存在: {p}")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except Exception as exc:
        print(f"[错误] 参数校验失败: {exc}", file=sys.stderr)
        return 2

    sgf_files = list_recent_sgfs(args.sgf_dir, args.recent_days, args.max_games)
    if not sgf_files:
        print("[错误] 没找到可分析的SGF文件", file=sys.stderr)
        return 2

    games: List[GameRecord] = []
    for sgf in sgf_files:
        try:
            game = parse_sgf(sgf)
            if game.moves:
                games.append(game)
        except Exception as exc:
            print(f"[警告] 跳过解析失败文件 {sgf}: {exc}", file=sys.stderr)
    if not games:
        print("[错误] SGF解析后没有有效对局", file=sys.stderr)
        return 2

    print(f"[信息] 准备分析 {len(games)} 局对局", file=sys.stderr)
    reviews: List[GameReview] = []
    try:
        with KataGoAnalyzer(
            bin_path=args.katago_bin,
            config_path=args.katago_config,
            model_path=args.katago_model,
            analysis_threads=args.analysis_threads,
            max_visits=args.max_visits,
            rules=args.rules,
            timeout_sec=args.timeout_sec,
        ) as analyzer:
            for i, game in enumerate(games, start=1):
                student_color = infer_student_color(
                    game=game,
                    forced_mode=args.student_color,
                    player_name=args.player_name,
                )
                print(
                    f"[信息] ({i}/{len(games)}) 分析 {Path(game.path).name}, 学生执子={student_color}",
                    file=sys.stderr,
                )
                review = analyze_game(
                    game=game,
                    student_color=student_color,
                    min_drop=args.min_winrate_drop,
                    analyzer=analyzer,
                )
                reviews.append(review)
    except Exception as exc:
        print(f"[错误] KataGo分析失败: {exc}", file=sys.stderr)
        return 3

    summary = aggregate_reviews(reviews)
    llm_text = generate_llm_advice(summary, args)
    coaching = llm_text if llm_text else fallback_advice(summary)
    report_md = render_markdown_report(reviews, summary, coaching)
    write_outputs(reviews, summary, report_md, args.out_dir)

    print(f"[完成] 报告已生成: {args.out_dir / 'student_review_report.md'}", file=sys.stderr)
    print(f"[完成] 结构化结果: {args.out_dir / 'summary.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
