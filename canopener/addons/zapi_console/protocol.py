"""What we know about the Zapi console protocol, as code: decoders for messages we've worked out.

**Nothing is known yet.** Every decoder added here must come from our own recordings, and its
finding must be in the findings table of docs/ZAPI-CONSOLE.md (with the capture it came from) in
the same change; ``Decoder.finding`` is that table's number. CLAUDE.md "Provenance" says what
may and may not be a source. Until a recording confirms a decoder, ``confirmed`` stays False and
the text it gives ends in "?".

``describe(message)`` is what the screens show beside a message: a decoder's text, else any
plain text in it (console menu screens are often characters), else "".
"""

from collections.abc import Callable
from dataclasses import dataclass

from ...core.serial_analysis import Message, text_runs


@dataclass(frozen=True)
class Decoder:
    name: str
    finding: int                                    # row in docs/ZAPI-CONSOLE.md#findings
    matches: Callable[[Message], bool]
    decode: Callable[[Message], str]
    confirmed: bool = False


DECODERS: list[Decoder] = []


def describe(message: Message) -> str:
    for d in DECODERS:
        try:
            if d.matches(message):
                text = d.decode(message)
                return text if d.confirmed else f"{text} ?"
        except (IndexError, ValueError):
            continue
    runs = text_runs([message], min_len=3)
    return " | ".join(runs) if runs else ""
