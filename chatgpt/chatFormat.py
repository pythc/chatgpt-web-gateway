import asyncio
import json
import random
import re
import string
import time
import uuid

import pybase64
import websockets
from fastapi import HTTPException

from api.files import get_file_content
from api.models import model_system_fingerprint
from api.tokens import split_tokens_from_content, calculate_image_tokens, num_tokens_from_messages
from utils.Logger import logger

moderation_message = "I'm sorry, I cannot provide or engage in any content related to pornography, violence, or any unethical material. If you have any other questions or need assistance, please feel free to let me know. I'll do my best to provide support and assistance."


async def format_not_stream_response(response, prompt_tokens, max_tokens, model):
    from chatgpt.webStream import UpstreamError, protocol_error
    all_text = ""
    thinking_effort = None
    reason = None
    lifecycle = None
    completed = False
    async for frame in response:
        if frame.startswith("data: [DONE]"):
            completed = True
            break
        if not frame.startswith("data: "):
            continue
        try:
            event = json.loads(frame[6:])
        except (ValueError, TypeError):
            protocol_error("invalid_gateway_stream", "Invalid completion frame")
        if event.get("error"):
            raise UpstreamError(status_code=502, detail={"error": event["error"]})
        model = event.get("model", model)
        thinking_effort = event.get("thinking_effort", thinking_effort)
        lifecycle = event.get("lifecycle", lifecycle)
        choices = event.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        text = (choice.get("delta") or {}).get("content")
        if isinstance(text, str):
            all_text += text
        reason = choice.get("finish_reason") or reason
    if not completed or reason is None:
        protocol_error("upstream_incomplete_response", "Completion ended without a final state")
    if not all_text:
        protocol_error("upstream_no_final_response", "Upstream supplied no visible final answer")
    content, count, local_reason = await split_tokens_from_content(all_text, max_tokens, model)
    result = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
              "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
              "logprobs": None, "finish_reason": "length" if reason == "length" or local_reason == "length" else reason}],
              "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": count, "total_tokens": prompt_tokens + count}}
    if thinking_effort is not None:result["thinking_effort"] = thinking_effort
    if lifecycle is not None:result["lifecycle"] = lifecycle
    return result


async def wss_stream_response(websocket, conversation_id):
    while not websocket.closed:
        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=10)
            if message:
                resultObj = json.loads(message)
                sequenceId = resultObj.get("sequenceId", None)
                if not sequenceId:
                    continue
                data = resultObj.get("data", {})
                if conversation_id != data.get("conversation_id", ""):
                    continue
                sequenceId = resultObj.get('sequenceId')
                if sequenceId and sequenceId % 80 == 0:
                    await websocket.send(
                        json.dumps(
                            {"type": "sequenceAck", "sequenceId": sequenceId}
                        )
                    )
                decoded_bytes = pybase64.b64decode(data.get("body", None))
                yield decoded_bytes
            else:
                print("No message received within the specified time.")
        except asyncio.TimeoutError:
            logger.error("Timeout! No message received within the specified time.")
            break
        except websockets.ConnectionClosed as e:
            if e.code == 1000:
                logger.error("WebSocket closed normally with code 1000 (OK)")
                yield b"data: [DONE]\n\n"
            else:
                logger.error(f"WebSocket closed with error code {e.code}")
        except Exception as e:
            logger.error(f"Error: {str(e)}")
            continue


async def head_process_response(response, service=None):
    # Peek one event only and replay it. Status/control frames are never consumed.
    iterator = response.__aiter__()
    try:
        first = await iterator.__anext__()
    except StopAsyncIteration:
        async def empty():
            if False:yield b""
        return empty(), True
    text = first.decode("utf-8") if isinstance(first, bytes) else first
    if service is not None and text.startswith("data: {"):
        try:event = json.loads(text[6:])
        except ValueError:event = {}
        message = event.get("message") or {}
        if message.get("author", {}).get("role") == "assistant" and (message.get("metadata") or {}).get("model_slug"):
            record_upstream_model(service, message)
    async def replay():
        yield first
        async for item in iterator:yield item
    return replay(), True


def record_upstream_model(service, message):
    if message.get("author", {}).get("role") != "assistant":
        return
    metadata = message.get("metadata", {})
    slug = metadata.get("model_slug")
    if slug:
        if service.strict_model and slug != service.req_model:
            raise HTTPException(status_code=502, detail={"error": "upstream_model_mismatch", "requested_model": service.req_model, "upstream_model": slug})
        service.upstream_model = slug
    if metadata.get("thinking_effort"):
        if service.thinking_effort and metadata["thinking_effort"] != service.thinking_effort:
            raise HTTPException(status_code=502, detail={"error": "upstream_thinking_effort_mismatch"})
        service.upstream_thinking_effort = metadata["thinking_effort"]


