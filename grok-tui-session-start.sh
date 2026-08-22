#!/usr/bin/env bash
# grok-tui-session-start.sh — grb TUI 레인의 상주 그록 세션 기동면 (T-260819-028)
#
# 사용자 지시 「그록 텔레그램 브릿지를 티먹스 터미널 CLI 에만 동기화시키자」(2026-08-19 22:30).
# grb 가 붙을 ★보이는 그록 TUI 를 tmux 안에 하나 세우고, 그 세션의 uuid 를 상태파일에
# 적어 브릿지가 어느 chat_history.jsonl 을 읽을지 알 수 있게 한다.
#
# ★uuid 를 고정하는 이유 = 세션 디렉토리가 `~/.grok/sessions/<urlquote(cwd)>/<uuid>/` 라서
#   uuid 를 모르면 어느 파일을 읽을지 계산할 수 없다. `--session-id` 는 ★신규 대화의 id 를
#   지정하는 플래그다(grok --help: "for a **new** conversation ... Does not resume").
#
# ★차단축이 헤드리스와 다르다 — README 명시: `--tools`/`--disallowed-tools`/`--max-turns` 는
#   헤드리스(-p) 전용이고 TUI 에서는 경고만 찍히고 무시된다. 그래서 여기서는 퍼미션 룰
#   (`--allow`/`--deny`)로 막는다 — README: "These flags work in both TUI and headless mode".
#   ⚠️ 성질이 다르다: 퍼미션 룰은 도구를 ★제거하지 않고 실행만 거부한다. 그래서 모델이
#   도구를 시도하는 것 자체는 날 수 있고, 브릿지는 그 시도를 fail-loud 로 남긴다.
#
# ⚠️ `--permission-mode` 는 기본으로 안 싣는다. `grok --help` 는 값 목록만 주고
#   (default/acceptEdits/auto/dontAsk/bypassPermissions/plan) README 는 각 값의 의미를
#   설명하지 않는다. 이름만 보고 고르면 두 갈래로 다 틀릴 수 있다 — 묻는 모드면 무인 TUI 가
#   멎고(지금 고치는 증상), 전면승인 모드면 deny 룰 밖 도구가 자동 실행된다.
#   배선은 해뒀으니 의미가 실측되면 `GRB_TUI_PERMISSION_MODE=<mode>` 로 켠다.
#
# ★운영 주의 (제어 노드 라이브 실측 2026-08-19 23:0x): `launchctl kickstart -k` 는 plist 의
#   EnvironmentVariables 를 ★재독하지 않는다. 레인 전환처럼 env 를 바꿨으면
#   기동면을 서비스로 돌리고 있다면 그 서비스를 재기동해야 반영된다.
#   kickstart 만 하고 "안 먹는다" 로 진단하면 엉뚱한 곳을 판다.
#
# 사용:
#   grok-tui-session-start.sh              # 없으면 기동, 있으면 그대로 둔다(멱등)
#   grok-tui-session-start.sh --print-cmd  # 조립될 grok argv 만 출력 (tmux 미접촉)
#   grok-tui-session-start.sh --print-cmd0 # 같은 argv 를 ★NUL 구분으로 (인자 경계 보존)
#   grok-tui-session-start.sh --print-name # 해석된 GRB_NAME 만 출력 (상태파일 축 진단용)
#   grok-tui-session-start.sh --force      # 기존 세션을 죽이고 새로 기동
#
# 종료코드 (T-260822-005 — 호출자가 「정상 멱등」과 「낡은 세션」을 구분해야 한다):
#   0  = 기동했거나, 이미 떠 있고 argv 도 일치한다(또는 판정 불가 — 그때는 경고를 찍는다)
#   1  = tmux new-session 실패
#   2  = 사용법·환경 오류 (노드 이름 미해석 등 fail-closed)
#   9  = ★이미 떠 있는데 argv 가 낡았다. 성공이 아니다 — --force 로 다시 세워야 한다.
#        (GRB_TUI_DRIFT_EXIT 로 바꿀 수 있다. 0 으로 두면 종전처럼 조용해지니 권하지 않는다.)
set -uo pipefail

