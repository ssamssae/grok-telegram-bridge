#!/usr/bin/env python3
"""Telegram -> grok bridge: one Telegram chat drives one machine's grok CLI.

Each Telegram message runs one `grok -p ...` call (`--resume <session-uuid>`
only after grok has assigned that id) and sends the final answer back to
Telegram through the Bot API.

Session continuity: grok's `-s` flag only accepts a UUID, so a chat_id ->
session uuid mapping is kept in a local state file instead of reusing a
human-readable key.
"""
import contextlib
import http.client
import importlib.util
import json
import mimetypes
import os
import shlex
import queue
import subprocess
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

HOME = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def env(k, default=None):
    v = os.environ.get(k)
    return v if v not in (None, "") else default


def int_env(k, default, minimum=1):
    try:
        v = int(env(k, str(default)))
    except (TypeError, ValueError):
        return default
    return v if v >= minimum else default


def bool_env(k, default=False):
    v = env(k)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


TOKEN_FILE = env("GRB_TOKEN_FILE")
# No default on purpose: a shipped chat id would hand strangers this machine.
CHAT_ID = env("GRB_CHAT_ID", "") or ""
STATE_DIR = env("GRB_STATE_DIR", os.path.join(HOME, ".grok-telegram-bridge", "state"))
NAME = env("GRB_NAME", "grok")
GROK_BIN = env("GRB_GROK_BIN", "grok")


GROK_HOME_OVERRIDE = env("GRB_GROK_HOME", "")
GROK_HOME_EFFECTIVE = GROK_HOME_OVERRIDE or env("GROK_HOME") or os.path.join(HOME, ".grok")


def grok_child_env():
    """Environment for the grok child process.

    Set GRB_GROK_HOME to run against a second grok account/profile; credentials
    live in <GROK_HOME>/auth.json. Leave it unset to keep grok's own default.
    """
    child = os.environ.copy()
    if GROK_HOME_OVERRIDE:
        child["GROK_HOME"] = GROK_HOME_OVERRIDE
    return child
GROK_TIMEOUT = int_env("GRB_GROK_TIMEOUT", 180, minimum=1)
TYPING_INTERVAL = int_env("GRB_TYPING_INTERVAL", 4, minimum=1)
DRY_RUN = bool_env("GRB_DRY_RUN", False)
# 👀 는 요청 범위 밖 — 기본 off. 켜려면 GRB_SUGGESTED_REPLY_EYES=1.
SUGGESTED_REPLY_EYES = bool_env("GRB_SUGGESTED_REPLY_EYES", False)
# T-260822-052 — 사용자 직지(2026-08-22 15:2x): 본문에서 <추천답변> 태그를 빼고 버블을 따로 보낸다.
#   T-260822-041 의 「원문 1통」 기본은 이 지시로 뒤집힘. 끄려면 GRB_SUGGESTED_REPLY_SPLIT=0.
SUGGESTED_REPLY_SPLIT = bool_env("GRB_SUGGESTED_REPLY_SPLIT", True)
LOCAL_INPUT = env(
    "GRB_LOCAL_INPUT",
    os.path.join(STATE_DIR, f"grok-bridge-{NAME}.fifo") if hasattr(os, "mkfifo") else "0",
)
STDIN_INPUT = bool_env("GRB_STDIN_INPUT", sys.stdin.isatty())
# T-260822-048 — 수신 미디어. 클로드 브릿지(clb) 와 같은 확장자·프롬프트 계약.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".weba"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
AUDIO_TRANSCRIBE_CMD = env("GRB_AUDIO_TRANSCRIBE_CMD", "")
AUDIO_TRANSCRIBE_TIMEOUT = int_env("GRB_AUDIO_TRANSCRIBE_TIMEOUT", 60, minimum=1)
try:
    DOWNLOAD_ATTEMPT_TIMEOUT = float(env("GRB_DOWNLOAD_ATTEMPT_TIMEOUT_SEC", "30") or "30")
except ValueError:
    DOWNLOAD_ATTEMPT_TIMEOUT = 30.0

# 라이브 모드에서만 토큰 파일을 강제한다 — dry-run(테스트·개발)은 grok 호출과 발신을
# 스텁·모킹으로 대체하므로 실토큰이 필요 없다.
if not DRY_RUN:
    if not TOKEN_FILE or not os.path.isfile(TOKEN_FILE):
        sys.exit(f"❌ GRB_TOKEN_FILE 부재: {TOKEN_FILE}")
    with open(TOKEN_FILE) as f:
        TOKEN = json.load(f).get("api_key", "").strip()
    if not TOKEN:
        sys.exit(f"❌ 토큰 비어있음: {TOKEN_FILE}")
else:
    TOKEN = ""

if not CHAT_ID:
    sys.exit(
        "GRB_CHAT_ID is not set. Set it to your own Telegram chat id - "
        "the bridge only answers that one chat. See README.md, Step 2."
    )

os.makedirs(STATE_DIR, exist_ok=True)
OFFSET_FILE = os.path.join(STATE_DIR, f"grok-bridge-{NAME}.offset")
SESSIONS_FILE = os.path.join(STATE_DIR, f"grok-bridge-{NAME}.sessions.json")
API = f"https://api.telegram.org/bot{TOKEN}"
GROK_LOCK = threading.Lock()
JOBS = queue.Queue()

# ── ② 잡 적체 관측성 + 유실 방지 스풀 (T-260821-033) ─────────────────────────
#
# ⚠️ 제거 금지 (DO NOT REMOVE) — 이 브릿지는 ★멈춘 것을 멈췄다고 말하지 못했다.
#   실사고 = 2026-08-20 22:50 마지막 정상 왕복 이후 fifo·텔레그램 입력이 `mirror_prompt`
#   로그조차 안 남기고 무기한 적체. 워커 스레드가 이전 잡 안에서 막히면 새 잡은 큐에만
#   쌓이고 ★밖에서는 아무 신호도 안 보인다(process_job 의 첫 동작이 mirror_prompt 라,
#   워커가 그 앞에서 막히면 로그가 시작조차 안 된다). 제어 노드은 py-spy 부재로 스레드 덤프도
#   못 떴고 결국 launchctl kickstart 로 덮었다 — 그 재기동이 ★적체분을 통째로 지웠다.
#
# ★관측을 워치독보다 먼저 넣는 이유 = 지금은 멈춘 것조차 밖에서 안 보인다. 무엇을 보고
#   재기동하는지 모르는 워치독은 「조용히 초록으로 되돌리는」 계기가 된다.
#
# ★자기재기동은 넣지 않는다(배차 명시). 경보까지가 이 레그다 — 재기동은 증거를 지운다.
HEALTH_FILE = os.path.join(STATE_DIR, f"grok-bridge-{NAME}.health.json")
INBOX_FILE = os.path.join(STATE_DIR, f"grok-bridge-{NAME}.inbox.jsonl")
# T-260823-051: 재기동 뒤 TUI 최종답 회수. 커서는 「이미 폰으로 보낸 최종답 개수」.
TUI_CURSOR_FILE = os.path.join(STATE_DIR, f"grok-bridge-{NAME}.tui-cursor.json")
TUI_INFLIGHT_FILE = os.path.join(STATE_DIR, f"grok-bridge-{NAME}.tui-inflight.json")
_JOB_SOURCE = "telegram"
# 폰발 턴이 도는 동안 로컬 미러는 비켜선다 (T-260824-036). GROK_LOCK 만으로는 못 막는
# 창이 있다 — process_job 은 락을 놓은 뒤에 mirror_answer 로 커서를 올리고, 그 사이를
# 미러가 비집으면 같은 답이 폰에 두 번 뜬다.
_TUI_JOB_ACTIVE = threading.Event()
# T-260825-003: /clear 가 물린 대기 루프를 접으라는 신호. handle_tui_reset 이 세우고
#   _tui_wait_for_final 이 본다. 다음 대기가 열릴 때 지운다.
_TUI_RESET = threading.Event()


def _int_env_raw(key, default):
    try:
        return int(str(os.environ.get(key, "")).strip() or default)
    except (TypeError, ValueError):
        return default


HEALTH_INTERVAL = max(5, _int_env_raw("GRB_HEALTH_INTERVAL", 30))
_HEALTH_LOCK = threading.Lock()
_HEALTH = {
    "pid": os.getpid(),
    "name": NAME,
    "started_at": time.time(),
    "last_poll_at": 0.0,
    "last_enqueue_at": 0.0,
    "last_job_started_at": 0.0,
    "last_job_done_at": 0.0,
    "enqueued": 0,
    "done": 0,
    "spool_errors": 0,
    "queue_depth": 0,
    "worker_alive": False,
}
_WORKER_THREAD = None


def health_snapshot():
    with _HEALTH_LOCK:
        snap = dict(_HEALTH)
    snap["queue_depth"] = JOBS.qsize()
    snap["worker_alive"] = bool(_WORKER_THREAD and _WORKER_THREAD.is_alive())
    snap["written_at"] = time.time()
    return snap


def health_write():
    """상태 파일을 원자적으로 갱신. ★어떤 실패도 브릿지를 죽이지 않는다(fail-open).

    ⚠️ 제거 금지 (DO NOT REMOVE) — tmp 경로는 ★호출마다 유일해야 하고 쓰기는 직렬이어야 한다.
      T-260821-033 최초판은 고정 `f"{HEALTH_FILE}.tmp"` 를 4개 스레드(ticker·poller·enqueue·worker)가
      공유했다. 라이브 재기동 직후 실측(2026-08-22 03:44, T-260822-028):
        grok bridge health write 실패: [Errno 2] ... 'grok-bridge-<node>.health.json.tmp' -> ...
      한 스레드가 os.replace 로 tmp 를 옮긴 뒤 다른 스레드가 같은 tmp 를 replace 하려다 죽는다.
      ★계기가 간헐적으로 실패하면 그 계기는 「가끔 거짓말하는 계기」다 — 웨지 판정의 입력이라 더 나쁘다.
      (fail-loud 로 만들어 둔 덕에 라이브 로그 한 줄로 잡혔다. 그 설계는 그대로 둔다.)

      ★수리는 tmp 유일성 하나다 — 락은 넣지 않는다. 변이 프로브 실측(T-260822-028):
      고정 tmp+락 제거 → RED / ★유일 tmp 만 남기고 락만 제거 → 여전히 GREEN.
      os.replace 가 원자적이라 유일 tmp 면 충돌 자체가 없고, snapshot 은 락 밖에서 뜨므로
      락을 걸어도 쓰기 순서를 보장하지 못한다 = 값을 못 하는 코드였다(원칙 9).
    """
    try:
        snap = health_snapshot()
        tmp = f"{HEALTH_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False)
        os.replace(tmp, HEALTH_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"grok bridge health write 실패: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass


def health_mark(**fields):
    if fields:
        with _HEALTH_LOCK:
            for key, value in fields.items():
                if key in ("enqueued", "done", "spool_errors"):
                    _HEALTH[key] = _HEALTH.get(key, 0) + value
                else:
                    _HEALTH[key] = value
    health_write()


def inbox_spool(update_id, source, text):
    """★유실 방지 — 수신분을 offset 전진 ★전에 디스크에 남긴다.

    종전 폴러는 `offset = update_id + 1` 을 **먼저 영속화**하고 그 다음 큐에 넣었다.
    큐는 프로세스 메모리라, 워커가 막힌 채 재기동되면 ★적체분이 통째로 사라지는데
    텔레그램은 offset 이 이미 지나가 재전송하지 않는다 ⇒ 사용자가 보낸 말이 없던 일이 된다.
    (같은 모양이 카톡봇에서도 잡혔다 — T-260822-002 mark 선전진.)

    ⚠️ 스풀 실패 시에도 offset 은 전진시킨다(호출부 finally). 디스크 실패로 같은 메시지를
       무한 재수신하는 편이 더 나쁘기 때문이다. 대신 실패 횟수를 health 에 남겨 ★조용히
       지나가지 않게 한다 — 이 트레이드오프는 의도된 것이고, 바꾸려면 백오프가 먼저다.
    """
    try:
        rec = {
            "ts": time.time(),
            "update_id": update_id,
            "source": source,
            "text": text,
        }
        with open(INBOX_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"grok bridge inbox spool 실패: {exc}", file=sys.stderr)
        health_mark(spool_errors=1)


def health_ticker():
    """주기 하트비트. ★적체가 있을 때만 로그로도 말한다(정상 시 침묵 유지)."""
    while True:
        health_write()
        depth = JOBS.qsize()
        if depth > 0:
            snap = health_snapshot()
            idle = time.time() - (snap["last_job_done_at"] or snap["started_at"])
            print(
                f"grok-bridge[{NAME}] backlog queue_depth={depth} "
                f"enqueued={snap['enqueued']} done={snap['done']} "
                f"since_last_done={idle:.0f}s worker_alive={snap['worker_alive']}",
                flush=True,
            )
        time.sleep(HEALTH_INTERVAL)



class GrokExecError(RuntimeError):
    pass


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _write(path, val):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(val))
    os.replace(tmp, path)


def _read_sessions():
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_sessions(sessions):
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sessions, f)
    os.replace(tmp, SESSIONS_FILE)


def read_session_uuid(chat_id):
    return _read_sessions().get(str(chat_id), "")


def write_session_uuid(chat_id, session_uuid):
    sessions = _read_sessions()
    sessions[str(chat_id)] = (session_uuid or "").strip()
    _write_sessions(sessions)


def clear_session_uuid(chat_id):
    sessions = _read_sessions()
    if str(chat_id) in sessions:
        del sessions[str(chat_id)]
        _write_sessions(sessions)


def get_or_create_session_uuid(chat_id):
    """저장된 session_id 만 돌려준다. 없으면 빈 문자열.

    GROK_BRIDGE_SESSION_NO_PREASSIGN — 로컬에서 uuid4 를 미리 만들어 -s 로
    붙이면 그 플래그는 *신규* 세션 id 지정이지 resume 이 아니다 (grok --help:
    "Does not resume existing sessions"). 신규는 세션 플래그 없이 호출하고
    system.init 의 session_id 를 저장한 뒤, 다음 턴은 --resume 으로만 재사용한다.
    """
    return read_session_uuid(chat_id)


def tg(method, timeout=60, **params):
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{method}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print(f"tg {method} 실패: {e}", file=sys.stderr)
                return None
            time.sleep(2)


def local_print(text):
    print(text, flush=True)


def mirror_prompt(source, text):
    if source == "local":
        local_print(f"local input:\n{text}")
    else:
        local_print(f"telegram input:\n{text}")


def typing_loop(stop_event):
    """입력중 표시. 예외 1번에 스레드가 죽으면 긴 턴 전체가 무신호가 된다 (T-260822-068)."""
    failures = 0
    while not stop_event.is_set():
        try:
            result = tg("sendChatAction", timeout=5, chat_id=CHAT_ID, action="typing")
            # tg() 는 Exception 을 삼키고 None 을 돌려준다. 그 경로도 침묵이면
            # 실사고처럼 로그에 sendChatAction 흔적 0건이 된다.
            if result is None:
                raise RuntimeError("sendChatAction returned None")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if failures <= 3:
                print(f"grb typing 실패({failures}): {exc}", file=sys.stderr)
        if stop_event.wait(TYPING_INTERVAL):
            break


def start_typing():
    stop_event = threading.Event()
    threading.Thread(
        target=typing_loop,
        args=(stop_event,),
        daemon=True,
        name=f"grok-bridge-{NAME}-typing",
    ).start()
    return stop_event


def set_eyes_reaction(chat_id, message_id):
    """R-C9: 추천답변 첫 bubble 에 👀 리액션. non-fatal — 실패해도 본문 발신 결과는 안 되돌린다."""
    if not SUGGESTED_REPLY_EYES or not message_id:
        return
    try:
        tg(
            "setMessageReaction",
            timeout=5,
            chat_id=chat_id,
            message_id=message_id,
            reaction=json.dumps([{"type": "emoji", "emoji": "👀"}]),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"eyes reaction 실패(non-fatal): {exc}", file=sys.stderr)


def _parse_grok_stream(stdout):
    """streaming-messages-json NDJSON 파싱 (T-260817-009 §① 실측 근거).

    반환: (answer, subtype, error_text, cost_usd). subtype != "success" 면
    error_text 가 채워진다. 다중 assistant 이벤트는 등장 순서대로 이어붙인다
    (전체 시맨틱은 grb-leg2 실측 대조 대상 — sot-note 참고).
    """
    answer_parts = []
    subtype = ""
    error_text = ""
    cost_usd = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text:
                answer_parts.append(text)
        elif etype == "result":
            subtype = str(event.get("subtype") or "")
            cost_usd = event.get("total_cost_usd")
            if subtype != "success":
                error_text = str(
                    event.get("error") or event.get("message") or event.get("reason") or ""
                ).strip()
    return "".join(answer_parts).strip(), subtype, error_text, cost_usd


def _tool_use_names(stdout):
    """스트림에 뜬 tool_use 블록 이름들. 채팅 레인에서는 항상 비어 있어야 한다.

    비어 있지 않다 = 도구 차단이 뚫렸다 = agentic 루프에 들어갔다는 뜻이고, 그
    경로의 실제 결말은 180s 침묵이었다(T-260819-024 실사고). 조용히 기다리지 않고
    호출부에서 즉시 실패로 바꾼다 — 침묵보다 시끄러운 실패가 낫다.
    """
    names = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names.append(str(block.get("name") or "?"))
    return names


