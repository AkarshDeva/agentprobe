import argparse
import json
import operator
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.judge import judge_graph
from agents.reporter import build_report
from agents.supervisor import decide
from agents.test_writer import write_tests
from config import CATEGORIES, MAX_ROUNDS, TARGET_URL, TESTS_PER_CATEGORY
from targets.http_target import send_to_target

REPORT_DIR = Path("reports")
RETRY_STATUSES = {429, 503}  # "busy" answers: worth one more try
RETRY_WAIT_SECONDS = 20
PAUSE_BETWEEN_TESTS = 2  # be kind to free-tier limits

# The web page registers a listener here to watch a run live. {run_id: function}
LISTENERS = {}


def emit(state, kind, **data):
    """Tell the web page (if it is watching) what just happened. Never breaks a run."""
    listener = LISTENERS.get(state.get("run_id"))
    if listener:
        try:
            listener({"kind": kind, **data})
        except Exception:
            pass


class LabState(TypedDict, total=False):
    run_id: str
    target_url: str
    categories: list
    per_category: int
    round: int  # which round we are in (1, 2, ...)
    max_rounds: int
    plan: dict  # {category: how many tests to write this round}
    similar: dict  # {category: failed messages to write variations of}
    tests: list  # tests of the CURRENT round
    pending: list  # replies of the current round, not graded yet
    results: Annotated[list, operator.add]  # ALL graded results, every round (adds up)
    notes: Annotated[list, operator.add]  # the supervisor's decisions (adds up)
    report: dict


# ---------- Worker 1: invent the tests ----------
def _plan_event(state, tests):
    emit(
        state,
        "plan",
        round=state["round"],
        tests=[{"id": t["id"], "category": t["category"]} for t in tests],
    )


def generate_tests(state):
    round_number = state["round"]
    emit(state, "stage", name="WRITING TESTS", round=round_number)

    # Replay mode: the tests were loaded from a saved run, so do not invent new ones.
    if round_number == 1 and state.get("tests"):
        print(f"[generate] replaying {len(state['tests'])} saved tests")
        _plan_event(state, state["tests"])
        return {}

    plan = state.get("plan") or {c: state["per_category"] for c in state["categories"]}
    similar = state.get("similar") or {}
    used = [r["test"]["message"] for r in state.get("results", [])]

    tests = []
    for category, count in plan.items():
        already = sum(1 for r in state.get("results", []) if r["test"]["category"] == category)
        made = write_tests(
            category,
            count,
            existing=used,
            start_index=already + 1,
            similar_to=similar.get(category),
        )
        for t in made:
            item = t.model_dump()
            item["round"] = round_number
            tests.append(item)
        print(f"[generate] round {round_number} {category}: {len(made)} tests")

    if not tests:
        raise RuntimeError(
            "No tests could be created. Check GROQ_API_KEY and GROQ_MODEL in your .env file."
        )
    _plan_event(state, tests)
    return {"tests": tests}


# ---------- Worker 2: fire them at the target ----------
def run_target(state):
    url = state["target_url"]
    tests = state["tests"]
    pending = []
    emit(state, "stage", name="FIRING", round=state["round"])

    for number, test in enumerate(tests, 1):
        print(f"[run {number}/{len(tests)}] {test['id']}")
        emit(state, "attack", id=test["id"], category=test["category"])
        reply = send_to_target(test["message"], url)

        if reply.status_code in RETRY_STATUSES:
            print(f"    target said HTTP {reply.status_code}; waiting {RETRY_WAIT_SECONDS}s and trying once more")
            time.sleep(RETRY_WAIT_SECONDS)
            reply = send_to_target(test["message"], url)

        if state["round"] == 1 and number == 1 and reply.status_code is None and reply.error.startswith("Could not reach"):
            raise RuntimeError(f"Cannot reach the target at {url}. Is it running?")

        emit(
            state,
            "reply",
            id=test["id"],
            status=reply.status_code,
            latency=reply.latency_seconds,
            ok=reply.ok,
        )
        pending.append({"test": test, "reply": reply.model_dump()})
        time.sleep(PAUSE_BETWEEN_TESTS)

    return {"pending": pending}


# ---------- Worker 3: grade every reply ----------
def grade_replies(state):
    graded = []
    total = len(state["pending"])
    emit(state, "stage", name="JUDGING", round=state["round"])
    for number, item in enumerate(state["pending"], 1):
        # The judge is its own graph. We call it from inside this node.
        outcome = judge_graph.invoke({"test": item["test"], "reply": item["reply"]})
        verdict = outcome["verdict"]
        graded.append({**item, "verdict": verdict})
        label = "PASS" if verdict["passed"] else "FAIL"
        if verdict["judged_by"] == "none":
            label = "SKIP"
        print(f"[grade {number}/{total}] {item['test']['id']}: {label} ({verdict['judged_by']})")
        emit(
            state,
            "verdict",
            id=item["test"]["id"],
            category=item["test"]["category"],
            passed=verdict["passed"],
            severity=verdict["severity"],
            score=verdict["score"],
            judged_by=verdict["judged_by"],
        )
    # "results" ADDS to the old list. "pending" is replaced (cleared).
    return {"results": graded, "pending": []}


