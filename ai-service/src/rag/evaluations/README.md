# RAG evaluations

This folder contains reusable evaluation code. Versioned, human-readable benchmark inputs live in `data/rag/evaluations/`; generated result files are written beside them and ignored by Git.

## Retrieval method benchmark

`retrieval_method_benchmark_100.json` contains 100 queries: 60 English, 20 Arabic, and 20 French. It covers every PDF in `rag_documents/`, includes 20 cross-PDF comparisons, and labels relevant documents with `expected_documents`.

A cross-PDF query lists every source expected to contribute evidence. `Recall@k` records whether any expected PDF is retrieved by rank k; `TargetCoverage@k` records the fraction of expected PDFs found. Results also report language, cross-PDF, and per-document breakdowns. Labels are document-level, so the benchmark measures source retrieval rather than answer correctness or exact passage matching.

Notebook 5 compares retrieval methods using this set. Cross-encoder reranking is optional because it substantially increases run time.
