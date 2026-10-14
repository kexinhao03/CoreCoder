import os
import sqlite3
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

from corecoder.runtime.agent_store import AgentJournal, RunLease
from corecoder.runtime.checkpoints import CheckpointCipher
from corecoder.runtime.state import ExecutionKind, RiskLevel, RunStatus, StepStatus, ToolCallStatus
from corecoder.runtime.store import SQLiteStore


@pytest.fixture
def journal(tmp_path):
    store = SQLiteStore(tmp_path / "runs.db")
    store.initialize()
    store.create_run(goal="test", workflow="agent", workspace=tmp_path, model="test", prompt_version="v1", run_id="run")
    store.transition_run("run", RunStatus.RUNNING, "run.started")
    journal = AgentJournal(store, CheckpointCipher(Fernet.generate_key()))
    journal.create("run", {"messages": [{"role": "user", "content": "private request"}]})
    return journal


def response(call_id="provider-A", arguments='{"content":"private arguments"}'):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "write_file", "arguments": arguments}}],
    }


def queue(journal):
    sequence = journal.begin_turn("run", {"messages": journal.load("run")["state"]["messages"]})
    return journal.record_response("run", sequence, response())[0]


def bind(journal, invocation):
    return journal.store.create_tool_call(
        run_id="run",
        tool_name="write_file",
        arguments={"content": "private arguments"},
        risk_level=RiskLevel.MUTATING,
        execution_kind=ExecutionKind.IN_PROCESS,
        idempotent=False,
        idempotency_key=None,
        timeout_seconds=30,
        agent_invocation_id=invocation["id"],
    )


def test_cipher_authenticates_key_identity_and_payload(monkeypatch):
    monkeypatch.delenv("RELIAGENT_CHECKPOINT_KEY", raising=False)
    with pytest.raises(ValueError):
        CheckpointCipher()
    with pytest.raises(ValueError):
        CheckpointCipher("invalid")
    key = Fernet.generate_key()
    monkeypatch.setenv("RELIAGENT_CHECKPOINT_KEY", key.decode())
    cipher = CheckpointCipher()
    identity = {"run_id": "run", "record": "context"}
    ciphertext = cipher.seal(identity, {"content": "private"})
    assert cipher.open(identity, ciphertext) == {"content": "private"}
    for candidate, token in [
        (CheckpointCipher(Fernet.generate_key()), ciphertext),
        (cipher, ciphertext[:-1] + b"x"),
    ]:
        with pytest.raises(ValueError):
            candidate.open(identity, token)
    with pytest.raises(ValueError):
        cipher.open({"run_id": "other", "record": "context"}, ciphertext)


def test_response_durable_and_provider_ids_scoped_by_turn(journal):
    invocation = queue(journal)
    assert invocation["provider_call_id"] == "provider-A"
    assert invocation["arguments"] == '{"content":"private arguments"}'
    assert invocation["state"] == "queued"
    assert journal.store.list_tool_calls("run") == []
    snapshot = journal.load("run")
    assert snapshot["phase"] == "dispatching"
    assert snapshot["turn_sequence"] == 1
    assert snapshot["state"]["messages"][-1] == response()
    assert journal.record_response("run", 1, response())[0]["id"] == invocation["id"]
    with pytest.raises(ValueError):
        journal.record_response("run", 1, response(arguments="{}"))
    with pytest.raises(ValueError):
        journal.complete_turn("run", snapshot["state"])
    journal.complete_without_attempt(invocation["id"], "denied")
    journal.complete_turn("run", journal.load("run")["state"])
    next_sequence = journal.begin_turn("run", {"messages": []})
    next_invocation = journal.record_response("run", next_sequence, response())[0]
    assert next_invocation["id"] != invocation["id"]
    assert next_invocation["provider_call_id"] == invocation["provider_call_id"]


def test_request_commits_before_response_and_blocks_second_inference(journal):
    assert journal.begin_turn("run", {"messages": [{"role": "user", "content": "exact request"}]}) == 1
    assert journal.load("run")["phase"] == "llm_inflight"
    with pytest.raises(ValueError):
        journal.begin_turn("run", {})
    journal.block("run", "private block reason")
    assert journal.load("run")["phase"] == "blocked"
    with pytest.raises(ValueError):
        journal.begin_turn("run", {})


def test_batch_response_rolls_back_every_invocation(journal):
    sequence = journal.begin_turn("run", {})
    message = response()
    message["tool_calls"].append(response("provider-B")["tool_calls"][0])
    with sqlite3.connect(journal.store.path) as conn:
        conn.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON agent_invocations
                        WHEN NEW.ordinal = 1 BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        journal.record_response("run", sequence, message)
    assert journal.list_invocations("run") == []
    assert journal.load("run")["phase"] == "llm_inflight"
    assert journal.load("run")["state"]["messages"] == [{"role": "user", "content": "private request"}]