def _parse_session_id(stdout):
    """system.init 의 session_id. 없으면 빈 문자열."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "system":
            continue
        if str(event.get("subtype") or "") != "init":
            continue
        sid = str(event.get("session_id") or "").strip()
        if sid:
            return sid
    return ""


# ── 채팅 레인 비-agentic 강제 (T-260819-024) ─────────────────────────────────
# 실사고 = 사용자 한 마디("답이 안오네")에 그록이 run_terminal_cmd(hostname/date/whoami)
# + read_file(설정 파일) 로 ★조사부터 시작해 input_tokens 31,857 · 180s 초과 →
# 21:08 이후 실사용 3건 전부 무응답. 이 레인은 대화지 작업 지시가 아니다.
#
# ★플래그 값은 전부 `grok --help`(1.0.4) · ~/.grok/README.md 실측이다(추측 0):
#   · --disallowed-tools = 도구 ID 콤마목록. README §Tool Filtering 표의 9종 전량 +
#     특수 항목 `Agent`(서브에이전트 전면 차단). 도구가 스키마에서 제거되므로
#     "승인 안 함" 이 아니라 ★애초에 호출할 수 없다 — 이것이 실행 차단의 본체다.
#   · --no-subagents = 같은 축의 독립 플래그. 한 겹 더.
#   · --cwd = 규칙파일 탐색 범위를 결정한다. README §AGENTS.md: git repo 안이면
#     repo 루트→cwd 의 모든 디렉토리에서 Agents.md/Claude.md/AGENT.md/AGENTS.md 와
#     에이전트 규칙파일을 읽어 시스템 프롬프트에 붙인다. grb 의 기본 cwd 는 $HOME 이고
#     $HOME 이 git repo 면 그 규칙파일이 통째로 실린다. 그게 조사 반사를 부추긴
#     입력이었다. 그래서 채팅 레인은
#     ★git repo 밖의 빈 디렉토리로 격리한다(기본 = 시스템 임시디렉토리 아래).
#   · --rules = 발견된 규칙파일 "위에 덧붙이는" 추가 규칙(README 주석). 즉답 계약을 여기 싣는다.
#
# ★--always-approve 를 유지하는 이유: 실행 차단의 본체는 위의 도구 제거이고, 도구가
#   0 개인 상태에서 이 플래그는 승인할 대상이 없다. 반대로 지금 빼면 승인 프롬프트가
#   뜨는 다른 경로에서 헤드리스가 멎을 수 있는데, 그게 바로 지금 고치는 증상(무응답)이다.
#   검증 없이 그 위험을 새로 들이지 않는다 — 대신 env 로 뺄 수 있게 열어둔다.
#   (제어 노드 라이브 실측 뒤 GRB_GROK_ALWAYS_APPROVE=0 으로 무중단 전환 가능)
# ★목록에 README 표 ID 와 ★실측 런타임 이름을 함께 싣는다 (T-260819-028).
#   실측: README §Tool Filtering 표는 `run_terminal_cmd` 인데, 실제 세션 로그
#   (~/.grok/sessions/<cwd>/<uuid>/chat_history.jsonl)의 tool_calls 이름은
#   `run_terminal_command` 다. 표에 아예 없는 이름도 돈다 — `search_tool`,
#   `get_command_or_subagent_output`. 어느 쪽이 필터 정본인지 이 노드에서 확정할 수
#   없으므로(grok 미인증) 양쪽을 다 넣는다. 모르는 ID 를 더 넣는 비용은 0 이고,
#   빠뜨렸을 때의 비용은 실사고(180s 무응답)다.
# ★T-260821-039: 기본 차단목록을 ★빈 문자열로 폐기한다 (사용자 직접 지시 원문 2건).
#   ①2026-08-21 21:4x 「안전장치 다 없애고 손발 붙여놔 그록에서 작업하다가
#                      안전장치 알아서 내가 만들게」
#   ②2026-08-21 23:2x 「이제 그록 손발 붙여줘 안전장치는 달지말고」
#   위 두 축이 아래 T-260819-024 서술의 전제(「이 레인은 대화지 작업 지시가 아니다」)를
#   사람 판단으로 뒤집었다. 이제 이 레인은 ★작업 지시 레인이다.
#   ★가역 보관 (원칙 7) — 되돌리려면 GRB_GROK_DISALLOWED_TOOLS 에 아래 문자열을 그대로
#     넣으면 코드 수정 없이 종전 상태다. 종전 기본값 전문:
#       run_terminal_cmd,run_terminal_command,grep,read_file,search_replace,list_dir,
#       web_search,web_fetch,todo_write,task,search_tool,get_command_or_subagent_output,Agent
CHAT_DISALLOWED_TOOLS_DEFAULT = ""
# ★이 변수만 env() 헬퍼를 안 쓴다. 헬퍼는 빈 문자열을 "미설정" 으로 접어 기본값을
#   돌려주는데, 여기서는 ★빈 문자열이 "차단 없음" 이라는 뜻을 가져야 한다 — 차단이
#   라이브 답변을 깨는 사태가 나면 제어 노드이 코드 수정 없이 env 하나로 되돌릴 수 있어야
#   하기 때문이다(가역 우선). 미설정(None)과 빈값을 구분하는 이유가 그것이다.
_CHAT_DISALLOWED_RAW = os.environ.get("GRB_GROK_DISALLOWED_TOOLS")
CHAT_DISALLOWED_TOOLS = (
    CHAT_DISALLOWED_TOOLS_DEFAULT if _CHAT_DISALLOWED_RAW is None else _CHAT_DISALLOWED_RAW.strip()
)
# T-260821-039: 종전 룰은 「도구는 전부 차단돼 있고 … 조사하지 말고 곧바로 답해라」였다.
#   도구를 열어놓고 이 문장을 남기면 차단이 코드에서 ★프롬프트로 옮겨갈 뿐이라 손발이
#   여전히 묶인다(모델이 "나는 도구가 없다"고 믿는다). 작업 레인 룰로 교체한다.
#   복원 = GRB_GROK_CHAT_RULES 로 종전 문자열을 덮으면 코드 수정 없이 원위치.
# T-260822-041 — 사용자 지시(2026-08-22 12:08): 그록 위에 씌운 층을 벗기고 순정으로 넘긴다.
#   기본 ★빈 문자열 = 순정. 아래 `if CHAT_RULES:` 가드가 이미 있어 빈 값이면
#   --rules 자체를 안 붙인다. 되살리려면 GRB_GROK_CHAT_RULES 로 문구를 넣어라.
CHAT_RULES = env("GRB_GROK_CHAT_RULES", "")
# ★기본 베이스는 ★고정 경로다. `tempfile.gettempdir()` 를 쓰면 안 된다 —
#   그건 TMPDIR 을 읽고, launchd 가 브릿지에 주는 TMPDIR(/var/folders/…/T)과
#   사람이 기동면을 돌리는 셸의 TMPDIR 은 다르다. 두 프로세스가 ★합의해야 하는
#   경로를 환경변수 파생값으로 잡으면 같은 코드가 환경마다 다른 답을 낸다.
#   실사고(T-260819-028 ⑤, 제어 노드 2026-08-19 23:2x): 기동면은
#   /private/tmp/grb-chat-cwd-<name> 에 세션을 만들었고(chat_history 65,873B) 브릿지는
#   /private/var/folders/…/T/grb-chat-cwd-<name> 을 읽어 exists=False → 180s 무응답.
#   심링크 정규화(②)로는 못 덮는다 — 문자열이 갈린 게 아니라 ★출발점이 달랐다.
CHAT_CWD_BASE = env("GRB_CHAT_CWD_BASE", "/tmp")
# ★T-260821-039: 기본 cwd 를 「git repo 밖의 빈 디렉토리」에서 ★$HOME 으로 되돌린다.
#   위 격리는 조사 반사를 막으려던 장치인데, 작업 레인에서는 그게 곧 ★손발 절단이다 —
#   빈 임시디렉토리 안에서는 열어준 셸·파일 도구로 건드릴 대상이 아예 없다.
#   위 CHAT_CWD_BASE 의 고정경로 논리(TMPDIR 파생 금지, T-260819-028 ⑤)는 그대로 남긴다:
#   GRB_CHAT_CWD_BASE 로 되돌아갈 때 런처·브릿지가 여전히 같은 경로를 계산해야 한다.
#   복원 = GRB_GROK_CHAT_CWD=/tmp/grb-chat-cwd-<name> (코드 수정 불요).
CHAT_CWD = env("GRB_GROK_CHAT_CWD", os.path.expanduser("~"))
GROK_MAX_TURNS = env("GRB_GROK_MAX_TURNS")
GROK_ALWAYS_APPROVE = bool_env("GRB_GROK_ALWAYS_APPROVE", True)


def ensure_chat_cwd():
    """채팅 레인 전용 빈 작업디렉토리. 없으면 만들고 ★realpath 로 정규화해 돌려준다.

    여기에 규칙파일을 두지 않는 것이 요점이다 — 두면 격리가 무의미해진다.

    ★정규화가 왜 필수인가 (T-260819-028 제어 노드 라이브 실측 ②):
    그록 TUI 는 cwd 를 realpath 로 정규화해 세션 키를 만든다. macOS 의 `/tmp` 는
    `/private/tmp` 심링크라, 문자열 그대로 quote 하면 브릿지는
    `~/.grok/sessions/%2Ftmp%2F…` 를 읽고 TUI 는 `%2Fprivate%2Ftmp%2F…` 에 쓴다.
    실측 증상 = 「답은 화면에 44s 만에 났는데 브릿지는 180s 무응답」 — 답이 없는 게
    아니라 ★다른 파일을 보고 있었다. 두 경로가 문자열로만 다르고 같은 디렉토리를
    가리키므로 로그만 봐서는 안 보인다.
    """
    path = os.path.expanduser(os.path.expandvars(CHAT_CWD))
    os.makedirs(path, exist_ok=True)
    return os.path.realpath(path)


def _grok_cmd(prompt, session_uuid=None):
    cmd = [GROK_BIN, "-p", prompt]
    if session_uuid:
        # -s/--session-id 는 신규 id 예약이다. 기존 id 에 쓰면
        # "Session ID … is already in use" 로 즉시 실패한다 (T-260819-004 실측).
        cmd.extend(["--resume", session_uuid])
    cmd.extend(["--output-format", "streaming-messages-json"])
    if GROK_ALWAYS_APPROVE:
        cmd.append("--always-approve")
    # ★여기부터가 비-agentic 강제 (T-260819-024). 순서는 의미 없지만 묶어서 읽히게 둔다.
    if CHAT_DISALLOWED_TOOLS:
        cmd.extend(["--disallowed-tools", CHAT_DISALLOWED_TOOLS])
    # T-260821-039: `--no-subagents` 무조건 부착을 걷는다. 도구를 열어놓고 서브에이전트만
    #   막으면 손발이 반만 붙는다. 복원은 이 자리에 cmd.append("--no-subagents") 한 줄.
    if CHAT_RULES:
        cmd.extend(["--rules", CHAT_RULES])
    if CHAT_CWD:
        cmd.extend(["--cwd", ensure_chat_cwd()])
    if GROK_MAX_TURNS:
        # 기본 미부착이다. 도구가 0 이면 루프 자체가 성립하지 않아 상한이 불필요하고,
        # 상한에 걸려 멈춘 턴의 result subtype 을 이 노드에서 실측할 수 없다(grok 미인증).
        # 답이 나왔는데 subtype 때문에 에러로 뒤집히는 회귀를 추측으로 들이지 않는다 —
        # 제어 노드 실측 뒤 env 로 켠다.
        cmd.extend(["--max-turns", str(GROK_MAX_TURNS)])
    return cmd


def _dry_run_grok_stream(prompt, session_uuid):
    """GRB_DRY_RUN 스텁 — 실 grok CLI 를 부르지 않고 성공 스트림을 합성한다."""
    sid = session_uuid or str(uuid.uuid4())
    return "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": sid}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"[dry-run] {prompt}"}]},
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0}),
        ]
    )


def run_grok(prompt, session_uuid=None):
    if DRY_RUN:
        stdout = _dry_run_grok_stream(prompt, session_uuid)
    else:
        cmd = _grok_cmd(prompt, session_uuid)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GROK_TIMEOUT,
                stdin=subprocess.DEVNULL,
                # T-260822-044 — 계정 분리. override 가 없으면 os.environ 사본 그대로라
                # 종전과 동일하다(무회귀).
                env=grok_child_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GrokExecError(f"시간 초과({GROK_TIMEOUT}s)") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            suffix = f": {detail[-1][:160]}" if detail else ""
            raise GrokExecError(f"rc={proc.returncode}{suffix}")
        stdout = proc.stdout

    # ★T-260821-039: 종전 여기서 도구 호출이 뜨면 GrokExecError 로 턴을 통째로 뒤집었다
    #   (「차단 플래그가 안 먹었다」). 그건 이 레인이 대화 전용이라는 전제 위에 선 판정이고,
    #   사용자가 그 전제를 해제했다 — 이제 도구 호출은 ★정상이며 오히려 목표다.
    #   플래그(--disallowed-tools)만 걷고 이 raise 를 남겨두면 도구를 쓰는 순간 매번
    #   에러로 죽어서 "열었는데 안 된다" 가 된다. 그래서 같이 걷는다.
    #   ★가역 보관 (원칙 7) — 되돌리려면 이 자리에 아래를 복원한다:
    #       tool_calls = _tool_use_names(stdout)
    #       if tool_calls:
    #           raise GrokExecError("채팅 레인에 agentic 도구 호출이 떴다("
    #                               + ", ".join(tool_calls[:3]) + ") — 차단 플래그가 안 먹었다")
    #   `_tool_use_names()` 는 지우지 않고 남겨둔다 — 진단·픽스처가 계속 쓴다.
    answer, subtype, error_text, cost_usd = _parse_grok_stream(stdout)
    if subtype and subtype != "success":
        raise GrokExecError(error_text or f"grok result subtype={subtype}")
    if not answer:
        raise GrokExecError("빈 응답")
    return answer, cost_usd, _parse_session_id(stdout)


# ── TUI 레인: tmux 대화형 REPL 미러 (T-260819-028) ────────────────────────────
# 사용자 지시 「그록 텔레그램 브릿지를 티먹스 터미널 CLI 에만 동기화시키자」(2026-08-19 22:30).
# grb 는 5노드 브릿지 중 유일하게 보이는 REPL 이 없었다 — 메시지마다 헤드리스 단발을
# 띄웠다 사라졌다. clb·crb 는 tmux 안의 TUI 에 붙여넣고 답은 ★구조화 세션로그에서 읽는다
# (crb docstring: "not screen scraping"). 그 형태를 그록에 옮긴다.
#
# ★기본값은 headless 다. 오늘 라이브 초록으로 검증된 레인을 기본으로 두고 TUI 는 opt-in —
#   기본 전환은 제어 노드 실측 초록 뒤 별도 결정이다(제어 노드 오케 판단 2, 가역 우선).
#
# ★차단축이 헤드리스와 ★다르다. README 명시: --tools/--disallowed-tools/--max-turns 는
#   헤드리스 전용이고 TUI 에서는 경고만 찍히고 무시된다. 그래서 TUI 레인의 차단은
#   퍼미션 룰(--allow/--deny, "These flags work in both TUI and headless mode")로 건다.
#   차이가 하나 더 있다: 퍼미션 룰은 도구를 ★제거하는 게 아니라 실행을 거부한다 —
#   즉 tool_calls 시도 자체는 날 수 있다. 그래서 여기서는 헤드리스처럼 시도를 무조건
#   실패로 뒤집지 않는다(아래 run_grok_tui 주석).
# T-260821-039: 기본 레인 headless → ★TUI. 사용자 지시 「이제 그록 손발 붙여줘」의 목표가
#   clb·crb 급 작업 레인이고, 그 둘은 tmux TUI 에 붙여 답을 세션로그에서 읽는 형태다.
#   ★TUI 를 기본으로 삼는 실측 근거: headless 레인은 도구 호출이 뜨면 아래 tool_calls
#   fail-loud 로 턴을 통째로 뒤집었고(이번 레그에서 같이 걷었다), TUI 레인은 처음부터
#   「도구 시도를 만나도 답이 오면 배달한다」로 관용이었다(run_grok_tui docstring).
#   되돌리려면 GRB_CHAT_LANE=headless — 코드 수정 없이 env 하나다(가역 우선).
CHAT_LANE = (env("GRB_CHAT_LANE", "headless") or "headless").strip().lower()
# T-260822-044 — 세션로그도 계정을 따라간다. 계정만 갈라놓고 세션 디렉토리가 1계정에
#   남아 있으면 브릿지가 영영 빈 파일을 읽는다(T-260819-028 과 동형의 갈림).
#   GRB_GROK_HOME 미지정이면 effective = $HOME/.grok 이라 종전 기본값과 ★글자 그대로 같다.
GROK_SESSIONS_DIR = env("GRB_GROK_SESSIONS_DIR", os.path.join(GROK_HOME_EFFECTIVE, "sessions"))
TUI_SESSION_ID_FILE = os.path.join(STATE_DIR, f"grok-tui-{NAME}.session-id")
TMUX_BIN = env("GRB_TMUX_BIN", "tmux")
TMUX_SOCKET = env("GRB_TMUX_SOCKET", "default")
TMUX_SESSION = env("GRB_TMUX_SESSION", "grok")
TMUX_PANE = env("GRB_TMUX_PANE", f"{TMUX_SESSION}:0.0")
TUI_SUBMIT_KEY = env("GRB_TUI_SUBMIT_KEY", "Enter")
# ⚠️ 제거 금지 (DO NOT REMOVE) — 「총 시간 상한」이 아니라 ★「무진전(idle) 상한」이다
#   (T-260822-042, 사용자 체감 축).
#   ★왜 숫자만 올리면 안 되나: 종전은 총 180s 통짜 상한이었다. 실측(제어 노드 11:36~11:44)에서
#   1,702B 실코딩 과제가 3회 전부 잘렸는데, 로그의 도구 시도가
#   todo_write·run_terminal_command·read_file·grep 였다 ⇒ ★못 쓴 게 아니라 180초 안에
#   못 끝낸 것이다. 그렇다고 180 을 1800 으로 바꾸면 잘림은 사라지지만 ★매달린 것과
#   일하는 것을 못 가른다 — 그록이 wedge 되면 30분을 조용히 기다리고, 그건 사용자 폰에서
#   「먹통」으로 보인다. 원 증상보다 나쁘다.
#   ⇒ 자르는 기준은 ★출력이 멈춘 시간이다. 세션 히스토리에 새 행이 붙으면 일하는 중이니
#     기다리고, 아무 행도 안 붙으면 IDLE 상한에서 자른다.
#   ★열린 도구(tool_call 있고 tool_result 없음)는 새 행이 없어도 일하는 중이다
#     (T-260822-053). 도구 실행 중엔 jsonl 이 멈춘다 — 그걸 무진전으로 보면 긴 셸이
#     3분에 잘린다. 침묵(도구 없음) idle 과 최종 캡은 그대로 둔다.
#   이 repo 관용구 정렬 = claude 브릿지의 「liveness 가 루프를 몰고 최종 캡이 backstop」
#     (claude-telegram-bridge.py:391 주석 · CLB_TYPING_MAX_SECONDS 기본 7200). 새 발명이 아니다.
#   ★ANSWER_TIMEOUT 은 의미가 바뀌었다 = 진전이 계속돼도 끊는 ★최종 캡. 무한 루프 방지용이라
#     크게 잡는다 (T-260823-048: 1800s 가 실작업을 버렸다. Claude 정렬 7200).
#     실제로 거의 항상 IDLE 쪽이 먼저 걸린다.
TUI_IDLE_TIMEOUT = int_env("GRB_TUI_IDLE_TIMEOUT", 180, minimum=1)
TUI_ANSWER_TIMEOUT = int_env("GRB_TUI_ANSWER_TIMEOUT", 7200, minimum=1)
# T-260823-007: 총 상한/무진전에 잘린 뒤에도 TUI 가 곧 답을 쓰면 그걸 배달한다.
#   0 이면 종전(잘리는 즉시 에러).
# T-260825-003: 창의 정본은 유예(60s). max(유예, 총상한) 은 무진전 컷 뒤에도
#   2시간을 더 쥐어 /clear 가 자물쇠 뒤에서 34분을 기다렸다(제어 노드 00:02~00:36).
#   총상한으로 잘렸고 진전이 있으면(T-260823-048) 유예 창을 진전마다 다시 연다 —
#   고정 60s 가 실작업을 버리던 축은 그 조건으로만 살린다.
#   GRB_TUI_HARVEST_FOLLOW=0 이면 종전 max(유예, 총상한).
TUI_LATE_HARVEST_GRACE = int_env("GRB_TUI_LATE_HARVEST_GRACE", 60, minimum=0)
TUI_HARVEST_FOLLOW = int_env("GRB_TUI_HARVEST_FOLLOW", 1, minimum=0)
# T-260825-003: /clear 가 GROK_LOCK 을 기다리면 물린 잡이 탈출구를 막는다.
#   1(기본) = 락을 기다리지 않고 레인을 되세운다. 0 이면 종전(락을 기다림).
TUI_RESET_STEAL = int_env("GRB_TUI_RESET_STEAL", 1, minimum=0)
# 폰에 「아직 살아있다」를 찍는 간격 (T-260822-068). typing 인디케이터가 안 보여도
#   이 1줄이 중간보고가 된다. 이야기체 루트에 쌓이면 낡아 보이므로 너무 촘촘히 보내지 않는다.
TUI_PROGRESS_INTERVAL = int_env("GRB_TUI_PROGRESS_INTERVAL", 60, minimum=1)
# 1분 간격이어도 26분 턴이면 26통. 상한을 같이 건다 (T-260822-068 폭주 방지).
#   ★앵커 편집이 켜져 있으면 이 상한은 ★새 말풍선 수에만 걸린다 — 편집은 말풍선을
#   안 늘리므로 여기서 세지 않는다 (T-260824-042).
TUI_PROGRESS_MAX = int_env("GRB_TUI_PROGRESS_MAX", 4, minimum=1)
# ── 진행 앵커 (T-260824-042, 사용자 요청 2026-08-24) ──────────────────────────
# 종전: 진행 문장을 매번 ★새 말풍선으로 보냈다. 실측 폐해 둘 —
#   ① 이야기체 루트에 「아직 하고 있어」가 쌓여 대화가 지저분해진다.
#   ② 3시간 47분 멈춘 턴(T-260824-041 인접 실사고)에서도 문장이 똑같아 dedup 에
#      걸려 폰엔 아무 변화가 없었다. 「살아있다」와 「멈췄다」가 같은 화면이었다.
# 지금: 앵커 말풍선 1통을 띄우고 그 통만 editMessageText 로 고쳐 쓴다. 경과시간이
#   같이 찍히므로 멈춘 턴은 초가 안 흐르는 게 아니라 ★단계가 안 바뀌는 걸로 보인다.
#   0 이면 종전 동작(새 말풍선 누적) — 되돌리기 스위치다.
TUI_PROGRESS_ANCHOR = int_env("GRB_TUI_PROGRESS_ANCHOR", 1, minimum=0)
# 앵커가 생긴 뒤의 갱신 간격. 새 말풍선이 아니라 편집이라 촘촘해도 알림이 안 뜬다
#   (텔레그램은 editMessageText 에 알림을 안 띄운다). 첫 통까지의 침묵은 여전히
#   TUI_PROGRESS_INTERVAL 이 쥔다 — 60초는 조용히, 그 뒤부터 살아 움직인다.
TUI_PROGRESS_EDIT_INTERVAL = int_env("GRB_TUI_PROGRESS_EDIT_INTERVAL", 30, minimum=5)
# 편집 폭주 방지. 30초 × 240 = 2시간이면 ANSWER_TIMEOUT(7200s) 과 같은 자리다.
TUI_PROGRESS_EDIT_MAX = int_env("GRB_TUI_PROGRESS_EDIT_MAX", 240, minimum=1)
TUI_TOOL_PROGRESS_LABELS = {
    "grep": "코드 찾는 중",
    "read_file": "파일 읽는 중",
    "run_terminal_command": "명령 실행 중",
    "search_replace": "파일 고치는 중",
    "write": "파일 쓰는 중",
    "todo_write": "할 일 정리 중",
    "get_command_or_subagent_output": "돌아가는 작업 기다리는 중",
    "spawn_subagent": "나눠서 보는 중",
    "web_search": "웹 찾는 중",
    "web_fetch": "페이지 읽는 중",
}
TUI_LAUNCHER = "grok-tui-session-start.sh"
TUI_LOG_KEY = "grb_tui_lane"
# T-260822-074: 그록 TUI 의 /clear=/new 는 새 UUIDv7 대화를 연다. 붙여넣으면
#   브릿지가 보는 일기장과 갈라져 답이 폰으로 안 돌아간다. 이 두 토큰만 가로챈다.
TUI_RESET_TOKENS = frozenset({"/clear", "/new"})
TUI_RESTART_TIMEOUT = int_env("GRB_TUI_RESTART_TIMEOUT", 30, minimum=1)


def float_env(k, default):
    try:
        v = float(env(k, str(default)))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


TUI_POLL_INTERVAL = float_env("GRB_TUI_POLL_INTERVAL", 0.5)
TUI_SUBMIT_DELAY = float_env("GRB_TUI_SUBMIT_DELAY", 0.3)
TUI_FALLBACK_HEADLESS = bool_env("GRB_TUI_FALLBACK_HEADLESS", False)
# ── 로컬 미러: 터미널에 직접 친 턴도 폰에 올린다 (T-260824-036) ─────────────────
# 종전 TUI 레인이 폰으로 되돌리는 답은 「폰에서 들어온 질문의 답」뿐이었다. 일기장을
# 읽는 시점이 딱 둘이라(기동 시 harvest 1회 · 폰발 job 직후 1회) 사용자가 tmux 창에
# 직접 친 턴은 chat_history.jsonl 에만 쌓이고 폰엔 안 떴다 — 원칙 2(모든 활동은 폰에
# 보인다)의 구멍이고, 실제로 「터미널에서 넣은 질문과 답이 왜 안 오냐」로 관측됐다.
# ★기본 OFF 다. 옆에서 길게 굴리는 턴까지 전부 밀면 폰이 시끄럽고, 지금 초록인 레인의
#   기본 동작을 바꾸지 않는 게 정석이다(원칙 8). 켜는 스위치는 env 1개.
TUI_MIRROR_LOCAL = bool_env("GRB_TUI_MIRROR_LOCAL", False)
TUI_MIRROR_LOCAL_INTERVAL = float_env("GRB_TUI_MIRROR_LOCAL_INTERVAL", 5.0)
TUI_MIRROR_LOCAL_SOURCE = "tui-local"
# 질문 원문을 통째로 밀면 긴 붙여넣기가 폰을 덮는다. 답이 본체고 질문은 꼬리표다.
TUI_MIRROR_LOCAL_PROMPT_MAX = int_env("GRB_TUI_MIRROR_LOCAL_PROMPT_MAX", 300)
# ── 터미널 /clear 카드 (T-260826-026) ────────────────────────────────────────
# 폰 /clear 는 handle_tui_reset 이 이미 카드를 보낸다. 터미널 슬래시 /clear|/new 는
# 그록이 *같은 pid* 에서 새 UUIDv7 대화를 열 뿐이라 브릿지가 침묵했다.
# 증거 = GROK_HOME/active_sessions.json 의 같은 pid + 다른 session_id.
# 폰 --force 재기동은 pid 가 바뀌므로 이 감시기는 침묵(중복 카드 금지).
TUI_CLEAR_WATCH = bool_env("GRB_TUI_CLEAR_WATCH", True)
TUI_CLEAR_WATCH_INTERVAL = float_env("GRB_TUI_CLEAR_WATCH_INTERVAL", 1.0)
TUI_CLEAR_CONFIRM = "세션 클리어됐어. 이어서 말하면 돼."
_TUI_CLEAR_SEEN = {}
_TUI_CLEAR_BASELINED = False


def tui_session_id():
    """상주 세션의 고정 uuid. 기동면이 파일에 적어둔다.

    uuid 를 고정하지 않으면 어느 세션 디렉토리를 읽어야 하는지 알 수 없다 —
    그래서 기동면이 `--session-id` 로 못 박고 그 값을 여기서 되읽는다.
    """
    return (env("GRB_TUI_SESSION_ID") or _read(TUI_SESSION_ID_FILE)).strip()


# ⚠️ 제거 금지 (DO NOT REMOVE) — 「보는 일기장이 맞나」를 묻는 층 (T-260824-028).
#
# 발원 실사고 2026-08-24 13:42 사용자 신고. 터미널엔 27초 만에 답이 완성됐는데 폰엔
# 「180s 무진전」 실패가 떴다. 제어 노드 프로브 실측:
#   핀(state/grok-tui-<name>.session-id) = 79dfae94… / 그 chat_history 마지막 = 01:02
#   살아있는 대화 = 01a02f5d-a3af-★7ca1(UUIDv7 = 그록이 새로 연 대화) / 13:41 까지 기록
# 즉 그록 TUI 가 도중 새 대화로 갈아탔는데 브릿지는 기동 때 적은 파일을 계속 읽었다.
# ★답이 없던 게 아니라 다른 일기장을 본 것이다. 그록 프로세스 argv 는 여전히
# `--session-id 79dfae94` 라 argv 로도 추적이 안 된다(실측).
#
# ★작업 노드 구조 신호(escalation T-260824-027): 이 표면 수리 4건
#   (T-260823-007/-048/-051, T-260824-001)이 전부 ★회수 창을 넓히는 층이고
#   「보는 파일이 맞나」를 묻는 층은 0건이었다. 그래서 계속 안 고쳐졌다.
#
# ★함부로 따라가지 않는다 — T-260822-074 의 판단을 뒤집지 않는다.
#   그 티켓은 "새 폴더를 멋대로 따라가면 잘못된 대화를 집을 수 있다" 며 추종을 거부했고
#   그 우려는 여전히 옳다(같은 cwd 에 `gg local` 등 평행 세션이 생길 수 있다).
#   그래서 ★증거가 모호하지 않을 때만 따라간다. 조건 넷을 모두 만족해야 한다:
#     ① 핀한 대화가 죽어 있다 (FOLLOW_STALE_SEC 동안 무기록) — 살아있는 대화는 안 뺏는다
#     ② 핀보다 ★엄격히 최신인 후보가 있다
#     ③ 그런 후보가 ★정확히 하나다 (둘 이상이면 모호 → 안 따라간다)
#     ④ 그 후보가 최근에 쓰였다 (FOLLOW_FRESH_SEC 이내)
#   하나라도 어긋나면 추종하지 않고 ★진단만 돌려준다. 그 진단은 타임아웃 문구에 실려
#   나간다 — 이 사고의 진짜 피해는 잘린 것이 아니라 ★조용히 잘린 것이었다.
TUI_FOLLOW_STALE_SEC = int_env("GRB_TUI_FOLLOW_STALE_SEC", 90, minimum=1)
TUI_FOLLOW_FRESH_SEC = int_env("GRB_TUI_FOLLOW_FRESH_SEC", 3600, minimum=1)
# ★실패 ★순간의 추종·회수 (T-260825-003).
#   위 조건 ④(후보가 FRESH_SEC 이내)는 ★발사 직전 판정에서 함정이 된다 — 레인이 한 시간만
#   쉬면 살아있는 대화도 창 밖으로 밀려 후보 0개가 되고, 브릿지는 죽은 일기장을 계속 본다.
#   그런데 그록이 답을 쓰는 순간 그 대화는 fresh 가 되고, 실패 직전의 tui_rotation_diagnosis()
#   는 그제서야 rotated 를 말한다. ⇒ ★판정이 이르고 진단은 늦다. 그 사이에서 답이 통째로
#   버려진다. 이 스위치가 그 틈을 닫는다: 진단이 rotated 를 말하는 자리에서 핀을 옮기고,
#   살아있는 일기장에서 ★이 질문의 답을 건져 배달한다.
TUI_RESCUE_ON_ROTATION = bool_env("GRB_TUI_RESCUE_ON_ROTATION", True)
# 질문 대조에 쓰는 앞머리 길이. 짧으면 남의 답을 내 답으로 오인하고, 너무 길면 TUI 가
# 줄바꿈·말줄임으로 다듬은 질문과 안 맞는다.
TUI_RESCUE_MATCH_CHARS = int_env("GRB_TUI_RESCUE_MATCH_CHARS", 24, minimum=4)


def _tui_sessions_root():
    return os.path.join(
        os.path.expanduser(GROK_SESSIONS_DIR),
        urllib.parse.quote(ensure_chat_cwd(), safe=""),
    )


def _history_stat(session_uuid):
    """일기장 상태. 0.0 하나로 없음·못 읽음·아직 안 씀을 뭉개지 않는다 (T-260824-041).

    반환 = (kind, mtime)
      ok          파일이 있고 내용이 있다 (mtime > 0)
      empty       세션 폴더는 있는데 일기장이 없거나 0바이트 — 방금 켬
      missing     세션 폴더 자체가 없다 — 핀이 가리키는 대화가 사라짐
      unreadable  권한 등 OSError — 없다고 단정하지 않는다 (T-260824-039 동형)
    """
    if not session_uuid:
        return ("missing", 0.0)
    root = _tui_sessions_root()
    session_dir = os.path.join(root, session_uuid)
    path = os.path.join(session_dir, "chat_history.jsonl")
    try:
        st = os.stat(path)
    except FileNotFoundError:
        try:
            if os.path.isdir(session_dir):
                return ("empty", 0.0)
        except OSError:
            return ("unreadable", 0.0)
        return ("missing", 0.0)
    except OSError:
        return ("unreadable", 0.0)
    if st.st_size == 0:
        return ("empty", float(st.st_mtime))
    return ("ok", float(st.st_mtime))


def tui_session_rotation():
    """핀한 세션과 실제로 살아 있는 세션이 갈렸는지 본다.

    반환 = dict(pin, pin_mtime, live, live_mtime, verdict, why)
      verdict: "ok"        핀이 살아 있다 (또는 판정 불가)
               "rotated"   갈렸고 추종 조건 4개를 모두 만족한다
               "ambiguous" 갈린 것 같은데 후보가 여럿이라 안 따라간다
    """
    pin = tui_session_id()
    root = _tui_sessions_root()
    now = time.time()
    kind, pin_mtime = _history_stat(pin) if pin else ("missing", 0.0)
    out = {"pin": pin, "pin_mtime": pin_mtime, "pin_kind": kind,
           "live": "", "live_mtime": 0.0, "verdict": "ok", "why": ""}

    if not pin:
        out["why"] = "핀이 비어 있다"
        return out
    # 못 읽으면 「없다」고 단정하지 않는다 — 추종을 열지 않는다.
    if kind == "unreadable":
        out["why"] = "핀한 대화의 일기장을 못 읽었다 — 없다고 단정하지 않는다"
        return out
    # 방금 켠 세션(폴더만 있고 첫 줄 전)을 죽었다고 보고 뺏지 않는다.
    # ★0.0 을 살아있다고 뒤집지 않는다. missing 은 아래 후보 검색으로 내려간다.
    if kind == "empty":
        out["why"] = "핀한 대화가 아직 기록 중이다"
        return out
    # ① 핀이 아직 살아 있으면 건드리지 않는다.
    #    pin_mtime 이 0 인 ok 는 없다. 0 을 truthy 로 뒤집으면 missing 추종이 영영 안 돈다.
    if kind == "ok" and pin_mtime and (now - pin_mtime) < TUI_FOLLOW_STALE_SEC:
        out["why"] = "핀한 대화가 아직 기록 중이다"
        return out

    try:
        names = os.listdir(root)
    except OSError as exc:
        out["why"] = f"세션 폴더를 못 읽었다({exc})"
        return out

    cands = []
    for name in names:
        if name == pin:
            continue
        cand_kind, m = _history_stat(name)
        if cand_kind != "ok":
            continue
        if m <= pin_mtime:                      # ② 핀보다 엄격히 최신만
            continue
        if (now - m) > TUI_FOLLOW_FRESH_SEC:    # ④ 최근에 쓰인 것만
            continue
        cands.append((m, name))

    if not cands:
        out["why"] = "핀보다 최신인 살아있는 후보가 없다"
        return out
    cands.sort(reverse=True)
    out["live"], out["live_mtime"] = cands[0][1], cands[0][0]
    if len(cands) > 1:                          # ③ 하나일 때만
        out["verdict"] = "ambiguous"
        out["why"] = f"후보가 {len(cands)}개다 — 잘못된 대화를 집을 수 있어 안 따라간다"
        return out
    out["verdict"] = "rotated"
    out["why"] = "핀한 대화는 죽었고 살아있는 후보가 정확히 하나다"
    return out


def tui_rotation_diagnosis(rot=None):
    """사람이 읽는 한 줄 진단. 조용한 타임아웃을 막는 것이 목적이다."""
    rot = rot or tui_session_rotation()
    if rot["verdict"] == "ok":
        return ""
    def _ts(v):
        return time.strftime("%H:%M:%S", time.localtime(v)) if v else "없음"
    return (
        f"★브릿지가 보는 대화가 갈렸다({rot['verdict']}) — "
        f"핀={rot['pin'][:8]}… 마지막기록={_ts(rot['pin_mtime'])} / "
        f"살아있는쪽={rot['live'][:8] or '?'}… 마지막기록={_ts(rot['live_mtime'])}. "
        f"{rot['why']}"
    )


def tui_follow_session_rotation():
    """조건을 만족할 때만 핀을 살아있는 세션으로 옮긴다. 옮겼으면 새 uuid 를 반환."""
    rot = tui_session_rotation()
    if rot["verdict"] != "rotated":
        return ""
    try:
        # 되돌릴 수 있게 옛 핀을 남긴다 (원칙 7). _write 는 tmp+os.replace —
        # open(..., "w") 는 열자마자 잘라 실패 시 핀이 빈 문자열이 된다 (T-260824-041).
        _write(TUI_SESSION_ID_FILE + ".prev", rot["pin"])
        _write(TUI_SESSION_ID_FILE, rot["live"])
    except OSError as exc:
        print(f"{TUI_LOG_KEY} 세션 추종 실패: {exc}", file=sys.stderr)
        return ""
    print(
        f"{TUI_LOG_KEY} 세션 추종 {rot['pin']} → {rot['live']} ({rot['why']})",
        file=sys.stderr,
    )
    return rot["live"]


def tui_history_path():
    """~/.grok/sessions/<urlquote(cwd)>/<uuid>/chat_history.jsonl (실측 구조).

    인코딩은 `urllib.parse.quote(path, safe='')` 와 정확히 일치한다 — 실물 디렉토리명
    실물 디렉토리명 2건으로 대조 확인했다.
    """
    cwd = ensure_chat_cwd()
    return os.path.join(
        os.path.expanduser(GROK_SESSIONS_DIR),
        urllib.parse.quote(cwd, safe=""),
        tui_session_id(),
        "chat_history.jsonl",
    )


def _tmux(*args, input_text=None):
    cmd = [TMUX_BIN, "-L", TMUX_SOCKET, *args]
    kwargs = {"capture_output": True, "text": True, "timeout": 15}
    if input_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = input_text
    return subprocess.run(cmd, **kwargs)


def tui_session_alive():
    try:
        return _tmux("has-session", "-t", TMUX_SESSION).returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"{TUI_LOG_KEY} has-session 실패: {exc}", file=sys.stderr)
        return False


def tui_dead_message():
    return (
        f"tmux 그록 세션이 없다(socket={TMUX_SOCKET} session={TMUX_SESSION}) — "
        f"{TUI_LAUNCHER} 로 기동해라. "
        "headless 폴백은 GRB_TUI_FALLBACK_HEADLESS=1 로 명시할 때만 돈다."
    )


def slash_token(text):
    """텔레그램 한 줄 슬래시 명령 토큰. clb slash_token 과 같은 계약.

    `/clear`, `/clear@bot`, ` /NEW  ` → `/clear`/`/new`.
    개행이 있거나 맨 앞이 / 가 아니면 평문("").
    """
    stripped = (text or "").strip()
    if "\n" in stripped or not stripped.startswith("/"):
        return ""
    return stripped.split(maxsplit=1)[0].split("@", 1)[0].lower()


def tui_launcher_bin():
    """기동면 실경로. 공개판은 브릿지와 같은 폴더, 내부 repo 는 scripts/ 옆.

    GRB_TUI_LAUNCHER 는 테스트 스텁·운영자 오버라이드. 건강감시 스크립트와 같은 이름.
    """
    return env("GRB_TUI_LAUNCHER") or os.path.join(SCRIPT_DIR, "grok-tui-session-start.sh")


def restart_tui_session():
    """기동면 --force 로 상주 TUI 를 새로 세운다. 새 uuid 가 상태파일에 박힌다.

    /clear 를 창에 붙여넣지 않는 이유 = 그록 TUI 의 /clear=/new 는 새 UUIDv7
    대화를 열고, 브릿지는 기동 때 박은 session-id 파일만 본다(T-260822-074).
    새 폴더를 멋대로 따라가면 잘못된 대화를 집을 수 있어 쓰지 않는다.
    """
    launcher = tui_launcher_bin()
    if not os.path.isfile(launcher):
        raise GrokExecError(f"TUI 기동면이 없다({launcher})")
    try:
        proc = subprocess.run(
            [launcher, "--force"],
            capture_output=True,
            text=True,
            timeout=TUI_RESTART_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise GrokExecError(f"TUI 재기동 시간 초과: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise GrokExecError(
            f"TUI 재기동 실패 rc={proc.returncode}: {err or 'stderr 비어 있음'}"
        )
    sid = tui_session_id()
    if not sid:
        raise GrokExecError(f"TUI 재기동 후 uuid 가 없다({TUI_SESSION_ID_FILE})")
    print(f"{TUI_LOG_KEY} /clear 재기동 uuid={sid}", file=sys.stderr)
    return sid


def handle_tui_reset(source="telegram", task_id=None):
    """TUI 레인 /clear|/new — 붙여넣지 않고 창을 다시 세운다.

    T-260825-003: ingest 는 줄을 건너뛰지만 이 함수가 GROK_LOCK 을 기다리면
      물린 잡이 탈출구를 막는다. 기본은 락을 기다리지 않는다. 물린 대기 루프는
      _TUI_RESET 을 보고 접힌다. 건질 답이 0인 경우가 이 경로의 전제다
      (오늘 실측: 핀한 대화가 죽어 있었다). GRB_TUI_RESET_STEAL=0 이면 종전.
    """
    _TUI_RESET.set()
    try:
        if TUI_RESET_STEAL:
            restart_tui_session()
        else:
            with GROK_LOCK:
                restart_tui_session()
    except GrokExecError as exc:
        mirror_error(source, str(exc), task_id=task_id)
        return
    notify_tui_cleared(task_id=task_id)


def tui_active_sessions_path():
    """그록이 지금 열어 둔 대화 목록. 계정 홈을 따른다 (T-260822-044).

    GRB_ACTIVE_SESSIONS_FILE 은 테스트 스텁. 라이브는 <GROK_HOME>/active_sessions.json.
    """
    override = env("GRB_ACTIVE_SESSIONS_FILE")
    if override:
        return override
    return os.path.join(os.path.expanduser(GROK_HOME_EFFECTIVE), "active_sessions.json")


def _read_active_sessions():
    try:
        with open(tui_active_sessions_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _adopt_tui_session(sid):
    """핀을 새 uuid 로 옮긴다. 실패해도 카드 발신은 막지 않는다(가시성이 목적)."""
    old = tui_session_id()
    try:
        if old and old != sid:
            _write(TUI_SESSION_ID_FILE + ".prev", old)
        _write(TUI_SESSION_ID_FILE, sid)
    except OSError as exc:
        print(f"{TUI_LOG_KEY} /clear 핀 이동 실패: {exc}", file=sys.stderr)
        return False
    print(f"{TUI_LOG_KEY} /clear 핀 {old or '-'} → {sid}", file=sys.stderr)
    return True


def notify_tui_cleared(task_id=None):
    """클리어 확인 1통. kind=final — ack 는 R-C5 가 본문을 버리고 이모지 1자만 보낸다.

    (실사고: 폰 /clear 확인이 부엉이만 큼직하게 찍힘, T-260823-025).
    """
    local_print(TUI_CLEAR_CONFIRM)
    deliver_mesh_event("final", TUI_CLEAR_CONFIRM, task_id=task_id)


def _active_sessions_for_cwd():
    """이 브릿지 cwd 의 (pid → session_id). cwd 가 다른 그록(서브에이전트 등)은 버린다."""
    try:
        cwd = os.path.realpath(ensure_chat_cwd())
    except OSError:
        return {}
    out = {}
    for row in _read_active_sessions():
        if not isinstance(row, dict):
            continue
        row_cwd = row.get("cwd") or ""
        if not row_cwd:
            continue
        try:
            if os.path.realpath(row_cwd) != cwd:
                continue
        except OSError:
            continue
        sid = str(row.get("session_id") or "").strip()
        if not sid:
            continue
        try:
            pid = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        out[pid] = sid
    return out


def watch_tui_local_clear():
    """같은 pid 의 session_id 가 바뀌면 터미널 /clear|/new 다.

    반환 = 이번에 보낸 카드 수(0 또는 1+). 헤드리스는 0.
    첫 관측은 기준선만 찍고 카드를 안 보낸다 — 기동 직후 살아있는 대화를
    '방금 클리어'로 오인하면 안 된다.
    """
    global _TUI_CLEAR_BASELINED
    if CHAT_LANE != "tui":
        return 0
    now = _active_sessions_for_cwd()
    if not _TUI_CLEAR_BASELINED:
        _TUI_CLEAR_SEEN.clear()
        _TUI_CLEAR_SEEN.update(now)
        _TUI_CLEAR_BASELINED = True
        return 0
    sent = 0
    for pid, sid in now.items():
        old = _TUI_CLEAR_SEEN.get(pid)
        if old and old != sid:
            _adopt_tui_session(sid)
            notify_tui_cleared()
            sent += 1
    _TUI_CLEAR_SEEN.clear()
    _TUI_CLEAR_SEEN.update(now)
    return sent


def tui_local_clear_ticker():
    """active_sessions.json 을 주기적으로 본다. 예외 1회가 감시를 죽이면 안 된다."""
    while True:
        time.sleep(TUI_CLEAR_WATCH_INTERVAL)
        try:
            sent = watch_tui_local_clear()
            if sent:
                print(f"{TUI_LOG_KEY} local /clear 카드 {sent}통", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"{TUI_LOG_KEY} local /clear 감시 실패: {exc}", file=sys.stderr)


def _read_history_rows(path):
    """chat_history.jsonl 을 관대하게 읽는다. 쓰는 중 잘린 마지막 줄은 버린다."""
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _tui_final_answer_rows(rows):
    """최종답변 꼴 = assistant + content 가 비지 않은 문자열 + tool_calls 없음.

    실측 분포(제어 노드 대화형 세션 394행): reasoning 104 · tool_result 175 · assistant 104
    중 이 꼴은 5본뿐이다. reasoning·tool_result 를 답으로 오인하면 생각 중인 문장이
    사용자에게 날아간다.
    """
    finals = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        if row.get("tool_calls"):
            continue
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            finals.append(row)
    return finals


# ★사람이 친 발화만 고른다 (T-260824-036). 실측(그록 1.0.4 세션 로그): 사람 줄은
#   type=user 이고 content 블록 안에 <user_query>…</user_query> 로 감싸여 온다.
#   하네스가 끼워 넣는 줄은 synthetic_reason 이 붙거나 그 태그가 아예 없다 — 안 가리면
#   system-reminder 뭉치가 질문인 척 폰에 뜬다.
_USER_QUERY_OPEN = "<user_query>"
_USER_QUERY_CLOSE = "</user_query>"


def _tui_user_query_text(row):
    if not isinstance(row, dict) or row.get("type") != "user":
        return ""
    if row.get("synthetic_reason"):
        return ""
    content = row.get("content")
    blocks = content if isinstance(content, list) else [content]
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else block
        if not isinstance(text, str):
            continue
        start = text.find(_USER_QUERY_OPEN)
        if start < 0:
            continue
        end = text.find(_USER_QUERY_CLOSE, start)
        body = text[start + len(_USER_QUERY_OPEN) : end if end >= 0 else None]
        if body.strip():
            return body.strip()
    return ""


def _tui_final_answer_indices(rows):
    """최종답이 몇 번째 행인지. 질문 짝을 뒤로 찾으려면 위치가 필요하다.

    판정 규칙은 재현하지 않고 _tui_final_answer_rows 에 그대로 묻는다 — 같은 규칙을
    두 곳에 두면 한쪽만 고쳐져 생각 중인 문장이 답으로 새는 길이 생긴다.
    """
    return [idx for idx, row in enumerate(rows) if _tui_final_answer_rows([row])]


def _tui_tool_call_names(rows):
    names = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for call in row.get("tool_calls") or []:
            if isinstance(call, dict):
                names.append(str(call.get("name") or "?"))
    return names


def _tui_elapsed_words(seconds):
    """경과시간을 사람 말로. 「3분 47초」 — 사용자 DM 은 숫자표(04:07)가 아니라 산문이다."""
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}초"
    if total < 3600:
        return f"{total // 60}분 {total % 60}초"
    return f"{total // 3600}시간 {(total % 3600) // 60}분"


def _tui_progress_line(reported, elapsed=None):
    """사용자 DM 용 한 줄. 선두 이모지 없음 (이야기체).

    ★elapsed 를 붙이는 이유(T-260824-042) = 「아직 하고 있어」만 반복하면 살아있는 턴과
      멈춘 턴이 폰에서 같은 화면이다. 실사고: 3시간 47분 물려 있던 턴이 같은 문장이라
      dedup 에 걸려 폰엔 변화가 0이었다. 경과시간이 있으면 「단계는 그대론데 시간만
      흐른다」가 눈에 보인다.
    """
    if not reported:
        head = "아직 하고 있어"
        tail = ". 답은 끝나면 바로 보낼게."
    else:
        last = reported[-1]
        head = f"아직 하고 있어. 지금은 {TUI_TOOL_PROGRESS_LABELS.get(last, last)}"
        tail = "."
    if elapsed is None:
        return f"{head}{tail}"
    return f"{head} · {_tui_elapsed_words(elapsed)} 경과"


def _tui_progress_done_line(elapsed, ok=True, reset=False):
    """앵커 마감 한 줄. 답 본문은 기존 mirror_answer 가 따로 배달한다.

    앵커를 그냥 두면 답이 온 뒤에도 「아직 하고 있어」가 위에 남는다 — 끝난 걸 끝났다고
    적어야 화면이 거짓말을 안 한다(원칙 6). 도구 목록은 안 싣는다: 바로 아래에 답이
    붙으므로 여기서 되풀이하면 같은 말을 두 번 하는 화면이 된다.

    ★실패 턴을 「다 했어」로 닫지 않는다 — 에러 메시지는 따로 가는데 앵커만 초록이면
      화면 두 줄이 서로 다른 말을 한다. 사유는 안 싣는다(에러 통이 이미 싣는다).
    ★/clear 로 접힌 턴은 에러 통이 안 따라온다 (process_job 이 mirror_error 없이
      return). 「사유는 아래」는 그때 거짓이다. handle_tui_reset ack 와 겹치지 않게
      이 말풍선만 「이 턴은 접었다」고 마감한다 (T-260825-003 회신).
    """
    if reset:
        return f"이 턴은 접었어 · {_tui_elapsed_words(elapsed)} 만에"
    if not ok:
        return f"여기서 멈췄어 · {_tui_elapsed_words(elapsed)} 만에 (사유는 아래)"
    return f"다 했어 · {_tui_elapsed_words(elapsed)} 걸림"


def _tui_tool_in_flight(rows):
    """아직 tool_result 가 안 온 tool_call 이 있으면 True.

    실측 jsonl: assistant.tool_calls[].id → 이후 type=tool_result.tool_call_id.
    도구가 돌아가는 동안에는 새 행이 안 붙는다. 그걸 idle(무진전)로 보면
    긴 셸이 3분에 잘린다(T-260822-053, C 작업 실사고).
    """
    pending = set()
    anonymous_open = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "tool_result":
            tid = str(row.get("tool_call_id") or "")
            if tid:
                pending.discard(tid)
            elif anonymous_open:
                anonymous_open -= 1
            continue
        for call in row.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            cid = str(call.get("id") or "")
            if cid:
                pending.add(cid)
            else:
                anonymous_open += 1
    return bool(pending) or anonymous_open > 0


def _tui_json_load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _tui_json_save(path, data):
    """디스크 실패가 워커를 죽이면 회수 레일 자체가 침묵 실패가 된다. fail-open."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        print(f"{TUI_LOG_KEY} state write 실패 {path}: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _tui_cursor_load():
    return _tui_json_load(TUI_CURSOR_FILE)