# ── ① 이름축 — 브릿지와 ★같은 이름을 쓴다 ─────────────────────────────────
#   기동면이 쓴 상태파일을 브릿지가 되읽는다. 두 쪽 이름이 갈리면 같은 기계에서도
#   서로 다른 파일을 보고 fail-closed 로 막힌다 — 그래서 기본값을 공유한다.
GRB_LIB_DIR="$(cd "$(dirname "$0")" && pwd)"
NAME="${GRB_NAME:-grok}"
DRIFT_EXIT="${GRB_TUI_DRIFT_EXIT:-9}"
STATE_DIR="${GRB_STATE_DIR:-$HOME/.grok-telegram-bridge/state}"
GROK_BIN="${GRB_GROK_BIN:-grok}"
TMUX_BIN="${GRB_TMUX_BIN:-tmux}"
TMUX_SOCKET="${GRB_TMUX_SOCKET:-grok}"
TMUX_SESSION="${GRB_TMUX_SESSION:-grok}"
# T-260822-047 — --force 경로 전용 노브. 기본값은 「사람이 못 느끼는 상한」으로 잡았다.
NEW_SESSION_TRIES="${GRB_TUI_NEW_SESSION_TRIES:-3}"
NEW_SESSION_BACKOFF="${GRB_TUI_NEW_SESSION_BACKOFF:-0.2}"
SESSION_ID_FILE="$STATE_DIR/grok-tui-$NAME.session-id"
CWD_FILE="$STATE_DIR/grok-tui-$NAME.cwd"

# ★작업디렉토리는 브릿지와 ★같은 식으로 구한다. 다르면 세션 디렉토리 경로가 갈려
#   브릿지가 영영 빈 파일을 읽는다 — 그래서 문자열로 흉내내지 않고 브릿지가 쓰는
#   tempfile.gettempdir() 를 그대로 부른다(TMPDIR 해석까지 동일해진다).
# ── ② cwd 심링크 정규화 (T-260819-028 제어 노드 실측 ②) ───────────────────────────
#   그록 TUI 는 cwd 를 ★realpath 로 정규화해 세션 키를 만든다. macOS 의 /tmp 는
#   /private/tmp 심링크라, 문자열 그대로 quote 하면 브릿지는 `%2Ftmp%2F…` 를 읽고
#   TUI 는 `%2Fprivate%2Ftmp%2F…` 에 쓴다 — 「답은 화면에 44s 만에 났는데 브릿지는
#   180s 무응답」이 그 갈림이었다(실측 2차 왕복, 세션 디렉토리 대조로 확정).
#   그래서 양쪽 다 ★정규화 후 quote 한다. 여기서도 브릿지와 같은 함수(os.path.realpath)를
#   쓴다 — 셸 realpath(1) 는 macOS 기본 셸에 없을 수 있다.
CHAT_CWD_RAW="${GRB_GROK_CHAT_CWD:-}"
#   ★베이스는 고정 경로다(T-260819-028 ⑤). tempfile.gettempdir() 는 TMPDIR 을 읽어
#   launchd 브릿지(/var/folders/…/T)와 사람 셸(/tmp)이 갈린다 — 두 쪽이 합의해야 하는
#   경로라 환경 의존값을 쓰면 안 된다. 브릿지와 ★같은 기본값·같은 env 이름을 쓴다.
CHAT_CWD="$(GRB_NAME="$NAME" GRB_GROK_CHAT_CWD="$CHAT_CWD_RAW" GRB_CHAT_CWD_BASE="${GRB_CHAT_CWD_BASE:-/tmp}" python3 -c '
import os
# T-260821-039: 기본값을 브릿지와 함께 $HOME 으로 옮긴다(작업 레인). 두 파일이 ★글자
#   그대로 같은 식이어야 같은 경로가 나온다 — 이 합치는
#   test_launcher_and_bridge_agree_under_different_tmpdirs 가 잠근다.
#   되돌릴 때도 양쪽 공통 env 하나(GRB_GROK_CHAT_CWD)로 끝난다.
raw = os.environ.get("GRB_GROK_CHAT_CWD") or os.path.expanduser("~")
raw = os.path.expanduser(os.path.expandvars(raw))
os.makedirs(raw, exist_ok=True)
print(os.path.realpath(raw))
')" || { echo "chat cwd 계산 실패 — python3 가 필요하다" >&2; exit 2; }

