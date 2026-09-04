"""
Verifies pipeline.py's wiring end to end without spending real API calls
or touching real GitHub: fakes the LLM and the four remaining MCP tools
(get_issue, add_issue_comment, create_pull_request, update_issue), but
exercises real git against a throwaway local repo + bare "origin" for
every branch/commit/push step -- proof those really are plain git now,
not an API call in disguise. Drives the interrupt with Command(resume=...)
instead of a console, and checks the properties that actually matter:

  1. context (the real issue, the real file on disk) is fetched BEFORE a
     human ever sees a plan
  2. the coder never runs before a human approves the plan
  3. rejecting the plan to the cap aborts with zero coder calls and no
     branch ever created, locally or on the remote
  4. approving the PLAN is not the same as approving the CODE: if the
     reviewer never converges, still no branch pushed and no PR, even
     though the coder did run (repeatedly) after your approval
  5. on the happy path, the committed content actually lands on the
     remote's branch, and the "in review" label gets merged into the
     issue's EXISTING labels rather than clobbering them

Run: python3 test_wiring.py
"""

import json
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from langgraph.types import Command

import pipeline as pl


class FakeResponse:
    def __init__(self, content: str):
        self.content = content


class FakeLLM:
    def __init__(self, reviewer_verdicts=("approve",)):
        self.reviewer_verdicts = list(reviewer_verdicts)
        self.planner_calls = 0
        self.coder_calls = 0
        self.reviewer_calls = 0

    async def ainvoke(self, messages):
        system_text = messages[0].content[0]["text"]  # Anthropic: cached content block
        if "You are the Planner" in system_text:
            self.planner_calls += 1
            return FakeResponse(f"plan, attempt {self.planner_calls}")
        if "You are the Coder" in system_text:
            self.coder_calls += 1
            return FakeResponse(f"```python\ndef fixed(): pass  # attempt {self.coder_calls}\n```")
        if "You are the Reviewer" in system_text:
            verdict = self.reviewer_verdicts[self.reviewer_calls]
            self.reviewer_calls += 1
            if verdict == "approve":
                return FakeResponse("VERDICT: APPROVE\nlooks good")
            return FakeResponse("VERDICT: REJECT\nnot done yet")
        raise AssertionError("unrecognized system prompt")


class FakeTool:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, args):
        return self._handler(args)


def json_block(obj):
    return [{"type": "text", "text": json.dumps(obj)}]


def make_fake_tools(calls):
    def get_issue(args):
        calls.append(("get_issue", args))
        return json_block(
            {
                "number": args["issue_number"],
                "title": "Handle empty input",
                "body": "...",
                "labels": [{"name": "bug"}],  # pre-existing -- must survive the update
            }
        )

    def create_pull_request(args):
        calls.append(("create_pull_request", args))
        return json_block({"html_url": "https://github.com/acme/widgets/pull/7"})

    def add_issue_comment(args):
        calls.append(("add_issue_comment", args))
        return json_block({"ok": True})

    def update_issue(args):
        calls.append(("update_issue", args))
        return json_block({"ok": True})

    return {
        "get_issue": FakeTool("get_issue", get_issue),
        "create_pull_request": FakeTool("create_pull_request", create_pull_request),
        "add_issue_comment": FakeTool("add_issue_comment", add_issue_comment),
        "update_issue": FakeTool("update_issue", update_issue),
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


def make_repo(root: Path) -> tuple[Path, Path]:
    """A bare 'origin' plus a working clone with one commit on main --
    stands in for the real local clone repo_path would point at."""
    origin = root / "origin.git"
    work = root / "work"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True, text=True)
    _git(work, "checkout", "-b", "main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("placeholder\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "push", "-u", "origin", "main")
    return work, origin


@contextmanager
def temp_repo():
    with tempfile.TemporaryDirectory() as tmp:
        yield make_repo(Path(tmp))


def base_input(repo_path: Path):
    return {
        "pr_owner": "acme",
        "pr_repo": "widgets",
        "issue_number": 1,
        "repo_path": str(repo_path),
        "base_branch": "main",
        "file_path": "src/fixed.py",  # doesn't exist yet -- simulates a new file
        "plan_round": 0,
        "plan_feedback": "",
        "review_round": 0,
        "review_feedback": "",
    }


