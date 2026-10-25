# claude-cowboy

A [Claude Code](https://claude.com/claude-code) skill for multi-agent orchestration — a Claude Code port of pi-cowboy.

Split a large job into focused subtasks and delegate them to background coding agents (Claude Code, Codex, pi, OpenCode). Each agent runs in its own Herdr workspace, isolated in a copy-on-write Git worktree on its own branch. The orchestrator then steers, reviews, merges and cleans them up.

See [`SKILL.md`](SKILL.md) for full usage.

## Requirements

- Claude Code
- Herdr (the skill must run inside a Herdr pane, `HERDR_ENV=1`)
- Git, Python 3
- Optional harnesses: `codex`, `pi`, `opencode`

## Install

```bash
git clone https://github.com/nicklausroach/claude-cowboy ~/.claude/skills/claude-cowboy
```

The skill calls its helper by the literal path `~/.claude/skills/claude-cowboy/scripts/cowboy`, so install it at exactly that location. You may want to add a permission allow-rule for that path, e.g. in `~/.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(~/.claude/skills/claude-cowboy/scripts/cowboy:*)"] } }
```

Then in Claude Code (inside Herdr, inside a Git repo): `/claude-cowboy <job>`.

## Notes

- Spawned agents run with permission checks **bypassed by default** (`agent.permission_mode = bypass`, matching pi-cowboy). Change it with `cowboy config --set agent.permission_mode=<mode>`.
- State lives in `$CLAUDE_COWBOY_HOME` (default `~/.claude/claude-cowboy`).
- Agent templates live in `agents/`, orchestrator templates in `orchestrators/`.
