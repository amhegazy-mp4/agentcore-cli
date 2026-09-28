"""Standalone construction helpers matching the managed AgentCore Harness runtime."""

from __future__ import annotations

import hashlib
import logging
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from strands import Agent, AgentSkills
from strands.storage import LocalFileStorage
from strands.tools import ToolProvider
from strands.tools.executors import SequentialToolExecutor
from strands.tools.tools import AgentTool
from strands.types.exceptions import StorageError
from strands.types.tools import ToolGenerator, ToolSpec, ToolUse
from strands.vended_plugins.context_offloader import ContextOffloader
from strands.vended_tools import make_shell, make_web_fetch
from strands_harness import create_harness
from strands_harness.tools import AgentSpec, Choice, Preset, edit, make_subagent, read, write

logger = logging.getLogger(__name__)

_BEDROCK_TOOL_NAME_LIMIT = 64
_INHERITABLE_TOOL_TYPES = {"builtin", "remote_mcp", "agentcore_gateway"}
_OFFLOAD_ROOT = "/tmp/loopy/offloaded"
_OFFLOAD_USAGE_ROOT = "/tmp/loopy/offload-usage"
_MAX_OFFLOAD_BYTES = 8 * 1024 * 1024 * 1024
_USAGE_KEY = "bytes"

_GENERALIST = Preset(
    instructions=(
        "You are a general-purpose subagent handling a focused subtask on behalf of a parent "
        "agent. You run in your own fresh conversation and cannot ask follow-up questions, so "
        "work from the task as given, make reasonable assumptions where it is underspecified, "
        "and see it through to a verified result. Return a self-contained answer: state what you "
        "did, what you found, and anything the parent needs to act on. Your final message is the "
        "only thing that returns to the parent, so put the substance there rather than in "
        "intermediate steps."
    ),
    description="a general-purpose agent for a focused subtask that runs in its own context",
)


@dataclass(frozen=True)
class ManagedToolEntry:
    """A tool plus the Harness namespace and lifecycle category that produced it."""

    namespace: str
    tool_type: str
    tool: Any


class AliasedAgentTool(AgentTool):
    """Expose a collision-safe model-facing name without mutating the original tool."""

    def __init__(self, tool: AgentTool, name: str) -> None:
        super().__init__()
        self._tool = tool
        self._name = name

    @property
    def original_tool(self) -> AgentTool:
        return self._tool

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return cast(ToolSpec, {**self._tool.tool_spec, "name": self._name})

    @property
    def tool_type(self) -> str:
        return self._tool.tool_type

    @property
    def supports_hot_reload(self) -> bool:
        return self._tool.supports_hot_reload

    @property
    def is_dynamic(self) -> bool:
        return self._tool.is_dynamic

    def mark_dynamic(self) -> None:
        self._tool.mark_dynamic()

    def get_display_properties(self) -> dict[str, str]:
        properties = dict(self._tool.get_display_properties())
        properties["Name"] = self._name
        return properties

    def stream(
        self,
        tool_use: ToolUse,
        invocation_state: dict[str, Any],
        **kwargs: Any,
    ) -> ToolGenerator:
        return self._tool.stream(tool_use, invocation_state, **kwargs)


