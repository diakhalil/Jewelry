"""Metrics for retrieval evaluation with document-level relevance labels."""

from __future__ import annotations

import math
import pandas as pd


def summarize_retrieval_metrics(test_cases, case_results, detailed_results, *, cutoffs=(1, 3, 5)):
    """Return overall, by-language, and cross-document metric tables.

    Multiple expected documents are measured using both query hit rate and
    the fraction of expected documents found within each cutoff.
    """
    case_by_id = {case["id"]: case for case in test_cases}
    required = {"method", "case_id", "relevant_rank", "latency_ms"}
    missing = required - set(case_results.columns)
    if missing:
        raise ValueError(f"case_results missing columns: {sorted(missing)}")
    required_details = {"method", "case_id", "rank", "file_name"}
    missing = required_details - set(detailed_results.columns)
    if missing:
        raise ValueError(f"detailed_results missing columns: {sorted(missing)}")

    overall_rows = []
    for method, group in case_results.groupby("method", sort=False):
        method_details = detailed_results[detailed_results["method"] == method]
        rows = []
        for result in group.to_dict(orient="records"):
            case = case_by_id[result["case_id"]]
            expected = set(case.get("expected_documents") or [])
            retrieved = method_details[method_details["case_id"] == result["case_id"]].sort_values("rank")
            rows.append((result, expected, retrieved))
        metric = {"method": method, "queries": len(rows)}
        for cutoff in cutoffs:
            hits = [pd.notna(r["relevant_rank"]) and r["relevant_rank"] <= cutoff for r, _, _ in rows]
            metric[f"Recall@{cutoff}"] = sum(hits) / len(hits) if hits else math.nan
            coverage = [len(set(frame.head(cutoff)["file_name"]) & expected) / len(expected)
                        for _, expected, frame in rows if expected]
            metric[f"TargetCoverage@{cutoff}"] = sum(coverage) / len(coverage) if coverage else math.nan
        metric["MRR@5"] = (
            sum(0.0 if pd.isna(r["relevant_rank"]) or r["relevant_rank"] > 5
                else 1.0 / r["relevant_rank"] for r, _, _ in rows) / len(rows)
            if rows else math.nan
        )
        metric["mean_latency_ms"] = group["latency_ms"].mean()
        overall_rows.append(metric)
    overall = pd.DataFrame(overall_rows)
    if len(overall):
        overall = overall.sort_values(["Recall@5", "MRR@5", "Recall@1", "mean_latency_ms"],
                                      ascending=[False, False, False, True]).reset_index(drop=True)

    frame = case_results.copy()
    frame["language"] = frame["case_id"].map({k: v.get("language", "unknown") for k, v in case_by_id.items()})
    frame["cross_pdf"] = frame["case_id"].map({k: bool(v.get("cross_pdf")) for k, v in case_by_id.items()})
    language_rows = []
    for (method, language), group in frame.groupby(["method", "language"], sort=False):
        language_rows.append({"method": method, "language": language, "queries": len(group),
                              "Recall@1": group["relevant_rank"].le(1).fillna(False).mean(),
                              "Recall@5": group["relevant_rank"].le(5).fillna(False).mean(),
                              "mean_latency_ms": group["latency_ms"].mean()})
    cross_rows = []
    for method, group in frame[frame["cross_pdf"]].groupby("method", sort=False):
        cross_rows.append({"method": method, "queries": len(group),
                           "Recall@1": group["relevant_rank"].le(1).fillna(False).mean(),
                           "Recall@5": group["relevant_rank"].le(5).fillna(False).mean()})
    document_rows = []
    for method, group in case_results.groupby("method", sort=False):
        for document in sorted({
            filename for case in test_cases
            for filename in (case.get("expected_documents") or [])
        }):
            ids = {case["id"] for case in test_cases
                   if document in (case.get("expected_documents") or [])}
            subset = group[group["case_id"].isin(ids)]
            if subset.empty:
                continue
            document_rows.append({
                "method": method, "document": document, "queries": len(subset),
                "Recall@1": subset["relevant_rank"].le(1).fillna(False).mean(),
                "Recall@5": subset["relevant_rank"].le(5).fillna(False).mean(),
            })
    by_document = pd.DataFrame(document_rows)
    return overall, pd.DataFrame(language_rows), pd.DataFrame(cross_rows), by_document
