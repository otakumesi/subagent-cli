"""Very small YAML subset parser for offline bootstrap.

This intentionally supports only the subset used by subagent v1 config:
- mappings via `key: value` and nested indentation
- lists via `- item`
- block scalar literal via `|` for multi-line strings
- scalar values: string, int, float, bool, null, `{}`, `[]`
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Line:
    indent: int
    content: str


class ParseError(ValueError):
    pass


def parse_yaml_subset(text: str) -> Any:
    lines = _tokenize(text)
    if not lines:
        return {}
    parser = _Parser(lines)
    value = parser.parse_block(0)
    if parser.has_next():
        raise ParseError("Unexpected trailing content")
    return value if value is not None else {}


def _tokenize(text: str) -> list[Line]:
    out: list[Line] = []
    for raw in text.splitlines():
        if "\t" in raw:
            raise ParseError("Tab indentation is not supported")
        stripped_comments = _strip_inline_comment(raw)
        if not stripped_comments.strip():
            continue
        indent = len(stripped_comments) - len(stripped_comments.lstrip(" "))
        out.append(Line(indent=indent, content=stripped_comments.strip()))
    return out


def _strip_inline_comment(line: str) -> str:
    in_single = False
    in_double = False
    for idx, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip()
    return line.rstrip()


class _Parser:
    def __init__(self, lines: list[Line]) -> None:
        self.lines = lines
        self.index = 0

    def has_next(self) -> bool:
        return self.index < len(self.lines)

    def peek(self) -> Line | None:
        if not self.has_next():
            return None
        return self.lines[self.index]

    def next_line(self) -> Line:
        line = self.lines[self.index]
        self.index += 1
        return line

    def parse_block(self, indent: int) -> Any:
        line = self.peek()
        if line is None or line.indent < indent:
            return None
        if line.indent > indent:
            raise ParseError(f"Unexpected indentation at line {self.index + 1}")
        if line.content.startswith("- "):
            return self.parse_list(indent)
        return self.parse_mapping(indent)

    def parse_mapping(self, indent: int) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        while self.has_next():
            line = self.peek()
            assert line is not None
            if line.indent < indent:
                break
            if line.indent > indent:
                raise ParseError(f"Unexpected indentation at line {self.index + 1}")
            if line.content.startswith("- "):
                break

            current = self.next_line()
            if ":" not in current.content:
                raise ParseError(f"Expected key:value mapping at line {self.index}")
            key, rest = current.content.split(":", 1)
            key = key.strip()
            rest = rest.strip()
            if not key:
                raise ParseError(f"Empty key at line {self.index}")

            if rest == "":
                nested = self.parse_block(indent + 2)
                mapping[key] = {} if nested is None else nested
            elif rest == "|":
                mapping[key] = self.parse_literal(indent + 2)
            else:
                mapping[key] = _parse_scalar(rest)
        return mapping

    def parse_list(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while self.has_next():
            line = self.peek()
            assert line is not None
            if line.indent < indent:
                break
            if line.indent > indent:
                raise ParseError(f"Unexpected indentation at line {self.index + 1}")
            if not line.content.startswith("- "):
                break

            current = self.next_line()
            body = current.content[2:].strip()
            if body == "":
                nested = self.parse_block(indent + 2)
                items.append({} if nested is None else nested)
            elif ":" in body and not body.startswith(("'", '"')):
                key, rest = body.split(":", 1)
                key = key.strip()
                rest = rest.strip()
                value: Any
                if rest == "":
                    nested = self.parse_block(indent + 2)
                    value = {} if nested is None else nested
                elif rest == "|":
                    value = self.parse_literal(indent + 2)
                else:
                    value = _parse_scalar(rest)
                items.append({key: value})
            else:
                items.append(_parse_scalar(body))
        return items

    def parse_literal(self, indent: int) -> str:
        fragments: list[str] = []
        while self.has_next():
            line = self.peek()
            assert line is not None
            if line.indent < indent:
                break
            current = self.next_line()
            # Preserve relative indentation in block scalar.
            slice_start = min(indent, current.indent)
            reconstructed = (" " * (current.indent - slice_start)) + current.content
            fragments.append(reconstructed)
        return "\n".join(fragments).rstrip()


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value == "{}":
        return {}
    if value == "[]":
        return []
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1]
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1]

    try:
        if value.startswith("0") and value != "0":
            raise ValueError
        return int(value)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        pass

    return value
