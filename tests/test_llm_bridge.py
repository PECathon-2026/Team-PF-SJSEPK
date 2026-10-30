from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

import autonomous_coder
from devpilot import config, llm_bridge
from devpilot.codegen import generate_patch, redact_secrets
from devpilot.orchestrator import run_mission
from devpilot.repository_tools import RepositoryTools


@pytest.fixture(autouse=True)
def isolate_dotenv(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda **_kwargs: None)


def test_cli_prompts_for_missing_gemini_key_without_echo_or_persistence(monkeypatch):
    entered = []
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(autonomous_coder.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        autonomous_coder,
        "getpass",
        lambda prompt: entered.append(prompt) or "local-test-key",
    )

    autonomous_coder._ensure_gemini_key()

    assert os.environ["GEMINI_API_KEY"] == "local-test-key"
    assert entered == ["Gemini API key (input hidden; not saved): "]


def test_cli_explains_missing_key_when_noninteractive(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(autonomous_coder.sys.stdin, "isatty", lambda: False)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        autonomous_coder._ensure_gemini_key()


def test_gemini_configuration_uses_environment_and_central_model(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-secret")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")

    loaded = config.load_gemini_config()

    assert loaded.api_key == "unit-test-gemini-secret"
    assert loaded.key_configured
    assert loaded.model == "gemini-test-model"
    assert "unit-test-gemini-secret" not in repr(loaded)


def test_gui_cli_and_backend_use_shared_configuration_loader():
    gui_source = (Path(__file__).parents[1] / "devpilot" / "gui.py").read_text(encoding="utf-8")

    assert autonomous_coder.load_gemini_config is config.load_gemini_config
    assert llm_bridge.load_gemini_config is config.load_gemini_config
    assert "from devpilot.config import load_gemini_config" in gui_source


def test_gemini_configuration_loads_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=dotenv-test-key\nGEMINI_MODEL=gemini-dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(config, "load_dotenv", load_dotenv)

    loaded = config.load_gemini_config(env_file=env_file)

    assert loaded.api_key == "dotenv-test-key"
    assert loaded.model == "gemini-dotenv-model"


def test_gemini_uses_configured_key_and_model(monkeypatch):
    observed = {}
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-secret")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "generated response"}]}}]}

    def fake_post(url, *, headers, json, timeout):
        observed.update(url=url, headers=headers, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr(llm_bridge.requests, "post", fake_post)
    assert llm_bridge.ask_gemini("hello") == "generated response"
    assert observed["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-test-model:generateContent"
    )
    assert observed["headers"]["X-goog-api-key"] == "unit-test-gemini-secret"
    assert observed["payload"]["contents"] == [{"parts": [{"text": "hello"}]}]


def test_gemini_requires_a_key_with_actionable_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        llm_bridge.ask_gemini("hello")


def test_gemini_redacts_key_and_reports_http_status(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-secret")

    class Response:
        status_code = 403
        text = "invalid key unit-test-gemini-secret"

        def raise_for_status(self):
            raise llm_bridge.requests.HTTPError("bad response")

    monkeypatch.setattr(llm_bridge.requests, "post", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError) as error:
        llm_bridge.ask_gemini("hello")
    assert "unit-test-gemini-secret" not in str(error.value)
    assert "[redacted]" in str(error.value)
    assert "403" in str(error.value)


def test_gemini_retries_rate_limits_with_bounded_backoff(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-secret")
    statuses = iter([429, 200])
    sleeps = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = "rate limited"

        def raise_for_status(self):
            if self.status_code != 200:
                raise llm_bridge.requests.HTTPError("rate limited")

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ready"}]}}]}

    monkeypatch.setattr(
        llm_bridge.requests,
        "post",
        lambda *args, **kwargs: Response(next(statuses)),
    )
    monkeypatch.setattr(llm_bridge.time, "sleep", sleeps.append)
    assert llm_bridge.ask_gemini("hello") == "ready"
    assert sleeps == [1]


def test_codegen_uses_retry_feedback_and_redacts_repository_secrets(tmp_path, monkeypatch):
    root = tmp_path / "custom-repository"
    root.mkdir()
    (root / "settings.py").write_text(
        'api_key = "private-test-value-123456"\nanswer = 41\n',
        encoding="utf-8",
    )
    (root / ".env.local").write_text("GEMINI_API_KEY=should-never-be-sent\n", encoding="utf-8")
    observed = {}

    def fake_ask(prompt):
        observed["prompt"] = prompt
        return "--- a/settings.py\n+++ b/settings.py\n@@ -2 +2 @@\n-answer = 41\n+answer = 42"

    monkeypatch.setattr("devpilot.codegen.ask_gemini", fake_ask)
    patch = generate_patch(
        RepositoryTools(root),
        {
            "description": "Set answer to 42",
            "acceptance_criteria": ["answer is 42"],
            "relevant_files": ["settings.py", ".env.local"],
            "retry_feedback": {"test_result": {"stderr": "assert 41 == 42"}},
        },
        "Set answer to 42",
    )
    assert patch.endswith("+answer = 42\n")
    assert "assert 41 == 42" in observed["prompt"]
    assert "private-test-value-123456" not in observed["prompt"]
    assert "should-never-be-sent" not in observed["prompt"]
    assert "[REDACTED]" in observed["prompt"]


def test_redact_secrets_handles_common_api_keys():
    source = "GEMINI_API_KEY=sk-" + ("a" * 24)
    assert "sk-" not in redact_secrets(source)


def test_no_legacy_provider_configuration_remains_in_project_sources():
    root = Path(__file__).parents[1]
    ignored_parts = {
        ".git", ".venv", "__pycache__", ".pytest_cache",
        ".pytest-tmp-debug1", ".pytest_local_temp", "state",
    }
    checked_suffixes = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".example", ".json"}
    secret_names = {".env", "secrets.toml"}
    legacy_provider_name = "Open" + "Router"
    matches = []
    for current, dirs, filenames in os.walk(root):
        dirs[:] = [name for name in dirs if name not in ignored_parts]
        for filename in filenames:
            if filename.casefold() in secret_names:
                continue
            path = Path(current) / filename
            if path.suffix.casefold() not in checked_suffixes:
                continue
            contents = path.read_text(encoding="utf-8", errors="ignore")
            if legacy_provider_name.casefold() in contents.casefold():
                matches.append(str(path.relative_to(root)))
    assert matches == []


def test_llm_patch_loop_revises_from_real_test_failure(tmp_path, monkeypatch):
    root = tmp_path / "custom-repository"
    root.mkdir()
    (root / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (root / "test_sample.py").write_text(
        "import sample\n\ndef test_value():\n    assert sample.value == 2\n",
        encoding="utf-8",
    )
    prompts = []
    responses = iter([
        "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-value = 1\n+value = 3",
        "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-value = 3\n+value = 2",
    ])

    def fake_ask(prompt):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("devpilot.codegen.ask_gemini", fake_ask)
    repository = RepositoryTools(root)
    mission = run_mission(
        "Set value to 2",
        lambda task, _attempt: generate_patch(repository, task, "Set value to 2"),
        lambda *_: True,
        repository=repository,
        state_dir=tmp_path / "state",
        max_retries=1,
    )
    assert mission["status"] == "completed"
    assert [result["passed"] for result in mission["test_results"]] == [False, True]
    assert "AssertionError" in prompts[1] or "assert 3 == 2" in prompts[1]


def test_gemini_generates_and_applies_code_in_a_tested_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-secret")
    root = tmp_path / "gemini-repository"
    root.mkdir()
    (root / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (root / "test_sample.py").write_text(
        "import sample\n\ndef test_greeting():\n"
        "    assert sample.greeting('Ada') == 'Hello, Ada!'\n",
        encoding="utf-8",
    )
    patch = (
        "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1,4 @@\n"
        " value = 1\n+\n+def greeting(name):\n"
        "+    return f'Hello, {name}!'\n"
    )
    observed = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": patch}]}}]}

    def fake_post(url, *, headers, json, timeout):
        observed.update(url=url, headers=headers, payload=json)
        return Response()

    monkeypatch.setattr(llm_bridge.requests, "post", fake_post)
    repository = RepositoryTools(root)
    mission = run_mission(
        "Add greeting(name) to sample.py.",
        lambda task, attempt: generate_patch(
            repository,
            task,
            "Add greeting(name) to sample.py.",
            attempt=attempt,
        ),
        lambda *_: True,
        repository=repository,
        state_dir=tmp_path / "state",
    )

    assert mission["status"] == "completed"
    assert mission["test_results"][-1]["passed"] is True
    assert "def greeting(name):" in (root / "sample.py").read_text(encoding="utf-8")
    assert observed["headers"]["X-goog-api-key"] == "unit-test-gemini-secret"
