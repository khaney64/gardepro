"""LLM image analysis — supports local llama.cpp (OpenAI-compatible) and Anthropic Claude."""
import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Optional

import requests

_CONFIG_PATH = Path.home() / ".gardepro" / "analysis_config.json"

_DEFAULTS = {
    "analyze_enabled": True,
    "alerts_enabled": False,
    "backend": "llm",
    "llm_url": os.environ.get("GARDEPRO_LLM_URL", "http://devbox.lan:8080").rstrip("/"),
    "llm_model": os.environ.get("GARDEPRO_LLM_MODEL", "").strip(),
    "anthropic_model": "claude-haiku-4-5-20251001",
    "prompt": (
        "This is a trail camera image. List every animal and person you can see. "
        "Be concise — just name what you observe, one item per line."
    ),
    "max_tokens": 800,
    "temperature": 0.1,
}

_KEYWORDS = [
    "cat", "raccoon", "deer", "fox", "dog", "rabbit", "bird",
    "person", "human", "legs", "squirrel", "possum", "opossum",
    "skunk", "bear", "coyote", "turkey",
]

_session = requests.Session()
_session.trust_env = False


def _load_config() -> dict:
    """Load analysis config from file, falling back to defaults."""
    cfg = dict(_DEFAULTS)
    if _CONFIG_PATH.exists():
        try:
            saved = json.loads(_CONFIG_PATH.read_text())
            cfg.update({k: v for k, v in saved.items() if k in _DEFAULTS})
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> dict:
    """Persist config dict to file. Returns the saved config."""
    merged = _load_config()
    merged.update({k: v for k, v in cfg.items() if k in _DEFAULTS})
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(merged, indent=2))
    return merged


def _parse_subjects(text: str) -> list[str]:
    lower = text.lower()
    found = []
    for kw in _KEYWORDS:
        if kw in lower and kw not in found:
            found.append(kw)
    if "opossum" in found and "possum" not in found:
        found.append("possum")
    return found


def _call_local(thumb_path: str, cfg: dict) -> dict:
    url = cfg["llm_url"].rstrip("/")
    data = Path(thumb_path).read_bytes()
    b64  = base64.b64encode(data).decode()
    payload = {
        "model": cfg["llm_model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": cfg["prompt"]},
                ],
            }
        ],
        "max_tokens": int(cfg["max_tokens"]),
        "temperature": float(cfg["temperature"]),
    }
    resp = _session.post(f"{url}/v1/chat/completions", json=payload, timeout=(5, 90))
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return {"subjects": _parse_subjects(text), "description": text.strip(), "engine": f"Local ({cfg['llm_model']})"}


def _call_anthropic(thumb_path: str, cfg: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    data = Path(thumb_path).read_bytes()
    b64  = base64.b64encode(data).decode()
    payload = {
        "model": cfg["anthropic_model"],
        "max_tokens": int(cfg["max_tokens"]),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    }},
                    {"type": "text", "text": cfg["prompt"]},
                ],
            }
        ],
    }
    resp = _session.post(
        "https://api.anthropic.com/v1/messages",
        json=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=(5, 90),
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    return {"subjects": _parse_subjects(text), "description": text.strip(), "engine": f"Anthropic ({cfg['anthropic_model']})"}


def _call(thumb_path: str, cfg: dict) -> dict:
    if cfg.get("backend") == "anthropic":
        return _call_anthropic(thumb_path, cfg)
    return _call_local(thumb_path, cfg)


async def analyze_image(thumb_path: str, config: Optional[dict] = None) -> dict:
    """Send thumbnail to configured LLM backend. Never raises."""
    cfg = config if config is not None else _load_config()
    backend = cfg.get("backend", "llm")
    if backend == "llm" and not (cfg.get("llm_url") and cfg.get("llm_model")):
        return {"subjects": [], "description": "", "error": "LLM URL or model not configured"}
    if backend == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return {"subjects": [], "description": "", "error": "ANTHROPIC_API_KEY not set"}
    try:
        return await asyncio.to_thread(_call, thumb_path, cfg)
    except Exception as exc:
        return {"subjects": [], "description": "", "error": str(exc)}
