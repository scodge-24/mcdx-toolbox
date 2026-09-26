# Reporting bugs and missing capabilities

mcdx-toolbox improves from real use. When you hit something it can't do, tell
the user and offer to draft an issue for
<https://github.com/scodge-24/mcdx-toolbox/issues>.

## When to offer

Offer once per distinct problem, at a natural pause, not mid-task:

- **Missing capability:** a block, expression, function, unit, style, paper size
  or layout the user needs that the schema or builder rejects, or that you had
  to work around.
- **Import gap:** `import-yaml` reported unsupported regions the user cares
  about.
- **Bug:** a crash or traceback, a worksheet Prime refuses or repairs on open,
  output that contradicts this skill's references, or a render that differs from
  the layout check in a way the references don't explain.
- **Documentation gap:** you had to guess because these references were silent
  or wrong.

Don't offer for Mathcad Express licence limits, the user's own mistakes, or
behaviour the references already describe as a limitation, unless the user
wants that limitation lifted.

## Never publish without the user

Filing an issue publishes it. Draft it, show the user the full text, and file
it only when they ask. If you can't file it, give them the draft and the
link above.

Issues are public. Never include the user's worksheets, company names, project
names, client data, file paths, credentials, or extracts of paid standards.
Reduce the problem to the smallest **synthetic** YAML or Python that shows it,
using made-up names and values, and confirm it still reproduces before
including it. If you can't reproduce it synthetically, describe it in words
and say so.

## Gather the details

```bash
uv run --project <plugin-directory> python -c "from importlib.metadata import version; print(version('mcdx-toolbox'))"
```

Also note the operating system (Windows, WSL, Linux or macOS), how the toolbox
is being used (Claude Code plugin, other MCP client, CLI or Python), and the
Mathcad Prime or Express version if a render is involved.

## Bug report skeleton

Title: `<command or tool>: <what goes wrong>`, for example
`build: kN/m² display unit rejected`.

````markdown
**What happened**
<one or two sentences>

**What I expected**
<one or two sentences, citing the skill reference if it says otherwise>

**Minimal reproduction**
```yaml
<smallest synthetic YAML, or the Python for `generate`>
```
Command: `pymcdx <command> ...` (or MCP tool name and arguments)

**Output**
```text
<error message or traceback, trimmed; no private paths>
```

**Environment**
- mcdx-toolbox: <version>
- OS: <Windows / WSL / Linux / macOS>
- Used via: <Claude Code plugin / MCP client / CLI / Python>
- Mathcad: <Prime version, Express, or not involved>
````

## Feature request skeleton

Title: `Support <capability>`, for example `Support Letter paper size`.

````markdown
**What I'm trying to do**
<the engineering task, in a sentence or two>

**What's missing**
<the block, expression, unit, layout or import behaviour that isn't supported>

**Example**
<how it could look in the YAML, or a synthetic example of the Mathcad
construct; a description is fine if no syntax is obvious>

**Current workaround**
<what you did instead, or "none">

**Environment**
- mcdx-toolbox: <version>
- Used via: <Claude Code plugin / MCP client / CLI / Python>
````
