//! Integration tests for CLI

use assert_cmd::Command;
use predicates::prelude::*;
use rusqlite::Connection;
use std::fs;
use tempfile::TempDir;

#[test]
fn test_cli_version() {
    let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd.arg("--version");
    cmd.assert().success().stdout(predicate::str::contains("repo-to-prompt"));
}

#[test]
fn test_cli_help() {
    let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd.arg("--help");
    cmd.assert()
        .success()
        .stdout(predicate::str::contains("Convert repositories"))
        .stdout(predicate::str::contains("export"))
        .stdout(predicate::str::contains("info"))
        .stdout(predicate::str::contains("index"))
        .stdout(predicate::str::contains("query"))
        .stdout(predicate::str::contains("codeintel"));
}

#[test]
fn test_export_requires_path_or_repo() {
    let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd.arg("export");
    cmd.assert()
        .failure()
        .stderr(predicate::str::contains("Either --path or --repo must be specified"));
}

#[test]
fn test_export_rejects_both_path_and_repo() {
    let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd.args(["export", "--path", ".", "--repo", "https://github.com/test/test"]);
    cmd.assert()
        .failure()
        .stderr(predicate::str::contains("Cannot specify both --path and --repo"));
}

#[test]
fn test_export_rejects_invalid_redaction_mode() {
    let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd.args(["export", "--path", ".", "--redaction-mode", "invalid"]);
    cmd.assert().failure().stderr(predicate::str::contains("Invalid redaction mode"));
}

#[test]
fn test_info_reports_tree_sitter_capabilities() {
    let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd.args(["info", "."]);
    cmd.assert().success().stdout(predicate::str::contains("Statistics:"));
}

#[test]
fn test_export_accepts_contribution_mode() {
    let out = TempDir::new().expect("temp out dir");
    let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd.args([
        "export",
        "--path",
        ".",
        "--mode",
        "contribution",
        "--max-tokens",
        "10",
        "--output-dir",
        out.path().to_str().expect("utf8 path"),
        "--no-timestamp",
    ]);
    cmd.assert().success();
}

#[test]
fn test_index_creates_sqlite_database_with_symbols() {
    let repo = TempDir::new().expect("temp repo dir");
    fs::create_dir_all(repo.path().join("src")).expect("mkdir src");
    fs::create_dir_all(repo.path().join("tests")).expect("mkdir tests");
    fs::write(repo.path().join("src/auth.py"), "def refresh_token(user):\n    return user\n")
        .expect("write source file");
    fs::write(
        repo.path().join("tests/test_auth.py"),
        "from src.auth import refresh_token\n\ndef test_refresh_token():\n    assert refresh_token('x')\n",
    )
    .expect("write test file");

    let db_path = repo.path().join("index.sqlite");
    let mut cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd.args([
        "index",
        "--path",
        repo.path().to_str().expect("utf8 repo path"),
        "--db",
        db_path.to_str().expect("utf8 db path"),
        "--chunk-tokens",
        "64",
        "--chunk-overlap",
        "8",
    ]);
    cmd.assert().success().stdout(predicate::str::contains("Index created at"));

    let mut cmd_again = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    cmd_again.args([
        "index",
        "--path",
        repo.path().to_str().expect("utf8 repo path"),
        "--db",
        db_path.to_str().expect("utf8 db path"),
    ]);
    cmd_again.assert().success().stdout(predicate::str::contains("files reused: 2"));

    let conn = Connection::open(&db_path).expect("open sqlite");
    let file_count: i64 =
        conn.query_row("SELECT COUNT(*) FROM files", [], |row| row.get(0)).expect("count files");
    assert!(file_count >= 2);

    let chunk_count: i64 =
        conn.query_row("SELECT COUNT(*) FROM chunks", [], |row| row.get(0)).expect("count chunks");
    assert!(chunk_count >= 2);

    let symbol_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM symbols WHERE symbol = 'refresh_token' AND kind = 'def'",
            [],
            |row| row.get(0),
        )
        .expect("count symbols");
    assert!(symbol_count >= 1);

    let mut query_cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    query_cmd.args([
        "query",
        "--db",
        db_path.to_str().expect("utf8 db path"),
        "--task",
        "refresh token",
        "--limit",
        "5",
    ]);
    query_cmd
        .assert()
        .success()
        .stdout(predicate::str::contains("Top matches for task"))
        .stdout(predicate::str::contains("src/auth.py"));

    let out_path = repo.path().join("codeintel.json");
    let mut codeintel_cmd = Command::new(assert_cmd::cargo::cargo_bin!("repo-to-prompt"));
    codeintel_cmd.args([
        "codeintel",
        "--db",
        db_path.to_str().expect("utf8 db path"),
        "--out",
        out_path.to_str().expect("utf8 out path"),
    ]);
    codeintel_cmd
        .assert()
        .success()
        .stdout(predicate::str::contains("Code-intel export written to"));

    let exported = fs::read_to_string(&out_path).expect("read codeintel output");
    let doc: serde_json::Value = serde_json::from_str(&exported).expect("parse codeintel json");
    assert_eq!(doc.get("format").and_then(|v| v.as_str()), Some("scip-lite"));
    assert_eq!(doc.get("schema_version").and_then(|v| v.as_str()), Some("0.4.0"));
    assert!(doc
        .get("symbols")
        .and_then(|v| v.as_array())
        .map(|arr| !arr.is_empty())
        .unwrap_or(false));
    assert!(doc
        .get("occurrences")
        .and_then(|v| v.as_array())
        .map(|arr| !arr.is_empty())
        .unwrap_or(false));
    assert!(doc
        .get("relationships")
        .and_then(|v| v.as_array())
        .map(|arr| !arr.is_empty())
        .unwrap_or(false));
    assert!(doc.get("symbol_links").and_then(|v| v.as_array()).is_some());
    assert!(doc.get("stats").and_then(|v| v.as_object()).is_some());
}
