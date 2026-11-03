import asyncio
import json
import os
import time
import uuid
import types

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request, HTTPException, Form, Security
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from starlette.background import BackgroundTask

import utils.globals as globals
from app import app, templates, security_scheme
from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens, get_req_token
from api.model_catalog import get_model_catalog
from api.client_tools import ClientToolBridge, ClientToolError, needs_tool_bridge, completion_chunks, fail
from api.continuation import complete_with_continuation
from chatgpt.webStream import UpstreamError, LifecycleBudget, protocol_error
from utils.Logger import logger
from chatgpt import errorObservation as observation
from chatgpt.requestScope import GatewayRequestScope
from utils.configs import (api_prefix, scheduled_refresh, history_disabled, default_model, default_reasoning_effort,
                          auto_continue, auto_continue_max_rounds, auto_continue_max_tokens, auto_continue_timeout, recovery_max_reads)
from utils.retry import async_retry

scheduler = AsyncIOScheduler()


def append_audit(record):
    # Explicit allowlisted metadata only. No request/response text, headers or tokens.
    try:
        fd = os.open("data/lifecycle-audit.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


@app.middleware("http")
async def gateway_observation(request: Request, call_next):
    # Full ASGI scope owns live requests; retain this callable for embedders.
    if getattr(request.state, "gateway_scope_observed", False):
        try:return await call_next(request)
        except BaseException as error:
            observation.capture(error)
            raise
    request.state.request_id = str(uuid.uuid4())
    value = request.headers.get("X-Client-Request-Id")
    try:client_id = str(uuid.UUID(value)) if value else None
    except (ValueError, TypeError, AttributeError):client_id = None
    started = time.monotonic()
    scoped = request.url.path.endswith(("/v1/chat/completions", "/v1/responses", "/v1/responses/compact"))
    observer = observation.RequestObservation() if scoped else None
    token = observation._current.set(observer) if scoped else None
    try:
        try:
            response = await call_next(request)
        except BaseException as error:
            if observer is not None:
                observer.capture(error)
                status = 499 if isinstance(error, asyncio.CancelledError) else 500
                append_audit({"request_id": request.state.request_id, "client_request_id": client_id,
                    "status": status, "elapsed_seconds": round(time.monotonic()-started, 3),
                    "trace": observer.trace(getattr(request.state, "gateway_trace", None), status)})
            raise
        response.headers["X-Request-Id"] = request.state.request_id
        if client_id:response.headers["X-Client-Request-Id"] = client_id
        if observer is not None:
            append_audit({"request_id": request.state.request_id, "client_request_id": client_id,
                "status": response.status_code, "elapsed_seconds": round(time.monotonic()-started, 3),
                "trace": observer.trace(getattr(request.state, "gateway_trace", None), response.status_code)})
        return response
    finally:
        if token is not None:observation._current.reset(token)



def append_activity(record):
    try:
        fd = os.open("data/lifecycle-activity.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:pass


app.add_middleware(GatewayRequestScope,
    audit=lambda record: append_audit(record), activity=lambda record: append_activity(record),
    budget_factory=lambda: LifecycleBudget(auto_continue_timeout, auto_continue_max_rounds+1, recovery_max_reads))


@app.exception_handler(UpstreamError)
@app.exception_handler(ClientToolError)
async def client_tool_error(request: Request, error: ClientToolError):
    detail = error.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        detail = {"error": dict(detail["error"])}
        detail["error"]["request_id"] = getattr(request.state, "request_id", None)
        budget = getattr(request.state, "lifecycle_budget", None)
        if budget:detail["error"]["gateway_budget"] = budget.summary()
        request.state.gateway_trace = {"error_code": detail["error"].get("code"), "lifecycle": detail["error"].get("lifecycle")}
    return JSONResponse(status_code=error.status_code, content=detail)


@app.on_event("startup")
async def app_start():
    if scheduled_refresh:
        scheduler.add_job(id='refresh', func=refresh_all_tokens, trigger='cron', hour=3, minute=0, day='*/2',
                          kwargs={'force_refresh': True})
        scheduler.start()
        asyncio.get_event_loop().call_later(0, lambda: asyncio.create_task(refresh_all_tokens(force_refresh=False)))


async def to_send_conversation(request_data, req_token, pinned_account=None, lifecycle_budget=None):
    observation.stage("service_init")
    chat_service = ChatService(req_token)
    observation.attach_service(chat_service)
    if pinned_account is not None:
        chat_service.req_token = pinned_account
    if lifecycle_budget is not None:
        chat_service.lifecycle_budget = lifecycle_budget
    try:
        observation.stage("account_setup")
        await chat_service.set_dynamic_data(request_data)
        observation.stage("chat_requirements")
        await chat_service.get_chat_requirements()
        return chat_service
    except HTTPException as e:
        observation.capture(e)
        await chat_service.close_client()
        raise
    except Exception as e:
        observation.capture(e)
        await chat_service.close_client()
        logger.error("Conversation setup failed; safe diagnostics retained")
        raise HTTPException(status_code=500, detail="Server error")
    except BaseException as error:
        observation.capture(error)
        await chat_service.close_client()
        raise


async def process(request_data, req_token, pinned_account=None, lifecycle_budget=None):
    chat_service = await to_send_conversation(request_data, req_token, pinned_account, lifecycle_budget)
    try:
        observation.stage("request_preparation")
        await chat_service.prepare_send_conversation()
        res = await chat_service.send_conversation()
        return chat_service, res
    except BaseException as error:
        observation.capture(error)
        await chat_service.close_client()
        raise


@app.get(f"/{api_prefix}/v1/models" if api_prefix else "/v1/models")
async def list_models(credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    models = await get_model_catalog(get_req_token(credentials.credentials), history_disabled)
    return {"object": "list", "data": [
        {"id": m["slug"], "object": "model", "created": 0, "owned_by": "chatgpt-web",
         "name": m.get("title", m["slug"]),
         "thinking_efforts": [e["thinking_effort"] for e in m.get("thinking_efforts", [])]}
        for m in models
    ] + [{"id": "auto", "object": "model", "created": 0, "owned_by": "chatgpt-web"}],
        "default_model": default_model, "default_reasoning_effort": default_reasoning_effort}


@app.post(f"/{api_prefix}/v1/chat/completions" if api_prefix else "/v1/chat/completions")
async def send_conversation(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    req_token = credentials.credentials
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})
    tool_bridge = ClientToolBridge(request_data) if needs_tool_bridge(request_data) else None
    response_format = request_data.get("response_format")
    if response_format is not None and response_format not in ({"type": "json_object"}, {"type": "text"}):
        fail(400, "unsupported_response_format", "Supported formats are text and json_object")
    enabled = request_data.get("auto_continue", auto_continue)
    requested_stream = request_data.get("stream", False)
    if not isinstance(enabled, bool) or not isinstance(requested_stream, bool):
        fail(400, "invalid_continuation_controls", "auto_continue and stream must be booleans")
    options = request_data.get("stream_options")
    if options is not None and (not isinstance(options, dict) or
            ("include_usage" in options and not isinstance(options["include_usage"], bool))):
        fail(400, "invalid_stream_options", "stream_options must contain boolean include_usage")
    if tool_bridge:
        if enabled:
            tool_bridge.limit = min(tool_bridge.limit, auto_continue_max_tokens)
        request_data = tool_bridge.upstream_request()
    chat_service = None
    lifecycle_budget = getattr(request.state, "lifecycle_budget", None)
    if lifecycle_budget is None:
        lifecycle_budget = LifecycleBudget(auto_continue_timeout, auto_continue_max_rounds+1, recovery_max_reads)
    request.state.lifecycle_budget = lifecycle_budget
    observation.attach_budget(lifecycle_budget)
    try:
        if enabled:
            async def run_turn(payload, pinned_account):
                return await async_retry(process, payload, req_token, pinned_account=pinned_account, lifecycle_budget=lifecycle_budget, max_retries=0)
            budget = auto_continue_max_tokens + (1024 if tool_bridge and tool_bridge.choice != "none" else 0)
            res = await complete_with_continuation(request_data, run_turn,
                max_rounds=auto_continue_max_rounds, max_tokens=budget, timeout=auto_continue_timeout)
        else:
            request_data.pop("auto_continue", None)
            chat_service, res = await async_retry(process, request_data, req_token, lifecycle_budget=lifecycle_budget, max_retries=0)
        observation.stage("response_validation")
        if not tool_bridge and isinstance(res, dict) and request_data.get("response_format") == {"type": "json_object"} and res["choices"][0]["finish_reason"] == "stop":
            from api.client_tools import envelope_text, strict_json
            content = envelope_text(res["choices"][0]["message"]["content"])
            try:
                parsed = strict_json(content)
                if not isinstance(parsed, dict):raise ValueError()
            except (ValueError, TypeError):
                protocol_error("invalid_json_response", "Final answer is not a valid JSON object")
            res["choices"][0]["message"]["content"] = content
        if isinstance(res, dict):
            res["gateway_budget"] = lifecycle_budget.summary()
            request.state.gateway_trace = {"model": res.get("model"), "thinking_effort": res.get("thinking_effort"),
                "continuation": res.get("continuation"), "lifecycle": res.get("lifecycle"), "budget": res["gateway_budget"]}
        if tool_bridge:
            observation.stage("tool_response_format")
            res = await tool_bridge.completion(res)
            if tool_bridge.stream:
                res = tool_bridge.chunks(res)
        elif enabled and requested_stream:
            res = completion_chunks(res, bool(options and options.get("include_usage")))
        observation.stage("response_encoding")
        if isinstance(res, types.AsyncGeneratorType):
            background = BackgroundTask(chat_service.close_client) if chat_service else None
            return StreamingResponse(res, media_type="text/event-stream", background=background)
        else:
            background = BackgroundTask(chat_service.close_client) if chat_service else None
            return JSONResponse(res, media_type="application/json", background=background)
    except ClientToolError as error:
        observation.capture(error)
        if chat_service:
            await chat_service.close_client()
        raise
    except HTTPException as e:
        observation.capture(e)
        if chat_service:
            await chat_service.close_client()
        if e.status_code == 500:
            logger.error("Conversation request failed; safe diagnostics retained")
            raise HTTPException(status_code=500, detail="Server error")
        raise
    except Exception as e:
        observation.capture(e)
        if chat_service:
            await chat_service.close_client()
        logger.error("Conversation request failed; safe diagnostics retained")
        raise HTTPException(status_code=500, detail="Server error")


@app.get(f"/{api_prefix}/tokens" if api_prefix else "/tokens", response_class=HTMLResponse)
async def upload_html(request: Request):
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return templates.TemplateResponse("tokens.html",
                                      {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(text: str = Form(...)):
    lines = text.split("\n")
    for line in lines:
        if line.strip() and not line.startswith("#"):
            globals.token_list.append(line.strip())
            with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
                f.write(line.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def clear_tokens():
    globals.token_list.clear()
    globals.error_token_list.clear()
    with open(globals.TOKENS_FILE, "w", encoding="utf-8") as f:
        pass
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens():
    error_tokens_list = list(set(globals.error_token_list))
    return {"status": "success", "error_tokens": error_tokens_list}


@app.get(f"/{api_prefix}/tokens/add/{{token}}" if api_prefix else "/tokens/add/{token}")
async def add_token(token: str):
    if token.strip() and not token.startswith("#"):
        globals.token_list.append(token.strip())
        with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
            f.write(token.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/seed_tokens/clear" if api_prefix else "/seed_tokens/clear")
async def clear_seed_tokens():
    globals.seed_map.clear()
    globals.conversation_map.clear()
    with open(globals.SEED_MAP_FILE, "w", encoding="utf-8") as f:
        f.write("{}")
    with open(globals.CONVERSATION_MAP_FILE, "w", encoding="utf-8") as f:
        f.write("{}")
    logger.info(f"Seed token count: {len(globals.seed_map)}")
    return {"status": "success", "seed_tokens_count": len(globals.seed_map)}
