"""LLM image analysis — supports local llama.cpp (OpenAI-compatible) and Anthropic Claude."""
import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests

_log = logging.getLogger(__name__)

_CONFIG_PATH = Path.home() / ".gardepro" / "analysis_config.json"

_DEFAULTS = {
    "analyze_enabled": True,
    "alerts_enabled": False,
    "chat_enabled": False,
    "backend": "llm",
    "llm_url": os.environ.get("GARDEPRO_LLM_URL", "http://devbox.lan:8080").rstrip("/"),
    "llm_model": os.environ.get("GARDEPRO_LLM_MODEL", "").strip(),
    "anthropic_model": "claude-haiku-4-5-20251001",
    "prompt": (
        "This is a trail camera image. List every animal and person you can see. "
        "Be concise — just name what you observe, one item per line."
    ),
    "max_tokens": 800,
    "thinking_budget": 2048,
    "temperature": 0.1,
    "alert_cooldown_minutes": 30,
    "alert_rules_enabled": {},  # {} = all rules enabled; {"person": False} to disable specific rules
    "battery_warning_threshold": 25,  # % — first low-battery alert; re-alerts every 5% below
}

_KEYWORDS = [
    "cat", "raccoon", "deer", "fox", "dog", "rabbit", "bird",
    "person", "human", "legs", "squirrel", "possum", "opossum",
    "skunk", "bear", "coyote", "turkey",
    "cupcake", "sox",
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


def _parse_subjects(text: str) -> tuple[list[str], dict[str, int]]:
    lower = text.lower()
    found = []
    confidence: dict[str, int] = {}
    for kw in _KEYWORDS:
        if kw in lower and kw not in found:
            found.append(kw)
            m = re.search(rf'\b{re.escape(kw)}\b[^\[]*\[([0-5])\]', lower)
            confidence[kw] = int(m.group(1)) if m else 0
    if "opossum" in found and "possum" not in found:
        found.append("possum")
        confidence["possum"] = confidence.get("opossum", 0)
    return found, confidence


def _call_local(thumb_path: str, cfg: dict) -> dict:
    url = cfg["llm_url"].rstrip("/")
    data = Path(thumb_path).read_bytes()
    b64  = base64.b64encode(data).decode()
    thinking_budget = int(cfg.get("thinking_budget", 0))
    payload = {
        "model": cfg["llm_model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": cfg["prompt"]},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": int(cfg["max_tokens"]) + thinking_budget,
        "temperature": float(cfg["temperature"]),
    }
    if thinking_budget > 0:
        payload["chat_template_kwargs"] = {
            "enable_thinking": True,
            "thinking_budget": thinking_budget,
        }
    resp = _session.post(f"{url}/v1/chat/completions", json=payload, timeout=(5, 90))
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"] or ""
    subjects, confidence = _parse_subjects(text)
    return {"subjects": subjects, "subject_confidence": confidence, "description": text.strip(), "engine": f"Local ({cfg['llm_model']})"}


def _call_anthropic(thumb_path: str, cfg: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    data = Path(thumb_path).read_bytes()
    b64  = base64.b64encode(data).decode()
    thinking_budget = int(cfg.get("thinking_budget", 0))
    payload = {
        "model": cfg["anthropic_model"],
        "max_tokens": int(cfg["max_tokens"]) + thinking_budget,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": cfg["prompt"]},
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    }},
                ],
            }
        ],
    }
    if thinking_budget > 0:
        payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
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
    # With thinking enabled the content array may start with a thinking block; find the text block.
    content_blocks = resp.json()["content"]
    text = next((b["text"] for b in content_blocks if b.get("type") == "text"), "")
    subjects, confidence = _parse_subjects(text)
    return {"subjects": subjects, "subject_confidence": confidence, "description": text.strip(), "engine": f"Anthropic ({cfg['anthropic_model']})"}


def _call(thumb_path: str, cfg: dict) -> dict:
    if cfg.get("backend") == "anthropic":
        return _call_anthropic(thumb_path, cfg)
    return _call_local(thumb_path, cfg)


