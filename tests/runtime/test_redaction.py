"""Runtime facts never persist or return sensitive values in clear text."""

from corecoder.runtime.redaction import redact


def test_redact_recursively_hides_sensitive_mapping_values_without_mutating_input():
    value = {
        "token": "plain-token",
        "nested": [{"Cookie": "session-value"}],
        "safe": "ok",
    }

    assert redact(value) == {
        "token": "[REDACTED]",
        "nested": [{"Cookie": "[REDACTED]"}],
        "safe": "ok",
    }
    assert value["token"] == "plain-token"
    assert value["nested"][0]["Cookie"] == "session-value"


def test_event_storage_never_retains_plain_secret(running_store):
    running_store.record_event(
        "run-1",
        "test.secret",
        {"authorization": "Bearer secret-value", "safe": "visible"},
    )

    event = running_store.list_events("run-1")[-1]
    assert event.payload == {"authorization": "[REDACTED]", "safe": "visible"}
    assert "secret-value" not in str(event)
