#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Nekokatt 2019-2020
#
# This file is part of Hikari.
#
# Hikari is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Hikari is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Hikari. If not, see <https://www.gnu.org/licenses/>.
"""Complex rate limiting mechanisms.

Provides an implementation for the complex rate limiting mechanisms that Discord
requires for rate limit handling that conforms to the passed bucket headers
correctly.

What is the theory behind this implementation?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In this module, we refer to a `hikari.net.routes.CompiledRoute` as a definition
of a route with specific major parameter values included (e.g.
`POST /channels/123/messages`), and a `hikari.net.routes.Route` as a
definition of a route without specific parameter values included (e.g.
`POST /channels/{channel_id}/messages`). We can compile a
`hikari.net.routes.CompiledRoute` from a `hikari.net.routes.Route`
by providing the corresponding parameters as kwargs, as you may already know.

In this module, a "bucket" is an internal data structure that tracks and
enforces the rate limit state for a specific `hikari.net.routes.CompiledRoute`,
and can manage delaying tasks in the event that we begin to get rate limited.
It also supports providing in-order execution of queued tasks.

Discord allocates types of buckets to routes. If you are making a request and
there is a valid rate limit on the route you hit, you should receive an
`X-RateLimit-Bucket` header from the server in your response. This is a hash
that identifies a route based on internal criteria that does not include major
parameters. This `X-RateLimitBucket` is known in this module as an "bucket hash".

This means that generally, the route `POST /channels/123/messages` and
`POST /channels/456/messages` will usually sit in the same bucket, but
`GET /channels/123/messages/789` and `PATCH /channels/123/messages/789` will
usually not share the same bucket. Discord may or may not change this at any
time, so hard coding this logic is not a useful thing to be doing.

Rate limits, on the other hand, apply to a bucket and are specific to the major
parameters of the compiled route. This means that `POST /channels/123/messages`
and `POST /channels/456/messages` do not share the same real bucket, despite
Discord providing the same bucket hash. A real bucket hash is the `str` hash of
the bucket that Discord sends us in a response concatenated to the corresponding
major parameters. This is used for quick bucket indexing internally in this
module.

One issue that occurs from this is that we cannot effectively hash a
`hikari.net.routes.CompiledRoute` that has not yet been hit, meaning that
until we receive a response from this endpoint, we have no idea what our rate
limits could be, nor the bucket that they sit in. This is usually not
problematic, as the first request to an endpoint should never be rate limited
unless you are hitting it from elsewhere in the same time window outside your
hikari.applications. To manage this situation, unknown endpoints are allocated to
a special unlimited bucket until they have an initial bucket hash code allocated
from a response. Once this happens, the route is reallocated a dedicated bucket.
Unknown buckets have a hardcoded initial hash code internally.

Initially acquiring time on a bucket
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each time you `BaseRateLimiter.acquire()` a request timeslice for a given
`hikari.net.routes.Route`, several things happen. The first is that we
attempt to find the existing bucket for that route, if there is one, or get an
unknown bucket otherwise. This is done by creating a real bucket hash from the
compiled route. The initial hash is calculated using a lookup table that maps
`hikari.net.routes.CompiledRoute` objects to their corresponding initial hash
codes, or to the unknown bucket hash code if not yet known. This initial hash is
processed by the `hikari.net.routes.CompiledRoute` to provide the real bucket
hash we need to get the route's bucket object internally.

The `acquire` method will take the bucket and acquire a new timeslice on
it. This takes the form of a `asyncio.Future` which should be awaited by
the caller and will complete once the caller is allowed to make a request. Most
of the time, this is done instantly, but if the bucket has an active rate limit
preventing requests being sent, then the future will be paused until the rate
limit is over. This may be longer than the rate limit period if you have queued
a large number of requests during this limit, as it is first-come-first-served.

Acquiring a rate limited bucket will start a bucket-wide task (if not already
running) that will wait until the rate limit has completed before allowing more
futures to complete. This is done while observing the rate limits again, so can
easily begin to re-ratelimit itself if needed. Once the task is complete, it
tidies itself up and disposes of itself. This task will complete once the queue
becomes empty.

The result of `RateLimiter.acquire()` is a tuple of a `asyncio.Future` to await
on which completes when you are allowed to proceed with making a request, and a
real bucket hash which should be stored temporarily. This will be explained in
the next section.

When you make your response, you should be sure to set the
`X-RateLimit-Precision` header to `millisecond` to ensure a much greater
accuracy against rounding errors for rate limits (reduces the error margin from
`1` second to `1` millisecond).

Handling the rate limit headers of a response
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Once you have received your response, you are expected to extract the values of
the vital rate limit headers manually and parse them to the correct data types.
These headers are:

* `Date`:
    the response date on the server. This should be parsed to a
    `datetime.datetime` using `email.utils.parsedate_to_datetime`.
* `X-RateLimit-Limit`:
    an `int` describing the max requests in the bucket from empty to being rate
    limited.
* `X-RateLimit-Remaining`:
    an `int` describing the remaining number of requests before rate limiting
    occurs in the current window.
* `X-RateLimit-Bucket`:
    a `str` containing the initial bucket hash.
* `X-RateLimit-Reset`:
    a `float` containing the number of seconds since
    1st January 1970 at 0:00:00 UTC at which the current ratelimit window
    resets. This should be parsed to a `datetime.datetime` using
    `datetime.datetime.fromtimestamp`, passing `datetime.timezone.utc`
    as `tz`.

Each of the above values should be passed to the `update_rate_limits` method to
ensure that the bucket you acquired time from is correctly updated should
Discord decide to alter their ratelimits on the fly without warning (including
timings and the bucket).

This method will manage creating new buckets as needed and resetting vital
information in each bucket you use.

Tidying up
~~~~~~~~~~

To prevent unused buckets cluttering up memory, each `BaseRateLimiter`
instance spins up a `asyncio.Task` that periodically locks the bucket list
(not threadsafe, only using the concept of asyncio not yielding in regular
functions) and disposes of any clearly stale buckets that are no longer needed.
These will be recreated again in the future if they are needed.

When shutting down an application, one must remember to `close()` the
`BaseRateLimiter` that has been used. This will ensure the garbage collection
task is stopped, and will also ensure any remaining futures in any bucket queues
have an `asyncio.CancelledError` set on them to prevent deadlocking ratelimited
calls that may be waiting to be unlocked.
"""

