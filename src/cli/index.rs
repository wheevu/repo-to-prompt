//! Index command implementation

use anyhow::{Context, Result};
use clap::Args;
use rusqlite::{params, Connection};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use super::utils::parse_csv;
use crate::chunk::{chunk_content, coalesce_small_chunks_with_max};
use crate::config::{load_config, merge_cli_with_config, CliOverrides};
use crate::domain::{Chunk, FileInfo, ScanStats};
use crate::fetch::fetch_repository;
use crate::rank::rank_files;
use crate::scan::scanner::FileScanner;
use crate::utils::read_file_safe;

#[derive(Args)]
pub struct IndexArgs {
    /// Local directory path to index
    #[arg(short, long, value_name = "PATH")]
    pub path: Option<PathBuf>,

    /// GitHub repository URL to clone and index
    #[arg(short = 'r', long, value_name = "URL")]
    pub repo: Option<String>,

    /// Git ref (branch/tag/SHA) when using --repo
    #[arg(long, value_name = "REF")]
    pub ref_: Option<String>,

    /// Path to config file (repo-to-prompt.toml or .r2p.yml)
    #[arg(short = 'c', long, value_name = "FILE")]
    pub config: Option<PathBuf>,

    /// SQLite path for the index database
    #[arg(long, value_name = "FILE", default_value = ".repo-to-prompt/index.sqlite")]
    pub db: PathBuf,

    /// Include only these extensions (comma-separated)
    #[arg(short = 'i', long, value_name = "EXTS")]
    pub include_ext: Option<String>,

    /// Exclude paths matching these globs (comma-separated)
    #[arg(short = 'e', long, value_name = "GLOBS")]
    pub exclude_glob: Option<String>,

    /// Skip files larger than this (bytes)
    #[arg(long, value_name = "BYTES")]
    pub max_file_bytes: Option<u64>,

    /// Stop after indexing this many bytes total
    #[arg(long, value_name = "BYTES")]
    pub max_total_bytes: Option<u64>,

    /// Ignore .gitignore rules
    #[arg(long)]
    pub no_gitignore: bool,

    /// Follow symbolic links when scanning
    #[arg(long)]
    pub follow_symlinks: bool,

    /// Include minified/bundled files
    #[arg(long)]
    pub include_minified: bool,

    /// Target tokens per chunk
    #[arg(long, value_name = "TOKENS")]
    pub chunk_tokens: Option<usize>,

    /// Overlap tokens between adjacent chunks
    #[arg(long, value_name = "TOKENS")]
    pub chunk_overlap: Option<usize>,

    /// Coalesce chunks smaller than this
    #[arg(long, value_name = "TOKENS")]
    pub min_chunk_tokens: Option<usize>,
}

pub fn run(args: IndexArgs) -> Result<()> {
    if args.path.is_some() && args.repo.is_some() {
        anyhow::bail!("Cannot specify both --path and --repo");
    }

    let cwd = std::env::current_dir()?;
    let config_anchor = match args.path.as_ref() {
        Some(path) if path.exists() => path.canonicalize().unwrap_or_else(|_| cwd.clone()),
        _ => cwd.clone(),
    };

    let file_config = load_config(&config_anchor, args.config.as_deref())?;
    let include_ext = parse_csv(&args.include_ext).map(|v| v.into_iter().collect());
    let exclude_glob = parse_csv(&args.exclude_glob).map(|v| v.into_iter().collect());

    let cli_overrides = CliOverrides {
        path: args.path.clone(),
        repo_url: args.repo.clone(),
        ref_: args.ref_.clone(),
        include_extensions: include_ext,
        exclude_globs: exclude_glob,
        max_file_bytes: args.max_file_bytes,
        max_total_bytes: args.max_total_bytes,
        respect_gitignore: if args.no_gitignore { Some(false) } else { None },
        follow_symlinks: if args.follow_symlinks { Some(true) } else { None },
        skip_minified: if args.include_minified { Some(false) } else { None },
        chunk_tokens: args.chunk_tokens,
        chunk_overlap: args.chunk_overlap,
        min_chunk_tokens: args.min_chunk_tokens,
        ..CliOverrides::default()
    };
    let merged = merge_cli_with_config(file_config, cli_overrides);

    if merged.path.is_none() && merged.repo_url.is_none() {
        anyhow::bail!("Either --path or --repo must be specified");
    }

    let repo_ctx = fetch_repository(
        merged.path.as_deref(),
        merged.repo_url.as_deref(),
        merged.ref_.as_deref(),
    )?;
    let root_path = repo_ctx.root_path.clone();

    let mut scanner = FileScanner::new(root_path.clone())
        .max_file_bytes(merged.max_file_bytes)
        .respect_gitignore(merged.respect_gitignore)
        .follow_symlinks(merged.follow_symlinks)
        .skip_minified(merged.skip_minified)
        .include_extensions(merged.include_extensions.iter().cloned().collect())
        .exclude_globs(merged.exclude_globs.iter().cloned().collect());

    let scanned_files = scanner.scan()?;
    let mut stats = scanner.stats().clone();
    let ranked_files = rank_files(&root_path, scanned_files)?;
    let selected_files = apply_byte_budget(ranked_files, Some(merged.max_total_bytes), &mut stats);

    let mut prepared = Vec::new();
    let mut unreadable = 0usize;
    for file in &selected_files {
        let (content, _encoding) = match read_file_safe(&file.path, None, None) {
            Ok(value) => value,
            Err(_) => {
                unreadable += 1;
                continue;
            }
        };

        let hash = sha256_hex(&content);
        prepared.push(PreparedFile { file: file.clone(), content, hash });
    }

    let summary = write_index(
        &args.db,
        &root_path,
        &selected_files,
        &prepared,
        &stats,
        IndexBuildOptions {
            chunk_tokens: merged.chunk_tokens,
            chunk_overlap: merged.chunk_overlap,
            min_chunk_tokens: merged.min_chunk_tokens,
        },
    )?;

    println!("Index created at {}", args.db.display());
    println!("  files indexed: {}", summary.files_indexed);
    println!("  chunks indexed: {}", summary.chunks_indexed);
    println!("  files reindexed: {}", summary.files_reindexed);
    println!("  files reused: {}", summary.files_reused);
    println!("  files removed: {}", summary.files_removed);
    if unreadable > 0 {
        println!("  files unreadable: {}", unreadable);
    }

    Ok(())
}

