"""Opt-in real provider acceptance through the production durable builder.

RELIAGENT_REAL_LLM=1 plus normal CORECODER_MODEL/OPENAI_BASE_URL/OPENAI_API_KEY
configuration enables this test. No model, tool, Runtime, or approval doubles.
Only unrelated user MCP/hook configuration is isolated from the toy workspace.
"""

import os
import shlex
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

from corecoder import cli
from corecoder.config import Config
from corecoder.llm import LLM, LiteLLM
from corecoder.permissions import Permission
from corecoder.runtime import RunStatus


@pytest.mark.skipif(os.environ.get("RELIAGENT_REAL_LLM") != "1", reason="explicit real-provider opt-in required")
def test_real_model_reads_edits_tests_and_cached_resume(tmp_path, monkeypatch):
    config = Config.from_env()
    assert config.api_key, "Configure a provider locally before enabling the real test"
    monkeypatch.setenv("RELIAGENT_CHECKPOINT_KEY", Fernet.generate_key().decode())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_mcp_tools", list)
    monkeypatch.setattr(cli, "load_hooks", lambda: None)
    source = tmp_path / "calc.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\nclass Addition(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8")
    command = subprocess.list2cmdline([sys.executable, "-B", "-m", "unittest", "-v"]) if os.name == "nt" else shlex.join([sys.executable, "-B", "-m", "unittest", "-v"])
    model_type = LiteLLM if config.provider == "litellm" else LLM
    llm = model_type(model=config.model, api_key=config.api_key, base_url=config.base_url,
                     temperature=0, max_tokens=config.max_tokens)
    approvals = []
    permission = Permission(ask=lambda name, args: approvals.append(name) or "once")
    agent, session = cli._build_agent(llm, config, permission, runtime_workspace=tmp_path, runtime_enabled=True)
    agent.max_rounds = 10
    try:
        agent.chat(f"Fix the addition bug in calc.py. Read calc.py and test_calc.py, use edit_file for the smallest fix, then use bash to run exactly: {command}. Stop after the test passes. Do not delegate or change tests.")
        invocations = session.journal.list_invocations(session.run_id)
        assert {"read_file", "edit_file", "bash"} <= {item["tool_name"] for item in invocations}
        assert {"edit_file", "bash"} <= set(approvals)
        assert any(item["tool_name"] == "bash" and "OK" in item["result"] for item in invocations)
        assert "return a + b" in source.read_text()
        assert all(item["state"] == "completed" for item in invocations)
        final = agent.messages[-1]["content"]
        run_id = session.run_id
        session.finish(RunStatus.SUCCEEDED)
    finally:
        session.close()
    before_tokens = llm.total_prompt_tokens
    restored, session = cli._build_agent(llm, config, permission, runtime_workspace=tmp_path,
                                         runtime_enabled=True, resume_run=run_id)
    try:
        assert restored.resume() == final
        assert llm.total_prompt_tokens == before_tokens
        assert session.journal.list_invocations(run_id) == invocations
    finally:
        session.close()
