"""Bounded text continuation; client functions are parsed only after assembly."""
import asyncio
import copy
import json
import time

from fastapi import HTTPException

from api.client_tools import fail
from chatgpt import errorObservation as observation
from api.tokens import num_tokens_from_content, split_tokens_from_content


def output_limit(data, default):
    value = data.get("max_completion_tokens")
    if value is None:
        value = data.get("max_tokens")
    if value is None:
        value = default
    if type(value) is not int or value < 1:
        fail(400, "invalid_max_tokens", "Token limit must be a positive integer")
    return value


def unwrap_escape(text, *, partial=False, previously_wrapped=False, initial=True):
    """Decode the web renderer's exact outer escape wrapper, without evaluating code.

    An interrupted opening wrapper is transport state carried across turns.
    Function JSON still has to pass the full nonce/schema parser afterwards.
    """
    wrapped = previously_wrapped
    if initial and text.startswith("<escape>") and (partial or text.endswith("</escape>")):
        text = text[len("<escape>"):]
        wrapped = True
    if wrapped and not partial and text.endswith("</escape>"):
        text = text[:-len("</escape>")]
    return text, wrapped


def continuation_boundary(text):
    """Restart a short last line, so a split word/record can be reconstructed."""
    offset = text.rfind("\n") + 1
    if offset and len(text) - offset <= 2048:
        return text[:offset], text[offset:]
    return text, None


def merge_continuation(text, following, prefix, tail):
    if not following:
        return None
    if following.startswith(text):
        return following if len(following) > len(text) else None
    overlap = 0
    for size in range(min(2048, len(text), len(following)), 63, -1):
        if text.endswith(following[:size]):
            overlap = size
            break
    if overlap:
        candidate = text + following[overlap:]
        return candidate if len(candidate) > len(text) else None
    if tail is not None:
        # A full replay is acceptable only when its already-delivered prefix matches exactly.
        if prefix and following.startswith(prefix):
            following = following[len(prefix):]
        if tail and not following.startswith(tail) and following.lstrip("\r\n").startswith(tail):
            following = following.lstrip("\r\n")
        if not following.startswith(tail):
            return None
        candidate = prefix + following
    else:
        candidate = text + following
    return candidate if candidate.startswith(text) and len(candidate) > len(text) else None


async def complete_with_continuation(data, run_turn, *, max_rounds=4,
                                     max_tokens=65536, timeout=600):
    """run_turn(payload, pinned_account) returns (service, nonstream completion).

    Each service is closed before the next request, including cancellation. A
    continuation failure preserves the assembled partial answer with length.
    """
    original = copy.deepcopy(data)
    original.pop("auto_continue", None)
    original.pop("stream_options", None)
    original.pop("max_completion_tokens", None)
    original["messages"] = [{"role": "system", "content": (
        "This API transports literal text, not a ChatGPT rich-text renderer. Return requested text, "
        "Markdown, code, or function-protocol JSON directly. Do not wrap ordinary text or data in "
        "renderer-only <text>, <escape>, JSX components, template interpolation, or JavaScript "
        "expressions that generate the answer. Explicit requests to write code are still allowed; "
        "write the requested code normally. For a continuation, output the literal missing text, "
        "not code to generate it.")}] + original["messages"]
    original["stream"] = False
    limit = min(output_limit(data, max_tokens), max_tokens)
    original["max_tokens"] = limit
    request = copy.deepcopy(original)
    deadline = time.monotonic() + timeout
    pinned = None
    answer = None
    text = ""
    prompt_tokens = 0
    rounds = 0
    truncations = 0
    reason = "completed"
    prefix, tail = "", None
    wrapped = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if answer is None:
                raise HTTPException(status_code=504, detail="Continuation deadline exceeded")
            reason = "deadline"
            break
        try:
            service, result = await asyncio.wait_for(run_turn(request, pinned), remaining)
        except (HTTPException, asyncio.TimeoutError) as error:
            if answer is None:
                observation.capture(error)
                if isinstance(error, asyncio.TimeoutError):
                    raise HTTPException(status_code=504, detail="Continuation deadline exceeded")
                raise
            reason = "upstream_error"
            break
        try:
            upstream_limited = bool(getattr(service, "upstream_output_limited", False))
            account = service.req_token
            choice = result["choices"][0]
            following = choice["message"].get("content") or ""
            following, wrapped = unwrap_escape(following, partial=upstream_limited,
                                               previously_wrapped=wrapped, initial=answer is None)
            prompt_tokens += result.get("usage", {}).get("prompt_tokens", 0)
        finally:
            await service.close_client()
        truncations += int(upstream_limited)
        if result.get("lifecycle"):
            if answer is not None:answer.setdefault("lifecycle_rounds", []).append(result["lifecycle"])
        if answer is None:
            answer = copy.deepcopy(result)
            text = following
            pinned = account
        else:
            if (result.get("model"), result.get("thinking_effort")) != (answer.get("model"), answer.get("thinking_effort")):
                reason = "model_mismatch"
                break
            merged = merge_continuation(text, following, prefix, tail)
            if merged is None:
                reason = "unverified_boundary"
                break
            text = merged
        count = await num_tokens_from_content(text, answer["model"])
        if count >= limit:
            text, _, _ = await split_tokens_from_content(text, limit, answer["model"])
            reason = "total_limit"
            break
        if choice.get("finish_reason") != "length":
            reason = "completed"
            break
        if not upstream_limited:
            reason = "local_limit"
            break
        if original.get("conversation_id"):
            reason = "external_conversation_context"
            break
        if rounds >= max_rounds:
            reason = "round_limit"
            break
        prefix, tail = continuation_boundary(text)
        instruction = (
            "Continue the interrupted assistant response. This is continuation of the SAME output, "
            "not a new task. Do not execute tools, invent tool results, start another tool call, "
            "add explanations, add fences, or repeat completed text. Preserve all whitespace and syntax. "
        )
        if tail is not None:
            instruction += ("The assistant text above contains the complete preceding lines. "
                            "Start by reproducing this unfinished last-line prefix exactly, finish that line, "
                            "then output every remaining part of the original answer: " + json.dumps(tail, ensure_ascii=False))
            delivered = prefix
        else:
            instruction += "Start immediately after the last character already output. Return only the missing suffix."
            delivered = text
        request = copy.deepcopy(original)
        request.pop("conversation_id", None)
        request.pop("parent_message_id", None)
        request["messages"] = copy.deepcopy(original["messages"]) + [
            {"role": "assistant", "content": delivered}, {"role": "user", "content": instruction}]
        # Include restarted tail space; the final assembled limit is checked separately.
        request["max_tokens"] = min(limit, max(1, limit - count +
            (await num_tokens_from_content(tail, answer["model"]) if tail else 0) + 64))
        rounds += 1
    choice = answer["choices"][0]
    choice["message"]["content"] = text
    choice["finish_reason"] = "stop" if reason == "completed" else "length"
    count = await num_tokens_from_content(text, answer["model"])
    answer["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": count,
                       "total_tokens": prompt_tokens + count}
    answer["continuation"] = {"additional_rounds": rounds, "upstream_truncations": truncations,
                              "stop_reason": reason}
    return answer