def test_binding_creates_step_and_attempt_atomically_and_rejects_duplicates(journal):
    invocation = queue(journal)
    call = bind(journal, invocation)
    assert call.step_id is not None
    assert journal.store.list_steps("run")[0].attempt_count == 1
    assert journal.list_invocations("run")[0]["attempt_id"] == call.id
    with pytest.raises(ValueError):
        bind(journal, invocation)
    assert len(journal.store.list_tool_calls("run")) == 1


def test_binding_rolls_back_step_and_attempt_on_event_failure(journal):
    invocation = queue(journal)
    with sqlite3.connect(journal.store.path) as conn:
        conn.execute("""CREATE TRIGGER reject_created BEFORE INSERT ON events
                        WHEN NEW.type = 'tool.created' BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        bind(journal, invocation)
    assert journal.store.list_steps("run") == []
    assert journal.store.list_tool_calls("run") == []
    assert journal.list_invocations("run")[0]["attempt_id"] is None


def test_terminal_result_and_model_message_survive_restart_without_summary(journal):
    invocation = queue(journal)
    call = bind(journal, invocation)
    journal.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    output = "private exact output" * 400
    journal.store.transition_tool_call(
        call.id,
        ToolCallStatus.SUCCEEDED,
        "tool.completed",
        result_summary="audit summary",
        agent_result=journal.result_token(invocation["id"], output),
    )
    restored = AgentJournal(SQLiteStore(journal.store.path), journal.cipher)
    assert restored.list_invocations("run")[0]["result"] == output
    assert restored.load("run")["state"]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "provider-A",
        "content": output,
    }
    assert restored.store.list_steps("run")[0].status is StepStatus.SUCCEEDED
    with sqlite3.connect(journal.store.path) as conn:
        dump = "\n".join(conn.iterdump())
    for secret in ["private request", "private arguments", "private exact output"]:
        assert secret not in dump


def test_terminal_rollback_keeps_running_attempt_without_fake_result(journal):
    invocation = queue(journal)
    call = bind(journal, invocation)
    journal.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    before = journal.load("run")
    with sqlite3.connect(journal.store.path) as conn:
        conn.execute("""CREATE TRIGGER reject_result BEFORE UPDATE OF result_ciphertext ON agent_invocations
                        BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        journal.store.transition_tool_call(
            call.id,
            ToolCallStatus.SUCCEEDED,
            "tool.completed",
            agent_result=journal.result_token(invocation["id"], "real result"),
        )
    assert journal.store.get_tool_call(call.id).status is ToolCallStatus.RUNNING
    assert journal.store.list_steps("run")[0].status is StepStatus.RUNNING
    assert journal.load("run") == before
    assert journal.list_invocations("run")[0]["result"] is None
    assert journal.store.list_events("run")[-1].type == "tool.started"


def test_terminal_requires_authenticated_matching_result_and_interruption_cannot_complete(journal):
    invocation = queue(journal)
    call = bind(journal, invocation)
    journal.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    for kwargs in [{}, {"agent_result": b"invalid"}]:
        with pytest.raises(ValueError):
            journal.store.transition_tool_call(call.id, ToolCallStatus.SUCCEEDED, "tool.completed", **kwargs)
    with pytest.raises(ValueError):
        journal.store.transition_tool_call(
            call.id,
            ToolCallStatus.INTERRUPTED,
            "tool.interrupted",
            agent_result=journal.result_token(invocation["id"], "[interrupted]"),
        )
    journal.store.transition_tool_call(call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted")
    assert journal.list_invocations("run")[0]["result"] is None
    assert journal.list_invocations("run")[0]["state"] == "blocked"
    assert journal.load("run")["phase"] == "blocked"


def test_final_response_and_wrong_key_restore(journal):
    sequence = journal.begin_turn("run", {})
    assert journal.record_response("run", sequence, {"role": "assistant", "content": "final private"}) == []
    journal.complete_turn("run", journal.load("run")["state"])
    assert journal.load("run")["phase"] == "complete"
    other = AgentJournal(SQLiteStore(journal.store.path), CheckpointCipher(Fernet.generate_key()))
    with pytest.raises(ValueError):
        other.load("run")


def test_lease_is_nonblocking_across_process_and_released_after_crash(tmp_path):
    script = """import os, sys
from pathlib import Path
from corecoder.runtime.agent_store import RunLease
lease = RunLease(Path(sys.argv[1]), 'run')
lease.acquire()
print('locked', flush=True)
sys.stdin.readline()
os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(ValueError):
            RunLease(tmp_path, "run").acquire()
        with RunLease(tmp_path, "different"):
            pass
        process.stdin.write("\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
        with RunLease(tmp_path, "run"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_private_terminal_output_never_becomes_plaintext_audit_summary(journal):
    invocation = queue(journal)
    call = bind(journal, invocation)
    journal.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    output = "ordinary private document body"
    journal.store.transition_tool_call(
        call.id,
        ToolCallStatus.SUCCEEDED,
        "tool.completed",
        result_summary=output,
        agent_result=journal.result_token(invocation["id"], output),
    )
    with sqlite3.connect(journal.store.path) as conn:
        assert output not in "\n".join(conn.iterdump())


def test_reconciliation_requires_evidence_and_commits_confirmed_output(journal):
    invocation = queue(journal)
    call = bind(journal, invocation)
    journal.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    journal.store.transition_tool_call(call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted")
    with pytest.raises(ValueError):
        journal.reconcile(invocation["id"], "confirmed_completed", output="verified", evidence={})
    with pytest.raises(ValueError):
        journal.store.transition_tool_call(
            call.id, ToolCallStatus.FAILED, "tool.failed", agent_result=journal.result_token(invocation["id"], "fake failure")
        )
    with pytest.raises(ValueError):
        journal.store.reconcile_interrupted_tool_call(call.id, ToolCallStatus.SUCCEEDED, "confirmed_succeeded")
    journal.reconcile(invocation["id"], "confirmed_completed", output="verified", evidence={"reference": "operator-check"})
    assert journal.list_invocations("run")[0]["result"] == "verified"
    assert journal.store.get_tool_call(call.id).status is ToolCallStatus.SUCCEEDED
    assert journal.store.list_steps("run")[0].status is StepStatus.SUCCEEDED
    assert journal.load("run")["phase"] == "dispatching"
    assert journal.load("run")["state"]["messages"][-1]["content"] == "verified"
    assert journal.store.list_reconciliation_evidence(call.id)[0].decision == "confirmed_completed"


def test_confirmed_not_executed_reserves_new_attempt_and_new_approval(journal):
    from corecoder.runtime.approvals import ApprovalDecision, ApprovalStatus

    invocation = queue(journal)
    call = bind(journal, invocation)
    journal.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    stale_token = journal.result_token(invocation["id"], "old attempt output")
    journal.store.transition_tool_call(call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted")
    journal.reconcile(invocation["id"], "confirmed_not_executed", evidence={"reference": "no-effect-check"})
    current = journal.list_invocations("run")[0]
    assert current["provider_call_id"] == "provider-A"
    assert current["result"] is None
    assert current["attempt_id"] != call.id
    retry = journal.store.get_tool_call(current["attempt_id"])
    assert retry.status is ToolCallStatus.WAITING_APPROVAL
    assert retry.retry_of == call.id
    assert retry.attempt == 2
    assert retry.step_id == call.step_id
    assert journal.store.list_steps("run")[0].attempt_count == 2
    assert journal.store.get_tool_call(call.id).status is ToolCallStatus.FAILED
    approval = journal.store.get_approval_for_tool_call(retry.id)
    assert approval.status is ApprovalStatus.PENDING
    journal.store.resolve_approval(approval.id, ApprovalDecision.ALLOW_ONCE)
    journal.store.transition_tool_call(retry.id, ToolCallStatus.RUNNING, "tool.started")
    with pytest.raises(ValueError):
        journal.store.transition_tool_call(retry.id, ToolCallStatus.SUCCEEDED, "tool.completed", agent_result=stale_token)
    journal.store.transition_tool_call(
        retry.id, ToolCallStatus.SUCCEEDED, "tool.completed", agent_result=journal.result_token(invocation["id"], "new output")
    )
    assert journal.list_invocations("run")[0]["result"] == "new output"


def test_approval_denial_and_result_are_one_transaction(journal):
    from corecoder.runtime.approvals import ApprovalDecision, ApprovalStatus

    invocation = queue(journal)
    call = bind(journal, invocation)
    approval = journal.store.request_approval(call.id, arguments_summary="safe", workspace=".", risk_reason="mutating")
    assert journal.store.list_steps("run")[0].status is StepStatus.WAITING_APPROVAL
    with pytest.raises(ValueError):
        journal.store.resolve_approval(approval.id, ApprovalDecision.DENY)
    assert journal.store.get_approval(approval.id).status is ApprovalStatus.PENDING
    journal.store.resolve_approval(
        approval.id, ApprovalDecision.DENY, agent_result=journal.result_token(invocation["id"], "denied by operator")
    )
    assert journal.list_invocations("run")[0]["result"] == "denied by operator"
    assert journal.store.list_steps("run")[0].status is StepStatus.CANCELLED


def test_malformed_arguments_and_reasoning_text_are_preserved_exactly(journal):
    sequence = journal.begin_turn("run", {})
    message = response(arguments='{"broken":')
    message["reasoning_content"] = "private reasoning text"
    invocation = journal.record_response("run", sequence, message)[0]
    assert invocation["arguments"] == '{"broken":'
    assert journal.load("run")["state"]["messages"][-1] == message
    journal.complete_without_attempt(invocation["id"], "invalid arguments")
    assert journal.list_invocations("run")[0]["result"] == "invalid arguments"


def test_llm_retry_authorization_preserves_old_request_and_allocates_new_turn(journal):
    journal.begin_turn("run", {"messages": [{"role": "user", "content": "original inference"}]})
    journal.block("run", "inference result unknown")
    with pytest.raises(ValueError):
        journal.authorize_llm_retry("run", evidence={})
    journal.authorize_llm_retry("run", evidence={"reference": "operator accepts duplicated inference"})
    assert journal.load("run")["phase"] == "ready"
    with sqlite3.connect(journal.store.path) as conn:
        assert conn.execute("SELECT status FROM agent_turns WHERE sequence = 1").fetchone()[0] == "outcome_unknown"
        assert conn.execute("SELECT request_ciphertext FROM agent_turns WHERE sequence = 1").fetchone()[0]
    assert journal.begin_turn("run", {"messages": []}) == 2
    assert journal.store.list_events("run")[-1].type == "agent.inference_retry_authorized"


def test_llm_retry_authorization_cannot_bypass_pending_tools(journal):
    queue(journal)
    journal.block("run", "operator block")
    with pytest.raises(ValueError):
        journal.authorize_llm_retry("run", evidence={"reference": "not an inference retry"})
    assert journal.load("run")["phase"] == "blocked"
    with pytest.raises(ValueError):
        journal.save_state("run", {"messages": []})


def test_response_rejects_non_string_provider_identity_without_partial_commit(journal):
    sequence = journal.begin_turn("run", {})
    with pytest.raises(ValueError):
        journal.record_response("run", sequence, response(call_id=123))
    assert journal.list_invocations("run") == []
    assert journal.load("run")["phase"] == "llm_inflight"


def test_direct_store_cannot_start_durable_attempt_without_authenticated_journal(journal):
    call = bind(journal, queue(journal))
    store = SQLiteStore(journal.store.path)
    with pytest.raises(ValueError):
        store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    assert journal.store.get_tool_call(call.id).status is ToolCallStatus.CREATED


def test_wrong_key_cannot_start_an_existing_durable_attempt(journal):
    call = bind(journal, queue(journal))
    wrong = AgentJournal(SQLiteStore(journal.store.path), CheckpointCipher(Fernet.generate_key()))
    with pytest.raises(ValueError):
        wrong.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    assert journal.store.get_tool_call(call.id).status is ToolCallStatus.CREATED


def test_unknown_first_call_blocks_later_validation_results(journal):
    sequence = journal.begin_turn("run", {})
    message = response()
    message["tool_calls"].append(response("provider-B")["tool_calls"][0])
    first, second = journal.record_response("run", sequence, message)
    with pytest.raises(ValueError):
        journal.complete_without_attempt(second["id"], "denied out of order")
    call = bind(journal, first)
    journal.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    journal.store.transition_tool_call(call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted")
    with pytest.raises(ValueError):
        journal.complete_without_attempt(second["id"], "cannot dispatch while unknown")
    assert [item["state"] for item in journal.list_invocations("run")] == ["blocked", "queued"]


@pytest.mark.parametrize("decision", ["confirmed_completed", "confirmed_not_executed"])
def test_reconciliation_event_failure_rolls_back_all_facts(journal, decision):
    invocation = queue(journal)
    call = bind(journal, invocation)
    journal.store.transition_tool_call(call.id, ToolCallStatus.RUNNING, "tool.started")
    journal.store.transition_tool_call(call.id, ToolCallStatus.INTERRUPTED, "tool.interrupted")
    before = (
        journal.load("run"),
        journal.list_invocations("run"),
        journal.store.list_tool_calls("run"),
        journal.store.list_steps("run"),
        journal.store.list_events("run"),
        journal.store.get_run("run"),
    )
    with sqlite3.connect(journal.store.path) as conn:
        conn.execute("""CREATE TRIGGER reject_reconciliation BEFORE INSERT ON events
                        WHEN NEW.type = 'agent.reconciled' BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        journal.reconcile(
            invocation["id"],
            decision,
            output="verified result" if decision == "confirmed_completed" else None,
            evidence={"reference": "operator"},
        )
    assert (
        journal.load("run"),
        journal.list_invocations("run"),
        journal.store.list_tool_calls("run"),
        journal.store.list_steps("run"),
        journal.store.list_events("run"),
        journal.store.get_run("run"),
    ) == before
    assert journal.store.list_reconciliation_evidence(call.id) == []
    assert journal.store.list_approvals("run") == []
