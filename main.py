import os
import json
import base64
import asyncio
import time
from pathlib import Path
from contextlib import suppress
from xml.sax.saxutils import escape
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from dotenv import load_dotenv

####################### LOAD ENV BEFORE IMPORTING GENIE *********************

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_FILE, override=True)

# Import only after .env is loaded.
from genie import ask_genie, ensure_genie_conversation

####################### ENVIRONMENT *********************

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_HOST = os.getenv("PUBLIC_HOST")
PORT = int(os.getenv("PORT", "8000"))

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")

if not PUBLIC_HOST:
    raise RuntimeError("PUBLIC_HOST is required")

####################### GPT-LIVE CONFIG *********************

OPENAI_LIVE_URL = "wss://api.openai.com/v1/live/sessions"
MODEL = "gpt-live-1"
VOICE = "marin"

####################### MUSIC CONFIG *********************

MUSIC_FILE = BASE_DIR / "ivr_upward_arp.ulaw"
MUSIC_START_DELAY = 3.0
SILENCE_BEFORE_MUSIC = 1.5
MUSIC_REPEAT_INTERVAL = 5.0
MUSIC_RETRY_INTERVAL = 1.0

if not MUSIC_FILE.exists():
    raise RuntimeError(f"Music filler not found: {MUSIC_FILE}")

MUSIC_BYTES = MUSIC_FILE.read_bytes()

print("\n======================================")
print("CONFIG")
print("OPENAI_API_KEY:", "configured")
print("GENIE_API_BASE_URL:", os.getenv("GENIE_API_BASE_URL"))
print("GENIE_ID:", os.getenv("GENIE_ID"))
print("GENIE_IDP_USER_ID fallback:", "configured" if os.getenv("GENIE_IDP_USER_ID") else "MISSING")
print("PUBLIC_HOST:", PUBLIC_HOST)
print("Music filler:", MUSIC_FILE)
print("Music bytes:", len(MUSIC_BYTES))
print("======================================")

app = FastAPI()

####################### CALL STATE *********************

call_states = {}


def get_call_state(call_sid: str):
    if call_sid not in call_states:
        call_states[call_sid] = {
            "genie_conversation_id": None,
            "workato_x_idp_user_id": None,
            "user_transcript": "",
            "genie_tasks": {},
            "generation": 0,
            "last_user_text_at": 0.0,
            "last_assistant_text_at": 0.0,
        }
    return call_states[call_sid]


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    dynamic_idp_user_id = request.query_params.get("workato_x_idp_user_id")

    if dynamic_idp_user_id:
        dynamic_idp_user_id = dynamic_idp_user_id.strip()

    if not dynamic_idp_user_id:
        dynamic_idp_user_id = None

    ws_url = f"wss://{PUBLIC_HOST}/media-stream"
    safe_idp_user_id = escape(dynamic_idp_user_id or "")

    print()
    print("======================================")
    print("TWILIO CALL SETUP")
    print("Media Stream URL:", ws_url)
    print("Dynamic Workato IDP User ID:", dynamic_idp_user_id or "<not provided - use .env>")
    print("======================================")

    # --------------------------------------------------------
    # Twilio carries the dynamic ID into the WebSocket
    # through customParameters.
    # --------------------------------------------------------
    twiml_xml = f"""
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter
                name="workato_x_idp_user_id"
                value="{safe_idp_user_id}"
            />
        </Stream>
    </Connect>
</Response>
""".strip()

    return Response(content=twiml_xml, media_type="application/xml")


# ============================================================
# OPENAI HELPERS
# ============================================================

async def send_openai_event(openai_ws, event):
    await openai_ws.send(json.dumps(event))


async def append_commentary(openai_ws, content: str, delegation_id=None):
    await send_openai_event(openai_ws, {
        "type": "session.commentary.append",
        "delegation_id": delegation_id,
        "content": content,
    })


async def append_thinking(openai_ws, content: str, delegation_id=None):
    await send_openai_event(openai_ws, {
        "type": "session.thinking.append",
        "delegation_id": delegation_id,
        "content": content,
    })


# ============================================================
# TWILIO AUDIO HELPERS
# ============================================================

async def clear_twilio_audio(twilio_ws, stream_sid):
    if not stream_sid:
        return
    with suppress(Exception):
        await twilio_ws.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))