class SessionByteBudgetStorage:
    """Session-scoped storage with the managed Harness's monotonic byte budget."""

    def __init__(
        self,
        data_storage: LocalFileStorage,
        usage_storage: LocalFileStorage,
        *,
        max_bytes: int,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._data_storage = data_storage
        self._usage_storage = usage_storage
        self._max_bytes = max_bytes

    def for_sandbox(self, sandbox: Any) -> "SessionByteBudgetStorage":
        return SessionByteBudgetStorage(
            self._data_storage.for_sandbox(sandbox),
            self._usage_storage,
            max_bytes=self._max_bytes,
        )

    async def write(self, key: str, data: bytes) -> None:
        used_bytes = await self._read_usage()
        next_used_bytes = used_bytes + len(data)
        if next_used_bytes > self._max_bytes:
            raise StorageError(f"Context offload byte limit reached ({self._max_bytes})")

        await self._write_usage(next_used_bytes)
        try:
            await self._data_storage.write(key, data)
        except BaseException:
            try:
                await self._write_usage(used_bytes)
            except Exception:
                logger.exception("Failed to roll back context offload byte reservation")
            raise

    async def read(self, key: str) -> bytes | None:
        return await self._data_storage.read(key)

    async def delete(self, key: str) -> None:
        await self._data_storage.delete(key)

    async def list(self, query: str = "") -> list[str]:
        return await self._data_storage.list(query)

    async def search(self, query: str) -> list[Any]:
        return await self._data_storage.search(query)

    async def _read_usage(self) -> int:
        raw = await self._usage_storage.read(_USAGE_KEY)
        if raw is None:
            return 0
        try:
            used_bytes = int(raw)
        except (TypeError, ValueError) as error:
            raise StorageError("Context offload byte counter is invalid") from error
        if used_bytes < 0:
            raise StorageError("Context offload byte counter is invalid")
        return used_bytes

    async def _write_usage(self, used_bytes: int) -> None:
        await self._usage_storage.write(_USAGE_KEY, str(used_bytes).encode("ascii"))


def create_context_offloader(session_id: str) -> ContextOffloader:
    """Create an offloader that survives Agent rebuilds while remaining session-isolated."""

    session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    storage = SessionByteBudgetStorage(
        LocalFileStorage(os.path.join(_OFFLOAD_ROOT, session_key)),
        LocalFileStorage(os.path.join(_OFFLOAD_USAGE_ROOT, session_key)),
        max_bytes=_MAX_OFFLOAD_BYTES,
    )
    return ContextOffloader(
        storage=storage,
        max_result_tokens=1500,
        preview_tokens=750,
        include_retrieval_tool=True,
        evict_after_cycles=None,
    )


def build_managed_builtin_tools(enabled_tools: Sequence[str]) -> list[ManagedToolEntry]:
    """Build the frozen managed tool set selected by the Harness allowedTools policy."""

    enabled = set(enabled_tools)
    candidates = [
        ("shell", make_shell()),
        ("read", read),
        ("write", write),
        ("edit", edit),
        ("web_fetch", make_web_fetch(mode="markdown")),
    ]
    return [ManagedToolEntry("builtin", "builtin", tool) for name, tool in candidates if name in enabled]


def _registry_name(name: str) -> str:
    return name.replace("-", "_")


def _name_with_hash_suffix(name: str, identity: str) -> str:
    suffix = f"_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:8]}"
    return f"{name[:_BEDROCK_TOOL_NAME_LIMIT - len(suffix)]}{suffix}"


def _allocate_tool_names(entries: Sequence[ManagedToolEntry], reserved_names: set[str]) -> list[str]:
    original_names = [entry.tool.tool_name for entry in entries]
    original_counts = Counter(_registry_name(name) for name in original_names)
    original_counts.update(_registry_name(name) for name in reserved_names)

    candidates = [
        (
            f"{entry.namespace}_{original_name}"
            if original_counts[_registry_name(original_name)] > 1
            else original_name
        )[:_BEDROCK_TOOL_NAME_LIMIT]
        for entry, original_name in zip(entries, original_names, strict=True)
    ]

    candidate_counts = Counter(_registry_name(name) for name in candidates)
    candidate_counts.update(_registry_name(name) for name in reserved_names)
    used_names = {_registry_name(name) for name in reserved_names}
    allocated_names: list[str] = []

    for index, (entry, original_name, candidate) in enumerate(
        zip(entries, original_names, candidates, strict=True)
    ):
        if candidate_counts[_registry_name(candidate)] > 1:
            candidate = _name_with_hash_suffix(
                candidate,
                f"{entry.namespace}/{original_name}/{index}",
            )
        attempt = 0
        while _registry_name(candidate) in used_names:
            candidate = _name_with_hash_suffix(
                candidate,
                f"{entry.namespace}/{original_name}/{index}/{attempt}",
            )
            attempt += 1
        used_names.add(_registry_name(candidate))
        allocated_names.append(candidate)

    return allocated_names


def _plugin_tool_names(plugins: Sequence[Any]) -> set[str]:
    return {
        tool.tool_name
        for plugin in plugins
        for tool in (getattr(plugin, "tools", None) or ())
        if hasattr(tool, "tool_name")
    }


def _fork_hooks(hooks: Sequence[Any]) -> list[Any]:
    return [hook.fork() if callable(getattr(hook, "fork", None)) else hook for hook in hooks]


class ManagedToolProvider(ToolProvider):
    """Resolve providers, allocate collision-safe names, and add the managed subagent."""

    def __init__(
        self,
        entries: Sequence[ManagedToolEntry],
        *,
        model: Any,
        system_prompt: str,
        conversation_manager_factory: Any,
        hooks: Sequence[Any] = (),
        consumer_plugins: Sequence[Any] = (),
        skill_paths: Sequence[str] = (),
        session_id: str,
        enable_todos: bool,
        enable_context_offloader: bool,
        enable_subagent: bool,
    ) -> None:
        self._entries = list(entries)
        self._model = model
        self._system_prompt = system_prompt
        self._conversation_manager_factory = conversation_manager_factory
        self._hooks = list(hooks)
        self._consumer_plugins = list(consumer_plugins)
        self._skill_paths = list(skill_paths)
        self._session_id = session_id
        self._enable_todos = enable_todos
        self._enable_context_offloader = enable_context_offloader
        self._enable_subagent = enable_subagent
        self._consumers: set[Any] = set()
        self._resolved_tools: list[AgentTool] | None = None
        self.inline_function_names: set[str] = set()

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        if consumer_id in self._consumers:
            return
        self._consumers.add(consumer_id)
        for index, entry in enumerate(self._entries):
            if isinstance(entry.tool, ToolProvider):
                entry.tool.add_consumer((consumer_id, index))

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        if consumer_id not in self._consumers:
            return
        self._consumers.remove(consumer_id)
        for index, entry in enumerate(self._entries):
            if isinstance(entry.tool, ToolProvider):
                entry.tool.remove_consumer((consumer_id, index))

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        if self._resolved_tools is not None:
            return self._resolved_tools

        resolved_entries: list[ManagedToolEntry] = []
        for entry in self._entries:
            if isinstance(entry.tool, ToolProvider):
                loaded_tools = await entry.tool.load_tools()
                resolved_entries.extend(
                    ManagedToolEntry(entry.namespace, entry.tool_type, tool)
                    for tool in loaded_tools
                )
            elif isinstance(entry.tool, AgentTool):
                resolved_entries.append(entry)

        reserved_names = _plugin_tool_names(self._consumer_plugins)
        if self._skill_paths:
            reserved_names.add("skills")
        if self._enable_todos:
            reserved_names.add("todo_write")
        if self._enable_context_offloader:
            reserved_names.add("retrieve_offloaded_content")
        if self._enable_subagent:
            reserved_names.add("subagent")

        allocated_names = _allocate_tool_names(resolved_entries, reserved_names)
        assigned_entries: list[ManagedToolEntry] = []
        for entry, assigned_name in zip(resolved_entries, allocated_names, strict=True):
            tool = entry.tool
            if assigned_name != tool.tool_name:
                tool = AliasedAgentTool(tool, assigned_name)
            assigned_entries.append(ManagedToolEntry(entry.namespace, entry.tool_type, tool))

        self.inline_function_names = {
            entry.tool.tool_name
            for entry in assigned_entries
            if entry.tool_type == "inline_function"
        }

        resolved_tools = [entry.tool for entry in assigned_entries]
        if self._enable_subagent:
            resolved_tools.append(self._build_subagent(assigned_entries))
        self._resolved_tools = resolved_tools
        return resolved_tools

    def _build_subagent(self, entries: Sequence[ManagedToolEntry]) -> AgentTool:
        safe_tools = {
            entry.tool.tool_name: entry.tool
            for entry in entries
            if entry.tool_type in _INHERITABLE_TOOL_TYPES
        }

        plugin_tool_names: set[str] = set()
        if self._skill_paths:
            plugin_tool_names.add("skills")
        if self._enable_todos:
            plugin_tool_names.add("todo_write")
        if self._enable_context_offloader:
            plugin_tool_names.add("retrieve_offloaded_content")
        inherited_tool_names = [*safe_tools, *sorted(plugin_tool_names)]

        def build_child(spec: AgentSpec) -> Agent:
            selected_names = set(spec.tools if spec.tools is not None else inherited_tool_names)
            selected_tools = [
                tool
                for name, tool in safe_tools.items()
                if name in selected_names
            ]

            selected_plugins: list[Any] = []
            if self._skill_paths and "skills" in selected_names:
                selected_plugins.append(AgentSkills(skills=self._skill_paths))
            if self._enable_context_offloader and "retrieve_offloaded_content" in selected_names:
                selected_plugins.append(create_context_offloader(self._session_id))

            return create_harness(
                model=spec.model or self._model,
                system_prompt=spec.instructions or self._system_prompt,
                tools=selected_tools,
                plugins=selected_plugins or None,
                hooks=_fork_hooks(self._hooks),
                conversation_manager=self._conversation_manager_factory(),
                tool_executor=SequentialToolExecutor(),
                callback_handler=None,
                builtin_tools=[],
                builtin_plugins=["todos"]
                if self._enable_todos and "todo_write" in selected_names
                else [],
                background_tasks=False,
                caching=False,
                context_manager=False,
                session=False,
                skills=False,
                memory=False,
            )

        return make_subagent(
            builder=build_child,
            presets={"generalist": _GENERALIST},
            inherited_tools=inherited_tool_names,
            context=Choice(["none", "all", "no_tools"]),
            name="subagent",
        )
