"""Account-specific web model discovery; no credentials in responses or cache keys."""
import hashlib
import time

from fastapi import HTTPException

from chatgpt.authorization import verify_token
from chatgpt.fp import get_fp
from utils.Client import Client
from utils.configs import chatgpt_base_url_list
from chatgpt import errorObservation as observation

_cache = {}


async def get_model_catalog(req_token, history_disabled=True):
    observation.stage("model_catalog")
    if not req_token:
        raise HTTPException(status_code=401, detail="Configure a ChatGPT account first")
    key = (hashlib.sha256(req_token.encode()).hexdigest(), bool(history_disabled))
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    parts = req_token.split(",", 1)
    token = await verify_token(parts[0])
    fp = get_fp(req_token).copy()
    proxy = fp.pop("proxy_url", None)
    impersonate = fp.pop("impersonate", "chrome123")
    headers = {**fp, "authorization": "Bearer " + token,
               "origin": "https://chatgpt.com", "referer": "https://chatgpt.com/"}
    if len(parts) == 2:
        headers["chatgpt-account-id"] = parts[1]
    client = Client(proxy=proxy, impersonate=impersonate)
    try:
        host = chatgpt_base_url_list[0] if chatgpt_base_url_list else "https://chatgpt.com"
        response = await client.get(host + "/backend-api/models", headers=headers,
                                    params={"history_and_training_disabled": str(bool(history_disabled)).lower()},
                                    timeout=20)
        observation.upstream_status(response.status_code)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code,
                                detail="ChatGPT rejected model discovery")
        payload = response.json()
        models = [{k: m[k] for k in ("slug", "title", "thinking_efforts") if k in m}
                  for m in payload.get("models", []) if isinstance(m, dict) and isinstance(m.get("slug"), str)]
        if not models:
            raise HTTPException(status_code=502, detail="ChatGPT returned no usable model catalog")
        _cache[key] = (time.monotonic(), models)
        return models
    except HTTPException as error:
        observation.capture(error)
        raise
    except Exception as error:
        observation.capture(error)
        raise HTTPException(status_code=502, detail="Could not fetch ChatGPT model catalog")
    finally:
        await client.close()


def select_thinking_effort(model, reasoning_effort=None, thinking_effort=None):
    # Explicit local compatibility mapping, not an official API/web equivalence.
    aliases = {"low": "min", "medium": "standard", "high": "extended", "xhigh": "max"}
    if any(value is not None and not isinstance(value, str) for value in (reasoning_effort, thinking_effort)):
        raise HTTPException(status_code=400, detail="Thinking effort must be a string")
    supported = [e.get("thinking_effort") for e in model.get("thinking_efforts", [])]
    mapped = aliases.get(reasoning_effort, reasoning_effort)
    if thinking_effort is not None and mapped is not None and thinking_effort != mapped:
        raise HTTPException(status_code=400, detail="Conflicting reasoning_effort and thinking_effort")
    selected = thinking_effort if thinking_effort is not None else mapped
    if selected is not None and selected not in supported:
        raise HTTPException(status_code=400, detail={"error": "Unsupported thinking effort",
                                                   "supported_thinking_efforts": supported})
    return selected