# 즉답 계약. 브릿지 헤드리스 레인과 ★같은 env 를 읽는다 — 한 곳만 고치면 두 레인이 같이 움직인다.
# T-260821-039: 종전 룰은 「도구 실행은 퍼미션 룰로 거부돼 있다 … 조사하지 말고 곧바로
#   답해라」였다. 도구를 열어놓고 이 문장을 남겨두면 프롬프트가 도구를 안 쓰게 만들어
#   ★플래그만 열리고 손발은 그대로 묶인 상태가 된다(차단축이 코드에서 프롬프트로 옮겨갈 뿐).
#   그래서 작업 레인 룰로 교체한다. 복원은 이 기본값 문자열을 되돌리거나 실행 중
#   GRB_GROK_CHAT_RULES 로 덮으면 된다(코드 수정 불요).
# T-260822-041 — 사용자 지시(2026-08-22 12:08): 그록 위에 씌운 층을 벗기고 순정으로 넘긴다.
#   기본을 ★빈 문자열로 둔다 = 순정 그록(지시문 주입 0). 되살리려면 코드 수정 없이
#   GRB_GROK_CHAT_RULES 에 문구를 넣으면 된다. `:-` 가 아니라 `-` 인 이유 = 명시적
#   빈 값을 「미설정」으로 되돌려 기본값을 되살리지 않기 위해서다.
CHAT_RULES="${GRB_GROK_CHAT_RULES-}"

MODE="run"
case "${1:-}" in
  --print-cmd)  MODE="print" ;;
  --print-cmd0) MODE="print0" ;;
  --print-name) MODE="name" ;;
  --force)      MODE="force" ;;
  "")           MODE="run" ;;
  *) echo "usage: $(basename "$0") [--print-cmd|--print-name|--force]" >&2; exit 2 ;;
esac

if [ "$MODE" = "name" ]; then
  printf '%s\n' "$NAME"
  exit 0
fi

mkdir -p "$STATE_DIR" "$CHAT_CWD" || exit 2

session_alive() {
  "$TMUX_BIN" -L "$TMUX_SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null
}

# ── ④ 멱등 판정은 ★상태파일이 아니라 tmux 세션 실존이다 (제어 노드 실측 ④) ──────────
#   종전엔 상태파일에 uuid 가 있으면 그걸 그대로 --session-id 로 다시 넣었다. 그런데
#   `--session-id` 는 ★신규 대화 전용이다(grok --help: "Does not resume existing
#   sessions"). 세션이 죽고 상태파일만 남은 조합에서 재기동하면 이미 존재하는 id 를
#   신규로 예약하려 들어 grok 이 즉사하고 tmux 서버째 사라졌다("no server running" 실측)
#   → 다음 왕복이 180s 무응답. 그래서 ★세션을 새로 세울 때는 uuid 도 새로 발급한다.
resolve_session_id() {
  # 명시 주입은 그대로 존중한다(테스트·운영자 의도).
  if [ -n "${GRB_TUI_SESSION_ID:-}" ]; then printf '%s' "$GRB_TUI_SESSION_ID"; return 0; fi
  # 세션이 살아 있으면 상태파일 uuid 가 ★그 세션의 키다 — 재사용이 맞다.
  if [ "$MODE" != "force" ] && session_alive && [ -f "$SESSION_ID_FILE" ]; then
    local sid; sid="$(tr -d '[:space:]' < "$SESSION_ID_FILE" 2>/dev/null)"
    if [ -n "$sid" ]; then printf '%s' "$sid"; return 0; fi
  fi
  python3 -c 'import uuid;print(uuid.uuid4())'
}