# ---------- Worker 4: the supervisor ----------
def supervisor(state):
    decision = decide(state["results"], state["round"], state["max_rounds"])
    print(f"[supervisor] {decision['note']}")
    emit(state, "supervisor", note=decision["note"], round=state["round"])
    return {
        "plan": decision["plan"],
        "similar": decision["similar"],
        "notes": [decision["note"]],
        "round": state["round"] + 1,
    }


def after_supervisor(state):
    """Keep going if the supervisor asked for more tests, otherwise write the report."""
    return "generate_tests" if state.get("plan") else "write_report"


# ---------- Worker 5: write the report ----------
def write_report(state):
    emit(state, "stage", name="WRITING REPORT", round=state["round"])
    return {"report": build_report(state["results"], state["target_url"], state.get("notes"))}


def build_lab_graph():
    graph = StateGraph(LabState)
    graph.add_node("generate_tests", generate_tests)
    graph.add_node("run_target", run_target)
    graph.add_node("grade_replies", grade_replies)
    graph.add_node("supervisor", supervisor)
    graph.add_node("write_report", write_report)

    graph.add_edge(START, "generate_tests")
    graph.add_edge("generate_tests", "run_target")
    graph.add_edge("run_target", "grade_replies")
    graph.add_edge("grade_replies", "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        after_supervisor,
        {"generate_tests": "generate_tests", "write_report": "write_report"},
    )
    graph.add_edge("write_report", END)
    return graph.compile()


lab_graph = build_lab_graph()


def save_report(final_state, label):
    REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = REPORT_DIR / f"{stamp}-{label}"
    md_path = base.with_suffix(".md")
    json_path = base.with_suffix(".json")
    md_path.write_text(final_state["report"]["markdown"], encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "label": label,
                "target_url": final_state["target_url"],
                "summary": final_state["report"]["summary"],
                "notes": final_state.get("notes", []),
                "results": final_state["results"],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return md_path, json_path


def load_core_tests(path):
    """Read the round-1 tests from a saved run, so the SAME tests can be run again."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [r["test"] for r in data["results"] if r["test"].get("round", 1) == 1]


def run_lab(
    label="run",
    categories=None,
    per_category=None,
    target_url=None,
    max_rounds=None,
    replay=None,
    run_id="",
):
    categories = categories or list(CATEGORIES)
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        raise ValueError(f"Unknown categories: {unknown}. Choose from {list(CATEGORIES)}")

    start = {
        "run_id": run_id,
        "target_url": target_url or TARGET_URL,
        "categories": categories,
        "per_category": per_category or TESTS_PER_CATEGORY,
        "round": 1,
        "max_rounds": max_rounds or MAX_ROUNDS,
        "results": [],
        "notes": [],
    }
    if replay:
        start["tests"] = load_core_tests(replay)
        start["max_rounds"] = 1  # a replay is a fair re-test, so no extra probing
        if not start["tests"]:
            raise ValueError(f"No saved tests found in {replay}")

    final = lab_graph.invoke(start)
    try:
        md_path, json_path = save_report(final, label)
    except OSError as exc:  # for example a read-only disk
        print(f"[lab] could not save the report files: {exc}")
        md_path = json_path = "(not saved)"

    summary = final["report"]["summary"]
    core = summary["core"]
    core_rate = "n/a" if core["pass_rate"] is None else f"{core['pass_rate']}%"
    print("\n========== DONE ==========")
    print(f"Core suite: {core['passed']} of {core['graded']} passed ({core_rate})")
    print(f"All tests:  {summary['passed']} of {summary['graded']} passed, {summary['deep_probe_tests']} deep-probe tests")
    print(f"Report: {md_path}")
    print(f"Data:   {json_path}")
    return final


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run AgentProbe against a target agent.")
    parser.add_argument("--label", default="baseline", help="a name for this run, e.g. baseline or after-fix")
    parser.add_argument("--per", type=int, default=None, help="tests per category in round 1")
    parser.add_argument("--only", default=None, help="comma-separated categories, e.g. unsafe,privacy")
    parser.add_argument("--url", default=None, help="target URL (default: TARGET_URL in .env)")
    parser.add_argument("--rounds", type=int, default=None, help="max rounds (default: MAX_ROUNDS in .env)")
    parser.add_argument("--replay", default=None, help="path of a saved .json report: re-run its round-1 tests")
    args = parser.parse_args()

    chosen = [c.strip() for c in args.only.split(",")] if args.only else None
    run_lab(
        label=args.label,
        categories=chosen,
        per_category=args.per,
        target_url=args.url,
        max_rounds=args.rounds,
        replay=args.replay,
    )