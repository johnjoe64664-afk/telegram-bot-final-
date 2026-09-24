import json
import os
import time
from pathlib import Path

import requests

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GROUP_ID = os.environ.get("GROUP_ID", "").strip()
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not GROUP_ID:
    raise RuntimeError("GROUP_ID is missing")

API = f"https://api.telegram.org/bot{TOKEN}"
ADMIN_IDS = {
    item.strip()
    for item in os.environ.get("ALLOWED_ADMIN_IDS", "").split(",")
    if item.strip()
}


def telegram(method, payload=None):
    response = requests.post(f"{API}/{method}", data=payload or {}, timeout=35)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"{method} failed: {result}")
    return result["result"]


def load_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"user_topics": {}, "group_messages": {}}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            value = json.load(file)
        value.setdefault("user_topics", {})
        value.setdefault("group_messages", {})
        return value
    except Exception as error:
        print("STATE LOAD ERROR:", repr(error), flush=True)
        return {"user_topics": {}, "group_messages": {}}


def save_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    temporary.replace(STATE_FILE)


def display_name(user):
    name = " ".join(
        part for part in [user.get("first_name"), user.get("last_name")] if part
    )
    username = user.get("username")
    if username:
        name += f" (@{username})"
    return name or f"User {user['id']}"


def check_target_group():
    chat = telegram("getChat", {"chat_id": GROUP_ID})
    print(
        "TARGET CHAT:", chat.get("id"), chat.get("title"),
        "type=", chat.get("type"), "is_forum=", chat.get("is_forum"),
        flush=True,
    )
    if chat.get("type") != "supergroup" or not chat.get("is_forum", False):
        raise RuntimeError(
            "GROUP_ID must be the numeric ID of the same Telegram supergroup "
            "where Topics is enabled."
        )


def topic_for_user(user):
    user_id = str(user["id"])
    existing = state["user_topics"].get(user_id)
    if existing:
        return int(existing)

    topic = telegram(
        "createForumTopic",
        {
            "chat_id": GROUP_ID,
            "name": f"{display_name(user)} [{user_id}]"[:128],
        },
    )
    thread_id = int(topic["message_thread_id"])
    state["user_topics"][user_id] = thread_id
    save_state()

    telegram(
        "sendMessage",
        {
            "chat_id": GROUP_ID,
            "message_thread_id": thread_id,
            "text": (
                f"Customer: {display_name(user)}\n"
                f"Telegram ID: {user_id}\n"
                "Reply to a customer message in this topic to respond."
            ),
        },
    )
    print("TOPIC CREATED:", user_id, thread_id, flush=True)
    return thread_id


def forward_customer_message(message):
    user = message["from"]
    thread_id = topic_for_user(user)

    # copyMessage supports all normal message types and explicitly targets the topic.
    copied = telegram(
        "copyMessage",
        {
            "chat_id": GROUP_ID,
            "message_thread_id": thread_id,
            "from_chat_id": message["chat"]["id"],
            "message_id": message["message_id"],
        },
    )

    group_message_id = str(copied["message_id"])
    state["group_messages"][group_message_id] = {
        "user_id": str(user["id"]),
        "thread_id": thread_id,
    }
    save_state()
    print(
        "COPIED:", user["id"], "TO TOPIC:", thread_id,
        "GROUP MESSAGE:", group_message_id, flush=True,
    )


def reply_to_customer(message):
    if ADMIN_IDS:
        sender_id = str(message.get("from", {}).get("id"))
        if sender_id not in ADMIN_IDS:
            print("IGNORED: sender is not an allowed admin", flush=True)
            return

    replied = message.get("reply_to_message") or {}
    mapping = state["group_messages"].get(str(replied.get("message_id")))
    if not mapping:
        print("NO MAPPING FOR GROUP MESSAGE:", replied.get("message_id"), flush=True)
        return

    telegram(
        "copyMessage",
        {
            "chat_id": mapping["user_id"],
            "from_chat_id": GROUP_ID,
            "message_id": message["message_id"],
        },
    )
    print("REPLIED TO CUSTOMER:", mapping["user_id"], flush=True)


state = load_state()
offset = 0

print("BOT STARTED", flush=True)
print("GROUP_ID:", GROUP_ID, flush=True)
print("STATE_FILE:", STATE_FILE, flush=True)
check_target_group()

while True:
    try:
        response = requests.get(
            f"{API}/getUpdates",
            params={
                "offset": offset,
                "timeout": 25,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=35,
        )
        data = response.json()
        if not data.get("ok"):
            print("TELEGRAM API ERROR:", data, flush=True)
            time.sleep(10)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue

            chat = message.get("chat", {})
            chat_type = chat.get("type")
            chat_id = str(chat.get("id"))

            if chat_type == "private":
                try:
                    forward_customer_message(message)
                except Exception as error:
                    print("FORWARD ERROR:", repr(error), flush=True)

            elif chat_type == "supergroup" and chat_id == GROUP_ID:
                if message.get("reply_to_message"):
                    try:
                        reply_to_customer(message)
                    except Exception as error:
                        print("REPLY ERROR:", repr(error), flush=True)

    except requests.RequestException as error:
        print("NETWORK ERROR:", repr(error), flush=True)
        time.sleep(10)
    except Exception as error:
        print("PROGRAM ERROR:", repr(error), flush=True)
        time.sleep(10)