SESSION_ID="$(resolve_session_id)" || { echo "session uuid 생성 실패" >&2; exit 2; }

# ── T-260821-039: 도구 차단축 폐기 (사용자 직접 지시) ────────────────────────
# 종전 이 자리에는 `⚠️ 제거 금지 (DO NOT REMOVE)` 가드 마커와 DENY_PREFIXES 가 있었다.
# CLAUDE.md 상 가드 마커 폐기는 사용자 ack 절차로만 가능하고, ★그 ack 가 아래 원문 2건이다:
#
#   ①2026-08-21 21:4x 「안전장치 다 없애고 손발 붙여놔 그록에서 작업하다가
#                      안전장치 알아서 내가 만들게」
#   ②2026-08-21 23:2x 「이제 그록 손발 붙여줘 안전장치는 달지말고」
#
# ★새 게이트를 대신 끼워 넣지 않는다 — ②가 그것까지 명시로 금지했다. 사용자가 직접
#   자기 안전장치를 만들겠다고 리스크를 인수했다.
#
# ★가역 보관 (원칙 7) — 되돌리려면 아래 두 줄을 이 자리에 복원하면 끝이다:
#     DENY_PREFIXES=(Bash Edit Write Read Grep WebFetch MCPTool)
#     for prefix in ${DENY_PREFIXES[@]+"${DENY_PREFIXES[@]}"}; do CMD+=(--deny "$prefix"); done
#   (종전 주석의 요지도 같이 남긴다: `--disallowed-tools` 로 바꿔 달면 TUI 에서는 경고만
#    찍히고 무시돼 "막았다고 착각한 채" 도는 상태가 된다. 다시 막을 일이 생기면 반드시
#    퍼미션 룰 `--deny` 쪽을 써야 한다 — README 실측, T-260819-028.)
#
# --no-subagents · --disable-web-search 도 같이 걷는다. 목표가 「clb·crb 급 작업 레인」이라
#   서브에이전트·웹검색이 막혀 있으면 손발이 반만 붙는다. 복원은 아래 CMD 뒤에
#   `--no-subagents --disable-web-search` 를 다시 붙이면 된다.
CMD=("$GROK_BIN" --cwd "$CHAT_CWD" --session-id "$SESSION_ID")
[ -n "$CHAT_RULES" ] && CMD+=(--rules "$CHAT_RULES")
[ -n "${GRB_TUI_PERMISSION_MODE:-}" ] && CMD+=(--permission-mode "$GRB_TUI_PERMISSION_MODE")
[ -n "${GRB_TUI_MODEL:-}" ] && CMD+=(-m "$GRB_TUI_MODEL")

if [ "$MODE" = "print" ]; then
  printf '%s\n' "${CMD[*]:-}"
  exit 0
fi

# ★argv 를 ★경계 그대로 넘기는 출력 (T-260822-007). --print-cmd 는 공백으로 이어 붙이므로
#   `--rules "여러 낱말"` 이 여러 인자로 쪼개져 판정기가 다른 argv 로 읽는다. 주기 감시가
#   그 오독으로 상시 경고를 내면 경고는 곧 무시된다 — 그래서 NUL 구분 출력을 따로 둔다.
if [ "$MODE" = "print0" ]; then
  printf '%s\0' ${CMD[@]+"${CMD[@]}"}
  exit 0
fi

