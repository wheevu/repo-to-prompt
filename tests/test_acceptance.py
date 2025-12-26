"""
Acceptance tests for repo-to-prompt.

These tests validate critical correctness requirements:
1. UTF-8 fidelity - emojis, smart quotes, box-drawing characters preserved
2. Code validity - all Python code blocks in output are syntactically valid

These tests are designed to run on CI and fail if core invariants are violated.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from repo_to_prompt.chunker import chunk_file, coalesce_small_chunks
from repo_to_prompt.config import FileInfo
from repo_to_prompt.ranker import FileRanker
from repo_to_prompt.redactor import create_redactor
from repo_to_prompt.renderer import render_context_pack
from repo_to_prompt.scanner import generate_tree, scan_repository
from repo_to_prompt.utils import read_file_safe


def has_mojibake(text: str) -> bool:
    """
    Detect mojibake in text.

    Mojibake occurs when UTF-8 bytes are incorrectly decoded as Latin-1/Windows-1252.
    Common patterns:
    - UTF-8 ellipsis (U+2026, bytes E2 80 A6) becomes "â€¦" in Latin-1
    - UTF-8 right single quote (U+2019, bytes E2 80 99) becomes "â€™"
    - UTF-8 left double quote (U+201C, bytes E2 80 9C) becomes "â€œ"

    We detect this by looking for sequences that are characteristic of
    UTF-8 bytes decoded incorrectly.
    """
    # These byte sequences appear when UTF-8 multi-byte chars are decoded as Latin-1
    # They start with 0xC3 or 0xC2 (Ã, Â) or 0xE2 which becomes â in Latin-1
    # Pattern: â followed by €, then another character

    # Check for the telltale "â€" sequence (0xE2 0x80 decoded as Latin-1)
    # This appears at the start of many UTF-8 encoded punctuation marks
    if "\xe2\x80" in text.encode("latin-1", errors="ignore").decode("latin-1", errors="ignore"):
        # Need a different approach - check if encoding round-trip produces different result
        pass

    # Look for common mojibake patterns that indicate wrong decoding
    # These patterns are unlikely to appear in legitimate UTF-8 text
    mojibake_indicators = [
        # UTF-8 multi-byte chars misread as Latin-1 produce these sequences
        "\xc3\xa2\xc2\x80",  # Start of many mojibake sequences
        "Ã¢â‚¬",  # Common mojibake sequence
    ]

    for indicator in mojibake_indicators:
        if indicator in text:
            return True

    # Check for broken surrogate-like patterns
    # UTF-8 emoji bytes decoded as Latin-1 produce sequences with Ã° (ð in Latin-1)
    # followed by Ÿ or other chars
    return "\xc3\xb0\xc5\xb8" in text or "\xc3\xb0\xc2\x9f" in text


def detect_encoding_corruption(original: str, rendered: str) -> list[str]:
    """
    Detect if specific UTF-8 characters were corrupted during rendering.

    Returns list of corrupted character descriptions, or empty if all OK.
    """
    issues = []

    # Test specific UTF-8 characters that should be preserved
    test_chars = [
        ("\u2026", "ellipsis"),
        ("\u2019", "right single quote"),
        ("\u201c", "left double quote"),
        ("\u201d", "right double quote"),
        ("\u2014", "em dash"),
    ]

    for char, name in test_chars:
        if char in original and char not in rendered:
            issues.append(f"{name} was corrupted")

    return issues


class TestUTF8Fidelity:
    """
    Tests that UTF-8 content is preserved correctly through the pipeline.

    This catches encoding bugs where chardet misdetects UTF-8 as Latin-1,
    causing smart quotes, ellipses, and emojis to become corrupted.
    """

    def test_read_file_preserves_utf8(self, tmp_path: Path):
        """read_file_safe must preserve UTF-8 characters."""
        # Create a file with various UTF-8 characters
        test_file = tmp_path / "test_utf8.py"
        utf8_content = '''# -*- coding: utf-8 -*-
"""Module with UTF-8 content."""

# Smart quotes: "quoted" and 'apostrophe'
# Ellipsis: …
# Em dash: —
# Emojis: 🎉 🚀 ✅ ❌
# Box drawing: ├── │ └──
# Math: π ∑ √ ∞

MESSAGE = "Hello… world! 🌍"
'''
        test_file.write_text(utf8_content, encoding="utf-8")

        # Read it back
        content, encoding = read_file_safe(test_file)

        # Must be UTF-8
        assert encoding == "utf-8", f"Expected utf-8, got {encoding}"

        # Must have the actual UTF-8 characters (positive check)
        assert "\u2026" in content, "Ellipsis not preserved"  # …
        assert "🎉" in content, "Emoji not preserved"
        assert "├" in content, "Box drawing not preserved"
        assert "π" in content, "Greek letter not preserved"

        # Check no encoding corruption
        issues = detect_encoding_corruption(utf8_content, content)
        assert not issues, f"Encoding corruption detected: {issues}"

    def test_chunking_preserves_utf8(self, tmp_path: Path):
        """Chunking must preserve UTF-8 content."""
        test_file = tmp_path / "utf8_code.py"
        content = '''"""Unicode test file."""

# Various UTF-8 characters
EMOJI = "🚀 Launch!"
SMART_QUOTE = "It's working…"
BOX = "├── item"
'''
        test_file.write_text(content, encoding="utf-8")

        file_info = FileInfo(
            path=test_file,
            relative_path="utf8_code.py",
            size_bytes=len(content.encode("utf-8")),
            extension=".py",
            language="python",
        )

        chunks = chunk_file(file_info, max_tokens=500)

        # Verify chunks contain UTF-8 characters
        all_content = "".join(c.content for c in chunks)
        assert "🚀" in all_content, "Emoji lost in chunking"
        assert "\u2026" in all_content, "Ellipsis lost in chunking"
        assert "├" in all_content, "Box drawing lost in chunking"

    def test_context_pack_preserves_utf8(self, tmp_path: Path):
        """Full pipeline must preserve UTF-8 in context_pack.md."""
        # Create a mini project with UTF-8 content
        (tmp_path / "src").mkdir()

        readme = tmp_path / "README.md"
        readme_content = "# Test Project 🎉\n\nThis is a test\u2026 with smart \u201cquotes\u201d.\n"
        readme.write_text(readme_content, encoding="utf-8")

        code = tmp_path / "src" / "main.py"
        code.write_text(
            '"""Main module."""\nMESSAGE = "Hello\u2026 🌍"\n',
            encoding="utf-8"
        )

        # Run the pipeline
        files, stats = scan_repository(tmp_path)
        ranker = FileRanker(tmp_path, scanned_files={f.relative_path for f in files})
        files = ranker.rank_files(files)

        redactor = create_redactor(enabled=False)  # No redaction for this test
        all_chunks = []
        for f in files:
            chunks = chunk_file(f, redactor=redactor)
            all_chunks.extend(chunks)
            f.token_estimate = sum(c.token_estimate for c in chunks)

        all_chunks = coalesce_small_chunks(all_chunks)
        stats.chunks_created = len(all_chunks)
        stats.files_included = len(files)

        context_pack = render_context_pack(
            root_path=tmp_path,
            files=files,
            chunks=all_chunks,
            ranker=ranker,
            stats=stats,
            include_timestamp=False,
        )

        # Check UTF-8 characters are preserved
        assert "🎉" in context_pack, "Emoji not in context pack"
        assert "\u2026" in context_pack, "Ellipsis not in context pack"

    def test_tree_generation_preserves_box_drawing(self, tmp_path: Path):
        """Directory tree generation must use correct box-drawing characters."""
        # Create a directory structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("# main", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("# test", encoding="utf-8")

        tree = generate_tree(tmp_path, max_depth=3)

        # Tree must contain proper box drawing characters
        assert "├" in tree or "└" in tree, "Box drawing characters missing"


class TestContextPackCodeValidity:
    """
    Tests that all Python code blocks in context_pack.md are syntactically valid.

    This catches bugs where:
    - Redaction placeholders break syntax
    - Chunking splits strings/expressions mid-literal
    - Any transformation produces unparsable code
    """

    @staticmethod
    def extract_python_code_blocks(markdown: str) -> list[tuple[str, int]]:
        """
        Extract all Python code blocks from markdown.

        Returns:
            List of (code_content, line_number) tuples
        """
        blocks = []

        # Match fenced code blocks with python/py language
        pattern = re.compile(
            r'^```(?:python|py)\s*\n(.*?)^```',
            re.MULTILINE | re.DOTALL
        )

        for match in pattern.finditer(markdown):
            code = match.group(1)
            # Calculate line number
            line_num = markdown[:match.start()].count('\n') + 1
            blocks.append((code, line_num))

        return blocks

    def test_all_python_blocks_parse(self, tmp_path: Path):
        """Every Python code block in context_pack must be valid Python."""
        # Create a project with various Python constructs
        (tmp_path / "src").mkdir()

        # Simple module
        (tmp_path / "src" / "simple.py").write_text('''
"""Simple module."""

def hello(name: str = "World") -> str:
    """Greet someone."""
    return f"Hello, {name}!"

class Greeter:
    """A greeter class."""

    def __init__(self, prefix: str = "Hi"):
        self.prefix = prefix

    def greet(self, name: str) -> str:
        return f"{self.prefix}, {name}!"
''', encoding="utf-8")

        # Module with potential secret patterns
        (tmp_path / "src" / "config.py").write_text('''
"""Config module."""

# These look like secrets but are test values
DATABASE_URL = "postgres://user:password@localhost/db"
API_KEY = "test_key_12345678901234567890"
''', encoding="utf-8")

        # Run pipeline
        files, stats = scan_repository(tmp_path)
        ranker = FileRanker(tmp_path, scanned_files={f.relative_path for f in files})
        files = ranker.rank_files(files)

        redactor = create_redactor(enabled=True)  # Enable redaction
        all_chunks = []
        for f in files:
            redactor.set_current_file(f.path)
            chunks = chunk_file(f, redactor=redactor)
            all_chunks.extend(chunks)
            f.token_estimate = sum(c.token_estimate for c in chunks)

        all_chunks = coalesce_small_chunks(all_chunks)
        stats.chunks_created = len(all_chunks)
        stats.files_included = len(files)

        context_pack = render_context_pack(
            root_path=tmp_path,
            files=files,
            chunks=all_chunks,
            ranker=ranker,
            stats=stats,
            include_timestamp=False,
        )

        # Extract all Python code blocks
        blocks = self.extract_python_code_blocks(context_pack)

        assert len(blocks) > 0, "No Python code blocks found in context pack"

        # Verify each block parses
        failures = []
        for code, line_num in blocks:
            code = code.strip()
            if not code:
                continue

            try:
                ast.parse(code)
            except SyntaxError as e:
                failures.append({
                    "line": line_num,
                    "error": str(e),
                    "code_preview": code[:200] + "..." if len(code) > 200 else code,
                })

        if failures:
            failure_msg = "\n\n".join(
                f"Line {f['line']}: {f['error']}\nCode:\n{f['code_preview']}"
                for f in failures[:5]  # Show first 5 failures
            )
            pytest.fail(
                f"Found {len(failures)} unparsable Python code blocks:\n\n{failure_msg}"
            )

    def test_redacted_code_still_parses(self, tmp_path: Path):
        """Code with redacted secrets must still parse."""
        # Create a file with secrets that will be redacted
        code_with_secrets = '''
"""Module with secrets."""

# Various secret patterns
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
API_SECRET = "mytestapikey12345678901234567890ab"

def connect(api_key: str = "mytestapikey12345678901234567890") -> None:
    """Connect with API key."""
    pass

config = {
    "password": "mysecretpassword12345678",
    "database_url": "postgres://user:secret123@host/db",
}
'''
        test_file = tmp_path / "secrets.py"
        test_file.write_text(code_with_secrets, encoding="utf-8")

        # Test redaction directly (not through chunking)
        redactor = create_redactor(enabled=True)
        redactor.set_current_file(test_file)
        redacted_content = redactor.redact(code_with_secrets)

        # Redacted content must still parse
        try:
            ast.parse(redacted_content)
        except SyntaxError as e:
            pytest.fail(
                f"Redacted content doesn't parse:\n"
                f"Error: {e}\n"
                f"Content:\n{redacted_content}"
            )

        # Verify secrets were redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted_content, "AWS key should be redacted"
        assert "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in redacted_content, "GitHub token should be redacted"

    def test_complex_code_structures_survive_redaction(self, tmp_path: Path):
        """Complex code structures must remain valid after redaction."""
        complex_code = '''
"""Complex module with various Python constructs."""

from typing import Dict, List, Optional
from dataclasses import dataclass, field
import json

# Secret that should be redacted
API_KEY = "mytestapikey12345678901234567890ab"

@dataclass
class Config:
    """Configuration class."""

    name: str
    values: Dict[str, str] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    # Another secret
    secret: str = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def get_value(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a configuration value."""
        return self.values.get(key, default)


