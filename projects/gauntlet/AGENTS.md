# Gauntlet

Gauntlet is an independent Claude Code plugin copied from
`cosmin220304/gauntlet`. Its author is Cosmin Aftanase. Read the README's origin and
licensing status before changing attribution or distributing a modified version.

## Boundaries

- Keep the plugin manifests, agents, commands, hooks, skills, and output styles
  inside this project. The root marketplace only points to this directory.
- Preserve the upstream plugin name, version, and author during imports. Record
  the exact upstream commit in the README. Change the version when releasing a
  behavioral modification and keep the root marketplace metadata consistent.
- Files in `skills/`, `commands/`, `agents/`, and `output-styles/` define the plugin's
  behavior. Editing or importing them does not require running the Gauntlet workflow.
- Resolve bundled files through `${CLAUDE_PLUGIN_ROOT}` as upstream does. Do not
  introduce paths to sibling projects or a developer's checkout.
- The Python scripts use the standard library. Keep any future dependencies local
  to this project and document them.
- Upstream has no declared license at the imported revision. `LICENSE` records the
  status; it does not grant a license. Do not substitute another project's license
  or invent a license declaration.

## Verification

From this directory, run:

```sh
claude plugin validate --strict .
claude plugin validate --strict .claude-plugin/plugin.json
python3 -m py_compile hooks/context-cap.py skills/run/watch.py
```

Validate the repository root if its marketplace entry changes. Test plugin
installation with an isolated `CLAUDE_CONFIG_DIR` outside the repository. Use
synthetic transcripts and temporary directories for hook and monitor checks; do
not read or modify a contributor's real sessions as a test fixture.

Report manifest validation, installation, local smoke checks, and live agent runs
separately. A successful installation does not verify model access or a full run.
