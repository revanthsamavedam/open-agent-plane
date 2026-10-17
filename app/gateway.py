"""BYO model gateway + model certification gate.

We don't sell you a model. Bring yours: set ``AGENTPLANE_MODEL`` (or the
model in Settings) to any Pydantic AI model string — e.g. ``openai:gpt-4o``,
``azure:...``, ``anthropic:...``, ``bedrock:...``, ``google:...``,
``ollama:...``, or an OpenAI-compatible endpoint via the model's own
configuration. Unset = the offline TestModel placeholder, always labelled
as a placeholder and never presented as a real certification.
"""
from __future__ import annotations

from typing import Any

from .models import EvalResult
from .store import Store, audit

TEST_MODEL_LABEL = "test-model (placeholder)"


def effective_model(store: Store) -> str:
    return store.get_settings().model or ""


def is_placeholder(model: str) -> bool:
    return not model or model.strip().lower() in {"test", "test-model", "placeholder"}


def _build_agent(model: str):
    """Construct a Pydantic AI agent for the configured model string."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    if is_placeholder(model):
        return Agent(TestModel())
    return Agent(model)


def generate(store: Store, prompt: str) -> tuple[str, str, bool]:
    """Run the configured model once. Returns (text, model_label, placeholder).
    Model failures propagate to the caller (runs record them as failed)."""
    model = effective_model(store)
    placeholder = is_placeholder(model)
    label = TEST_MODEL_LABEL if placeholder else model
    agent = _build_agent(model)
    result = agent.run_sync(prompt)
    text = getattr(result, "output", None) or str(getattr(result, "data", ""))
    return str(text), label, placeholder


def evaluate_model(store: Store, actor: str, model: str | None = None) -> list[EvalResult]:
    """Certification gate: a small eval suite with programmatic checks,
    run per skill. A model must pass before production agents may use it
    for that skill; swapping the model string re-triggers this eval.
    TestModel results are labelled placeholder and never count as a real
    certification."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    model = model if model is not None else effective_model(store)
    placeholder = is_placeholder(model)
    label = TEST_MODEL_LABEL if placeholder else model

    checks: dict[str, bool] = {}
    detail = ""
    # Check 1: model constructs.
    try:
        agent_model = TestModel() if placeholder else model
        checks["constructs"] = True
    except Exception as exc:  # pragma: no cover - defensive
        checks["constructs"] = False
        detail = f"construction failed: {exc}"

    # Check 2: answers a simple prompt with non-empty output.
    # Check 3: calls a tool when one is offered (TestModel does by default).
    tool_called = {"hit": False}
    if checks.get("constructs"):
        try:
            agent = Agent(agent_model)

            @agent.tool_plain
            def lookup_records(query: str) -> str:
                """Look up records for a query."""
                tool_called["hit"] = True
                return "alpha, beta, gamma"

            result = agent.run_sync(
                "Use the lookup_records tool to find records about alpha, then answer."
            )
            text = str(getattr(result, "output", "") or getattr(result, "data", ""))
            checks["responds"] = bool(text.strip())
            checks["tool_calling"] = bool(tool_called["hit"])
        except Exception as exc:
            checks.setdefault("responds", False)
            checks.setdefault("tool_calling", False)
            detail = f"eval run failed: {type(exc).__name__}: {exc}"[:300]

    passed = all(checks.get(k) for k in ("constructs", "responds", "tool_calling"))
    results: list[EvalResult] = []
    from .skills import load_skills

    for skill_name in load_skills():
        res = EvalResult(model=label, skill=skill_name, passed=passed,
                         placeholder=placeholder, checks=dict(checks),
                         detail=detail or ("placeholder eval (offline TestModel)"
                                            if placeholder else "eval passed"))
        store.put_eval(res)
        results.append(res)
    audit(store, "model_eval", actor=actor,
          summary=f"model eval for {label}: {'passed' if passed else 'failed'}"
                  + (" (placeholder)" if placeholder else ""),
          detail=f"model={label} passed={passed} placeholder={placeholder}")
    if passed and not placeholder:
        audit(store, "model_certified", actor=actor,
              summary=f"model {label} certified for production agents",
              detail=f"model={label}")
    return results


def model_gate(store: Store, model_label: str, placeholder: bool, skill: str) -> tuple[bool, str]:
    """Production runs need a passing, non-placeholder eval for the skill —
    unless the run itself is on the placeholder model (demo mode, labelled)."""
    if placeholder:
        return True, "placeholder model: demo run, not a production certification"
    for res in store.all_evals():
        if res.skill == skill and res.model == model_label and res.passed and not res.placeholder:
            return True, "model certified for this skill"
    return False, (f"model {model_label} has not passed the certification eval "
                   f"for skill '{skill}' — run the eval in Settings first")
