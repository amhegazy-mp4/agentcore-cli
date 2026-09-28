{{#if isExportHarness}}
import threading
{{/if}}
import time
from typing import Optional

{{#if isExportHarness}}
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent
{{else}}
from strands.hooks import BeforeModelCallEvent
{{/if}}
from strands.hooks.registry import HookProvider, HookRegistry
from strands.types.exceptions import EventLoopException


class ExecutionLimitExceeded(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


{{#if isExportHarness}}
class InvocationBudget:
    """One invocation-wide budget shared by the root Agent and its subagents."""

    def __init__(
        self,
        max_iterations: Optional[int] = None,
        max_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._iteration_count = 0
            self._start_time: float | None = None
            self._output_tokens = 0

    def check_before_model_call(self) -> None:
        with self._lock:
            self._iteration_count += 1
            now = time.monotonic()
            if self._start_time is None:
                self._start_time = now

            if self._max_iterations is not None and self._iteration_count > self._max_iterations:
                raise EventLoopException(
                    ExecutionLimitExceeded(f"Max iterations exceeded: {self._max_iterations}")
                )

            if self._timeout_seconds is not None:
                elapsed = now - self._start_time
                if elapsed > self._timeout_seconds:
                    raise EventLoopException(
                        ExecutionLimitExceeded(
                            f"Timeout exceeded: {self._timeout_seconds}s (elapsed {elapsed:.1f}s)"
                        )
                    )

            if self._max_tokens is not None and self._output_tokens >= self._max_tokens:
                raise EventLoopException(
                    ExecutionLimitExceeded(
                        f"Max output tokens exceeded: {self._output_tokens}/{self._max_tokens}"
                    )
                )

    def record_model_call(self, event: AfterModelCallEvent) -> None:
        response = event.stop_response
        if response is None:
            return
        metadata = response.message.get("metadata") or {}
        usage = metadata.get("usage")
        if not isinstance(usage, dict):
            return
        output_tokens = usage.get("outputTokens")
        if isinstance(output_tokens, (int, float)):
            with self._lock:
                self._output_tokens += int(output_tokens)


class ExecutionLimitsHook(HookProvider):
    def __init__(
        self,
        max_iterations: Optional[int] = None,
        max_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        *,
        budget: Optional[InvocationBudget] = None,
    ) -> None:
        self._budget = budget or InvocationBudget(
            max_iterations=max_iterations,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self._check_limits)
        registry.add_callback(AfterModelCallEvent, self._record_usage)

    def fork(self) -> "ExecutionLimitsHook":
        """Create a child hook backed by the same invocation-wide budget."""

        return ExecutionLimitsHook(budget=self._budget)

    def start_invocation(self) -> None:
        """Reset counters immediately before a new root invocation starts."""

        self._budget.reset()

    def _check_limits(self, event: BeforeModelCallEvent) -> None:
        self._budget.check_before_model_call()

    def _record_usage(self, event: AfterModelCallEvent) -> None:
        self._budget.record_model_call(event)
{{else}}
class ExecutionLimitsHook(HookProvider):
    def __init__(
        self,
        max_iterations: Optional[int] = None,
        max_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds
        self._iteration_count = 0
        self._start_time = time.monotonic()

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self._check_limits)

    def _check_limits(self, event: BeforeModelCallEvent) -> None:
        self._iteration_count += 1

        if self._max_iterations is not None and self._iteration_count > self._max_iterations:
            raise EventLoopException(
                ExecutionLimitExceeded(f"Max iterations exceeded: {self._max_iterations}")
            )

        if self._timeout_seconds is not None:
            elapsed = time.monotonic() - self._start_time
            if elapsed > self._timeout_seconds:
                raise EventLoopException(
                    ExecutionLimitExceeded(
                        f"Timeout exceeded: {self._timeout_seconds}s (elapsed {elapsed:.1f}s)"
                    )
                )

        if self._max_tokens is not None:
            used = event.agent.event_loop_metrics.accumulated_usage.get("outputTokens", 0)
            if used >= self._max_tokens:
                raise EventLoopException(
                    ExecutionLimitExceeded(
                        f"Max output tokens exceeded: {used}/{self._max_tokens}"
                    )
                )
{{/if}}
