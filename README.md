# repo-context

<p align="center">
  <img src="assets/title.svg" width="72%" alt="repo-context">
</p>

A Rust CLI that turns repositories into prompt packs, retrieval chunks, and inspection reports.

<p align="center">
  <img src="assets/cli.gif" width="88%" alt="repo-context exporting a repository">
</p>

It reads local directories, GitHub repositories, and Hugging Face repositories.
Focused scans start from a file or module, then follow static imports, callers, tests, and repository anchors.
Secret redaction is on by default.

## Outputs

- A Markdown context pack
- JSONL retrieval chunks
- A report with selection, limits, hashes, and run metadata
- A versioned impact pack for working-tree or ref-to-ref review

<p align="center">
  <img src="assets/demo.png" width="82%" alt="Example repo-context output">
</p>

[Install, commands, configuration, modes, and Godot support](GUIDE.md).