def new_config():
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


async def test_context_fetched_before_human_sees_plan():
    with temp_repo() as (work, _origin):
        calls = []
        fake = FakeLLM()
        pl.llm = fake
        pl.TOOLS.clear()
        pl.TOOLS.update(make_fake_tools(calls))
        config = new_config()

        result = await pl.app.ainvoke(base_input(work), config=config)
        payload = result["__interrupt__"][0].value
        assert "Handle empty input" in payload["issue_summary"]
        assert fake.coder_calls == 0
        print("PASS: issue fetched from GitHub MCP and shown in the interrupt payload, coder hasn't run")


async def test_approve_plan_then_approve_review_opens_pr():
    with temp_repo() as (work, origin):
        calls = []
        fake = FakeLLM(reviewer_verdicts=["approve"])
        pl.llm = fake
        pl.TOOLS.clear()
        pl.TOOLS.update(make_fake_tools(calls))
        config = new_config()

        result = await pl.app.ainvoke(base_input(work), config=config)
        assert "__interrupt__" in result
        assert fake.coder_calls == 0

        result = await pl.app.ainvoke(Command(resume={"approved": True}), config=config)
        assert "__interrupt__" not in result
        assert result["pr_url"] == "https://github.com/acme/widgets/pull/7"

        tool_names = [name for name, _ in calls]
        assert tool_names == [
            "get_issue",  # fetch_context
            "create_pull_request",
            "add_issue_comment",
            "get_issue",  # re-fetched to merge labels
            "update_issue",
        ]

        # the pre-existing "bug" label survives -- update_issue REPLACES
        # the full label set, so open_pr_node has to merge, not clobber
        update_args = calls[-1][1]
        assert update_args["labels"] == ["bug", "in review"]

        # the branch/commit/push really happened via git, not an API call
        branch = "agent/issue-1"
        committed = _git(origin, "show", f"{branch}:src/fixed.py")
        assert "def fixed(): pass  # attempt 1" in committed
        print("PASS: approve plan -> approve review -> local git branch+commit+push, PR opened, labels merged")


async def test_plan_rejected_to_max_never_touches_git_or_github():
    with temp_repo() as (work, origin):
        calls = []
        fake = FakeLLM()
        pl.llm = fake
        pl.TOOLS.clear()
        pl.TOOLS.update(make_fake_tools(calls))
        config = new_config()

        result = await pl.app.ainvoke(base_input(work), config=config)
        rounds = 0
        while "__interrupt__" in result:
            rounds += 1
            result = await pl.app.ainvoke(
                Command(resume={"approved": False, "feedback": "no"}), config=config
            )

        assert rounds == pl.MAX_PLAN_ROUNDS
        assert fake.coder_calls == 0
        assert [n for n, _ in calls] == ["get_issue", "add_issue_comment"]
        assert "agent/issue-1" not in _git(work, "branch", "--list")
        assert "agent/issue-1" not in _git(origin, "branch", "--list")
        print("PASS: plan rejected to the cap -> zero coder calls, no git branch, one issue comment")


async def test_plan_approved_but_review_never_converges_still_opens_no_pr():
    with temp_repo() as (work, origin):
        calls = []
        fake = FakeLLM(reviewer_verdicts=["reject", "reject", "reject"])
        pl.llm = fake
        pl.TOOLS.clear()
        pl.TOOLS.update(make_fake_tools(calls))
        config = new_config()

        result = await pl.app.ainvoke(base_input(work), config=config)
        result = await pl.app.ainvoke(Command(resume={"approved": True}), config=config)

        assert result["escalated"] is True
        assert fake.coder_calls == pl.MAX_REVIEW_ROUNDS
        assert "create_pull_request" not in [n for n, _ in calls], (
            "approving the PLAN is not the same as approving the CODE"
        )
        assert "agent/issue-1" not in _git(origin, "branch", "--list")
        print("PASS: plan approved but code never passes review -> coder ran, but still no branch pushed, no PR")


async def run_all():
    await test_context_fetched_before_human_sees_plan()
    await test_approve_plan_then_approve_review_opens_pr()
    await test_plan_rejected_to_max_never_touches_git_or_github()
    await test_plan_approved_but_review_never_converges_still_opens_no_pr()
    print("\nAll wiring tests passed.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_all())
