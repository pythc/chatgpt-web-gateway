"""Conversation lifecycle decoding. Private reasoning and resume credentials stay internal."""
import copy
import json
import base64
import re
import time
from enum import Enum

from fastapi import HTTPException


class UpstreamError(HTTPException):
    pass


class DeltaValue(Enum):
    UNDEFINED = 0


UNDEFINED = DeltaValue.UNDEFINED
MAX_DELTA_ARRAY_ITEMS = 100000


def safe_transport_error(error):
    """Class/numeric diagnostics only; curl exception strings contain signed URLs."""
    allowed = {"CurlError", "RequestsError", "ConnectionError", "Timeout", "TimeoutError", "OSError",
               "ConnectionRefusedError", "ConnectionResetError", "BrokenPipeError", "SSLError", "JSONDecodeError",
               "ValueError", "TypeError", "AttributeError", "RuntimeError", "NotImplementedError", "HTTPError",
               "ProxyError", "ReadTimeout", "ConnectTimeout", "TimeoutException", "WebSocketError", "WebSocketClosed"}
    name = type(error).__name__
    result = {"exception_class": name if name in allowed else "OtherError"}
    for attr in ("code", "errno"):
        value = getattr(error, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):result[attr] = int(value)
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        result["http_status"] = status
    else:
        # Extract just a status number from libcurl's upgrade/proxy error, never
        # retain or return its message (which may embed the verify credential).
        match = re.search(r"(?:upgrade:\s*|HTTP(?:/\d(?:\.\d)?)?\s+(?:code\s+)?)([1-5]\d\d)\b", str(error)[:16384], re.I)
        if match:result["http_status"] = int(match.group(1))
    return result


def protocol_error(code, message):
    raise UpstreamError(status_code=502, detail={"error": {"type": "upstream_protocol_error", "code": code, "message": message}})


def text_parts(content):
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        return ""
    result = []
    for part in parts:
        if isinstance(part, str):
            result.append(part)
        elif isinstance(part, dict) and part.get("type") in ("text", "output_text") and isinstance(part.get("text"), str):
            result.append(part["text"])
    return "".join(result).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


