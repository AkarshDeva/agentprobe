from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ---------- 1. What the Test Writer fills in ----------
class GeneratedTest(BaseModel):
    """One attack message invented by the Test Writer."""

    message: str = Field(description="The exact message to send to the target agent.")
    expected_behavior: str = Field(
        description="In one sentence: what a GOOD agent should do with this message."
    )


class GeneratedBatch(BaseModel):
    """The Test Writer's full answer: a list of attack messages."""

    tests: list[GeneratedTest]


# ---------- 2. A test case once the lab has labelled it ----------
class TestCase(BaseModel):
    __test__ = False  # tells pytest: this is not a test class

    id: str
    category: str
    message: str
    expected_behavior: str


# ---------- 3. What the target agent sent back ----------
class TargetReply(BaseModel):
    ok: bool  # did we get a normal reply?
    status_code: int | None = None
    text: str = ""
    latency_seconds: float = 0.0
    error: str = ""


# ---------- 4. The Judge's grade ----------
class JudgeAnswer(BaseModel):
    """What the Judge LLM fills in for one reply."""

    score: int = Field(description="0 = terrible, 10 = perfect.")
    passed: bool = Field(description="True if the agent behaved acceptably.")
    severity: Literal["low", "medium", "high", "critical"] = Field(
        description="How bad a failure would be. Use 'low' for passes."
    )
    reason: str = Field(description="One or two sentences explaining the grade.")

    @field_validator("score")
    @classmethod
    def keep_score_in_range(cls, value):
        return max(0, min(10, value))


class Verdict(JudgeAnswer):
    """The final grade stored in the report: the Judge's answer plus WHO decided."""

    # llm = the AI judge, rules = a hard rule decided, none = could not be graded
    judged_by: Literal["llm", "rules", "none"] = "llm"


# ---------- 5. One finished row of the report ----------
class TestResult(BaseModel):
    __test__ = False

    test: TestCase
    reply: TargetReply
    verdict: Verdict


# ---------- 6. The Supervisor's decision ----------
class FocusItem(BaseModel):
    category: str = Field(description="A category name that had failures.")
    extra_tests: int = Field(description="How many more tests to write for it (1 to 3).")


class SupervisorDecision(BaseModel):
    """What the Supervisor LLM decides after looking at the results."""

    action: Literal["probe_more", "finish"] = Field(
        description="'probe_more' to test weak areas further, 'finish' to stop."
    )
    focus: list[FocusItem] = Field(
        default_factory=list,
        description="Categories to probe further. Empty if action is 'finish'.",
    )
    reasoning: str = Field(description="One or two sentences explaining the decision.")