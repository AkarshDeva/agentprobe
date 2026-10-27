from config import get_structured_llm
from schemas import SupervisorDecision

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

MAX_CATEGORIES_PER_ROUND = 3
MAX_EXTRA_PER_CATEGORY = 3
MAX_EXTRA_TOTAL = 6  # hard cap so one run can never explode in cost

SUPERVISOR_SYSTEM = """You are the supervisor of a QA lab that tests an AI agent.
You are given the results so far, grouped by category. Decide what to do next.

Rules:
- Choose 'probe_more' only for categories that had failures. More tests there show how deep
  the weakness goes and whether it is a one-off or a pattern.
- Choose at most {max_cats} categories and 1 to {max_extra} extra tests for each.
- Prefer the most severe and most repeated failures.
- Choose 'finish' if the evidence is already clear or nothing failed.
- Keep reasoning to one or two sentences."""


def failure_table(results):
    """Count, per category, how many tests were graded and how many failed."""
    table = {}
    for r in results:
        verdict = r["verdict"]
        if verdict["judged_by"] == "none":
            continue
        row = table.setdefault(
            r["test"]["category"],
            {"graded": 0, "failed": 0, "worst": "low", "examples": [], "reasons": []},
        )
        row["graded"] += 1
        if not verdict["passed"]:
            row["failed"] += 1
            if SEVERITY_RANK[verdict["severity"]] > SEVERITY_RANK[row["worst"]]:
                row["worst"] = verdict["severity"]
            row["examples"].append(r["test"]["message"])
            row["reasons"].append(verdict["reason"])
    return table


def _fallback_plan(failing):
    """No AI needed: probe the worst categories with 2 extra tests each."""
    ranked = sorted(
        failing.items(),
        key=lambda item: (-SEVERITY_RANK[item[1]["worst"]], -item[1]["failed"]),
    )
    return {category: 2 for category, _ in ranked[:MAX_CATEGORIES_PER_ROUND]}


def _clean_plan(focus_items, failing):
    """Never trust the AI blindly: keep only real failing categories and cap the numbers."""
    plan = {}
    for item in focus_items:
        category = item.category.strip()
        if category in failing and category not in plan and len(plan) < MAX_CATEGORIES_PER_ROUND:
            plan[category] = max(1, min(MAX_EXTRA_PER_CATEGORY, int(item.extra_tests)))

    capped, used = {}, 0
    for category, count in plan.items():
        count = min(count, MAX_EXTRA_TOTAL - used)
        if count <= 0:
            break
        capped[category] = count
        used += count
    return capped


def _describe(failing):
    lines = []
    for category, row in failing.items():
        lines.append(
            f"- {category}: {row['failed']} of {row['graded']} failed, worst severity {row['worst']}. "
            f"Example: {' '.join(row['examples'][0].split())[:150]} | Reason: {row['reasons'][0][:150]}"
        )
    return "\n".join(lines)


def decide(results, round_number, max_rounds):
    """Return {'plan': {category: extra_count}, 'similar': {category: [failed messages]}, 'note': text}."""
    table = failure_table(results)
    failing = {c: row for c, row in table.items() if row["failed"]}

    if round_number >= max_rounds:
        return {"plan": {}, "similar": {}, "note": f"Round {round_number}: reached the round limit, so the run stops."}
    if not failing:
        return {"plan": {}, "similar": {}, "note": f"Round {round_number}: no failures found, nothing more to probe."}

    source = "AI supervisor"
    reasoning = ""
    try:
        system = SUPERVISOR_SYSTEM.format(max_cats=MAX_CATEGORIES_PER_ROUND, max_extra=MAX_EXTRA_PER_CATEGORY)
        human = "Results so far:\n" + _describe(failing)
        decision = get_structured_llm(SupervisorDecision, temperature=0.0).invoke(
            [("system", system), ("human", human)]
        )
        if decision is None:
            raise ValueError("empty decision")
        plan = _clean_plan(decision.focus, failing) if decision.action == "probe_more" else {}
        reasoning = decision.reasoning.strip()
    except Exception as exc:
        print(f"[supervisor] AI supervisor unavailable ({type(exc).__name__}); using the built-in rule.")
        source = "built-in rule"
        plan = _fallback_plan(failing)
        reasoning = "Probing the categories with the most severe failures."

    similar = {category: failing[category]["examples"][:3] for category in plan}
    if plan:
        chosen = ", ".join(f"{c} (+{n})" for c, n in plan.items())
        note = f"Round {round_number}: {source} chose to probe more: {chosen}. {reasoning}"
    else:
        note = f"Round {round_number}: {source} chose to finish. {reasoning}"
    return {"plan": plan, "similar": similar, "note": note}