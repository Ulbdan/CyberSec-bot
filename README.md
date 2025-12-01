# ============================================================
# 📦 COMPLETE PROJECT — ALL FILES IN ONE CODE BLOCK
# ============================================================

# ============================================================
# 📁 Project Structure
# ============================================================
project-root/
  setup.py
  README.md
  app/
    __init__.py
    config.py
    llm.py
    main.py


# ============================================================
# 📄 setup.py
# ============================================================
from setuptools import setup, find_packages

setup(
    name="slack_hf_bot",
    version="0.1.0",
    description="Slack chatbot using FastAPI + Hugging Face Inference Providers",
    author="Your Name",
    packages=find_packages(exclude=("tests",)),
    install_requires=[
        "fastapi",
        "uvicorn[standard]",
        "slack-sdk",
        "httpx",
    ],
    python_requires=">=3.10",
)


# ============================================================
# 📄 README.md
# ============================================================
# Slack + Hugging Face Inference Providers Bot

A production-ready Slack chatbot powered by FastAPI and Hugging Face’s OpenAI-compatible `/v1/chat/completions` endpoint.

This bot:
- Listens to Slack `app_mention` events  
- Verifies Slack signatures  
- Sends your message to an LLM (Gemma/Phi/etc.)  
- Replies inside Slack with AI answers  

---

## 1️⃣ Install Python Environment

python -m venv venv
source venv/bin/activate        # macOS / Linux
venv\Scripts\activate           # Windows

pip install --upgrade pip
pip install -e .

---

## 2️⃣ Configure Slack App

Go to https://api.slack.com/apps → Create New App.

### Get Your Credentials
Slack → **Basic Information**
- Signing Secret → put in `Config.slack_signing_secret`

Slack → **OAuth & Permissions**
Add Bot Token Scopes:
- app_mentions:read  
- chat:write  

Install to workspace → copy **Bot User OAuth Token (xoxb-...)**  
Put in `Config.slack_bot_token`

Slack → **Event Subscriptions**
- Enable events ON  
- Add bot event: `app_mention`  
- Request URL will be added after ngrok is running

---

## 3️⃣ Configure Hugging Face

1. Go to https://huggingface.co/settings/tokens  
2. Create token with permission:  
   **Make calls to Inference Providers**
3. Copy token → put in `Config.hf_token`

Choose a free model:
- google/gemma-2-2b-it  
- microsoft/Phi-3.5-mini-instruct

Put in: `Config.hf_model`

---

## 4️⃣ Run FastAPI

uvicorn app.main:app --reload

Check health:
http://127.0.0.1:8000/health

---

## 5️⃣ Expose to Slack with ngrok

ngrok config add-authtoken <YOUR_TOKEN>
ngrok http 8000

Copy the HTTPS URL:

https://xxxxxx.ngrok-free.app

Set in Slack → Event Subscriptions → Request URL:

https://xxxxxx.ngrok-free.app/slack/events  
→ should show “Verified”

---

## 6️⃣ Test in Slack

In any channel:

@YourBotName hello

You should get an AI response.

---

## 7️⃣ Done 🎉


# ============================================================
# 📄 app/config.py
# ============================================================
class Config:
    # Slack credentials (replace these)
    slack_signing_secret = "REPLACE_WITH_SLACK_SIGNING_SECRET"
    slack_bot_token = "xoxb-REPLACE_WITH_SLACK_BOT_TOKEN"

    # Hugging Face Inference Providers
    hf_token = "hf_REPLACE_WITH_HF_TOKEN"

    # Free LLM models such as:
    #   google/gemma-2-2b-it
    #   microsoft/Phi-3.5-mini-instruct
    hf_model = "google/gemma-2-2b-it"


# ============================================================
# 📄 app/llm.py
# ============================================================
import httpx
from .config import Config

HF_TOKEN = Config.hf_token
HF_MODEL = Config.hf_model
HF_URL = "https://router.huggingface.co/v1/chat/completions"


async def llm_echo(text: str) -> str:
    return f"Model: {HF_MODEL}\nECHO: {text}"


async def test_llm_connection():
    try:
        headers = {
            "Authorization": f"Bearer {HF_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": HF_MODEL,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(HF_URL, headers=headers, json=payload)
            r.raise_for_status()

        return "HF_ROUTER_OK"
    except Exception as e:
        return f"ERROR: {e}"


async def llm_generate(text: str) -> str:
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": HF_MODEL,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "max_tokens": 512,
        "temperature": 0.7,
        "top_p": 0.9,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(HF_URL, headers=headers, json=payload)
        r.raise_for_status()

    data = r.json()
    return data["choices"][0]["message"]["content"]


# ============================================================
# 📄 app/main.py
# ============================================================
import json
import time
import hmac
import hashlib
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .config import Config
from .llm import llm_echo, test_llm_connection, llm_generate

app = FastAPI(title="SlackBot HF", version="1.1")

SLACK_SIGNING_SECRET = Config.slack_signing_secret
SLACK_BOT_TOKEN = Config.slack_bot_token

client = WebClient(token=SLACK_BOT_TOKEN)


def verify_slack(req: Request, raw_body: bytes):
    timestamp = req.headers.get("X-Slack-Request-Timestamp")
    signature = req.headers.get("X-Slack-Signature")

    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack headers")

    if abs(time.time() - int(timestamp)) > 300:
        raise HTTPException(status_code=401, detail="Stale timestamp")

    basestring = f"v0:{timestamp}:{raw_body.decode()}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        basestring.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/slack/events")
async def slack_events(req: Request, bg: BackgroundTasks):
    raw_body = await req.body()

    try:
        verify_slack(req, raw_body)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

    data = json.loads(raw_body.decode() or "{}")

    if data.get("type") == "url_verification":
        return {"challenge": data["challenge"]}

    if data.get("type") == "event_callback":
        event = data["event"]

        if "bot_id" in event:
            return {"ok": True}

        user = event.get("user")
        channel = event.get("channel")
        text = event.get("text", "")

        if "<@" in text:
            text = text.split(">", 1)[1].strip()

        async def reply():
            echo_msg = await llm_echo(text)
            answer = await llm_generate(text)
            llm_status = await test_llm_connection()

            msg = (
                f"👋 Hello <@{user}>!\n"
                f"*Echo:* {echo_msg}\n\n"
                f"*Answer:*\n{answer}\n\n"
                f"LLM Status: `{llm_status}`"
            )

            client.chat_postMessage(channel=channel, text=msg)

        bg.add_task(reply)

    return {"ok": True}
