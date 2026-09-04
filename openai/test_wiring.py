"""
Same as ../claude/test_wiring.py, pointed at this folder's pipeline.py.
Only one line differs (marked CHANGED): how the fake LLM reads the system
prompt back out of the message, since this pipeline sends it as a plain
string instead of Anthropic's cache_control content-block list. Every
assertion is identical -- proof the approval-gate, local-git, and
MCP-publish guarantees don't depend on which provider is behind `llm`.

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
        system_text = messages[0].content  # CHANGED: plain string, not a content-block list
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
    def issue_read(args):
        assert args["method"] == "get"
        calls.append(("issue_read", args))
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

    def issue_write(args):
        assert args["method"] == "update"
        calls.append(("issue_write", args))
        return json_block({"ok": True})

    return {
        "issue_read": FakeTool("issue_read", issue_read),
        "create_pull_request": FakeTool("create_pull_request", create_pull_request),
        "add_issue_comment": FakeTool("add_issue_comment", add_issue_comment),
        "issue_write": FakeTool("issue_write", issue_write),
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
            "issue_read",  # fetch_context
            "create_pull_request",
            "add_issue_comment",
            "issue_read",  # re-fetched to merge labels
            "issue_write",
        ]

        # the pre-existing "bug" label survives -- issue_write REPLACES
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
        assert [n for n, _ in calls] == ["issue_read", "add_issue_comment"]
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
