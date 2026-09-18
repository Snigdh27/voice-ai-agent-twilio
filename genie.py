import os
import asyncio
import time
import requests

from pathlib import Path
from dotenv import load_dotenv

# ============================================================
# ENVIRONMENT
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

GENIE_API_BASE_URL = os.getenv("GENIE_API_BASE_URL")
GENIE_API_KEY = os.getenv("GENIE_API_KEY")
GENIE_ID = os.getenv("GENIE_ID")

# ============================================================
# FALLBACK IDP USER
#
# Used when workato_x_idp_user_id was NOT supplied dynamically.
# ============================================================

GENIE_IDP_USER_ID = os.getenv("GENIE_IDP_USER_ID")

# ============================================================
# VALIDATE
# ============================================================

required_env = {
    "GENIE_API_BASE_URL": GENIE_API_BASE_URL,
    "GENIE_API_KEY": GENIE_API_KEY,
    "GENIE_ID": GENIE_ID,
}

missing_env = [name for name, value in required_env.items() if not value]

if missing_env:
    raise RuntimeError("Missing required Genie environment variables: " + ", ".join(missing_env))

GENIE_API_BASE_URL = GENIE_API_BASE_URL.rstrip("/")

print()
print("======================================")
print("GENIE CONFIG")
print("GENIE_API_BASE_URL:", GENIE_API_BASE_URL)
print("GENIE_ID:", GENIE_ID)
print("GENIE_API_KEY:", "configured")
print("Fallback GENIE_IDP_USER_ID:", "configured" if GENIE_IDP_USER_ID else "MISSING")
print("======================================")

# ============================================================
# HTTP
# ============================================================

http = requests.Session()


# ============================================================
# RESOLVE IDP USER ID
# ============================================================

def resolve_idp_user_id(idp_user_id=None):
    """
    Priority:

    1. Dynamic workato_x_idp_user_id from the current call.
    2. GENIE_IDP_USER_ID from .env.

    Empty or whitespace-only dynamic values also fall back
    to the environment value.
    """
    dynamic_value = str(idp_user_id or "").strip()

    if dynamic_value:
        return dynamic_value

    env_value = str(GENIE_IDP_USER_ID or "").strip()

    if env_value:
        return env_value

    raise RuntimeError(
        "No Workato IDP user ID is available. "
        "Pass workato_x_idp_user_id dynamically "
        "or configure GENIE_IDP_USER_ID in .env."
    )


# ============================================================
# HEADERS
# ============================================================

