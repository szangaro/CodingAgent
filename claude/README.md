# Claude version -- issue-to-PR pipeline with a human approval gate

Every file in this folder, and only these files:

```
pipeline.py       everything: the graph, the MCP wiring, the console driver
test_wiring.py    proves the graph's logic without real API calls or credentials
agents/
  planner.md      standing instructions for the Planner node
  coder.md        standing instructions for the Coder node
  reviewer.md      standing instructions for the Reviewer node
requirements.txt  pip install -r requirements.txt
```

## What it does

1. `fetch_context` -- pulls a real GitHub issue (title + body) and the
   current content of the file it likely touches, read straight off disk
   at `repo_path` (empty if the file doesn't exist yet).
2. `planner` -- drafts a plan from that real context.
3. `plan_gate` -- **stops and shows you the plan on the console.** Nothing
   gets coded until you type `y`. Reject it (with feedback) and the
   planner tries again, up to 3 rounds, then it gives up and comments on
   the issue instead of guessing.
4. `coder` -- writes the complete new file content.
5. `reviewer` -- an LLM checks the code against the issue and plan.
   Rejects loop back to the coder (up to 3 rounds); repeated failure
   comments on the issue and stops, without touching git or GitHub.
6. `open_pr` -- only reached if BOTH gates passed: checks out `base_branch`,
   branches, writes the file, commits, and pushes -- all plain local git
   against `repo_path` -- then opens a **draft** PR via the GitHub API (the
   one step git can't do), with `Closes #<issue>` in the body so merging
   it closes the issue automatically, and comments the PR link back on
   the issue.

Two separate approvals stand between an LLM and your repo: yours (before
any code exists) and the reviewer's (before it's pushed). Approving the
plan is not the same as approving the code -- the test file checks this
specifically.

## Run it

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...      # your personal key
export GITHUB_PAT=...             # repo scope -- issues + PRs on the target repo

# repo_path must be an existing local clone with an `origin` remote you
# already have push access to (SSH key / credential helper already set
# up -- this pipeline doesn't manage git auth, only the MCP calls)

# edit the bottom of pipeline.py: issue_number / pr_owner / pr_repo / repo_path / file_path
python3 pipeline.py
```

This opens a **real** draft PR on GitHub -- there's no separate smoke
test needed. Point it at a throwaway repo/issue the first time you try
it.

## Verify without spending API calls or touching real GitHub

```
python3 test_wiring.py
```

Fakes the LLM and the four MCP tools (`get_issue`, `add_issue_comment`,
`create_pull_request`, `update_issue`), but runs real git against a
throwaway local repo + bare "origin" for every branch/commit/push. Drives
the approval gate programmatically (no console needed), and checks:
context gets fetched before you ever see a plan; the coder never runs
before you approve; rejecting the plan to the cap creates no branch
anywhere; approving the plan but never getting review approval still
pushes no branch and opens no PR; on the happy path the "in review" label
gets merged into the issue's existing labels rather than clobbering them.

## Notes

- Reading the file, branching, committing, and pushing are all plain
  local `git` against `repo_path` -- no GitHub API call until the issue
  fetch/comment and the PR itself, which git has no equivalent for.
  Everything in this pipeline goes through GitHub's hosted MCP server; no
  other credential or service is needed.
- The PR opens as a **draft**. A human still has to promote it -- this
  pipeline never merges anything.
- `file_path` is an input you set, not something the pipeline figures out
  on its own. Locating the right file from an issue alone (code search,
  parsing a stack trace) is a separate, harder problem this doesn't solve.
- Applying an `in review` label to the issue after the PR opens is
  best-effort: it re-fetches the issue's current labels and merges rather
  than overwriting, but is silently skipped if the update call fails.
  Change `IN_REVIEW_LABEL` near the top of `pipeline.py` to match your
  repo's labels.
