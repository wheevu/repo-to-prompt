//! File ranking by importance

use crate::domain::{Chunk, FileInfo, RankingWeights};
use anyhow::Result;
use serde_json::Value as JsonValue;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};

pub mod bm25;
pub mod ranker;

pub use ranker::FileRanker;

pub fn rerank_chunks_by_task(
    chunks: &mut [Chunk],
    query: &str,
    relevance_weight: f64,
) -> HashMap<String, f64> {
    let weight = relevance_weight.clamp(0.0, 1.0);
    let lexical_scores = bm25::score_query_against_chunks(chunks, query);

    let mut max_score = 0.0_f64;
    for score in &lexical_scores {
        max_score = max_score.max(*score);
    }

    let mut file_scores: HashMap<String, f64> = HashMap::new();
    let mut lexical_by_file: HashMap<String, f64> = HashMap::new();

    for (chunk, lexical) in chunks.iter_mut().zip(lexical_scores.into_iter()) {
        let normalized = if max_score > 0.0 { lexical / max_score } else { 0.0 };
        let blended = (chunk.priority * (1.0 - weight)) + (normalized * weight);
        chunk.priority = (blended * 1000.0).round() / 1000.0;
        file_scores
            .entry(chunk.path.clone())
            .and_modify(|existing| *existing = existing.max(chunk.priority))
            .or_insert(chunk.priority);
        lexical_by_file
            .entry(chunk.path.clone())
            .and_modify(|existing| *existing = existing.max(normalized))
            .or_insert(normalized);
    }

    let expansion = dependency_expansion_scores(chunks, &lexical_by_file);
    for chunk in chunks.iter_mut() {
        if let Some(expanded) = expansion.get(&chunk.path) {
            let boosted = (chunk.priority * 0.8) + (expanded * 0.2);
            chunk.priority = (boosted * 1000.0).round() / 1000.0;
        }
    }

    file_scores.clear();
    for chunk in chunks.iter() {
        file_scores
            .entry(chunk.path.clone())
            .and_modify(|existing| *existing = existing.max(chunk.priority))
            .or_insert(chunk.priority);
    }

    file_scores
}

fn dependency_expansion_scores(
    chunks: &[Chunk],
    lexical_by_file: &HashMap<String, f64>,
) -> HashMap<String, f64> {
    let known_files: HashSet<String> = chunks.iter().map(|c| c.path.clone()).collect();
    if known_files.is_empty() {
        return HashMap::new();
    }

    let symbol_defs = symbol_definitions(chunks);
    let graph = dependency_graph(chunks, &known_files, &symbol_defs);

    let mut seeds: Vec<(&String, &f64)> =
        lexical_by_file.iter().filter(|(_, s)| **s > 0.0).collect();
    seeds.sort_by(|a, b| {
        b.1.partial_cmp(a.1).unwrap_or(std::cmp::Ordering::Equal).then_with(|| a.0.cmp(b.0))
    });

    let mut expanded: HashMap<String, f64> = HashMap::new();
    for (seed, score) in seeds.into_iter().take(5) {
        expanded.entry(seed.clone()).and_modify(|v| *v = v.max(*score)).or_insert(*score);

        if let Some(neighbors) = graph.get(seed) {
            for neighbor in neighbors {
                let value = (score * 0.6).min(1.0);
                expanded.entry(neighbor.clone()).and_modify(|v| *v = v.max(value)).or_insert(value);

                if let Some(level2) = graph.get(neighbor) {
                    for neighbor2 in level2 {
                        let value2 = (score * 0.3).min(1.0);
                        expanded
                            .entry(neighbor2.clone())
                            .and_modify(|v| *v = v.max(value2))
                            .or_insert(value2);
                    }
                }
            }
        }
    }

    expanded
}

fn symbol_definitions(chunks: &[Chunk]) -> HashMap<String, HashSet<String>> {
    let mut defs: HashMap<String, HashSet<String>> = HashMap::new();
    for chunk in chunks {
        for tag in &chunk.tags {
            if let Some((kind, name)) = tag.split_once(':') {
                if matches!(kind, "def" | "type" | "impl") && !name.is_empty() {
                    defs.entry(name.to_ascii_lowercase()).or_default().insert(chunk.path.clone());
                }
            }
        }
    }
    defs
}

fn dependency_graph(
    chunks: &[Chunk],
    known_files: &HashSet<String>,
    symbol_defs: &HashMap<String, HashSet<String>>,
) -> HashMap<String, BTreeSet<String>> {
    let mut graph: HashMap<String, BTreeSet<String>> = HashMap::new();

    for chunk in chunks {
        for reference in extract_import_references(&chunk.content) {
            for target in resolve_reference(&reference, &chunk.path, known_files) {
                if target != chunk.path {
                    graph.entry(chunk.path.clone()).or_default().insert(target.clone());
                    graph.entry(target).or_default().insert(chunk.path.clone());
                }
            }
        }

        for token in tokenize(&chunk.content) {
            if let Some(def_files) = symbol_defs.get(&token) {
                for target in def_files {
                    if target != &chunk.path {
                        graph.entry(chunk.path.clone()).or_default().insert(target.clone());
                        graph.entry(target.clone()).or_default().insert(chunk.path.clone());
                    }
                }
            }
        }
    }

    graph
}