def _tui_cursor_save(session_id, finals_sent):
    _tui_json_save(
        TUI_CURSOR_FILE,
        {"session_id": session_id, "finals_sent": int(finals_sent), "ts": time.time()},
    )


# ── 히스토리 압축 대응 (T-260825-005) ─────────────────────────────────────────
# 그록 TUI 는 대화가 길어지면 chat_history.jsonl 을 ★스스로 줄인다. 그 순간 브릿지가
# 들고 있던 "몇 번째부터 읽어라"(baseline 인덱스)와 "몇 개까지 보냈다"(커서 카운터)가
# 통째로 무효가 된다 — 둘 다 줄어들 수 없는 파일을 전제로 만든 값이기 때문이다.
#
# ★실사고 2026-08-24 22:19~00:47 (작업 노드, 2시간 28분):
#   640행에서 baseline=634 를 적고 붙여넣었는데 그록이 22:24 에 52행으로 압축했다.
#   rows[634:] 는 그때부터 영구 빈 슬라이스다. 답은 22:24:08 에 파일 맨 뒤에 멀쩡히
#   적혀 있었는데 브릿지는 없는 페이지만 넘기며 굶었다. 무진전 타임아웃도 못 잡았다 —
#   이미 도구를 쓴 턴이라 그 가지는 break 대신 타이머를 재무장하기 때문이다(위 참조).
#   그래서 총상한 7200s 를 다 채우고 늦은회수 7200s 로 넘어갔다. 그 사이 다음 지시가
#   큐에 쌓였다가 재기동 때 유실됐다.
#
# ★이 사고의 본체는 "잘못 읽은 것" 이 아니라 ★"조용히 빈 슬라이스를 돌려준 것" 이다.
#   그래서 두 헬퍼 다 값을 되돌리기만 하지 않고 반드시 크게 찍는다(원칙 6 fail-loud).
def _tui_rebaseline_if_compacted(path, baseline):
    """행수가 baseline 보다 적으면 압축이다 — 0 으로 되돌리고 rows 와 함께 돌려준다."""
    rows = _read_history_rows(path)
    if len(rows) < baseline:
        print(
            f"{TUI_LOG_KEY} ★히스토리 압축 감지 — baseline={baseline} > 현재 {len(rows)}행. "
            f"0 으로 재기준화한다 (T-260825-005)",
            file=sys.stderr,
        )
        baseline = 0
    return rows, baseline


