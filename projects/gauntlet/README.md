# Gauntlet

Fable plans and reviews. Opus 4.6 builds. You decide at three gates.

| | |
| --- | --- |
| Author | [Cosmin Aftanase](https://github.com/cosmin220304) |
| Maintainer of this copy | [Light](https://github.com/light-space) |
| Status | Experimental |
| Language | Python / Markdown |
| License | [Not specified upstream](LICENSE) |

`/gauntlet:run <spec or ticket>` turns Fable into the product owner for one feature in the current worktree. It writes a brief that says what and why, splits the work only where a second engineer could take a part without stepping on the first, and spawns one `gauntlet-builder` per slice. Builders run on `claude-opus-4-6`. They explore the code, choose their own files, names and steps, write their own tests, and never run them. Fable never tells a builder how to build. A hook stops any builder that crosses 130k tokens of context and makes it hand off. Fable checks every diff for business sense, runs the tests itself, and ships through `/gauntlet:ship-it`.

The run stops to ask you three times:

1. Before any builder starts: what Fable understood, what is in and out, and every ambiguity it would otherwise decide alone.
2. After the builders report: does the code look right, and may it run validation and the tests.
3. With everything green: ship it?

A monitor at http://localhost:7777 shows every session and builder on this machine: what each one is doing right now, its context against the cap, how it ended (report, stopped, cap hit, compacted), and the uncommitted diff of its worktree, one file at a time.

## Install

You need Claude Code with access to `claude-opus-4-6`, `python3`, `git`, and `gh` for shipping.

Inside Claude Code:

```
/plugin marketplace add light-space/open-source
/plugin install gauntlet@light-open-source
```

From a checkout of this repository, for trying it out:

```sh
cd projects/gauntlet
claude --plugin-dir .
```

The project also retains its standalone marketplace. From this directory, use
`/plugin marketplace add .` and `/plugin install gauntlet@gauntlet` to install it
independently. Choose one installation source so the plugin is not enabled twice.

## Use

```
/gauntlet:run SHOP-142
/gauntlet:run docs/plans/payment-provider.md
/gauntlet:run "Add a second payment provider: checkout flow and refund flow"
```

Answer the three gates. Fixes you ask for at gate 2 become new builder slices and come back to the same gate.

The first run starts the monitor if nothing is listening on port 7777 and registers the session with its checkout; open http://localhost:7777. It shows the latest registered run of each checkout, grouped by checkout, and diffs that checkout, so a run started on main that moves into a worktree shows that worktree. It reads local transcripts under `~/.claude/projects` and sends nothing anywhere. The Changes tab loads its diff viewer from a CDN, so it needs internet access in the browser.

If the monitor is not up, or you want this session diffed against another checkout, run `/gauntlet:monitor` or `/gauntlet:monitor /path/to/worktree`. It starts the monitor if needed, registers the session, and opens the page.

## Knobs

- `GAUNTLET_CONTEXT_CAP` (default `130000`): the builder context cap, read by the hook and the monitor.
- `agents/gauntlet-builder.md`: the builder's model, turn limit, and working rules.
- `commands/ship-it.md`: how a run becomes a PR. Edit the commit and PR conventions there.

## Output style

The plugin ships a "Simple language" output style and forces it on while the plugin is enabled: Simplified Technical English, active voice, one idea per sentence, bullets over paragraphs. It applies to every conversation, not only gauntlet runs. Disable the plugin to get your own style back, or edit `output-styles/simple-language.md` and drop the `force-for-plugin` line to make it optional.

## Layout

```
skills/run/SKILL.md     the orchestration playbook Fable follows
skills/run/watch.py     the monitor, one stdlib Python file
agents/gauntlet-builder.md
commands/ship-it.md
commands/monitor.md     start the monitor, register this session, open the page
hooks/hooks.json        PreToolUse hook wiring
hooks/context-cap.py    blocks a gauntlet builder's next tool call past the cap
output-styles/simple-language.md
```

## Development

The monitor and hook use the Python standard library; there are no package
dependencies to install and no application build. From the repository root:

```sh
cd projects/gauntlet
claude plugin validate --strict .
claude plugin validate --strict .claude-plugin/plugin.json
python3 -m py_compile hooks/context-cap.py skills/run/watch.py
```

Both validation commands should report `Validation passed`. Python compilation
should exit successfully without output. Upstream does not include an automated
test suite. To check interactive behavior, run the usage example above in a
disposable worktree, follow the three review gates, and inspect the monitor at
http://localhost:7777. This requires Claude Code model access and performs real
agent work against that worktree.

Follow the [contribution guide](../../CONTRIBUTING.md) and [project instructions](AGENTS.md).
If marketplace metadata changes, also update this project's entry in
[`../../.claude-plugin/marketplace.json`](../../.claude-plugin/marketplace.json) and
validate the repository-root marketplace. Keep this project's runtime files here.

## Origin and license

Copied from [cosmin220304/gauntlet](https://github.com/cosmin220304/gauntlet) at
commit [`1f5c5e9`](https://github.com/cosmin220304/gauntlet/commit/1f5c5e99b4e6fd6825a91f4cdeb9440387837ba4),
version `0.1.26`. The plugin manifests, agents, commands, hooks, skills, and output
style are copied unchanged. This README adapts installation paths and adds project
metadata, development instructions, and attribution. The local example uses
Claude Code's normal permission handling.

Upstream did not include a license file or a license declaration at this commit.
The [licensing status file](LICENSE) records that fact; this copy does not assign a
new license. Light MCP's MIT license applies to Light MCP only.
