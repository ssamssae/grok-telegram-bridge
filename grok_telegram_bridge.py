#!/usr/bin/env python3
"""Telegram -> grok bridge: one Telegram chat drives one machine's grok CLI.

Each Telegram message runs one `grok -p ...` call (`--resume <session-uuid>`
only after grok has assigned that id) and sends the final answer back to
Telegram through the Bot API.

Session continuity: grok's `-s` flag only accepts a UUID, so a chat_id ->
session uuid mapping is kept in a local state file instead of reusing a
human-readable key.
"""
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
    while not stop_event.is_set():
        tg("sendChatAction", timeout=5, chat_id=CHAT_ID, action="typing")
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
TMUX_SOCKET = env("GRB_TMUX_SOCKET", "grok")
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
#     크게 잡는다. 실제로 거의 항상 IDLE 쪽이 먼저 걸린다.
TUI_IDLE_TIMEOUT = int_env("GRB_TUI_IDLE_TIMEOUT", 180, minimum=1)
TUI_ANSWER_TIMEOUT = int_env("GRB_TUI_ANSWER_TIMEOUT", 1800, minimum=1)
TUI_LAUNCHER = "grok-tui-session-start.sh"
TUI_LOG_KEY = "grb_tui_lane"


def float_env(k, default):
    try:
        v = float(env(k, str(default)))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


TUI_POLL_INTERVAL = float_env("GRB_TUI_POLL_INTERVAL", 0.5)
TUI_SUBMIT_DELAY = float_env("GRB_TUI_SUBMIT_DELAY", 0.3)
TUI_FALLBACK_HEADLESS = bool_env("GRB_TUI_FALLBACK_HEADLESS", False)


def tui_session_id():
    """상주 세션의 고정 uuid. 기동면이 파일에 적어둔다.

    uuid 를 고정하지 않으면 어느 세션 디렉토리를 읽어야 하는지 알 수 없다 —
    그래서 기동면이 `--session-id` 로 못 박고 그 값을 여기서 되읽는다.
    """
    return (env("GRB_TUI_SESSION_ID") or _read(TUI_SESSION_ID_FILE)).strip()


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


def _tui_tool_call_names(rows):
    names = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for call in row.get("tool_calls") or []:
            if isinstance(call, dict):
                names.append(str(call.get("name") or "?"))
    return names


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


def _tui_paste(prompt):
    """crb 와 같은 tmux 3단(load-buffer → paste-buffer → 제출키).

    send-keys 로 본문을 직접 타이핑하지 않는 이유는 crb 와 같다 — 여러 줄·특수문자가
    조합키로 해석되는 경로를 아예 안 만든다.
    """
    payload = (prompt or "").rstrip("\n")
    if not payload:
        return
    _tmux("load-buffer", "-", input_text=payload)
    _tmux("paste-buffer", "-p", "-t", TMUX_PANE)
    time.sleep(TUI_SUBMIT_DELAY)
    _tmux("send-keys", "-t", TMUX_PANE, TUI_SUBMIT_KEY)


def run_grok_tui(prompt):
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
    _tui_paste(prompt)

    hard_deadline = time.time() + TUI_ANSWER_TIMEOUT
    last_progress = time.time()
    seen_rows = 0
    reported = []
    stop_reason = None
    while True:
        fresh = _read_history_rows(path)[baseline:]
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
            return str(finals[-1].get("content") or "").strip(), None, session_id
        now = time.time()
        # ★조용한 실패가 제일 나쁘다 — 왜 잘렸는지를 문구로 가른다. 사용자 폰에 그대로 뜬다.
        if now - last_progress >= TUI_IDLE_TIMEOUT and not _tui_tool_in_flight(fresh):
            stop_reason = f"{TUI_IDLE_TIMEOUT}s 동안 새 출력이 없었다(무진전)"
            break
        if now >= hard_deadline:
            stop_reason = (
                f"총 상한 {TUI_ANSWER_TIMEOUT}s 초과 — 계속 움직이는데 답이 안 끝났다"
            )
            break
        time.sleep(TUI_POLL_INTERVAL)

    suffix = f" (도구 시도: {', '.join(reported[:3])})" if reported else ""
    raise GrokExecError(f"tmux 세션에서 답이 안 왔다 — {stop_reason}{suffix}")


def _remember_session(chat_id, returned_sid):
    if returned_sid:
        write_session_uuid(chat_id, returned_sid)


def _execute_with_session(chat_id, prompt):
    if CHAT_LANE == "tui":
        if tui_session_alive():
            answer, cost_usd, _session_id = run_grok_tui(prompt)
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


def deliver_mesh_event(kind, body, *, task_id=None, visibility=None, reply_markup=None):
    """Send one message straight to the Telegram Bot API.

    The maintainer's internal build routes delivery through a private message
    bus; this public build has no such bus, so delivery is a direct
    `sendMessage` call. The returned dict keeps the delivery-list shape the
    callers read, so nothing downstream has to know which path was taken.

    kind/task_id/visibility are accepted and ignored here - they are routing
    hints for the bus, and there is no bus to route to. reply_markup is
    forwarded on the first chunk only, so a confirm button can sit on the
    first message of a long answer.
    """
    payload = str(body or "").strip()
    if not payload:
        return {"deliveries": []}
    deliveries = []
    first = True
    for chunk in _tg_chunks(payload, TG_CHUNK):
        extra = {}
        if first and reply_markup:
            extra["reply_markup"] = reply_markup
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


def mirror_answer(source, text, task_id=None):
    """본문 1통 + (있으면) 추천답변 버블 1통.

    ★버블 kind 가 copy_content 인 이유 = clb 의 버블도 send_copy_content 경로로
    나간다(clb: "`<추천답변>` 버블도 같은 경로를 타서"). R-C4 가 그 kind 에
    "헤더·구분선·백틱·꼬리말 없이 원문 그대로" 를 계약으로 걸어두므로, 사용자가
    그대로 복사해 보낼 문장이 장식 없이 착지한다. mesh_group 도 억제라 그룹 로그에
    '끝' 이 두 줄 찍히지 않는다.
    """
    local_print(f"grok answer ({source}):\n{text}")
    body, suggested = (
        split_suggested_reply(text) if SUGGESTED_REPLY_SPLIT else (text, "")
    )
    deliver_mesh_event("final", body, task_id=task_id)
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


def mirror_error(source, text, task_id=None):
    msg = f"grok 호출 실패: {text}"
    local_print(f"{msg} ({source})")
    deliver_mesh_event("error", msg, task_id=task_id)


def process_job(source, text, task_id=None):
    mirror_prompt(source, text)
    typing_stop = start_typing()
    try:
        with GROK_LOCK:
            answer, _cost_usd = _execute_with_session(CHAT_ID, text)
    except GrokExecError as exc:
        mirror_error(source, str(exc), task_id=task_id)
        return
    except Exception as exc:  # noqa: BLE001
        print(f"grok bridge 처리 실패: {exc}", file=sys.stderr)
        mirror_error(source, "내부 오류", task_id=task_id)
        return
    finally:
        typing_stop.set()
    mirror_answer(source, answer, task_id=task_id)


def job_worker():
    while True:
        job = JOBS.get()
        health_mark(last_job_started_at=time.time())
        try:
            source, text = job
            process_job(source, text)
        except Exception as exc:  # noqa: BLE001
            print(f"grok bridge worker 실패: {exc}", file=sys.stderr)
        finally:
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
