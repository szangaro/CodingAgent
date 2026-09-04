# OpenAI version -- issue-to-PR pipeline with a human approval gate

Same as `../claude/`, ported to OpenAI. Files:

```
pipeline.py       everything: the graph, the MCP wiring, the console driver
test_wiring.py    proves the graph's logic without real API calls or credentials
agents/
  planner.md      standing instructions (identical to the Claude version --
  coder.md        agent instructions aren't provider-specific)
  reviewer.md
requirements.txt  pip install -r requirements.txt
```

## What's actually different from ../claude/

Two things, both marked `CHANGED` in `pipeline.py`:

- `ChatOpenAI` instead of `ChatAnthropic` (model: `gpt-4.1`).
- The system prompt is sent as a plain string instead of Anthropic's
  `cache_control: {"type": "ephemeral"}`-tagged content block. OpenAI
  caches automatically (longest matching prefix, prompts over ~1024
  tokens) -- there's no field to set, so there's nothing to wrap.

Everything else -- the graph shape, the MCP tool calls, the `interrupt()`/
`Command(resume=...)` approval gate, the checkpointer, the console driver
loop, the "coder never runs before approval" and "approving the plan
isn't approving the code" guarantees -- is identical, because none of it
is provider-specific. See `../claude/README.md` for the full flow
description; it applies here unchanged.

## Run it

```
pip install -r requirements.txt
export OPENAI_API_KEY=...
export GITHUB_PAT=...   # repo scope -- issues + PRs on the target repo

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

Same checks as the Claude version's test file, including running real git
against a throwaway local repo + bare "origin" for every branch/commit/push,
and that the "in review" label gets merged into the issue's existing
labels rather than clobbering them.
