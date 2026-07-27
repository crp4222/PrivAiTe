from __future__ import annotations

import json

from privaite.pii.mapping import PIIMapping


def json_escape(value: str) -> str:
    """The JSON string-literal encoding of value, without the surrounding
    quotes: what a restored original must look like when spliced into streamed
    JSON fragments (tool-call arguments arrive as JSON source text, so a raw
    quote, backslash or newline would break the client's parse)."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def json_escaped_mapping(mapping: PIIMapping) -> PIIMapping:
    """A derived mapping whose originals are JSON-string-escaped, for holdback
    buffers restoring inside JSON source text. The fakes are left as-is:
    placeholders contain no JSON-escaping characters, so they appear literally
    in the fragments (a fake that does escape would never match either way,
    with or without this derivation)."""
    escaped = PIIMapping()
    for fake, original in mapping.get_all_fakes().items():
        escaped.add(json_escape(original), fake, mapping.get_entity_type(original) or "")
    return escaped


class _TrieNode:
    __slots__ = ("children", "value")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.value: str | None = None


class StreamingDeAnonymizer:
    def __init__(self, mapping: PIIMapping) -> None:
        self.mapping = mapping
        self.buffer: str = ""
        self._root = _TrieNode()
        self._max_fake_length = 0

        for fake, original in mapping.get_all_fakes().items():
            self._insert_trie(fake, original)
            if len(fake) > self._max_fake_length:
                self._max_fake_length = len(fake)

    def _insert_trie(self, key: str, value: str) -> None:
        node = self._root
        for char in key:
            if char not in node.children:
                node.children[char] = _TrieNode()
            node = node.children[char]
        node.value = value

    def _trie_lookup(self, text: str) -> tuple[str | None, int]:
        node = self._root
        last_match_value: str | None = None
        last_match_len = 0

        for i, char in enumerate(text):
            if char not in node.children:
                break
            node = node.children[char]
            if node.value is not None:
                last_match_value = node.value
                last_match_len = i + 1

        return last_match_value, last_match_len

    def _has_prefix(self, text: str) -> bool:
        node = self._root
        for char in text:
            if char not in node.children:
                return False
            node = node.children[char]
        return True

    def feed(self, token: str) -> str:
        self.buffer += token
        output: list[str] = []

        while self.buffer:
            match_value, match_len = self._trie_lookup(self.buffer)

            if match_value is not None:
                output.append(match_value)
                self.buffer = self.buffer[match_len:]
                continue

            if len(self.buffer) <= self._max_fake_length and self._has_prefix(self.buffer):
                break

            output.append(self.buffer[0])
            self.buffer = self.buffer[1:]

        return "".join(output)

    def flush(self) -> str:
        remaining = self.buffer
        self.buffer = ""
        return remaining