def _tui_cursor_clamp(already, total):
    """커서가 현재 최종답 개수를 앞서면 압축 전 파일의 값이다 — 0 부터 다시 회수한다.

    압축된 파일은 짧아서 0 부터 훑어도 폭주하지 않는다. 반대로 그냥 두면 갇힌 답이
    영원히 안 나간다 — 실사고에서 커서 13 vs 실제 최종답 1본이 정확히 그 상태였다.
    """
    if already > total:
        print(
            f"{TUI_LOG_KEY} ★커서가 파일보다 앞선다 — finals_sent={already} > 현재 {total}본. "
            f"압축으로 보고 0 부터 회수한다 (T-260825-005)",
            file=sys.stderr,
        )
        return 0
    return max(already, 0)


def _tui_inflight_load():
    return _tui_json_load(TUI_INFLIGHT_FILE)


def _tui_inflight_write(payload):
    rec = dict(payload or {})
    rec["ts"] = time.time()
    _tui_json_save(TUI_INFLIGHT_FILE, rec)


def _tui_inflight_clear():
    try:
        os.unlink(TUI_INFLIGHT_FILE)
    except OSError:
        pass


def harvest_orphaned_tui_finals(source="telegram", task_id=None):
    """재기동 뒤 jsonl 에 남은 미발신 최종답을 폰으로 보낸다 (T-260823-051).

    커서 부재의 첫 기동은 현재 개수로만 초기화한다 — 과거 일기장 폭주를 막는다.
    다만 inflight 가 같은 세션이면 baseline 이후만 회수한다(죽은 턴의 답).
    """
    if CHAT_LANE != "tui":
        return 0
    sid = (tui_session_id() or "").strip()
    if not sid:
        return 0
    rows = _read_history_rows(tui_history_path())
    finals = _tui_final_answer_rows(rows)
    cur = _tui_cursor_load()
    already = None
    if cur.get("session_id") == sid:
        try:
            already = int(cur.get("finals_sent") or 0)
        except (TypeError, ValueError):
            already = 0
    if already is None:
        job = _tui_inflight_load()
        if job.get("session_id") == sid:
            try:
                baseline = int(job.get("baseline") or 0)
            except (TypeError, ValueError):
                baseline = 0
            already = len(_tui_final_answer_rows(rows[: max(baseline, 0)]))
            source = job.get("source") or source
        else:
            _tui_cursor_save(sid, len(finals))
            return 0
    new_rows = finals[_tui_cursor_clamp(already, len(finals)) :]
    sent = 0
    for row in new_rows:
        text = str(row.get("content") or "").strip()
        if not text:
            continue
        print(f"{TUI_LOG_KEY} orphaned final harvest", file=sys.stderr)
        mirror_answer(source, text, task_id=task_id)
        sent += 1
    _tui_cursor_save(sid, len(finals))
    return sent


