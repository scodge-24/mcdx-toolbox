# Release validation

For maintainers preparing a distribution, not routine worksheet authoring.
Run commands in this reference from the repository root.

Before distribution, run the checked-in CI against this tree, install both the
wheel and sdist in fresh environments outside the checkout, and run the public
workflow tests against each. Confirm runtime schemas, templates, metrics and
PowerShell assets exist in the installed package. Run optional Prime tests and
visually inspect the generated page images on supported local hosts.

`uv run --locked python scripts/verify_release.py` runs formatting, lint, typing,
source tests, both package builds, and isolated installed-command/MCP tests.
The MCP dependency is bounded to 1.x: the 2.x SDK has an incompatible API and
requires a deliberate migration. The lockfile selects a supported 1.x patch.

Prime tests skip by default. On a supported local host, opt in with
`PYMCDX_TEST_PRIME=1 uv run pytest -m prime` (set the environment variable using
the shell's native syntax on Windows). A passing no-Prime CI run does not establish
desktop or numerical acceptance.

`claude plugin validate .claude-plugin/plugin.json` checks plugin structure and
`claude plugin validate .claude-plugin/marketplace.json` checks the single-plugin
marketplace that lets users run `/plugin marketplace add scodge-24/mcdx-toolbox`.
Validating the repository root checks only the marketplace, so CI validates both
files. Validation does not establish safe behaviour, numerical correctness,
licensing, host compatibility or acceptance into Anthropic's directory. The directory's current submission flow includes
separate validation/security review and publisher approval. Confirm portal-specific
size, file-type and binary restrictions when submitting; no numeric limit is
assumed here. This bundle includes source and text assets, not interpreter or
Prime executables. Dependency installers may obtain platform binaries separately.

Keep plugin and package versions equal. Regenerate and commit `uv.lock` after a
reviewed dependency change, and re-check dependency licences against the
repository's `NOTICE.md` whenever the lockfile changes. The local launcher uses
`uv run --locked --no-dev` against the plugin root. A future published-package launcher may use a pinned
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

## Public launch metadata

The repository topics describe Mathcad, Python/CLI, MCP and the Claude plugin;
they do not advertise a standalone numerical solver. The README links the real
CI workflow and MIT licence. Add a PyPI version badge and registry link only
after a package is actually published. Leave the repository homepage
unset until there is a dedicated documentation site.

`.github/social-preview.svg` is original project artwork using a synthetic
worksheet illustration, explicitly not a Prime render. Convert it to a 1280×640
PNG for GitHub (requires Cairo and the optional CairoSVG tool, not a runtime
package dependency):

```bash
uv run --no-project --with cairosvg==2.8.2 cairosvg .github/social-preview.svg -o social-preview.png
```

Inspect the PNG and keep it below 1 MB. The generated PNG is an upload artifact,
not a source export. GitHub's first social-preview upload requires a public
repository; after the owner changes visibility, upload it under Settings →
General → Social preview. See [GitHub's social-preview guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).

After the owner approves a release, confirm matching package/plugin versions,
green CI and the acceptance limits above, then tag the reviewed commit and create
a GitHub Release with those limits in its notes. Do not create a release, publish
to PyPI, change visibility or submit to a directory merely by running validation.
