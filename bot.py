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
            "pending_broadcast_admins": {},
            "broadcast_button_message_id": None,
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            value = json.load(file)

        value.setdefault("user_topics", {})
        value.setdefault("group_messages", {})
        value.setdefault("pending_broadcast_admins", {})
        value.setdefault("broadcast_button_message_id", None)
        return value

    except Exception as error:
        print("STATE LOAD ERROR:", repr(error), flush=True)
        return {
            "user_topics": {},
            "group_messages": {},
            "pending_broadcast_admins": {},
            "broadcast_button_message_id": None,
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
    existing = state.setdefault("user_topics", {}).get(user_id)

    if existing:
        return int(existing)

    # Recover the old topic from previous saved group-message mappings.
    # This prevents a new topic when user_topics was partially lost.
    for saved in state.get("group_messages", {}).values():
        if str(saved.get("user_id")) == user_id and saved.get("thread_id"):
            recovered_thread_id = int(saved["thread_id"])
            state["user_topics"][user_id] = recovered_thread_id
            save_state()
            print("TOPIC RECOVERED:", user_id, recovered_thread_id, flush=True)
            return recovered_thread_id

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


def is_allowed_admin(message_or_user_id):
    """Allow only Telegram group administrators/creator; no user ID env needed."""
    if isinstance(message_or_user_id, dict):
        user_id = str(message_or_user_id.get("from", {}).get("id", ""))
    else:
        user_id = str(message_or_user_id)

    if not user_id:
        return False

    try:
        member = telegram(
            "getChatMember",
            {
                "chat_id": GROUP_ID,
                "user_id": user_id,
            },
        )
        return member.get("status") in {"administrator", "creator"}
    except Exception as error:
        print("ADMIN CHECK ERROR:", repr(error), flush=True)
        return False


def ensure_broadcast_button():
    """Show one Broadcast button in the group's General topic."""
    if state.get("broadcast_button_message_id"):
        return

    sent = telegram(
        "sendMessage",
        {
            "chat_id": GROUP_ID,
            "text": "Broadcast လုပ်ရန် အောက်က button ကိုနှိပ်ပါ။",
            "reply_markup": json.dumps({
                "inline_keyboard": [[{
                    "text": "📢 Broadcast",
                    "callback_data": "start_broadcast",
                }]],
            }),
        },
    )
    state["broadcast_button_message_id"] = sent["message_id"]
    save_state()


def handle_broadcast_button(callback_query):
    user_id = str(callback_query.get("from", {}).get("id", ""))
    callback_id = callback_query.get("id")

    if callback_id:
        telegram("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Admin စစ်ဆေးနေပါတယ်...",
            "show_alert": False,
        })

    if not is_allowed_admin(user_id):
        telegram("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "ဒီ Telegram account ကို Broadcast သုံးခွင့်မပေးထားပါ။ ALLOWED_ADMIN_IDS စစ်ပါ။",
            "show_alert": True,
        })
        print("BROADCAST BUTTON DENIED:", user_id, flush=True)
        return

    state.setdefault("pending_broadcast_admins", {})[user_id] = True
    save_state()

    telegram(
        "sendMessage",
        {
            "chat_id": GROUP_ID,
            "text": "Broadcast ပို့မည့်စာကို ဒီ message ကို reply လုပ်ပြီး ပို့ပါ။",
            "reply_markup": json.dumps({
                "force_reply": True,
                "input_field_placeholder": "Broadcast စာသားရေးပါ",
            }),
        },
    )


def send_broadcast_text(message, broadcast_text):
    if not is_allowed_admin(message) or not broadcast_text.strip():
        return

    success = 0
    failed = 0
    for user_id in list(state.get("user_topics", {}).keys()):
        try:
            telegram("sendMessage", {
                "chat_id": user_id,
                "text": broadcast_text.strip(),
            })
            success += 1
            time.sleep(0.05)
        except Exception as error:
            failed += 1
            print("BROADCAST SEND ERROR:", user_id, repr(error), flush=True)

    telegram("sendMessage", {
        "chat_id": GROUP_ID,
        "text": f"Broadcast ပြီးပါပြီ။\\nအောင်မြင်: {success}\\nမအောင်မြင်: {failed}",
    })


def broadcast_command(message):
    text = (message.get("text") or "").strip()
    _, _, broadcast_text = text.partition(" ")
    if broadcast_text.strip():
        send_broadcast_text(message, broadcast_text)
    else:
        telegram("sendMessage", {
            "chat_id": GROUP_ID,
            "text": "အသုံးပြုပုံ: /broadcast သင့်ရဲ့ စာသား",
        })


def reply_to_customer(message):
    """
    Send any admin message from a customer Topic back to that customer.

    This intentionally does NOT require reply_to_message. The admin only
    needs to send a normal message inside the correct customer Topic.
    """
    if not is_allowed_admin(message):
        print(
            "IGNORED: sender is not a group admin",
            message.get("from", {}).get("id"),
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
try:
    ensure_broadcast_button()
except Exception as error:
    print("BROADCAST BUTTON ERROR:", repr(error), flush=True)

while True:
    try:
        response = requests.get(
            f"{API}/getUpdates",
            params={
                "offset": offset,
                "timeout": 25,
                "allowed_updates": json.dumps(["message", "callback_query"]),
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
            callback_query = update.get("callback_query")
            if callback_query:
                try:
                    if callback_query.get("data") == "start_broadcast":
                        handle_broadcast_button(callback_query)
                except Exception as error:
                    print("CALLBACK ERROR:", repr(error), flush=True)
                continue

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

            # Admin commands and replies in the target supergroup.
            elif chat_type == "supergroup" and chat_id == GROUP_ID:
                try:
                    text = (message.get("text") or "").strip()
                    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
                    thread_id = message.get("message_thread_id")
                    sender_id = str(message.get("from", {}).get("id", ""))

                    # A pending button request accepts the next General-topic text.
                    if (
                        not thread_id
                        and state.get("pending_broadcast_admins", {}).pop(sender_id, False)
                        and text
                        and not text.startswith("/")
                    ):
                        save_state()
                        send_broadcast_text(message, text)
                    elif command == "/broadcast" and not thread_id:
                        broadcast_command(message)
                    else:
                        # Original customer-topic reply flow remains unchanged.
                        reply_to_customer(message)
                except Exception as error:
                    print("REPLY ERROR:", repr(error), flush=True)

    except requests.RequestException as error:
        print("NETWORK ERROR:", repr(error), flush=True)
        time.sleep(10)

    except Exception as error:
        print("PROGRAM ERROR:", repr(error), flush=True)
        time.sleep(10)