def _tui_wait_inflight_if_any():
    """기동 직후: 죽기 전 붙여넣은 턴의 답이 아직 안 왔으면 기다린다. 붙여넣기는 안 한다."""
    job = _tui_inflight_load()
    if not job:
        return
    sid = (tui_session_id() or "").strip()
    if not sid or job.get("session_id") != sid:
        return
    if not tui_session_alive():
        return
    try:
        baseline = int(job.get("baseline") or 0)
    except (TypeError, ValueError):
        return
    try:
        _tui_wait_for_final(tui_history_path(), baseline)
    except GrokExecError as exc:
        print(f"{TUI_LOG_KEY} inflight wait: {exc}", file=sys.stderr)


def _darwin_clipboard_info_has_image(info):
    """osascript 'clipboard info' 출력에 사진 flavor 가 있는지 (T-260824-006).

    실측 2026-08-24 제어 노드: 스크린샷이 남은 pasteboard 는
    `«class PNGf», 3511527, «class AVIF», … TIFF picture …` 이고 문자열 flavor 가 없다.
    글만 남은 pasteboard 는 `«class utf8», 0, string, 0`.
    """
    blob = (info or "").lower()
    if not blob.strip():
        return False
    needles = (
        "pngf",
        "tiff",
        "jpeg picture",
        "gif picture",
        "jp2",
        "class 8bps",
        "class bmp",
        "class avif",
    )
    return any(token in blob for token in needles)


def _darwin_clipboard_info():
    try:
        result = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{TUI_LOG_KEY} clipboard info: {exc}", file=sys.stderr)
        return ""
    return result.stdout or ""


