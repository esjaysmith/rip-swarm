from __future__ import annotations

from typing import Any


def load_yaml(text: str) -> Any:
    lines = _lines(text)
    if not lines:
        return None
    value, idx = _parse(lines, 0, lines[0][0])
    if idx != len(lines):
        raise ValueError("unexpected YAML content")
    return value


def _lines(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped[:1] == "\t" or "\t" in line[:indent]:
            raise ValueError("YAML indent must use spaces, not tabs")
        if not stripped.strip():
            continue
        if stripped.startswith("#"):
            continue
        content = _strip_comment(stripped).rstrip()
        if not content:
            continue
        if indent % 2:
            raise ValueError("YAML indent must be a multiple of 2")
        out.append((indent, content))
    return out


def _strip_comment(content: str) -> str:
    """Drop a trailing ` #...` comment. A hash inside quotes, or one not
    preceded by whitespace (`a#b`), is literal text."""
    quote: str | None = None
    for i, ch in enumerate(content):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (i == 0 or content[i - 1] in " \t"):
            return content[:i]
    return content


def _parse(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    content = lines[i][1]
    if content.startswith("- ") or content == "-":
        return _parse_list(lines, i, indent)
    if content.startswith("[") and content.endswith("]"):
        return _parse_flow(content), i + 1
    return _parse_map(lines, i, indent)


def _parse_map(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    n = len(lines)
    while i < n:
        ind, content = lines[i]
        if ind < indent or content.startswith("- ") or content == "-":
            break
        if ind > indent:
            raise ValueError("bad YAML indent")
        key, raw = _split_kv(content)
        i += 1
        if raw is None:
            if i < n and lines[i][0] > indent:
                val, i = _parse(lines, i, lines[i][0])
            else:
                val = None
        else:
            val = _parse_scalar(raw)
        result[key] = val
    return result, i


def _parse_list(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[list, int]:
    items: list[Any] = []
    n = len(lines)
    while i < n:
        ind, content = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError("bad YAML indent")
        if not (content.startswith("- ") or content == "-"):
            break
        rest = "" if content == "-" else content[2:].strip()
        i += 1
        if rest == "":
            if i < n and lines[i][0] > indent:
                val, i = _parse(lines, i, lines[i][0])
            else:
                val = None
        elif ":" in rest and not _is_quoted(rest):
            map_indent = indent + 2
            chunk = [(map_indent, rest)]
            while i < n and lines[i][0] > indent:
                chunk.append(lines[i])
                i += 1
            val, j = _parse_map(chunk, 0, map_indent)
            if j != len(chunk):
                raise ValueError("unexpected YAML content")
        else:
            val = _parse_scalar(rest)
        items.append(val)
    return items, i


def _split_kv(content: str) -> tuple[str, str | None]:
    if ":" not in content:
        raise ValueError(f"expected key: {content!r}")
    key, _, rest = content.partition(":")
    key = key.strip()
    rest = rest.strip()
    if not key:
        raise ValueError("empty YAML key")
    if rest == "":
        return key, None
    return key, rest


def _parse_flow(text: str) -> list:
    inner = text[1:-1].strip()
    if not inner:
        return []
    return [_parse_scalar(part.strip()) for part in inner.split(",")]


def _is_quoted(text: str) -> bool:
    return len(text) >= 2 and text[0] in ("'", '"') and text[-1] == text[0]


def _parse_scalar(text: str) -> Any:
    if _is_quoted(text):
        # Quoted scalars are verbatim strings: never coerced to bool/int/null.
        return text[1:-1]
    if text == "true":
        return True
    if text == "false":
        return False
    if text == "null":
        return None
    if text.startswith("[") and text.endswith("]"):
        return _parse_flow(text)
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return text