from __future__ import annotations

__all__ = [
    "BaseRateLimiter",
    "BurstRateLimiter",
    "ManualRateLimiter",
    "WindowedBurstRateLimiter",
    "ExponentialBackOff",
]

import abc
import asyncio
import logging
import random
import time
import typing

if typing.TYPE_CHECKING:
    from . import more_typing


UNKNOWN_HASH: typing.Final[str] = "UNKNOWN"
"""The hash used for an unknown bucket that has not yet been resolved."""


class BaseRateLimiter(abc.ABC):
    """Base for any asyncio-based rate limiter being used.

    Supports being used as a synchronous context manager.

    !!! warning
        Async context manager support is not supported and will not be supported.
    """

    __slots__ = ()

    @abc.abstractmethod
    def acquire(self) -> more_typing.Future[None]:
        """Acquire permission to perform a task that needs to have rate limit management enforced.

        Returns
        -------
        asyncio.Future
            A future that should be awaited. Once the future is complete, you
            can proceed to execute your rate-limited task.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Close the rate limiter, cancelling any internal tasks that are executing."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class BurstRateLimiter(BaseRateLimiter, abc.ABC):
    """Base implementation for a burst-based rate limiter.

    This provides an internal queue and throttling placeholder, as well as
    complete logic for safely aborting any pending tasks when being shut down.
    """

    __slots__ = ("name", "throttle_task", "queue", "logger", "_closed")

    name: typing.Final[str]
    """The name of the rate limiter."""

    throttle_task: typing.Optional[more_typing.Task[None]]
    """The throttling task, or `None` if it isn't running."""

    queue: typing.Final[typing.List[more_typing.Future[None]]]
    """The queue of any futures under a rate limit."""

    logger: typing.Final[logging.Logger]
    """The logger used by this rate limiter."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.throttle_task = None
        self.queue = []
        self.logger = logging.getLogger(f"hikari.net.{type(self).__qualname__}.{name}")
        self._closed = False

    @abc.abstractmethod
    def acquire(self) -> more_typing.Future[None]:
        """Acquire time on this rate limiter.

        The implementation should define this.

        Returns
        -------
        asyncio.Future
            A future that should be immediately awaited. Once the await
            completes, you are able to proceed with the operation that is
            under this rate limit.
        """

    def close(self) -> None:
        """Close the rate limiter, and shut down any pending tasks.

        Once this is invoked, you should not reuse this object.
        """
        if self._closed:
            return

        if self.throttle_task is not None:
            self.throttle_task.cancel()

        failed_tasks = 0
        while self.queue:
            failed_tasks += 1
            future = self.queue.pop(0)
            # Make the future complete with an exception
            future.cancel()

        if failed_tasks:
            self.logger.debug("%s rate limiter closed with %s pending tasks!", self.name, failed_tasks)
        else:
            self.logger.debug("%s rate limiter closed", self.name)
        self._closed = True

    @property
    def is_empty(self) -> bool:
        """Return `True` if no futures are on the queue being rate limited."""
        return len(self.queue) == 0


class ManualRateLimiter(BurstRateLimiter):
    """Rate limit handler for the global RESTSession rate limit.

    This is a non-preemptive rate limiting algorithm that will always return
    completed futures until `ManualRateLimiter.throttle` is invoked. Once this
    is invoked, any subsequent calls to `ManualRateLimiter.acquire` will return
    incomplete futures that will be enqueued to an internal queue. A task will
    be spun up to wait for a period of time given to the
    `ManualRateLimiter.throttle`. Once that has passed, the lock will begin to
    re-consume incomplete futures on the queue, completing them.

    Triggering a throttle when it is already set will cancel the current
    throttle task that is sleeping and replace it.

    This is used to enforce the global RESTSession rate limit that will occur
    "randomly" during RESTSession API interaction.

    Expect random occurrences.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("global")

    def acquire(self) -> more_typing.Future[None]:
        """Acquire time on this rate limiter.

        Returns
        -------
        asyncio.Future
            A future that should be immediately awaited. Once the await
            completes, you are able to proceed with the operation that is
            under this rate limit.
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        if self.throttle_task is not None:
            self.queue.append(future)
        else:
            future.set_result(None)
        return future

    def throttle(self, retry_after: float) -> None:
        """Perform the throttling rate limiter logic.

        Iterates repeatedly while the queue is not empty, adhering to any
        rate limits that occur in the mean time.

        Parameters
        ----------
        retry_after : float
            How long to sleep for before unlocking and releasing any futures
            in the queue.

        !!! note
            This will invoke `ManualRateLimiter.unlock_later` as a scheduled task
            in the future (it will not await it to finish).

            When the `ManualRateLimiter.unlock_later` coroutine function
            completes, it should be expected to set the `throttle_task` to
            `None`. This means you can check if throttling is occurring by
            checking if `throttle_task` is not `None`.

            If this is invoked while another throttle is in progress, that one
            is cancelled and a new one is started. This enables new rate limits
            to override existing ones.
        """
        if self.throttle_task is not None:
            self.throttle_task.cancel()

        loop = asyncio.get_running_loop()
        self.throttle_task = loop.create_task(self.unlock_later(retry_after))

    async def unlock_later(self, retry_after: float) -> None:
        """Sleeps for a while, then removes the lock.

        Parameters
        ----------
        retry_after : float
            How long to sleep for before unlocking and releasing any futures
            in the queue.

        !!! note
            You shouldn't need to invoke this directly. Call
            `ManualRateLimiter.throttle` instead.

            When the `ManualRateLimiter.unlock_later` coroutine function
            completes, it should be expected to set the `throttle_task` to
            `None`. This means you can check if throttling is occurring by
            checking if `throttle_task` is not `None`.
        """
        self.logger.warning("you are being globally rate limited for %ss", retry_after)
        await asyncio.sleep(retry_after)
        while self.queue:
            next_future = self.queue.pop(0)
            next_future.set_result(None)
        self.throttle_task = None


class WindowedBurstRateLimiter(BurstRateLimiter):
    """Windowed burst rate limiter.

    Rate limiter for rate limits that last fixed periods of time with a
    fixed number of times it can be used in that time frame.

    To use this, you should call WindowedBurstRateLimiter.aquire` and await the
    result immediately before performing your rate-limited task.

    If the rate limit has been hit, acquiring time will return an incomplete
    future that is placed on the internal queue. A throttle task is then spun up
    if not already running that will be expected to provide some implementation
    of backing off and sleeping for a given period of time until the limit has
    passed, and then proceed to consume futures from the queue while adhering
    to those rate limits.

    If the throttle task is already running, the acquired future will always be
    incomplete and enqueued regardless of whether the rate limit is actively
    reached or not.

    Acquiring a future from this limiter when no throttling task is running and
    when the rate limit is not reached will always result in the task invoking
    a drip and a completed future being returned.

    Dripping is left to the implementation of this class, but will be expected
    to provide some mechanism for updating the internal statistics to represent
    that a unit has been placed into the bucket.
    """

    __slots__ = ("reset_at", "remaining", "limit", "period")

    reset_at: float
    """The `time.perf_counter` that the limit window ends at."""

    remaining: int
    """The number of `WindowedBurstRateLimiter.acquire`'s left in this window
    before you will get rate limited.
    """

    period: float
    """How long the window lasts for from the start in seconds."""

    limit: int
    """The maximum number of `WindowedBurstRateLimiter.acquire`'s allowed in
    this time window.
    """

    def __init__(self, name: str, period: float, limit: int) -> None:
        super().__init__(name)
        self.reset_at = 0.0
        self.remaining = 0
        self.limit = limit
        self.period = period

    def acquire(self) -> more_typing.Future[None]:
        """Acquire time on this rate limiter.

        Returns
        -------
        asyncio.Future
            A future that should be immediately awaited. Once the await
            completes, you are able to proceed with the operation that is
            under this rate limit.
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        # If we are rate limited, delegate invoking this to the throttler and spin it up
        # if it hasn't started. Likewise, if the throttle task is still running, we should
        # delegate releasing the future to the throttler task so that we still process
        # first-come-first-serve
        if self.throttle_task is not None or self.is_rate_limited(time.perf_counter()):
            self.queue.append(future)
            if self.throttle_task is None:
                self.throttle_task = loop.create_task(self.throttle())
        else:
            self.drip()
            future.set_result(None)

        return future

    def get_time_until_reset(self, now: float) -> float:
        """Determine how long until the current rate limit is reset.

        Parameters
        ----------
        now : float
            The monotonic `time.perf_counter` timestamp.

        !!! warning
            Invoking this method will update the internal state if we were
            previously rate limited, but at the given time are no longer under
            that limit. This makes it imperative that you only pass the current
            timestamp to this function, and not past or future timestamps. The
            effects of doing the latter are undefined behaviour.

        Returns
        -------
        float
            The time left to sleep before the rate limit is reset. If no rate limit
            is in effect, then this will return `0.0` instead.
        """
        if not self.is_rate_limited(now):
            return 0.0
        return self.reset_at - now

    def is_rate_limited(self, now: float) -> bool:
        """Determine if we are under a rate limit at the given time.

        Parameters
        ----------
        now : float
            The monotonic `time.perf_counter` timestamp.

        Returns
        -------
        bool
            `True` if we are being rate limited. `False` if we are not.

        !!! warning
            Invoking this method will update the internal state if we were
            previously rate limited, but at the given time are no longer under
            that limit. This makes it imperative that you only pass the current
            timestamp to this function, and not past or future timestamps. The
            effects of doing the latter are undefined behaviour.
        """
        if self.reset_at <= now:
            self.remaining = self.limit
            self.reset_at = now + self.period
            return False

        return self.remaining <= 0

    def drip(self) -> None:
        """Decrements the remaining counter."""
        self.remaining -= 1

    async def throttle(self) -> None:
        """Perform the throttling rate limiter logic.

        Iterates repeatedly while the queue is not empty, adhering to any
        rate limits that occur in the mean time.

        !!! note
            You should usually not need to invoke this directly, but if you do,
            ensure to call it using `asyncio.create_task`, and store the
            task immediately in `throttle_task`.

            When this coroutine function completes, it will set the
            `throttle_task` to `None`. This means you can check if throttling
            is occurring by checking if `throttle_task` is not `None`.
        """
        self.logger.debug(
            "you are being rate limited on bucket %s, backing off for %ss",
            self.name,
            self.get_time_until_reset(time.perf_counter()),
        )

        while self.queue:
            sleep_for = self.get_time_until_reset(time.perf_counter())
            await asyncio.sleep(sleep_for)

            while self.remaining > 0 and self.queue:
                self.drip()
                self.queue.pop(0).set_result(None)

        self.throttle_task = None


class ExponentialBackOff:
    r"""Implementation of an asyncio-compatible exponential back-off algorithm with random jitter.

    .. math::

        t_{backoff} = b^{i} +  m \cdot \mathrm{rand}()

    Such that \(t_{backoff}\) is the backoff time, \(b\) is the base,
    \(i\) is the increment that increases by 1 for each invocation, and
    \(m\) is the jitter multiplier. \(\mathrm{rand}()\) returns a value in
    the range \([0,1]\).

    Parameters
    ----------
    base : float
        The base to use. Defaults to `2`.
    maximum : float, optional
        If not `None`, then this is the max value the backoff can be in a
        single iteration before an `asyncio.TimeoutError` is raised.
        Defaults to `64` seconds.
    jitter_multiplier : float
        The multiplier for the random jitter. Defaults to `1`.
        Set to `0` to disable jitter.
    initial_increment : int
        The initial increment to start at. Defaults to `0`.
    """

    __slots__ = ("base", "increment", "maximum", "jitter_multiplier")

    base: typing.Final[float]
    """The base to use. Defaults to 2."""

    increment: int
    """The current increment."""

    maximum: typing.Optional[float]
    """If not `None`, then this is the max value the backoff can be in a
    single iteration before an `asyncio.TimeoutError` is raised.
    """

    jitter_multiplier: typing.Final[float]
    """The multiplier for the random jitter.

    This defaults to `1`. Set to `0` to disable jitter.
    """

    def __init__(
        self,
        base: float = 2,
        maximum: typing.Optional[float] = 64,
        jitter_multiplier: float = 1,
        initial_increment: int = 0,
    ) -> None:
        self.base = base
        self.maximum = maximum
        self.increment = initial_increment
        self.jitter_multiplier = jitter_multiplier

    def __next__(self) -> float:
        """Get the next back off to sleep by."""
        value = self.base ** self.increment

        self.increment += 1

        if self.maximum is not None and value >= self.maximum:
            raise asyncio.TimeoutError()

        value += random.random() * self.jitter_multiplier  # nosec
        return value

    def __iter__(self) -> ExponentialBackOff:
        """Return this object, as it is an iterator."""
        return self

    def reset(self) -> None:
        """Reset the exponential back-off."""
        self.increment = 0