def _darwin_park_clipboard_png():
    """사진 flavor 가 있으면 PNG 로 백업하고 pasteboard 를 글만 남긴다. 없으면 None."""
    if not _darwin_clipboard_info_has_image(_darwin_clipboard_info()):
        return None
    handle = tempfile.NamedTemporaryFile(prefix="grb-clip-", suffix=".png", delete=False)
    handle.close()
    path = handle.name
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        "try\n"
        "  set pngData to the clipboard as «class PNGf»\n"
        f'  set outFile to open for access POSIX file "{escaped}" with write permission\n'
        "  set eof outFile to 0\n"
        "  write pngData to outFile\n"
        "  close access outFile\n"
        '  return "ok"\n'
        "on error\n"
        '  return "no"\n'
        "end try"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{TUI_LOG_KEY} clipboard park: {exc}", file=sys.stderr)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    if (result.stdout or "").strip() != "ok" or not os.path.isfile(path) or os.path.getsize(path) <= 0:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    try:
        subprocess.run(["pbcopy"], input=b"", capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{TUI_LOG_KEY} clipboard clear: {exc}", file=sys.stderr)
    print(f"{TUI_LOG_KEY} clipboard image parked {path}", file=sys.stderr)
    return path


def _darwin_restore_clipboard_png(path):
    if not path or not os.path.isfile(path):
        return
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'set pngData to (read POSIX file "{escaped}" as «class PNGf»)\n'
        "set the clipboard to pngData"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{TUI_LOG_KEY} clipboard restore: {exc} (parked at {path})", file=sys.stderr)
        return
    try:
        os.unlink(path)
    except OSError:
        pass


@contextlib.contextmanager
def _isolate_os_clipboard_images():
    """주입 동안 macOS pasteboard 사진을 가린다 (T-260824-006).

    그록 TUI 는 paste/submit 때 NSPasteboard 의 스크린샷도 [Image #1] 칩으로 붙인다
    (docs/user-guide/03-keyboard-shortcuts.md Image Paste = Cmd+V 가 사진을 칩으로).
    텔레그램 inbox 는 글만인데 TUI 제출이 같은 화면을 반복 첨부하던 축:
    제어 노드 2026-08-24 00:00·00:33 「안녕하세요」 2회, update 687750885/886 text-only,
    세션 asset md5 동일, pasteboard PNG 3456x2234 3,511,527B = 재인코딩 전 원본.

    T-260823-030/034/046 의 Space+C-c 는 입력칸 칩만 지운다. paste 가 pasteboard 를
    다시 읽으면 칩이 되살아나서 세 번 고치고도 남았다. 주입 구간(초점·클리어·paste·
    Enter) 동안 사진 flavor 만 비웠다가 되돌린다. darwin 이외는 no-op.
    """
    parked = None
    if sys.platform == "darwin":
        try:
            parked = _darwin_park_clipboard_png()
        except Exception as exc:  # noqa: BLE001
            print(f"{TUI_LOG_KEY} clipboard park failed: {exc}", file=sys.stderr)
            parked = None
    try:
        yield
    finally:
        if parked:
            try:
                _darwin_restore_clipboard_png(parked)
            except Exception as exc:  # noqa: BLE001
                print(f"{TUI_LOG_KEY} clipboard restore failed: {exc}", file=sys.stderr)


def _tui_clear_composer():
    """직전 턴의 사진 칩이 입력칸에 남는 것을 걷는다 (T-260823-046).

    그록 TUI 는 사진 경로를 [Image #1] 칩으로 붙이고 제출 후에도 칩이 남는다.
    글만 paste 하면 그 칩이 같이 제출된다 (제어 노드 실측: 「안녕하세요 [Image #1]」).

    단축키 SoT (docs/user-guide/03-keyboard-shortcuts.md):
      idle + 비어있지 않은 초안(글 또는 image chips) + **prompt focused**
      → Ctrl+C 1회로 비움.
    초점이 스크롤백에 있으면 C-c 는 칩을 안 지운다 (제어 노드 22:45 「ㅎㅇ」 실측:
    텔레그램은 글만, 제출 버블은 「ㅎㅇ[Image #1]」). Space 는 스크롤백에서
    입력칸으로 초점을 옮긴다. 이미 입력이면 공백 1자가 들어가고 바로 뒤 C-c 가
    그 공백과 칩을 같이 지운다.
    2× Esc 는 빈 칸에서 rewind 를 열므로 쓰지 않는다.

    C-c 만으로는 모자란다. macOS pasteboard 에 스크린샷이 남아 있으면 paste 가
    칩을 다시 붙인다 — `_isolate_os_clipboard_images` (T-260824-006) 가 그 축이다.
    """
    _tmux("send-keys", "-t", TMUX_PANE, "Space")
    time.sleep(TUI_SUBMIT_DELAY)
    _tmux("send-keys", "-t", TMUX_PANE, "C-c")
    time.sleep(TUI_SUBMIT_DELAY)


def _tui_paste(prompt):
    """crb 와 같은 tmux 3단(load-buffer → paste-buffer → 제출키).

    send-keys 로 본문을 직접 타이핑하지 않는 이유는 crb 와 같다 — 여러 줄·특수문자가
    조합키로 해석되는 경로를 아예 안 만든다.

    사진 칩 축은 두 겹이다. 입력칸 잔칩 = Space 뒤 C-c (T-260823-046).
    pasteboard 재주입 = `_isolate_os_clipboard_images` 가 제출키까지 감싼다
    (T-260824-006). 둘 중 하나만 있으면 「안녕하세요 [Image #1]」 가 되살아난다.
    """
    payload = (prompt or "").rstrip("\n")
    if not payload:
        return
    # ★붙여넣기 직전에 「보는 일기장이 맞나」를 확인한다 (T-260824-028).
    #   그록 TUI 가 도중 새 대화로 갈아탔으면 여기서 따라간다 — 조건 4개를 모두
    #   만족할 때만이고, 모호하면 안 따라가고 진단만 남긴다(tui_session_rotation).
    #   발사 ★직전에 두는 이유 = 대기 루프가 열기 전에 맞춰야 그 턴이 회수된다.
    try:
        tui_follow_session_rotation()
    except Exception as exc:  # noqa: BLE001
        print(f"{TUI_LOG_KEY} 세션 추종 점검 실패: {exc}", file=sys.stderr)
    with _isolate_os_clipboard_images():
        _tui_clear_composer()
        _tmux("load-buffer", "-", input_text=payload)
        _tmux("paste-buffer", "-p", "-t", TMUX_PANE)
        time.sleep(TUI_SUBMIT_DELAY)
        _tmux("send-keys", "-t", TMUX_PANE, TUI_SUBMIT_KEY)


def _tui_rescue_after_rotation(prompt, started_at):
    """실패 직전 1회 — 갈린 대화에서 ★이 질문의 답을 건진다 (T-260825-003).

    반환 = (답 문자열 or "", 핀을 옮겼는가).

    ⚠️ 제거 금지 (DO NOT REMOVE) — 발사 직전 추종(_tui_paste)만으로는 이 사고를 못 막는다.
      실사고 2026-08-24 22:17 KST (macOS 노드): 그록 TUI 는 멀쩡히 일해서 22:17 에 조직도 작업을
      끝내고 답까지 썼는데(PR 머지·배포 완료), 브릿지는 하루 전 죽은 일기장
      (핀=05d0ee83…, 마지막기록 8/23 23:17)을 보며 기다렸고 사용자 폰에는 ★2시간 동안
      한 글자도 안 갔다. 00:20 실패 통지에는 「rotated … 살아있는쪽=01a02f49…
      마지막기록 00:18:53」 이 ★정확히 찍혔는데도 핀은 그대로였다 — 아는데 안 옮겼다.
      그래서 다음 질문도 같은 죽은 파일을 보고 또 침묵했을 것이다(수동 개입으로 끊었다).

    ★남의 답을 배달하지 않는다 = 두 겹으로 확인한 뒤에만 건진다.
      ① 살아있는 일기장이 ★이 잡이 시작된 뒤에 쓰였다 (그 전 기록이면 남의 턴이다)
      ② 그 최종답 ★바로 앞의 질문이 내가 보낸 질문이다 (앞머리 대조)
      하나라도 어긋나면 건지지 않고 종전대로 실패를 올린다. ★핀 이동은 그래도 남는다 —
      다음 턴이 살아있는 대화를 보게 하는 것이 회수 실패보다 중요하다.
    """
    if not TUI_RESCUE_ON_ROTATION:
        return "", False
    head = (prompt or "").strip()
    needle = head.splitlines()[0].strip()[:TUI_RESCUE_MATCH_CHARS] if head else ""
    rot = tui_session_rotation()
    if rot["verdict"] != "rotated":
        return "", False
    # ★잡보다 먼저 쓰인 일기장은 이 턴의 증거가 아니다.
    if not rot["live_mtime"] or rot["live_mtime"] < started_at:
        print(
            f"{TUI_LOG_KEY} 갈림 회수 안 함 — 살아있는쪽이 이 잡 시작 뒤에 안 쓰였다",
            file=sys.stderr,
        )
        return "", False
    if not tui_follow_session_rotation():
        return "", False
    if len(needle) < 4:
        return "", True
    try:
        rows = _read_history_rows(tui_history_path())
    except Exception as exc:  # noqa: BLE001
        print(f"{TUI_LOG_KEY} 갈림 회수 — 새 일기장을 못 읽었다: {exc}", file=sys.stderr)
        return "", True
    finals = _tui_final_answer_indices(rows)
    if not finals:
        return "", True
    pos = finals[-1]
    for back in range(pos - 1, -1, -1):
        question = _tui_user_query_text(rows[back])
        if not question:
            continue
        # ★가장 가까운 질문 ★하나만 본다. 더 뒤로 가면 옛 질문에 새 답을 붙인다.
        if needle in question or question.strip()[: len(needle)] == needle:
            print(f"{TUI_LOG_KEY} 갈린 대화에서 답 회수 (핀 이동 후)", file=sys.stderr)
            return str(rows[pos].get("content") or "").strip(), True
        print(
            f"{TUI_LOG_KEY} 갈림 회수 안 함 — 마지막 답 앞의 질문이 내 질문이 아니다",
            file=sys.stderr,
        )
        return "", True
    return "", True


def _tui_wait_for_final(path, baseline, on_progress=None, rescue_prompt=""):
    """baseline 이후 최종답 1개를 기다린다. 없으면 GrokExecError.

    run_grok_tui 와 재기동 회수가 같은 대기 루프를 쓴다 — 잘라내는 기준이
    갈리면 회수 쪽이 답을 버리고 본 루프만 성공하는 구멍이 생긴다.
    """
    _TUI_RESET.clear()
    started = time.time()
    hard_deadline = started + TUI_ANSWER_TIMEOUT
    last_progress = started
    last_emit = started
    seen_rows = 0
    reported = []
    stop_reason = None
    last_progress_line = None
    # on_progress 가 돌려주는 배달 모드. "anchor" = 말풍선 1통을 고쳐 쓰는 중,
    #   "message" = 새 말풍선 경로(앵커 실패·기능 OFF), "" = 아직 한 통도 안 보냄.
    progress_mode = ""

    def _emit_progress():
        nonlocal last_emit, last_progress_line, progress_mode
        if not on_progress:
            return
        now = time.time()
        # dedup 기준선은 ★시계를 뺀 문장이다 — 시계를 넣으면 매번 달라져 dedup 이 죽는다.
        base = _tui_progress_line(reported)
        use_anchor = bool(TUI_PROGRESS_ANCHOR) and progress_mode != "message"
        line = _tui_progress_line(reported, now - started) if use_anchor else base
        # T-260823-001: 같은 문장을 1분마다 다시 보내지 않는다.
        #   직전 발신과 같으면 타이머만 돌리고 폰에는 안 찍는다. 행동이 바뀌면 1통.
        #   ★앵커 편집 경로는 예외다 (T-260824-042): 편집은 말풍선을 안 늘리고 알림도
        #     안 띄우므로, 같은 단계여도 시계를 갱신하는 편이 「살아있다」의 증거다.
        if not use_anchor and base == last_progress_line:
            last_emit = now
            return
        try:
            progress_mode = on_progress(line) or "message"
            last_progress_line = base
        except Exception as exc:  # noqa: BLE001
            print(f"{TUI_LOG_KEY} 진행 발신 실패: {exc}", file=sys.stderr)
        last_emit = time.time()

    def _progress_interval():
        """첫 통까지는 조용히(60s), 앵커가 생긴 뒤엔 촘촘히(30s) 고쳐 쓴다."""
        if progress_mode == "anchor":
            return TUI_PROGRESS_EDIT_INTERVAL
        return TUI_PROGRESS_INTERVAL

    while True:
        if _TUI_RESET.is_set():
            raise GrokExecError("/clear 로 레인을 되세웠다")
        rows, baseline = _tui_rebaseline_if_compacted(path, baseline)
        fresh = rows[baseline:]
        # ★진전 신호 = 히스토리 행 증가. reasoning·tool_call·tool_result 전부 포함한다
        #   (최종답변 행만 세면 도구를 오래 쓰는 턴이 「무진전」으로 잘린다 — 그게 원 증상이다).
        #   이미 매 폴에서 읽던 값이라 추가 비용 0.
        if len(fresh) > seen_rows:
            seen_rows = len(fresh)
            last_progress = time.time()
        for name in _tui_tool_call_names(fresh):
            if name not in reported:
                reported.append(name)
                # T-260821-039: 종전 문구는 "(거부 기대)" 였다. 도구가 열린 지금은 정반대라
                #   로그가 진단을 거꾸로 이끈다(실측: 개방 검증 왕복에서 성공한 3종이
                #   전부 "거부 기대" 로 찍혔다). 판정엔 무영향이고 문구만 바로잡는다.
                print(f"{TUI_LOG_KEY} 도구 사용: {name}", file=sys.stderr)
        finals = _tui_final_answer_rows(fresh)
        if finals:
            return str(finals[-1].get("content") or "").strip()
        now = time.time()
        if on_progress and now - last_emit >= _progress_interval():
            _emit_progress()
        # ★조용한 실패가 제일 나쁘다 — 왜 잘렸는지를 문구로 가른다. 사용자 폰에 그대로 뜬다.
        #   ★단, 이 턴에서 도구를 이미 썼으면 침묵은 생각·다음 도구 대기다. 실패로 뒤집으면
        #     typing 이 꺼지고 폰엔 「고장난 줄」만 남는다(T-260822-068, 22:02~22:28 26분 무신호).
        if now - last_progress >= TUI_IDLE_TIMEOUT and not _tui_tool_in_flight(fresh):
            if reported:
                last_progress = now
                _emit_progress()
            else:
                stop_reason = f"{TUI_IDLE_TIMEOUT}s 동안 새 출력이 없었다(무진전)"
                break
        if now >= hard_deadline:
            stop_reason = (
                f"총 상한 {TUI_ANSWER_TIMEOUT}s 초과 — 계속 움직이는데 답이 안 끝났다"
            )
            break
        time.sleep(TUI_POLL_INTERVAL)

    # T-260823-007: 잘린 직후 TUI 가 답을 마무리하면 그 답을 버린 채 다음 붙여넣기를
    #   하지 않는다.
    # T-260825-003: 창의 정본은 유예. max(유예, 총상한) 은 무진전 컷 뒤에도 2시간을
    #   더 쥐었다. 총상한으로 잘렸고 진전이 있으면(T-260823-048) 유예 창을 진전마다
    #   다시 연다. GRB_TUI_HARVEST_FOLLOW=0 이면 종전 max(유예, 총상한).
    if TUI_LATE_HARVEST_GRACE > 0:
        follow = bool(TUI_HARVEST_FOLLOW) and stop_reason and "총 상한" in stop_reason
        if TUI_HARVEST_FOLLOW:
            harvest_until = time.time() + TUI_LATE_HARVEST_GRACE
        else:
            harvest_until = time.time() + max(
                TUI_LATE_HARVEST_GRACE, TUI_ANSWER_TIMEOUT
            )
        seen_harvest = seen_rows
        while time.time() < harvest_until:
            if _TUI_RESET.is_set():
                raise GrokExecError("/clear 로 레인을 되세웠다")
            rows, baseline = _tui_rebaseline_if_compacted(path, baseline)
            fresh = rows[baseline:]
            if follow and (
                len(fresh) > seen_harvest or _tui_tool_in_flight(fresh)
            ):
                seen_harvest = max(seen_harvest, len(fresh))
                harvest_until = time.time() + TUI_LATE_HARVEST_GRACE
            finals = _tui_final_answer_rows(fresh)
            if finals:
                print(
                    f"{TUI_LOG_KEY} 늦은 회수 ({stop_reason})",
                    file=sys.stderr,
                )
                return str(finals[-1].get("content") or "").strip()
            now = time.time()
            if on_progress and now - last_emit >= _progress_interval():
                _emit_progress()
            time.sleep(TUI_POLL_INTERVAL)

    suffix = f" (도구 시도: {', '.join(reported[:3])})" if reported else ""
    # ★조용히 자르지 않는다 (T-260824-028). 이 사고의 진짜 피해는 「잘린 것」이 아니라
    #   「왜 잘렸는지 아무도 모른 것」이었다. 답이 없어 보이면 ★보던 일기장부터 의심한다.
    #   실사고: 터미널은 27초 만에 답 완성, 폰엔 「180s 무진전」 — 다른 파일을 보고 있었다.
    diag = ""
    try:
        diag = tui_rotation_diagnosis()
    except Exception as exc:  # noqa: BLE001
        diag = f"(세션 갈림 판정 실패: {exc})"
    # ★진단이 「갈렸다」를 말하는 ★그 자리에서 핀을 옮기고 답을 건진다 (T-260825-003).
    #   종전은 갈림을 문구로만 알리고 끝나서, 답이 살아있는 일기장에 멀쩡히 적혀 있어도
    #   버려졌고 ★다음 턴도 같은 죽은 파일을 봤다.
    moved = False
    if rescue_prompt:
        try:
            rescued, moved = _tui_rescue_after_rotation(rescue_prompt, started)
        except Exception as exc:  # noqa: BLE001
            rescued = ""
            print(f"{TUI_LOG_KEY} 갈림 회수 실패: {exc}", file=sys.stderr)
        if rescued:
            return rescued
    if diag:
        diag = " · " + diag
    if moved:
        diag += " · ★핀을 살아있는 대화로 옮겼다 — 다시 물어보면 그쪽에서 답한다"
    raise GrokExecError(f"tmux 세션에서 답이 안 왔다 — {stop_reason}{suffix}{diag}")


def run_grok_tui(prompt, on_progress=None, source=None):
    """상주 TUI 에 주입하고 세션 JSONL 에서 새 최종답변을 읽는다.

    ★화면(capture-pane)은 읽지 않는다 — crb 가 세운 계약이고, 화면은 렌더 폭·스크롤·
    스피너에 따라 같은 답이 달라 보인다.

    ★도구 시도(tool_calls)를 만나도 답이 오면 답을 배달한다. TUI 의 차단은 제거가 아니라
    거부라 시도 자체는 정상적으로 날 수 있기 때문이다 — 시도를 이유로 답을 버리면 그건
    유실이다. 대신 grep 가능한 키로 남기고, 굶은 채 끝나면 그 이름을 진단에 싣는다.
    """
    if not tui_session_alive():
        raise GrokExecError(tui_dead_message())
    session_id = tui_session_id()
    if not session_id:
        raise GrokExecError(
            f"TUI 세션 uuid 가 없다({TUI_SESSION_ID_FILE}) — {TUI_LAUNCHER} 로 기동해라"
        )

    path = tui_history_path()
    baseline = len(_read_history_rows(path))
    # 붙여넣기 전에 inflight 를 남긴다. 재기동이 이 턴을 회수하는 근거(T-260823-051).
    _tui_inflight_write(
        {
            "session_id": session_id,
            "baseline": baseline,
            "source": source or _JOB_SOURCE or "telegram",
            "preview": (prompt or "").strip().splitlines()[0][:80] if prompt else "",
        }
    )
    _tui_paste(prompt)
    # rescue_prompt = 실패했을 때 「이 답이 ★내 질문의 답인가」를 대조할 원문 (T-260825-003).
    answer = _tui_wait_for_final(
        path, baseline, on_progress=on_progress, rescue_prompt=prompt
    )
    return answer, None, session_id


def _remember_session(chat_id, returned_sid):
    if returned_sid:
        write_session_uuid(chat_id, returned_sid)


def _execute_with_session(chat_id, prompt, on_progress=None):
    if CHAT_LANE == "tui":
        if tui_session_alive():
            answer, cost_usd, _session_id = run_grok_tui(prompt, on_progress=on_progress)
            return answer, cost_usd
        if not TUI_FALLBACK_HEADLESS:
            # ★fail-closed 가 기본이다. 보이는 세션이 죽었는데 조용히 안 보이는 레인으로
            #   답하면 사용자 화면엔 답이 오고 세션은 죽은 채 남는다 — 그게 은폐다.
            raise GrokExecError(tui_dead_message())
        print(
            f"{TUI_LOG_KEY} tmux 세션 부재 — headless 폴백(GRB_TUI_FALLBACK_HEADLESS opt-in)",
            file=sys.stderr,
        )

    stored = read_session_uuid(chat_id)
    if stored:
        try:
            answer, cost_usd, returned_sid = run_grok(prompt, stored)
            _remember_session(chat_id, returned_sid or stored)
            return answer, cost_usd
        except GrokExecError as exc:
            # resume 실패 시 다른 가짜 uuid 로 다시 -s 하지 않는다 (T-260819-004).
            # 그 경로는 180초 침묵 후 같은 실패를 반복한다.
            print(f"grok resume 실패, 세션 플래그 없이 신규: {exc}", file=sys.stderr)
            clear_session_uuid(chat_id)
    answer, cost_usd, returned_sid = run_grok(prompt, None)
    _remember_session(chat_id, returned_sid)
    return answer, cost_usd


SUGGESTED_CALLBACK_PREFIX = "grb-sr"
SUGGESTED_BUTTON_TEXT = "확인"
SUGGESTED_SENT_BUTTON_TEXT = "✅ 보냄"
SUGGESTED_DONE_CALLBACK = f"{SUGGESTED_CALLBACK_PREFIX}:done"
SUGGESTED_STORE_FILE = os.path.join(STATE_DIR, f"grok-bridge-{NAME}.suggested.json")
SUGGESTED_STORE_MAX = 40


def _read_suggested_store():
    try:
        with open(SUGGESTED_STORE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_suggested_store(store):
    tmp = SUGGESTED_STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, SUGGESTED_STORE_FILE)


def register_suggested_reply(text):
    """문구를 짧은 id 로 저장한다. Telegram callback_data 는 64바이트 한도."""
    phrase = (text or "").strip()
    if not phrase:
        return ""
    store = _read_suggested_store()
    cid = uuid.uuid4().hex[:12]
    store[cid] = {"text": phrase, "ts": time.time()}
    if len(store) > SUGGESTED_STORE_MAX:
        ordered = sorted(store.items(), key=lambda item: float((item[1] or {}).get("ts") or 0))
        for old_id, _ in ordered[: len(store) - SUGGESTED_STORE_MAX]:
            store.pop(old_id, None)
    _write_suggested_store(store)
    return cid


def remember_suggested_message_id(cid, message_id):
    """버블 발신 후 message_id 를 붙여 둔다. 콜백에 message 가 비어도 버튼을 바꿀 수 있다."""
    key = str(cid or "")
    if not key or not message_id:
        return
    store = _read_suggested_store()
    rec = store.get(key)
    if not isinstance(rec, dict):
        return
    try:
        rec["message_id"] = int(message_id)
    except (TypeError, ValueError):
        return
    store[key] = rec
    _write_suggested_store(store)


def take_suggested_reply(cid):
    """한 번 쓰면 지운다. (문구, 저장해 둔 message_id) 를 돌려준다."""
    key = str(cid or "")
    if not key:
        return "", None
    store = _read_suggested_store()
    rec = store.pop(key, None)
    if rec is not None:
        _write_suggested_store(store)
    if isinstance(rec, dict):
        mid = rec.get("message_id")
        return str(rec.get("text") or "").strip(), mid
    return str(rec or "").strip(), None


def suggested_confirm_markup(cid):
    if not cid:
        return ""
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": SUGGESTED_BUTTON_TEXT, "callback_data": f"{SUGGESTED_CALLBACK_PREFIX}:{cid}"}]
            ]
        },
        ensure_ascii=False,
    )


def suggested_sent_markup():
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": SUGGESTED_SENT_BUTTON_TEXT, "callback_data": SUGGESTED_DONE_CALLBACK}]
            ]
        },
        ensure_ascii=False,
    )


def mark_suggested_pressed(chat_id, message_id):
    """눌렀다는 표시 — 버튼을 ✅ 보냄 으로 바꾼다. 실패해도 입력은 이미 넣는다."""
    if not chat_id or not message_id:
        return
    try:
        res = tg(
            "editMessageReplyMarkup",
            timeout=10,
            chat_id=chat_id,
            message_id=int(message_id),
            reply_markup=suggested_sent_markup(),
        )
        if not res or not res.get("ok"):
            print(f"suggested button mark 실패: {res}", file=sys.stderr)
    except (TypeError, ValueError, Exception) as exc:  # noqa: BLE001
        print(f"suggested button mark 실패: {exc}", file=sys.stderr)


def handle_telegram_callback(callback):
    """추천답변 확인 버튼. 문구를 그대로 다음 입력으로 넣는다."""
    cb = callback if isinstance(callback, dict) else {}
    qid = str(cb.get("id") or "")
    message = cb.get("message") if isinstance(cb.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}

    def answer(text=""):
        if not qid:
            return
        params = {"callback_query_id": qid}
        if text:
            params["text"] = text
        tg("answerCallbackQuery", timeout=10, **params)

    if str(chat.get("id")) != str(CHAT_ID):
        answer("이 채팅의 버튼이 아니야")
        return
    data = str(cb.get("data") or "")
    prefix = f"{SUGGESTED_CALLBACK_PREFIX}:"
    if not data.startswith(prefix):
        answer("알 수 없는 버튼이야")
        return
    if data == SUGGESTED_DONE_CALLBACK:
        answer()
        return
    phrase, stored_mid = take_suggested_reply(data[len(prefix) :])
    mid = message.get("message_id") or stored_mid
    if not phrase:
        answer()
        mark_suggested_pressed(CHAT_ID, mid)
        return
    answer()
    mark_suggested_pressed(CHAT_ID, mid)
    inbox_spool(cb.get("id"), "telegram", phrase)
    print(f">>> [{NAME}] 텔레그램 grok 확인버튼: {phrase[:80]}", flush=True)
    handle_message_text(phrase, source="telegram")


TG_CHUNK = int_env("GRB_TG_CHUNK", 4096, minimum=1)


def _tg_chunks(text, limit):
    """Telegram caps one message at 4096 characters. Split on line ends when
    a line end is available, so a long answer does not break mid-sentence."""
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        yield remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        yield remaining


