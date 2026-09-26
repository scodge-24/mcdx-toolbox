"""Readable entrypoint for the bundled worksheet MCP server."""

from pymcdx import worksheet_mcp


def main() -> None:
    worksheet_mcp.main()


if __name__ == "__main__":
    main()
