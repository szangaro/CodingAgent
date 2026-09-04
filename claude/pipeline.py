"""
Complete example: pick up a real GitHub issue, draft a fix for a repo
checked out locally on disk, get a HUMAN's approval on the plan before
any code is written, get an LLM reviewer's approval on the code before
anything is pushed, then open a draft PR and comment back on the issue.

Requires (see requirements.txt and README.md):
  ANTHROPIC_API_KEY, GITHUB_PAT

repo_path must point at an existing local clone that already has an
`origin` remote with push access configured (SSH key, credential helper,
whatever you normally use for `git push` -- this pipeline doesn't manage
git auth, only the MCP calls). The issue and the PR are assumed to live
in the same repo (pr_owner/pr_repo) -- GITHUB_PAT needs read access to
issues and write access to contents/PRs there.

Flow
----
fetch_context   pull the real issue (title + body) + read the current
                file, if any, straight off disk at repo_path/file_path
planner         draft a plan from that real context
plan_gate       STOP. A human looks at the plan before anything else happens.
                  reject -> planner drafts again (up to MAX_PLAN_ROUNDS),
                            then plan_rejected: comment on the issue, end.
                  approve -> coder
coder           write the complete new file content
reviewer        an LLM checks the code against the issue and plan
                  reject -> coder retries (up to MAX_REVIEW_ROUNDS),
                            then flag_for_human: comment on the issue, end.
                  approve -> open_pr
open_pr         plain local git: checkout base_branch, branch, write the
                file, commit, push -- then open a DRAFT PR via the GitHub
                API (the one step git can't do), with "Closes #<issue>" in
                the body so merging it closes the issue automatically, and
                comment the PR link back on the issue

So there are two separate gates before anything lands on GitHub: yours
(before code exists at all) and the reviewer's (before it's pushed). A
human approving the plan is not the same as the code passing review --
both have to happen.

Where the issue goes: bottom of this file, in main() -- change
issue_number / pr_owner / pr_repo / repo_path / file_path there, or call
run_with_console_approval({...}) yourself with different values.

Where you review the plan: printed to the console by
run_with_console_approval(), which is the only part of this file that's
specific to a terminal. Swap that one function for something that posts
to Slack and waits on a button, or renders a web form -- the graph above
it doesn't change.

Tool-name provenance (so nothing here is a guess): reading the file,
branching, and pushing are all plain `git` -- no MCP tool involved. The
GitHub MCP tool names used (get_issue, add_issue_comment,
create_pull_request, update_issue) were checked against the current
github-mcp-server README, not assumed from an older API shape. One thing
that's best-effort rather than guaranteed: applying an "in review" label
after the PR opens -- it re-fetches current labels and merges rather than
overwriting them, but update_issue's exact label semantics can vary by
server version, so it's silently skipped if the call fails.
"""

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

AGENTS_DIR = Path(__file__).parent / "agents"
MODEL_NAME = "claude-sonnet-4-5-20250929"
MAX_REVIEW_ROUNDS = 3
MAX_PLAN_ROUNDS = 3
# Label applied to the issue after a PR opens -- best-effort, see
# open_pr_node. Change to match your repo's labels, or ignore: it's
# skipped entirely if the update call fails.
IN_REVIEW_LABEL = "in review"


def load_instructions(name: str) -> str:
    return (AGENTS_DIR / f"{name}.md").read_text()


def cached_system_message(instructions: str) -> SystemMessage:
    """Anthropic's prompt cache is opt-in: tagging a content block
    cache_control:ephemeral gets it cached (~5 min) instead of re-sent in
    full on every node call that reuses the same agents/<name>.md file."""
    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": instructions,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    )


llm = ChatAnthropic(model=MODEL_NAME, temperature=0)

# Populated once at startup by setup_tools() -- name -> LangChain BaseTool.
TOOLS: dict[str, Any] = {}


def build_mcp_client() -> MultiServerMCPClient:
    # Every MCP call in this pipeline goes through this one server now:
    # get_issue / add_issue_comment / update_issue for the issue, and
    # create_pull_request for the PR. Reading the file, branching,
    # committing, and pushing are all local git against repo_path instead
    # (see _run_git / open_pr_node).
    return MultiServerMCPClient(
        {
            "github": {
                "transport": "streamable_http",
                "url": "https://api.githubcopilot.com/mcp/",
                "headers": {
                    "Authorization": f"Bearer {os.environ.get('GITHUB_PAT', '')}"
                },
            },
        }
    )


