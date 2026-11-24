"""Asynchronous events over NATS JetStream.

One stream, ``TASKFLOW``, captures every subject under ``taskflow.>``.
Producers publish ``taskflow.<entity>.<action>`` (e.g.
``taskflow.task.created``). Each consuming service reads through its own
*durable pull consumer*: the stream remembers how far that service got, so
a restarted or rescheduled Pod resumes where it stopped, and replicas of the
same service share one consumer and split the work between them. That is
what lets notification-service scale out on its backlog (``pending``).

The W3C trace context is injected into the message headers on publish and
extracted on consume, so a trace continues across the asynchronous hop.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

import nats
from nats.js.api import AckPolicy, ConsumerConfig, StreamConfig
from nats.js.errors import NotFoundError
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind

from . import config

log = logging.getLogger(__name__)

STREAM = "TASKFLOW"
Handler = Callable[[str, dict], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self.nc = None
        self.js = None
        self._tasks: list[asyncio.Task] = []
        self._tracer = trace.get_tracer(config.SERVICE_NAME)

    async def connect(self, attempts: int = 60) -> None:
        url = config.env("NATS_URL", "nats://nats:4222")
        for attempt in range(1, attempts + 1):
            try:
                self.nc = await nats.connect(
                    url, name=config.SERVICE_NAME, max_reconnect_attempts=-1
                )
                break
            except Exception as exc:  # noqa: BLE001 -- any connect failure is retried
                log.warning(
                    "nats not ready, retrying",
                    extra={"fields": {"attempt": attempt, "error": str(exc)}},
                )
                await asyncio.sleep(2)
        else:
            raise RuntimeError("nats unreachable")
        self.js = self.nc.jetstream()
        try:
            await self.js.stream_info(STREAM)
        except NotFoundError:
            await self.js.add_stream(
                StreamConfig(
                    name=STREAM,
                    subjects=["taskflow.>"],
                    max_age=7 * 24 * 3600,  # seconds; keep a week of events
                    num_replicas=1,
                )
            )
            log.info("created JetStream stream", extra={"fields": {"stream": STREAM}})

    async def publish(self, subject: str, data: dict) -> None:
        """Publish ``taskflow.<subject>`` with a standard envelope."""
        full = f"taskflow.{subject}"
        event = {
            "id": str(uuid.uuid4()),
            "type": subject,
            "source": config.SERVICE_NAME,
            "time": time.time(),
            "data": data,
        }
        with self._tracer.start_as_current_span(
            f"publish {full}", kind=SpanKind.PRODUCER,
            attributes={"messaging.system": "nats", "messaging.destination.name": full},
        ):
            headers: dict[str, str] = {}
            inject(headers)
            await self.js.publish(full, json.dumps(event).encode(), headers=headers)

    def consume(self, durable: str, subject: str, handler: Handler, batch: int = 10) -> None:
        """Start a background loop delivering ``subject`` to ``handler``."""
        self._tasks.append(asyncio.create_task(self._loop(durable, subject, handler, batch)))

    async def _loop(self, durable: str, subject: str, handler: Handler, batch: int) -> None:
        sub = await self.js.pull_subscribe(
            subject,
            durable=durable,
            stream=STREAM,
            config=ConsumerConfig(
                ack_policy=AckPolicy.EXPLICIT, ack_wait=30, max_deliver=10
            ),
        )
        log.info("consuming", extra={"fields": {"durable": durable, "subject": subject}})
        while True:
            try:
                msgs = await sub.fetch(batch, timeout=5)
            except (asyncio.TimeoutError, nats.errors.TimeoutError):
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- keep the loop alive across reconnects
                log.warning("fetch failed", extra={"fields": {"error": str(exc)}})
                await asyncio.sleep(2)
                continue
            for msg in msgs:
                ctx = extract(msg.headers or {})
                with self._tracer.start_as_current_span(
                    f"consume {msg.subject}", context=ctx, kind=SpanKind.CONSUMER,
                    attributes={"messaging.system": "nats",
                                "messaging.consumer.group.name": durable},
                ):
                    try:
                        event = json.loads(msg.data)
                        await handler(msg.subject.removeprefix("taskflow."), event)
                        await msg.ack()
                    except Exception:  # noqa: BLE001 -- redeliver later, never drop silently
                        log.exception("event handler failed",
                                      extra={"fields": {"subject": msg.subject}})
                        await msg.nak(delay=5)

    async def pending(self, durable: str) -> int:
        """Messages not yet acknowledged by a durable consumer (the backlog)."""
        info = await self.js.consumer_info(STREAM, durable)
        return int(info.num_pending) + int(info.num_ack_pending)

    async def check(self) -> None:
        if not self.nc or not self.nc.is_connected:
            raise RuntimeError("nats disconnected")

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.nc:
            await self.nc.drain()
