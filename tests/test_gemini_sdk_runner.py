from __future__ import annotations

from io import StringIO
import sys
from types import ModuleType, SimpleNamespace

from orch import gemini_sdk_runner


def test_gemini_sdk_runner_makes_one_generate_content_call(
    monkeypatch,
    capsys,
) -> None:
    calls: list[dict] = []

    class FakeModels:
        @staticmethod
        def generate_content(*, model, contents):
            calls.append({"model": model, "contents": contents})
            return SimpleNamespace(text='ORCH_HANDOFF: {"action": "planned"}')

    class FakeClient:
        def __init__(self, *, api_key):
            calls.append({"api_key": api_key})
            self.models = FakeModels()

    google = ModuleType("google")
    genai = ModuleType("google.genai")
    genai.Client = FakeClient
    google.genai = genai

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setattr(sys, "stdin", StringIO("Plan this request."))

    assert gemini_sdk_runner.main([]) == 0

    assert calls == [
        {"api_key": "test-key"},
        {"model": "gemini-test", "contents": "Plan this request."},
    ]
    assert capsys.readouterr().out == 'ORCH_HANDOFF: {"action": "planned"}\n'


def test_gemini_sdk_runner_version_does_not_require_auth(capsys) -> None:
    assert gemini_sdk_runner.main(["--version"]) == 0
    assert "gemini_sdk_runner" in capsys.readouterr().out
