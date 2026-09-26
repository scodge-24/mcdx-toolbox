# Release validation

Before distribution, run the checked-in CI against this tree, install both the
wheel and sdist in fresh environments outside the checkout, and run the public
workflow tests against each. Confirm runtime schemas, templates, metrics and
PowerShell assets exist in the installed package. Run optional Prime tests and
visually inspect the generated page images on supported local hosts.

`uv run --locked python scripts/verify_release.py` runs formatting, lint, typing,
source tests, both package builds, and isolated installed-command/MCP tests.
The MCP dependency is bounded to 1.x: the 2.x SDK has an incompatible API and
requires a deliberate migration. The lockfile selects a supported 1.x patch.

`claude plugin validate .` checks plugin structure. It does not establish safe
behaviour, numerical correctness, licensing, host compatibility or acceptance
into Anthropic's directory. The directory's current submission flow includes
separate validation/security review and publisher approval. Confirm portal-specific
size, file-type and binary restrictions when submitting; no numeric limit is
assumed here. This bundle includes source and text assets, not interpreter or
Prime executables. Dependency installers may obtain platform binaries separately.

Keep plugin and package versions equal. Regenerate and commit `uv.lock` after a
reviewed dependency change. The local launcher uses `uv run --locked --no-dev`
against the plugin root. A future published-package launcher may use a pinned
`uvx --from mcdx-toolbox==VERSION pymcdx mcp`, only after that version actually
exists under the publisher's control. pipx is a user-managed installation option,
not an automatic plugin bootstrap step.

Official references checked during preparation:

- https://code.claude.com/docs/en/plugins-reference
- https://support.claude.com/en/articles/13837440-use-plugins-in-claude
- https://claude.com/blog/build-plugins-for-claude

Test native Windows and WSL separately. Cowork runtime isolation may prevent
desktop COM access; do not advertise that integration based on CLI success alone.
Normal Claude chat cannot run this local MCP server via the plugin.
