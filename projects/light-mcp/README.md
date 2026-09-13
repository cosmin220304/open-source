# Light MCP Plugin for Claude Code

A [Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces) that connects Claude Code to [Light](https://light.inc)'s financial platform via MCP.

Once installed, Claude Code can query your bills, vendors, cards, expenses, ledger accounts, and more — directly from your terminal.

| | |
| --- | --- |
| Maintainer | [Light](https://github.com/light-space) |
| Status | Active |
| Language | JSON configuration |
| License | [MIT](LICENSE) |

> **Full setup guide:** [Connect your AI assistant to Light](https://help.light.inc/ai-features/how-to-use-light-mcp)

## Prerequisites

- A Light account with MCP enabled (see [Connect your AI assistant to Light](https://help.light.inc/ai-features/how-to-use-light-mcp))
- [Claude Code](https://code.claude.com) installed

## Setup

### 1. Generate a Light MCP token

1. Open **Settings > Profile** in Light
2. Scroll to **MCP Tokens**
3. Click **Add**, enter a name (e.g. `Claude Code`), and click **Create**
4. Copy the token (starts with `lmcp_`) — it won't be shown again

### 2. Set the token as an environment variable

Add this to your shell profile (`~/.zshrc`, `~/.bashrc`, etc.):

```sh
export LIGHT_MCP_TOKEN="lmcp_paste_your_token_here"
```

Then reload your shell:

```sh
source ~/.zshrc  # or ~/.bashrc
```

### 3. Install the plugin

In Claude Code, run:

```
/plugin marketplace add light-space/open-source
/plugin install light-mcp@light-open-source
```

Restart Claude Code. Light tools will appear in the tool list.

### Verify

Ask Claude Code:

> "What Light tools do you have available?"

If the connection is working, it will list the available Light tools.

## What's included

The plugin registers Light's remote MCP server (`https://api.light.inc/rest/ext/mcp`) which provides tools for:

- **Bills** — search, view details, and manage invoice payables
- **Vendors** — search and look up vendor/supplier information
- **Cards** — list and inspect corporate cards
- **Card transactions** — search, view, and modify draft transactions
- **Expenses** — view draft expenses and submit for reimbursement
- **Accounts** — search chart of accounts and tax codes
- **Users** — search team members
- **Tasks** — view pending approvals
- **Policy** — query company policies
- **Help articles** — browse Light platform documentation

## Links

- [Connect your AI assistant to Light](https://help.light.inc/ai-features/how-to-use-light-mcp) — full setup guide
- [Light API documentation](https://docs.light.inc)
- [Claude Code plugin marketplace docs](https://code.claude.com/docs/en/plugin-marketplaces)

## Development

This project contains JSON configuration for Light's remote MCP server. There is no
local server, dependency installation, or application build.

From a checkout of this repository:

```sh
cd projects/light-mcp
claude plugin validate .
claude plugin validate ./plugins/light-mcp
```

To test installation from this project alone, start Claude Code in this directory
and run:

```text
/plugin marketplace add .
/plugin install light-mcp@light-mcp
```

The project-local marketplace retains the name `light-mcp`. The repository-wide
marketplace is named `light-open-source`; both point to the same plugin files.
Use a separate test configuration when switching marketplace sources so you do not
enable the same MCP server twice. A live connection requires a valid `LIGHT_MCP_TOKEN`.

If you change marketplace metadata, also update this project's entry in
[`../../.claude-plugin/marketplace.json`](../../.claude-plugin/marketplace.json) and
run `claude plugin validate .` from the repository root.

## Contributing

Follow the [repository contribution guide](../../CONTRIBUTING.md) and the
[project instructions](AGENTS.md). Keep this plugin's implementation in this folder.

## Origin

Copied from [light-space/light-mcp](https://github.com/light-space/light-mcp) at
commit [`34ee2f6`](https://github.com/light-space/light-mcp/commit/34ee2f6d53bead668aba099eec009671e9fe75d3).
The MCP server URL and token header are unchanged. Version 1.0.1 adds the explicit
HTTP transport type required by Claude Code's manifest validator, plus author and
license metadata. Repository links and installation instructions have been adapted
for this repository, and contributor documentation and the MIT license text have
been added.

## License

[MIT](LICENSE), as declared by the original project's README and marketplace manifest.
