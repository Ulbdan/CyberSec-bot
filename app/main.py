import os
import json
import time
import hmac
import hashlib
import re
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .config import Config
from .llm import llm_echo, test_llm_connection, llm_generate
from .db import users_collection, questions_collection  # <-- your Mongo collections

# ------------------------------------------------------
# App setup
# ------------------------------------------------------
app = FastAPI(title="SlackBot HF", version="1.2")

SLACK_SIGNING_SECRET = Config.slack_signing_secret
SLACK_BOT_TOKEN = Config.slack_bot_token

client = WebClient(token=SLACK_BOT_TOKEN)


# ------------------------------------------------------
# Helper: verify Slack signature (same as your teammate)
# ------------------------------------------------------
def verify_slack(req: Request, raw_body: bytes):
    print("\n🔵 [VERIFY] Incoming Slack Request")

    timestamp = req.headers.get("X-Slack-Request-Timestamp")
    signature = req.headers.get("X-Slack-Signature")

    print(f"🔹 Timestamp header: {timestamp}")
    print(f"🔹 Signature header: {signature}")
    print(f"🔹 Raw body: {raw_body.decode()}")

    if not timestamp or not signature:
        print("❌ Missing Slack signature headers")
        raise HTTPException(status_code=401, detail="Missing Slack headers")

    # Clock drift protection
    server_time = time.time()
    diff = abs(server_time - int(timestamp))
    print(f"🕒 Time difference: {diff}s")

    if diff > 300:
        print("❌ Stale timestamp (possible clock drift)")
        raise HTTPException(status_code=401, detail="Stale timestamp")

    basestring = f"v0:{timestamp}:{raw_body.decode()}"
    computed = (
        "v0="
        + hmac.new(
            SLACK_SIGNING_SECRET.encode(),
            basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )

    print(f"🔹 Computed signature: {computed}")

    if not hmac.compare_digest(computed, signature):
        print("❌ Signature mismatch — invalid request")
        raise HTTPException(status_code=401, detail="Invalid signature")

    print("✅ Signature verification passed")


# ------------------------------------------------------
# Small helpers for training mode
# ------------------------------------------------------
async def get_or_create_user(slack_user_id: str) -> dict:
    doc = await users_collection.find_one({"slack_user_id": slack_user_id})
    if doc:
        return doc

    doc = {
        "slack_user_id": slack_user_id,
        "current_level": 1,
        "in_training": False,
        "last_question_number": None,
        "last_question_answer": None,
        "last_mcq_correct_option": None,
        "correct_streak": 0,
        "updated_at": time.time(),
    }
    result = await users_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def extract_mcq_choice(text: str) -> str | None:
    """
    Extract A/B/C/D from user text, ignoring case and extra words.
    """
    t = text.strip().upper()
    for letter in ["A", "B", "C", "D"]:
        if t == letter or t.startswith(letter + " ") or f" {letter} " in t:
            return letter
    return None


async def send_training_question(user_doc: dict, channel: str):
    """
    Pick one question for the user's level and send as multiple choice.
    """
    level = user_doc.get("current_level", 1)

    cursor = questions_collection.aggregate(
        [
            {"$match": {"level": level}},
            {"$sample": {"size": 1}},
        ]
    )

    question = None
    async for q in cursor:
        question = q
        break

    if not question:
        client.chat_postMessage(
            channel=channel,
            text=f"⚠️ I could not find any questions for level {level}.",
        )
        return

    print(f"❓ Selected question from DB: {question}")

    number = question.get("number")
    q_text = question.get("question_text")
    answer_text = question.get("answer_text")

    # Ask LLM to build MCQ options
    mcq_prompt = (
        "You are a cybersecurity training assistant.\n"
        "Given the following question and correct short answer, "
        "create a multiple-choice question with four options A, B, C, and D.\n"
        "Exactly one option must be correct.\n"
        "Return ONLY a JSON object with the following fields:\n"
        "{\n"
        '  "options": {\n'
        '    "A": "...",\n'
        '    "B": "...",\n'
        '    "C": "...",\n'
        '    "D": "..."\n'
        "  },\n"
        '  "correct_option": "A" | "B" | "C" | "D"\n'
        "}\n\n"
        f"Question: {q_text}\n"
        f"Correct short answer: {answer_text}\n"
    )

    try:
        mcq_raw = await llm_generate(mcq_prompt)
        print("🧾 Raw MCQ LLM output:", mcq_raw)

        # Try to extract JSON object from the LLM output
        start = mcq_raw.find("{")
        end = mcq_raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("No JSON object found in MCQ output")

        json_str = mcq_raw[start : end + 1]
        mcq_data = json.loads(json_str)

        options = mcq_data.get("options", {})
        correct_option = (mcq_data.get("correct_option") or "").upper().strip()
        if correct_option not in ["A", "B", "C", "D"]:
            raise ValueError("Invalid or missing correct_option")

    except Exception as e:
        print("❌ Error generating MCQ:", e)
        client.chat_postMessage(
            channel=channel,
            text=(
                "⚠️ I had a problem generating multiple-choice options.\n"
                "Please try `start training` again in a moment."
            ),
        )
        return


    # Save state
    await users_collection.update_one(
        {"_id": user_doc["_id"]},
        {
            "$set": {
                "in_training": True,
                "last_question_number": number,
                "last_question_answer": answer_text,
                "last_mcq_correct_option": correct_option,
                "updated_at": time.time(),
            }
        },
    )

    # Build Slack message
    lines = [
        f"🎓 *Training mode — Level {user_doc.get('current_level', 1)}*",
        "",
        f"*Question #{number}:*",
        q_text,
        "",
        "*Please answer with A, B, C or D:*",
    ]
    for letter in ["A", "B", "C", "D"]:
        if letter in options:
            lines.append(f"{letter}) {options[letter]}")

    lines.append("")
    lines.append(
        "_You can also type `next question` to skip, or `stop training` to exit training mode._"
    )

    client.chat_postMessage(channel=channel, text="\n".join(lines))


async def stop_training(user_doc: dict, channel: str, user: str):
    if not user_doc.get("in_training"):
        client.chat_postMessage(
            channel=channel,
            text=f"ℹ️ You are not currently in training mode, <@{user}>.",
        )
        return

    await users_collection.update_one(
        {"_id": user_doc["_id"]},
        {
            "$set": {
                "in_training": False,
                "last_question_number": None,
                "last_question_answer": None,
                "last_mcq_correct_option": None,
                "correct_streak": 0,
                "updated_at": time.time(),
            }
        },
    )

    client.chat_postMessage(
        channel=channel,
        text=(
            f"🛑 Training mode stopped for <@{user}>.\n"
            "You can now chat normally. Type `start training` again anytime."
        ),
    )


async def evaluate_training_answer(
    user_doc: dict, channel: str, user: str, cleaned_text: str
):
    """
    Evaluate A/B/C/D answer, update level/streak, and send feedback.
    """
    if not user_doc.get("in_training"):
        # Not in training → handled in normal chat
        return False

    expected_answer = user_doc.get("last_question_answer")
    question_number = user_doc.get("last_question_number")
    correct_option = (user_doc.get("last_mcq_correct_option") or "").upper().strip()

    if not correct_option or correct_option not in ["A", "B", "C", "D"]:
        return False  # no MCQ state, let normal chat handle it

    choice = extract_mcq_choice(cleaned_text)
    if not choice:
        client.chat_postMessage(
            channel=channel,
            text=(
                "❓ I could not detect a valid option in your answer.\n"
                "Please reply with *A, B, C or D*, or type `stop training` to exit."
            ),
        )
        return True  # handled as training

    current_level = user_doc.get("current_level", 1)
    streak = user_doc.get("correct_streak", 0)
    level_up_threshold = 3

    if choice == correct_option:
        streak += 1
        base_msg = (
            f"✅ *Your answer for Question #{question_number} is CORRECT!* 🎉\n\n"
            f"*Correct option:* {correct_option}\n"
        )
        if expected_answer:
            base_msg += f"*Explanation:* {expected_answer}\n"

        level_up_msg = ""
        if streak >= level_up_threshold:
            next_level = current_level + 1
            next_count = await questions_collection.count_documents(
                {"level": next_level}
            )

            if next_count > 0:
                current_level = next_level
                streak = 0
                level_up_msg = (
                    f"\n\n🏆 You have answered {level_up_threshold} "
                    f"questions correctly in a row.\n"
                    f"You are now promoted to *Level {current_level}*!"
                )
            else:
                level_up_msg = (
                    f"\n\nℹ️ You reached the threshold to move to Level {next_level}, "
                    "but there are no questions configured for that level yet."
                )

        msg = base_msg + level_up_msg

    else:
        streak = 0
        msg = (
            f"❌ *Your answer for Question #{question_number} is INCORRECT.*\n\n"
            f"*Correct option:* {correct_option}\n"
        )
        if expected_answer:
            msg += f"*Explanation:* {expected_answer}\n"

    await users_collection.update_one(
        {"_id": user_doc["_id"]},
        {
            "$set": {
                "current_level": current_level,
                "correct_streak": streak,
                "updated_at": time.time(),
            }
        },
    )

    msg += (
        "\n\n➡️ Type `next question` for another question, "
        "or `stop training` to exit training mode."
    )

    client.chat_postMessage(channel=channel, text=msg)
    return True  # handled as training


# ------------------------------------------------------
# Health check (igual que antes)
# ------------------------------------------------------
@app.get("/health")
def health():
    print("💚 HEALTH CHECK HIT")
    return {"ok": True}


# ------------------------------------------------------
# Slack Events Endpoint (misma estructura que el original)
# ------------------------------------------------------
@app.post("/slack/events")
async def slack_events(req: Request, bg: BackgroundTasks):
    print("\n🟣 [EVENT] Incoming Slack Event")

    # Ignore Slack retries to avoid duplicates
    if req.headers.get("X-Slack-Retry-Num"):
        print("🔁 Ignoring Slack retry:", req.headers.get("X-Slack-Retry-Reason"))
        return {"ok": True}

    raw_body = await req.body()

    # --- Verify Slack request ---
    try:
        verify_slack(req, raw_body)
    except Exception as e:
        print(f"❌ [EVENT] Verification error: {e}")
        raise

    print("✅ [EVENT] Slack request verified")

    # Parse JSON body
    data = json.loads(raw_body.decode() or "{}")
    print(f"📨 Parsed event: {json.dumps(data, indent=2)}")

    # URL verification challenge
    if data.get("type") == "url_verification":
        print("🔧 Responding to Slack challenge")
        return {"challenge": data["challenge"]}

    # Actual events
    if data.get("type") == "event_callback":
        event = data["event"]
        print(f"🔄 Processing event: {event}")

        event_type = event.get("type")
        channel_type = event.get("channel_type")

        print(f"🔍 Event type: {event_type}, channel_type: {channel_type}")

        # Only handle:
        #  - app_mention in channels
        #  - direct messages (message in IM)
        if not (
            event_type == "app_mention"
            or (event_type == "message" and channel_type == "im")
        ):
            print("🚫 Ignoring event (not app_mention or DM message)")
            return {"ok": True}


        # Ignore bot messages
        if "bot_id" in event:
            print("🤖 Ignored bot message")
            return {"ok": True}

        user = event.get("user")
        channel = event.get("channel")
        text = event.get("text", "") or ""

        # Remove mentions like <@U12345> to get the real text
        cleaned_text = re.sub(r"<@[^>]+>", "", text).strip()

        print(f"👤 User: {user}")
        print(f"💬 Raw Message: '{text}'")
        print(f"🧹 Cleaned Message: '{cleaned_text}'")
        print(f"📡 Channel: {channel}")

        async def reply():
            try:
                # Load or create trainee document
                user_doc = await get_or_create_user(user)
                lower = cleaned_text.lower()

                # ----- TRAINING COMMANDS -----
                if "start training" in lower:
                    print("🎓 User requested START TRAINING")
                    # Ensure in_training flag and send first question
                    await users_collection.update_one(
                        {"_id": user_doc["_id"]},
                        {
                            "$set": {
                                "in_training": True,
                                "updated_at": time.time(),
                            }
                        },
                    )
                    await send_training_question(user_doc, channel)
                    return

                if "stop training" in lower:
                    print("🛑 User requested STOP TRAINING")
                    await stop_training(user_doc, channel, user)
                    return

                if "next question" in lower or lower == "next":
                    print("➡️ User requested NEXT QUESTION")
                    if user_doc.get("in_training"):
                        await send_training_question(user_doc, channel)
                    else:
                        client.chat_postMessage(
                            channel=channel,
                            text=(
                                "ℹ️ You are not in training mode. "
                                "Type `start training` to begin."
                            ),
                        )
                    return

                # ----- TRAINING ANSWERS (A/B/C/D) -----
                if user_doc.get("in_training"):
                    handled = await evaluate_training_answer(
                        user_doc, channel, user, cleaned_text
                    )
                    if handled:
                        return
                    # if not handled, fall through to normal chat

                # ----- NORMAL CHAT (ORIGINAL BEHAVIOUR) -----
                echo_msg = await llm_echo(cleaned_text)
                llm_status = await test_llm_connection()
                answer = await llm_generate(cleaned_text)

                final_msg = (
                    f"👋 Hello <@{user}>!\n"
                    f"*Echo:* {echo_msg}\n\n"
                    f"*Answer:*\n{answer}\n\n"
                    f"LLM Status: `{llm_status}`"
                )

                print(f"✉️ Sending Slack reply:\n{final_msg}")
                client.chat_postMessage(channel=channel, text=final_msg)

            except SlackApiError as e:
                print("❌ Slack API Error:", e)
                print("❌ Full error:", e.response)
            except Exception as e:
                print("❌ Unexpected error in reply():", e)

        bg.add_task(reply)

    return {"ok": True}
