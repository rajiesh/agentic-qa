from agentic_qa.core.models import (
    QARun,
    TechStack,
    TestPlan,
    TestPlanEntry,
    TestScope,
)


def test_tech_stack_defaults():
    ts = TechStack()
    assert ts.languages == []
    assert ts.api_style is None


def test_test_plan_roundtrip():
    plan = TestPlan(
        repo_url="https://github.com/example/app",
        tech_stack=TechStack(languages=["Python"], frameworks=["FastAPI"]),
        entries=[
            TestPlanEntry(
                test_type="functional",
                priority="high",
                scope=TestScope(description="Test API endpoints"),
                suggested_framework="pytest",
                rationale="FastAPI app needs endpoint coverage",
            )
        ],
    )
    data = plan.model_dump_json()
    restored = TestPlan.model_validate_json(data)
    assert restored.repo_url == plan.repo_url
    assert len(restored.entries) == 1
    assert restored.entries[0].test_type == "functional"


def test_qa_run_defaults():
    run = QARun(repo_url="https://example.com/repo")
    assert run.success is False
    assert run.specialist_results == []
    assert run.test_plan is None
