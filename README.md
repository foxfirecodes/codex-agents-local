# Codex Agents Local

Codex Agents Local loads applicable `AGENTS.local.md` files into Codex task
context at session start and when a subagent starts.

## Install

Trust and add this repository's `.agents/plugins/marketplace.json` marketplace
in Codex, then install **Codex Agents Local**. Review the hook source before
installing because it reads local instruction files and supplies their contents
to Codex.

## Use

Start, clear, or compact a Codex session to run the `SessionStart` hook;
resuming a session does not run it. Starting a subagent runs `SubagentStart`.
Both invoke:

```text
python3 "${PLUGIN_ROOT}/src/hook.py"
```

## Runtime

Codex discovers `hooks/hooks.json` automatically. The hook requires Python 3.9+
and only the standard library. It finds the nearest ancestor with a `.git` file
or directory, then reads the fixed filename `AGENTS.local.md` from that root
through the current working directory, in root-to-cwd order. If no `.git`
marker is found, it considers the current working directory only.

It reads at most 16 nonempty regular files, each up to 16 KiB and collectively
up to 32 KiB. Symlinks and other non-regular files are skipped; the reader also
checks the opened file against the pathname to avoid following a replacement
symlink. Hook output is configured with an approximate 10,000-token additional
context limit.

The hook does not parse `AGENTS.md` or `.git`, follow a linked worktree back to
its main repository, run Git or other subprocesses, access the network, or
write files. Local instructions are loaded once per configured start event;
changing directories later does not reload them.

## Security

Installing this plugin authorizes local execution of `src/hook.py` for the
configured lifecycle events. Trust the hook source and review future changes
before installing, especially if local instruction files contain sensitive data.

## License

MIT. See [LICENSE](LICENSE).