async def play_music_filler(*, twilio_ws, stream_sid):
    """
    Play the actual instrumental_02.ulaw directly to Twilio.

    File format:
      PCMU / mu-law
      8000 Hz
      mono
    """
    if not stream_sid:
        return

    print()
    print("Playing instrumental filler")

    # 20ms of PCMU @ 8kHz.
    chunk_size = 160

    try:
        for offset in range(0, len(MUSIC_BYTES), chunk_size):
            chunk = MUSIC_BYTES[offset:offset + chunk_size]
            payload = base64.b64encode(chunk).decode("ascii")

            await twilio_ws.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload},
            }))

            await asyncio.sleep(0.02)

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        print("Music playback error:", repr(exc))


# ============================================================
# SILENCE DETECTION
# ============================================================

def conversation_is_quiet(state, quiet_for=SILENCE_BEFORE_MUSIC):
    now = time.monotonic()
    last_activity = max(state.get("last_user_text_at", 0.0), state.get("last_assistant_text_at", 0.0))

    if not last_activity:
        return True

    return now - last_activity >= quiet_for


# ============================================================
# MUSIC FILLER LOOP
# ============================================================

async def backend_music_fillers(*, twilio_ws, stream_sid, state, done_event: asyncio.Event):
    try:
        # ----------------------------------------------------
        # INITIAL WAIT
        # ----------------------------------------------------
        try:
            await asyncio.wait_for(done_event.wait(), timeout=MUSIC_START_DELAY)
            return
        except asyncio.TimeoutError:
            pass

        # ----------------------------------------------------
        # KEEP LOOKING FOR SILENT WINDOWS
        # ----------------------------------------------------
        while not done_event.is_set():

            if conversation_is_quiet(state):
                print("Silence detected - playing filler")

                await play_music_filler(twilio_ws=twilio_ws, stream_sid=stream_sid)

                try:
                    await asyncio.wait_for(done_event.wait(), timeout=MUSIC_REPEAT_INTERVAL)
                    return
                except asyncio.TimeoutError:
                    pass

            else:
                print("Conversation active - waiting for silent window")

                try:
                    await asyncio.wait_for(done_event.wait(), timeout=MUSIC_RETRY_INTERVAL)
                    return
                except asyncio.TimeoutError:
                    pass

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        print("Music filler task error:", repr(exc))


# ============================================================
# BACKGROUND GENIE
# ============================================================

async def run_genie_background(*, call_sid: str, delegation_id: str, request_text: str, state: dict,
                                openai_ws, twilio_ws, stream_sid):
    generation = state["generation"]
    done_event = asyncio.Event()

    print()
    print("######################################")
    print("BACKGROUND GENIE START")
    print("Call:", call_sid)
    print("Delegation:", delegation_id)
    print("Workato IDP User ID:", state.get("workato_x_idp_user_id") or "<using .env fallback>")
    print("Request:", request_text)
    print("######################################")

    # --------------------------------------------------------
    # MUSIC MONITOR
    # --------------------------------------------------------
    music_task = asyncio.create_task(
        backend_music_fillers(twilio_ws=twilio_ws, stream_sid=stream_sid, state=state, done_event=done_event)
    )

    # --------------------------------------------------------
    # Helper: stop the filler immediately and wait for the
    # in-flight play_music_filler() chunk loop to actually
    # exit, so it can never overlap with what plays next.
    # --------------------------------------------------------
    async def stop_music():
        done_event.set()
        if not music_task.done():
            music_task.cancel()
        with suppress(asyncio.CancelledError):
            await music_task

    try:
        # ----------------------------------------------------
        # RUN GENIE
        # ----------------------------------------------------
        result = await ask_genie(request_text, state)

        # Stop any in-flight filler *before* we clear the
        # buffer / speak the result - otherwise the filler
        # keeps streaming media frames on top of the answer.
        await stop_music()

        # ----------------------------------------------------
        # STALE RESULT
        # ----------------------------------------------------
        if generation != state["generation"]:
            print("Discarding stale Genie result:", delegation_id)
            return

        print()
        print("======================================")
        print("BACKGROUND GENIE RESULT")
        print(result)
        print("======================================")

        # ----------------------------------------------------
        # CLEAR MUSIC BEFORE FINAL RESPONSE
        # ----------------------------------------------------
        await clear_twilio_audio(twilio_ws, stream_sid)

        # ----------------------------------------------------
        # PROVIDE FINAL RESULT TO GPT-LIVE
        # ----------------------------------------------------
        await append_thinking(
            openai_ws,
            ("Workato Genie has completed the delegated business request. "
             "Use the supplied result as the source of truth. "
             "Do not invent business facts. "
             "Communicate it naturally and concisely."),
            delegation_id=delegation_id,
        )

        await append_commentary(openai_ws, result, delegation_id=delegation_id)

    except asyncio.CancelledError:
        await stop_music()
        print("Background Genie cancelled:", delegation_id)
        raise

    except Exception as exc:
        # Stop the filler before touching the stream / GPT,
        # same as the success path above.
        await stop_music()

        print()
        print("======================================")
        print("BACKGROUND GENIE ERROR")
        print("Delegation:", delegation_id)
        print("Error:", repr(exc))
        print("======================================")

        await clear_twilio_audio(twilio_ws, stream_sid)

        with suppress(Exception):
            await append_commentary(
                openai_ws,
                "I wasn't able to complete that request right now. Please try again.",
                delegation_id=delegation_id,
            )

    finally:
        # Safety net - guaranteed no-op if stop_music()
        # already ran above.
        await stop_music()
        state.get("genie_tasks", {}).pop(delegation_id, None)


