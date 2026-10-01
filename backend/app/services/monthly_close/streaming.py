"""Transport real assistant stages and the single durable result without model tokens."""

import asyncio
import json


def frame(kind, payload):
    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_reply(answer):
    queue = asyncio.Queue(maxsize=16)

    async def progress(payload):
        await queue.put(("progress", payload))

    async def run():
        cancelled = False
        try:
            reply = await answer(progress)
            await queue.put(("result", reply.to_dict()))
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:  # noqa: BLE001 - transport boundary must redact all failures
            # Never send exception text, provider payloads or database details.
            await queue.put(
                (
                    "error",
                    {
                        "message": "本次请求没有正常返回。请刷新查看已保存的结果，再决定是否重试；不要重复确认同一方案。"
                    },
                )
            )
        finally:
            if cancelled:
                # A disconnected consumer cannot drain a full progress queue.
                # Never block session cleanup trying to publish its end marker.
                try:
                    queue.put_nowait(("end", None))
                except asyncio.QueueFull:
                    pass
            else:
                await queue.put(("end", None))

    task = asyncio.create_task(run())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if kind == "end":
                break
            yield frame(kind, payload)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
