"""Strict consensus and flagging — no majority auto-accept."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field

from pipeline.ocr_engines import OcrResult, WordSpan


@dataclass
class FlaggedSpan:
    span_id: str
    line_index: int
    word_index: int
    char_start: int
    char_end: int
    text: str
    reason: str
    engine_texts: dict[str, str] = field(default_factory=dict)
    engine_confidences: dict[str, float] = field(default_factory=dict)
    bbox: tuple[int, int, int, int] | None = None
    resolved: bool = False
    resolution: str = ""


def _tokenize_lines(text: str) -> list[list[str]]:
    return [re.findall(r"\S+", line) for line in text.splitlines()]


def _normalize_token(tok: str) -> str:
    return tok.strip()


def _align_line_tokens(runs: dict[str, OcrResult], line_idx: int) -> list[dict]:
    per_run: dict[str, list[str]] = {}
    conf_maps: dict[str, dict[str, float]] = {k: {} for k in runs}

    for run_id, result in runs.items():
        lines = _tokenize_lines(result.full_text)
        per_run[run_id] = lines[line_idx] if line_idx < len(lines) else []
        by_line: dict[int, list[WordSpan]] = {}
        for w in result.words:
            by_line.setdefault(w.line_id, []).append(w)
        word_conf = {}
        if line_idx in by_line:
            for w in sorted(by_line[line_idx], key=lambda x: x.bbox[0]):
                word_conf[w.text] = w.confidence
        conf_maps[run_id] = word_conf

    max_len = max((len(v) for v in per_run.values()), default=0)
    aligned = []
    for wi in range(max_len):
        entry: dict = {"word_index": wi}
        texts = []
        for run_id in sorted(runs.keys()):
            tokens = per_run[run_id]
            if wi < len(tokens):
                tok = tokens[wi]
                entry[run_id] = tok
                entry[f"{run_id}_conf"] = conf_maps[run_id].get(tok, 0.0)
                texts.append(tok)
            else:
                entry[run_id] = ""
                entry[f"{run_id}_conf"] = 0.0
                texts.append("")
        entry["texts"] = texts
        aligned.append(entry)
    return aligned


def _all_agree(texts: list[str]) -> bool:
    normalized = [_normalize_token(t) for t in texts]
    if not any(normalized):
        return True
    first = normalized[0]
    return all(t == first for t in normalized)


def _all_high_conf(entry: dict, run_ids: list[str], threshold: float) -> bool:
    for rid in run_ids:
        conf = float(entry.get(f"{rid}_conf", 0))
        tok = str(entry.get(rid, ""))
        if tok and conf < threshold:
            return False
    return True


def analyze_runs(
    runs: dict[str, OcrResult],
    confidence_threshold: float = 85.0,
    extra_flags: list[FlaggedSpan] | None = None,
) -> tuple[list[FlaggedSpan], dict]:
    run_ids = sorted(runs.keys())
    max_lines = max((len(r.full_text.splitlines()) for r in runs.values()), default=0)
    flags: list[FlaggedSpan] = list(extra_flags or [])
    auto_accepted_words = 0
    flagged_words = 0
    span_counter = len(flags)

    for line_idx in range(max_lines):
        aligned = _align_line_tokens(runs, line_idx)
        char_pos = 0
        for entry in aligned:
            texts = entry["texts"]
            if not any(texts):
                continue
            agree = _all_agree(texts)
            high_conf = _all_high_conf(entry, run_ids, confidence_threshold)
            if agree and high_conf:
                auto_accepted_words += 1
                char_pos += len(str(entry.get("A", texts[0]))) + 1
                continue

            flagged_words += 1
            span_counter += 1
            display = str(entry.get("A") or entry.get("C") or texts[0] or "?")
            engine_texts = {rid: str(entry.get(rid, "")) for rid in run_ids}
            engine_conf = {rid: float(entry.get(f"{rid}_conf", 0)) for rid in run_ids}
            reasons = []
            if not agree:
                reasons.append("engine_disagreement")
            if not high_conf:
                reasons.append("low_confidence")
            flags.append(
                FlaggedSpan(
                    span_id=f"flag_{span_counter:05d}",
                    line_index=line_idx,
                    word_index=int(entry["word_index"]),
                    char_start=char_pos,
                    char_end=char_pos + len(display),
                    text=display,
                    reason=";".join(reasons),
                    engine_texts=engine_texts,
                    engine_confidences=engine_conf,
                )
            )
            char_pos += len(display) + 1

    stats = {
        "auto_accepted_words": auto_accepted_words,
        "flagged_words": flagged_words,
        "total_flags": len(flags),
        "auto_accept": len(flags) == 0,
        "confidence_threshold": confidence_threshold,
    }
    return flags, stats


def pick_draft_text(runs: dict[str, OcrResult]) -> str:
    return runs.get("A", next(iter(runs.values()))).full_text


def flags_to_csv(path: str, flags: list[FlaggedSpan]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["span_id", "line_index", "word_index", "text", "reason",
             "A", "B", "C", "D", "A_conf", "B_conf", "C_conf", "D_conf"]
        )
        for f in flags:
            writer.writerow([
                f.span_id, f.line_index, f.word_index, f.text, f.reason,
                f.engine_texts.get("A", ""), f.engine_texts.get("B", ""),
                f.engine_texts.get("C", ""), f.engine_texts.get("D", ""),
                f.engine_confidences.get("A", 0), f.engine_confidences.get("B", 0),
                f.engine_confidences.get("C", 0), f.engine_confidences.get("D", 0),
            ])


def consensus_to_json(path: str, page_id: str, flags: list[FlaggedSpan], stats: dict, layout_meta: dict) -> None:
    payload = {
        "page_id": page_id,
        "stats": stats,
        "layout": layout_meta,
        "flags": [
            {
                "span_id": f.span_id,
                "line_index": f.line_index,
                "word_index": f.word_index,
                "char_start": f.char_start,
                "char_end": f.char_end,
                "text": f.text,
                "reason": f.reason,
                "engine_texts": f.engine_texts,
                "engine_confidences": f.engine_confidences,
                "bbox": f.bbox,
                "resolved": f.resolved,
                "resolution": f.resolution,
            }
            for f in flags
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def build_review_needed(flags: list[FlaggedSpan], draft: str) -> str:
    if not flags:
        return ""
    lines = ["# Flagged spans requiring human review", ""]
    for f in flags:
        lines.append(f"- [{f.span_id}] line {f.line_index + 1}: `{f.text}` ({f.reason})")
        opts = ", ".join(f"{k}={v!r}" for k, v in f.engine_texts.items() if v)
        lines.append(f"  engines: {opts}")
    return "\n".join(lines)
