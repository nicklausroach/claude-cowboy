---
name: claude-cowboy
description: "Multi-agent orchestrator (a Claude Code port of pi-cowboy): split a large job into focused subtasks and delegate them to background coding agents (Claude Code, Codex, pi, OpenCode), each running in its own Herdr workspace and isolated in a copy-on-write Git worktree on its own branch; then steer them and, when the user says so, open PRs, merge and clean up. Use when the user invokes /claude-cowboy, asks for cowboy agents, or asks to launch parallel background agents in Herdr worktrees. Requires running inside Herdr (HERDR_ENV=1) in a Git repo."
---

# claude-cowboy

You are the orchestrator. You break work into focused subtasks, hand each to a background
**cowboy agent**, collect their reports and review their branches. Merging, PRs and
cleanup happen only when the user says so.

Every operation goes through one helper CLI, `~/.claude/skills/claude-cowboy/scripts/cowboy`.
Always call it by that literal path, never through a shell variable or alias, so the
user's permission allow-rule for it matches.

## Preconditions

Check these first. If one fails, tell the user and stop.

```bash
test "${HERDR_ENV:-}" = 1 && git rev-parse --show-toplevel
```

- You must be inside a Herdr pane. Agents get their own Herdr workspaces, and the user
  can open any of them to watch or steer. Agent workspaces are opened with
  `herdr worktree open`, so Herdr records each one as a linked worktree of the main repo
  and groups it with the repo's workspace. `herdr worktree list --cwd <repo>` shows which
  workspace each agent branch is open in.
- You must be inside a Git repo. Agents branch from its current branch by default.
- Worktrees are copy-on-write clones (APFS `cp -c`, Linux `cp --reflink`), so ignored
  files like `node_modules` and build caches come along almost free. If cloning fails,
  the helper falls back to a plain `git worktree add`.

## If invoked with arguments

`/claude-cowboy` behaves like `/cowboy` in pi-cowboy:

| Invocation | Do |
| --- | --- |
| (no args) | Run `~/.claude/skills/claude-cowboy/scripts/cowboy status`, then summarize and offer the next actions |
| `status` / `st` | `~/.claude/skills/claude-cowboy/scripts/cowboy status` |
| `spawn <task…>` / `sp` | Spawn one agent for the described task |
| `worktree <name>` / `wt` | `~/.claude/skills/claude-cowboy/scripts/cowboy worktree "<name>"` (worktree with no agent) |
| `model <alias>` | `~/.claude/skills/claude-cowboy/scripts/cowboy config --set agent.model=<alias>` (`clear` sets it to `null`) |
| `templates` | `~/.claude/skills/claude-cowboy/scripts/cowboy templates` |
| `config` | `~/.claude/skills/claude-cowboy/scripts/cowboy config` |
| `diff <id>` | `~/.claude/skills/claude-cowboy/scripts/cowboy diff <id>` |
| anything else | Treat it as a job to break down and delegate |

## Workflow

### 1. Delegate

Write one self-contained prompt per subtask. The agent has none of your context, so
include the goal, the relevant files, constraints and what done looks like. Keep each
task focused: one clear job leaves less room for drift than a long list.

```bash
~/.claude/skills/claude-cowboy/scripts/cowboy spawn -t "fix login flow" -p "…task instructions…"
# Long prompts: write them to a scratch file and pass -f FILE, or use -p - with a heredoc.
# Options: -a <template>  -m <model: haiku|sonnet|opus|fable|full id>  -e <effort>
#          --harness claude|codex|pi|opencode  --base <ref>  --include-dirty
```

`-t` takes a 2–3 word name. It names the branch (`cow-<slug>-<id>`), the worktree and
the Herdr workspace. Spawn returns right away with an agent id. Spawns beyond the
concurrency limit (default 4) wait in a queue.

### 2. Get notified

Right after each spawn, start a waiter **with Bash `run_in_background: true`**, one per
agent, so each report reaches you as its agent settles:

```bash
~/.claude/skills/claude-cowboy/scripts/cowboy wait <id>
```

The waiter also starts queued agents as slots free up, so **every spawned agent needs a
waiter**, including queued ones. Don't poll. You are re-invoked when a waiter exits, and
its output is the completion message: status, the agent's report, and its branch,
commit count and worktree state. Meanwhile, keep working or tell the user what is running.

A waiter can exit with these statuses:
- `settled`: the agent wrote its report. Review it.
- `blocked`: either the report starts with `BLOCKED:` (the agent needs a decision), or
  the agent is stuck at an approval or startup dialog in its pane. Relay the question to
  the user, or answer it with `steer`. Never approve a dialog on the user's behalf
  unless they have said to.
- `exited`: the agent process died. Its pane output is included.

### 3. Review, steer, then report back

- **Review** the work: `~/.claude/skills/claude-cowboy/scripts/cowboy diff <id>` (commits since the base plus
  any uncommitted changes; `--stat` for a summary), or `git diff <base>...<branch>` from the main checkout.
- **Steer** if it needs another pass. This wakes a settled agent in its existing pane.
  Afterwards, start a new background `wait`:
  ```bash
  ~/.claude/skills/claude-cowboy/scripts/cowboy steer <id> [<id>…] -m "follow-up instructions"
  ```
