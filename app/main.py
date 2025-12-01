import os
import json
import time
import hmac
import hashlib
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .config import Config
from .llm import llm_echo, test_llm_connection, llm_generate

# ------------------------------------------------------
# App setup
# ------------------------------------------------------
app = FastAPI(title="SlackBot HF", version="1.1")

SLACK_SIGNING_SECRET = Config.slack_signing_secret
SLACK_BOT_TOKEN = Config.slack_bot_token

client = WebClient(token=SLACK_BOT_TOKEN)


# ------------------------------------------------------ 
# Verify Slack Request Signature (WITH LOGS)
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
        "v0=" +
        hmac.new(
            SLACK_SIGNING_SECRET.encode(),
            basestring.encode(),
            hashlib.sha256
        ).hexdigest()
    )

    print(f"🔹 Computed signature: {computed}")

    if not hmac.compare_digest(computed, signature):
        print("❌ Signature mismatch — invalid request")
        raise HTTPException(status_code=401, detail="Invalid signature")

    print("✅ Signature verification passed")


# ------------------------------------------------------
# Health check
# ------------------------------------------------------
@app.get("/health")
def health():
    print("💚 HEALTH CHECK HIT")
    return {"ok": True}


# ------------------------------------------------------
# Slack Events Endpoint
# ------------------------------------------------------

