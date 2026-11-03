import asyncio
import copy
import hashlib
import json
import random
import uuid
import time
from urllib.parse import quote, urlsplit

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from api.files import get_image_size, get_file_extension, determine_file_use_case
from api.models import model_proxy
from api.model_catalog import get_model_catalog, select_thinking_effort
from chatgpt.authorization import get_req_token, verify_token
from chatgpt.chatFormat import api_messages_to_chat, stream_response, format_not_stream_response, head_process_response
from chatgpt.chatLimit import check_is_limit, handle_request_limit
from chatgpt.fp import get_fp
from chatgpt.proofofWork import get_config, get_dpl, get_answer_token, get_requirements_token

from utils.Client import Client
from utils.Logger import logger
from utils.configs import (
    chatgpt_base_url_list,
    ark0se_token_url_list,
    sentinel_proxy_url_list,
    history_disabled,
    pow_difficulty,
    conversation_only,
    enable_limit,
    upload_by_url,
    auth_key,
    turnstile_solver_url,
    oai_language,
    default_model,
    default_reasoning_effort,
    conversation_timeout,
    auto_continue_timeout,
    auto_continue_max_rounds,
    recovery_max_reads,
    recovery_max_seconds,
    websocket_idle_timeout,
    websocket_handshake_max_attempts,
    websocket_receive_max_reconnects,
)


from chatgpt import errorObservation as error_observation