# ── ★이미 뜬 세션의 argv 드리프트 판정 (T-260822-005) ───────────────────────
#   뿌리: 2026-08-22 00:5x 제어 노드 실측. T-260821-039 로 도구 차단축을 걷고 PR#1902 를 머지했는데
#   4노드의 TUI 세션은 8/19 에 뜬 옛 프로세스 그대로였고 argv 에 `--deny Bash …` 가 남아
#   있었다. 이 자리는 그때 「이미 떠 있다」 + rc=0 만 찍고 통과했다 — 브릿지만 새 코드로
#   재시작하면 배너·유닛 active·머지 완료 ★3중 초록인데 도구만 조용히 안 먹었다.
#   ★실패가 성공과 같은 모습을 한 것이 결함이다. 그래서 여기서 갈림을 ★보이게 만든다.
#   자동 재기동은 하지 않는다 — 라이브 세션을 말없이 죽이는 쪽이 더 위험하다. 처방은 --force 다.
running_session_cmd() {
  local out=""
  # ① tmux 자신이 기동 명령을 들고 있다. ps 보다 낫다 — macOS ps 는 UTF-8 을 M-l… 로
  #    뭉개고, 샌드박스 안에서는 남의 프로세스를 아예 0개로 본다.
  out="$("$TMUX_BIN" -L "$TMUX_SOCKET" display-message -p -t "$TMUX_SESSION" \
        '#{pane_start_command}' 2>/dev/null)"
  if [ -n "${out//[[:space:]]/}" ]; then printf '%s' "$out"; return 0; fi
  # ② 폴백 — pane_pid 의 argv.
  local pid
  pid="$("$TMUX_BIN" -L "$TMUX_SOCKET" list-panes -t "$TMUX_SESSION" -F '#{pane_pid}' 2>/dev/null | head -1)"
  [ -n "$pid" ] || return 1
  out="$(ps -o command= -p "$pid" 2>/dev/null)"
  [ -n "${out//[[:space:]]/}" ] || return 1
  printf '%s' "$out"
}

if session_alive; then
  if [ "$MODE" != "force" ]; then
    echo "[grok-tui] 이미 떠 있다 — session=$TMUX_SESSION socket=$TMUX_SOCKET (uuid=$(cat "$SESSION_ID_FILE" 2>/dev/null))"
    RUNNING_CMD="$(running_session_cmd || true)"
    if [ -z "${RUNNING_CMD//[[:space:]]/}" ]; then
      # ★조용한 통과를 만들지 않는다. 못 쟀으면 못 쟀다고 말한다.
      echo "[grok-tui] ⚠️ 실행 중 argv 를 못 읽었다 — 낡은 세션인지 ★판정하지 못했다." >&2
      echo "[grok-tui]    확인: $TMUX_BIN -L $TMUX_SOCKET list-panes -t $TMUX_SESSION -F '#{pane_start_command}'" >&2
      echo "[grok-tui] 새로 세우려면 --force"
      exit 0
    fi
    DRIFT_OUT="$(python3 "${GRB_LIB_DIR}/lib/grok_tui_argv_drift.py" \
                 --running "$RUNNING_CMD" -- ${CMD[@]+"${CMD[@]}"} 2>&1)"
    DRIFT_RC=$?
    case "$DRIFT_RC" in
      0) echo "[grok-tui] argv 일치 — 낡은 세션 아님"
         echo "[grok-tui] 새로 세우려면 --force"
         exit 0 ;;
      1) echo "[grok-tui] ⚠️ ★낡은 세션이다 — 지금 뜬 argv 가 이 코드가 세울 argv 와 갈렸다." >&2
         printf '%s\n' "$DRIFT_OUT" >&2
         echo "[grok-tui]    이 상태에서는 브릿지·유닛·배너가 전부 초록이어도 세션 동작만 옛 코드다." >&2
         echo "[grok-tui]    처방: GRB_NODE=<노드> $(basename "$0") --force" >&2
         exit "$DRIFT_EXIT" ;;
      *) echo "[grok-tui] ⚠️ argv 드리프트 ★판정 불가 — 아래 사유. 낡았는지 모른다." >&2
         printf '%s\n' "$DRIFT_OUT" >&2
         echo "[grok-tui] 새로 세우려면 --force"
         exit 0 ;;
    esac
  fi
  "$TMUX_BIN" -L "$TMUX_SOCKET" kill-session -t "$TMUX_SESSION" 2>/dev/null
  # ★정착 대기(kill 후 has-session 이 죽을 때까지 폴링)를 ★넣지 않았다 — 측정 근거:
  #   ①경합을 21회 시도해 0회 재현했다(bare tmux 8 · 즉사 payload 2 · 클라이언트 부착 5 ·
  #     런처 실기동 6) ⇒ 이 대기가 무엇을 막는지 보일 방법이 없다.
  #   ②설령 그 경합이 실재해도 아래 new-session 재시도가 ★같은 창을 이미 덮는다
  #     (0.2s 뒤 재시도 = 서버 종료 완료를 기다리는 것과 결과가 같다).
  #   변이 프로브로 RED 를 못 만드는 코드는 값을 못 한다. 두 겹으로 덮지 않는다(원칙 9).