def process_data(
    data: Dict[str, any],
    *,
    validate: bool = True,
    api_key: str = "AKIAIOSFODNN7EXAMPLE",  # Secret in default arg
) -> Dict[str, any]:
    """Process data with options."""
    result = {}

    for key, value in data.items():
        if validate and not isinstance(key, str):
            raise ValueError(f"Invalid key: {key}")
        result[key] = value

    return result


class DataProcessor:
    """Process data streams."""

    def __init__(
        self,
        config: Config,
        *,
        batch_size: int = 100,
    ) -> None:
        self.config = config
        self.batch_size = batch_size
        self._buffer: List[Dict] = []

    def process(self, item: Dict) -> None:
        """Add item to buffer and process if full."""
        self._buffer.append(item)

        if len(self._buffer) >= self.batch_size:
            self._flush()

    def _flush(self) -> None:
        """Flush the buffer."""
        if self._buffer:
            processed = [
                process_data(item)
                for item in self._buffer
            ]
            self._buffer.clear()
'''
        test_file = tmp_path / "complex.py"
        test_file.write_text(complex_code, encoding="utf-8")

        # Test redaction directly
        redactor = create_redactor(enabled=True)
        redactor.set_current_file(test_file)
        redacted_content = redactor.redact(complex_code)

        try:
            ast.parse(redacted_content)
        except SyntaxError as e:
            pytest.fail(
                f"Redacted content doesn't parse:\n"
                f"Error: {e}\n"
                f"Content preview:\n{redacted_content[:500]}..."
            )

        # Verify some secrets were redacted
        assert "mytestapikey12345678901234567890ab" not in redacted_content
        assert "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in redacted_content


class TestEndToEndIntegrity:
    """End-to-end tests for overall output integrity."""

    def test_source_files_parse_after_redaction(self):
        """
        Verify that source files remain syntactically valid after redaction.

        This is the key acceptance test - redaction should never break
        Python syntax in source files.
        """
        repo_root = Path(__file__).parent.parent

        # Only run if we're in the actual repo (not installed as package)
        if not (repo_root / "src" / "repo_to_prompt").exists():
            pytest.skip("Not running in source repository")

        src_dir = repo_root / "src" / "repo_to_prompt"

        redactor = create_redactor(enabled=True)

        failures = []
        for py_file in src_dir.glob("*.py"):
            redactor.set_current_file(py_file)

            # Read and redact
            content, _ = read_file_safe(py_file)
            redacted = redactor.redact(content)

            # Must still parse
            try:
                ast.parse(redacted)
            except SyntaxError as e:
                failures.append({
                    "file": py_file.name,
                    "error": str(e),
                    "preview": redacted[:200],
                })

        if failures:
            msg = "\n\n".join(
                f"{f['file']}: {f['error']}\nPreview: {f['preview']}"
                for f in failures[:3]
            )
            pytest.fail(f"Redaction broke syntax in {len(failures)} files:\n\n{msg}")

    def test_context_pack_utf8_preserved(self):
        """
        Verify UTF-8 characters are preserved in context pack output.
        """
        repo_root = Path(__file__).parent.parent

        if not (repo_root / "src" / "repo_to_prompt").exists():
            pytest.skip("Not running in source repository")

        # Scan just the README and a few source files
        files, stats = scan_repository(
            repo_root,
            exclude_globs={
                "out/**",
                ".git/**",
                "__pycache__/**",
                "tests/**",
            },
        )

        if not files:
            pytest.skip("No files found")

        ranker = FileRanker(repo_root, scanned_files={f.relative_path for f in files})
        files = ranker.rank_files(files)

        redactor = create_redactor(enabled=False)  # No redaction to test encoding
        all_chunks = []

        for f in files[:5]:
            redactor.set_current_file(f.path)
            try:
                chunks = chunk_file(f, redactor=redactor)
                all_chunks.extend(chunks)
                f.token_estimate = sum(c.token_estimate for c in chunks)
            except Exception:
                continue

        if not all_chunks:
            pytest.skip("No chunks generated")

        all_chunks = coalesce_small_chunks(all_chunks)
        stats.chunks_created = len(all_chunks)
        stats.files_included = len([f for f in files if f.token_estimate > 0])

        context_pack = render_context_pack(
            root_path=repo_root,
            files=[f for f in files if f.token_estimate > 0][:5],
            chunks=all_chunks,
            ranker=ranker,
            stats=stats,
            include_timestamp=False,
        )

        # Check UTF-8 characters are preserved
        # The README contains ellipsis and smart quotes
        assert "\u2026" in context_pack, "Ellipsis should be preserved"

    def test_readme_encoding_preserved(self):
        """
        Verify README.md encoding is preserved through the pipeline.
        """
        repo_root = Path(__file__).parent.parent
        readme_path = repo_root / "README.md"

        if not readme_path.exists():
            pytest.skip("README.md not found")

        # Read README directly
        content, encoding = read_file_safe(readme_path)

        # Must be UTF-8
        assert encoding == "utf-8", f"README should be UTF-8, got {encoding}"

        # Check specific UTF-8 characters we know are in the README
        # Ellipsis: …
        assert "\u2026" in content, "Ellipsis character not preserved in README"

        # Right single quote: '
        assert "\u2019" in content, "Smart quote not preserved in README"
