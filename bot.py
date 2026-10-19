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

# Optional: comma-separated numeric Telegram user IDs.
# Example: ALLOWED_ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    item.strip()
    for item in os.environ.get("ALLOWED_ADMIN_IDS", "").split(",")
    if item.strip()
}


def telegram(method, payload=None):
    response = requests.post(
        f"{API}/{method}",
        data=payload or {},
        timeout=35,
    )
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"{method} failed: {result}")

    return result["result"]


def load_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {
            "user_topics": {},
            "group_messages": {},
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            value = json.load(file)

        value.setdefault("user_topics", {})
        value.setdefault("group_messages", {})
        return value

    except Exception as error:
        print("STATE LOAD ERROR:", repr(error), flush=True)
        return {
            "user_topics": {},
            "group_messages": {},
        }


def save_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

    temporary.replace(STATE_FILE)


def display_name(user):
    name = " ".join(
        part
        for part in [user.get("first_name"), user.get("last_name")]
        if part
    )

    username = user.get("username")
    if username:
        name += f" (@{username})"

    return name or f"User {user['id']}"


def check_target_group():
    chat = telegram("getChat", {"chat_id": GROUP_ID})

    print(
        "TARGET CHAT:",
        chat.get("id"),
        chat.get("title"),
        "type=",
        chat.get("type"),
        "is_forum=",
        chat.get("is_forum"),
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
                "ဒီ Topic ထဲမှာ ပုံမှန်စာပို့ရုံနဲ့ customer ဆီ ပြန်ပို့ပါမယ်။"
            ),
        },
    )

    print("TOPIC CREATED:", user_id, thread_id, flush=True)
    return thread_id


def forward_customer_message(message):
    user = message["from"]
    thread_id = topic_for_user(user)

    # Copy the customer's message into that customer's forum topic.
    copied = telegram(
        "copyMessage",
        {
            "chat_id": GROUP_ID,
            "message_thread_id": thread_id,
            "from_chat_id": message["chat"]["id"],
            "message_id": message["message_id"],
        },
    )

    # Keep this mapping for reference and compatibility with old state files.
    group_message_id = str(copied["message_id"])
    state["group_messages"][group_message_id] = {
        "user_id": str(user["id"]),
        "thread_id": thread_id,
    }
    save_state()

    print(
        "COPIED:",
        user["id"],
        "TO TOPIC:",
        thread_id,
        "GROUP MESSAGE:",
        group_message_id,
        flush=True,
    )


def user_for_topic(thread_id):
    """Find the customer ID associated with a forum topic."""
    if not thread_id:
        return None

    for user_id, saved_thread_id in state["user_topics"].items():
        if str(saved_thread_id) == str(thread_id):
            return str(user_id)

    return None


def is_allowed_admin(message):
    """Return True only for configured admin Telegram user IDs."""
    if not ADMIN_IDS:
        print("BROADCAST BLOCKED: set ALLOWED_ADMIN_IDS first", flush=True)
        return False

    sender_id = str(message.get("from", {}).get("id", ""))
    return sender_id in ADMIN_IDS


def broadcast_message(message):
    """Send /broadcast <text> from General topic to all known customers."""
    if not is_allowed_admin(message):
        print(
            "IGNORED BROADCAST: sender is not an allowed admin",
            message.get("from", {}).get("id"),
            flush=True,
        )
        return

    text = (message.get("text") or "").strip()
    command, _, broadcast_text = text.partition(" ")
    broadcast_text = broadcast_text.strip()

    if not broadcast_text:
        telegram(
            "sendMessage",
            {
                "chat_id": GROUP_ID,
                "text": "အသုံးပြုပုံ: /broadcast သင့်ရဲ့ စာသား",
            },
        )
        return

    user_ids = list(state.get("user_topics", {}).keys())
    success = 0
    failed = 0

    for user_id in user_ids:
        try:
            telegram(
                "sendMessage",
                {"chat_id": user_id, "text": broadcast_text},
            )
            success += 1
            # Keep a small delay to reduce the chance of Telegram flood limits.
            time.sleep(0.05)
        except Exception as error:
            failed += 1
            print("BROADCAST SEND ERROR:", user_id, repr(error), flush=True)

    telegram(
        "sendMessage",
        {
            "chat_id": GROUP_ID,
            "text": (
                "Broadcast ပြီးပါပြီ။\n"
                f"အောင်မြင်: {success}\n"
                f"မအောင်မြင်: {failed}"
            ),
        },
    )
    print("BROADCAST DONE:", success, "success,", failed, "failed", flush=True)


def reply_to_customer(message):
    """
    Send any admin message from a customer Topic back to that customer.

    This intentionally does NOT require reply_to_message. The admin only
    needs to send a normal message inside the correct customer Topic.
    """
    if ADMIN_IDS:
        sender_id = str(message.get("from", {}).get("id", ""))

        if sender_id not in ADMIN_IDS:
            print(
                "IGNORED: sender is not an allowed admin",
                sender_id,
                flush=True,
            )
            return

    thread_id = message.get("message_thread_id")

    # Messages in General topic may not have a thread ID. Do not forward them.
    if not thread_id:
        print(
            "IGNORED: message has no message_thread_id",
            message.get("message_id"),
            flush=True,
        )
        return

    user_id = user_for_topic(thread_id)

    if not user_id:
        print(
            "NO USER FOR TOPIC:",
            thread_id,
            flush=True,
        )
        return

    # Copy the admin's normal message to the customer chat.
    telegram(
        "copyMessage",
        {
            "chat_id": user_id,
            "from_chat_id": GROUP_ID,
            "message_id": message["message_id"],
        },
    )

    print(
        "REPLIED TO CUSTOMER:",
        user_id,
        "FROM TOPIC:",
        thread_id,
        flush=True,
    )


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

            # Customer sends a private message to the bot.
            if chat_type == "private":
                try:
                    forward_customer_message(message)
                except Exception as error:
                    print("FORWARD ERROR:", repr(error), flush=True)

            # Admin can broadcast from the General topic with /broadcast text.
            elif chat_type == "supergroup" and chat_id == GROUP_ID:
                try:
                    text = (message.get("text") or "").strip()
                    thread_id = message.get("message_thread_id")

                    # General topic messages have no message_thread_id.
                    # Broadcast is intentionally disabled inside customer topics.
                    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
                    if command == "/broadcast" and not thread_id:
                        broadcast_message(message)
                    else:
                        reply_to_customer(message)
                except Exception as error:
                    print("GROUP MESSAGE ERROR:", repr(error), flush=True)

    except requests.RequestException as error:
        print("NETWORK ERROR:", repr(error), flush=True)
        time.sleep(10)

    except Exception as error:
        print("PROGRAM ERROR:", repr(error), flush=True)
        time.sleep(10)