fn extract_import_references(content: &str) -> Vec<String> {
    let mut refs = Vec::new();
    for line in content.lines() {
        let trimmed = line.trim();

        if let Some(rest) = trimmed.strip_prefix("from ") {
            if let Some(module) = rest.split_whitespace().next() {
                refs.push(module.trim_matches('"').trim_matches('\'').to_string());
            }
        }

        if let Some(rest) = trimmed.strip_prefix("import ") {
            for module in rest.split(',') {
                if let Some(m) = module.split_whitespace().next() {
                    refs.push(m.trim_matches('"').trim_matches('\'').to_string());
                }
            }
        }

        if let Some(rest) = trimmed.strip_prefix("use ") {
            if let Some(module) = rest.split(';').next() {
                refs.push(module.trim().to_string());
            }
        }

        if let Some(rest) = trimmed.strip_prefix("mod ") {
            if let Some(module) = rest.split(';').next() {
                refs.push(module.trim().to_string());
            }
        }

        for marker in [" from '", " from \"", "require('", "require(\""] {
            if let Some(pos) = trimmed.find(marker) {
                let tail = &trimmed[pos + marker.len()..];
                let module = tail.split(['\'', '"', ')']).next().unwrap_or("").trim();
                if !module.is_empty() {
                    refs.push(module.to_string());
                }
            }
        }
    }
    refs
}

fn resolve_reference(
    reference: &str,
    current_file: &str,
    known_files: &HashSet<String>,
) -> Vec<String> {
    let mut out = BTreeSet::new();
    let cleaned = reference
        .trim()
        .trim_start_matches("crate::")
        .trim_start_matches("self::")
        .trim_start_matches("super::")
        .replace("::", "/")
        .replace('.', "/");

    if cleaned.is_empty() {
        return Vec::new();
    }

    let mut candidates = vec![cleaned.clone()];
    candidates.extend(candidate_paths(&cleaned));

    if reference.starts_with("./") || reference.starts_with("../") {
        let base = Path::new(current_file).parent().unwrap_or_else(|| Path::new(""));
        let rel = normalize_path(base.join(reference));
        candidates.push(rel.clone());
        candidates.extend(candidate_paths(&rel));
    }

    let lower_known: HashMap<String, String> =
        known_files.iter().map(|f| (f.to_ascii_lowercase(), f.clone())).collect();

    for candidate in candidates {
        let c = candidate.to_ascii_lowercase();
        if let Some(exact) = lower_known.get(&c) {
            out.insert(exact.clone());
        }
        for (lf, original) in &lower_known {
            if lf.ends_with(&format!("/{c}"))
                || lf.ends_with(&format!("/{c}.py"))
                || lf.ends_with(&format!("/{c}.rs"))
                || lf.ends_with(&format!("/{c}.ts"))
                || lf.ends_with(&format!("/{c}.js"))
            {
                out.insert(original.clone());
            }
        }
    }

    out.into_iter().collect()
}

fn candidate_paths(module: &str) -> Vec<String> {
    [
        module.to_string(),
        format!("{module}.py"),
        format!("{module}/__init__.py"),
        format!("{module}.rs"),
        format!("{module}/mod.rs"),
        format!("{module}.ts"),
        format!("{module}.tsx"),
        format!("{module}.js"),
        format!("{module}.jsx"),
        format!("{module}.go"),
    ]
    .to_vec()
}

fn normalize_path(path: PathBuf) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn tokenize(text: &str) -> Vec<String> {
    text.split(|c: char| !c.is_alphanumeric() && c != '_')
        .filter_map(|token| {
            let lower = token.trim().to_ascii_lowercase();
            if lower.len() >= 2 {
                Some(lower)
            } else {
                None
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::rerank_chunks_by_task;
    use crate::domain::Chunk;
    use std::collections::BTreeSet;

    #[test]
    fn reranking_expands_to_related_files() {
        let mut chunks = vec![
            Chunk {
                id: "1".to_string(),
                path: "src/auth.py".to_string(),
                language: "python".to_string(),
                start_line: 1,
                end_line: 20,
                content: "def refresh_token():\n    return True\n".to_string(),
                priority: 0.5,
                tags: BTreeSet::from(["def:refresh_token".to_string()]),
                token_estimate: 10,
            },
            Chunk {
                id: "2".to_string(),
                path: "tests/test_auth.py".to_string(),
                language: "python".to_string(),
                start_line: 1,
                end_line: 20,
                content: "from src.auth import login\n\ndef test_login():\n    assert login()\n"
                    .to_string(),
                priority: 0.2,
                tags: BTreeSet::new(),
                token_estimate: 16,
            },
        ];

        let scores = rerank_chunks_by_task(&mut chunks, "refresh token bug", 0.4);
        assert!(scores.contains_key("src/auth.py"));
        assert!(scores.contains_key("tests/test_auth.py"));
        assert!(scores["tests/test_auth.py"] >= 0.12);
    }
}

pub fn rank_files(root_path: &Path, files: Vec<FileInfo>) -> Result<Vec<FileInfo>> {
    rank_files_with_weights(root_path, files, RankingWeights::default())
}

pub fn rank_files_with_weights(
    root_path: &Path,
    mut files: Vec<FileInfo>,
    weights: RankingWeights,
) -> Result<Vec<FileInfo>> {
    let scanned_files: HashSet<String> = files.iter().map(|f| f.relative_path.clone()).collect();
    let ranker = FileRanker::with_weights(root_path, scanned_files, weights);
    ranker.rank_files(&mut files);
    Ok(files)
}

/// Same as `rank_files_with_weights` but also returns manifest info extracted during ranking.
/// The manifest info includes `scripts`, `name`, `description` from `package.json` etc.
pub fn rank_files_with_manifest(
    root_path: &Path,
    mut files: Vec<FileInfo>,
    weights: RankingWeights,
) -> Result<(Vec<FileInfo>, HashMap<String, JsonValue>)> {
    let scanned_files: HashSet<String> = files.iter().map(|f| f.relative_path.clone()).collect();
    let ranker = FileRanker::with_weights(root_path, scanned_files, weights);
    ranker.rank_files(&mut files);
    let manifest = ranker.get_manifest_info().clone();
    Ok((files, manifest))
}