@app.post("/slack/events")
async def slack_events(req: Request, bg: BackgroundTasks):
    """
    Main Slack events endpoint.

    Handles:
    - Slack verification
    - Training mode
    - Answer evaluation
    - Default LLM chat
    """
    print("\n🟣 [EVENT] Incoming Slack Event")

    raw_body = await req.body()

    # --- Verify Slack request ---
    try:
        verify_slack(req, raw_body)
    except Exception as e:
        print(f"❌ Verification error: {e}")
        raise

    data = json.loads(raw_body.decode() or "{}")

    # Slack URL verification
    if data.get("type") == "url_verification":
        return {"challenge": data["challenge"]}

    # Main event
    if data.get("type") == "event_callback":
        event = data.get("event", {})
        user = event.get("user")
        channel = event.get("channel")
        text = (event.get("text") or "").strip()

        # Ignore bot messages
        if "bot_id" in event:
            return {"ok": True}

        # Remove mention (if any)
        if "<@" in text:
            text = text.split(">", 1)[1].strip()

        lower_text = text.lower()

                # ---------------------------------------------------
        # STOP TRAINING MODE
        # ---------------------------------------------------
        if lower_text in ["stop training", "exit training", "quit training", "exit"]:
            async def stop_training():
                from .db import users_collection
                await users_collection.update_one(
                    {"slack_user_id": user},
                    {"$set": {"in_training": False}}
                )
                client.chat_postMessage(
                    channel=channel,
                    text=f"🛑 Training mode stopped for <@{user}>. You can now chat normally."
                )
            bg.add_task(stop_training)
            return {"ok": True}

        # ---------------------------------------------------
        # TRAINING MODE TRIGGER
        # ---------------------------------------------------
        if "start training" in lower_text:
            async def send_training_question():
                from .db import users_collection, questions_collection
                import time, random, json, re

                print("🎓 Entering training mode (MCQ)")

                # ---- Fetch or create user document ----
                user_doc = await users_collection.find_one({"slack_user_id": user})

                if not user_doc:
                    user_doc = {
                        "slack_user_id": user,
                        "current_level": 1,
                        "in_training": True,
                        "last_question_number": None,
                        "last_question_answer": None,
                        "last_mcq_correct_option": None,
                        "correct_streak": 0,
                        "updated_at": time.time(),
                    }
                    await users_collection.insert_one(user_doc)
                    print(f"👤 Created new trainee document for user {user}")
                else:
                    await users_collection.update_one(
                        {"_id": user_doc["_id"]},
                        {"$set": {"in_training": True, "updated_at": time.time()}},
                    )
                    print(f"👤 Updated trainee document for user {user} (in_training = True)")

                level = user_doc.get("current_level", 1)

                # ---- Pick random question for this level ----
                query = {"level": level}
                total = await questions_collection.count_documents(query)
                print(f"📚 Questions available for level {level}: {total}")

                if total == 0:
                    msg = (
                        f"Hi <@{user}>! I could not find any training questions "
                        f"for level {level} in the database yet."
                    )
                    client.chat_postMessage(channel=channel, text=msg)
                    return

                skip = random.randint(0, total - 1)
                cursor = questions_collection.find(query).skip(skip).limit(1)
                docs = await cursor.to_list(length=1)

                if not docs:
                    msg = (
                        f"Hi <@{user}>! Something went wrong while fetching "
                        f"a question for level {level}."
                    )
                    client.chat_postMessage(channel=channel, text=msg)
                    return

                q = docs[0]
                print(f"❓ Selected question from DB: {q}")

                db_question = q.get("question_text", "")
                db_answer = q.get("answer_text", "")

                # ---- Ask LLM to turn DB question into MCQ JSON ----
                mcq_prompt = (
                    "You are a cybersecurity training assistant.\n"
                    "You will receive a training item from the database.\n"
                    "Create ONE multiple-choice question with exactly four options A, B, C, and D.\n"
                    "Make sure exactly ONE option is clearly correct.\n"
                    "Respond STRICTLY in this JSON format (no extra text, no markdown, no code fences):\n\n"
                    "{\n"
                    "  \"question\": \"...\",\n"
                    "  \"options\": {\n"
                    "    \"A\": \"...\",\n"
                    "    \"B\": \"...\",\n"
                    "    \"C\": \"...\",\n"
                    "    \"D\": \"...\"\n"
                    "  },\n"
                    "  \"correct_option\": \"A\" | \"B\" | \"C\" | \"D\"\n"
                    "}\n\n"
                    "Do NOT add ```json or ``` anywhere.\n\n"
                    f"Database question: {db_question}\n"
                    f"Reference answer: {db_answer}\n"
                )

                mcq_raw = await llm_generate(mcq_prompt)
                print("🧪 Raw MCQ from LLM:", mcq_raw)

                # ---- Clean and parse JSON ----
                clean = mcq_raw.strip()
                clean = re.sub(r"^```[a-zA-Z]*\s*", "", clean)
                clean = re.sub(r"```$", "", clean)

                start = clean.find("{")
                end = clean.rfind("}")
                if start != -1 and end != -1 and end > start:
                    json_str = clean[start : end + 1]
                else:
                    json_str = clean

                print("🧪 MCQ JSON candidate:", json_str)

                try:
                    mcq = json.loads(json_str)
                    question_text = mcq.get("question", db_question)
                    options = mcq.get("options", {})
                    correct_option = str(mcq.get("correct_option", "")).upper().strip()

                    if correct_option not in ["A", "B", "C", "D"]:
                        raise ValueError("Invalid correct_option in MCQ JSON")

                except Exception as e:
                    print("❌ Failed to parse MCQ JSON:", e)
                    # Fallback: just ask the DB question as open text
                    fallback_msg = (
                        f"🎓 *Training mode* — Level {level}\n\n"
                        f"Question #{q.get('number', '?')}:\n"
                        f"{db_question}\n\n"
                        "(MCQ generation failed, using open question.)"
                    )
                    client.chat_postMessage(channel=channel, text=fallback_msg)
                    return

                # ---- Save correct option & DB answer for this user ----
                await users_collection.update_one(
                    {"slack_user_id": user},
                    {
                        "$set": {
                            "last_question_number": q.get("number"),
                            "last_question_answer": db_answer,
                            "last_mcq_correct_option": correct_option,
                            "updated_at": time.time(),
                        }
                    },
                )

                # ---- Build Slack message with A–D options ----
                optA = options.get("A", "")
                optB = options.get("B", "")
                optC = options.get("C", "")
                optD = options.get("D", "")

                message = (
                    f"🎓 *Training mode* — Level {level}\n\n"
                    f"Question #{q.get('number', '?')}:\n"
                    f"{question_text}\n\n"
                    f"A) {optA}\n"
                    f"B) {optB}\n"
                    f"C) {optC}\n"
                    f"D) {optD}\n\n"
                    f"Please answer by typing A, B, C or D."
                )

                print(f"✉️ Sending MCQ to Slack:\n{message}")
                client.chat_postMessage(channel=channel, text=message)

            bg.add_task(send_training_question)
            return {"ok": True}


        # ---------------------------------------------------
        #  ANSWER EVALUATION (user responds to question)
        # ---------------------------------------------------
        async def evaluate_answer():
            from .db import users_collection, questions_collection
            import time

            user_doc = await users_collection.find_one({"slack_user_id": user})

            # Not in training mode → normal chat
            if not user_doc or not user_doc.get("in_training"):
                return await default_chat()

            expected_answer = user_doc.get("last_question_answer")
            question_number = user_doc.get("last_question_number")
            correct_option = (user_doc.get("last_mcq_correct_option") or "").upper().strip()

            if not correct_option or correct_option not in ["A", "B", "C", "D"]:
                # We don't have MCQ info → fall back to normal chat or old logic
                return await default_chat()

            # --- Extract user choice (A/B/C/D) from the text ---
            user_text = text.strip().upper()

            # Try to get a single letter A–D
            user_choice = None
            for letter in ["A", "B", "C", "D"]:
                if (
                    user_text == letter
                    or user_text.startswith(letter + " ")
                    or f" {letter} " in user_text
                ):
                    user_choice = letter
                    break

            if not user_choice:
                # User did not provide a clear A/B/C/D
                client.chat_postMessage(
                    channel=channel,
                    text=(
                        f"❓ I could not detect a valid option in your answer.\n"
                        f"Please reply with A, B, C or D."
                    ),
                )
                return

            # --- Compare user choice with correct option ---
            is_correct = (user_choice == correct_option)

            current_level = user_doc.get("current_level", 1)
            correct_streak = user_doc.get("correct_streak", 0)
            level_up_threshold = 3  # e.g., 3 correct answers to level up
            level_up_message = ""

            if is_correct:
                correct_streak += 1
                emoji = "✅"
                base_msg = (
                    f"{emoji} *Your answer for Question #{question_number} is CORRECT!* 🎉\n\n"
                    f"*Correct option:* {correct_option}\n"
                )
                if expected_answer:
                    base_msg += f"*Explanation:* {expected_answer}\n"
                print(f"✅ Correct MCQ answer. New streak: {correct_streak}")

                # Check level up condition
                if correct_streak >= level_up_threshold:
                    next_level = current_level + 1
                    next_level_count = await questions_collection.count_documents(
                        {"level": next_level}
                    )
                    if next_level_count > 0:
                        current_level = next_level
                        correct_streak = 0
                        level_up_message = (
                            f"\n\n🏆 You have answered {level_up_threshold} questions correctly in a row.\n"
                            f"You are now promoted to *Level {current_level}*!"
                        )
                    else:
                        level_up_message = (
                            f"\n\nℹ️ You reached the threshold to move to Level {next_level}, "
                            f"but there are no questions configured for that level yet."
                        )

            else:
                emoji = "❌"
                base_msg = (
                    f"{emoji} *Your answer for Question #{question_number} is INCORRECT.*\n\n"
                    f"*Correct option:* {correct_option}\n"
                )
                if expected_answer:
                    base_msg += f"*Explanation:* {expected_answer}\n"
                print("❌ Incorrect MCQ answer. Streak reset to 0.")
                correct_streak = 0

            # Update user level + streak
            await users_collection.update_one(
                {"_id": user_doc["_id"]},
                {
                    "$set": {
                        "current_level": current_level,
                        "correct_streak": correct_streak,
                        "updated_at": time.time(),
                    }
                },
            )

            final_msg = base_msg + level_up_message
            client.chat_postMessage(channel=channel, text=final_msg)


        # User typed something → either evaluate or default chat
        async def default_chat():
            try:
                # Echo
                echo_msg = await llm_echo(text)

                # LLM status ping
                llm_status = await test_llm_connection()

                # Actual LLM answer
                answer = await llm_generate(text)

                final_msg = (
                    f"👋 Hello <@{user}>!\n"
                    f"*Echo:* {echo_msg}\n\n"
                    f"*Answer:*\n{answer}\n\n"
                    f"LLM Status: `{llm_status}`"
                )

                client.chat_postMessage(channel=channel, text=final_msg)

            except Exception as e:
                print("❌ Unexpected error in default chat:", e)
                client.chat_postMessage(
                    channel=channel,
                    text="❌ Sorry, something went wrong in normal chat mode."
                )


        # Evaluate or default
        bg.add_task(evaluate_answer)
        return {"ok": True}

    return {"ok": True}

