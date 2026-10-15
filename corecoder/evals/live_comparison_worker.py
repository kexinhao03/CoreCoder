"""Standalone worker: select the source tree BEFORE importing any corecoder module."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path


def atomic(path, value):
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def append(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(value, sort_keys=True) + '\n').encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def effect(path):
    manifest = json.loads((path / 'manifest.json').read_text())
    owner = int(os.environ.get('RELIAGENT_EVAL_WORKER_PID', '0'))
    def check():
        if owner < 1 or time.monotonic() >= manifest['deadline_monotonic']:
            raise SystemExit(125)
        if os.name == 'posix':
            try:
                os.kill(owner, 0)
            except ProcessLookupError:
                raise SystemExit(125) from None
    check()
    append(path / 'children.jsonl', {'pid': os.getpid(), 'worker_pid': owner, 'pgid': os.getpgid(0) if os.name == 'posix' else None})
    check()
    atomic(path / 'child-environment.json', {'python': sys.version, 'executable': sys.executable, 'pid': os.getpid()})
    append(path / 'effects.jsonl', {'task_id': manifest['trial']['trial_id'], 'operation': 'effect'})
    if (os.environ.get('RELIAGENT_EVAL_PHASE') == 'initial'
            and manifest['trial']['fault_point'] == 'after_effect_before_result_persist'):
        atomic(path / 'handshake.json', {'boundary': manifest['trial']['fault_point'], 'pid': owner,
            'emitter_pid': os.getpid(), 'effect_count': len(lines(path / 'effects.jsonl')), 'role': 'child',
            'monotonic': time.monotonic()})
        while True:
            check()
            time.sleep(0.01)
    append(path / 'child-exits.jsonl', {'pid': os.getpid(), 'worker_pid': owner})
    print('external effect committed', flush=True)


def execute(path, phase):
    manifest = json.loads((path / 'manifest.json').read_text())
    source = Path(manifest['source']['path'])
    # Defensive even if called via runpy in an already imported Python process.
    for name in list(sys.modules):
        if name == 'corecoder' or name.startswith('corecoder.'):
            del sys.modules[name]
    sys.path.insert(0, str(source))
    from corecoder import agent as agent_module
    from corecoder.llm import LLM, LLMResponse, ToolCall
    from corecoder.permissions import Permission
    from corecoder.tools.bash import BashTool
    from corecoder.tools.read import ReadFileTool
    from corecoder.tools.write import WriteFileTool

    actual = Path(agent_module.__file__).resolve()
    digest = hashlib.sha256(actual.read_bytes()).hexdigest()
    if not actual.is_relative_to(source) or digest != manifest['source']['agent_sha256']:
        raise ValueError('worker source identity mismatch')
    atomic(path / 'source-identity.json', {'agent_file': str(actual), 'agent_sha256': digest})
    os.environ['RELIAGENT_EVAL_WORKER_PID'] = str(os.getpid())
    os.environ['RELIAGENT_EVAL_PHASE'] = phase
    work = path / 'work'
    os.chdir(work)
    session = None
    durable = manifest['trial']['config_id'] == 'D_agent'
    provider = manifest['provider']

    class CapturedLLM(LLM):
        def __init__(self):
            super().__init__(model=manifest['model'], api_key='ollama', base_url=manifest['base_url'], **manifest['generation'])
            self.client = self.client.with_options(timeout=max(1, manifest['deadline_monotonic'] - time.monotonic()))
            def sent(request):
                append(path / 'provider.jsonl', {'event': 'http_request', 'phase': phase,
                    'body': json.loads(request.content)})
            def received(response):
                append(path / 'provider.jsonl', {'event': 'http_response', 'phase': phase,
                    'status_code': response.status_code})
            self.client._client.event_hooks['request'].append(sent)
            self.client._client.event_hooks['response'].append(received)

        def _call_with_retry(self, params, *args, **kwargs):
            # Capture each actual OpenAI invocation, including upstream retries.
            append(path / 'provider.jsonl', {'event': 'request', 'phase': phase, 'provider': provider, 'params': params})
            if provider == 'fixture':
                raise AssertionError('fixture must never reach the network')
            # Observe real SDK sends as well as parsed Agent responses below.
            return super()._call_with_retry(params, *args, **kwargs)

        def chat(self, messages, tools=None, on_token=None):
            if not durable:
                accepted_boundary(messages)
            model_started = time.monotonic()
            if provider == 'fixture':
                requests = sum(item['event'] == 'request' for item in lines(path / 'provider.jsonl'))
                append(path / 'provider.jsonl', {'event': 'request', 'phase': phase, 'provider': 'fixture',
                    'params': {'messages': messages, 'tools': tools, 'model': self.model, **self.extra}})
                if manifest['fixture_mode'] == 'wait':
                    while True:
                        time.sleep(0.01)
                expected = json.dumps({'task_id': manifest['trial']['trial_id'], 'verified_effects': 1})
                calls = [('read_file', {'file_path': 'instructions.txt'}),
                         ('bash', {'command': manifest['command']}),
                         ('write_file', {'file_path': 'final-artifact.json', 'content': expected})]
                if manifest['fixture_mode'] == 'no_tools' or requests >= len(calls):
                    response = LLMResponse(content='fixture finished')
                else:
                    name, arguments = calls[requests]
                    response = LLMResponse(tool_calls=[ToolCall(id=f'fixture-{requests}', name=name, arguments=arguments)])
            else:
                response = super().chat(messages, tools=tools, on_token=on_token)
            append(path / 'provider.jsonl', {'event': 'response', 'phase': phase, 'provider': provider,
                'model_seconds': time.monotonic() - model_started, 'message': response.message, 'prompt_tokens': response.prompt_tokens, 'completion_tokens': response.completion_tokens})
            return response

    def approve(name, arguments):
        allowed = ((name == 'bash' and arguments.get('command') == manifest['command'])
                   or (name == 'write_file' and Path(arguments.get('file_path', '')).resolve() == work / 'final-artifact.json'))
        append(path / 'approvals.jsonl', {'phase': phase, 'tool': name, 'arguments': arguments, 'allowed': allowed})
        return 'once' if allowed else 'deny'

    def accepted_boundary(messages):
        if manifest['trial']['fault_point'] != 'after_accepted_tool_result_before_next_inference':
            return
        bash_ids = {call['id'] for message in messages for call in message.get('tool_calls', [])
                    if call.get('function', {}).get('name') == 'bash'}
        accepted = [message['tool_call_id'] for message in messages
                    if message.get('role') == 'tool' and message.get('tool_call_id') in bash_ids]
        if accepted and lines(path / 'effects.jsonl'):
            if session is not None:
                invocations = session.journal.list_invocations(session.run_id)
                if not any(item['provider_call_id'] == accepted[-1] and item['state'] == 'completed' for item in invocations):
                    raise ValueError('accepted durable result is not completed in production journal')
            boundary('after_accepted_tool_result_before_next_inference', accepted_tool_call_id=accepted[-1])

    def boundary(point, **evidence):
        if phase != 'initial' or manifest['trial']['fault_point'] != point:
            return
        effects = lines(path / 'effects.jsonl')
        if not effects:
            return
        if session is not None:
            atomic(path / 'invocations-initial.json', session.journal.list_invocations(session.run_id))
        atomic(path / 'handshake.json', {'boundary': point, 'pid': os.getpid(), 'effect_count': len(effects),
                                        'role': 'runtime', 'monotonic': time.monotonic(),
                                        'boundary_label': manifest['trial'].get('boundary_label', point), **evidence})
        while time.monotonic() < manifest['deadline_monotonic']:
            time.sleep(0.01)
        raise TimeoutError('supervisor did not inject fault before trial deadline')

    tools = [ReadFileTool(), WriteFileTool(), BashTool()]
    for tool in tools:
        original, name = tool.execute, tool.name
        def captured(_original=original, _name=name, **arguments):
            append(path / 'tools.jsonl', {'event': 'start', 'phase': phase, 'tool': _name, 'arguments': arguments})
            if _name == 'bash':
                unresolved = False
                if session is not None:
                    unresolved = any(item['tool_name'] == 'bash' and item['state'] != 'completed'
                                     for item in session.journal.list_invocations(session.run_id)[:-1])
                append(path / 'dispatch.jsonl', {'phase': phase, 'prior_unresolved': unresolved, 'command': arguments['command']})
            output = _original(**arguments)
            append(path / 'tools.jsonl', {'event': 'result', 'phase': phase, 'tool': _name, 'output': output})
            if _name == 'bash':
                boundary('after_effect_before_result_persist')
            return output
        tool.execute = captured
    permission = Permission(ask=approve)
    llm = CapturedLLM()
    result = {'error': None, 'human_intervention': False, 'runtime_status': None}
    try:
        if durable:
            import sqlite3

            from corecoder import cli
            from corecoder.config import Config
            from corecoder.evals.comparison_worker import MeasuredConnection
            from corecoder.runtime.store import SQLiteStore
            def measured_connect(store):
                connection = sqlite3.connect(store.path, timeout=5, factory=MeasuredConnection)
                connection.ledger = path / 'sql.jsonl'
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA foreign_keys = ON')
                connection.execute('PRAGMA busy_timeout = 5000')
                connection.set_trace_callback(connection.trace)
                return connection
            SQLiteStore._connect = measured_connect
            os.environ['RELIAGENT_CHECKPOINT_KEY'] = Path(os.environ['RELIAGENT_EVAL_KEY_FILE']).read_text()
            cli.builtin_tools = lambda: tools
            cli.load_mcp_tools = list
            cli.load_hooks = lambda: None
            run_id = json.loads((path / 'run.json').read_text())['run_id'] if phase == 'resume' else None
            config = Config(model=manifest['model'], api_key='ollama', base_url=manifest['base_url'], max_context_tokens=4096)
            agent, session = cli._build_agent(llm, config, permission, runtime_workspace=work, runtime_enabled=True, resume_run=run_id)
            atomic(path / 'run.json', {'run_id': session.run_id})
            def before_execution(agent, invocation, cancellation):
                arguments = json.loads(invocation['arguments']) if isinstance(invocation['arguments'], str) else invocation['arguments']
                append(path / 'tools.jsonl', {'event': 'start', 'phase': phase,
                    'tool': invocation['tool_name'], 'arguments': arguments})
                if invocation['tool_name'] == 'bash':
                    started_ids = {item.get('invocation_id') for item in lines(path / 'dispatch.jsonl')}
                    unresolved = any(item['tool_name'] == 'bash' and item['state'] != 'completed'
                        and item['id'] in started_ids for item in session.journal.list_invocations(session.run_id))
                    append(path / 'dispatch.jsonl', {'phase': phase, 'prior_unresolved': unresolved,
                        'invocation_id': invocation['id'], 'command': arguments['command']})
            session.before_execution = before_execution
            prior = session.after_result
            def after_result(agent, invocation, output):
                if prior is not None:
                    prior(agent, invocation, output)
                append(path / 'tools.jsonl', {'event': 'result', 'phase': phase,
                    'tool': invocation['tool_name'], 'output': output})
                if invocation['tool_name'] == 'bash':
                    boundary('after_result_before_next_inference')
            session.after_result = after_result
            prior_inference = session.before_inference
            def before_inference(agent):
                if prior_inference is not None:
                    prior_inference(agent)
                checkpoint = session.journal.load(session.run_id)
                if checkpoint['phase'] == 'ready':
                    accepted_boundary(checkpoint['state']['messages'])
            session.before_inference = before_inference
        else:
            if phase != 'initial':
                raise ValueError('upstream has no resume implementation')
            agent = agent_module.Agent(llm, tools=tools, max_context_tokens=4096, max_rounds=12, permission=permission, hooks=None)
        agent.max_rounds = 12
        atomic(path / f'agent-config-{phase}.json', {'system': agent._system, 'tools': agent._tool_schemas(),
            'generation': manifest['generation'], 'model': llm.model, 'base_url': manifest['base_url'], 'provider': provider})
        result['answer'] = agent.resume() if phase == 'resume' else agent.chat(manifest['prompt'])
    except Exception as error:  # noqa: BLE001 - failures are retained
        result['error'] = f'{type(error).__name__}: {error}'
        result['human_intervention'] = durable and phase == 'resume' and ('unknown' in str(error) or 'blocks' in str(error))
    finally:
        if session is not None:
            atomic(path / f'invocations-{phase}.json', session.journal.list_invocations(session.run_id))
            result['runtime_status'] = session.store.get_run(session.run_id).status.value
            from corecoder.runtime.tracing import TraceService
            atomic(path / 'trace.json', TraceService(session.store).export_run(session.run_id))
            session.close()
        atomic(path / f'worker-{phase}.json', result)


if __name__ == '__main__':
    directory = Path(sys.argv[1]).resolve()
    if sys.argv[2] == 'effect':
        effect(directory)
    else:
        execute(directory, sys.argv[2])