- **Stop** a runaway agent (interrupts it and keeps its worktree): `~/.claude/skills/claude-cowboy/scripts/cowboy stop <id>`
- **Report** to the user once the work looks good: what changed, your review verdict, and
  the branch and worktree path. Then **stop and wait**. Leave the worktree, branch and
  Herdr workspace in place so the user can inspect it, and so you can keep steering the
  agent if they want changes.

### 4. Land the work (only when the user says so)

**Never merge, push, open a PR or clean up on your own initiative**, even after a clean
review. The user decides how the work lands. Don't assume a merge; ask if it's unclear.
The two ways it lands:

- **Pull request from the worktree** (often the preferred path). Push the agent's branch
  and open the PR straight from its worktree, leaving the worktree open. Don't merge into
  the main checkout.
  ```bash
  git -C <worktree> push -u origin <branch>
  (cd <worktree> && gh pr create --base <base> --head <branch> --title "…" --body "…")
  ```
  If review feedback comes in, steer the agent and push again from the same worktree.
- **Local merge** into the main checkout. The checkout must be on the target branch with
  no uncommitted tracked changes. The helper stops at the first conflict and leaves it in
  place for you to resolve.
  ```bash
  ~/.claude/skills/claude-cowboy/scripts/cowboy merge <branch-or-id> [...] [--target main]
  ```

### 5. Clean up (only when the user says so)

Clean up an agent only when the user asks, typically after its branch is merged (locally
or via a merged PR) or rejected:

```bash
~/.claude/skills/claude-cowboy/scripts/cowboy cleanup <id> [<id>…]
```

Cleanup closes the agent's Herdr workspace, removes the worktree, and deletes the branch
only if it is already merged. It refuses a running agent and keeps a worktree that has
uncommitted changes. Two flags discard work, so **use them only after the user explicitly
confirms**, naming what will be lost: `--force` (discards uncommitted worktree changes)
and `--delete-unmerged` (deletes an unmerged branch).

## Status

```bash
~/.claude/skills/claude-cowboy/scripts/cowboy status          # active agents: queued, running, blocked, settled, stopped, exited
~/.claude/skills/claude-cowboy/scripts/cowboy status --all    # include agents that have been cleaned up
~/.claude/skills/claude-cowboy/scripts/cowboy status --json
```

## Templates

**Agent templates** are reusable roles. Pick one with `-a <name>`. The bundled ones are
`general-purpose` (the default) and `Explore` (read-only search). Users add their own as
`~/.claude/claude-cowboy/agents/<name>.toml`:

```toml
name = "release-notes"
description = "Prepares release notes."
harness_type = "claude"        # optional
model = "sonnet"               # optional
effort = "medium"              # optional
system_prompt = '''
You prepare release notes. Read git log / git tag and put the notes in your report.'''

[harness.claude]               # optional per-harness overrides
tools = ["Read", "Grep", "Glob", "Bash", "Write"]   # --tools allowlist
exclude_tools = ["WebFetch"]                       # --disallowedTools
model = "haiku"
```

**Orchestrator templates** add guidance that applies to every agent. Save them as
`~/.claude/claude-cowboy/orchestrators/<name>.toml` with `name` and `guidance` keys
(`review-before-commit` is bundled), then turn one on with
`~/.claude/skills/claude-cowboy/scripts/cowboy config --set orchestrator=review-before-commit` (set it to `null` to turn it off).

`~/.claude/skills/claude-cowboy/scripts/cowboy templates` lists everything available.

## Configuration

Settings live in `~/.claude/claude-cowboy/config.json`. Change them with
`~/.claude/skills/claude-cowboy/scripts/cowboy config --set key.path=value`; values are parsed as JSON.

| Key | Default | Meaning |
| --- | --- | --- |
| `concurrency.default` | `4` | Global cap on running agents |
| `concurrency.harnesses.<h>` | — | Cap per harness, e.g. `concurrency.harnesses.codex=2` |
| `concurrency.models.<m>` | — | Cap per model, e.g. `concurrency.models.opus=1` |
| `agent.harness` | `claude` | Default harness |
| `agent.template` | `general-purpose` | Default agent template |
| `agent.model` / `agent.effort` | `null` | Default model and effort |
| `agent.permission_mode` | `bypass` | `bypass` → `--dangerously-skip-permissions` (pi-cowboy's behavior); otherwise passed as `--permission-mode` |
| `agent.worktree_materialization` | `cow` | `cow` or `checkout` |
| `agent.base_branch` | `null` | Base ref (default: the current branch) |
| `orchestrator` | `null` | Active orchestrator template |
| `auto_trust` | `inherit` | Answer Claude's folder-trust dialog for new worktrees: `inherit` (only if the main repo is already trusted), `always`, or `never` |
| `idle_grace_seconds` | `45` | Seconds an agent can sit idle without a report before it counts as settled |

Model resolution order: `-m` flag, then the template's `[harness.<h>].model`, then the
template's `model`, then `agent.model`, then the harness's own default.

## Rules for you, the orchestrator

- Don't do a subtask yourself while an agent owns it, and don't edit an agent's worktree.
  Steer the agent instead.
- Give parallel agents non-overlapping files where you can, so merges stay clean.
- Merging, pushing, opening a PR and cleaning up each need the user's explicit go-ahead.
  A good review is not permission to land the work.
- Read reports critically and check the diff before merging. A report is the agent's
  claim, not proof.
- Never close Herdr workspaces you didn't create. Cleanup closes only the agent's own.
- Agents run with permission checks bypassed by default (as in pi-cowboy). Mention this
  the first time you spawn agents for a user.
