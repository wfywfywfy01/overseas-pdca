"""Shared utilities: auth, API client, memory I/O, date helpers."""

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

TENANT_ID         = int(os.environ["TENANT_ID"])
API_KEY           = os.environ["API_KEY"]
BASE_URL          = os.environ.get("SALESEPOCH_BASE_URL", "https://api.socialepoch.com")
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL    = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
OLLAMA_BASE_URL   = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL      = os.environ.get("OLLAMA_MODEL", "nous-hermes2")

AGENTS_DIR = Path(__file__).parent


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_headers() -> dict[str, str]:
    timestamp = int(time.time() * 1000)
    raw   = f"{TENANT_ID}{timestamp}{API_KEY}"
    token = hashlib.md5(raw.encode()).hexdigest()
    return {
        "tenant_id":    str(TENANT_ID),
        "timestamp":    str(timestamp),
        "token":        token,
        "Content-Type": "application/json",
    }


# ── SalesEpoch API client ─────────────────────────────────────────────────────

def api_post(endpoint: str, payload: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    url = BASE_URL + endpoint
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, headers=get_headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 200:
                return data
            logger.warning("[API] non-200 code from %s: %s (payload=%s)",
                           endpoint, data.get("message"), payload)
            return data
        except requests.RequestException as exc:
            if attempt == retries - 1:
                logger.error("[API] failed %s after %d attempts: %s", endpoint, retries, exc)
                raise
            time.sleep(1)
    return {}


def api_post_paged(endpoint: str, base_payload: dict[str, Any], page_size: int = 100,
                   info_filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fetch all pages from a paginated endpoint.

    For msgPage: filters go in payload['info']; pagination goes at root level.
    For other endpoints: filters at root level.
    """
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = {**base_payload, "current": page, "pageSize": page_size}
        if info_filters is not None:
            payload["info"] = info_filters
        data = api_post(endpoint, payload)
        records = data.get("data", {})
        if isinstance(records, list):
            items = records
            total_pages = 1
        elif isinstance(records, dict):
            items = records.get("records", records.get("list", records.get("data", [])))
            total_pages = records.get("pages", records.get("totalPage", 1))
        else:
            break
        results.extend(items)
        if page >= total_pages or not items:
            break
        page += 1
        time.sleep(0.05)  # stay under 50 req/s
    return results


# ── Date helpers ──────────────────────────────────────────────────────────────

def yesterday_str() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def day_range_ms(date_str: str) -> tuple[int, int]:
    """Return (start_ms, end_ms) for a YYYY-MM-DD date, midnight to midnight."""
    dt    = datetime.strptime(date_str, "%Y-%m-%d")
    start = int(dt.timestamp() * 1000)
    end   = int((dt + timedelta(days=1)).timestamp() * 1000) - 1
    return start, end


# ── MEMORY.md helpers ─────────────────────────────────────────────────────────

def load_memory(agent_name: str) -> str:
    path = AGENTS_DIR / agent_name / "MEMORY.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def append_memory(agent_name: str, section: str, content: str) -> None:
    """Append a timestamped entry to an agent's MEMORY.md."""
    path  = AGENTS_DIR / agent_name / "MEMORY.md"
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n### [{ts}] {section}\n{content.strip()}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    logger.debug("[MEMORY] %s: appended '%s'", agent_name, section)


def bump_memory_version(agent_name: str) -> None:
    """Increment the version counter in the first line of MEMORY.md."""
    import re
    path = AGENTS_DIR / agent_name / "MEMORY.md"
    if not path.exists():
        return
    text  = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "版本" in line and "v" in line:
            match = re.search(r"v(\d+)", line)
            if match:
                new_v    = int(match.group(1)) + 1
                lines[i] = re.sub(r"v\d+", f"v{new_v}", line)
            ts       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lines[i] = re.sub(r"最后更新：[\d\- :]+", f"最后更新：{ts}", lines[i])
            break
    path.write_text("\n".join(lines), encoding="utf-8")


# ── Reports dir ───────────────────────────────────────────────────────────────

def report_path(date_str: str, filename: str) -> Path:
    p = AGENTS_DIR / "reports" / date_str
    p.mkdir(parents=True, exist_ok=True)
    return p / filename


# ── DeepSeek API ──────────────────────────────────────────────────────────────

def deepseek_chat(system_prompt: str, user_content: str) -> str:
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "your_deepseek_api_key_here":
        return '{"error": "DEEPSEEK_API_KEY not configured"}'
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens":  1500,
    }
    resp = requests.post(
        f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
        json=payload, headers=headers, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ── Ollama (Nous Hermes 本地) ──────────────────────────────────────────────────

def hermes_chat(system_prompt: str, user_content: str, timeout: int = 120) -> str:
    """Call local Nous Hermes 2 via Ollama. Data never leaves the machine."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
        "stream":  False,
        "options": {"temperature": 0.3, "num_predict": 1200},
    }
    try:
        resp = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except requests.RequestException as exc:
        logger.warning("[Hermes] 调用失败: %s", exc)
        return ""


def hermes_available() -> bool:
    try:
        r      = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        return any(OLLAMA_MODEL in m for m in models)
    except Exception:
        return False