async def stream_response(service, response, model, max_tokens):
    from chatgpt.webStream import ConversationState, events, protocol_error
    from chatgpt import errorObservation as observation
    state = ConversationState(service)
    service.lifecycle_state = state
    try:
        async for event in events(response):
            state.feed(event)
            if event == "[DONE]":break
        state.transport_observation["initial_sse_exited_ms"] = state.observation_ms()
        bootstrap = state.checkpoint()
        if not state.candidate() and state.topic_id and hasattr(service, "resume_web_handoff"):
            observation.stage("websocket_recovery")
            await service.resume_web_handoff(state)
        if not state.candidate() and bootstrap.resume_token and hasattr(service, "resume_web_sse"):
            # A WS catchup replays from offset zero. Its duplicated events must
            # never advance the bootstrap SSE cursor or duplicate append deltas.
            for key, value in state.__dict__.items():
                if key.startswith("websocket_"):
                    setattr(bootstrap, key, value.copy() if isinstance(value, dict) else value)
            bootstrap.websocket_replayed_events = max(state.websocket_replayed_events, state.events - bootstrap.events, 0)
            state = bootstrap
            service.lifecycle_state = state
            observation.stage("sse_recovery")
            await service.resume_web_sse(state)
        if not state.candidate() and hasattr(service, "recover_web_conversation"):
            observation.stage("snapshot_recovery")
            await service.recover_web_conversation(state)
        observation.stage("final_response_validation")
        candidate = state.candidate()
        if not candidate:
            protocol_error("upstream_no_final_response", "Upstream stream ended without a visible final answer")
        message, reason = candidate
        if service.strict_model and not service.upstream_model:
            protocol_error("upstream_model_not_identified", "Upstream did not identify the selected model")
        from chatgpt.webStream import text_parts
        text = text_parts(message["content"])
        try:text.encode("utf-8")
        except UnicodeEncodeError:
            protocol_error("invalid_upstream_unicode", "Final answer contains an incomplete Unicode character")
        content, _, local_reason = await split_tokens_from_content(text, max_tokens, service.upstream_model or model)
        if local_reason == "length":reason = "length"
        base = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": service.upstream_model or model, "lifecycle": state.summary()}
        if service.upstream_thinking_effort is not None:base["thinking_effort"] = service.upstream_thinking_effort
        def frame(delta, finish=None):
            return "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}]}, ensure_ascii=False) + "\n\n"
        yield frame({"role": "assistant", "content": ""})
        for offset in range(0, len(content), 512):yield frame({"content": content[offset:offset+512]})
        yield frame({}, reason)
        yield "data: [DONE]\n\n"
    except HTTPException as error:
        observation.capture(error)
        detail = error.detail
        if isinstance(detail, dict) and isinstance(detail.get("error"), dict):detail = detail["error"]
        else:detail = {"type": "upstream_protocol_error", "code": "upstream_response_error", "message": "Upstream response could not be completed"}
        detail = dict(detail)
        detail["lifecycle"] = state.summary()
        yield "data: " + json.dumps({"error": detail}) + "\n\n"


def get_url_from_content(content):
    if isinstance(content, str) and content.startswith('http'):
        try:
            url = re.match(
                r'(?i)\b((?:[a-z][\w-]+:(?:/{1,3}|[a-z0-9%])|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:\'".,<>?«»“”‘’]))',
                content.split(' ')[0])[0]
            content = content.replace(url, '').strip()
            return url, content
        except Exception:
            return None, content
    return None, content


def format_messages_with_url(content):
    url_list = []
    while True:
        url, content = get_url_from_content(content)
        if url:
            url_list.append(url)
            logger.info(f"Found a file_url from messages: {url}")
        else:
            break
    if not url_list:
        return content
    new_content = [
        {
            "type": "text",
            "text": content
        }
    ]
    for url in url_list:
        new_content.append({
            "type": "image_url",
            "image_url": {
                "url": url
            }
        })
    return new_content


async def api_messages_to_chat(service, api_messages, upload_by_url=False):
    file_tokens = 0
    chat_messages = []
    for api_message in api_messages:
        role = api_message.get('role')
        content = api_message.get('content')
        if upload_by_url:
            if isinstance(content, str):
                content = format_messages_with_url(content)
        if isinstance(content, list):
            parts = []
            attachments = []
            content_type = "multimodal_text"
            for i in content:
                if i.get("type") == "text":
                    parts.append(i.get("text"))
                elif i.get("type") == "image_url":
                    image_url = i.get("image_url")
                    url = image_url.get("url")
                    detail = image_url.get("detail", "auto")
                    file_content, mime_type = await get_file_content(url)
                    file_meta = await service.upload_file(file_content, mime_type)
                    if file_meta:
                        file_id = file_meta["file_id"]
                        file_size = file_meta["size_bytes"]
                        file_name = file_meta["file_name"]
                        mime_type = file_meta["mime_type"]
                        use_case = file_meta["use_case"]
                        if mime_type.startswith("image/"):
                            width, height = file_meta["width"], file_meta["height"]
                            file_tokens += await calculate_image_tokens(width, height, detail)
                            parts.append({
                                "content_type": "image_asset_pointer",
                                "asset_pointer": f"file-service://{file_id}",
                                "size_bytes": file_size,
                                "width": width,
                                "height": height
                            })
                            attachments.append({
                                "id": file_id,
                                "size": file_size,
                                "name": file_name,
                                "mime_type": mime_type,
                                "width": width,
                                "height": height
                            })
                        else:
                            if not use_case == "ace_upload":
                                await service.check_upload(file_id)
                            file_tokens += file_size // 1000
                            attachments.append({
                                "id": file_id,
                                "size": file_size,
                                "name": file_name,
                                "mime_type": mime_type,
                            })
            metadata = {
                "attachments": attachments
            }
        else:
            content_type = "text"
            parts = [content]
            metadata = {}
        chat_message = {
            "id": f"{uuid.uuid4()}",
            "author": {"role": role},
            "content": {"content_type": content_type, "parts": parts},
            "metadata": metadata
        }
        chat_messages.append(chat_message)
    text_tokens = await num_tokens_from_messages(api_messages, service.resp_model)
    prompt_tokens = text_tokens + file_tokens
    return chat_messages, prompt_tokens
