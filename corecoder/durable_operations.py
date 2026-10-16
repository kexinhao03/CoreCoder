"""Linked child agents and shell hooks for the production durable Agent."""

import base64
import hashlib
import json
import threading
from pathlib import Path

from .permissions import Permission
from .runtime.executor import UnknownEffect
from .runtime.state import InvalidTransition, RunStatus, ToolCallStatus
from .tools import builtin_tools
from .tools.base import Tool
from .tools.bash import BashTool


def child_result(result):
    if len(result) > 5000:
        result = result[:4500] + "\n... (sub-agent output truncated)"
    return "[Sub-agent completed]\n" + result


class HookTool(BashTool):
    def execute(self, command: str, timeout: int = 10, input: str = "") -> str:
        raise RuntimeError("hook execution requires the managed subprocess dispatcher")


class UndoTool(Tool):
    name = "_undo"

    def execute(self, snapshot_id: str, target: str, prior: str | None) -> str:
        path = Path(target)
        if prior is None:
            path.unlink(missing_ok=True)
            return f"Removed {target} (created this session)."
        path.write_bytes(base64.b64decode(prior, validate=True))
        return f"Restored {target}."


class DurableOperations:
    def __init__(self, session, agent):
        self.session = session
        self.agent = agent
        agent._tool_by_name["_undo"] = UndoTool()
        session.operation_handlers["agent"] = self.child
        session.operation_handlers["_undo"] = self.undo_operation
        for name in ("write_file", "edit_file"):
            session.operation_handlers[name] = self.file_operation
        session.before_execution = self.before
        session.after_result = self.after
        session.before_inference = self.gate

    def file_operation(self, agent, invocation, arguments, cancellation):
        from .checkpoints import capture_with

        self._admit(cancellation)
        def record(path, prior):
            encoded = base64.b64encode(prior).decode() if prior is not None else None
            self.session.journal.record_undo(invocation["id"], str(path), encoded)
        with capture_with(record):
            return agent._tool_by_name[invocation["tool_name"]].execute(**arguments)

    def undo_operation(self, agent, invocation, arguments, cancellation):
        self._admit(cancellation)
        snapshots = self.session.journal.load(self.session.run_id)["state"].get("undo", [])
        if arguments not in snapshots:
            raise ValueError("undo arguments do not match a persisted original snapshot")
        return agent._tool_by_name["_undo"].execute(**arguments)

    def undo(self):
        from .llm import LLMResponse, ToolCall

        session = self.session
        checkpoint = session.ready(self.agent)
        invocations = session.journal.list_invocations(session.run_id)
        completed = {item["id"] for item in invocations if item["state"] == "completed"}
        undone = set()
        for item in invocations:
            if item["tool_name"] == "_undo" and item["state"] == "completed" and item["result"].startswith(("Restored ", "Removed ")):
                arguments = json.loads(item["arguments"]) if isinstance(item["arguments"], str) else item["arguments"]
                undone.add(arguments["snapshot_id"])
        snapshot = next((item for item in reversed(checkpoint["state"].get("undo", []))
                         if item["snapshot_id"] in completed and item["snapshot_id"] not in undone), None)
        if snapshot is None:
            return "Nothing to undo."
        sequence = session.journal.begin_turn(session.run_id, {"source": "operator_undo"})
        calls = session.journal.record_response(session.run_id, sequence,
                    LLMResponse(tool_calls=[ToolCall("operator-undo", "_undo", snapshot)]).message)
        session.dispatch(self.agent, calls[0])
        session._sync(self.agent)
        session.journal.complete_turn(session.run_id, session._state(self.agent))
        return session.journal.list_invocations(session.run_id, sequence)[0]["result"]

    def _link(self, invocation, kind, slot=0, **details):
        return self.session.journal.link_operation(invocation["id"], kind, slot, details)

    def _open(self, link, permission):
        from .agent_runtime import DurableAgentSession

        parent = self.session
        self._admit()
        try:
            parent.store.get_run(link["run_id"])
        except KeyError:
            parent.store.create_run(run_id=link["run_id"], goal="Linked CoreCoder operation",
                                    workflow="corecoder_agent", workspace=parent.store.get_run(parent.run_id).workspace,
                                    model=self.agent.llm.model, prompt_version="corecoder-agent-durable-v1")
        run = parent.store.get_run(link["run_id"])
        if run.status is RunStatus.CREATED:
            parent.store.transition_run(run.id, RunStatus.RUNNING, "run.started")
        session = DurableAgentSession.open(Path(run.workspace), run.model, permission,
                                           cipher=parent.journal.cipher, run_id=run.id)
        # Only this persisted, authenticated parent relationship authorizes
        # initialization of a linked Run interrupted before its first checkpoint.
        with parent.store._connect() as connection:
            exists = connection.execute("SELECT 1 FROM agent_sessions WHERE run_id = ?", (run.id,)).fetchone()
        session._new = not exists
        session.parent_run_id = parent.run_id
        session._cwd = parent._cwd
        return session

    def _admit(self, cancellation=None):
        if (cancellation is not None and cancellation.is_set()) or self.session.store.get_run(self.session.run_id).status is RunStatus.CANCELLED:
            raise UnknownEffect("parent Run cancelled; linked operation admission closed")

    def _monitor_cancellation(self, session, cancellation=None):
        stop = threading.Event()

        def cancel_linked():
            while not stop.wait(.02):
                if ((cancellation is not None and cancellation.is_set())
                        or self.session.store.get_run(self.session.run_id).status is RunStatus.CANCELLED):
                    try:
                        session.executor.cancel_run(session.run_id)
                    except InvalidTransition:
                        # The linked Run may have finished while cancellation arrived.
                        if session.store.get_run(session.run_id).status not in {
                            RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED,
                        }:
                            raise
                    return

        monitor = threading.Thread(target=cancel_linked, daemon=True)
        monitor.start()
        return stop, monitor

    def _child_agent(self, session):
        from .agent import Agent
        from .mcp import MCPTool

        tools = [tool for tool in builtin_tools() if tool.name != "agent"]
        # Each MCP wrapper is fresh, with the same authenticated server binding.
        tools.extend(MCPTool(tool._client, {"name": tool._remote_name, "description": tool.description,
                                           "inputSchema": tool.parameters})
                     for tool in self.agent.tools if isinstance(tool, MCPTool))
        return Agent(self.agent.llm, tools=tools, max_context_tokens=self.agent.context.max_tokens,
                     max_rounds=20, permission=self.agent.permission, hooks=self.agent.hooks,
                     runtime_session=session)

    def child(self, agent, invocation, arguments, cancellation):
        link = self._link(invocation, "child")
        return self._continue_child(link, arguments, cancellation)

    def _continue_child(self, link, arguments, cancellation):
        child_session = self._open(link, self.session.permission)
        child = self._child_agent(child_session)
        stop, monitor = self._monitor_cancellation(child_session, cancellation)
        previous_gate = child_session.before_inference

        def gate(sub):
            self._admit(cancellation)
            previous_gate(sub)

        child_session.before_inference = gate
        try:
            self._admit(cancellation)
            state = child_session.journal.load(child_session.run_id)["state"]
            result = child.resume() if state["messages"] else child.chat(arguments["task"])
            checkpoint = child_session.journal.load(child_session.run_id)
            if checkpoint["phase"] != "complete":
                raise UnknownEffect("child has unfinished work")
            child_session.finish(RunStatus.SUCCEEDED)
            return child_result(result)
        except Exception as error:
            raise UnknownEffect("linked child requires recovery") from error
        finally:
            stop.set()
            monitor.join()
            child_session.close()

    def recover_children(self):
        from .runtime.state import ToolCallStatus

        self._admit()
        invocations = {item["id"]: item for item in self.session.journal.list_invocations(self.session.run_id)}
        for link in self._links():
            invocation = invocations[link["invocation_id"]]
            if link["kind"] != "child" or invocation["state"] == "completed":
                continue
            if invocation["tool_name"] != "agent" or not invocation["attempt_id"]:
                raise ValueError("invalid persisted child relationship")
            attempt = self.session.store.get_tool_call(invocation["attempt_id"])
            if attempt.status is ToolCallStatus.RUNNING:
                self.session.store.transition_tool_call(attempt.id, ToolCallStatus.INTERRUPTED, "tool.interrupted",
                                                        payload={"reason": "linked child recovery"})
            if self.session.store.get_tool_call(attempt.id).status is not ToolCallStatus.INTERRUPTED:
                continue
            arguments = invocation["arguments"]
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            self._continue_child(link, arguments, threading.Event())
            self.session.journal.reconcile_child(invocation["id"])

    def before(self, agent, invocation, cancellation):
        self._admit(cancellation)
        if agent.hooks:
            for kind, hooks in (("pre", agent.hooks.pre), ("post", agent.hooks.post)):
                for slot, hook in enumerate(hooks):
                    if hook.get("matcher", "") in ("", "*", invocation["tool_name"]):
                        self._link(invocation, kind, slot, command=hook["command"])
            for link in self._links():
                if link["invocation_id"] == invocation["id"] and link["kind"] == "pre":
                    result, exit_code = self._hook(link, invocation, cancellation)
                    if exit_code == 2:
                        return "Blocked by hook: " + result
        return None

    def _links(self):
        return list(self.session.journal.load(self.session.run_id)["state"].get("linked_operations", {}).values())

    def _hook(self, link, invocation, cancellation=None):
        from .agent import Agent
        from .llm import LLMResponse, ToolCall

        self._admit(cancellation)
        session = self._open(link, Permission(allow_all=True))
        stop, monitor = self._monitor_cancellation(session, cancellation)
        # The parent's approval authorizes its configured hooks. Their independent
        # Runs avoid nesting an active ToolCall under the parent's one-call gate.
        agent = Agent(self.agent.llm, tools=[HookTool()], permission=session.permission,
                      max_context_tokens=self.agent.context.max_tokens)
        try:
            session.attach(agent)
            checkpoint = session.journal.load(session.run_id)
            if not session.journal.list_invocations(session.run_id) and checkpoint["phase"] in {"ready", "llm_inflight"}:
                arguments = invocation["arguments"]
                payload = {"tool_name": invocation["tool_name"],
                           "tool_input": json.loads(arguments) if isinstance(arguments, str) else arguments}
                if link["kind"] == "post":
                    payload["tool_response"] = invocation["result"]
                command = link["command"]
                if checkpoint["phase"] == "llm_inflight":
                    sequence = checkpoint["turn_sequence"]
                    request = session.journal.request(session.run_id, sequence)
                    if request != {"source": "configured_hook", "parent": self.session.run_id}:
                        raise ValueError("linked hook request identity mismatch")
                else:
                    sequence = session.journal.begin_turn(session.run_id, {"source": "configured_hook", "parent": self.session.run_id})
                session.journal.record_response(session.run_id, sequence,
                    LLMResponse(tool_calls=[ToolCall("hook", "bash", {"command": command, "timeout": 10, "input": json.dumps(payload)})]).message)
            calls = session.journal.list_invocations(session.run_id)
            session._check_pending(session.journal.load(session.run_id))
            if calls[0]["state"] != "completed":
                session.dispatch(agent, calls[0])
            calls = session.journal.list_invocations(session.run_id)
            exit_code = next((event.payload["exit_code"] for event in reversed(session.store.list_events(session.run_id))
                              if event.payload.get("tool_call_id") == calls[0]["attempt_id"]
                              and "exit_code" in event.payload), None)
            session._sync(agent)
            if session.journal.load(session.run_id)["phase"] == "dispatching":
                session.journal.complete_turn(session.run_id, session._state(agent))
            session.finish(RunStatus.SUCCEEDED)
            return calls[0]["result"], exit_code
        finally:
            stop.set()
            monitor.join()
            session.close()

    def after(self, agent, invocation, output):
        self.gate(agent)

    def gate(self, agent):
        self._admit()
        invocations = {item["id"]: item for item in self.session.journal.list_invocations(self.session.run_id)}
        links = self._links()
        completed_hooks = set()
        # Validate every existing effect before admitting any pending post-hook.
        for link in links:
            with self.session.store._connect() as connection:
                initialized = connection.execute("SELECT 1 FROM agent_sessions WHERE run_id = ?", (link["run_id"],)).fetchone()
            if not initialized:
                if link["kind"] == "child":
                    raise ValueError("unfinished linked child blocks execution")
                continue  # Reserved hook identity has not admitted an effect.
            checkpoint = self.session.journal.load(link["run_id"])
            calls = self.session.journal.list_invocations(link["run_id"])
            if link["kind"] == "child":
                if (checkpoint["phase"] != "complete"
                        or self.session.store.get_run(link["run_id"]).status is not RunStatus.SUCCEEDED
                        or any(call["state"] != "completed" for call in calls)):
                    raise ValueError("unknown or unfinished linked child blocks execution")
            elif checkpoint["phase"] == "blocked" or any(
                call["state"] != "completed" and call["attempt_id"]
                and self.session.store.get_tool_call(call["attempt_id"]).status not in {
                    ToolCallStatus.CREATED, ToolCallStatus.WAITING_APPROVAL,
                } for call in calls
            ):
                self._hook(link, invocations[link["invocation_id"]])  # Own and mark a restored running attempt interrupted.
                raise ValueError("unknown linked hook blocks execution")
            elif calls and all(call["state"] == "completed" for call in calls):
                completed_hooks.add(link["run_id"])
        for link in links:
            invocation = invocations[link["invocation_id"]]
            if ((link["kind"] == "post" and invocation["state"] == "completed")
                    or (link["kind"] == "pre" and link["run_id"] in completed_hooks)):
                self._hook(link, invocation)



def configuration_fingerprint(agent):
    hooks = {"pre": agent.hooks.pre, "post": agent.hooks.post} if agent.hooks else None
    return {
        "hooks_sha256": hashlib.sha256(json.dumps(hooks, sort_keys=True).encode()).hexdigest(),
        "mcp_bindings": {tool.name: tool._client.binding_digest for tool in agent.tools
                         if hasattr(getattr(tool, "_client", None), "binding_digest")},
    }
