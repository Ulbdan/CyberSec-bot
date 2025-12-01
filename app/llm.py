import httpx
from .config import Config

HF_TOKEN = Config.hf_token
HF_MODEL = Config.hf_model  # e.g. "google/gemma-2-2b-it"
HF_URL = "https://router.huggingface.co/v1/chat/completions"


async def llm_echo(text: str) -> str:
    """Simple echo for debugging."""
    return f"Model: {HF_MODEL}\nECHO: {text}"


async def test_llm_connection():
    """Quick ping to HF router."""
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
    """
    Call Hugging Face Inference Providers (OpenAI-style chat API)
    and return the assistant's reply text.
    """
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": HF_MODEL,
        "messages": [
            {
                "role": "user",
                "content": text,
            }
        ],
        "stream": False,
        "max_tokens": 512,          # you can tune this
        "temperature": 0.7,         # creativity
        "top_p": 0.9,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(HF_URL, headers=headers, json=payload)
        r.raise_for_status()

    data = r.json()
    # Defensive parsing
    return data["choices"][0]["message"]["content"]
