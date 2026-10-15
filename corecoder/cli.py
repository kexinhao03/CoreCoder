"""Interactive REPL - the user-facing terminal interface."""

import argparse
import json
import os
import sys
from pathlib import Path

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .agent import Agent
from .agent_runtime import DurableAgentSession
from .config import Config
from .hooks import load_hooks
from .llm import LLM, LiteLLM
from .mcp import load_mcp_tools
from .permissions import Permission
from .runtime import RunStatus
from .session import list_sessions, load_session, save_session
from .tools import builtin_tools

console = Console()


def _parse_args():
    p = argparse.ArgumentParser(
        prog="corecoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $CORECODER_MODEL or gpt-5.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("--yes", action="store_true", help="Auto-approve every tool call (for scripts and CI)")
    p.add_argument("--runtime-workspace", type=Path, help="Runtime audit workspace (default: current directory)")
    p.add_argument("--no-runtime", action="store_true", help="Use the legacy non-durable Agent")
    p.add_argument("--demo", action="store_true", help="Run the offline scripted demo (no API key needed)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    p.add_argument("--resume-run", help="Resume an encrypted durable Run without adding a prompt")
    p.add_argument("--run-status", metavar="RUN", help="Inspect durable Run state without a model")
    p.add_argument("--retry-inference", metavar="RUN", help="Explicitly authorize repeating lost inference")
    p.add_argument("--reconcile-invocation", metavar="ID", help="Reconcile an invocation in --resume-run")
    p.add_argument("--decision", choices=["confirmed_completed", "confirmed_not_executed", "unresolved"])
    p.add_argument("--evidence", help="Operator evidence supporting reconciliation or inference retry")
    p.add_argument("--result-file", type=Path, help="Full confirmed tool output, read from this UTF-8 file")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    if args.resume_run and (args.resume or args.prompt or args.no_runtime):
        p.error("--resume-run cannot be combined with -r, --prompt, or --no-runtime")
    if args.reconcile_invocation and (not args.resume_run or not args.decision or not args.evidence):
        p.error("reconciliation requires --resume-run, --decision, and --evidence")
    if args.retry_inference and not args.evidence:
        p.error("--retry-inference requires --evidence")
    return args


def _build_agent(
    llm, config: Config, permission: Permission, *, runtime_workspace: Path | None, runtime_enabled: bool,
    resume_run: str | None = None,
    initial_messages: list | None = None,
) -> tuple[Agent, DurableAgentSession | None]:
    session = None
    if runtime_enabled:
        session = DurableAgentSession.open(
            runtime_workspace or Path.cwd(), config.model, permission,
            key=os.environ.get("RELIAGENT_CHECKPOINT_KEY"), run_id=resume_run,
        )
    tools = [*builtin_tools(), *load_mcp_tools()]
    hooks = load_hooks()
    agent = Agent(
        llm=llm,
        tools=tools,
        max_context_tokens=config.max_context_tokens,
        permission=permission,
        hooks=hooks,
    )
    if initial_messages:
        from copy import deepcopy
        agent.messages = deepcopy(initial_messages)
    if session is not None:
        from .durable_operations import DurableOperations
        agent.runtime_session = session
        agent.durable_operations = DurableOperations(session, agent)
        session.attach(agent)
    return agent, session


def _runtime_action(args):
    """Model-free inspection and explicit recovery, with no provider configuration."""
    from .runtime import SQLiteStore, ToolCallStatus
    from .runtime.agent_store import AgentJournal, RunLease
    from .runtime.checkpoints import CheckpointCipher

    workspace = (args.runtime_workspace or Path.cwd()).resolve()
    cipher = CheckpointCipher()
    store = SQLiteStore(workspace / ".reliagent" / "agent-runtime.sqlite")
    store.initialize()
    journal = AgentJournal(store, cipher)
    run_id = args.run_status or args.retry_inference or args.resume_run
    run = store.get_run(run_id)
    if args.run_status:
        checkpoint = journal.load(run_id)
        calls = journal.list_invocations(run_id)
        links = []
        for link in checkpoint["state"].get("linked_operations", {}).values():
            try:
                phase = journal.load(link["run_id"])["phase"]
            except KeyError:
                phase = "not_initialized"
            links.append({key: link[key] for key in ("run_id", "kind", "invocation_id")} | {"phase": phase})
        console.print_json(data={"run_id": run_id, "status": run.status.value, "phase": checkpoint["phase"],
                                 "invocations": [{key: item[key] for key in ("id", "tool_name", "state", "attempt_id")}
                                                 for item in calls], "linked_operations": links})
        return
    with RunLease(workspace, run_id):
        if args.retry_inference:
            journal.authorize_llm_retry(run_id, evidence={"operator": args.evidence})
        else:
            call = next((item for item in journal.list_invocations(run_id) if item["id"] == args.reconcile_invocation), None)
            if call is None or not call["attempt_id"]:
                raise ValueError("invocation does not belong to this Run or has no started attempt")
            attempt = store.get_tool_call(call["attempt_id"])
            if attempt.status is ToolCallStatus.RUNNING:
                store.transition_tool_call(attempt.id, ToolCallStatus.INTERRUPTED, "tool.interrupted",
                                           payload={"reason": "operator inspected orphaned attempt"})
            output = args.result_file.read_text(encoding="utf-8") if args.result_file else None
            journal.reconcile(call["id"], args.decision, output=output, evidence={"operator": args.evidence})
    console.print(f"Recovery decision recorded for Run {run_id}.")


def main():
    args = _parse_args()

    if args.run_status or args.retry_inference or args.reconcile_invocation:
        _runtime_action(args)
        return

    if args.demo:
        from .demo import run_demo
        raise SystemExit(run_demo())

    config = Config.from_env()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    # consent layer: ask in the REPL, refuse in one-shot mode, --yes skips it
    if args.yes:
        permission = Permission(allow_all=True)
    elif args.prompt:
        permission = Permission()
    else:
        permission = Permission(ask=_ask_permission)

    # Resolve the resumed model before recording the Runtime session.
    loaded = None
    if args.resume:
        loaded = load_session(args.resume)
        if not loaded:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)
        if not args.model:
            llm.model = config.model = loaded[1]

    agent, runtime_session = _build_agent(
        llm, config, permission,
        runtime_workspace=args.runtime_workspace,
        runtime_enabled=not args.no_runtime,
        resume_run=args.resume_run,
        initial_messages=loaded[0] if loaded else None,
    )

    status = RunStatus.SUCCEEDED
    try:
        if loaded:
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")

        if args.resume_run:
            console.print(agent.resume())
        elif args.prompt:
            _run_once(agent, args.prompt)
        else:
            _repl(agent, config)
    except KeyboardInterrupt:
        status = RunStatus.CANCELLED
        raise
    except SystemExit as exc:
        if exc.code:
            status = RunStatus.CANCELLED if exc.code == 130 else RunStatus.FAILED
        raise
    except Exception:
        status = RunStatus.FAILED
        raise
    finally:
        if runtime_session is not None:
            runtime_session.finish(status)


def _ask_permission(tool_name: str, arguments: dict) -> str:
    """REPL consent prompt. Anything but a clear yes counts as a no."""
    console.print(f"\n[bold yellow]permission requested:[/] [cyan]{tool_name}[/cyan]({_brief(arguments)})")
    try:
        answer = pt_prompt("  [y] allow once  [a] always allow this tool  [n] deny: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]denied[/dim]")
        return "deny"
    if answer in ("y", "yes"):
        return "once"
    if answer in ("a", "always"):
        return "always"
    return "deny"


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    perm = agent.permission
    if perm is not None and perm.ask is None and not perm.allow_all:
        console.print("[dim]one-shot mode: mutating tools are refused unless you pass --yes[/dim]")

    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    try:
        agent.chat(prompt, on_token=on_token, on_tool=on_tool)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        # one-shot mode: print whatever went wrong and exit non-zero
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    print()


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    perm = agent.permission
    mode = "auto-approve every tool call (--yes)" if (perm and perm.allow_all) else "ask before mutating tools"
    mcp_count = sum(1 for t in agent.tools if t.name.startswith("mcp__"))
    console.print(Panel(
        f"[bold]CoreCoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + f"\nPermissions: [cyan]{mode}[/cyan]"
        + (f"\nHooks: [cyan]{len(agent.hooks.pre)} pre, {len(agent.hooks.post)} post[/cyan]"
           " from ~/.corecoder/hooks.json" if agent.hooks else "")
        + (f"\nMCP: [cyan]{mcp_count} tools[/cyan] from ~/.corecoder/mcp.json" if mcp_count else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))

    hist_path = os.path.expanduser("~/.corecoder_history")
    history = InMemoryHistory() if agent.runtime_session else FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        try:
            user_input = pt_prompt(
                "You (plan) > " if agent.plan_mode else "You > ",
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not user_input:
            continue

        if agent.runtime_session and (user_input in {"/reset", "/plan", "/compact", "/save", "/undo"}
                                      or (agent.plan_mode and user_input.lower() in {"approve", "/approve"})):
            try:
                agent.runtime_session.ready(agent)
            except ValueError as error:
                console.print(f"[red]Error: {error}[/red]")
                continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/plan":
            if agent.runtime_session:
                agent.runtime_session.ready(agent)
            agent.plan_mode = not agent.plan_mode
            if agent.plan_mode:
                console.print(
                    "[yellow]Plan mode on.[/yellow] The agent can look but not touch: it will "
                    "investigate read-only and present a plan. Type [bold]approve[/bold] to "
                    "accept the plan, or [bold]/plan[/bold] again to exit."
                )
            else:
                console.print("[yellow]Plan mode off.[/yellow]")
            continue
        if agent.plan_mode and user_input.lower() in ("approve", "/approve"):
            agent.plan_mode = False
            console.print("[yellow]Plan mode off.[/yellow]")
            user_input = "approve"  # the approval itself goes to the model, which then executes
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model and agent.runtime_session is not None:
                console.print("A durable Run keeps its original model. Start a new Run to switch.")
                continue
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            compressed = agent.compact()
            after = estimate_tokens(agent.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            continue
        if user_input == "/save":
            if agent.runtime_session is not None:
                reference = agent.runtime_session.export()
                destination = Path(reference["workspace"]) / ".reliagent" / (reference["run_id"] + ".json")
                destination.write_text(json.dumps(reference, indent=2), encoding="utf-8")
                console.print(f"Run reference saved: {destination}")
                console.print(f"Resume with: corecoder --resume-run {reference['run_id']}")
                continue
            sid = save_session(agent.messages, config.model)
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: corecoder -r {sid}")
            continue
        if user_input == "/diff":
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/undo":
            console.print(agent.undo())
            continue
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")
            continue

        # an unknown /command shouldn't be sent to the model as a prompt
        if user_input.startswith("/"):
            console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok, streamed=streamed):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        try:
            response = agent.chat(user_input, on_token=on_token, on_tool=on_tool)
            if streamed:
                print()  # newline after streamed tokens
            else:
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:  # noqa: BLE001
            # keep the REPL alive no matter what chat() throws
            console.print(f"\n[red]Error: {e}[/red]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /undo          Revert the most recent file change\n"
        "  /plan          Toggle plan mode: read-only, then a plan to approve\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  quit           Exit CoreCoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="CoreCoder Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")
