"""ORF format conversion channels."""

from orf.channels.xliff2json import apply_xliff_to_json

# Dispatch table: channel name → callable
CHANNELS: dict[str, callable] = {
    "xliff2json": apply_xliff_to_json,
}

__all__ = [
    "apply_xliff_to_json",
    "CHANNELS",
]
