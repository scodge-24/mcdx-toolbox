# Privacy

mcdx-toolbox is an open-source tool that runs entirely on your own machine.
It collects no data about you and sends nothing to the project or anyone else.

## What the toolbox does with your data

- **No collection, telemetry or analytics.** The package contains no networking
  code and has no account, sign-in or usage tracking. See
  [SECURITY.md](SECURITY.md) for the evidence.
- **Your files stay local.** It reads and writes only the worksheets, YAML,
  images and other files that you or your agent point it at, plus temporary
  files it cleans up.
- **Rendering stays local.** On Windows it opens your worksheet in your own
  installed Mathcad Prime. Nothing is uploaded.

## What other services see

- **Your AI agent.** When an agent uses these tools, the results (worksheet
  outlines, YAML, layout reports, page images) are returned to that agent and
  become part of your conversation. How that conversation is handled is governed
  by your agent provider's privacy policy, for example Anthropic's for Claude.
- **Package downloads.** On first launch, `uv` downloads the pinned dependencies
  from the Python Package Index, which sees an ordinary download request.
- **GitHub.** Installing the plugin or cloning the repository fetches it from
  GitHub. Issues and discussions you post there are public and covered by
  GitHub's privacy statement. Don't include confidential worksheets or project
  details in them.

## Contact

Ask privacy questions through the repository's
[issues](https://github.com/scodge-24/mcdx-toolbox/issues), or report a security
concern privately as described in [SECURITY.md](SECURITY.md).