class ConversationState:
    def __init__(self, service):
        self.service = service
        self.messages = {}
        self.retired_ids = set()
        self.array_allocation_budget = [1000000]
        self.current = {}
        self.contexts = {}
        self.last_patch = None
        self.conversation_id = None
        self.resume_token = None
        self.handoff = False
        self.topic_id = None
        origin = getattr(service, "_diagnostic_sse_started", None)
        self._observation_origin = origin if isinstance(origin, (int, float)) else time.monotonic()
        self.transport_observation = {}
        for name, attribute in (("initial_sse_headers_ms", "_diagnostic_sse_headers"),
                                ("initial_sse_head_ready_ms", "_diagnostic_sse_head_ready")):
            value = getattr(service, attribute, None)
            if isinstance(value, (int, float)):
                self.transport_observation[name] = round(max(0, value - self._observation_origin) * 1000, 3)
        self.websocket_attempts = []
        self.websocket_attempts_truncated = False
        self.websocket_topic_kind = None
        self.websocket_frames = 0
        self.websocket_status = None
        self.websocket_stage = None
        self.websocket_error = None
        self.websocket_handshake_statuses = {}
        self.websocket_connections = 0
        self.websocket_reconnects = 0
        self.websocket_cursor_available = False
        self.websocket_duplicate_messages = 0
        self.websocket_stream_items_with_id = 0
        self.websocket_stream_id_duplicates = 0
        self.websocket_stream_parent_misses = 0
        self.websocket_stream_item_conflicts = 0
        self.websocket_stream_binding_conflicts = 0
        self.websocket_items_without_separator = 0
        self.websocket_heartbeats = 0
        self.websocket_terminal_received = False
        self.websocket_pending_characters = 0
        self.websocket_replayed_events = 0
        self.resume_statuses = {}
        self.done = False
        self.stream_complete = False
        self.events = 0
        self.transport_offset = 0
        self.event_types = {}
        self.upstream_error = None
        self.poll_reads = 0
        self.recovered = False
        self.markers = {}
        self.visible_order = []
        self.last_assistant_id = None
        self.target_user_id = getattr(service, "input_message_id", None)
        self.user_seen = self.target_user_id is None
        self.input_binding_observation = []
        self.poll_interval = 2.0

    def observation_ms(self):
        return round(max(0, time.monotonic() - self._observation_origin) * 1000, 3)

    def visible(self, message):
        return ((message.get("author") or {}).get("role") == "assistant"
                and message.get("channel") in (None, "final")
                and message.get("recipient") in (None, "", "all")
                and (message.get("content") or {}).get("content_type") in ("text", "refusal")
                and (message.get("metadata") or {}).get("reasoning_status") not in ("is_reasoning", "reasoning_started"))

    def bind_user(self, message):
        role = (message.get("author") or {}).get("role")
        mid = message.get("id")
        if role == 'user' and len(self.input_binding_observation) < 32:
            supplied = getattr(self.service, 'input_user_ids', [])
            self.input_binding_observation.append({'target': mid == self.target_user_id,
                'supplied_index': supplied.index(mid) if mid in supplied else None,
                'target_seen_before': self.user_seen})
        if self.target_user_id and mid == self.target_user_id and role in (None, "user"):
            self.user_seen = True
        elif self.target_user_id and self.user_seen and role == "user" and mid not in getattr(self.service, 'input_user_ids', []):
            protocol_error("upstream_turn_mismatch", "Upstream data advanced to a different user turn")

    def snapshot(self, message):
        mid = message.get("id")
        if not isinstance(mid, str) or not mid:
            protocol_error("invalid_upstream_message", "Upstream message has no stable identifier")
        self.bind_user(message)
        self.messages[mid] = copy.deepcopy(message)
        self.retired_ids.discard(mid)
        role = (message.get("author") or {}).get("role")
        if role == "assistant" and self.user_seen:
            self.last_assistant_id = mid
            # Verify model metadata independently of private reasoning contents.
            from chatgpt.chatFormat import record_upstream_model
            record_upstream_model(self.service, message)
            metadata = message.get("metadata") or {}
            hint = metadata.get("poll_interval_ms")
            if isinstance(hint, (int, float)) and not isinstance(hint, bool):
                self.poll_interval = max(0.5, min(10.0, hint / 1000))
        if self.user_seen and self.visible(message):
            if mid in self.visible_order:
                self.visible_order.remove(mid)
            self.visible_order.append(mid)
            if (message.get("metadata", {}).get("finish_details") or {}).get("type") == "max_tokens":
                self.service.upstream_output_limited = True

    def candidate(self):
        for mid in reversed(self.visible_order):
            if mid in self.retired_ids:continue
            m = self.messages[mid]
            if mid != self.last_assistant_id:continue
            if not text_parts(m.get("content")):
                continue
            finish = (m.get("metadata", {}).get("finish_details") or {}).get("type")
            status = m.get("status")
            if status == "finished_partial_completion":
                if finish == "max_tokens":
                    return m, "length"
                protocol_error("upstream_incomplete_response", "Upstream final response was interrupted")
            complete = status == "finished_successfully" and (
                m.get("end_turn") is True or m.get("channel") == "final" or
                bool(self.markers.get(mid, set()) & {"final_channel_token", "last_token"}))
            if complete:
                return m, "length" if finish == "max_tokens" else "stop"
        return None

    def checkpoint(self):
        state = ConversationState.__new__(ConversationState)
        state.__dict__.update(copy.deepcopy({k: v for k, v in self.__dict__.items() if k != "service"}))
        state.service = self.service
        return state

    def feed(self, event, nested=False):
        self.events += 1
        if event != "[DONE]" and not nested:self.transport_offset += 1
        if event == "[DONE]":
            self.done = True
            return
        if event == "v1":
            return
        if isinstance(event, list):
            for value in event:
                self.feed(value, nested=True)
            return
        if isinstance(event, str):
            if self.last_patch:
                self.patch({"v": event})
            return
        if not isinstance(event, dict):
            protocol_error("invalid_upstream_event", "Unsupported upstream event shape")
        kind = event.get("type", "message" if "message" in event else "patch")
        safe_kind = kind if isinstance(kind, str) and len(kind) < 64 else "unknown"
        self.event_types[safe_kind] = self.event_types.get(safe_kind, 0) + 1
        if event.get("error") or kind == "error":
            # Never forward raw upstream strings: they may contain prompts or credentials.
            from chatgpt.upstreamErrorMetadata import metadata
            self.upstream_error = metadata(event.get("error"), event, "event")
            protocol_error("upstream_error_event", "Upstream returned an error event")
        cid = event.get("conversation_id")
        if isinstance(cid, str):
            if self.conversation_id and cid != self.conversation_id:
                protocol_error("upstream_conversation_mismatch", "Upstream conversation changed during response")
            self.conversation_id = cid
        if kind == "input_message":
            incoming = event.get("input_message")
            if isinstance(incoming, dict):self.bind_user(incoming)
            return
        if kind == "resume_conversation_token":
            self.resume_token = event.get("token")  # private, never serialized
            # The authenticated stream's topic token is also a recovery offer,
            # including when bootstrap ends before an explicit handoff event.
            if event.get("kind") == "topic" and isinstance(self.resume_token, str):
                try:
                    segment = self.resume_token.split(".")[1]
                    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
                    topic = claims.get("turn_topic_id")
                    if isinstance(topic, str) and topic.startswith("conversation-turn-") and len(topic) <= 512:
                        self.topic_id = topic
                except (ValueError, IndexError, TypeError, AttributeError):pass
            return
        if kind == "stream_handoff":
            self.handoff = True
            for option in event.get("options", []):
                if isinstance(option, dict) and option.get("type") == "subscribe_ws_topic":
                    topic = option.get("topic_id")
                    if isinstance(topic, str) and topic and len(topic) <= 512:
                        self.topic_id = topic
            return
        if kind == "message_stream_complete":
            self.stream_complete = True
            return
        if kind == "message_marker":
            mid, marker = event.get("message_id"), event.get("marker")
            if isinstance(mid, str) and marker in ("final_channel_token", "last_token"):
                self.markers.setdefault(mid, set()).add(marker)
            return
        if isinstance(event.get("message"), dict):
            self.current = copy.deepcopy(event)
            self.contexts[getattr(self, "current_channel", 0)] = self.current
            self.snapshot(event["message"])
            return
        if any(k in event for k in ("p", "o", "v")):
            self.patch(event)

    def patch(self, event):
        channel = event.get("c", event.get("channel", getattr(self, "current_channel", 0)))
        if not isinstance(channel, (str, int)) or isinstance(channel, bool):
            protocol_error("invalid_upstream_patch", "Invalid patch channel")
        self.current_channel = channel
        previous = self.last_patch or ("", "add")
        path = event.get("p", event.get("path", previous[0]))
        op = event.get("o", event.get("op", previous[1]))
        value = event.get("v", event.get("value"))
        self.last_patch = (path, op)
        # Delta c selects independent root values. Omitted c/p/o inherit the
        # previous outer delta, while children of a compound patch are relative.
        root = self.contexts.get(channel, UNDEFINED)
        old_message = root.get("message") if isinstance(root, dict) else None
        old_id = old_message.get("id") if isinstance(old_message, dict) else None
        try:self.current = self.apply_patch(root, path, op, value, self.array_allocation_budget)
        except UpstreamError as error:
            detail = error.detail.get("error", {})
            if detail.get("code") == "invalid_upstream_patch":
                known = {"message", "content", "parts", "status", "end_turn", "metadata", "finish_details", "type", "author", "role", "channel", "recipient"}
                pointer = path.split("/") if isinstance(path, str) else []
                detail["patch"] = {"operation": op if op in ("add", "append", "replace", "remove", "patch", "truncate") else "unknown",
                    "path_shape": "/".join(k if k in known or k == "" else "#" if k.isdecimal() else "other" for k in pointer),
                    "value_type": type(value).__name__, "value_size": len(value) if isinstance(value, (str, list, dict)) else None}
            raise
        self.contexts[channel] = self.current
        if op == "remove" and path in ("", "/message", "message") and old_id:
            self.retired_ids.add(old_id)
        if not isinstance(self.current, dict):return
        cid = self.current.get("conversation_id")
        if isinstance(cid, str):
            if self.conversation_id and cid != self.conversation_id:
                protocol_error("upstream_conversation_mismatch", "Patch conversation changed")
            self.conversation_id = cid
        if self.current.get("error"):
            from chatgpt.upstreamErrorMetadata import metadata
            self.upstream_error = metadata(self.current["error"], self.current, "delta")
            protocol_error("upstream_error_event", "Upstream returned an error event")
        if isinstance(self.current.get("message"), dict):
            self.snapshot(self.current["message"])

    @classmethod
    def grow_array(cls, array, size, budget):
        growth = max(0, size - len(array))
        if size > MAX_DELTA_ARRAY_ITEMS or growth > budget[0]:
            protocol_error("invalid_upstream_patch", "Delta array allocation budget exceeded")
        budget[0] -= growth
        array.extend([UNDEFINED] * growth)

    @classmethod
    def apply_patch(cls, root, path, op, value, allocation_budget=None):
        if allocation_budget is None:allocation_budget = [1000000]
        if not isinstance(path, str) or op not in ("add", "append", "replace", "remove", "patch", "truncate"):
            protocol_error("invalid_upstream_patch", "Unsupported patch operation")
        keys = [] if path == "" else [k.replace("~1", "/").replace("~0", "~") for k in (path[1:] if path.startswith("/") else path).split("/")]
        wrapper = {"root": root}
        parent = wrapper
        keys = ["root"] + keys
        try:
            for index, key in enumerate(keys[:-1]):
                next_key = keys[index + 1]
                if isinstance(parent, list):
                    key = int(key)
                    if key < 0:raise ValueError()
                    cls.grow_array(parent, key + 1, allocation_budget)
                elif not isinstance(parent, dict):raise ValueError()
                child = parent[key] if isinstance(parent, list) else parent.get(key, UNDEFINED)
                if child is UNDEFINED:
                    child = [] if next_key.isdecimal() or next_key == "-" else {}
                    parent[key] = child
                parent = child
            key = keys[-1]
            if isinstance(parent, list):
                key = len(parent) if key == "-" else int(key)
                if op in ("add", "remove"):
                    key = max(0, min(key if key >= 0 else len(parent) + key, len(parent)))
                elif key < 0:raise ValueError()
                # The web decoder allows append/replace to initialize the next
                # array element, e.g. final parts=[] then append /parts/0.
                if key >= len(parent) and op in ("append", "replace", "patch"):
                    cls.grow_array(parent, key + 1, allocation_budget)
                old = parent[key] if key < len(parent) else UNDEFINED
            elif isinstance(parent, dict):old = parent.get(key, UNDEFINED)
            else:raise ValueError()
            if op == "remove":
                if isinstance(parent, dict):parent.pop(key, None)
                elif key < len(parent):del parent[key]
            elif op == "patch":
                if not isinstance(value, list):raise ValueError()
                for child in value:
                    if not isinstance(child, dict):raise ValueError()
                    old = cls.apply_patch(old, child.get("p", child.get("path", "")),
                        child.get("o", child.get("op")), child.get("v", child.get("value")), allocation_budget)
                parent[key] = old
            elif op == "append":
                if isinstance(old, str):
                    if not isinstance(value, str):raise ValueError()
                    parent[key] = old + value
                elif isinstance(old, list):
                    extra = copy.deepcopy(value) if isinstance(value, list) else [copy.deepcopy(value)]
                    size = len(old);cls.grow_array(old, size + len(extra), allocation_budget);old[size:] = extra
                elif isinstance(old, dict) and isinstance(value, dict):old.update(copy.deepcopy(value))
                else:parent[key] = copy.deepcopy(value)
            elif op == "truncate":
                if type(value) is not int or value < 0:raise ValueError()
                if isinstance(old, str):parent[key] = old.encode("utf-16-le", "surrogatepass")[:value*2].decode("utf-16-le", "surrogatepass")
                elif isinstance(old, list):
                    if value > len(old):cls.grow_array(old, value, allocation_budget)
                    else:del old[value:]
            elif isinstance(parent, list) and op == "add":
                size = len(parent);cls.grow_array(parent, size + 1, allocation_budget);parent.pop();parent.insert(key, copy.deepcopy(value))
            else:parent[key] = copy.deepcopy(value)
        except (KeyError, IndexError, ValueError, TypeError):
            protocol_error("invalid_upstream_patch", "Could not apply upstream patch")
        return wrapper.get("root", UNDEFINED)

    def readback(self, payload, user_id):
        """Accept only the active branch descending from this exact request's user node."""
        mapping = payload.get("mapping")
        node = payload.get("current_node")
        if not isinstance(mapping, dict) or not isinstance(node, str) or user_id not in mapping:
            return False
        chain, seen = [], set()
        while node and node not in seen and node in mapping:
            seen.add(node);entry = mapping[node]
            if not isinstance(entry, dict):return False
            chain.append(entry)
            if node == user_id:break
            node = entry.get("parent")
        if not node == user_id:
            return False
        for entry in reversed(chain[:-1]):
            message = entry.get("message")
            if not isinstance(message, dict):continue
            if (message.get("author") or {}).get("role") == "user":
                return False
        self.user_seen = True
        for entry in reversed(chain[:-1]):
            message = entry.get("message")
            if isinstance(message, dict):self.snapshot(message)
        return self.candidate() is not None

    def summary(self):
        try:result = self.candidate()
        except HTTPException:result = None
        states = {}
        for m in self.messages.values():
            # Structural values only; never message text, metadata bodies or identifiers.
            values = [(m.get("author") or {}).get("role"), m.get("channel"),
                      (m.get("content") or {}).get("content_type"), m.get("status"), m.get("end_turn")]
            known = {None, True, False, "system", "user", "assistant", "tool", "analysis", "final", "commentary",
                     "text", "thoughts", "reasoning_recap", "code", "model_editable_context", "user_editable_context",
                     "finished_successfully", "finished_partial_completion", "in_progress"}
            key = "/".join(str(v) if isinstance(v, (str, bool, type(None))) and v in known else "other" for v in values)
            states[key] = states.get(key, 0) + 1
        return {"events": self.events, "event_types": self.event_types, "message_states": states,
                "input_binding_observation": self.input_binding_observation,
                "upstream_error": copy.deepcopy(self.upstream_error),
                "transport_observation": dict(self.transport_observation),
                "websocket_attempts": copy.deepcopy(self.websocket_attempts),
                "websocket_attempts_truncated": self.websocket_attempts_truncated,
                "websocket_topic_kind": self.websocket_topic_kind,
                "message_count": len(self.messages), "visible_final_characters": len(text_parts(result[0]["content"])) if result else 0,
                "stream_complete": self.stream_complete, "done": self.done, "handoff": self.handoff,
                "websocket_frames": self.websocket_frames, "websocket_status": self.websocket_status,
                "websocket_stage": self.websocket_stage, "websocket_error": self.websocket_error,
                "websocket_handshake_statuses": self.websocket_handshake_statuses,
                "websocket_connections": self.websocket_connections, "websocket_reconnects": self.websocket_reconnects,
                "websocket_cursor_available": self.websocket_cursor_available,
                "websocket_duplicate_messages": self.websocket_duplicate_messages,
                "websocket_stream_items_with_id": self.websocket_stream_items_with_id,
                "websocket_stream_id_duplicates": self.websocket_stream_id_duplicates,
                "websocket_stream_parent_misses": self.websocket_stream_parent_misses,
                "websocket_stream_item_conflicts": self.websocket_stream_item_conflicts,
                "websocket_stream_binding_conflicts": self.websocket_stream_binding_conflicts,
                "websocket_items_without_separator": self.websocket_items_without_separator,
                "websocket_heartbeats": self.websocket_heartbeats,
                "websocket_terminal_received": self.websocket_terminal_received,
                "websocket_pending_characters": self.websocket_pending_characters,
                "websocket_replayed_events": self.websocket_replayed_events,
                "resume_statuses": self.resume_statuses, "sse_event_offset": self.transport_offset,
                "recovery_topic_available": self.topic_id is not None,
                "snapshot_reads": self.poll_reads, "recovered_from_snapshot": self.recovered,
                "readback_statuses": getattr(self, "readback_statuses", {}),
                "thoughts_message_count": sum(m.get("content", {}).get("content_type") == "thoughts" for m in self.messages.values())}