def headers(idp_user_id=None):
    resolved_idp_user_id = resolve_idp_user_id(idp_user_id)

    return {
        "Authorization": f"Bearer {GENIE_API_KEY}",
        "X-IDP-User-ID": resolved_idp_user_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ============================================================
# CREATE GENIE CONVERSATION
# ============================================================

def create_conversation(idp_user_id=None):
    started = time.perf_counter()

    url = f"{GENIE_API_BASE_URL}/api/v1/genies/{GENIE_ID}/chat/conversations"

    response = http.post(url, headers=headers(idp_user_id), timeout=30)

    elapsed = time.perf_counter() - started
    print(f"[Genie] create conversation: {elapsed:.2f}s")

    if not response.ok:
        raise RuntimeError(f"Create conversation failed: {response.status_code} {response.text}")

    data = response.json()
    conversation_id = data.get("result", {}).get("conversation_id")

    if not conversation_id:
        raise RuntimeError(f"No conversation_id returned: {response.text}")

    print("Created Genie conversation:", conversation_id)

    return conversation_id


# ============================================================
# SEND MESSAGE
# ============================================================

def send_message(conversation_id, message, idp_user_id=None):
    started = time.perf_counter()

    url = f"{GENIE_API_BASE_URL}/api/v1/genies/{GENIE_ID}/chat/conversations/{conversation_id}/messages"

    message = str(message or "").strip()

    if not message:
        raise ValueError("Cannot send empty message to Genie")

    genie_message = f"{message}\n\nChannel: Voice"

    payload = {"message": genie_message, "stream": False}

    print("Sending to Genie:", message)

    response = http.post(url, headers=headers(idp_user_id), json=payload, timeout=30)

    elapsed = time.perf_counter() - started
    print(f"[Genie] send message HTTP: {elapsed:.2f}s")

    if not response.ok:
        raise RuntimeError(f"Send message failed: {response.status_code} {response.text}")

    if response.text:
        return response.json()

    return {}


# ============================================================
# GET JSON
# ============================================================

def get_json(url, idp_user_id=None):
    response = http.get(url, headers=headers(idp_user_id), timeout=30)

    if not response.ok:
        raise RuntimeError(f"GET failed: {response.status_code} {response.text}")

    return response.json()


# ============================================================
# MESSAGE HELPERS
# ============================================================

def extract_messages(data):
    if isinstance(data, list):
        return data

    result = data.get("result")

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        messages = result.get("messages")

        if isinstance(messages, list):
            return messages

    messages = data.get("messages")

    if isinstance(messages, list):
        return messages

    return []


def is_agent_message(message):
    source = str(message.get("source") or message.get("role") or message.get("type") or "").lower()
    return source in {"genie", "agent", "assistant", "agent.message"}


def message_id(message):
    return str(message.get("id") or message.get("message_id") or message.get("_id") or "")


def extract_reply(message):
    if not message:
        return ""

    content = message.get("content")

    if isinstance(content, str):
        return content.strip()

    value = message.get("message")

    if isinstance(value, str):
        return value.strip()

    value = message.get("text")

    if isinstance(value, str):
        return value.strip()

    if isinstance(content, dict):
        text = content.get("text")

        if isinstance(text, str):
            return text.strip()

    return ""


# ============================================================
# GET AGENT MESSAGES
# ============================================================

def get_agent_messages(conversation_id, idp_user_id=None):
    url = f"{GENIE_API_BASE_URL}/api/v1/genies/{GENIE_ID}/chat/conversations/{conversation_id}/messages"

    data = get_json(url, idp_user_id)

    return [message for message in extract_messages(data) if is_agent_message(message)]


# ============================================================
# WAIT FOR GENIE RESPONSE
#
# NOTE:
#   This used to poll via a fresh asyncio.to_thread() call
#   every 0.25s. That spun up a new OS thread ~4x/second for
#   up to 30s straight, right in the middle of the window
#   where play_music_filler() is trying to hold a strict 20ms
#   send cadence to Twilio. The resulting thread/GIL churn was
#   enough to jitter the music sender's timing and produce
#   audible glitching on the hold-music filler.
#
#   Fix: run the entire poll loop as ONE blocking function in
#   ONE background thread (a single asyncio.to_thread call),
#   instead of a new thread per poll iteration.
# ============================================================

def _wait_for_response_blocking(conversation_id, known_message_ids, idp_user_id=None):
    """
    Synchronous polling loop. Meant to be run inside a single
    asyncio.to_thread(...) call so it occupies exactly one
    background thread for its whole duration, instead of
    creating a new thread on every poll.
    """
    status_url = f"{GENIE_API_BASE_URL}/api/v1/genies/{GENIE_ID}/chat/conversations/{conversation_id}"

    started = time.perf_counter()

    for attempt in range(120):

        conversation = get_json(status_url, idp_user_id)

        state = str(
            conversation.get("result", {}).get("state") or conversation.get("state") or ""
        ).strip().lower()

        elapsed = time.perf_counter() - started
        print(f"[Genie +{elapsed:.2f}s] state={state} attempt={attempt}")

        # ----------------------------------------------------
        # APPROVAL
        # ----------------------------------------------------
        if state == "awaiting_approval":
            return "This request requires approval and cannot currently be completed over the phone."

        # ----------------------------------------------------
        # IDLE = FINAL RESPONSE READY
        # ----------------------------------------------------
        if state == "idle":

            messages = get_agent_messages(conversation_id, idp_user_id)

            new_messages = [message for message in messages if message_id(message) not in known_message_ids]

            if new_messages:

                # Genie can return newest-first.
                # Explicitly sort chronologically.
                new_messages.sort(key=lambda message: str(message.get("created_at", "")))

                newest_message = new_messages[-1]
                reply = extract_reply(newest_message)

                if reply:
                    print()
                    print("======================================")
                    print("FINAL GENIE MESSAGE")
                    print("Created:", newest_message.get("created_at"))
                    print("Message ID:", message_id(newest_message))
                    print("Reply:", reply)
                    print("======================================")

                    return reply

        time.sleep(0.25)

    raise TimeoutError("Timed out waiting for Genie response")


async def wait_for_response(conversation_id, known_message_ids, idp_user_id=None):
    """
    Async wrapper - offloads the ENTIRE polling loop to a
    single background thread so it doesn't create per-poll
    scheduling churn on the main event loop.
    """
    return await asyncio.to_thread(_wait_for_response_blocking, conversation_id, known_message_ids, idp_user_id)


# ============================================================
# ENSURE ONE GENIE CONVERSATION PER CALL
# ============================================================

async def ensure_genie_conversation(state):
    if "genie_conversation_lock" not in state:
        state["genie_conversation_lock"] = asyncio.Lock()

    async with state["genie_conversation_lock"]:

        if not state.get("genie_conversation_id"):

            # -----------------------------------------------
            # Dynamic value may be None.
            #
            # create_conversation -> headers ->
            # resolve_idp_user_id will fall back to .env.
            # -----------------------------------------------
            idp_user_id = state.get("workato_x_idp_user_id")

            conversation_id = await asyncio.to_thread(create_conversation, idp_user_id)

            state["genie_conversation_id"] = conversation_id

    return state["genie_conversation_id"]


# ============================================================
# PUBLIC ASK GENIE
# ============================================================

async def ask_genie(message, state):
    overall_started = time.perf_counter()

    message = str(message or "").strip()

    if not message:
        raise ValueError("ask_genie received an empty message")

    # --------------------------------------------------------
    # DYNAMIC USER
    #
    # Can be None. The HTTP layer then falls back to .env.
    # --------------------------------------------------------
    idp_user_id = state.get("workato_x_idp_user_id")
    resolved_idp_user_id = resolve_idp_user_id(idp_user_id)

    print()
    print("[Genie] IDP source:", "dynamic Twilio parameter" if idp_user_id else ".env fallback")
    print("[Genie] IDP user:", resolved_idp_user_id)

    # --------------------------------------------------------
    # GET/CREATE CONVERSATION
    # --------------------------------------------------------
    conversation_id = await ensure_genie_conversation(state)

    print("[Genie] using conversation:", conversation_id)

    # --------------------------------------------------------
    # SNAPSHOT EXISTING AGENT MESSAGES
    # --------------------------------------------------------
    existing_messages = await asyncio.to_thread(get_agent_messages, conversation_id, idp_user_id)

    known_message_ids = {message_id(message) for message in existing_messages}

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------
    await asyncio.to_thread(send_message, conversation_id, message, idp_user_id)

    # --------------------------------------------------------
    # WAIT
    # --------------------------------------------------------
    reply = await wait_for_response(conversation_id, known_message_ids, idp_user_id)

    elapsed = time.perf_counter() - overall_started
    print(f"[Genie] complete request: {elapsed:.2f}s")

    return reply