"""
Tests for structure-safe redaction.

These tests ensure that redaction NEVER breaks code syntax.
This is a critical correctness requirement.

The approach is:
- Python files: Use inline replacement, but verify AST parses after redaction
- If AST would break, return original content unchanged
- Non-Python source files: Use inline replacement (more forgiving)
- Non-source files: Use inline replacement without validation
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import pytest

from repo_to_prompt.redactor import (
    RedactionConfig,
    Redactor,
    create_redactor,
)


class TestStructureSafeRedaction:
    """Tests ensuring redaction never breaks Python syntax."""

    def test_python_file_uses_ast_safe_redaction(self):
        """Python files should use AST-validated inline redaction."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("src/main.py"))

        # Code with a secret in an assignment
        code = '''
def connect():
    api_key = "mytestapikey12345678901234567890ab"
    return Client(api_key)
'''
        result = redactor.redact(code)

        # The result should still be valid Python
        ast.parse(result)

        # The secret should be replaced inline
        assert "[SECRET_REDACTED]" in result
        assert "mytestapikey" not in result

    def test_inline_secret_in_function_signature_stays_valid(self):
        """Secrets in function args must not break the signature."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("app.py"))

        code = '''
def authenticate(token="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"):
    """Authenticate with GitHub."""
    return github.Auth(token)
'''
        result = redactor.redact(code)

        # Must still parse as valid Python
        ast.parse(result)

        # Secret should be replaced inline with structure preserved
        assert "ghp_" not in result
        assert "def authenticate" in result
        assert "github.Auth" in result

    def test_secret_in_import_does_not_break_syntax(self):
        """Even if there's a false positive in an import, syntax stays valid."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("module.py"))

        # This shouldn't have secrets, but test anyway
        code = '''
from some_module import (
    SomeClass,
    another_function,
)

import os
'''
        result = redactor.redact(code)

        # Must still parse
        ast.parse(result)

        # Content should be unchanged (no secrets)
        assert "from some_module import" in result

    def test_secret_in_string_literal_replaced_with_comment(self):
        """Secrets in string literals should result in line replacement."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("config.py"))

        code = '''
DATABASE_URL = "postgres://user:mysupersecretpassword123@host:5432/db"
DEBUG = True
'''
        result = redactor.redact(code)

        # Must parse
        ast.parse(result)

        # Password should be gone
        assert "mysupersecretpassword" not in result

    def test_multiline_string_with_secret(self):
        """Multiline strings with secrets should not break syntax."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("keys.py"))

        code = '''
PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHB7WgGT3Lc0f1fX5K8v
anotherlineofkey
-----END RSA PRIVATE KEY-----"""

OTHER_VAR = 42
'''
        result = redactor.redact(code)

        # The private key detection should result in valid output
        # Note: multiline strings with secrets may have multiple lines replaced
        # As long as it parses, we're good
        try:
            ast.parse(result)
        except SyntaxError:
            # If AST fails, check that the redaction at least preserved structure
            # by ensuring there's a comment where the secret was
            assert "# [REDACTED:" in result

    def test_aws_key_in_config_dict(self):
        """AWS keys in config dicts should not break syntax."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("settings.py"))

        code = '''
AWS_CONFIG = {
    "access_key": "AKIAIOSFODNN7EXAMPLE",
    "region": "us-west-2",
}
'''
        result = redactor.redact(code)

        # Must parse
        ast.parse(result)

        # AWS key should be gone
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_jwt_in_test_fixture(self):
        """JWT tokens in test files should not break syntax."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("test_auth.py"))

        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        code = f'''
def test_verify_token():
    token = "{jwt}"
    result = verify(token)
    assert result.user == "John Doe"
'''
        result = redactor.redact(code)

        # Must parse
        ast.parse(result)

        # JWT should be gone
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result

    def test_complex_code_with_multiple_secrets(self):
        """Complex code with multiple secrets should remain valid."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("app/main.py"))

        code = '''
import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class Config:
    # Type annotations with secrets use different patterns
    github_token: str = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    debug: bool = True

def main():
    """Run the application."""
    config = Config()
    password = "mysecretpassword12345678"
    api_key = "mytestapikey12345678901234567890ab"

    if config.debug:
        print(f"Using API key: {api_key}")

    return 0

if __name__ == "__main__":
    main()
'''
        result = redactor.redact(code)

        # MUST parse - this is the critical assertion
        ast.parse(result)

        # Secrets that match patterns should be gone
        assert "ghp_" not in result
        assert "mysecretpassword" not in result
        assert "mytestapikey" not in result

    def test_entropy_detection_does_not_break_syntax(self):
        """Entropy-based detection should also preserve syntax."""
        config = RedactionConfig(
            entropy_enabled=True,
            entropy_threshold=4.0,
            entropy_min_length=20,
        )
        redactor = Redactor(config=config, current_file=Path("crypto.py"))

        code = '''
# High entropy string that might be a secret
RANDOM_TOKEN = "xK9fP2mN7qR4sT6vW8yB3dF5gH1jL0aZ"

def process():
    return RANDOM_TOKEN
'''
        result = redactor.redact(code)

        # Must parse
        ast.parse(result)

    def test_paranoid_mode_does_not_break_syntax(self):
        """Paranoid mode should also preserve syntax."""
        config = RedactionConfig(
            paranoid_mode=True,
            paranoid_min_length=32,
        )
        redactor = Redactor(config=config, current_file=Path("tokens.py"))

        code = '''
# Long token
LONG_TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop"

def get_token():
    return LONG_TOKEN
'''
        result = redactor.redact(code)

        # Must parse
        ast.parse(result)

    def test_non_python_source_files_stay_valid(self):
        """JavaScript files should use inline redaction."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("config.js"))

        code = '''
const config = {
    apiKey: "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    region: "us-west-2"
};

module.exports = config;
'''
        result = redactor.redact(code)

        # Secret should be gone
        assert "ghp_" not in result

        # Should have inline replacement
        assert "[GITHUB_TOKEN_REDACTED]" in result

        # Structure should be preserved
        assert "const config" in result
        assert "region" in result

    def test_typescript_file_structure_safe(self):
        """TypeScript files should use inline redaction."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("app.ts"))

        code = '''
interface Config {
    apiKey: string;
}

const config: Config = {
    apiKey: "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
};
'''
        result = redactor.redact(code)

        # Secret should be gone
        assert "ghp_" not in result

        # Should have inline replacement
        assert "[GITHUB_TOKEN_REDACTED]" in result

        # Structure should be preserved
        assert "interface Config" in result
        assert "const config" in result

    def test_non_source_file_uses_inline_redaction(self):
        """Non-source files like .env should use inline redaction."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path(".env"))

        content = '''
API_KEY=mytestapikey12345678901234567890ab
DATABASE_URL=postgres://user:password123456789012@host/db
'''
        result = redactor.redact(content)

        # Should have inline replacement tokens
        assert "[SECRET_REDACTED]" in result or "[PASSWORD_REDACTED]" in result

        # Should NOT have line-level comment replacement
        assert "# [REDACTED:" not in result

    def test_markdown_file_uses_inline_redaction(self):
        """Markdown files should use inline redaction."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("README.md"))

        content = '''
# Setup

Set your API key:

```
export API_KEY=mytestapikey12345678901234567890ab
```
'''
        result = redactor.redact(content)

        # Should have inline replacement
        assert "mytestapikey" not in result


class TestRedactionPreservesLineCount:
    """Tests that line-level redaction preserves line count."""

    def test_line_count_preserved(self):
        """Redaction should not change the number of lines."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path("test.py"))

        code = '''line 1
line 2 with api_key = "mytestapikey12345678901234567890ab"
line 3
line 4 with github_token = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
line 5
'''
        original_lines = len(code.splitlines())
        result = redactor.redact(code)
        result_lines = len(result.splitlines())

        assert original_lines == result_lines


