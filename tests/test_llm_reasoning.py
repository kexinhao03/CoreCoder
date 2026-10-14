"""Provider tool-turn fields survive streaming and can be checkpointed exactly."""

from types import SimpleNamespace

from corecoder.llm import LLM


def stream_client(chunks):
    llm = object.__new__(LLM)
    llm.model = "deepseek-flash"
    llm.extra = {}
    llm.total_prompt_tokens = llm.total_completion_tokens = 0
    llm._call_with_retry = lambda params: iter(chunks)
    return llm


def chunk(**fields):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**fields))], usage=None)


def test_reasoning_chunks_are_preserved_but_not_streamed_as_answer():
    answer_tokens = []
    llm = stream_client([
        chunk(reasoning_content="provider continuation "),
        chunk(reasoning_content="context"),
        chunk(content="answer"),
    ])
    response = llm.chat([{"role": "user", "content": "question"}], on_token=answer_tokens.append)
    assert response.message["reasoning_content"] == "provider continuation context"
    assert response.message["content"] == "answer"
    assert answer_tokens == ["answer"]


def test_raw_invalid_tool_arguments_are_retained_for_durable_rejection():
    tc = SimpleNamespace(index=0, id="provider-A", function=SimpleNamespace(name="write_file", arguments='{"content":'))
    response = stream_client([chunk(tool_calls=[tc])]).chat([{"role": "user", "content": "write"}])
    assert response.message["tool_calls"][0]["id"] == "provider-A"
    assert response.message["tool_calls"][0]["function"]["arguments"] == '{"content":'


def test_nonreasoning_provider_messages_remain_compatible():
    response = stream_client([chunk(content="answer")]).chat([])
    assert response.message == {"role": "assistant", "content": "answer"}


def test_durable_openai_sdk_transport_has_no_automatic_retry(monkeypatch):
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    import pytest
    from openai import APIConnectionError, OpenAI

    from corecoder import llm as llm_module

    mode = getattr(llm_module, "durable_model_request", None)
    assert mode is not None, "durable requests need an explicit retry boundary"
    attempts = []

    def handle(request):
        attempts.append(request)
        raise httpx.ConnectError("synthetic transport loss", request=request)

    client = OpenAI(api_key="synthetic-key", base_url="https://model.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    llm = object.__new__(LLM)
    llm.model, llm.client, llm.extra = "model", client, {}
    llm.total_prompt_tokens = llm.total_completion_tokens = 0
    monkeypatch.setattr("corecoder.llm.time.sleep", lambda seconds: None)
    try:
        with mode(), pytest.raises(APIConnectionError):
            llm.chat([])
        assert len(attempts) == 1
        assert client.max_retries == 2
    finally:
        client.close()


def test_legacy_openai_helper_keeps_transient_retry(monkeypatch):
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    from openai import OpenAI

    attempts = []

    def handle(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("synthetic transport loss", request=request)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"recovered"}}]}\n\ndata: [DONE]\n\n',
                               headers={"content-type": "text/event-stream"}, request=request)

    client = OpenAI(api_key="synthetic-key", base_url="https://model.invalid/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    llm = object.__new__(LLM)
    llm.model, llm.client, llm.extra = "model", client, {}
    llm.total_prompt_tokens = llm.total_completion_tokens = 0
    monkeypatch.setattr("corecoder.llm.time.sleep", lambda seconds: None)
    try:
        assert llm.chat([]).content == "recovered"
        assert len(attempts) == 2
    finally:
        client.close()


def test_litellm_durable_disables_helper_provider_and_global_retry_policy(monkeypatch):
    import sys
    from types import ModuleType

    import pytest

    from corecoder import llm as llm_module
    from corecoder.llm import LiteLLM

    mode = getattr(llm_module, "durable_model_request", None)
    assert mode is not None, "durable requests need an explicit retry boundary"
    library = ModuleType("litellm")
    attempts = []
    library.num_retries = 4

    def completion(**params):
        attempts.append(params)
        retries = params.get("num_retries") or library.num_retries
        policy = params.get("retry_policy")
        if policy is not None:
            retries = policy["DefaultRetries"]
        if retries or params.get("max_retries") != 0:
            return iter([chunk(content="hidden retry succeeded")])
        raise ConnectionError("provider connection outcome unknown")

    library.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", library)
    monkeypatch.setattr("corecoder.llm.time.sleep", lambda seconds: None)
    llm = LiteLLM("openai/model", num_retries=8, max_retries=7)
    with mode(), pytest.raises(ConnectionError):
        llm.chat([])
    assert len(attempts) == 1
    assert attempts[0]["num_retries"] == 0
    assert attempts[0]["max_retries"] == 0
    assert library.num_retries == 4
    assert llm.chat([]).content == "hidden retry succeeded"
    assert len(attempts) == 2


def test_durable_confirmed_stream_options_rejection_can_fallback(monkeypatch):
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    from openai import OpenAI

    from corecoder import llm as llm_module

    mode = getattr(llm_module, "durable_model_request", None)
    assert mode is not None, "durable requests need an explicit retry boundary"
    requests = []

    def handle(request):
        import json

        body = json.loads(request.content)
        requests.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "unsupported stream_options", "param": "stream_options"}}, request=request)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"accepted"}}]}\n\ndata: [DONE]\n\n',
                               headers={"content-type": "text/event-stream"}, request=request)

    client = OpenAI(api_key="synthetic-key", base_url="https://model.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    llm = object.__new__(LLM)
    llm.model, llm.client, llm.extra = "model", client, {}
    llm.total_prompt_tokens = llm.total_completion_tokens = 0
    try:
        with mode():
            assert llm.chat([]).content == "accepted"
        assert len(requests) == 2
        assert "stream_options" not in requests[1]
    finally:
        client.close()
