"""Generate deterministic engineering GIFs from real benchmark trace events.

The renderer deliberately has no scenario timeline of its own: every frame is
the cumulative view after one event from the input JSONL.  Story validation
only checks that the trace contains the events needed to tell that story.
Unknown event types are displayed as ``UNKNOWN:<type>`` and do not abort a
trace, which keeps forward-compatible traces inspectable.

This module uses only the Python standard library.  The small GIF89a writer
keeps output reproducible in CI and avoids introducing a plotting dependency
into the benchmark runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


SUPPORTED_EVENTS = frozenset(
    {
        "AGENT_CLAIM",
        "TASK_CREATED",
        "STEP_DISPATCHED",
        "TOOL_CALL_STARTED",
        "TOOL_CALL_COMPLETED",
        "FAULT_INJECTED",
        "FAILURE_DETECTED",
        "REPAIR_STARTED",
        "REPAIR_COMPLETED",
        "VERIFICATION_STARTED",
        "VERIFICATION_PASSED",
        "VERIFICATION_FAILED",
        "STEP_VERIFIED",
        "StepFailureProvenance",
        "WAITING_FOR_VERIFICATION",
    }
)

REQUIRED_FIELDS = (
    "timestamp",
    "event_type",
    "task_id",
    "step_id",
    "attempt_id",
    "status",
    "metadata",
)


class TraceEventError(ValueError):
    """Raised when a trace cannot support the requested demonstrations."""


@dataclass(frozen=True)
class TraceEvent:
    timestamp: Any
    event_type: str
    task_id: str
    step_id: str
    attempt_id: str
    status: str
    metadata: dict[str, Any]
    ordinal: int


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def load_trace(path: Path) -> list[TraceEvent]:
    """Load one JSONL trace while preserving the recorded event order."""
    if not path.is_file():
        raise TraceEventError(f"TRACE_NOT_FOUND:{path}")
    events: list[TraceEvent] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise TraceEventError(f"INVALID_JSONL:{path}:{line_number}") from exc
            if not isinstance(record, dict):
                raise TraceEventError(f"EVENT_NOT_OBJECT:{path}:{line_number}")
            missing = [field for field in REQUIRED_FIELDS if field not in record]
            if missing:
                raise TraceEventError(
                    f"TRACE_SCHEMA_MISSING:{path}:{line_number}:{','.join(missing)}"
                )
            event_type = _string(record["event_type"]).strip()
            if not event_type:
                raise TraceEventError(f"EMPTY_EVENT_TYPE:{path}:{line_number}")
            metadata = record["metadata"]
            if not isinstance(metadata, dict):
                raise TraceEventError(f"METADATA_NOT_OBJECT:{path}:{line_number}")
            events.append(
                TraceEvent(
                    timestamp=record["timestamp"],
                    event_type=event_type,
                    task_id=_string(record["task_id"]),
                    step_id=_string(record["step_id"]),
                    attempt_id=_string(record["attempt_id"]),
                    status=_string(record["status"]),
                    metadata=dict(metadata),
                    ordinal=len(events),
                )
            )
    if not events:
        raise TraceEventError(f"TRACE_EMPTY:{path}")
    return events


def _metadata_value(event: TraceEvent, *names: str) -> str:
    for name in names:
        if name in event.metadata and event.metadata[name] is not None:
            return _string(event.metadata[name])
    return ""


def _configuration(event: TraceEvent) -> str:
    return _metadata_value(event, "config", "configuration", "config_name", "lane").casefold()


def _has(events: Sequence[TraceEvent], event_type: str) -> bool:
    return any(event.event_type == event_type for event in events)


def _require_story(events: Sequence[TraceEvent], story: str) -> None:
    present = {event.event_type for event in events}
    if story == "false_completion_prevention":
        required = {"VERIFICATION_STARTED", "VERIFICATION_FAILED"}
        missing = required - present
    elif story == "selective_repair":
        required = {
            "STEP_VERIFIED",
            "FAILURE_DETECTED",
            "REPAIR_STARTED",
            "REPAIR_COMPLETED",
            "VERIFICATION_PASSED",
        }
        missing = required - present
    elif story == "baseline_vs_odys":
        required = {"FAILURE_DETECTED", "STEP_DISPATCHED", "REPAIR_STARTED", "REPAIR_COMPLETED", "VERIFICATION_PASSED"}
        missing = required - present
        configs = {_configuration(event) for event in events} - {""}
        if not ({"minimal", "baseline"} & configs):
            missing = set(missing) | {"config=minimal or config=baseline"}
        if not ({"odys", "odys_p3"} & configs):
            missing = set(missing) | {"config=odys_p3"}
    else:
        raise TraceEventError(f"UNKNOWN_STORY:{story}")
    if missing:
        raise TraceEventError(
            f"MISSING_REQUIRED_EVENT:{story}:{','.join(sorted(missing))}"
        )


def _story_events(events: Sequence[TraceEvent], story: str) -> list[TraceEvent]:
    """Select only explicitly tagged story events when tags are present.

    Untagged traces are kept intact.  No synthetic event is added and no event
    is reordered; the source trace remains the sole timeline authority.
    """
    tagged = [
        event
        for event in events
        if _metadata_value(event, "story", "scenario")
        and _metadata_value(event, "story", "scenario").casefold() == story.casefold()
    ]
    return tagged if tagged else list(events)


# A compact fixed-width font for labels.  Unsupported characters are rendered
# as a question mark rather than changing the trace or failing the render.
_FONT: dict[str, tuple[str, ...]] = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "?": ("11110", "00001", "00010", "00100", "00100", "00000", "00100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ":": ("00000", "00100", "00000", "00000", "00100", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}


def _add_letters() -> None:
    patterns = {
        "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
        "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
        "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
        "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
        "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
        "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
        "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
        "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
        "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
        "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
        "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
        "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
        "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
        "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
        "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
        "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
        "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
        "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
        "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
        "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
        "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
        "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
        "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
        "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    }
    _FONT.update(patterns)


_add_letters()


class _Canvas:
    WIDTH = 480
    HEIGHT = 270

    def __init__(self) -> None:
        self.pixels = [0] * (self.WIDTH * self.HEIGHT)

    def fill(self, color: int) -> None:
        self.pixels[:] = [color] * len(self.pixels)

    def rect(self, x: int, y: int, width: int, height: int, color: int, *, filled: bool = True) -> None:
        for yy in range(max(0, y), min(self.HEIGHT, y + height)):
            for xx in range(max(0, x), min(self.WIDTH, x + width)):
                if filled or yy in (y, y + height - 1) or xx in (x, x + width - 1):
                    self.pixels[yy * self.WIDTH + xx] = color

    def line(self, x1: int, y1: int, x2: int, y2: int, color: int) -> None:
        dx = abs(x2 - x1)
        sx = 1 if x1 < x2 else -1
        dy = -abs(y2 - y1)
        sy = 1 if y1 < y2 else -1
        error = dx + dy
        while True:
            if 0 <= x1 < self.WIDTH and 0 <= y1 < self.HEIGHT:
                self.pixels[y1 * self.WIDTH + x1] = color
            if x1 == x2 and y1 == y2:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x1 += sx
            if twice <= dx:
                error += dx
                y1 += sy

    def text(self, x: int, y: int, value: str, color: int, *, scale: int = 2, limit: int | None = None) -> None:
        text = value.upper()
        if limit is not None:
            text = text[:limit]
        cursor = x
        for character in text:
            pattern = _FONT.get(character, _FONT["?"])
            for row, bits in enumerate(pattern):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(cursor + column * scale, y + row * scale, scale, scale, color)
            cursor += 6 * scale


PALETTE = (
    (15, 20, 28),    # 0 background
    (232, 238, 245), # 1 primary text
    (125, 141, 160), # 2 muted text
    (53, 69, 86),    # 3 panel
    (57, 190, 125),  # 4 success
    (235, 178, 66),  # 5 warning
    (226, 83, 83),   # 6 failure
    (73, 157, 218),  # 7 accent
    (36, 48, 62),    # 8 grid
    (18, 27, 38),    # 9 panel dark
    (174, 104, 207), # 10 repair
    (89, 202, 202),  # 11 info
    (255, 255, 255), # 12 white
    (100, 110, 120), # 13 gray
    (30, 120, 90),   # 14 dark success
    (130, 45, 45),   # 15 dark failure
)


def _color_for_event(event: TraceEvent) -> int:
    if event.event_type in {"VERIFICATION_PASSED", "STEP_VERIFIED", "REPAIR_COMPLETED"}:
        return 4
    if event.event_type in {"FAILURE_DETECTED", "VERIFICATION_FAILED", "FAULT_INJECTED"}:
        return 6
    if event.event_type in {"REPAIR_STARTED", "VERIFICATION_STARTED"}:
        return 10
    if event.event_type.startswith("UNKNOWN:"):
        return 5
    return 7


def _event_label(event: TraceEvent) -> str:
    label = event.event_type if event.event_type in SUPPORTED_EVENTS else f"UNKNOWN:{event.event_type}"
    details = []
    for key in ("failure_class", "repair_scope", "state", "phase"):
        value = _metadata_value(event, key)
        if value:
            details.append(f"{key}={value}")
    if event.status:
        details.append(f"status={event.status}")
    return label + (" " + " ".join(details) if details else "")


def _metrics(events: Sequence[TraceEvent]) -> dict[str, int]:
    return {
        "events": len(events),
        "tasks": len({event.task_id for event in events if event.task_id}),
        "steps": len({event.step_id for event in events if event.step_id}),
        "attempts": len({event.attempt_id for event in events if event.attempt_id}),
        "failures": sum(event.event_type in {"FAILURE_DETECTED", "VERIFICATION_FAILED"} for event in events),
        "repairs": sum(event.event_type == "REPAIR_COMPLETED" for event in events),
        "verified": sum(event.event_type in {"VERIFICATION_PASSED", "STEP_VERIFIED"} for event in events),
    }


def _draw_frame(story: str, events: Sequence[TraceEvent], current: int) -> list[int]:
    canvas = _Canvas()
    canvas.fill(0)
    canvas.text(18, 14, story.replace("_", " "), 1, scale=2, limit=48)
    canvas.text(18, 34, "REAL TRACE / FRAME %d OF %d" % (current + 1, len(events)), 2, scale=1, limit=54)
    canvas.rect(16, 54, 448, 1, 8)

    shown = events[: current + 1]
    max_rows = 7
    start = max(0, len(shown) - max_rows)
    for row, event in enumerate(shown[start:], start=0):
        y = 68 + row * 22
        color = _color_for_event(event)
        canvas.rect(18, y, 8, 8, color)
        canvas.text(34, y - 2, _event_label(event), 1, scale=1, limit=68)
        identity = f"task={event.task_id} step={event.step_id} attempt={event.attempt_id}"
        canvas.text(34, y + 9, identity, 2, scale=1, limit=68)

    metric = _metrics(shown)
    metric_text = (
        f"EVENTS {metric['events']}  TASKS {metric['tasks']}  STEPS {metric['steps']}  "
        f"FAILURES {metric['failures']}  REPAIRS {metric['repairs']}  VERIFIED {metric['verified']}"
    )
    canvas.rect(16, 236, 448, 20, 9)
    canvas.text(22, 243, metric_text, 1, scale=1, limit=73)
    return canvas.pixels


def _lzw_subblocks(pixels: Sequence[int], min_code_size: int = 4) -> bytes:
    """Encode indexed pixels with a simple valid GIF LZW stream."""
    clear = 1 << min_code_size
    end = clear + 1
    code_size = min_code_size + 1
    # Emit a clear code before every literal.  That intentionally keeps the
    # decoder's table and code width fixed, making this tiny encoder easy to
    # audit and valid for arbitrarily long traces without a mutable dictionary.
    codes = [clear]
    for pixel in pixels:
        codes.extend((clear, int(pixel)))
    codes.append(end)
    packed = bytearray()
    accumulator = 0
    bits = 0
    for code in codes:
        accumulator |= int(code) << bits
        bits += code_size
        while bits >= 8:
            packed.append(accumulator & 0xFF)
            accumulator >>= 8
            bits -= 8
    if bits:
        packed.append(accumulator & 0xFF)
    output = bytearray([min_code_size])
    for offset in range(0, len(packed), 255):
        block = packed[offset : offset + 255]
        output.append(len(block))
        output.extend(block)
    output.append(0)
    return bytes(output)


def _gif_bytes(frames: Sequence[Sequence[int]], *, delay_cs: int = 12) -> bytes:
    if not frames:
        raise ValueError("GIF_REQUIRES_FRAME")
    width, height = _Canvas.WIDTH, _Canvas.HEIGHT
    data = bytearray(b"GIF89a")
    # The palette has 16 entries, so the GIF size-of-table bits are 3
    # (2 ** (3 + 1) == 16); the color-resolution bits remain 7.
    data.extend(struct.pack("<HHBBB", width, height, 0xF3, 0, 0))
    for red, green, blue in PALETTE:
        data.extend((red, green, blue))
    data.extend(b"!\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")
    for pixels in frames:
        data.extend(b"!\xf9\x04\x00")
        data.extend(struct.pack("<H", delay_cs))
        data.extend(b"\x00\x00")
        data.extend(b",\x00\x00\x00\x00")
        data.extend(struct.pack("<HH", width, height))
        data.append(0)
        data.extend(_lzw_subblocks(pixels))
    data.append(0x3B)
    return bytes(data)


def _render_story(events: Sequence[TraceEvent], story: str, output: Path) -> str:
    selected = _story_events(events, story)
    _require_story(selected, story)
    frames = [_draw_frame(story, selected, index) for index in range(len(selected))]
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = _gif_bytes(frames)
    output.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def generate_gifs(trace_path: Path, output_dir: Path) -> dict[str, Path]:
    events = load_trace(trace_path)
    names = {
        "false_completion_prevention": "false_completion_prevention.gif",
        "selective_repair": "selective_repair.gif",
        "baseline_vs_odys": "baseline_vs_odys.gif",
    }
    # Validate the complete set before writing anything.  A partial output
    # directory would falsely suggest that the missing story was observed.
    selected: dict[str, list[TraceEvent]] = {}
    for story in names:
        selected[story] = _story_events(events, story)
        _require_story(selected[story], story)
    outputs: dict[str, Path] = {}
    for story, filename in names.items():
        output = output_dir / filename
        _render_story(selected[story], story, output)
        outputs[story] = output
    return outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path, help="benchmark execution trace JSONL")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("evals/reliability/demo_gif"),
        help="directory for generated GIFs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outputs = generate_gifs(args.trace, args.output_dir)
    except TraceEventError as exc:
        print(f"trace error: {exc}", file=sys.stderr)
        return 2
    for path in outputs.values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
