# repo-to-prompt

Turn repositories into LLM-friendly context packs for prompting and RAG.

_Because LLMs are smart… but they still can't read your repo on their own._

[![CI](https://github.com/wheevu/repo-to-prompt/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/wheevu/repo-to-prompt/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

`repo-to-prompt` is a CLI tool that converts code repositories into high-signal text bundles for LLM prompting and Retrieval-Augmented Generation (RAG) workflows. It scans your repo, ranks the most important files, chunks content into model-friendly sizes, and exports structured outputs ready for AI workflows.

### Key Features

- **Smart File Ranking**: Prioritizes READMEs, configs, and entrypoints over tests and generated files
- **Language-Aware Chunking**: Structure-aware chunking for Python, JS/TS, Go, Rust, and Markdown
- **Advanced Secret Redaction**: 25+ patterns, entropy detection, paranoid mode, and allowlists
- **Structure-Safe Redaction**: Never breaks code syntax (AST-validated for Python)
- **UTF-8 Fidelity**: Preserves emojis, smart quotes, and international characters
- **Gitignore Respect**: Honors `.gitignore` using the `ignore` crate
- **GitHub & HuggingFace Support**: Clone and process remote repositories directly
- **Deterministic Output**: Stable ordering and chunk IDs for reproducible results
- **Cross-Platform**: Works on macOS, Linux, and Windows

### Design Philosophy

- **High signal > high volume**: READMEs and entrypoints first, `node_modules` never
- **Deterministic**: Running twice produces identical results
- **Language-aware**: Code is structured text; treat it accordingly

## Installation

### Download a pre-built binary (recommended)

Download the latest release for your platform from the [Releases page](https://github.com/wheevu/repo-to-prompt/releases):

| Platform | Archive |
| -------- | ------- |
| Linux x86_64 | `repo-to-prompt-linux-x86_64.tar.gz` |
| Linux aarch64 | `repo-to-prompt-linux-aarch64.tar.gz` |
| macOS x86_64 | `repo-to-prompt-macos-x86_64.tar.gz` |
| macOS Apple Silicon | `repo-to-prompt-macos-aarch64.tar.gz` |
| Windows x86_64 | `repo-to-prompt-windows-x86_64.zip` |

Extract and place the binary somewhere on your `PATH`.

### Build from source

```bash
git clone https://github.com/wheevu/repo-to-prompt.git
cd repo-to-prompt
cargo build --release
# Binary at: target/release/repo-to-prompt

# Or install to ~/.cargo/bin
cargo install --path .
```

Requires Rust 1.75+. Install via [rustup.rs](https://rustup.rs).

## Quick Start

```bash
# Export a local repository
repo-to-prompt export --path /path/to/your/repo

# Export from GitHub
repo-to-prompt export --repo https://github.com/owner/repo

# View repository info without exporting
repo-to-prompt info /path/to/your/repo
```

## Usage

### Command: `export`

Convert a repository into context packs.

```bash
repo-to-prompt export [OPTIONS]
```

#### Input Options

| Option        | Short | Description                               |
| ------------- | ----- | ----------------------------------------- |
| `--path PATH` | `-p`  | Local path to the repository              |
| `--repo URL`  | `-r`  | GitHub or HuggingFace repository URL      |
| `--ref REF`   |       | Git ref (branch/tag/SHA) for remote repos |

#### Filter Options

| Option                | Short | Default  | Description                              |
| --------------------- | ----- | -------- | ---------------------------------------- |
| `--include-ext EXT`   | `-i`  | (many)   | Comma-separated extensions to include    |
| `--exclude-glob GLOB` | `-e`  | (many)   | Comma-separated glob patterns to exclude |
| `--max-file-bytes N`  |       | 1048576  | Max size per file (1 MB)                 |
| `--max-total-bytes N` |       | 20000000 | Max total export size (20 MB)            |
| `--no-gitignore`      |       | false    | Don't respect .gitignore files           |

#### Chunking Options

| Option                 | Default | Description                                                     |
| ---------------------- | ------- | --------------------------------------------------------------- |
| `--chunk-tokens N`     | 800     | Target tokens per chunk                                         |
| `--chunk-overlap N`    | 120     | Token overlap between chunks                                    |
| `--min-chunk-tokens N` | 200     | Minimum chunk size; smaller chunks are coalesced (`0` disables) |

#### Output Options

| Option             | Short | Default | Description                                                |
| ------------------ | ----- | ------- | ---------------------------------------------------------- |
| `--mode MODE`      | `-m`  | both    | Output mode: `prompt`, `rag`, or `both`                    |
| `--output-dir DIR` | `-o`  | ./out   | Base output directory (outputs go into `DIR/<repo-name>/`) |
| `--tree-depth N`   |       | 4       | Max depth for directory tree                               |
| `--no-redact`      |       | false   | Disable secret redaction                                   |

### Command: `info`

Display repository statistics without exporting.

```bash
repo-to-prompt info PATH [OPTIONS]
```

Supports the same `--include-ext`, `--exclude-glob`, `--max-file-bytes`, and `--no-gitignore` flags as `export` for consistent statistics.

## Examples

### Basic local export

```bash
repo-to-prompt export -p .

# Output:
# ./out/<repo-name>/context_pack.md  — Markdown context pack
# ./out/<repo-name>/chunks.jsonl     — JSONL chunks for RAG
# ./out/<repo-name>/report.json      — Processing statistics
```

### GitHub repository

```bash
repo-to-prompt export --repo https://github.com/pallets/flask
repo-to-prompt export --repo https://github.com/pallets/flask --ref 3.0.0
```

### HuggingFace repository

```bash
repo-to-prompt export --repo https://huggingface.co/mistralai/Mistral-7B-v0.1
repo-to-prompt export --repo https://huggingface.co/datasets/wikipedia
```

### Filtered export

```bash
# Only Python and Markdown, skip tests
repo-to-prompt export \
  -p ./my-project \
  --include-ext ".py,.md,.toml" \
  --exclude-glob "tests/**,test_*"
```

### RAG-only output

```bash
repo-to-prompt export -p ./repo --mode rag -o ./embeddings
```

## Output Structure

Outputs are written to `--output-dir/<repo-name>/`.

### context_pack.md

A structured Markdown document containing:

1. **Repository Overview** — summary, detected languages, entrypoints
2. **Directory Structure** — visual tree with important files highlighted
3. **Key Files** — categorized list of documentation, configs, and entrypoints
4. **File Contents** — chunked content with file paths and line numbers

### chunks.jsonl

JSONL file with one chunk per line:

```json
{"id": "a1b2c3d4e5f67890", "path": "src/main.rs", "lang": "rust", "start_line": 1, "end_line": 45, "content": "...", "priority": 0.85, "tags": ["entrypoint", "core"]}
```

### report.json

Processing statistics and file manifest including files scanned, bytes included, tokens estimated, skip reasons, and per-file metadata.

## Secret Redaction

By default, `repo-to-prompt` detects and redacts common secrets using 25+ built-in patterns:

- AWS access keys and secret keys
- GitHub / GitLab tokens
- Slack tokens and webhooks
- Stripe, OpenAI, Google API keys
- JWT tokens and private key blocks
- Authorization headers (Bearer, Basic)
- Generic patterns (`api_key`, `secret_key`, `password`, etc.)
- Connection string passwords

Redacted content is replaced with descriptive placeholders like `[AWS_ACCESS_KEY_REDACTED]`.

### Entropy detection

High-entropy strings (random-looking, likely secrets) are automatically detected:

```bash
# Detected: high entropy (4.8 bits/char)
SECRET = "xK9fP2mN7qR4sT6vW8yB3dF5gH1jL0aZ"

# Not detected: low entropy, known patterns (UUIDs)
UUID = "550e8400-e29b-41d4-a716-446655440000"
```

### Structure-safe redaction

For Python source files, redaction is AST-validated — if replacing a secret would break Python syntax, the original code is preserved instead.

### Advanced redaction config

```toml
# repo-to-prompt.toml
[redaction]
allowlist_patterns = ["*.example", "docs/**"]
allowlist_strings  = ["test-uuid-12345"]

[redaction.entropy]
enabled   = true
threshold = 4.5
min_length = 20

[redaction.paranoid]
enabled    = true
min_length = 32

[[redaction.custom_rules]]
name        = "internal_key"
pattern     = "INTERNAL_[A-Z0-9]{16}"
replacement = "[INTERNAL_KEY_REDACTED]"
```

## File Priority Ranking

| Priority | Category    | Examples                                 |
| -------- | ----------- | ---------------------------------------- |
| 1.00     | README      | `README.md`, `README.rst`                |
| 0.95     | Main docs   | `CONTRIBUTING.md`, `CHANGELOG.md`        |
| 0.90     | Config      | `pyproject.toml`, `package.json`, `Dockerfile` |
| 0.85     | Entrypoints | `main.py`, `index.js`, `cli.py`          |
| 0.75     | Core source | `src/**`, `lib/**`                       |
| 0.50     | Tests       | `tests/*`, `*_test.py`                   |
| 0.20     | Generated   | `*.min.js`, auto-generated files         |
| 0.15     | Lock files  | `package-lock.json`, `Cargo.lock`        |
| 0.10     | Vendored    | `vendor/**`, `node_modules/**`           |

Custom weights can be configured via `[repo-to-prompt.ranking_weights]` in your config file.

## Configuration Files

`repo-to-prompt` searches for config files in this order:

1. `repo-to-prompt.toml`
2. `.repo-to-prompt.toml`
3. `r2p.toml` / `.r2p.toml`
4. `r2p.yml` / `.r2p.yml` / `r2p.yaml` / `.r2p.yaml`

CLI flags always override config file values. Passing `--config` makes parse errors fatal.

### Example (TOML)

```toml
# repo-to-prompt.toml
[repo-to-prompt]
include_extensions = [".rs", ".toml", ".md"]
exclude_globs      = ["tests/**", "*.test.ts"]
max_file_bytes     = 1048576
max_total_bytes    = 20000000
chunk_tokens       = 800
chunk_overlap      = 120
min_chunk_tokens   = 200
output_dir         = "./out"
mode               = "both"
tree_depth         = 4
respect_gitignore  = true
redact_secrets     = true

[repo-to-prompt.ranking_weights]
readme = 1.0
test   = 0.3
```

### Example (YAML)

```yaml
# .r2p.yml
include_extensions: [.rs, .toml, .md]
chunk_tokens: 800
mode: both

ranking_weights:
  readme: 1.0
  test: 0.3
```

## Architecture

```
src/
├── main.rs            — Binary entry point
├── lib.rs             — Library entry point
├── cli/               — CLI commands (clap): export, info
├── config/            — Config loading (TOML/YAML) and CLI merging
├── domain/            — Core types: FileInfo, Chunk, Config, ScanStats
├── fetch/             — Repository fetching: local, GitHub, HuggingFace
├── scan/              — File discovery with gitignore respect + tree rendering
├── rank/              — File importance ranking with manifest parsing
├── chunk/             — Language-aware chunking: tree-sitter, markdown, line
├── redact/            — Secret redaction: rules, entropy, AST validation
├── render/            — Output generation: context_pack.md, chunks.jsonl, report.json
└── utils/             — Encoding, classify, tokens, hashing, paths
```

## Development

```bash
# Run tests (189 tests)
cargo test

# Run with verbose logging
RUST_LOG=debug cargo run -- export --path .

# Format
cargo fmt

# Lint
cargo clippy --all-targets --all-features

# Build optimized release
cargo build --release
```

Golden snapshot tests use [insta](https://insta.rs). To update snapshots after intentional output changes:

```bash
INSTA_UPDATE=always cargo test -- golden
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes with tests
4. Run `cargo fmt && cargo clippy && cargo test`
5. Commit and open a Pull Request

See `AGENTS.md` for detailed architecture notes and contribution guidelines.

## License

MIT License.
