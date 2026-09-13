# Light MCP

This project is a Claude Code plugin marketplace containing a single MCP plugin.
It connects to Light's hosted server; it does not implement or run that server.

- Plugin configuration lives in `plugins/light-mcp/.claude-plugin/plugin.json`.
- `.claude-plugin/marketplace.json` lets this project work as a standalone local
  marketplace. The root marketplace points to these same plugin files for GitHub
  installation. Keep metadata in both catalogs consistent.
- Preserve `${LIGHT_MCP_TOKEN}` as an environment-variable reference. Never put a
  real token in configuration, examples, tests, or commits.
- Do not change the hosted server URL or authentication behavior when editing docs
  or moving files. Such changes need a task that specifically calls for them.
- Keep the MIT license in both the project root and the plugin directory, because
  Claude Code copies only the plugin directory into its installation cache.
- Run `claude plugin validate .` and `claude plugin validate ./plugins/light-mcp`
  from this directory. Validate the repository root if its marketplace entry changes.
- Test marketplace registration and installation in an isolated Claude Code config.
  Do not modify a contributor's existing plugin registrations for a test.
- Report separately whether schema validation, installation, and a live connection
  were checked. A live connection requires a Light account and MCP token.
