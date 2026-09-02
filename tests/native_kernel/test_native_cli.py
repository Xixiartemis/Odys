import asyncio

from typer.testing import CliRunner

from lhas.cli import app
from lhas.cli_runtime import ProductRuntime, decode_cli_config
from lhas.domain.enums import EventType
from lhas.native.models import ModelContext
from lhas.native.provider import OpenAIChatProviderAdapter
from lhas.persistence.event_store import EventStore
from lhas.persistence.repositories import AttemptRepository, RunRepository, TaskRepository


def test_cli_native_path_owns_loop_and_acceptance(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    test_file = repo / "test_pass.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    before = test_file.read_bytes()
    db_path = tmp_path / "native.db"
    result = CliRunner().invoke(app, [
        "run", "Verify the already-correct repository through native completion acceptance",
        "--repo", str(repo),
        "--verify", "pytest -q",
        "--provider", "offline",
        "--kernel", "native",
        "--no-ui",
        "--yes",
        "--db", str(db_path),
    ])
    assert result.exit_code == 0, result.output
    assert "RESULT: PASS" in result.output
    assert test_file.read_bytes() == before

    runtime = ProductRuntime(db_path)
    task = TaskRepository(runtime.db).list()[0]
    run = RunRepository(runtime.db).list_for_task(task.id)[0]
    attempt = AttemptRepository(runtime.db).list_for_run(run.id)[0]
    event_types = [item.event_type for item in EventStore(runtime.db).list_for_run(run.id)]
    config = decode_cli_config(task)
    runtime.close()
    assert run.executor_type == "NativeAgentExecutor"
    assert attempt.status.value == "COMPLETED"
    assert config["kernel"] == "native"
    assert EventType.NATIVE_MODEL_TURN_STARTED in event_types
    assert EventType.COMPLETION_CANDIDATE_ACCEPTED in event_types


def test_native_real_provider_adapter_is_single_api_call_only():
    class Completions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return {"choices": [{"message": {"content": "candidate", "tool_calls": []}}]}

    class Client:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": Completions()})()

    client = Client()
    adapter = OpenAIChatProviderAdapter(model="real-model", api_key="not-persisted", base_url="https://provider.invalid/v1", client=client)
    context = ModelContext(messages=[{"role": "user", "content": "goal"}], sections={}, chars_used=4, budget_chars=1000)
    response = asyncio.run(adapter.generate(context=context, tools=[], timeout_seconds=1))
    assert response["choices"][0]["message"]["content"] == "candidate"
    assert len(client.chat.completions.calls) == 1
    assert client.chat.completions.calls[0]["model"] == "real-model"
    assert "max_turns" not in client.chat.completions.calls[0]


def test_native_kernel_mode_is_persisted_for_fresh_resume(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    runtime = ProductRuntime(tmp_path / "persist.db")
    prepared = runtime.prepare_new(goal="verify", repo=repo, verify_argv=["pytest", "-q"], max_attempts=2, max_turns=4, provider="offline", kernel="native")
    assert decode_cli_config(prepared.task)["kernel"] == "native"
    assert prepared.kernel == "native"
    runtime.close()
