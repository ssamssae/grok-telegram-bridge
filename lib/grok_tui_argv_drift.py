#!/usr/bin/env python3
"""이미 떠 있는 grok TUI 세션의 argv 가 ★지금 조립될 argv 와 갈렸는지 판정한다 (T-260822-005).

뿌리 (제어 노드 2026-08-22 00:5x 실측): T-260821-039 로 도구 차단축을 걷고 PR#1902 를 머지했는데,
여러 노드의 TUI 세션은 8/19 에 뜬 옛 프로세스 그대로였고 argv 에
`--deny Bash …`가 그대로 붙어 있었다. 런처는 멱등이라 「이미 떠 있다」+rc=0 을 냈고, 브릿지만
새 코드로 재시작하면 배너·유닛 active·머지 완료 ★3중 초록인데 도구만 조용히 안 먹었다.
★실패가 성공과 같은 모습을 한다 — 사람이 ps 로 argv 를 대조해야만 보였다.

판정 축은 ★의미 있는 것만 본다. 문자열 전량 일치로 재면 `--session-id` 처럼 매번 달라지는
값 때문에 상시 경고가 나고, 상시 경고는 곧 무시된다(그러면 이 도구가 원래 결함으로 되돌아간다).

  보는 축:  cwd · deny 목록 · no-subagents · disable-web-search · permission-mode · model
            · rules 지문(sha256 앞 12자 — 원문은 길고 따옴표 처리가 양쪽에서 달라 지문으로 잰다)
  안 보는 축: session-id (신규 대화 예약값이라 매 기동 달라진다)

exit: 0 = 동일 · 1 = 드리프트(축 목록을 stdout 에 출력) · 2 = 판정 불가/사용법 오류
"""

from __future__ import annotations

import argparse
import hashlib
import shlex
import sys

VOLATILE_FLAGS = {"--session-id"}
VALUE_FLAGS = {
    "--cwd", "--session-id", "--rules", "--permission-mode", "-m", "--model", "--deny",
}


def parse_axes(argv: list[str]) -> dict[str, object]:
    """argv 를 판정 축 사전으로 접는다. 모르는 플래그는 flags 집합에 그대로 담는다."""
    axes: dict[str, object] = {"cwd": None, "deny": [], "permission_mode": None,
                               "model": None, "rules": None, "flags": set()}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in VALUE_FLAGS:
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if token == "--cwd":
                axes["cwd"] = value
            elif token == "--deny":
                axes["deny"].append(value)          # type: ignore[union-attr]
            elif token == "--rules":
                axes["rules"] = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            elif token == "--permission-mode":
                axes["permission_mode"] = value
            elif token in ("-m", "--model"):
                axes["model"] = value
            # session-id 는 일부러 버린다 (VOLATILE_FLAGS)
            i += 2
            continue
        if token.startswith("-") and token not in VOLATILE_FLAGS:
            axes["flags"].add(token)                # type: ignore[union-attr]
        i += 1
    axes["deny"] = sorted(axes["deny"])             # type: ignore[arg-type]
    return axes


def describe(name: str, running, desired) -> str:
    return f"  {name}: 실행중={running!r}  조립될={desired!r}"


def diff_axes(running: dict[str, object], desired: dict[str, object]) -> list[str]:
    out: list[str] = []
    for key, label in (("cwd", "cwd"), ("permission_mode", "permission-mode"),
                       ("model", "model"), ("rules", "rules 지문")):
        if running[key] != desired[key]:
            out.append(describe(label, running[key], desired[key]))
    if running["deny"] != desired["deny"]:
        out.append(describe("deny 목록", running["deny"], desired["deny"]))
    r_flags, d_flags = running["flags"], desired["flags"]      # type: ignore[assignment]
    only_running = sorted(r_flags - d_flags)                   # type: ignore[operator]
    only_desired = sorted(d_flags - r_flags)                   # type: ignore[operator]
    if only_running:
        out.append(f"  실행중에만 있는 플래그: {' '.join(only_running)}")
    if only_desired:
        out.append(f"  조립될 쪽에만 있는 플래그: {' '.join(only_desired)}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--running", required=True,
                        help="실행 중 세션의 명령줄 문자열 (tmux pane_start_command 등). '-' 면 stdin")
    parser.add_argument("desired", nargs=argparse.REMAINDER,
                        help="'--' 뒤에 지금 조립될 argv 를 ★배열 그대로 넘긴다")
    args = parser.parse_args()

    running_raw = sys.stdin.read() if args.running == "-" else args.running
    running_raw = running_raw.strip()
    if not running_raw:
        print("ARGV_DRIFT UNKNOWN reason=running-argv-empty", file=sys.stderr)
        return 2

    desired = [a for a in args.desired if a != "--"]
    if not desired:
        print("ARGV_DRIFT UNKNOWN reason=desired-argv-empty", file=sys.stderr)
        return 2

    try:
        running = shlex.split(running_raw)
    except ValueError as exc:
        print(f"ARGV_DRIFT UNKNOWN reason=running-argv-unparsable detail={exc}", file=sys.stderr)
        return 2

    diffs = diff_axes(parse_axes(running), parse_axes(desired))
    if not diffs:
        print("ARGV_DRIFT NONE")
        return 0
    print(f"ARGV_DRIFT FOUND axes={len(diffs)}")
    for line in diffs:
        print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