def deliver_mesh_event(kind, body, *, task_id=None, visibility=None, reply_markup=None, telegram_method=None, message_id=None, telegram_code_entity=None):
    """Send one message straight to the Telegram Bot API.

    The maintainer's internal build routes delivery through a private message
    bus; this public build has no such bus, so delivery is a direct
    `sendMessage` call. The returned dict keeps the delivery-list shape the
    callers read, so nothing downstream has to know which path was taken.

    kind/task_id/visibility are accepted and ignored here - they are routing
    hints for the bus, and there is no bus to route to. reply_markup is
    forwarded on the first chunk only, so a confirm button can sit on the
    first message of a long answer.

    telegram_method="editMessageText" rewrites one existing message instead of
    posting a new one - that is how the progress line updates in place rather
    than stacking a new bubble every minute. An edit is never chunked: a
    message already has a fixed identity, so it is truncated to the API cap.

    telegram_code_entity marks a copy-paste command bubble. The public build
    turns that into a Telegram `pre` entity so the phone shows a copy button.
    """
    payload = str(body or "").strip()
    if not payload:
        return {"deliveries": []}
    if telegram_method == "editMessageText" and message_id:
        res = tg(
            "editMessageText",
            chat_id=CHAT_ID,
            message_id=message_id,
            text=payload[:TG_CHUNK],
        )
        if not res or not res.get("ok"):
            print(f"telegram editMessageText 실패: {res}", file=sys.stderr)
            return {"deliveries": [{"surface": SEND_SURFACE, "result": "failed"}]}
        return {
            "deliveries": [
                {"surface": SEND_SURFACE, "result": "sent", "message_id": message_id}
            ]
        }
    deliveries = []
    first = True
    for chunk in _tg_chunks(payload, TG_CHUNK):
        extra = {}
        if first and reply_markup:
            extra["reply_markup"] = reply_markup
        if telegram_code_entity:
            extra["entities"] = json.dumps(
                [{"type": "pre", "offset": 0, "length": len(chunk.encode("utf-16-le")) // 2}]
            )
        first = False
        res = tg("sendMessage", chat_id=CHAT_ID, text=chunk, **extra)
        if not res or not res.get("ok"):
            print(f"telegram sendMessage 실패: {res}", file=sys.stderr)
            deliveries.append({"surface": SEND_SURFACE, "result": "failed"})
            continue
        deliveries.append(
            {
                "surface": SEND_SURFACE,
                "result": "sent",
                "message_id": (res.get("result") or {}).get("message_id"),
            }
        )
    return {"deliveries": deliveries}


SEND_SURFACE = "direct"


def _first_sent_message_id(mesh_result, surface=SEND_SURFACE):
    for delivery in (mesh_result or {}).get("deliveries", []):
        if delivery.get("surface") == surface and delivery.get("result") == "sent":
            mid = delivery.get("message_id")
            if mid:
                return mid
    return None


# 편집이 「먹었다」로 볼 결과 (T-260824-042). skipped_unchanged 를 실패로 읽으면
#   같은 단계가 이어지는 턴마다 앵커를 버리고 새 말풍선을 띄운다 — 고치려던 증상 그대로다.
#   발신 경로는 변화 없는 편집을 API 호출 전에 접고 이 결과를 돌려준다.
_ANCHOR_EDIT_OK = frozenset({"sent", "skipped_unchanged"})


def _tui_progress_anchor_edit(message_id, text, task_id=None, surface=SEND_SURFACE):
    """앵커 말풍선 1통을 고쳐 쓴다. 실패면 False — 호출부가 앵커를 버린다.

    실패하는 실경로가 있다: 사용자가 그 말풍선을 지웠거나, 48시간이 지나 편집이
    막혔거나, 발신 예산·flood 쿨다운에 걸린 경우. 그때 조용히 멈추면 폰은 다시
    「멈춘 것과 구분 안 되는 화면」이 되므로, 앵커를 버리고 새 통을 띄우게 한다.
    """
    result = deliver_mesh_event(
        "report",
        text,
        task_id=task_id,
        telegram_method="editMessageText",
        message_id=message_id,
    )
    for delivery in (result or {}).get("deliveries", []):
        if delivery.get("surface") == surface and delivery.get("result") in _ANCHOR_EDIT_OK:
            return True
    return False


SUGGESTED_SURFACE = SEND_SURFACE
SUGGESTED_SPLIT_LOG_KEY = "grb_suggested_split"
SUGGESTED_OPEN = "<" + "추천답변" + ">"
SUGGESTED_CLOSE = "</" + "추천답변" + ">"


def split_suggested_reply(text):
    """Split a trailing suggestion marker into (body, suggestion).

    Only a marker at the very end of the answer is a suggestion. A marker in
    the middle of the prose is left in the body. The maintainer private
    parser has more repair rules; this public copy only implements the tail
    split, which is what the confirm-button bubble needs.
    """
    raw = text or ""
    stripped = raw.rstrip()
    if not stripped.endswith(SUGGESTED_CLOSE):
        return raw, ""
    open_at = stripped.rfind(SUGGESTED_OPEN)
    close_at = stripped.rfind(SUGGESTED_CLOSE)
    if open_at < 0 or close_at < 0 or open_at > close_at:
        return raw, ""
    if stripped[close_at + len(SUGGESTED_CLOSE):].strip():
        return raw, ""
    reply = stripped[open_at + len(SUGGESTED_OPEN):close_at].strip()
    body = stripped[:open_at].rstrip()
    if not reply:
        return raw, ""
    if not body:
        return raw, ""
    return body, reply


def split_copy_content(text):
    """Public build keeps commands in the prose.

    The maintainer build splits copy-paste bubbles with the shared parser
    from the sister bridge. That file is not in this package, so this public
    copy does not split commands.
    """
    return text or "", []


def mirror_answer(source, text, task_id=None):
    """본문 1통 + (있으면) 명령 복붙 버블 N통 + (있으면) 추천답변 버블 1통.

    발신 순서 = R-C8 4항: 남은 산문 → 복붙 버블 → 추천답변 버블.
    명령 버블만 telegram_code_entity=true (복사 버튼). 추천답변 버블은 같은
    kind=copy_content 여도 이 플래그가 없다 — 확인 버튼 문구이지 명령이 아니다.
    ★kind 가 copy_content 인 이유 = clb 의 버블도 send_copy_content 경로로
    나간다. R-C4 가 그 kind 에 "헤더·구분선·백틱·꼬리말 없이 원문 그대로" 를
    걸어 두므로 장식 없이 착지한다. mesh_group 은 억제라 그룹 로그에 '끝' 이
    두 줄 찍히지 않는다.
    """
    local_print(f"grok answer ({source}):\n{text}")
    body, suggested = (
        split_suggested_reply(text) if SUGGESTED_REPLY_SPLIT else (text, "")
    )
    body, copy_bubbles = split_copy_content(body)
    if body or not copy_bubbles:
        deliver_mesh_event("final", body, task_id=task_id)
    for bubble in copy_bubbles:
        deliver_mesh_event(
            "copy_content",
            bubble,
            task_id=task_id,
            telegram_code_entity=True,
        )
    if CHAT_LANE == "tui":
        sid = (tui_session_id() or "").strip()
        if sid:
            n = len(_tui_final_answer_rows(_read_history_rows(tui_history_path())))
            _tui_cursor_save(sid, n)
    if not suggested:
        # R-C9: 유효한 bubble ID 가 없으면 리액션도 만들지 않는다. 종전엔 버블이
        # 아예 없던 시절이라 본문 첫 chunk 에 👀 를 붙였는데, 그 자리는 규격이
        # 지정한 대상이 아니다(대상 = 추천답변 버블의 첫 message_id).
        return
    cid = register_suggested_reply(suggested)
    bubble = deliver_mesh_event(
        "copy_content",
        suggested,
        task_id=task_id,
        reply_markup=suggested_confirm_markup(cid),
    )
    remember_suggested_message_id(cid, _first_sent_message_id(bubble))
    set_eyes_reaction(CHAT_ID, _first_sent_message_id(bubble))


def mirror_local_tui_turns():
    """커서 이후의 최종답을, 그 답을 부른 질문과 함께 폰으로 올린다 (T-260824-036).

    ★커서가 이 세션 것이 아니면 현재 개수로 baseline 만 잡고 0 을 돌려준다 — 켠 순간
      과거 일기장이 통째로 폰에 쏟아지는 걸 막는다(harvest_orphaned_tui_finals 와 같은 계약).
    """
    if CHAT_LANE != "tui" or not TUI_MIRROR_LOCAL:
        return 0
    sid = (tui_session_id() or "").strip()
    if not sid:
        return 0
    rows = _read_history_rows(tui_history_path())
    finals = _tui_final_answer_indices(rows)
    cur = _tui_cursor_load()
    if cur.get("session_id") != sid:
        _tui_cursor_save(sid, len(finals))
        return 0
    try:
        already = int(cur.get("finals_sent") or 0)
    except (TypeError, ValueError):
        already = 0
    sent = 0
    for pos in finals[_tui_cursor_clamp(already, len(finals)) :]:
        answer = str(rows[pos].get("content") or "").strip()
        if not answer:
            continue
        question = ""
        for back in range(pos - 1, -1, -1):
            question = _tui_user_query_text(rows[back])
            if question:
                break
        if question:
            head = question[:TUI_MIRROR_LOCAL_PROMPT_MAX]
            if len(question) > TUI_MIRROR_LOCAL_PROMPT_MAX:
                head += " …"
            deliver_mesh_event("report", f"터미널에서 물어본 것 — {head}")
        print(f"{TUI_LOG_KEY} local mirror 배달", file=sys.stderr)
        mirror_answer(TUI_MIRROR_LOCAL_SOURCE, answer)
        sent += 1
    # 보낼 게 0 건이었던 tick 도 커서를 전진시킨다 — 안 그러면 매번 같은 자리를 다시 읽는다.
    _tui_cursor_save(sid, len(finals))
    return sent


def tui_local_mirror_ticker():
    """일기장을 주기적으로 훔쳐본다. 폰발 턴이 도는 동안엔 비켜선다.

    ★한 번의 예외로 스레드가 죽으면 그 뒤로는 조용히 아무것도 안 미러한다 — 침묵
      실패가 제일 나쁘다(원칙 6). 예외는 찍고 다음 tick 으로 넘어간다.
    """
    while True:
        time.sleep(TUI_MIRROR_LOCAL_INTERVAL)
        try:
            if _TUI_JOB_ACTIVE.is_set() or not GROK_LOCK.acquire(blocking=False):
                continue
            try:
                tui_follow_session_rotation()
                sent = mirror_local_tui_turns()
            finally:
                GROK_LOCK.release()
            if sent:
                print(f"{TUI_LOG_KEY} local mirror {sent}건", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"{TUI_LOG_KEY} local mirror 실패: {exc}", file=sys.stderr)


def mirror_error(source, text, task_id=None):
    msg = f"grok 호출 실패: {text}"
    local_print(f"{msg} ({source})")
    deliver_mesh_event("error", msg, task_id=task_id)


def process_job(source, text, task_id=None):
    if CHAT_LANE == "tui" and slash_token(text) in TUI_RESET_TOKENS:
        handle_tui_reset(source, task_id=task_id)
        return
    mirror_prompt(source, text)
    started = time.time()
    typing_stop = start_typing()
    progress_sent = 0
    # ── 진행 앵커 (T-260824-042) ────────────────────────────────────────────────
    # 말풍선 1통을 잡아 두고 그 통만 고쳐 쓴다. dict 로 드는 이유 = 아래 두 클로저가
    #   같은 상태를 읽고 쓴다(nonlocal 3개보다 이쪽이 읽힌다).
    anchor = {"message_id": None, "edits": 0}

    def on_progress(msg):
        """진행 1회. 돌려주는 값 = 배달 모드("anchor"/"message") — 대기 루프가 이걸 보고
        다음 갱신 간격을 고른다(앵커면 촘촘히, 새 말풍선이면 종전 그대로)."""
        nonlocal progress_sent
        if not msg:
            return ""
        if anchor["message_id"] and TUI_PROGRESS_ANCHOR:
            if anchor["edits"] >= TUI_PROGRESS_EDIT_MAX:
                # 상한 도달. 새 말풍선으로 흘려보내지 않는다 — 폭주 방지가 목적이므로
                # 조용히 멈추되 앵커 자체는 유지해 마감 한 줄은 찍히게 둔다.
                return "anchor"
            if _tui_progress_anchor_edit(anchor["message_id"], msg, task_id=task_id):
                anchor["edits"] += 1
                return "anchor"
            # 편집이 안 먹었다(원문 삭제·48h 초과·쿨다운). 앵커를 버리고 새로 띄운다.
            print(f"{TUI_LOG_KEY} 진행 앵커 편집 실패 — 새 말풍선으로 되돌린다", file=sys.stderr)
            anchor["message_id"] = None
        if progress_sent >= TUI_PROGRESS_MAX:
            return "message"
        progress_sent += 1
        result = deliver_mesh_event("report", msg, task_id=task_id)
        if TUI_PROGRESS_ANCHOR:
            mid = _first_sent_message_id(result)
            if mid:
                anchor["message_id"] = mid
                anchor["edits"] = 0
                return "anchor"
        return "message"

    def close_anchor(ok, reset=False):
        """앵커를 마감한다. 편집이 실패해도 답 배달은 그대로 간다(non-fatal).

        성공·실패·예외 어느 갈래로 끝나든 부른다 — 안 부르면 답이 온 뒤에도 위에
        「아직 하고 있어」가 남아 화면이 거짓말을 한다(원칙 6).
        """
        if not anchor["message_id"] or not TUI_PROGRESS_ANCHOR:
            return
        try:
            _tui_progress_anchor_edit(
                anchor["message_id"],
                _tui_progress_done_line(time.time() - started, ok=ok, reset=reset),
                task_id=task_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{TUI_LOG_KEY} 진행 앵커 마감 실패: {exc}", file=sys.stderr)
        anchor["message_id"] = None

    outcome = {"ok": False, "reset": False}
    try:
        with GROK_LOCK:
            global _JOB_SOURCE
            _JOB_SOURCE = source or "telegram"
            answer, _cost_usd = _execute_with_session(
                CHAT_ID, text, on_progress=on_progress
            )
        outcome["ok"] = True
    except GrokExecError as exc:
        if "레인을 되세웠" in str(exc):
            print(f"{TUI_LOG_KEY} /clear 가 물린 잡을 접었다", file=sys.stderr)
            outcome["reset"] = True
            return
        mirror_error(source, str(exc), task_id=task_id)
        return
    except Exception as exc:  # noqa: BLE001
        print(f"grok bridge 처리 실패: {exc}", file=sys.stderr)
        mirror_error(source, "내부 오류", task_id=task_id)
        return
    finally:
        typing_stop.set()
        # ★return 이 먼저 도는 갈래(에러 2종)에서도 앵커는 닫힌다 — finally 라서.
        close_anchor(outcome["ok"], reset=outcome["reset"])
    mirror_answer(source, answer, task_id=task_id)
    _tui_inflight_clear()


def job_worker():
    try:
        if CHAT_LANE == "tui":
            _tui_wait_inflight_if_any()
            harvested = harvest_orphaned_tui_finals()
            if harvested:
                print(f"{TUI_LOG_KEY} startup harvest {harvested}건", file=sys.stderr)
            _tui_inflight_clear()
    except Exception as exc:  # noqa: BLE001
        print(f"{TUI_LOG_KEY} startup harvest 실패: {exc}", file=sys.stderr)
    while True:
        job = JOBS.get()
        health_mark(last_job_started_at=time.time())
        try:
            source, text = job
            # 미러는 이 플래그가 내려갈 때까지 비켜선다 (T-260824-036). process_job 은
            # GROK_LOCK 을 놓은 뒤에 답을 배달·커서 갱신하므로 락만으로는 창이 남는다.
            _TUI_JOB_ACTIVE.set()
            process_job(source, text)
        except Exception as exc:  # noqa: BLE001
            print(f"grok bridge worker 실패: {exc}", file=sys.stderr)
        finally:
            _TUI_JOB_ACTIVE.clear()
            JOBS.task_done()
            health_mark(last_job_done_at=time.time(), done=1)



def handle_message_text(text, source="telegram"):
    text = (text or "").strip()
    if not text:
        return

    if text.lower() in ("/start", "/ping"):
        msg = "grok 헤드리스 모드 작동중"
        local_print(msg)
        deliver_mesh_event("ack", msg)
        return

    if CHAT_LANE == "tui" and slash_token(text) in TUI_RESET_TOKENS:
        handle_tui_reset(source)
        return

    # 자비스(시리) 음성 입력 가시화 (T-260827-024): fifo 로 들어온 [VOICE] 입력은
    # 텔레그램 발화가 아니라 질문이 폰에 안 남는다 — enqueue 즉시 원문을 챗에 미러.
    # telegram 발 [VOICE] 는 이미 사용자 말풍선이 있으므로 미러하지 않는다.
    if source == "local" and text.startswith(VOICE_PROMPT_PREFIX):
        deliver_mesh_event("final", f"음성 입력: {text[len(VOICE_PROMPT_PREFIX):]}")

    JOBS.put((source, text))
    health_mark(last_enqueue_at=time.time(), enqueued=1)


def safe_filename_part(value):
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(value or ""))
    return cleaned.strip("-")[:80] or "file"


def suffix_from_metadata(file_name="", mime_type="", default=".bin"):
    suffix = os.path.splitext(file_name or "")[1].lower()
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed.lower() if guessed else default


def format_metadata(metadata):
    parts = []
    for key, value in (metadata or {}).items():
        if value in (None, "", [], {}):
            continue
        parts.append(f"{key}={value}")
    return "; ".join(parts)


def media_output_dir():
    return os.path.join(STATE_DIR, "grok-telegram-bridge-media", NAME)


def download_file(file_id, output_dir, name_hint, default_suffix=".bin", allowed_extensions=None):
    """Telegram getFile → 로컬 저장. clb download_file 과 같은 청크·재시도 계약."""
    payload = tg("getFile", file_id=file_id)
    if not payload or not payload.get("ok") or not isinstance(payload.get("result"), dict):
        raise RuntimeError("Telegram getFile failed")
    file_path = str(payload["result"].get("file_path") or "")
    if not file_path:
        raise RuntimeError("Telegram getFile returned empty file_path")

    suffix = os.path.splitext(file_path)[1].lower() or default_suffix
    if allowed_extensions is not None and suffix not in allowed_extensions:
        suffix = default_suffix
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{safe_filename_part(name_hint)}{suffix}")

    quoted_path = urllib.parse.quote(file_path, safe="/")
    request = urllib.request.Request(f"https://api.telegram.org/file/bot{TOKEN}/{quoted_path}")
    last_err = None
    for attempt in range(3):
        try:
            buf = bytearray()
            with urllib.request.urlopen(request, timeout=DOWNLOAD_ATTEMPT_TIMEOUT) as response:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    buf.extend(chunk)
            with open(output_path, "wb") as fh:
                fh.write(bytes(buf))
            return output_path
        except (OSError, http.client.HTTPException) as err:
            last_err = err
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"file download failed after 3 attempts: {last_err}")


def transcribe_audio(media_path):
    template = AUDIO_TRANSCRIBE_CMD
    if not template:
        return "", "not_available: set GRB_AUDIO_TRANSCRIBE_CMD to enable audio transcription"
    quoted_path = shlex.quote(str(media_path))
    cmd = template.replace("{path}", quoted_path) if "{path}" in template else f"{template} {quoted_path}"
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=AUDIO_TRANSCRIBE_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return "", f"failed: transcription timed out after {AUDIO_TRANSCRIBE_TIMEOUT}s"
    except OSError as exc:
        return "", f"failed: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        suffix = f": {detail[-1][:200]}" if detail else ""
        return "", f"failed: transcription command rc={proc.returncode}{suffix}"
    transcript = (proc.stdout or "").strip()
    if not transcript:
        return "", "failed: transcription command returned empty stdout"
    return transcript[:12000], "ok"


def image_prompt_text(caption_text, image_path, metadata):
    lines = [
        "[Telegram image received]",
        f"local_path: {image_path}",
    ]
    if caption_text:
        lines.append(f"caption: {caption_text}")
    metadata_line = format_metadata(metadata)
    if metadata_line:
        lines.append(f"metadata: {metadata_line}")
    lines.extend(
        [
            "",
            "Open the local image path, inspect it, and answer the Telegram user in Korean. "
            "Keep the answer concise and useful.",
        ]
    )
    return "\n".join(lines)


def audio_prompt_text(media_kind, caption_text, media_path, metadata, transcript, transcript_status):
    lines = [
        "[Telegram audio received]",
        f"local_path: {media_path}",
        f"media_kind: {media_kind}",
    ]
    if caption_text:
        lines.append(f"caption: {caption_text}")
    metadata_line = format_metadata(metadata)
    if metadata_line:
        lines.append(f"metadata: {metadata_line}")
    lines.append("")
    if transcript:
        lines.extend(["transcript:", transcript])
    else:
        lines.append(f"transcript_status: {transcript_status}")
    lines.extend(
        [
            "",
            "Answer the Telegram user in Korean. If transcript is unavailable, say the audio file "
            "was received and ask for text or GRB_AUDIO_TRANSCRIBE_CMD setup when needed.",
        ]
    )
    return "\n".join(lines)


def video_prompt_text(
    media_kind,
    caption_text,
    media_path,
    metadata,
    thumbnail_path,
    transcript,
    transcript_status,
):
    lines = [
        "[Telegram video received]",
        f"local_path: {media_path}",
        f"media_kind: {media_kind}",
    ]
    if thumbnail_path:
        lines.append(f"thumbnail_path: {thumbnail_path}")
    if caption_text:
        lines.append(f"caption: {caption_text}")
    metadata_line = format_metadata(metadata)
    if metadata_line:
        lines.append(f"metadata: {metadata_line}")
    lines.append("")
    if transcript:
        lines.extend(["audio_transcript:", transcript])
    else:
        lines.append(f"audio_transcript_status: {transcript_status}")
    lines.extend(
        [
            "",
            "Open thumbnail_path with the local image tool if present. Answer the Telegram user "
            "in Korean based on the local video path, thumbnail, caption, metadata, and transcript. "
            "If the video cannot be inspected directly, state that limitation briefly.",
        ]
    )
    return "\n".join(lines)


def document_prompt_text(caption_text, media_path, metadata):
    lines = [
        "[Telegram file received]",
        f"local_path: {media_path}",
        "media_kind: document",
    ]
    if caption_text:
        lines.append(f"caption: {caption_text}")
    metadata_line = format_metadata(metadata)
    if metadata_line:
        lines.append(f"metadata: {metadata_line}")
    lines.extend(
        [
            "",
            "Answer the Telegram user in Korean. Use the local_path and metadata above. "
            "If the file cannot be inspected directly, say that the file was received and "
            "ask for the specific action needed.",
        ]
    )
    return "\n".join(lines)


def download_thumbnail(media, media_dir, update_id):
    thumbnail = media.get("thumbnail") or media.get("thumb")
    if not isinstance(thumbnail, dict) or not thumbnail.get("file_id"):
        return None
    name_hint = f"telegram-video-thumb-{update_id}-{thumbnail.get('file_unique_id') or thumbnail.get('file_id')}"
    try:
        return download_file(
            str(thumbnail["file_id"]),
            media_dir,
            name_hint,
            default_suffix=".jpg",
            allowed_extensions=IMAGE_EXTENSIONS,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"thumbnail download failed: {exc}", file=sys.stderr)
        return None


def _pick_largest_photo(photos):
    candidates = [item for item in photos if isinstance(item, dict) and item.get("file_id")]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            int(item.get("file_size") or 0),
            int(item.get("width") or 0) * int(item.get("height") or 0),
        ),
    )