async def setup_tools() -> None:
    client = build_mcp_client()
    for tool in await client.get_tools():
        TOOLS[tool.name] = tool


def _tool_text(result: Any) -> str:
    """MCP tool results come back as a list of content blocks
    ([{"type": "text", "text": "..."}]); pull the text out."""
    if isinstance(result, list):
        return "\n".join(
            block.get("text", "") for block in result if isinstance(block, dict)
        )
    return str(result)


def _strip_code_fence(text: str) -> str:
    """The Coder returns a markdown code block; the GitHub write tools
    want raw file content, not the fence around it."""
    text = text.strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _run_git(repo_path: Path, *args: str) -> str:
    """Every branch/commit/push here is plain local git -- no API call
    needed until the PR itself, which git has no concept of."""
    result = subprocess.run(
        ["git", *args], cwd=repo_path, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _extract_pr_url(result: Any) -> str:
    raw = _tool_text(result)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed.get("html_url") or parsed.get("url") or raw
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


class PipelineState(TypedDict):
    pr_owner: str
    pr_repo: str
    issue_number: int
    repo_path: str
    base_branch: str
    file_path: str
    issue_summary: str
    existing_content: str
    plan: str
    plan_approved: bool
    plan_feedback: str
    plan_round: int
    code: str
    review_verdict: str  # "approve" | "reject"
    review_feedback: str
    review_round: int
    pr_url: str
    escalated: bool


# --- fetch real context: the issue via MCP, the file straight off disk
# (no LLM involved -- we already know exactly which issue/file, there's
# nothing to decide) ---
async def fetch_context_node(state: PipelineState) -> dict:
    issue_raw = await TOOLS["get_issue"].ainvoke(
        {
            "owner": state["pr_owner"],
            "repo": state["pr_repo"],
            "issue_number": state["issue_number"],
        }
    )
    issue = json.loads(_tool_text(issue_raw))
    issue_summary = (
        f"#{state['issue_number']}: {issue.get('title', '')}\n\n"
        f"{issue.get('body') or ''}"
    )

    target = Path(state["repo_path"]) / state["file_path"]
    # File doesn't exist on disk yet -- the issue is asking for something
    # new, not a fix to something already there.
    existing_content = target.read_text() if target.exists() else ""

    return {"issue_summary": issue_summary, "existing_content": existing_content}


async def planner_node(state: PipelineState) -> dict:
    system = cached_system_message(load_instructions("planner"))
    parts = [
        f"Issue:\n{state['issue_summary']}",
        f"Current file ({state['file_path']}, empty if new):\n"
        f"{state['existing_content'] or '(file does not exist yet)'}",
    ]
    if state.get("plan_feedback"):
        parts.append(
            "A human rejected your previous plan and left this feedback "
            f"(address it):\n{state['plan_feedback']}"
        )
    human = HumanMessage(content="\n\n".join(parts))
    response = await llm.ainvoke([system, human])
    return {"plan": response.content}


# --- human gate: nothing gets coded until you say so ---
async def plan_gate_node(state: PipelineState) -> dict:
    decision = interrupt(
        {
            "kind": "plan_approval",
            "issue_number": state["issue_number"],
            "issue_summary": state["issue_summary"],
            "file_path": state["file_path"],
            "plan": state["plan"],
            "round": state.get("plan_round", 0) + 1,
        }
    )
    return {
        "plan_approved": bool(decision.get("approved")),
        "plan_feedback": decision.get("feedback", ""),
        "plan_round": state.get("plan_round", 0) + 1,
    }


def route_after_gate(state: PipelineState) -> Literal["coder", "planner", "abort"]:
    if state["plan_approved"]:
        return "coder"
    if state["plan_round"] >= MAX_PLAN_ROUNDS:
        return "abort"
    return "planner"


async def coder_node(state: PipelineState) -> dict:
    system = cached_system_message(load_instructions("coder"))
    parts = [
        f"Issue:\n{state['issue_summary']}",
        f"Plan:\n{state['plan']}",
        f"Current file ({state['file_path']}, empty if new):\n"
        f"{state['existing_content'] or '(file does not exist yet)'}",
    ]
    if state.get("review_feedback"):
        parts.append(
            "Reviewer feedback on your previous attempt "
            f"(fix all of this):\n{state['review_feedback']}"
        )
    human = HumanMessage(content="\n\n".join(parts))
    response = await llm.ainvoke([system, human])
    return {"code": response.content}


async def reviewer_node(state: PipelineState) -> dict:
    system = cached_system_message(load_instructions("reviewer"))
    human = HumanMessage(
        content=(
            f"Issue:\n{state['issue_summary']}\n\n"
            f"Plan:\n{state['plan']}\n\n"
            f"Proposed file:\n{state['code']}"
        )
    )
    response = await llm.ainvoke([system, human])
    text = response.content
    first_line, _, rest = text.partition("\n")
    verdict = "approve" if "APPROVE" in first_line.upper() else "reject"
    return {
        "review_verdict": verdict,
        "review_feedback": rest.strip(),
        "review_round": state.get("review_round", 0) + 1,
    }


def route_after_review(state: PipelineState) -> Literal["retry", "publish", "escalate"]:
    if state["review_verdict"] == "approve":
        return "publish"
    if state["review_round"] >= MAX_REVIEW_ROUNDS:
        return "escalate"
    return "retry"


# --- approved by both gates: branch, commit, and push locally, then open
# the PR (the one step that has to go through the GitHub API) and tell
# the issue ---
async def open_pr_node(state: PipelineState) -> dict:
    repo_path = Path(state["repo_path"])
    branch_name = f"agent/issue-{state['issue_number']}"

    _run_git(repo_path, "checkout", state["base_branch"])
    _run_git(repo_path, "pull", "origin", state["base_branch"])
    _run_git(repo_path, "checkout", "-b", branch_name)

    code = _strip_code_fence(state["code"])
    target = repo_path / state["file_path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code)

    _run_git(repo_path, "add", state["file_path"])
    _run_git(repo_path, "commit", "-m", f"#{state['issue_number']}: automated fix")
    _run_git(repo_path, "push", "-u", "origin", branch_name)

    pr_raw = await TOOLS["create_pull_request"].ainvoke(
        {
            "owner": state["pr_owner"],
            "repo": state["pr_repo"],
            "title": f"#{state['issue_number']}: automated fix",
            "head": branch_name,
            "base": state["base_branch"],
            # "Closes #N" is a GitHub closing keyword -- merging this PR
            # closes the issue automatically, no separate API call needed.
            "body": f"Closes #{state['issue_number']}\n\n{state['plan']}",
            "draft": True,  # a human still reviews before this merges
        }
    )
    pr_url = _extract_pr_url(pr_raw)

    await TOOLS["add_issue_comment"].ainvoke(
        {
            "owner": state["pr_owner"],
            "repo": state["pr_repo"],
            "issue_number": state["issue_number"],
            "body": f"Opened draft PR: {pr_url}",
        }
    )

    if "update_issue" in TOOLS:
        try:
            # Re-fetch so we merge into whatever labels are on the issue
            # right now instead of overwriting them -- update_issue's
            # labels field replaces the full set, it doesn't append.
            fresh_raw = await TOOLS["get_issue"].ainvoke(
                {
                    "owner": state["pr_owner"],
                    "repo": state["pr_repo"],
                    "issue_number": state["issue_number"],
                }
            )
            fresh = json.loads(_tool_text(fresh_raw))
            current_labels = [
                label.get("name", label) if isinstance(label, dict) else label
                for label in fresh.get("labels", [])
            ]
            if IN_REVIEW_LABEL not in current_labels:
                await TOOLS["update_issue"].ainvoke(
                    {
                        "owner": state["pr_owner"],
                        "repo": state["pr_repo"],
                        "issue_number": state["issue_number"],
                        "labels": current_labels + [IN_REVIEW_LABEL],
                    }
                )
        except Exception:
            pass  # best-effort -- the PR + comment already happened

    return {"pr_url": pr_url}


# --- reviewer never converged: tell the issue, touch nothing on GitHub ---
async def flag_for_human_node(state: PipelineState) -> dict:
    await TOOLS["add_issue_comment"].ainvoke(
        {
            "owner": state["pr_owner"],
            "repo": state["pr_repo"],
            "issue_number": state["issue_number"],
            "body": (
                f"Automated attempt did not pass review after "
                f"{state['review_round']} round(s). No branch or PR was "
                f"created. Last reviewer feedback:\n\n{state['review_feedback']}"
            ),
        }
    )
    return {"escalated": True}


# --- plan never approved by YOU: tell the issue, touch nothing on GitHub ---
async def plan_rejected_node(state: PipelineState) -> dict:
    await TOOLS["add_issue_comment"].ainvoke(
        {
            "owner": state["pr_owner"],
            "repo": state["pr_repo"],
            "issue_number": state["issue_number"],
            "body": (
                f"Proposed plan was rejected {state['plan_round']} time(s) "
                f"and never approved. No code was written. Last feedback:\n\n"
                f"{state['plan_feedback']}"
            ),
        }
    )
    return {"escalated": True}


graph = StateGraph(PipelineState)
graph.add_node("fetch_context", fetch_context_node)
graph.add_node("planner", planner_node)
graph.add_node("plan_gate", plan_gate_node)
graph.add_node("coder", coder_node)
graph.add_node("reviewer", reviewer_node)
graph.add_node("open_pr", open_pr_node)
graph.add_node("flag_for_human", flag_for_human_node)
graph.add_node("plan_rejected", plan_rejected_node)

graph.add_edge(START, "fetch_context")
graph.add_edge("fetch_context", "planner")
graph.add_edge("planner", "plan_gate")
graph.add_conditional_edges(
    "plan_gate",
    route_after_gate,
    {"coder": "coder", "planner": "planner", "abort": "plan_rejected"},
)
graph.add_edge("plan_rejected", END)
graph.add_edge("coder", "reviewer")
graph.add_conditional_edges(
    "reviewer",
    route_after_review,
    {"retry": "coder", "publish": "open_pr", "escalate": "flag_for_human"},
)
graph.add_edge("open_pr", END)
graph.add_edge("flag_for_human", END)

# A checkpointer is required for interrupt()/Command(resume=...) to work
# at all -- it's what persists graph state across the pause. InMemorySaver
# only survives this process's lifetime; swap in a real persistent
# checkpointer if approvals need to survive a restart.
app = graph.compile(checkpointer=InMemorySaver())


async def run_with_console_approval(initial_state: dict) -> dict:
    """Drives the graph, printing each plan to the console and blocking
    on input() for your decision. This function is the only part of the
    file specific to a terminal -- swap it for a Slack/web equivalent and
    the graph above doesn't change."""
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    next_input = initial_state

    while True:
        result = await app.ainvoke(next_input, config=config)

        if "__interrupt__" not in result:
            return result  # reached an END node

        payload = result["__interrupt__"][0].value
        print("\n" + "=" * 70)
        print(f"#{payload['issue_number']} -- plan (round {payload['round']}), file: {payload['file_path']}")
        print("=" * 70)
        print(payload["issue_summary"])
        print("-" * 70)
        print(payload["plan"])
        print("=" * 70)

        answer = input("Approve this plan? [y/n]: ").strip().lower()
        if answer == "y":
            next_input = Command(resume={"approved": True})
        else:
            feedback = input("What should change? (feedback for the planner): ").strip()
            next_input = Command(resume={"approved": False, "feedback": feedback})


async def main():
    required = ["ANTHROPIC_API_KEY", "GITHUB_PAT"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Set these env vars first: {', '.join(missing)}")

    await setup_tools()

    # <-- the issue and repo go here --
    result = await run_with_console_approval(
        {
            "pr_owner": "your-org",
            "pr_repo": "your-repo",
            "issue_number": 123,  # change to a real issue number
            "repo_path": "/path/to/local/clone",  # existing checkout with
            # an `origin` remote you already have push access to
            "base_branch": "main",
            "file_path": "src/example.py",  # which file the fix touches --
            # locating this automatically from the issue alone (code
            # search, stack-trace parsing) is a separate, harder problem
            # not solved here; this takes it as a given on purpose.
            "plan_round": 0,
            "plan_feedback": "",
            "review_round": 0,
            "review_feedback": "",
        }
    )

    if result.get("escalated") and not result.get("pr_url"):
        print(
            f"\nStopped without opening a PR after {result['plan_round']} plan round(s), "
            f"{result['review_round']} review round(s)."
        )
    else:
        print(f"\nOpened: {result['pr_url']}")


if __name__ == "__main__":
    asyncio.run(main())