# ============================================================
# MEDIA STREAM
# ============================================================

@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket):
    await twilio_ws.accept()
    print("Twilio media stream connected")

    stream_sid = None
    call_sid = None
    session_ready = False
    session_requested = False
    openai_session_id = None
    state = None

    # ========================================================
    # OPENAI CONNECTION
    # ========================================================
    async with websockets.connect(
        OPENAI_LIVE_URL,
        additional_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "User-Agent": "workato-genie-voice/python",
        },
    ) as openai_ws:

        # ====================================================
        # START GPT-LIVE
        # ====================================================
        async def start_live_session():
            nonlocal session_requested

            if session_requested or not stream_sid:
                return

            session_requested = True

            await send_openai_event(openai_ws, {
                "type": "session.start",
                "session": {
                    "model": MODEL,
                    "instructions": (
                        "You are the voice interface for Workato Genie. "
                        "Workato Genie is responsible for business reasoning, "
                        "business rules, deciding what information is required, "
                        "and determining the response or action. "
                        "For any customer or business request, delegate the "
                        "caller's request to the client backend. "
                        "Do not independently decide what business information "
                        "is required. "
                        "Do not independently answer business questions. "
                        "Do not invent business facts. "
                        "When delegating, briefly acknowledge the caller, for "
                        "example, 'Sure, let me check that for you.' "
                        "When the backend response arrives, communicate it "
                        "naturally to the caller. "
                        "If the backend asks for additional information, ask the "
                        "caller for exactly that information. "
                        "When the caller provides the requested information, "
                        "delegate the new information back to the backend. "
                        "Keep your spoken responses concise and natural. "
                        "Do not read markdown syntax aloud."
                    ),
                    "audio": {
                        "format": {"type": "audio/pcmu", "rate": 8000},
                        "output": {"voice": VOICE},
                    },
                    "delegation": {"type": "client"},
                },
            })

        # ====================================================
        # TWILIO -> OPENAI
        # ====================================================
        async def receive_from_twilio():
            nonlocal stream_sid, call_sid, state

            try:
                while True:
                    raw = await twilio_ws.receive_text()
                    data = json.loads(raw)
                    event_type = data.get("event")

                    # ----------------------------------------
                    # STREAM START
                    # ----------------------------------------
                    if event_type == "start":
                        start = data.get("start", {})
                        stream_sid = start.get("streamSid")
                        call_sid = start.get("callSid")

                        # ------------------------------------
                        # DYNAMIC ID FROM <Parameter>
                        # ------------------------------------
                        custom_parameters = start.get("customParameters", {})
                        dynamic_idp_user_id = custom_parameters.get("workato_x_idp_user_id")

                        # ------------------------------------
                        # NORMALIZE
                        # ------------------------------------
                        if dynamic_idp_user_id:
                            dynamic_idp_user_id = dynamic_idp_user_id.strip()

                        if not dynamic_idp_user_id:
                            dynamic_idp_user_id = None

                        # ------------------------------------
                        # CALL STATE
                        # ------------------------------------
                        state = get_call_state(call_sid)
                        state["workato_x_idp_user_id"] = dynamic_idp_user_id

                        print()
                        print("======================================")
                        print("TWILIO STREAM STARTED")
                        print("Stream SID:", stream_sid)
                        print("Call SID:", call_sid)
                        print("Workato IDP User ID:", dynamic_idp_user_id or "<using .env fallback>")
                        print("======================================")

                        # ------------------------------------
                        # PREWARM GENIE
                        # ------------------------------------
                        async def prewarm():
                            try:
                                genie_id = await ensure_genie_conversation(state)
                                print("Pre-warmed Genie:", genie_id)
                            except Exception as exc:
                                print("Genie prewarm error:", repr(exc))

                        asyncio.create_task(prewarm())
                        await start_live_session()

                    # ----------------------------------------
                    # CALLER AUDIO
                    # ----------------------------------------
                    elif event_type == "media" and session_ready:
                        payload = data.get("media", {}).get("payload")

                        if payload:
                            await send_openai_event(openai_ws, {
                                "type": "session.input_audio.append",
                                "audio": payload,
                            })

                    # ----------------------------------------
                    # STOP
                    # ----------------------------------------
                    elif event_type == "stop":
                        print("Twilio stream stopped")
                        break

            except Exception as exc:
                print("Twilio receive ended:", repr(exc))

        # ====================================================
        # OPENAI -> TWILIO
        # ====================================================
        async def receive_from_openai():
            nonlocal session_ready, openai_session_id, state

            try:
                async for raw in openai_ws:
                    event = json.loads(raw)
                    event_type = event.get("type")

                    # ----------------------------------------
                    # SESSION READY
                    # ----------------------------------------
                    if event_type == "session.started":
                        session_ready = True
                        openai_session_id = event.get("session", {}).get("id")

                        print()
                        print("======================================")
                        print("GPT-LIVE SESSION READY")
                        print("Session:", openai_session_id)
                        print("======================================")

                        await append_commentary(
                            openai_ws,
                            "Welcome to Idea Lifestyle Support. How can I help you?",
                            delegation_id=None,
                        )

                    # ----------------------------------------
                    # USER TRANSCRIPT
                    # ----------------------------------------
                    elif event_type == "session.input_transcript.delta":
                        if state is not None:
                            delta = event.get("delta", "")

                            if delta:
                                state["user_transcript"] += delta
                                state["last_user_text_at"] = time.monotonic()
                                print("USER TRANSCRIPT DELTA:", repr(delta))

                    # ----------------------------------------
                    # DELEGATION
                    # ----------------------------------------
                    elif event_type == "session.delegation.created":
                        if state is None:
                            continue

                        delegation = event.get("delegation", {})
                        delegation_id = delegation.get("id")

                        if not delegation_id or delegation.get("target") != "client":
                            continue

                        request_text = state.get("user_transcript", "").strip()
                        state["user_transcript"] = ""

                        if not request_text:
                            await append_commentary(
                                openai_ws,
                                "Could you repeat that request for me?",
                                delegation_id=delegation_id,
                            )
                            continue

                        print()
                        print("######################################")
                        print("CLIENT DELEGATION CREATED")
                        print("Delegation:", delegation_id)
                        print("Task:", request_text)
                        print("######################################")

                        # ------------------------------------
                        # ACKNOWLEDGE
                        # ------------------------------------
                        await append_commentary(
                            openai_ws,
                            "Thanks, I'll check that now.",
                            delegation_id=delegation_id,
                        )

                        # ------------------------------------
                        # BACKGROUND GENIE
                        # ------------------------------------
                        task = asyncio.create_task(run_genie_background(
                            call_sid=call_sid,
                            delegation_id=delegation_id,
                            request_text=request_text,
                            state=state,
                            openai_ws=openai_ws,
                            twilio_ws=twilio_ws,
                            stream_sid=stream_sid,
                        ))

                        state["genie_tasks"][delegation_id] = task

                    # ----------------------------------------
                    # GPT AUDIO -> TWILIO
                    # ----------------------------------------
                    elif event_type == "session.output_audio.delta":
                        if not stream_sid:
                            continue

                        delta = event.get("delta")

                        if not delta:
                            continue

                        await twilio_ws.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": delta},
                        }))

                    # ----------------------------------------
                    # ASSISTANT TRANSCRIPT
                    # ----------------------------------------
                    elif event_type == "session.output_transcript.delta":
                        delta = event.get("delta", "")

                        if delta:
                            if state is not None:
                                state["last_assistant_text_at"] = time.monotonic()

                            print("ASSISTANT:", delta, end="", flush=True)

                    # ----------------------------------------
                    # ERROR
                    # ----------------------------------------
                    elif event_type == "error":
                        print()
                        print("======================================")
                        print("OPENAI LIVE ERROR")
                        print(json.dumps(event, indent=2))
                        print("======================================")

            except Exception as exc:
                print("OpenAI receive ended:", repr(exc))

        # ====================================================
        # RUN RELAYS
        # ====================================================
        twilio_task = asyncio.create_task(receive_from_twilio())
        openai_task = asyncio.create_task(receive_from_openai())

        done, pending = await asyncio.wait(
            {twilio_task, openai_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # ====================================================
        # CLEANUP
        # ====================================================
        for task in pending:
            task.cancel()

        for task in pending:
            with suppress(asyncio.CancelledError):
                await task

        if state is not None:
            state["generation"] += 1

            for task in list(state.get("genie_tasks", {}).values()):
                if not task.done():
                    task.cancel()

            call_states.pop(call_sid, None)

        print("Call cleaned up:", call_sid)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)