fn write_index(
    db_path: &Path,
    root_path: &Path,
    files: &[FileInfo],
    prepared: &[PreparedFile],
    stats: &ScanStats,
    build: IndexBuildOptions,
) -> Result<IndexSummary> {
    if let Some(parent) = db_path.parent() {
        fs::create_dir_all(parent)?;
    }

    let mut conn = Connection::open(db_path)
        .with_context(|| format!("Failed to open SQLite database at {}", db_path.display()))?;

    ensure_schema(&conn)?;

    let tx = conn.transaction()?;

    let existing_hashes = {
        let mut stmt = tx.prepare("SELECT path, file_hash FROM files")?;
        let rows =
            stmt.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)))?;
        let mut map = HashMap::new();
        for row in rows {
            let (path, hash) = row?;
            map.insert(path, hash);
        }
        map
    };

    let selected_paths: HashSet<String> = files.iter().map(|f| f.relative_path.clone()).collect();
    let existing_paths: HashSet<String> = existing_hashes.keys().cloned().collect();
    let stale_paths: Vec<String> = existing_paths.difference(&selected_paths).cloned().collect();
    for path in &stale_paths {
        tx.execute("DELETE FROM chunk_fts WHERE path = ?1", params![path])?;
        tx.execute("DELETE FROM files WHERE path = ?1", params![path])?;
    }

    let mut files_reindexed = 0usize;
    let mut files_reused = 0usize;
    let indexed_at = chrono::Utc::now().to_rfc3339();

    for prepared_file in prepared {
        let path = &prepared_file.file.relative_path;
        let was_same = existing_hashes.get(path).is_some_and(|h| h == &prepared_file.hash);

        if was_same {
            files_reused += 1;
            tx.execute(
                "
                UPDATE files
                SET language = ?2, extension = ?3, size_bytes = ?4, priority = ?5, indexed_at = ?6
                WHERE path = ?1
                ",
                params![
                    path,
                    &prepared_file.file.language,
                    &prepared_file.file.extension,
                    prepared_file.file.size_bytes as i64,
                    prepared_file.file.priority,
                    &indexed_at,
                ],
            )?;
            continue;
        }

        files_reindexed += 1;
        tx.execute("DELETE FROM chunk_fts WHERE path = ?1", params![path])?;
        tx.execute("DELETE FROM files WHERE path = ?1", params![path])?;

        let raw_chunks = chunk_content(
            &prepared_file.file,
            &prepared_file.content,
            build.chunk_tokens,
            build.chunk_overlap,
        )?;
        let file_chunks =
            coalesce_small_chunks_with_max(raw_chunks, build.min_chunk_tokens, build.chunk_tokens);
        let file_tokens = file_chunks.iter().map(|c| c.token_estimate).sum::<usize>();

        tx.execute(
            "
            INSERT INTO files
                (path, language, extension, size_bytes, priority, token_estimate, file_hash, indexed_at)
            VALUES
                (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
            ",
            params![
                path,
                &prepared_file.file.language,
                &prepared_file.file.extension,
                prepared_file.file.size_bytes as i64,
                prepared_file.file.priority,
                file_tokens as i64,
                &prepared_file.hash,
                &indexed_at,
            ],
        )?;

        for chunk in &file_chunks {
            insert_chunk(&tx, chunk)?;
        }
    }

    let files_indexed: usize =
        tx.query_row("SELECT COUNT(*) FROM files", [], |row| row.get::<_, i64>(0))? as usize;
    let chunks_indexed: usize =
        tx.query_row("SELECT COUNT(*) FROM chunks", [], |row| row.get::<_, i64>(0))? as usize;

    tx.execute("DELETE FROM metadata", [])?;

    let metadata = [
        ("repo_root".to_string(), root_path.to_string_lossy().to_string()),
        ("files_scanned".to_string(), stats.files_scanned.to_string()),
        ("files_indexed".to_string(), files_indexed.to_string()),
        ("chunks_indexed".to_string(), chunks_indexed.to_string()),
        ("languages".to_string(), json!(stats.languages_detected).to_string()),
    ];
    for (key, value) in metadata {
        tx.execute("INSERT INTO metadata (key, value) VALUES (?1, ?2)", params![key, value])?;
    }

    tx.commit()?;

    Ok(IndexSummary {
        files_indexed,
        chunks_indexed,
        files_reindexed,
        files_reused,
        files_removed: stale_paths.len(),
    })
}

fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            language TEXT NOT NULL,
            extension TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            priority REAL NOT NULL,
            token_estimate INTEGER NOT NULL,
            file_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            language TEXT NOT NULL,
            priority REAL NOT NULL,
            token_estimate INTEGER NOT NULL,
            tags_json TEXT NOT NULL,
            content TEXT NOT NULL,
            FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS symbols (
            symbol TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_path TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            PRIMARY KEY(symbol, kind, chunk_id),
            FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE,
            FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
            chunk_id UNINDEXED,
            path UNINDEXED,
            content
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_file_path ON chunks(file_path);
        CREATE INDEX IF NOT EXISTS idx_symbols_symbol ON symbols(symbol);
        CREATE INDEX IF NOT EXISTS idx_symbols_file_path ON symbols(file_path);
        ",
    )?;
    Ok(())
}

fn insert_chunk(tx: &rusqlite::Transaction<'_>, chunk: &Chunk) -> Result<()> {
    let tags = serde_json::to_string(&chunk.tags)?;

    tx.execute(
        "
        INSERT INTO chunks
            (id, file_path, start_line, end_line, language, priority, token_estimate, tags_json, content)
        VALUES
            (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
        ",
        params![
            &chunk.id,
            &chunk.path,
            chunk.start_line as i64,
            chunk.end_line as i64,
            &chunk.language,
            chunk.priority,
            chunk.token_estimate as i64,
            tags,
            &chunk.content,
        ],
    )?;

    tx.execute(
        "INSERT INTO chunk_fts (chunk_id, path, content) VALUES (?1, ?2, ?3)",
        params![&chunk.id, &chunk.path, &chunk.content],
    )?;

    for tag in &chunk.tags {
        if let Some((kind, symbol)) = tag.split_once(':') {
            if matches!(kind, "def" | "type" | "impl") && !symbol.trim().is_empty() {
                tx.execute(
                    "
                    INSERT OR IGNORE INTO symbols (symbol, kind, file_path, chunk_id)
                    VALUES (?1, ?2, ?3, ?4)
                    ",
                    params![symbol.to_ascii_lowercase(), kind, &chunk.path, &chunk.id],
                )?;
            }
        }
    }
    Ok(())
}

#[derive(Debug)]
struct PreparedFile {
    file: FileInfo,
    content: String,
    hash: String,
}

#[derive(Debug)]
struct IndexSummary {
    files_indexed: usize,
    chunks_indexed: usize,
    files_reindexed: usize,
    files_reused: usize,
    files_removed: usize,
}

#[derive(Debug, Copy, Clone)]
struct IndexBuildOptions {
    chunk_tokens: usize,
    chunk_overlap: usize,
    min_chunk_tokens: usize,
}

fn sha256_hex(content: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(content.as_bytes());
    let digest = hasher.finalize();
    format!("{digest:x}")
}

fn apply_byte_budget(
    ranked_files: Vec<FileInfo>,
    max_total_bytes: Option<u64>,
    stats: &mut ScanStats,
) -> Vec<FileInfo> {
    let Some(limit) = max_total_bytes else {
        return ranked_files;
    };

    let mut selected = Vec::new();
    let mut total = 0_u64;
    for (idx, file) in ranked_files.iter().enumerate() {
        if total >= limit {
            for remaining in &ranked_files[idx..] {
                stats.files_dropped_budget += 1;
                stats.dropped_files.push(HashMap::from([
                    ("path".to_string(), json!(remaining.relative_path)),
                    ("reason".to_string(), json!("bytes_limit")),
                    ("priority".to_string(), json!(remaining.priority)),
                ]));
            }
            break;
        }
        total += file.size_bytes;
        selected.push(file.clone());
    }
    stats.total_bytes_included = total;
    selected
}
