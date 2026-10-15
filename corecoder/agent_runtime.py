"""One durable Runtime session for the production CoreCoder CLI Agent."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from .permissions import Permission
from .runtime import (
    ApprovalDecision,
    ApprovalRecord,
    RunRecord,
    RunStatus,
    RuntimeExecutor,
    RuntimeToolAdapter,
    SQLiteStore,
    ToolPolicyRegistry,
)
from .tools.base import Tool


@dataclass
class AgentRuntimeSession:
    store: SQLiteStore
    executor: RuntimeExecutor
    run_id: str
    permission: Permission
    # A Run admits one active ToolCall, including while approval is pending.
    _execution_gate: AbstractContextManager = field(default_factory=Lock, init=False, repr=False)

    @classmethod
    def open(cls, workspace: Path, model: str, permission: Permission) -> AgentRuntimeSession:
        workspace = workspace.resolve()
        runtime_dir = workspace / ".reliagent"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        store = SQLiteStore(runtime_dir / "agent-runtime.sqlite")
        store.initialize()
        run = store.create_run(
            goal="CoreCoder agent session",
            workflow="corecoder_agent",
            workspace=workspace,
            model=model,
            prompt_version="corecoder-agent-v1",
        )
        store.transition_run(run.id, RunStatus.RUNNING, "run.started")
        executor = RuntimeExecutor(store, ToolPolicyRegistry.with_builtin_defaults())
        return cls(store, executor, run.id, permission)

    def wrap_tools(self, tools: Sequence[Tool]) -> list[Tool]:
        return [
            RuntimeToolAdapter(
                tool, self.store, self.executor, self.run_id,
                approval_handler=self._decide_approval,
                execution_gate=self._execution_gate,
            )
            if tool.name in {"read_file", "write_file", "edit_file", "bash"} else tool
            for tool in tools
        ]

    def _decide_approval(self, approval: ApprovalRecord, arguments: dict) -> ApprovalDecision:
        refusal = self.permission.check(approval.tool_name, arguments)
        return ApprovalDecision.ALLOW_ONCE if refusal is None else ApprovalDecision.DENY

    def finish(self, status: RunStatus) -> RunRecord:
        run = self.store.get_run(self.run_id)
        if run.status in {
            RunStatus.RECOVERABLE,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run
        if status is RunStatus.CANCELLED:
            return self.executor.cancel_run(self.run_id)
        event = {RunStatus.SUCCEEDED: "run.completed", RunStatus.FAILED: "run.failed"}[status]
        return self.store.transition_run(self.run_id, status, event)


class DurableAgentSession(AgentRuntimeSession):
    """Explicit encrypted session; the real Agent supplies context, tools and LLM.

    Task 3 integrates Agent/CLI delegation. Operation handlers receive the bound
    invocation and cancellation event and may own separate linked child Runs.
    They must raise UnknownEffect when a child, hook or remote result is unknown.
    """

    @classmethod
    def open(cls, workspace, model, permission, *, key=None, cipher=None, run_id=None):
        from .runtime.agent_store import AgentJournal, RunLease
        from .runtime.checkpoints import CheckpointCipher

        if cipher is not None and key is not None:
            raise ValueError("provide a cipher or key, not both")
        cipher = cipher if cipher is not None else CheckpointCipher(key)
        workspace = Path(workspace).resolve()
        runtime_dir = workspace / ".reliagent"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        store = SQLiteStore(runtime_dir / "agent-runtime.sqlite")
        store.initialize()
        new = run_id is None
        if new:
            run = store.create_run(goal="CoreCoder agent session", workflow="corecoder_agent", workspace=workspace,
                                   model=model, prompt_version="corecoder-agent-durable-v1")
            store.transition_run(run.id, RunStatus.RUNNING, "run.started")
        else:
            run = store.get_run(run_id)
            if Path(run.workspace).resolve() != workspace or run.model != model:
                raise ValueError("Run workspace or model metadata mismatch")
        policies = ToolPolicyRegistry.with_builtin_defaults()
        session = cls(store, RuntimeExecutor(store, policies), run.id, permission)
        session.journal = AgentJournal(store, cipher)
        session.policies = policies
        session._lease = RunLease(workspace, run.id)
        session._owned = False
        session._new = new
        session._agent = None
        session._cwd = str(workspace)
        session.operation_handlers = {}
        session.before_execution = None
        session.after_result = None
        session.before_inference = None
        return session

    def _require_owner(self, agent=None):
        import os

        if not self._owned or self._lease._pid != os.getpid():
            raise ValueError("durable session does not own its Run")
        if agent is not None and agent is not self._agent:
            raise ValueError("Agent is not attached to this durable session")
        if self.store.get_run(self.run_id).status is RunStatus.CANCELLED:
            raise ValueError("cancelled Run is history-only")
        if self._agent is not None:
            state = self.journal.load(self.run_id)["state"]
            parent_run = state.get("parent_run_id")
            if parent_run and self.store.get_run(parent_run).status is RunStatus.CANCELLED:
                raise ValueError("parent Run cancelled; linked operation admission closed")
        if self._agent is not None and self.journal.load(self.run_id)["state"].get("metadata") != self._metadata(self._agent):
            raise ValueError("checkpoint model/tool/policy metadata signature mismatch")

    def _metadata(self, agent):
        import json
        from urllib.parse import urlsplit, urlunsplit

        from .llm import LiteLLM

        def endpoint(value):
            parts = urlsplit(str(value or ""))
            return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]))

        credential_options = {
            "api_key", "api_token", "azure_ad_token", "azure_ad_token_provider",
            "aws_access_key_id", "aws_secret_access_key", "aws_session_token", "vertex_credentials",
        }
        credential_headers = {"authorization", "proxy-authorization", "api-key", "x-api-key", "x-goog-api-key", "cookie"}
        options = {key: value for key, value in getattr(agent.llm, "extra", {}).items() if key not in credential_options}
        if isinstance(options.get("extra_body"), dict):
            options["extra_body"] = {key: value for key, value in options["extra_body"].items()
                                     if key not in credential_options}
        for header_option in ("extra_headers", "headers", "default_headers"):
            if isinstance(options.get(header_option), dict):
                options[header_option] = {key: value for key, value in options[header_option].items()
                                          if key.lower() not in credential_headers}
        for endpoint_option in ("base_url", "api_base", "azure_endpoint"):
            if endpoint_option in options:
                options[endpoint_option] = endpoint(options[endpoint_option])
        client = getattr(agent.llm, "client", None)
        is_litellm = isinstance(agent.llm, LiteLLM)
        base_url = (getattr(agent.llm, "base_url", None) or options.get("api_base")) if is_litellm else getattr(client, "base_url", "")
        metadata = {
            "schema_version": 1, "model": agent.llm.model,
            "backend": "litellm" if is_litellm else "openai-compatible",
            "base_url": endpoint(base_url),
            "model_options": options,
            "system": agent._system, "tools": agent._tool_schemas(),
            "policies": self.policies.signature([tool.name for tool in agent.tools] + ["__unknown__"]),
            "max_context_tokens": agent.context.max_tokens,
        }
        from .durable_operations import configuration_fingerprint

        metadata["operations"] = configuration_fingerprint(agent)
        return json.loads(json.dumps(metadata, sort_keys=True))

    def attach(self, agent):
        from copy import deepcopy

        if self._agent is agent:
            self._require_owner(agent)
            return self
        if self._agent is not None:
            raise ValueError("session already has an attached Agent")
        self._lease.acquire()
        self._owned = True
        try:
            self._require_owner()
            if agent.llm.model != self.store.get_run(self.run_id).model:
                raise ValueError("model metadata mismatch")
            if self._new:
                self.journal.create(self.run_id, {
                    "metadata": self._metadata(agent), "messages": deepcopy(agent.messages),
                    "effective_messages": deepcopy(agent.messages), "effective_count": len(agent.messages),
                    "todo": deepcopy(agent._todo._tasks) if agent._todo is not None else [],
                    "cwd": self._cwd, "plan_mode": agent.plan_mode,
                    "parent_run_id": getattr(self, "parent_run_id", None),
                })
                self._new = False
            state = self.journal.load(self.run_id)["state"]
            if state.get("metadata") != self._metadata(agent):
                raise ValueError("checkpoint model/tool/policy metadata signature mismatch")
            # Authenticate every original argument/result before dispatch is possible.
            self.journal.list_invocations(self.run_id)
            self._agent = agent
            self._sync(agent)
        except BaseException:
            self._agent = None
            self.close()
            raise
        return self

    def close(self):
        self._lease.release()
        self._owned = False

    def _sync(self, agent):
        from copy import deepcopy

        state = self.journal.load(self.run_id)["state"]
        raw = state["messages"]
        agent.messages = deepcopy(state["effective_messages"] + raw[state["effective_count"]:])
        agent.plan_mode = state["plan_mode"]
        self._cwd = state["cwd"]
        if agent._todo is not None:
            agent._todo._tasks = deepcopy(state["todo"])
        return state

    def _state(self, agent):
        from copy import deepcopy

        state = self.journal.load(self.run_id)["state"]
        state.update(effective_messages=deepcopy(agent.messages), effective_count=len(state["messages"]),
                     todo=deepcopy(agent._todo._tasks) if agent._todo is not None else [],
                     cwd=self._cwd, plan_mode=agent.plan_mode)
        return state

    def ready(self, agent=None):
        self._require_owner(agent)
        checkpoint = self.journal.load(self.run_id)
        self._check_pending(checkpoint)
        if self.before_inference is not None:
            self.before_inference(self._agent)
        if checkpoint["phase"] not in {"ready", "complete"}:
            raise ValueError("unfinished or unknown work blocks new input/reset/export")
        if self.store.get_run(self.run_id).status is not RunStatus.RUNNING:
            raise ValueError("Run cannot accept new work")
        return checkpoint

    def reset(self, agent):
        self.ready(agent)
        agent.messages.clear()
        self.journal.save_state(self.run_id, self._state(agent))

    def export(self):
        self.ready()
        run = self.store.get_run(self.run_id)
        return {"schema_version": 1, "run_id": run.id, "workspace": run.workspace,
                "model": run.model, "durable": True}

    def chat(self, agent, user_input, on_token=None, on_tool=None):
        self.ready(agent)
        state = self._state(agent)
        message = {"role": "user", "content": user_input}
        state["messages"].append(message)
        agent.messages.append(message)
        state["effective_messages"] = agent.messages
        state["effective_count"] = len(state["messages"])
        self.journal.save_state(self.run_id, state)
        return self._loop(agent, on_token, on_tool)

    def resume(self, agent, on_token=None, on_tool=None):
        self._require_owner(agent)
        self._sync(agent)
        checkpoint = self.journal.load(self.run_id)
        if checkpoint["phase"] == "complete":
            return agent.messages[-1].get("content") or ""
        return self._loop(agent, on_token, on_tool)

    def _check_pending(self, checkpoint):
        from .runtime.state import ToolCallStatus

        if checkpoint["phase"] in {"llm_inflight", "blocked"}:
            raise ValueError("unknown inference/tool outcome blocks execution; explicit reconciliation required")
        all_invocations = self.journal.list_invocations(self.run_id)
        invocations = [item for item in all_invocations if item["turn_sequence"] == checkpoint["turn_sequence"]]
        if any(item["state"] != "completed" for item in all_invocations
               if checkpoint["phase"] != "dispatching" or item["turn_sequence"] != checkpoint["turn_sequence"]):
            raise ValueError("unfinished invocation blocks inference")
        # Validate the entire batch before starting even its first unstarted call.
        for invocation in invocations:
            if invocation["state"] == "completed":
                continue
            attempt = invocation["attempt_id"]
            if invocation["state"] == "blocked" or (attempt and self.store.get_tool_call(attempt).status not in {
                ToolCallStatus.CREATED, ToolCallStatus.WAITING_APPROVAL,
            }):
                if attempt and self.store.get_tool_call(attempt).status is ToolCallStatus.RUNNING:
                    self.store.transition_tool_call(attempt, ToolCallStatus.INTERRUPTED, "tool.interrupted",
                                                    payload={"reason": "restored started attempt has no result"})
                else:
                    self.journal.block(self.run_id, "tool outcome is unknown or full result is missing")
                raise ValueError("unknown tool outcome blocks execution")
        return invocations

    def _loop(self, agent, on_token, on_tool):
        rounds = 0
        while True:
            self._require_owner(agent)
            checkpoint = self.journal.load(self.run_id)
            invocations = self._check_pending(checkpoint)
            if checkpoint["phase"] == "dispatching":
                for invocation in invocations:
                    if invocation["state"] != "completed":
                        self.dispatch(agent, invocation, on_tool=on_tool)
                    self._sync(agent)
                self.journal.complete_turn(self.run_id, self._state(agent))
                if not invocations:
                    return agent.messages[-1].get("content") or ""
            if rounds >= agent.max_rounds:
                return "(reached maximum tool-call rounds)"
            self.ready(agent)
            # Deterministic compression preserves the raw append-only journal.
            # It intentionally uses extraction rather than hidden summary inference.
            agent.context.maybe_compress(agent.messages, None)
            self.journal.save_state(self.run_id, self._state(agent))
            request = {"messages": agent._full_messages(), "tools": agent._tool_schemas()}
            sequence = self.journal.begin_turn(self.run_id, request)
            rounds += 1
            from .llm import durable_model_request

            with durable_model_request():
                response = agent.llm.chat(**request, on_token=on_token)
            self.journal.record_response(self.run_id, sequence, response.message)
            self._sync(agent)
            # A final response commits its turn before returning to the caller.
            if not response.tool_calls:
                self.journal.complete_turn(self.run_id, self._state(agent))
                return response.content

    def dispatch(self, agent, invocation, *, on_tool=None):
        import inspect
        import json
        import os

        from .runtime.executor import UnknownEffect
        from .runtime.policies import FailureKind
        from .runtime.state import ExecutionKind
        from .tools.bash import _check_dangerous, _update_cwd

        self._require_owner(agent)
        self._check_pending(self.journal.load(self.run_id))
        if self.before_inference is not None:
            self.before_inference(agent)
        name = invocation["tool_name"]
        raw = invocation["arguments"]
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be an object")
        except (ValueError, TypeError) as error:
            self.journal.complete_without_attempt(invocation["id"], f"Error: bad arguments for {name}: {error}")
            return
        tool = agent._tool_by_name.get(name)
        refusal = None
        if tool is None:
            refusal = f"Error: unknown tool '{name}'"
        elif agent.plan_mode and name not in Permission.READ_ONLY:
            refusal = "Plan mode is read-only; this call was refused. Do not retry it."
        else:
            try:
                inspect.signature(tool.execute).bind(**arguments)
            except TypeError as error:
                refusal = f"Error: bad arguments for {name}: {error}"
        if refusal is not None:
            self.journal.complete_without_attempt(invocation["id"], refusal)
            return
        if on_tool:
            on_tool(name, arguments)
        policy = self.policies.resolve(name)
        operation_arguments = None
        timeout = None
        if policy.execution_kind is ExecutionKind.SUBPROCESS:
            command = arguments.get("command")
            timeout = arguments.get("timeout", 120)
            if not isinstance(command, str) or type(timeout) is not int or not 0 < timeout <= policy.timeout_seconds:
                self.journal.complete_without_attempt(invocation["id"], "Error: invalid bash command or timeout")
                return
            warning = _check_dangerous(command)
            if warning:
                self.journal.complete_without_attempt(invocation["id"], f"Blocked: {warning}")
                return
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", command] if os.name == "nt" else ["/bin/sh", "-c", command]
            operation_arguments = {"argv": argv, "cwd": self._cwd}
            if "input" in arguments:
                operation_arguments["stdin"] = arguments["input"].encode("utf-8")

        def operation(args, cancellation):
            handler = self.operation_handlers.get(name)
            if handler is not None:
                bound = next(item for item in self.journal.list_invocations(self.run_id) if item["id"] == invocation["id"])
                output = handler(agent, bound, args, cancellation)
            else:
                if name == "agent":
                    raise UnknownEffect("durable child/hook operation handler is required")
                output = tool.execute(**args)
            if name in {"read_file", "write_file", "edit_file", "glob", "grep", "todo_write", "_undo"} and output.startswith(("Error:", "Invalid regex:")):
                raise RuntimeError(output)
            return output

        pre_refusal = None

        def finalize(output, failure_kind, exit_code):
            if pre_refusal is not None:
                return pre_refusal, {"todo": agent._todo._tasks if agent._todo is not None else [], "cwd": self._cwd}
            if (policy.execution_kind is ExecutionKind.IN_PROCESS and failure_kind is FailureKind.EXECUTION_ERROR
                    and not output.startswith(("Error:", "Invalid regex:"))):
                output = f"Error executing {name}: {output}"
            if policy.execution_kind is ExecutionKind.SUBPROCESS:
                if failure_kind is FailureKind.TIMED_OUT:
                    output = f"Error: timed out after {timeout}s"
                elif failure_kind is FailureKind.CANCELLED:
                    output += "\n[cancelled]"
                elif failure_kind not in {None, FailureKind.NONZERO_EXIT}:
                    output = f"Error running command: {output}"
                elif exit_code != 0:
                    output += f"\n[exit code: {exit_code}]"
                else:
                    self._cwd = _update_cwd(arguments["command"], self._cwd)
                output = output.strip() or "(no output)"
            return output, {"todo": agent._todo._tasks if agent._todo is not None else [], "cwd": self._cwd}

        def before_execute(cancellation):
            nonlocal pre_refusal
            if self.before_execution is not None:
                bound = next(item for item in self.journal.list_invocations(self.run_id) if item["id"] == invocation["id"])
                pre_refusal = self.before_execution(agent, bound, cancellation)
                return pre_refusal
            if agent.hooks is not None:
                raise UnknownEffect("durable hook operation handler is required")
            return None

        result = self.executor.execute_agent_invocation(
            self.journal, invocation, arguments, operation=operation, operation_arguments=operation_arguments,
            timeout_seconds=timeout, decide_approval=lambda approval, args: self.permission.check(name, args),
            finalize=finalize, before_execute=before_execute,
        )
        if self.after_result is not None:
            bound = next(item for item in self.journal.list_invocations(self.run_id) if item["id"] == invocation["id"])
            self.after_result(agent, bound, result.output)
        self._check_pending(self.journal.load(self.run_id))

    def finish(self, status):
        """Finalization cannot discard unfinished inference or unknown effects."""
        try:
            run = self.store.get_run(self.run_id)
            if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
                return run
            self._require_owner()
            if status is RunStatus.CANCELLED:
                return self.executor.cancel_run(self.run_id)
            checkpoint = self.journal.load(self.run_id)
            unfinished = any(item["state"] != "completed" for item in self.journal.list_invocations(self.run_id))
            if status is not RunStatus.CANCELLED and (
                checkpoint["phase"] not in {"ready", "complete"} or unfinished
            ):
                return self.store.get_run(self.run_id)
            if self.before_inference is not None:
                try:
                    self.before_inference(self._agent)
                except Exception:  # noqa: BLE001 - retain unresolved linked operations for recovery
                    # A committed main tool does not settle an unknown linked hook.
                    return self.store.get_run(self.run_id)
            return super().finish(status)
        finally:
            self.close()