class ChatService:
    def __init__(self, origin_token=None):
        # self.user_agent = random.choice(user_agents_list) if user_agents_list else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        self.req_token = get_req_token(origin_token)
        self.chat_token = "gAAAAAB"
        self.s = None
        self.ss = None
        self.ws = None
        self.upstream_model = None
        self.upstream_thinking_effort = None
        self.upstream_output_limited = False
        from chatgpt.webStream import LifecycleBudget
        self.lifecycle_budget = LifecycleBudget(auto_continue_timeout, auto_continue_max_rounds + 1, recovery_max_reads)
        self.input_message_id = None
        self.lifecycle_state = None
        self.web_protocol = "buffered-v1"

    async def set_dynamic_data(self, data):
        error_observation.stage("account_verification")
        if self.req_token:
            req_len = len(self.req_token.split(","))
            if req_len == 1:
                self.access_token = await verify_token(self.req_token)
                self.account_id = None
            else:
                self.access_token = await verify_token(self.req_token.split(",")[0])
                self.account_id = self.req_token.split(",")[1]
        else:
            logger.info("Request token is empty, use no-auth 3.5")
            self.access_token = None
            self.account_id = None

        error_observation.stage("account_setup")
        self.fp = get_fp(self.req_token).copy()
        self.proxy_url = self.fp.pop("proxy_url", None)
        self.impersonate = self.fp.pop("impersonate", "safari15_3")
        self.user_agent = self.fp.get("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
        logger.info(f"Request credentials configured: {bool(self.req_token)}")
        logger.info(f"Request proxy: {self.proxy_url}")
        logger.info(f"Request UA: {self.user_agent}")
        logger.info(f"Request impersonate: {self.impersonate}")

        self.data = data
        error_observation.stage("model_selection")
        await self.set_model()
        error_observation.stage("account_setup")
        if enable_limit and self.req_token:
            limit_response = await handle_request_limit(self.req_token, self.req_model)
            if limit_response:
                raise HTTPException(status_code=429, detail=limit_response)

        self.account_id = self.data.get('Chatgpt-Account-Id', self.account_id)
        self.parent_message_id = self.data.get('parent_message_id')
        self.conversation_id = self.data.get('conversation_id')
        self.history_disabled = self.data.get('history_disabled', history_disabled)

        self.api_messages = self.data.get("messages", [])
        self.prompt_tokens = 0
        self.max_tokens = self.data.get("max_tokens", 2147483647)
        if not isinstance(self.max_tokens, int):
            self.max_tokens = 2147483647

        # self.proxy_url = random.choice(proxy_url_list) if proxy_url_list else None

        self.host_url = random.choice(chatgpt_base_url_list) if chatgpt_base_url_list else "https://chatgpt.com"
        self.ark0se_token_url = random.choice(ark0se_token_url_list) if ark0se_token_url_list else None

        session_id = hashlib.md5(self.req_token.encode()).hexdigest()
        proxy_url = self.proxy_url.replace("{}", session_id) if self.proxy_url else None
        self.s = Client(proxy=proxy_url, impersonate=self.impersonate)
        if sentinel_proxy_url_list:
            sentinel_proxy_url = (random.choice(sentinel_proxy_url_list)).replace("{}", session_id) if sentinel_proxy_url_list else None
            self.ss = Client(proxy=sentinel_proxy_url, impersonate=self.impersonate)
        else:
            self.ss = self.s

        self.persona = None
        self.ark0se_token = None
        self.proof_token = None
        self.turnstile_token = None

        self.chat_headers = None
        self.chat_request = None

        self.base_headers = {
            'accept': '*/*',
            'accept-encoding': 'gzip, deflate, br, zstd',
            'accept-language': 'en-US,en;q=0.9',
            'content-type': 'application/json',
            'oai-language': oai_language,
            'origin': self.host_url,
            'priority': 'u=1, i',
            'referer': f'{self.host_url}/',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin'
        }
        self.base_headers.update(self.fp)

        if self.access_token:
            self.base_url = self.host_url + "/backend-api"
            self.base_headers['authorization'] = f'Bearer {self.access_token}'
            if self.account_id:
                self.base_headers['chatgpt-account-id'] = self.account_id
        else:
            self.base_url = self.host_url + "/backend-anon"

        if auth_key:
            self.base_headers['authkey'] = auth_key

        error_observation.stage("web_bootstrap")
        await get_dpl(self)

    async def set_model(self):
        self.origin_model = self.data.get("model", default_model)
        if not isinstance(self.origin_model, str) or not self.origin_model:
            raise HTTPException(status_code=400, detail="model must be a nonempty string")
        self.thinking_effort = None
        self.strict_model = False
        if self.origin_model.startswith(("gpt-5", "gpt-6")):
            catalog = await get_model_catalog(self.req_token, self.data.get("history_disabled", history_disabled))
            error_observation.stage("model_selection")
            selected = next((m for m in catalog if m["slug"] == self.origin_model), None)
            if selected is None:
                raise HTTPException(status_code=404, detail={"code": "model_not_found", "message": "Model is not listed for this account"})
            self.gizmo_id = None
            self.req_model = self.origin_model
            self.resp_model = self.origin_model
            self.strict_model = True
            effort = self.data.get("reasoning_effort")
            web_effort = self.data.get("thinking_effort")
            if effort is None and web_effort is None and self.origin_model == default_model:
                effort = default_reasoning_effort
            self.thinking_effort = select_thinking_effort(selected, effort, web_effort)
            return
        if "reasoning_effort" in self.data or "thinking_effort" in self.data:
            raise HTTPException(status_code=400, detail="Select an explicit current model to set thinking effort")
        legacy_models = set(model_proxy) | set(model_proxy.values()) | {
            "auto", "o3", "o3-mini", "o3-mini-high", "o3-mini-medium", "o3-mini-low",
            "o1-pro", "gpt-4.5o", "gpt-4o-canmore", "gpt-4-mobile"}
        if self.origin_model not in legacy_models and not self.origin_model.startswith("gpt-4-gizmo-g-"):
            raise HTTPException(status_code=404, detail={"code": "model_not_found", "message": "Unknown model; no fallback was used"})
        self.resp_model = model_proxy.get(self.origin_model, self.origin_model)
        if "gizmo" in self.origin_model or "g-" in self.origin_model:
            self.gizmo_id = "g-" + self.origin_model.split("g-")[-1]
        else:
            self.gizmo_id = None

        if "o3-mini-high" in self.origin_model:
            self.req_model = "o3-mini-high"
        elif "o3-mini-medium" in self.origin_model:
            self.req_model = "o3-mini-medium"
        elif "o3-mini-low" in self.origin_model:
            self.req_model = "o3-mini-low"
        elif "o3-mini" in self.origin_model:
            self.req_model = "o3-mini"
        elif "o3" in self.origin_model:
            self.req_model = "o3"
        elif "o1-preview" in self.origin_model:
            self.req_model = "o1-preview"
        elif "o1-pro" in self.origin_model:
            self.req_model = "o1-pro"
        elif "o1-mini" in self.origin_model:
            self.req_model = "o1-mini"
        elif "o1" in self.origin_model:
            self.req_model = "o1"
        elif "gpt-4.5o" in self.origin_model:
            self.req_model = "gpt-4.5o"
        elif "gpt-4o-canmore" in self.origin_model:
            self.req_model = "gpt-4o-canmore"
        elif "gpt-4o-mini" in self.origin_model:
            self.req_model = "gpt-4o-mini"
        elif "gpt-4o" in self.origin_model:
            self.req_model = "gpt-4o"
        elif "gpt-4-mobile" in self.origin_model:
            self.req_model = "gpt-4-mobile"
        elif "gpt-4" in self.origin_model:
            self.req_model = "gpt-4"
        elif "gpt-3.5" in self.origin_model:
            self.req_model = "text-davinci-002-render-sha"
        elif "auto" in self.origin_model:
            self.req_model = "auto"
        else:
            raise HTTPException(status_code=404, detail={"code": "model_not_found", "message": "Unknown model; no fallback was used"})

    async def get_chat_requirements(self):
        error_observation.stage("chat_requirements")
        if conversation_only:
            return None
        url = f'{self.base_url}/sentinel/chat-requirements'
        headers = self.base_headers.copy()
        try:
            config = get_config(self.user_agent, self.req_token)
            p = get_requirements_token(config)
            data = {'p': p}
            r = await self.ss.post(url, headers=headers, json=data, timeout=5)
            error_observation.upstream_status(r.status_code)
            if r.status_code == 200:
                resp = r.json()

                self.persona = resp.get("persona")
                if self.persona != "chatgpt-paid":
                    if self.req_model == "gpt-4" or self.req_model == "o1-preview":
                        logger.error(f"Model {self.resp_model} not support for {self.persona}")
                        raise HTTPException(
                            status_code=404,
                            detail={
                                "message": f"The model `{self.origin_model}` does not exist or you do not have access to it.",
                                "type": "invalid_request_error",
                                "param": None,
                                "code": "model_not_found",
                            },
                        )

                turnstile = resp.get('turnstile', {})
                turnstile_required = turnstile.get('required')
                if turnstile_required:
                    turnstile_dx = turnstile.get("dx")
                    try:
                        if turnstile_solver_url:
                            error_observation.stage("turnstile")
                            res = await self.s.post(
                                turnstile_solver_url, json={"url": "https://chatgpt.com", "p": p, "dx": turnstile_dx, "ua": self.user_agent}
                            )
                            self.turnstile_token = res.json().get("t")
                    except Exception as e:
                        logger.info("Turnstile could not be resolved")
                    # raise HTTPException(status_code=403, detail="Turnstile required")
                    error_observation.stage("chat_requirements")
                    error_observation.upstream_status(r.status_code)

                ark0se = resp.get('ark' + 'ose', {})
                ark0se_required = ark0se.get('required')
                if ark0se_required:
                    if self.persona == "chatgpt-freeaccount":
                        ark0se_method = "chat35"
                    else:
                        ark0se_method = "chat4"
                    if not self.ark0se_token_url:
                        raise HTTPException(status_code=403, detail="Ark0se service required")
                    ark0se_dx = ark0se.get("dx")
                    error_observation.stage("challenge")
                    ark0se_client = Client(impersonate=self.impersonate)
                    try:
                        r2 = await ark0se_client.post(
                            url=self.ark0se_token_url, json={"blob": ark0se_dx, "method": ark0se_method}, timeout=15
                        )
                        r2esp = r2.json()
                        logger.info("Challenge response received")
                        if r2esp.get('solved', True):
                            self.ark0se_token = r2esp.get('token')
                        else:
                            raise HTTPException(status_code=403, detail="Failed to get Ark0se token")
                    except Exception as error:
                        error_observation.capture(error)
                        raise HTTPException(status_code=403, detail="Failed to get Ark0se token")
                    finally:
                        await ark0se_client.close()
                    error_observation.stage("chat_requirements")
                    error_observation.upstream_status(r.status_code)

                proofofwork = resp.get('proofofwork', {})
                proofofwork_required = proofofwork.get('required')
                if proofofwork_required:
                    proofofwork_diff = proofofwork.get("difficulty")
                    if proofofwork_diff <= pow_difficulty:
                        raise HTTPException(status_code=403, detail=f"Proof of work difficulty too high: {proofofwork_diff}")
                    proofofwork_seed = proofofwork.get("seed")
                    error_observation.stage("proof_of_work")
                    self.proof_token, solved = await run_in_threadpool(
                        get_answer_token, proofofwork_seed, proofofwork_diff, config
                    )
                    if not solved:
                        raise HTTPException(status_code=403, detail="Failed to solve proof of work")

                error_observation.stage("chat_requirements")
                error_observation.upstream_status(r.status_code)
                self.chat_token = resp.get('token')
                if not self.chat_token:
                    raise HTTPException(status_code=403, detail=f"Failed to get chat token: {r.text}")
                return self.chat_token
            else:
                if "application/json" == r.headers.get("Content-Type", ""):
                    detail = r.json().get("detail", r.json())
                else:
                    detail = r.text
                if "cf_chl_opt" in detail:
                    raise HTTPException(status_code=r.status_code, detail="cf_chl_opt")
                if r.status_code == 429:
                    raise HTTPException(status_code=r.status_code, detail="rate-limit")
                raise HTTPException(status_code=r.status_code, detail=detail)
        except HTTPException as e:
            error_observation.capture(e)
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        except Exception as e:
            error_observation.capture(e)
            raise HTTPException(status_code=500, detail="Server error")

    async def prepare_send_conversation(self):
        try:
            chat_messages, self.prompt_tokens = await api_messages_to_chat(self, self.api_messages, upload_by_url)
        except Exception as e:
            error_observation.capture(e)
            logger.error("Failed to format messages")
            raise HTTPException(status_code=400, detail="Failed to format messages.")
        self.input_message_id = next((m["id"] for m in reversed(chat_messages) if m.get("author", {}).get("role") == "user"), None)
        self.chat_headers = self.base_headers.copy()
        self.chat_headers.update(
            {
                'accept': 'text/event-stream',
                'openai-sentinel-chat-requirements-token': self.chat_token,
                'openai-sentinel-proof-token': self.proof_token,
            }
        )
        if self.ark0se_token:
            self.chat_headers['openai-sentinel-ark' + 'ose-token'] = self.ark0se_token

        if self.turnstile_token:
            self.chat_headers['openai-sentinel-turnstile-token'] = self.turnstile_token

        if conversation_only:
            self.chat_headers.pop('openai-sentinel-chat-requirements-token', None)
            self.chat_headers.pop('openai-sentinel-proof-token', None)
            self.chat_headers.pop('openai-sentinel-ark' + 'ose-token', None)
            self.chat_headers.pop('openai-sentinel-turnstile-token', None)

        if self.gizmo_id:
            conversation_mode = {"kind": "gizmo_interaction", "gizmo_id": self.gizmo_id}
            logger.info(f"Gizmo id: {self.gizmo_id}")
        else:
            conversation_mode = {"kind": "primary_assistant"}

        logger.info(f"Model mapping: {self.origin_model} -> {self.req_model}")
        self.chat_request = {
            "action": "next",
            "client_contextual_info": {
                "is_dark_mode": False,
                "time_since_loaded": random.randint(50, 500),
                "page_height": random.randint(500, 1000),
                "page_width": random.randint(1000, 2000),
                "pixel_ratio": 1.5,
                "screen_height": random.randint(800, 1200),
                "screen_width": random.randint(1200, 2200),
            },
            "conversation_mode": conversation_mode,
            "conversation_origin": None,
            "force_paragen": False,
            "force_paragen_model_slug": "",
            "force_rate_limit": False,
            "history_and_training_disabled": self.history_disabled,
            "messages": chat_messages,
            "model": self.req_model,
            "paragen_cot_summary_display_override": "allow",
            "paragen_stream_type_override": None,
            "parent_message_id": self.parent_message_id if self.parent_message_id else f"{uuid.uuid4()}",
            "reset_rate_limits": False,
            "suggestions": [],
            "supported_encodings": ["v1"],
            "supports_buffering": True,
            "enable_message_followups": True,
            "force_parallel_switch": "auto",
            "system_hints": [],
            "timezone": "America/Los_Angeles",
            "timezone_offset_min": -480,
            "variant_purpose": "comparison_implicit",
            "websocket_request_id": f"{uuid.uuid4()}",
        }
        if self.conversation_id:
            self.chat_request['conversation_id'] = self.conversation_id
        if self.thinking_effort is not None:
            self.chat_request['thinking_effort'] = self.thinking_effort
        self.chat_request["client_contextual_info"]["app_name"] = "chatgpt.com"
        return self.chat_request

    async def prepare_buffered_conversation(self):
        from chatgpt.webStream import UpstreamError, protocol_error
        # The prepare request binds routing to the same model, effort and parent.
        fields = ("action", "parent_message_id", "conversation_id", "model", "thinking_effort",
                  "timezone", "timezone_offset_min", "conversation_mode", "system_hints",
                  "history_and_training_disabled", "supports_buffering", "supported_encodings")
        body = {k: self.chat_request[k] for k in fields if k in self.chat_request}
        body["client_contextual_info"] = {"app_name": "chatgpt.com"}
        if self.lifecycle_budget.remaining() <= 0:
            protocol_error("upstream_budget_exhausted", "Conversation deadline exhausted before preparation")
        headers = {**self.chat_headers, "accept": "application/json"}
        error_observation.stage("conversation_preparation")
        response = await self.s.post(self.base_url + "/f/conversation/prepare", headers=headers,
                                     json=body, timeout=min(30, self.lifecycle_budget.remaining()))
        error_observation.upstream_status(response.status_code)
        if response.status_code != 200:
            raise UpstreamError(response.status_code, {"error": {"type": "upstream_http_error",
                "code": "upstream_prepare_http_" + str(response.status_code),
                "message": "ChatGPT rejected conversation preparation", "upstream_status": response.status_code}})
        token = response.json().get("conduit_token")
        if not isinstance(token, str) or not token:
            protocol_error("upstream_prepare_missing_conduit", "Conversation preparation returned no routing token")
        self.chat_headers["x-conduit-token"] = token

    async def send_conversation(self):
        try:
            modern = getattr(self, "web_protocol", None) == "buffered-v1"
            if modern:
                await self.prepare_buffered_conversation()
            url = self.base_url + ("/f/conversation" if modern else "/conversation")
            stream = self.data.get("stream", False)
            error_observation.stage("generation_request")
            self.lifecycle_budget.generation()
            self._diagnostic_sse_started = time.monotonic()
            r = await self.s.post_stream(url, headers=self.chat_headers, json=self.chat_request, timeout=min(conversation_timeout, self.lifecycle_budget.remaining()), stream=True)
            error_observation.upstream_status(r.status_code)
            self._diagnostic_sse_headers = time.monotonic()
            if r.status_code != 200:
                from chatgpt.webStream import UpstreamError
                raise UpstreamError(status_code=r.status_code, detail={"error": {
                    "type": "upstream_http_error", "code": "upstream_http_" + str(r.status_code),
                    "message": "ChatGPT rejected the conversation request", "upstream_status": r.status_code}})

            content_type = r.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                error_observation.stage("initial_sse")
                error_observation.upstream_status(r.status_code)
                res, start = await head_process_response(r.aiter_lines(), self)
                self._diagnostic_sse_head_ready = time.monotonic()
                if not start:
                    raise HTTPException(
                        status_code=403,
                        detail="Our systems have detected unusual activity coming from your system. Please try again later.",
                    )
                if stream:
                    return stream_response(self, res, self.resp_model, self.max_tokens)
                else:
                    return await format_not_stream_response(
                        stream_response(self, res, self.resp_model, self.max_tokens),
                        self.prompt_tokens,
                        self.max_tokens,
                        self.resp_model,
                    )
            else:
                from chatgpt.webStream import protocol_error
                protocol_error("unexpected_upstream_content_type", "ChatGPT did not return a conversation event stream")
        except HTTPException as error:
            error_observation.capture(error)
            raise
        except Exception as error:
            error_observation.capture(error)
            from chatgpt.webStream import protocol_error
            protocol_error("upstream_transport_error", "Upstream conversation transport failed")

    async def open_websocket_recovery(self, state):
        from chatgpt.webStream import protocol_error, safe_transport_error
        # Same-account, read-budgeted handshake; never generate a model turn.
        for attempt in range(websocket_handshake_max_attempts):
            if not self.lifecycle_budget.read("websocket_setup"):
                state.websocket_status = "recovery_budget_exhausted"
                return
            state.websocket_stage = "endpoint"
            error_observation.stage("websocket_endpoint")
            response = await self.s.get(self.base_url + "/celsius/ws/user", headers=self.base_headers,
                                        timeout=min(15, self.lifecycle_budget.remaining()))
            error_observation.upstream_status(response.status_code)
            if response.status_code != 200:
                state.websocket_status = "endpoint_http_" + str(response.status_code)
                return
            url = response.json().get("websocket_url")
            target = urlsplit(url) if isinstance(url, str) else None
            if not target or target.scheme != "wss" or target.hostname != "ws.chatgpt.com" or target.port not in (None, 443) or target.username or target.password:
                protocol_error("invalid_upstream_websocket", "Untrusted upstream websocket address")
            # Only reissue this short-lived WS credential on an observed 401.
            # Each attempt is a bounded recovery read, never a generation.
            state.websocket_stage = "handshake"
            error_observation.stage("websocket_handshake")
            try:
                connector = getattr(self.s, "connect_websocket", None) or self.s.session.ws_connect
                socket = await asyncio.wait_for(connector(url,
                    headers={"origin": self.host_url}, quote=False, timeout=min(30, self.lifecycle_budget.remaining())),
                    timeout=min(30, self.lifecycle_budget.remaining()))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                diagnostic = safe_transport_error(error)
                status = diagnostic.get("http_status")
                key = "http_" + str(status) if status is not None else "transport_error"
                state.websocket_handshake_statuses[key] = state.websocket_handshake_statuses.get(key, 0) + 1
                if status != 401 or attempt + 1 >= websocket_handshake_max_attempts:
                    raise
                state.websocket_status = "retrying_handshake"
                await asyncio.sleep(min(0.3, self.lifecycle_budget.remaining()))
                continue
            state.websocket_handshake_statuses["http_101"] = state.websocket_handshake_statuses.get("http_101", 0) + 1
            return socket

    async def resume_web_handoff(self, state):
        """Recover one offered topic with opaque pubsub cursors, never regenerate."""
        from curl_cffi.const import CurlWsFlag
        from chatgpt.webStream import ConversationState, events, protocol_error, safe_transport_error, sse_lines
        if not state.topic_id:
            return
        topic = state.topic_id
        conversation = state.conversation_id
        target_user = state.target_user_id
        decoder_service = copy.copy(state.service)
        wire = None
        pending = ""
        cursor = None
        cursor_origin = None
        cursor_safe = True
        applied = {}
        acknowledged_batches = {}
        stream_items_seen = {}
        bound_turn = None
        max_pending = 16 * 1024 * 1024
        state.websocket_topic_kind = ("conv-turn-low-ttl" if topic.startswith("conv-turn-low-ttl-")
                                      else "conversation" if topic.startswith("conversation-") else "other")

        def valid_cursor(value):
            return isinstance(value, str) and 0 < len(value) <= 512

        def cursor_hash(value):
            return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest() if valid_cursor(value) else None

        def envelope_fingerprint(inner):
            fields = ("type", "conversation_id", "turn_id", "stream_item_id", "parent_stream_item_id", "encoded_item")
            return hashlib.sha256(json.dumps([inner.get(k) for k in fields], separators=(",", ":")).encode()).digest()

        def flush_complete_tail():
            nonlocal pending
            data_lines = [line[5:].lstrip(" ") for line in sse_lines(pending) if line.startswith("data:")]
            if not data_lines:
                return None
            value = "\n".join(data_lines)
            try:
                if value != "[DONE]":json.loads(value)
            except (ValueError, TypeError):
                return None
            block, pending = pending, ""
            return block

        def apply_item(item):
            nonlocal pending, cursor, cursor_safe, conversation, bound_turn
            if not isinstance(item, dict) or item.get("topic_id") != topic:
                return None
            payload = item.get("payload") or {}
            if not isinstance(payload, dict) or payload.get("type") != "conversation-turn-stream":
                return None
            inner = payload.get("payload") or {}
            if not isinstance(inner, dict):return "stream_envelope_unsupported"
            kind = inner.get("type")
            if kind not in ("stream-item", "heartbeat", "done"):
                return "stream_envelope_unsupported"
            cid, turn = inner.get("conversation_id"), inner.get("turn_id")
            if cid is not None:
                if not isinstance(cid, str) or (conversation is not None and cid != conversation):
                    state.websocket_stream_binding_conflicts += 1
                    protocol_error("upstream_turn_mismatch", "Websocket envelope changed the bound conversation")
                conversation = cid
            if turn is not None:
                if not isinstance(turn, str) or len(turn) > 512 or (bound_turn is not None and turn != bound_turn):
                    state.websocket_stream_binding_conflicts += 1
                    protocol_error("upstream_turn_mismatch", "Websocket envelope changed the bound turn")
                bound_turn = turn
            fingerprint = envelope_fingerprint(inner)
            offset = item.get("offset")
            encoded = inner.get("encoded_item")
            sid, parent = inner.get("stream_item_id"), inner.get("parent_stream_item_id")
            duplicate = False
            if kind == "stream-item":
                if not isinstance(encoded, str):return "stream_envelope_unsupported"
                for identifier in (sid, parent):
                    if identifier is not None and (not isinstance(identifier, str) or len(identifier) > 512):
                        return "stream_identifier_unsupported"
                if sid is not None:
                    state.websocket_stream_items_with_id += 1
                    if sid in stream_items_seen:
                        if stream_items_seen[sid] != fingerprint:
                            state.websocket_stream_item_conflicts += 1
                            protocol_error("upstream_stream_item_conflict", "Repeated websocket stream item has conflicting metadata or data")
                        duplicate = True
                        state.websocket_stream_id_duplicates += 1
                    elif len(stream_items_seen) >= 100000:
                        return "stream_item_limit"
            elif kind == "heartbeat":
                state.websocket_heartbeats += 1
            else:
                state.websocket_terminal_received = True
            if valid_cursor(offset) and offset in applied:
                if applied[offset] != fingerprint:
                    protocol_error("upstream_cursor_conflict", "Repeated websocket cursor has conflicting data")
                state.websocket_duplicate_messages += 1
                return None
            if valid_cursor(offset) and len(applied) >= 100000:
                return "cursor_dedup_limit"
            blocks = []
            if kind == "stream-item" and not duplicate:
                if parent is not None and parent not in stream_items_seen:
                    state.websocket_stream_parent_misses += 1
                    return "stream_parent_missing"
                native_item = "stream_item_id" in inner or "parent_stream_item_id" in inner
                if native_item:
                    fields = [line[5:].strip() for line in sse_lines(encoded) if line.startswith("data:")]
                    value = "\n".join(fields)
                    try:
                        if not fields:raise ValueError()
                        if value != "[DONE]":json.loads(value)
                    except (ValueError, TypeError):
                        return "stream_item_invalid"
                    if pending.strip():return "stream_pending_conflict"
                    pending = ""
                if not valid_cursor(offset):cursor_safe = False
                was_empty = not pending
                # Keep legacy partial-item compatibility, but a complete native
                # encoded_item is an event boundary even without a blank line.
                pending = (pending + encoded).replace("\r\n", "\n")
                if len(pending) > max_pending:
                    protocol_error("upstream_websocket_frame_limit", "Upstream websocket event exceeded limit")
                while "\n\n" in pending:
                    block, pending = pending.split("\n\n", 1)
                    blocks.append(block)
                tail = flush_complete_tail()
                if tail is not None:
                    blocks.append(tail)
                    if was_empty:state.websocket_items_without_separator += 1
            elif kind == "done":
                tail = flush_complete_tail()
                if tail is not None:blocks.append(tail)
            state.websocket_pending_characters = len(pending)
            return {"offset": offset, "fingerprint": fingerprint, "blocks": blocks,
                    "kind": kind, "stream_id": sid if kind == "stream-item" else None,
                    "duplicate": duplicate}

        async def process_items(items):
            nonlocal cursor, cursor_safe, cursor_origin, expected_native_cursor
            for item in items:
                if isinstance(item, tuple):
                    acknowledged, last_fingerprint, batch_fingerprint, advance_ack = item
                    if valid_cursor(acknowledged):
                        if advance_ack or not cursor_safe or cursor is None:
                            if len(applied) >= 100000 and acknowledged not in applied:
                                state.websocket_status = "cursor_dedup_limit"
                                return True
                            cursor = acknowledged
                            cursor_origin = "subscription_last_offset"
                            cursor_safe = True
                            if last_fingerprint is not None:
                                applied[acknowledged] = last_fingerprint
                        if batch_fingerprint is not None:
                            acknowledged_batches.setdefault(acknowledged, batch_fingerprint)
                        state.websocket_cursor_available = cursor_safe and cursor is not None
                    continue
                prepared = apply_item(item)
                if prepared is None:
                    continue
                if isinstance(prepared, str):
                    state.websocket_status = prepared
                    return True
                offset, fingerprint, blocks = prepared["offset"], prepared["fingerprint"], prepared["blocks"]
                for block in blocks:
                    async def one_block():yield block + "\n\n"
                    async for event in events(one_block()):
                        wire.feed(event)
                        state.websocket_replayed_events += 1
                        if wire.topic_id not in (None, topic) or (conversation and wire.conversation_id not in (None, conversation)):
                            protocol_error("upstream_turn_mismatch", "Websocket data changed the bound conversation or topic")
                sid = prepared["stream_id"]
                if sid is not None and not prepared["duplicate"]:
                    stream_items_seen[sid] = fingerprint
                # A partial SSE tail is retained together with its acknowledged
                # pubsub cursor. Replaying that message must not append it twice.
                if valid_cursor(offset):
                    applied[offset] = fingerprint
                    cursor = offset
                    cursor_origin = "message_offset"
                    expected_native_cursor = offset
                    cursor_safe = True
                state.websocket_cursor_available = cursor_safe and cursor is not None
                candidate = wire.candidate()
                if candidate:
                    original_service = state.service
                    for attribute in ("upstream_model", "upstream_thinking_effort"):
                        if hasattr(wire.service, attribute):
                            setattr(original_service, attribute, getattr(wire.service, attribute))
                    original_service.upstream_output_limited = candidate[1] == "length"
                    diagnostics = {k: v for k, v in state.__dict__.items()
                                   if k.startswith("websocket_") or k in ("transport_observation", "_observation_origin")}
                    state.__dict__.update(wire.__dict__)
                    state.__dict__.update(diagnostics)
                    state.service = original_service
                    state.websocket_status = "final_received"
                    return True
                if prepared["kind"] == "done":
                    state.websocket_status = "stream_done_incomplete" if pending.strip() else "stream_done"
                    return True
            return False

        for connection in range(websocket_receive_max_reconnects + 1):
            socket = None
            disconnected = False
            subscribed = False
            ready = False
            queued = []
            queued_size = 0
            observation = {"connection": connection + 1, "started_ms": state.observation_ms(),
                           "frames": 0, "live_messages": 0, "outcome": None}
            if len(state.websocket_attempts) < 32:
                state.websocket_attempts.append(observation)
            else:
                state.websocket_attempts_truncated = True
            initial_replayed = state.websocket_replayed_events
            expected_native_cursor = None
            requested_cursor = cursor if connection else "0"
            try:
                socket = await self.open_websocket_recovery(state)
                if socket is None:
                    observation["outcome"] = state.websocket_status
                    return
                state.websocket_connections += 1
                observation["opened_ms"] = state.observation_ms()
                state.websocket_stage = "send_connect"
                await socket.asend(json.dumps([{"id": 1, "command": {"type": "connect"}}]).encode(), CurlWsFlag.TEXT)
                state.websocket_status = "connected"
                state.websocket_stage = "receive_connect"
                error_observation.stage("websocket_receive")
                while self.lifecycle_budget.remaining() > 0:
                    remaining = self.lifecycle_budget.remaining()
                    idle_wait = min(websocket_idle_timeout, remaining) if websocket_idle_timeout else remaining
                    raw, flags = await asyncio.wait_for(self.s.recv_websocket(socket), timeout=idle_wait)
                    if flags & CurlWsFlag.CLOSE:
                        size = len(raw) if isinstance(raw, (bytes, bytearray, str)) else 0
                        observation.update(outcome="peer_close_frame", closed_ms=state.observation_ms(),
                            close_payload_bytes=size,
                            close_code=int.from_bytes(raw[:2], "big") if isinstance(raw, (bytes, bytearray)) and size >= 2 else None)
                        state.websocket_status = "closed"
                        disconnected = True
                        break
                    state.websocket_frames += 1
                    observation["frames"] += 1
                    observation["last_frame_ms"] = state.observation_ms()
                    if len(raw) > max_pending:
                        protocol_error("upstream_websocket_frame_limit", "Upstream websocket frame exceeded limit")
                    try:envelope = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):continue
                    items = envelope if isinstance(envelope, list) else [envelope]
                    work = []
                    for item in items:
                        if not isinstance(item, dict):continue
                        reply = item.get("reply") or {}
                        if not isinstance(reply, dict):reply = {}
                        if reply.get("type") == "connect" and not subscribed:
                            state.websocket_stage = "send_subscribe"
                            requested_cursor = cursor if connection else "0"
                            observation.update(requested_cursor_origin=cursor_origin if connection else "bootstrap_zero",
                                requested_cursor_hash=cursor_hash(requested_cursor) if connection else None,
                                subscribe_sent_ms=state.observation_ms())
                            await socket.asend(json.dumps([{"id": 2, "command": {"type": "subscribe",
                                "topic_id": topic, "offset": requested_cursor}}]).encode(), CurlWsFlag.TEXT)
                            subscribed = True
                            state.websocket_status = "subscribed"
                            state.websocket_stage = "receive_topic"
                        if reply.get("type") == "subscribe" and reply.get("topic_id") == topic:
                            recovered = reply.get("recovered")
                            category = ("missing" if "recovered" not in reply else "true" if recovered is True
                                        else "false" if recovered is False else "null" if recovered is None else "invalid_type")
                            caught = reply.get("catchups")
                            target_caught = [x for x in caught if isinstance(x, dict) and x.get("topic_id") == topic] if isinstance(caught, list) else []
                            advertised_cursor = reply.get("last_offset")
                            expected_native_cursor = advertised_cursor if valid_cursor(advertised_cursor) else requested_cursor
                            for caught_item in target_caught:
                                if valid_cursor(caught_item.get("offset")):expected_native_cursor = caught_item["offset"]
                            observation.update(subscribe_reply_ms=state.observation_ms(), recovered_field=category,
                                subscribe_reply_id_matches=item.get("id") == 2,
                                last_offset_present="last_offset" in reply, last_offset_valid=valid_cursor(advertised_cursor),
                                last_offset_hash=cursor_hash(advertised_cursor),
                                last_offset_equals_requested=advertised_cursor == requested_cursor,
                                catchup_count=len(caught) if isinstance(caught, list) else 0,
                                target_catchup_count=len(target_caught),
                                catchups_with_offset=sum(valid_cursor(x.get("offset")) for x in target_caught),
                                catchups_without_offset=sum("offset" not in x for x in target_caught),
                                catchups_invalid_offset=sum("offset" in x and not valid_cursor(x.get("offset")) for x in target_caught))
                            if type(recovered) is not bool or (connection and not recovered):
                                observation["outcome"] = "recovery_refused" if recovered is False else "invalid_recovered_field"
                                state.websocket_status = "cursor_not_recovered"
                                return
                            catchups = reply.get("catchups") or []
                            if not isinstance(catchups, list):
                                protocol_error("invalid_upstream_subscription", "Invalid websocket subscription catchup")
                            if wire is None:
                                if recovered and catchups:
                                    # Offset zero replay starts from a fresh delta
                                    # decoder, not an already-applied SSE state.
                                    wire = ConversationState(decoder_service)
                                    wire.target_user_id = target_user
                                    wire.user_seen = target_user is None
                                    wire.conversation_id = conversation
                                    wire.resume_token = state.resume_token
                                    wire.topic_id = topic
                                    wire.handoff = state.handoff
                                else:
                                    wire = state.checkpoint()
                                    wire.service = decoder_service
                            advertised = reply.get("last_offset")
                            stream_items = []
                            for caught in catchups:
                                if not isinstance(caught, dict) or caught.get("topic_id") != topic:
                                    continue
                                outer = caught.get("payload") or {}
                                inner = outer.get("payload") if isinstance(outer, dict) else None
                                if isinstance(outer, dict) and outer.get("type") == "conversation-turn-stream" and isinstance(inner, dict) and inner.get("type") == "stream-item" and isinstance(inner.get("encoded_item"), str):
                                    stream_items.append(caught)
                            # Native replies may provide only a batch last_offset.
                            # Commit it after the catchup, before any queued live data.
                            tail = catchups[-1] if catchups and isinstance(catchups[-1], dict) and catchups[-1].get("topic_id") == topic else None
                            tail_outer = (tail.get("payload") or {}) if tail is not None else {}
                            tail_inner = tail_outer.get("payload") if isinstance(tail_outer, dict) else None
                            if not isinstance(tail_inner, dict) or tail_inner.get("type") not in ("stream-item", "heartbeat", "done"):
                                tail = None
                            uses_batch_cursor = tail is not None and not valid_cursor(tail.get("offset"))
                            needs_batch_guard = uses_batch_cursor or any(not valid_cursor(x.get("offset")) for x in stream_items)
                            batch_fingerprint = None
                            tail_fingerprint = None
                            replayed_batch = False
                            if recovered and needs_batch_guard and tail is not None and valid_cursor(advertised):
                                batch_fingerprint = hashlib.sha256(json.dumps(catchups, sort_keys=True).encode()).digest()
                                tail_fingerprint = envelope_fingerprint(tail_inner)
                                previous = acknowledged_batches.get(advertised)
                                previous_tail = applied.get(advertised)
                                if connection and previous is not None and previous == batch_fingerprint:
                                    replayed_batch = True
                                    state.websocket_duplicate_messages += len(stream_items)
                                elif connection and len(catchups) == 1 and previous_tail is not None:
                                    if previous_tail != tail_fingerprint:
                                        protocol_error("upstream_cursor_conflict", "Repeated websocket catchup cursor has conflicting data")
                                    replayed_batch = True
                                    state.websocket_duplicate_messages += len(stream_items)
                                elif connection and (previous is not None or advertised == requested_cursor or advertised in applied):
                                    protocol_error("upstream_cursor_conflict", "Websocket catchup did not advance its bound cursor")
                            if recovered and not replayed_batch:work.extend(catchups)
                            if replayed_batch:
                                for caught in stream_items:
                                    identity = caught["payload"]["payload"].get("stream_item_id")
                                    if identity is not None:
                                        state.websocket_stream_items_with_id += 1
                                        if identity in stream_items_seen:state.websocket_stream_id_duplicates += 1
                            if recovered and uses_batch_cursor and valid_cursor(advertised):
                                work.append((advertised, tail_fingerprint, batch_fingerprint, not replayed_batch))
                            work.extend(queued)
                            queued = []
                            queued_size = 0
                            ready = True
                        if item.get("type") == "message" and item.get("topic_id") == topic:
                            observation["live_messages"] += 1
                            if ready:work.append(item)
                            else:
                                queued_size += len(json.dumps(item))
                                if queued_size > max_pending:
                                    protocol_error("upstream_websocket_frame_limit", "Pending websocket subscription exceeded limit")
                                queued.append(item)
                    if wire is not None and await process_items(work):
                        observation["outcome"] = state.websocket_status
                        return
                else:
                    state.websocket_status = "deadline_exhausted"
                    return
            except asyncio.CancelledError:
                observation["outcome"] = "cancelled"
                raise
            except asyncio.TimeoutError as error:
                state.websocket_status = ("deadline_exhausted" if self.lifecycle_budget.remaining() <= 0
                                          else "idle_timeout" if socket else "connect_timeout")
                state.websocket_error = safe_transport_error(error)
                observation.update(outcome=state.websocket_status, error=dict(state.websocket_error))
                return
            except HTTPException:
                observation["outcome"] = "protocol_error"
                if wire is not None and wire.upstream_error is not None:
                    state.upstream_error = copy.deepcopy(wire.upstream_error)
                raise
            except Exception as error:
                state.websocket_status = "transport_error"
                state.websocket_error = safe_transport_error(error)
                observation.update(outcome="transport_error", error=dict(state.websocket_error))
                disconnected = (state.websocket_stage in ("receive_connect", "receive_topic") and
                    (state.websocket_error.get("code") in (52, 56) or
                     isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))))
                if not disconnected:
                    return
            finally:
                observation.update(exited_ms=state.observation_ms(), applied_cursor_origin=cursor_origin,
                    applied_cursor_hash=cursor_hash(cursor), replayed_events=state.websocket_replayed_events - initial_replayed)
                if ready and valid_cursor(expected_native_cursor) and valid_cursor(cursor):
                    observation["native_cursor_matches_applied"] = expected_native_cursor == cursor
                if observation["outcome"] is None:observation["outcome"] = state.websocket_status
                if socket:
                    try:
                        closer = getattr(self.s, "close_websocket", None)
                        await asyncio.wait_for(closer(socket) if closer else socket.aclose(), timeout=3)
                    except Exception:pass
            if not disconnected or connection >= websocket_receive_max_reconnects:
                return
            if wire is None or not cursor_safe or not valid_cursor(cursor):
                if state.websocket_status != "closed":
                    state.websocket_status = "cursor_unavailable"
                return
            if self.lifecycle_budget.remaining() <= 0:
                state.websocket_status = "deadline_exhausted"
                return
            state.websocket_reconnects += 1
            state.websocket_status = "reconnecting"
            await asyncio.sleep(min(0.5 * 2 ** min(connection, 4), 5, self.lifecycle_budget.remaining()))

    async def resume_web_sse(self, state):
        """Resume an existing turn at its acknowledged SSE cursor, never generate."""
        from chatgpt.webStream import events
        if not state.conversation_id or not state.resume_token:return
        for attempt in range(3):
            if not self.lifecycle_budget.read("sse_resume"):return
            response = None
            try:
                headers = {**self.base_headers, "accept": "text/event-stream", "x-conduit-token": state.resume_token}
                response = await self.s.post_stream(self.base_url + "/f/conversation/resume", headers=headers,
                    json={"conversation_id": state.conversation_id, "offset": state.transport_offset},
                    timeout=min(conversation_timeout, self.lifecycle_budget.remaining()), stream=True)
                status = response.status_code
                key = "http_" + str(status)
                state.resume_statuses[key] = state.resume_statuses.get(key, 0) + 1
                if status != 200:
                    if status not in (408, 409, 425, 429, 502, 504):return
                elif "text/event-stream" not in response.headers.get("Content-Type", ""):
                    state.resume_statuses["unexpected_content_type"] = 1
                    return
                else:
                    async for event in events(response.aiter_lines()):
                        state.feed(event)
                        if event == "[DONE]":break
                    # A completed stream is not blindly retried, even without final.
                    return
            except asyncio.CancelledError:
                raise
            except HTTPException:
                raise
            except Exception:
                state.resume_statuses["transport_error"] = state.resume_statuses.get("transport_error", 0) + 1
                # After any received delta, preserve cursor/state and let native
                # offset semantics continue; no model generation is retried.
            finally:
                if response is not None and hasattr(response, "aclose"):
                    try:await response.aclose()
                    except Exception:pass
            if attempt < 2:
                await asyncio.sleep(min(0.3 * 1.5 ** attempt, self.lifecycle_budget.remaining()))

    async def recover_web_conversation(self, state):
        # Same account, same conversation, exact active-branch ancestry. No new model turn.
        if not state.conversation_id or not self.input_message_id:
            return
        deadline = min(self.lifecycle_budget.deadline, time.monotonic() + recovery_max_seconds)
        missing = 0
        state.readback_statuses = {}
        while time.monotonic() < deadline and self.lifecycle_budget.read():
            state.poll_reads += 1
            try:
                response = await self.s.get(self.base_url + "/conversation/" + quote(state.conversation_id, safe=""),
                    headers=self.base_headers, timeout=min(10, max(0.1, deadline-time.monotonic())))
                status = response.status_code
                state.readback_statuses[str(status)] = state.readback_statuses.get(str(status), 0) + 1
                if status == 200:
                    payload = response.json()
                    if isinstance(payload, dict) and state.readback(payload, self.input_message_id):
                        state.recovered = True
                        return
                elif status == 404:
                    missing += 1
                    if missing >= 3:return
                elif status in (401, 403):
                    return
            except asyncio.CancelledError:
                raise
            except HTTPException:
                raise
            except Exception:
                state.readback_statuses["transport_error"] = state.readback_statuses.get("transport_error", 0) + 1
            delay = min(state.poll_interval, max(0, deadline-time.monotonic()))
            if delay:await asyncio.sleep(delay)

    async def get_download_url(self, file_id):
        url = f"{self.base_url}/files/{file_id}/download"
        headers = self.base_headers.copy()
        try:
            r = await self.s.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                download_url = r.json().get('download_url')
                return download_url
            else:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        except Exception as e:
            logger.error(f"Failed to get download url: {e}")
            return ""

    async def get_attachment_url(self, file_id, conversation_id):
        url = f"{self.base_url}/conversation/{conversation_id}/attachment/{file_id}/download"
        headers = self.base_headers.copy()
        try:
            r = await self.s.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                download_url = r.json().get('download_url')
                return download_url
            else:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        except Exception as e:
            logger.error(f"Failed to get download url: {e}")
            return ""

    async def get_download_url_from_upload(self, file_id):
        url = f"{self.base_url}/files/{file_id}/uploaded"
        headers = self.base_headers.copy()
        try:
            r = await self.s.post(url, headers=headers, json={}, timeout=10)
            if r.status_code == 200:
                download_url = r.json().get('download_url')
                return download_url
            else:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        except Exception as e:
            logger.error(f"Failed to get download url from upload: {e}")
            return ""

    async def get_upload_url(self, file_name, file_size, use_case="multimodal"):
        url = f'{self.base_url}/files'
        headers = self.base_headers.copy()
        try:
            r = await self.s.post(
                url,
                headers=headers,
                json={"file_name": file_name, "file_size": file_size, "reset_rate_limits": False, "timezone_offset_min": -480, "use_case": use_case},
                timeout=5,
            )
            if r.status_code == 200:
                res = r.json()
                file_id = res.get('file_id')
                upload_url = res.get('upload_url')
                logger.info(f"file_id: {file_id}, upload_url: {upload_url}")
                return file_id, upload_url
            else:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        except Exception as e:
            logger.error(f"Failed to get upload url: {e}")
            return "", ""

    async def upload(self, upload_url, file_content, mime_type):
        headers = self.base_headers.copy()
        headers.update(
            {
                'accept': 'application/json, text/plain, */*',
                'content-type': mime_type,
                'x-ms-blob-type': 'BlockBlob',
                'x-ms-version': '2020-04-08',
            }
        )
        headers.pop('authorization', None)
        headers.pop('oai-device-id', None)
        headers.pop('oai-language', None)
        try:
            r = await self.s.put(upload_url, headers=headers, data=file_content, timeout=60)
            if r.status_code == 201:
                return True
            else:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        except Exception as e:
            logger.error(f"Failed to upload file: {e}")
            return False

    async def upload_file(self, file_content, mime_type):
        if not file_content or not mime_type:
            return None

        width, height = None, None
        if mime_type.startswith("image/"):
            try:
                width, height = await get_image_size(file_content)
            except Exception as e:
                logger.error(f"Error image mime_type, change to text/plain: {e}")
                mime_type = 'text/plain'
        file_size = len(file_content)
        file_extension = await get_file_extension(mime_type)
        file_name = f"{uuid.uuid4()}{file_extension}"
        use_case = await determine_file_use_case(mime_type)

        file_id, upload_url = await self.get_upload_url(file_name, file_size, use_case)
        if file_id and upload_url:
            if await self.upload(upload_url, file_content, mime_type):
                download_url = await self.get_download_url_from_upload(file_id)
                if download_url:
                    file_meta = {
                        "file_id": file_id,
                        "file_name": file_name,
                        "size_bytes": file_size,
                        "mime_type": mime_type,
                        "width": width,
                        "height": height,
                        "use_case": use_case,
                    }
                    logger.info(f"File_meta: {file_meta}")
                    return file_meta

    async def check_upload(self, file_id):
        url = f'{self.base_url}/files/{file_id}'
        headers = self.base_headers.copy()
        try:
            for i in range(30):
                r = await self.s.get(url, headers=headers, timeout=5)
                if r.status_code == 200:
                    res = r.json()
                    retrieval_index_status = res.get('retrieval_index_status', '')
                    if retrieval_index_status == "success":
                        break
                await asyncio.sleep(1)
            return True
        except HTTPException:
            return False

    async def get_response_file_url(self, conversation_id, message_id, sandbox_path):
        try:
            url = f"{self.base_url}/conversation/{conversation_id}/interpreter/download"
            params = {"message_id": message_id, "sandbox_path": sandbox_path}
            headers = self.base_headers.copy()
            r = await self.s.get(url, headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                return r.json().get("download_url")
            else:
                return None
        except Exception:
            logger.info("Failed to get response file url")
            return None

    async def close_client(self):
        if self.s:
            await self.s.close()
            del self.s
        if self.ss:
            await self.ss.close()
            del self.ss
        if self.ws:
            await self.ws.close()
            del self.ws
