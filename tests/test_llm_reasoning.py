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