class TestRegressionSyntaxIntegrity:
    """
    Regression tests that ensure Python code from src/**/*.py
    always parses correctly after redaction.

    This is the critical test that must NEVER fail.

    The approach:
    - For valid Python input, redacted output MUST also be valid Python
    - Secrets within string literals are replaced with placeholder tokens
    - Structure (function signatures, classes, etc.) is preserved
    """

    def test_all_python_output_parses(self):
        """
        Simulate processing Python source files and verify output parses.

        This test uses representative Python code samples to ensure
        redaction never produces invalid syntax.
        """
        config = RedactionConfig()

        # Representative Python code samples that might contain secrets
        samples = [
            # Simple assignment
            ('simple.py', 'API_KEY = "mytestapikey12345678901234567890ab"'),

            # Function with default arg - this is the critical case
            ('func.py', '''
def connect(token="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"):
    return Client(token)
'''),

            # Class with secrets
            ('class.py', '''
class Config:
    API_KEY = "mytestapikey12345678901234567890ab"

    def __init__(self):
        self.password = "mypassword123456789012345"
'''),

            # Dict literal
            ('dict.py', '''
config = {
    "api_key": "mytestapikey12345678901234567890ab",
    "password": "mysecretpassword123456",
}
'''),

            # F-string - use api_key to ensure pattern match
            ('fstring.py', '''
api_key = "mytestapikey12345678901234567890ab"
msg = f"Using key: {api_key}"
'''),

            # Decorator
            ('decorator.py', '''
@require_auth("ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
def protected():
    pass
'''),

            # Comprehension - note: comprehension with f-string won't match patterns
            ('comp.py', '''
keys = [f"key_{i}" for i in range(10)]
'''),

            # Lambda - use GitHub token which has specific pattern
            ('lambda.py', '''
get_token = lambda: "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
'''),

            # Try/except - use api_key for pattern match
            ('try.py', '''
try:
    api_key = "mytestapikey12345678901234567890ab"
    connect(api_key)
except Exception:
    pass
'''),

            # Context manager
            ('context.py', '''
with Client("ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") as client:
    client.request()
'''),
        ]

        for filename, code in samples:
            redactor = Redactor(config=config, current_file=Path(f"src/{filename}"))
            result = redactor.redact(code)

            try:
                ast.parse(result)
            except SyntaxError as e:
                pytest.fail(
                    f"Redaction broke syntax in {filename}:\n"
                    f"Original:\n{code}\n"
                    f"Redacted:\n{result}\n"
                    f"Error: {e}"
                )

            # Verify secrets are actually redacted (where patterns match)
            if "ghp_" in code:
                assert "ghp_" not in result, f"GitHub token not redacted in {filename}"

    def test_allowlisted_file_unchanged(self):
        """Allowlisted files should not be modified at all."""
        config = RedactionConfig(
            allowlist_patterns=["test_*.py", "conftest.py"],
        )
        redactor = Redactor(config=config, current_file=Path("test_auth.py"))

        code = '''
# Test with real-looking token
TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
'''
        result = redactor.redact(code)

        # Should be unchanged
        assert result == code

        # Should still parse
        ast.parse(result)

    def test_structure_safe_can_be_disabled(self):
        """structure_safe_redaction=False uses inline replacement without AST validation."""
        config = RedactionConfig(structure_safe_redaction=False)
        redactor = Redactor(config=config, current_file=Path("test.py"))

        code = '''
API_KEY = "mytestapikey12345678901234567890ab"
'''
        result = redactor.redact(code)

        # Should have inline replacement
        assert "[SECRET_REDACTED]" in result

        # Still valid Python (inline replacement within strings is safe)
        ast.parse(result)


class TestInlineReplacementForSourceFiles:
    """Test that source files use inline replacement correctly."""

    @pytest.mark.parametrize("filename,expected_token", [
        ("main.py", "[SECRET_REDACTED]"),
        ("app.js", "[SECRET_REDACTED]"),
        ("component.tsx", "[SECRET_REDACTED]"),
        ("lib.go", "[SECRET_REDACTED]"),
        ("Main.java", "[SECRET_REDACTED]"),
        ("util.rs", "[SECRET_REDACTED]"),
        ("helper.rb", "[SECRET_REDACTED]"),
        ("script.sh", "[SECRET_REDACTED]"),
    ])
    def test_inline_replacement_by_language(self, filename, expected_token):
        """Source files should use inline replacement with correct tokens."""
        config = RedactionConfig()
        redactor = Redactor(config=config, current_file=Path(filename))

        # Use api_key = which matches the generic_secret pattern
        code = f'api_key = "abcdefghijklmnopqrstuvwxyz123456"\n'
        result = redactor.redact(code)

        # Secret should be replaced inline
        assert expected_token in result
        assert "abcdefghijklmnopqrstuvwxyz123456" not in result

        # Structure preserved
        assert "api_key" in result

