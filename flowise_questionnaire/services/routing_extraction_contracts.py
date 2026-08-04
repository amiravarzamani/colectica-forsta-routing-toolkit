from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExtractedRoutingEdge:
    source_question: str
    target_question: str
    condition_text: str
    edge_type: str
    sequence_index: int
