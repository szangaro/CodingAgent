"""
Same as ../claude/smoke_test_github.py, pointed at this folder's
pipeline.py. Opens a REAL draft PR on YOUR GitHub while faking Jira --
for validating the git/GitHub wiring end to end without a Jira account.
Planner/Coder/Reviewer are real OpenAI calls; branch/commit/push are real
local git against repo_path; create_pull_request is a real call to the
GitHub MCP server.

Costs real OpenAI API calls and pushes a real branch + opens a real draft
PR on the repo you point it at. Use a throwaway test repo, not something
that matters.

Requires:
  OPENAI_API_KEY, GITHUB_PAT (repo scope)

Setup:
  1. Create (or reuse) a small test repo on your GitHub account.
  2. Clone it locally somewhere and confirm `git push` already works from
     that clone (SSH key or credential helper already configured -- this
     pipeline doesn't manage git auth itself).
  3. Edit REPO_PATH / PR_OWNER / PR_REPO / BASE_BRANCH / FILE_PATH below.

Run: python3 smoke_test_github.py
"""

import asyncio
import json
import os

import pipeline as pl

# <-- point these at your real test repo --
REPO_PATH = "/path/to/local/clone"
PR_OWNER = "your-github-username"
PR_REPO = "your-test-repo"
BASE_BRANCH = "main"
FILE_PATH = "smoke_test.py"  # fine if it doesn't exist yet
JIRA_TICKET_KEY = "SMOKE-1"  # made up -- no real Jira involved


class FakeTool:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, args):
        return self._handler(args)


def json_block(obj):
    return [{"type": "text", "text": json.dumps(obj)}]


def install_fake_jira() -> None:
    def jira_get_issue(args):
        return json_block(
            {
                "fields": {
                    "summary": "Smoke test: add a hello() function",
                    "description": (
                        f"Add a function called hello() to {FILE_PATH} that "
                        "returns the string 'hello from the pipeline smoke test'."
                    ),
                }
            }
        )

    def jira_add_comment(args):
        print(f"[fake jira] would comment on {args['issue_key']}:\n{args['body']}")
        return json_block({"ok": True})

    pl.TOOLS["jira_get_issue"] = FakeTool("jira_get_issue", jira_get_issue)
    pl.TOOLS["jira_add_comment"] = FakeTool("jira_add_comment", jira_add_comment)
    # jira_get_transitions / jira_transition_issue stay unset on purpose --
    # open_pr_node already skips the transition step when they're missing
    # from TOOLS, so there's nothing to fake for it.


async def setup_real_github_tools() -> None:
    """Same MCP client pipeline.py builds, minus the jira server -- nothing
    here needs a Jira account."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
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
    for tool in await client.get_tools():
        pl.TOOLS[tool.name] = tool


async def main() -> None:
    missing = [v for v in ("OPENAI_API_KEY", "GITHUB_PAT") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Set these env vars first: {', '.join(missing)}")

    await setup_real_github_tools()
    install_fake_jira()

    result = await pl.run_with_console_approval(
        {
            "jira_ticket_key": JIRA_TICKET_KEY,
            "pr_owner": PR_OWNER,
            "pr_repo": PR_REPO,
            "repo_path": REPO_PATH,
            "base_branch": BASE_BRANCH,
            "file_path": FILE_PATH,
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
