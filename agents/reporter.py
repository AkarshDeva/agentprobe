from config import get_llm, get_text

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Used when the AI cannot write suggestions. Always available.
FIX_HINTS = {
    "normal": "Check the tools and prompts: normal requests should always return a complete answer.",
    "edge_case": "Add input checks for impossible budgets, fake places and odd dates, and answer with honest caveats.",
    "off_topic": "Add a topic check at the start that politely redirects non-travel requests.",
    "prompt_injection": "Add an input guard that detects override attempts, never place secrets in prompts, and refuse politely.",
    "unsafe": "Add a safety check before any tool runs, and refuse harmful requests clearly.",
    "privacy": "Detect and mask phone, email and card numbers before they reach the model, and never echo them back.",
    "messy_input": "Validate empty, very long and emoji-only input early and return a friendly message instead of crashing.",
}


def _rate(passed, graded):
    return round(100 * passed / graded, 1) if graded else None


def _short(text, limit):
    return " ".join(str(text).split())[:limit]


def summarize(results):
    graded = [r for r in results if r["verdict"]["judged_by"] != "none"]
    passed = [r for r in graded if r["verdict"]["passed"]]
    failed = [r for r in graded if not r["verdict"]["passed"]]

    by_category = {}
    for r in results:
        d = by_category.setdefault(
            r["test"]["category"],
            {"total": 0, "graded": 0, "passed": 0, "failed": 0, "ungraded": 0},
        )
        d["total"] += 1
        if r["verdict"]["judged_by"] == "none":
            d["ungraded"] += 1
        else:
            d["graded"] += 1
            d["passed" if r["verdict"]["passed"] else "failed"] += 1
    for d in by_category.values():
        d["pass_rate"] = _rate(d["passed"], d["graded"])

    failed.sort(key=lambda r: (-SEVERITY_RANK[r["verdict"]["severity"]], r["verdict"]["score"]))
    times = [r["reply"]["latency_seconds"] for r in results if r["reply"].get("latency_seconds")]

    # The "core suite" is round 1 only. It is the part you can compare between runs.
    core = [r for r in graded if r["test"].get("round", 1) == 1]
    core_passed = [r for r in core if r["verdict"]["passed"]]

    return {
        "core": {"graded": len(core), "passed": len(core_passed), "pass_rate": _rate(len(core_passed), len(core))},
        "deep_probe_tests": len([r for r in results if r["test"].get("round", 1) > 1]),
        "total": len(results),
        "graded": len(graded),
        "passed": len(passed),
        "failed": len(failed),
        "ungraded": len(results) - len(graded),
        "pass_rate": _rate(len(passed), len(graded)),
        "by_category": by_category,
        "worst": failed[:5],
        "average_latency": round(sum(times) / len(times), 1) if times else None,
    }


def suggest_fixes(summary):
    """Short list of fixes. The AI writes it; if it fails, we use the built-in hints."""
    if not summary["worst"]:
        return "No failures were found in this run. That does not prove the agent is safe, only that these tests passed."

    failing = [c for c, d in summary["by_category"].items() if d["failed"]]
    fallback = "\n".join(f"- **{c}**: {FIX_HINTS.get(c, 'Review these failures.')}" for c in failing)

    evidence = "\n".join(
        f"- [{r['test']['category']}] message: {_short(r['test']['message'], 150)} | "
        f"reply: {_short(r['reply']['text'] or r['reply']['error'], 200)} | why: {r['verdict']['reason']}"
        for r in summary["worst"]
    )
    prompt = (
        "You are helping a developer fix an AI agent. These tests failed:\n"
        f"{evidence}\n\n"
        "Write 3 to 5 short, concrete bullet points telling the developer what to change. "
        "Each bullet must start with '- ' and mention the failing category. No introduction."
    )
    try:
        text = get_text(get_llm(0.2).invoke(prompt))
        return text if text else fallback
    except Exception as exc:
        print(f"[reporter] AI suggestions failed ({type(exc).__name__}); using built-in hints.")
        return fallback


def render_markdown(summary, suggestions, target_url="", notes=None):
    rate = "n/a" if summary["pass_rate"] is None else f"{summary['pass_rate']}%"
    lines = [
        "# AgentProbe Report",
        "",
        f"**Target:** {target_url or 'unknown'}",
        "",
        f"**Overall:** {summary['passed']} of {summary['graded']} graded tests passed ({rate}).",
        "",
        f"Total tests: {summary['total']} | Failed: {summary['failed']} | Not graded: {summary['ungraded']}",
    ]
    lines.append("")
    core = summary["core"]
    core_rate = "n/a" if core["pass_rate"] is None else f"{core['pass_rate']}%"
    lines.append(
        f"**Core suite (round 1, use this to compare runs):** {core['passed']} of {core['graded']} passed ({core_rate}). "
        f"Extra deep-probe tests: {summary['deep_probe_tests']}"
    )
    if summary["average_latency"] is not None:
        lines += ["", f"Average reply time: {summary['average_latency']} s"]

    lines += ["", "## Results by category", "", "| Category | Passed | Failed | Pass rate |", "|---|---|---|---|"]
    for cat, d in summary["by_category"].items():
        r = "n/a" if d["pass_rate"] is None else f"{d['pass_rate']}%"
        lines.append(f"| {cat} | {d['passed']} | {d['failed']} | {r} |")

    lines += ["", "## Worst failures", ""]
    if not summary["worst"]:
        lines.append("No failures.")
    for i, r in enumerate(summary["worst"], 1):
        v = r["verdict"]
        lines += [
            f"### {i}. [{v['severity'].upper()}] {r['test']['category']} ({r['test']['id']})",
            f"- **Message:** {_short(r['test']['message'], 200)}",
            f"- **Reply:** {_short(r['reply']['text'] or r['reply']['error'], 300)}",
            f"- **Why it failed:** {v['reason']} (decided by: {v['judged_by']})",
            "",
        ]

    if notes:
        lines += ["## How the supervisor steered this run", ""]
        lines += [f"- {n}" for n in notes]
        lines.append("")

    lines += [
        "## Suggested fixes",
        "",
        suggestions,
        "",
        "## Notes",
        "",
        "- Most grades come from an AI judge, so scores are estimates, not proof.",
        "- Hard rules (leaked keys, echoed private numbers, crashes) are checked without AI.",
        "- Tests that could not be graded are left out of the pass rate.",
        "- Deep-probe tests are chosen where failures were found, so they lower the overall rate. Compare runs using the core suite.",
    ]
    return "\n".join(lines)


def build_report(results, target_url="", notes=None):
    summary = summarize(results)
    suggestions = suggest_fixes(summary)
    return {
        "summary": summary,
        "suggestions": suggestions,
        "markdown": render_markdown(summary, suggestions, target_url, notes),
    }