def _call_raw(thumb_path: str, cfg: dict) -> dict:
    """Like _call() but returns only raw text — no subject parsing (for chat)."""
    data = Path(thumb_path).read_bytes()
    b64  = base64.b64encode(data).decode()
    thinking_budget = int(cfg.get("thinking_budget", 0))
    if cfg.get("backend") == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        payload = {
            "model": cfg["anthropic_model"],
            "max_tokens": int(cfg["max_tokens"]) + thinking_budget,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": cfg["prompt"]},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            ]}],
        }
        if thinking_budget > 0:
            payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        resp = _session.post(
            "https://api.anthropic.com/v1/messages", json=payload,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            timeout=(5, 90),
        )
        resp.raise_for_status()
        content_blocks = resp.json()["content"]
        text = next((b["text"] for b in content_blocks if b.get("type") == "text"), "")
        return {"description": text.strip()}
    else:
        url = cfg["llm_url"].rstrip("/")
        payload = {
            "model": cfg["llm_model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": cfg["prompt"]},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            "max_tokens": int(cfg["max_tokens"]) + thinking_budget,
            "temperature": float(cfg["temperature"]),
        }
        if thinking_budget > 0:
            payload["chat_template_kwargs"] = {
                "enable_thinking": True,
                "thinking_budget": thinking_budget,
            }
        resp = _session.post(f"{url}/v1/chat/completions", json=payload, timeout=(5, 90))
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"] or ""
        return {"description": text.strip()}


async def chat_image(thumb_path: str, prompt: str, config: Optional[dict] = None) -> dict:
    """One-shot image chat with a custom prompt. Returns raw text only. Never raises."""
    cfg = {**(config if config is not None else _load_config()), "prompt": prompt}
    backend = cfg.get("backend", "llm")
    if backend == "llm" and not (cfg.get("llm_url") and cfg.get("llm_model")):
        return {"description": "", "error": "LLM URL or model not configured"}
    if backend == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return {"description": "", "error": "ANTHROPIC_API_KEY not set"}
    try:
        result = await asyncio.to_thread(_call_raw, thumb_path, cfg)
        if not result.get("description") and int(cfg.get("thinking_budget", 0)) > 0:
            budget = int(cfg["thinking_budget"]) * 2
            _log.warning("chat empty response — retrying with thinking_budget=%d", budget)
            retry_cfg = {**cfg, "thinking_budget": budget}
            result = await asyncio.to_thread(_call_raw, thumb_path, retry_cfg)
            if result.get("description"):
                _log.info("chat retry succeeded (thinking_budget=%d)", budget)
            else:
                _log.warning("chat retry also returned empty (thinking_budget=%d)", budget)
        return result
    except Exception as exc:
        return {"description": "", "error": str(exc)}


async def analyze_image(thumb_path: str, config: Optional[dict] = None) -> dict:
    """Send thumbnail to configured LLM backend. Never raises."""
    cfg = config if config is not None else _load_config()
    backend = cfg.get("backend", "llm")
    if backend == "llm" and not (cfg.get("llm_url") and cfg.get("llm_model")):
        return {"subjects": [], "description": "", "error": "LLM URL or model not configured"}
    if backend == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return {"subjects": [], "description": "", "error": "ANTHROPIC_API_KEY not set"}
    try:
        result = await asyncio.to_thread(_call, thumb_path, cfg)
        if not result.get("description") and int(cfg.get("thinking_budget", 0)) > 0:
            budget = int(cfg["thinking_budget"]) * 2
            _log.warning("analysis empty response — retrying with thinking_budget=%d", budget)
            retry_cfg = {**cfg, "thinking_budget": budget}
            result = await asyncio.to_thread(_call, thumb_path, retry_cfg)
            if result.get("description"):
                _log.info("analysis retry succeeded (thinking_budget=%d)", budget)
            else:
                _log.warning("analysis retry also returned empty (thinking_budget=%d)", budget)
        if (not result.get("error")
                and result.get("subjects")
                and all(v <= 3 for v in result.get("subject_confidence", {}).values())
                and int(cfg.get("thinking_budget", 0)) > 0):
            budget = int(cfg["thinking_budget"]) * 2
            _log.warning("analysis low-confidence — retrying with thinking_budget=%d", budget)
            retry_cfg = {**cfg, "thinking_budget": budget}
            result = await asyncio.to_thread(_call, thumb_path, retry_cfg)
            if any(v > 3 for v in result.get("subject_confidence", {}).values()):
                _log.info("analysis confidence retry succeeded (thinking_budget=%d)", budget)
            else:
                _log.warning("analysis confidence retry still low (thinking_budget=%d)", budget)
        return result
    except Exception as exc:
        return {"subjects": [], "description": "", "error": str(exc)}
