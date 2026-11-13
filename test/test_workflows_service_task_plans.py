"""Workflow service delegation to saved task plans."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from test_workflows_service import FakeSessions

from kiro_crew.workflows.library import WorkflowDefinitionLibrary
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio


async def test_start_task_plan_definition_delegates_to_taskrunner_without_python_execution(
    tmp_path,
) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(
        source="agents:\n  test:\n    prompt: run tests\n",
        name="Test project",
        source_format="task-plan",
    )

    class FakeTaskRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def start_workflow_definition(self, definition, **kwargs):
            self.calls.append({"definition": definition, **kwargs})
            return {"task_id": "task_123", "run_id": "wf_task"}

    task_runner = FakeTaskRunner()
    svc = WorkflowService(
        sessions=FakeSessions([]),
        persist=False,
        definition_library=library,
        task_runner=task_runner,
    )

    started = await svc.start_definition(saved["slug"], input_text="from slash")

    assert started["run_id"] == "wf_task"
    assert started["task_id"] == "task_123"
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    assert task_runner.calls == [
        {
            "definition": saved,
            "input_text": "from slash",
            "author": "",
            "session_key": "",
            "execution_context": ExecutionContext(
                member_id=None,
                store=MemoryStoreRef("default"),
                selection_kind="template",
                template_id="kirocrew",
            ),
        }
    ]


async def test_disabled_task_runner_app_refuses_a_saved_task_plan(tmp_path) -> None:
    # Task Runner plans and runs a saved task plan, so its Library switch applies.
    library = WorkflowDefinitionLibrary(tmp_path)
    saved = library.create(
        source="agents:\n  test:\n    prompt: run tests\n",
        name="Test project",
        source_format="task-plan",
    )
    task_runner = SimpleNamespace(start_workflow_definition=AsyncMock())
    svc = WorkflowService(
        sessions=FakeSessions([]),
        persist=False,
        definition_library=library,
        task_runner=task_runner,
    )

    with patch(
        "kiro_crew.apps.manager._read_installed",
        return_value=SimpleNamespace(enabled=False),
    ) as read:
        started = await svc.start_definition(saved["slug"], input_text="from slash")

    assert started == {
        "error": "Task Runner is disabled. Enable it in Library to start tasks.",
        "app_disabled": True,
    }
    read.assert_called_once_with("projects")
    task_runner.start_workflow_definition.assert_not_awaited()
