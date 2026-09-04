"""A2A client: send one JSON-RPC message to an agent and read its reply."""

from __future__ import annotations

import json
import uuid

import httpx

from .cards import base_url


def _text_of(result: dict) -> str:
    for holder in (result, result.get("status", {}) or {}):
        msg = holder.get("message") or {}
        for part in msg.get("parts", []) or []:
            if "text" in part:
                return part["text"]
    for art in result.get("artifacts", []) or []:
        for part in art.get("parts", []) or []:
            if "text" in part:
                return part["text"]
    if isinstance(result.get("parts"), list):
        for part in result["parts"]:
            if "text" in part:
                return part["text"]
    return json.dumps(result)


# message/send over jsonrpc; the payload is json text so agents stay transport-agnostic
async def call_agent(name: str, payload: dict, timeout: float = 300.0) -> dict:
    body = {
        "jsonrpc": "2.0", "id": uuid.uuid4().hex[:12], "method": "message/send",
        "params": {"message": {
            "role": "user", "message_id": uuid.uuid4().hex,
            "parts": [{"text": json.dumps(payload, ensure_ascii=False, default=str)}],
        }},
    }
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(base_url(name) + "/", json=body)
        r.raise_for_status()
        data = r.json()
    if "error" in data:
        return {"error": data["error"]}
    text = _text_of(data.get("result", {}))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


# fetch and validate an agent's published card
async def fetch_card(name: str, timeout: float = 15.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as c:
        for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            r = await c.get(base_url(name) + path)
            if r.status_code == 200:
                return r.json()
    return {"error": f"no agent card served by {name}"}