def prompt_from_telegram_message(message, update_id):
    """글이면 본문, 미디어면 로컬 경로 프롬프트. clb prompt_from_telegram_message 동형(위치공유 제외)."""
    raw_text = message.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text

    caption = message.get("caption")
    caption_text = caption.strip() if isinstance(caption, str) else ""
    dest = media_output_dir()

    try:
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            photo = _pick_largest_photo(photos)
            if photo:
                name_hint = f"telegram-{update_id}-{photo.get('file_unique_id') or photo.get('file_id')}"
                image_path = download_file(
                    str(photo["file_id"]),
                    dest,
                    name_hint,
                    default_suffix=".jpg",
                    allowed_extensions=IMAGE_EXTENSIONS,
                )
                return image_prompt_text(
                    caption_text,
                    image_path,
                    {
                        "width": photo.get("width"),
                        "height": photo.get("height"),
                        "file_size": photo.get("file_size"),
                    },
                )

        document = message.get("document") if isinstance(message.get("document"), dict) else None

        if document and str(document.get("mime_type") or "").startswith("image/"):
            file_id = str(document.get("file_id") or "")
            if file_id:
                name_hint = f"telegram-{update_id}-{document.get('file_unique_id') or file_id}"
                default_suffix = suffix_from_metadata(
                    str(document.get("file_name") or ""),
                    str(document.get("mime_type") or ""),
                    ".jpg",
                )
                image_path = download_file(
                    file_id,
                    dest,
                    name_hint,
                    default_suffix=default_suffix,
                    allowed_extensions=IMAGE_EXTENSIONS,
                )
                return image_prompt_text(
                    caption_text,
                    image_path,
                    {
                        "mime_type": document.get("mime_type"),
                        "file_name": document.get("file_name"),
                        "file_size": document.get("file_size"),
                    },
                )

        audio = None
        audio_kind = ""
        for key, kind in (("voice", "voice"), ("audio", "audio")):
            candidate = message.get(key)
            if isinstance(candidate, dict) and candidate.get("file_id"):
                audio = candidate
                audio_kind = kind
                break
        if audio is None and document and document.get("file_id"):
            mime_type = str(document.get("mime_type") or "")
            file_name = str(document.get("file_name") or "")
            if mime_type.startswith("audio/") or os.path.splitext(file_name)[1].lower() in AUDIO_EXTENSIONS:
                audio = document
                audio_kind = "audio_document"
        if audio is not None:
            file_id = str(audio.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{audio.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(audio.get("file_name") or ""),
                str(audio.get("mime_type") or ""),
                ".ogg" if audio_kind == "voice" else ".mp3",
            )
            media_path = download_file(
                file_id,
                dest,
                name_hint,
                default_suffix=default_suffix,
                allowed_extensions=AUDIO_EXTENSIONS,
            )
            transcript, transcript_status = transcribe_audio(media_path)
            return audio_prompt_text(
                audio_kind,
                caption_text,
                media_path,
                {
                    "duration": audio.get("duration"),
                    "mime_type": audio.get("mime_type"),
                    "file_name": audio.get("file_name"),
                    "title": audio.get("title"),
                    "performer": audio.get("performer"),
                    "file_size": audio.get("file_size"),
                },
                transcript,
                transcript_status,
            )

        video = None
        video_kind = ""
        for key, kind in (("video", "video"), ("video_note", "video_note"), ("animation", "animation")):
            candidate = message.get(key)
            if isinstance(candidate, dict) and candidate.get("file_id"):
                video = candidate
                video_kind = kind
                break
        if video is None and document and document.get("file_id"):
            mime_type = str(document.get("mime_type") or "")
            file_name = str(document.get("file_name") or "")
            if mime_type.startswith("video/") or os.path.splitext(file_name)[1].lower() in VIDEO_EXTENSIONS:
                video = document
                video_kind = "video_document"
        if video is not None:
            file_id = str(video.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{video.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(video.get("file_name") or ""),
                str(video.get("mime_type") or ""),
                ".mp4",
            )
            media_path = download_file(
                file_id,
                dest,
                name_hint,
                default_suffix=default_suffix,
                allowed_extensions=VIDEO_EXTENSIONS,
            )
            thumbnail_path = download_thumbnail(video, dest, update_id)
            transcript, transcript_status = transcribe_audio(media_path)
            return video_prompt_text(
                video_kind,
                caption_text,
                media_path,
                {
                    "duration": video.get("duration"),
                    "mime_type": video.get("mime_type"),
                    "file_name": video.get("file_name"),
                    "width": video.get("width") or video.get("length"),
                    "height": video.get("height") or video.get("length"),
                    "file_size": video.get("file_size"),
                },
                thumbnail_path,
                transcript,
                transcript_status,
            )

        if document and document.get("file_id"):
            file_id = str(document.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{document.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(document.get("file_name") or ""),
                str(document.get("mime_type") or ""),
                ".bin",
            )
            media_path = download_file(
                file_id,
                dest,
                name_hint,
                default_suffix=default_suffix,
            )
            return document_prompt_text(
                caption_text,
                media_path,
                {
                    "mime_type": document.get("mime_type"),
                    "file_name": document.get("file_name"),
                    "file_size": document.get("file_size"),
                },
            )
    except Exception as exc:  # noqa: BLE001
        print(f"telegram media download 실패: {exc}", file=sys.stderr)
        if caption_text:
            return caption_text
        raise

    return caption_text


def telegram_prompt_from_update(upd):
    """폴러가 쓰는 진입점. 글·미디어를 한 경로로 돌려준다. 해당 챗이 아니면 빈 문자열."""
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return ""
    if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
        return ""
    try:
        return (prompt_from_telegram_message(msg, upd.get("update_id")) or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"telegram media 처리 실패: {exc}", file=sys.stderr)
        return ""


def telegram_poller():
    offset = _read(OFFSET_FILE)
    offset = int(offset) if offset.isdigit() else 0
    while True:
        res = tg("getUpdates", offset=offset, timeout=30)
        # ★폴러 생존 스탬프 — 「입력이 없어 조용한 것」과 「폴러가 죽어 조용한 것」은
        #   종전 계기로 구분할 수 없었다(둘 다 로그 0). 이 한 줄이 그 둘을 가른다.
        health_mark(last_poll_at=time.time())
        if not res or not res.get("ok"):
            time.sleep(3)
            continue
        for upd in res.get("result", []):
            try:
                callback = upd.get("callback_query")
                if callback:
                    handle_telegram_callback(callback)
                    continue
                text = telegram_prompt_from_update(upd)
                if not text:
                    continue
                # ★내구화 먼저, offset 전진은 finally 에서 — 순서가 유실 축의 전부다.
                inbox_spool(upd.get("update_id"), "telegram", text)
                preview = text.strip().splitlines()[0][:80]
                print(f">>> [{NAME}] 텔레그램 grok: {preview}", flush=True)
                handle_message_text(text, source="telegram")
            except Exception as exc:  # noqa: BLE001
                print(f"telegram update 처리 실패: {exc}", file=sys.stderr)
            finally:
                # 예외가 나도 전진시킨다 — 안 그러면 독성 메시지 1건이 폴러를 영구 정지시킨다.
                offset = upd["update_id"] + 1
                _write(OFFSET_FILE, offset)


def local_input_enabled():
    return str(LOCAL_INPUT).strip().lower() not in {"", "0", "false", "no", "off", "none"}


def ensure_local_fifo():
    if not local_input_enabled():
        return
    if not hasattr(os, "mkfifo"):
        raise RuntimeError("GRB_LOCAL_INPUT requires os.mkfifo support")
    path = os.path.expanduser(os.path.expandvars(LOCAL_INPUT))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        if not stat.S_ISFIFO(os.stat(path).st_mode):
            raise RuntimeError(f"GRB_LOCAL_INPUT exists but is not a FIFO: {path}")
        return
    os.mkfifo(path, 0o600)


def local_fifo_loop():
    if not local_input_enabled():
        return
    path = os.path.expanduser(os.path.expandvars(LOCAL_INPUT))
    while True:
        try:
            with open(path, encoding="utf-8") as fifo:
                for line in fifo:
                    handle_message_text(line, source="local")
        except Exception as exc:  # noqa: BLE001
            print(f"local input 실패: {exc}", file=sys.stderr)
            time.sleep(2)


def stdin_loop():
    for line in sys.stdin:
        handle_message_text(line, source="local")


def install_thread_dump_handler():
    """★SIGUSR1 → 전 스레드 스택 덤프 (T-260822-028, 제어 노드 채택).

    왜: 2026-08-21 웨지 때 제어 노드이 ★py-spy 부재로 스레드 덤프를 못 떠서, 워커가
      `_execute_with_session` 안 어디서 막혔는지 모른 채 `launchctl kickstart` 로 덮었다.
      그 재기동이 적체분을 지웠다(T-260821-033). 다음 웨지에서 같은 자리에 서지 않으려면
      ★재기동 전에 스택을 뜰 수단이 있어야 한다.

    왜 stdlib 인가: py-spy 상비는 ★새 의존성 도입 축이라 사용자 ack 사안으로 별건 보류다.
      faulthandler 는 표준 라이브러리이고 평시 동작 변화가 0 이다 — 시그널이 올 때만 돈다.

    ★왜 chain=False 인가 (T-260822-034 — 되돌리지 마라):
      최초판은 `chain=True` 였고 주석도 「기존 핸들러를 죽이지 않는다(fail-open)」라고 적혀
      있었다. ★실효는 정반대였다. chain 은 덤프 뒤 ★이전 핸들러로 넘긴다는 뜻인데, 이
      프로세스에는 앞서 등록된 파이썬 SIGUSR1 핸들러가 없다(`signal.signal` 호출 0건 —
      이 파일의 유일한 SIGUSR1 소비자가 여기다). 그러면 chain 대상은 `SIG_DFL` 이고
      ★SIGUSR1 의 기본 처분은 terminate 다. 즉 덤프를 뜨고 나서 브릿지를 죽였다.

      2026-08-22 04:01 라이브 실측: `kill -USR1 26084` → 4스레드 44줄 덤프는 정상 출력,
      그리고 26084 사망 → launchd KeepAlive 가 pid 27930 으로 재기동. 격리 A/B 대조군은
      `chain=True` → rc=158(128+30, SIGUSR1) · `chain=False` → rc=0 생존.

      ★이 훅의 존재 이유가 「재기동 ★전에」 스택을 뜨는 것인데, chain=True 는 뜨는 즉시
      재기동을 강제해 ★보려던 웨지 상태를 파괴한다. 있다고 믿고 쏘기 때문에 없는 것보다
      나쁘다. fail-open 은 「등록 실패해도 브릿지가 돈다」(아래 except)가 담당하지,
      chain 이 담당하는 것이 아니다 — 두 개를 같은 것으로 읽어서 난 사고다.

      되살리려면 먼저 `signal.signal(SIGUSR1, ...)` 소비자를 실제로 만들고,
      test_grb_thread_dump_260822_028.py 의 ★「USR1 후 생존」 단언을 통과시켜라.

    등록 실패해도 브릿지는 그대로 돈다(fail-open) — 그건 아래 except 가 한다.
    """
    try:
        import faulthandler
        import signal

        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"grok bridge thread dump 등록 실패: {exc}", file=sys.stderr)
        return False


def start_workers():
    global _WORKER_THREAD
    _WORKER_THREAD = threading.Thread(
        target=job_worker, daemon=True, name=f"grok-bridge-{NAME}-worker"
    )
    _WORKER_THREAD.start()
    threading.Thread(
        target=health_ticker, daemon=True, name=f"grok-bridge-{NAME}-health"
    ).start()
    if CHAT_LANE == "tui" and TUI_MIRROR_LOCAL:
        threading.Thread(
            target=tui_local_mirror_ticker,
            daemon=True,
            name=f"grok-bridge-{NAME}-tui-mirror",
        ).start()
        local_print(
            f"grok-bridge[{NAME}] tui local mirror ON "
            f"(interval={TUI_MIRROR_LOCAL_INTERVAL}s) — 터미널 직접 입력분도 폰으로 올린다"
        )
    if CHAT_LANE == "tui" and TUI_CLEAR_WATCH:
        threading.Thread(
            target=tui_local_clear_ticker,
            daemon=True,
            name=f"grok-bridge-{NAME}-tui-clear",
        ).start()
    install_thread_dump_handler()
    health_mark(started_at=time.time())
    local_print(f"grok-bridge[{NAME}] health={HEALTH_FILE} inbox={INBOX_FILE}")
    local_print(f"grok-bridge[{NAME}] thread dump: kill -USR1 {os.getpid()} → 이 로그로 전 스레드 스택")
    if local_input_enabled():
        ensure_local_fifo()
        local_print(f"grok-bridge[{NAME}] local input fifo={LOCAL_INPUT}")
        threading.Thread(target=local_fifo_loop, daemon=True, name=f"grok-bridge-{NAME}-fifo").start()
    if STDIN_INPUT:
        local_print(f"grok-bridge[{NAME}] stdin local input enabled")
        threading.Thread(target=stdin_loop, daemon=True, name=f"grok-bridge-{NAME}-stdin").start()


def start_banner():
    """시작 배너 — ★실제 레인을 찍는다.

    종전엔 레인과 무관하게 `mode=grok-headless` 고정이었다(T-260819-028 제어 노드 실측 ③).
    TUI 로 전환해 놓고도 배너는 headless 라고 말하니, 로그만으로는 어느 레인이 도는지
    확인할 방법이 없었다 — 판정엔 무영향이지만 진단을 정확히 반대로 이끈다.
    """
    lane = CHAT_LANE or "headless"
    mode = f"dry-run-{lane}" if DRY_RUN else f"grok-{lane}"
    return f"grok-bridge[{NAME}] start — chat={CHAT_ID} mode={mode}"


def main():
    print(start_banner())
    start_workers()
    telegram_poller()


if __name__ == "__main__":
    main()