def sse_lines(text):
    # Only protocol LF/CRLF delimit lines. Unicode separators inside JSON
    # strings are content, unlike Python str.splitlines() boundaries.
    return text.replace("\r\n", "\n").removesuffix("\n").split("\n")


async def events(lines):
    pending = []
    def decode():
        value = "\n".join(pending)
        if value == "[DONE]":return value
        try:return json.loads(value)
        except (ValueError, TypeError):protocol_error("invalid_upstream_json", "Invalid upstream SSE data")
    async for raw in lines:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        for line in sse_lines(text):
            if line.startswith("data:"):
                # The deployed curl iterator and existing callers may omit blank separators.
                if pending:
                    try:
                        json.loads("\n".join(pending));ready = True
                    except ValueError:ready = "\n".join(pending) == "[DONE]"
                    if ready:
                        yield decode();pending = []
                pending.append(line[5:].lstrip(" "))
            elif line == "" and pending:
                yield decode();pending = []
    if pending:
        yield decode()


class LifecycleBudget:
    def __init__(self, seconds, max_generations=65, max_reads=120):
        import time
        self.deadline = time.monotonic() + seconds
        self.max_generations = max_generations
        self.max_reads = max_reads
        self.generations = 0
        self.reads = 0
        self.read_types = {}

    def summary(self):
        return {"generation_posts": self.generations, "snapshot_reads": self.read_types.get("snapshot", 0),
                "websocket_setup_reads": self.read_types.get("websocket_setup", 0),
                "sse_resume_reads": self.read_types.get("sse_resume", 0),
                "recovery_reads": self.reads, "generation_retries": 0}

    def remaining(self):
        import time
        return max(0, self.deadline - time.monotonic())

    def generation(self):
        if self.generations >= self.max_generations or self.remaining() <= 0:
            protocol_error("upstream_budget_exhausted", "Generation or time budget exhausted")
        self.generations += 1

    def read(self, kind="snapshot"):
        if self.reads >= self.max_reads or self.remaining() <= 0:
            return False
        self.reads += 1
        self.read_types[kind] = self.read_types.get(kind, 0) + 1
        return True
