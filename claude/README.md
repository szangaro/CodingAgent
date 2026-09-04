# Claude version -- ticket-to-PR pipeline with a human approval gate

Every file in this folder, and only these files:

```
pipeline.py           everything: the graph, the MCP wiring, the console driver
test_wiring.py        proves the graph's logic without real API calls or credentials
smoke_test_github.py  fakes only Jira; opens a REAL draft PR on your GitHub
agents/
  planner.md      standing instructions for the Planner node
  coder.md        standing instructions for the Coder node
  reviewer.md      standing instructions for the Reviewer node
requirements.txt  pip install -r requirements.txt
```

## What it does

1. `fetch_context` -- pulls a real Jira ticket and the current content of
   the file it likely touches, read straight off disk at `repo_path`
   (empty if the file doesn't exist yet).
2. `planner` -- drafts a plan from that real context.
3. `plan_gate` -- **stops and shows you the plan on the console.** Nothing
   gets coded until you type `y`. Reject it (with feedback) and the
   planner tries again, up to 3 rounds, then it gives up and comments on
   the ticket instead of guessing.
4. `coder` -- writes the complete new file content.
5. `reviewer` -- an LLM checks the code against the ticket and plan.
   Rejects loop back to the coder (up to 3 rounds); repeated failure
   comments on the ticket and stops, without touching git or GitHub.
6. `open_pr` -- only reached if BOTH gates passed: checks out `base_branch`,
   branches, writes the file, commits, and pushes -- all plain local git
   against `repo_path` -- then opens a **draft** PR via the GitHub API (the
   one step git can't do) and comments the link back on the ticket.

Two separate approvals stand between an LLM and your repo: yours (before
any code exists) and the reviewer's (before it's pushed). Approving the
plan is not the same as approving the code -- the test file checks this
specifically.

## Run it

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...      # your personal key
export GITHUB_PAT=...             # repo scope -- only used to open the PR itself
export JIRA_URL=https://your-domain.atlassian.net
export JIRA_USERNAME=you@example.com
export JIRA_API_TOKEN=...         # id.atlassian.com/manage-profile/security/api-tokens

# repo_path must be an existing local clone with an `origin` remote you
# already have push access to (SSH key / credential helper already set
# up -- this pipeline doesn't manage git auth, only the MCP calls)

# edit the bottom of pipeline.py: jira_ticket_key / pr_owner / pr_repo / repo_path / file_path
python3 pipeline.py
```

## Verify without spending API calls or touching real Jira

```
python3 test_wiring.py
```

Fakes the LLM and the two remaining MCP tools (Jira + create_pull_request),
but runs real git against a throwaway local repo + bare "origin" for
every branch/commit/push. Drives the approval gate programmatically (no
console needed), and checks: context gets fetched before you ever see a
plan; the coder never runs before you approve; rejecting the plan to the
cap creates no branch anywhere; approving the plan but never getting
review approval still pushes no branch and opens no PR.

## Test against your own GitHub (real PR, no Jira needed)

```
export ANTHROPIC_API_KEY=...
export GITHUB_PAT=...   # repo scope

# edit the top of smoke_test_github.py: REPO_PATH / PR_OWNER / PR_REPO / FILE_PATH
python3 smoke_test_github.py
```

Fakes only `jira_get_issue`/`jira_add_comment` with a made-up ticket --
everything else is real: real Claude calls for the plan/code/review, real
local git branch+commit+push against `REPO_PATH`, real `create_pull_request`
call that opens an actual draft PR on your repo. Costs real API calls and
touches your real GitHub, so point it at a throwaway test repo. You still
get the console approval prompt for the plan.

## Notes

- Reading the file, branching, committing, and pushing are all plain
  local `git` against `repo_path` -- no GitHub API call until the PR
  itself, which git has no equivalent for. That's the only thing GitHub's
  hosted MCP server is still used for. Jira connects to the community
  `mcp-atlassian` server locally via stdio (`pip install mcp-atlassian` --
  no Docker needed).
- The PR opens as a **draft**. A human still has to promote it -- this
  pipeline never merges anything.
- `file_path` is an input you set, not something the pipeline figures out
  on its own. Locating the right file from a ticket alone (code search,
  parsing a stack trace) is a separate, harder problem this doesn't solve.
- Jira transition matching (moving the ticket to "In Review" after the PR
  opens) is best-effort and workflow-specific -- it's silently skipped if
  your project's transition names don't match the candidates in
  `TRANSITION_NAME_CANDIDATES` near the top of `pipeline.py`.
