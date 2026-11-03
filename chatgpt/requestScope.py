"""Own only this ASGI request's task; bound work/cleanup and journal safe lifecycle metadata."""
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from chatgpt import errorObservation as observation

CLEANUP_GRACE_SECONDS = 3
ACTIVE_REQUESTS = {}
CLEANUP_TASKS = {}


class RequestDeadlineExceeded(Exception):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def consume(task):
    if not task.cancelled():
        try:task.exception()
        except BaseException:pass


class GatewayRequestScope:
    def __init__(self, app, audit, activity, budget_factory):
        self.app, self.audit, self.activity, self.budget_factory = app, audit, activity, budget_factory

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').endswith('/v1/chat/completions'):
            return await self.app(scope, receive, send)
        state = scope.setdefault('state', {})
        request_id = str(uuid.uuid4())
        raw_id = next((v for k, v in scope.get('headers', []) if k.lower() == b'x-client-request-id'), None)
        try:client_id = str(uuid.UUID(raw_id.decode('ascii'))) if raw_id else None
        except (ValueError, UnicodeDecodeError):client_id = None
        state.update(request_id=request_id, gateway_scope_observed=True)
        observer = observation.RequestObservation()
        budget = self.budget_factory()
        state['lifecycle_budget'] = budget
        observer.budget = budget
        started = time.monotonic()
        started_at = timestamp()
        token = observation._current.set(observer)
        disconnected = asyncio.Event()
        messages = asyncio.Queue(maxsize=1)
        response_status, response_complete, content_type = None, False, b''
        outcome, status, work, cleanup_pending = 'response', 500, None, False
        stage_count = 0

        def activity(event, **fields):
            self.activity({'event': event, 'at': timestamp(), 'request_id': request_id,
                'client_request_id': client_id, **fields})

        def stage_changed(value):
            nonlocal stage_count
            stage_count += 1
            if stage_count <= 256:activity('stage', stage=value, budget_at_stage_entry=budget.summary())
            elif stage_count == 257:activity('stages_truncated')

        observer.on_stage = stage_changed
        activity('started', stage='request_validation', budget=budget.summary())

        async def pump():
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':disconnected.set()
                await messages.put(message)
                if message['type'] == 'http.disconnect':return

        async def wrapped_send(message):
            nonlocal response_status, response_complete, content_type
            if message['type'] == 'http.response.start':
                response_status = message['status']
                headers = [(k, v) for k, v in message.get('headers', [])
                           if k.lower() not in (b'x-request-id', b'x-client-request-id')]
                headers.append((b'x-request-id', request_id.encode()))
                if client_id:headers.append((b'x-client-request-id', client_id.encode()))
                content_type = next((v for k, v in headers if k.lower() == b'content-type'), b'')
                message = {**message, 'headers': headers}
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                response_complete = True

        async def worker_send(message):
            # A worker that ignores cancellation may finish later. It must not
            # replace the scope's timeout response or write to a departed client.
            if outcome != 'response':return
            await wrapped_send(message)

        async def error_response(code, detail):
            if disconnected.is_set():return
            if response_status is None:
                await wrapped_send({'type': 'http.response.start', 'status': code,
                    'headers': [(b'content-type', b'application/json')]})
                body = json.dumps({'detail': detail}).encode()
            elif b'text/event-stream' in content_type:
                body = ('data: ' + json.dumps({'error': {'code': 'gateway_request_deadline',
                    'message': detail}}) + '\n\n').encode()
            else:
                # Do not invent a successful final answer for an interrupted body.
                body = b''
            if not response_complete:
                await wrapped_send({'type': 'http.response.body', 'body': body, 'more_body': False})

        def terminal():
            record = {'request_id': request_id, 'client_request_id': client_id,
                'status': status, 'elapsed_seconds': round(time.monotonic()-started, 3),
                'started_at': started_at, 'finished_at': timestamp(), 'outcome': outcome,
                'response_status_sent': response_status, 'response_complete': response_complete,
                'local_task_done': True, 'remote_generation_stopped': None,
                'trace': observer.trace(state.get('gateway_trace'), status)}
            self.audit(record)
            activity('finished', outcome=outcome, status=status, local_task_done=True)

        async def stop_owned_work():
            nonlocal cleanup_pending
            if work is None or work.done():return
            work.cancel()
            done, _ = await asyncio.wait({work}, timeout=CLEANUP_GRACE_SECONDS)
            cleanup_pending = work not in done
            if cleanup_pending:
                CLEANUP_TASKS[request_id] = work
                activity('cleanup_pending', outcome=outcome, status=status)
                def completed(task):
                    consume(task)
                    CLEANUP_TASKS.pop(request_id, None)
                    ACTIVE_REQUESTS.pop(request_id, None)
                    activity('cleanup_completed', local_task_done=True)
                    terminal()
                work.add_done_callback(completed)
            else:consume(work)

        pump_task = asyncio.create_task(pump())
        disconnect_wait = asyncio.create_task(disconnected.wait())
        try:
            work = asyncio.create_task(self.app(scope, messages.get, worker_send))
            ACTIVE_REQUESTS[request_id] = work
            done, _ = await asyncio.wait({work, disconnect_wait}, timeout=budget.remaining(),
                                         return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
                status = response_status if response_status is not None else 500
                if not response_complete:outcome = 'response_incomplete'
            elif disconnect_wait in done and not response_complete:
                outcome, status = 'client_disconnected', 499
                observer.capture(asyncio.CancelledError())
                activity('client_disconnected', stage=observer.stage, budget=budget.summary())
                await stop_owned_work()
            elif response_complete:
                # A disconnect after the final body does not invalidate success.
                cleanup_done, _ = await asyncio.wait({work}, timeout=budget.remaining())
                if work not in cleanup_done:raise RequestDeadlineExceeded()
                await work
                status = response_status
            else:
                raise RequestDeadlineExceeded()
        except RequestDeadlineExceeded:
            outcome = 'cleanup_deadline_exceeded' if response_complete else 'deadline_exceeded'
            status = 504
            observer.capture(TimeoutError())
            activity('deadline_exceeded', stage=observer.stage, budget=budget.summary())
            await stop_owned_work()
            await error_response(504, 'Request deadline exceeded')
        except asyncio.CancelledError as error:
            outcome, status = 'server_cancelled', 499
            observer.capture(error)
            await stop_owned_work()
            raise
        except Exception as error:
            outcome, status = 'unhandled_error', 500
            observer.capture(error)
            await stop_owned_work()
            await error_response(500, 'Server error')
        finally:
            pump_task.cancel();disconnect_wait.cancel()
            await asyncio.gather(pump_task, disconnect_wait, return_exceptions=True)
            try:
                if not cleanup_pending:
                    if work is not None:consume(work)
                    ACTIVE_REQUESTS.pop(request_id, None)
                    terminal()
            finally:
                observation._current.reset(token)
