"""orf capabilities — Print ORF module capabilities.

Companion CLI to the ORF MCP get_capabilities tool. Prints the
16 MD output formats, 9 XLIFF backfill formats, and 7 MCP tool names.
"""
from __future__ import annotations

import json

import click

from orf.mcp.tools.get_capabilities import get_capabilities


@click.command("capabilities")
@click.option("--json", "output_json", is_flag=True, default=True, help="Output as JSON (default)")
@click.option("--pretty", is_flag=True, default=False, help="Pretty-print instead of raw JSON")
def capabilities(output_json: bool, pretty: bool) -> None:
    """Print ORF module capabilities (formats, tools)."""
    result = get_capabilities()
    data = json.loads(result)
    if not data.get("success"):
        click.echo(f"Error: {result}", err=True)
        raise click.ClickException("Failed to retrieve capabilities")

    if pretty:
        typer_echo_json(data["content"])
    else:
        click.echo(result)


def typer_echo_json(content: dict) -> None:
    """Pretty-print the capabilities dict."""
    click.echo(f"Module: {content.get('module')}")
    click.echo(f"Version: {content.get('version')}")
    click.echo(f"Input formats: {', '.join(content.get('input_formats', []))}")
    click.echo(f"MD output formats ({len(content.get('output_formats', []))}): {', '.join(content.get('output_formats', []))}")
    click.echo(f"XLIFF backfill formats ({len(content.get('xliff_backfill_formats', []))}): {', '.join(content.get('xliff_backfill_formats', []))}")
    click.echo(f"Tools ({len(content.get('tools', []))}):")
    for tool in content.get("tools", []):
        click.echo(f"  - {tool}")