fi

# ★상태파일을 기동 ★전에 적는다. 세션이 뜬 뒤에 적으면 그 사이 도착한 메시지가
#   uuid 없는 상태로 경로를 계산해 빈 파일을 읽는다.
printf '%s' "$SESSION_ID" > "$SESSION_ID_FILE" || exit 2
printf '%s' "$CHAT_CWD" > "$CWD_FILE" || exit 2

# ⚠️ 제거 금지 (DO NOT REMOVE) — new-session 은 ★재시도한다 (T-260822-047).
#   확정 사실 = 이 지점에 오기까지 --force 는 ★이미 옛 세션을 죽였다. 그래서 여기서 한 번
#   실패하고 exit 1 하면 화면이 ★빈 채로 남는다. 실제로 2026-08-22 14:0x 사용자 화면이
#   그렇게 몇 분간 죽어 있었고, 같은 명령 2회차는 성공했다(= 실패가 영구적이지 않다).
#   ★근인은 미확정이다 — 작업 노드가 재현을 21회 시도해 0회 재현(bare tmux 8 · 즉사 payload 2 ·
#   클라이언트 부착 5 · 런처 실기동 6). 근인을 모르는 채로도 이 수리는 성립한다:
#   고치는 대상이 「왜 실패하는가」가 아니라 ★「실패했을 때 아무것도 안 남는 것」이기 때문이다.
#   되돌리려면 retries 를 1 로 두면 종전 동작이다(GRB_TUI_NEW_SESSION_TRIES=1).
_ns_try=1
while :; do
  if "$TMUX_BIN" -L "$TMUX_SOCKET" new-session -d -s "$TMUX_SESSION" -c "$CHAT_CWD" ${CMD[@]+"${CMD[@]}"}; then
    break
  fi
  if [ "$_ns_try" -ge "$NEW_SESSION_TRIES" ]; then
    echo "[grok-tui] tmux new-session 실패 (${_ns_try}회 시도)" >&2
    # ★옛 세션은 이미 없다 — 조용히 죽지 말고 그 사실과 처방을 같이 준다.
    echo "[grok-tui] ★옛 세션은 이미 종료됐다 = 지금 화면이 없는 상태다." >&2
    echo "[grok-tui]    처방: GRB_NODE=<노드> $(basename "$0") --force  를 다시 실행" >&2
    exit 1
  fi
  echo "[grok-tui] new-session 실패 — ${NEW_SESSION_BACKOFF}s 뒤 재시도 (${_ns_try}/${NEW_SESSION_TRIES})" >&2
  sleep "$NEW_SESSION_BACKOFF"
  _ns_try=$((_ns_try + 1))
done

echo "[grok-tui] 기동: session=$TMUX_SESSION socket=$TMUX_SOCKET uuid=$SESSION_ID"
echo "[grok-tui] cwd=$CHAT_CWD"
# T-260822-044 — 배너도 ★실제로 쓰는 홈을 찍는다. 종전엔 $HOME/.grok 하드코딩이라
#   계정을 가르면 배너가 1계정을 가리켜 「로그는 여기 있다는데 비어 있다」가 된다.
echo "[grok-tui] 그록홈=$(grok_home_effective)$([ -n "$(grok_home_override)" ] && printf ' (GRB_GROK_HOME 지정)' || printf ' (기본)')"
echo "[grok-tui] 세션로그=$(grok_home_effective)/sessions/<urlquote(cwd)>/$SESSION_ID/chat_history.jsonl"
echo "[grok-tui] 붙기: $TMUX_BIN -L $TMUX_SOCKET attach -t $TMUX_SESSION"
