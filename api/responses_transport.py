"""Bounded stream backpressure and owned cancellation, without model retries."""
import asyncio


async def heartbeat(stream, interval=5):
    queue = asyncio.Queue(maxsize=8)
    errors = []

    async def produce():
        try:
            async for event in stream:
                await queue.put(event)
        except BaseException as error:
            errors.append(error)
        finally:
            if not asyncio.current_task().cancelling():
                await queue.put(None)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                value = await asyncio.wait_for(queue.get(), interval)
            except asyncio.TimeoutError:
                if task.done():
                    if errors:raise errors[0]
                    return
                yield ': gateway awaiting upstream\n\n'
                continue
            if value is None:
                if errors:raise errors[0]
                return
            yield value
    finally:
        if not task.done():task.cancel()
        done, _ = await asyncio.wait({task}, timeout=3)
        if task in done:
            try:task.result()
            except BaseException:pass
        else:
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
