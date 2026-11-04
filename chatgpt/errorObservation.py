"""Request-local, bounded diagnostics. Never stringify exceptions or retain bodies."""
from contextvars import ContextVar

_current = ContextVar("gateway_error_observation", default=None)
STAGES = frozenset({"request_validation", "service_init", "account_verification", "account_setup",
    "model_selection", "model_catalog", "web_bootstrap", "chat_requirements", "turnstile",
    "challenge", "proof_of_work", "request_preparation", "conversation_preparation",
    "generation_request", "initial_sse", "websocket_recovery", "websocket_endpoint",
    "websocket_handshake", "websocket_receive", "sse_recovery", "snapshot_recovery",
    "final_response_validation", "response_validation", "tool_response_format", "response_encoding", "other"})
EXCEPTIONS = frozenset({"HTTPException", "UpstreamError", "ClientToolError", "CurlError", "RequestsError",
    "ConnectionError", "Timeout", "TimeoutError", "CancelledError", "OSError", "ConnectionRefusedError",
    "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError", "SSLError", "JSONDecodeError",
    "ValueError", "TypeError", "AttributeError", "KeyError", "IndexError", "RuntimeError",
    "NotImplementedError", "HTTPError", "ProxyError", "ReadTimeout", "ConnectTimeout", "TimeoutException",
    "WebSocketError", "WebSocketClosed", "UnicodeDecodeError", "UnicodeEncodeError"})


def http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def guarded_attribute(value, field):
    try:return getattr(value, field, None)
    except Exception:return None


def safe_exception(error):
    name = type(error).__name__
    result = {"exception_class": name if name in EXCEPTIONS else "OtherError"}
    for field in ("code", "errno"):
        value = guarded_attribute(error, field)
        if type(value) is int and -(2**31) <= value < 2**31:
            result[field] = value
    response = guarded_attribute(error, "response")
    status = http_status(guarded_attribute(response, "status_code"))
    if status is not None:result["upstream_status"] = status
    return result


class RequestObservation:
    def __init__(self):
        self.stage = "request_validation"
        self.upstream_status = None
        self.failure = None
        self.budget = None
        self.service = None
        self.on_stage = None

    def set_stage(self, value):
        self.stage = value if value in STAGES else "other"
        self.upstream_status = None
        if self.on_stage is not None:
            try:self.on_stage(self.stage)
            except Exception:pass

    def capture(self, error):
        # Preserve the original exception across wrappers and cleanup. An outer
        # wait_for TimeoutError replaces its induced inner CancelledError.
        if self.failure and self.failure["exception_class"] != "CancelledError":return
        value = {"stage": self.stage, **safe_exception(error)}
        if self.upstream_status is not None:value.setdefault("upstream_status", self.upstream_status)
        self.failure = value

    def trace(self, existing, status):
        result = dict(existing) if isinstance(existing, dict) else {}
        if self.budget is not None:
            try:result["budget"] = self.budget.summary()
            except Exception:pass
        elif "budget" not in result:
            # Validation failed before a generation/read budget was created.
            result["budget"] = {"generation_posts": 0, "snapshot_reads": 0,
                "websocket_setup_reads": 0, "sse_resume_reads": 0,
                "recovery_reads": 0, "generation_retries": 0}
        if not result.get("lifecycle") and self.service is not None:
            from chatgpt.webStream import ConversationState
            state = getattr(self.service, "lifecycle_state", None)
            if isinstance(state, ConversationState):
                try:result["lifecycle"] = state.summary()
                except Exception:pass
        if self.service is not None and getattr(self.service, 'chat_requirements_connect_retries', 0) == 1:
            result['preflight_connect_retries'] = 1
        if (status >= 400 or result.get('response_status') == 'failed') and self.failure is not None:
            result["failure"] = dict(self.failure)
        return result or None


def stage(value):
    observation = _current.get()
    if observation is not None:observation.set_stage(value)


def upstream_status(value):
    observation = _current.get()
    if observation is not None:observation.upstream_status = http_status(value)


def capture(error):
    observation = _current.get()
    if observation is not None:observation.capture(error)


def attach_service(service):
    observation = _current.get()
    if observation is not None:observation.service = service


def attach_budget(budget):
    observation = _current.get()
    if observation is not None:observation.budget = budget
