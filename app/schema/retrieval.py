"""Relevance-filtered schema retrieval.

Each table is represented by a document (name, description, columns, column descriptions, approved categorical
values, glossary terms that reference it). The question and table documents are embedded with a local,
deterministic hashed bag-of-words + character-trigram embedding (no network call, reproducible in tests) and
compared with cosine similarity. Tables above a threshold are selected; glossary hits force-include their tables;
then bridge tables on the shortest foreign-key path between selected tables are added so required joins survive.
The `Embedder` protocol allows swapping in a hosted embedding model.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from app.models import SchemaModel

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "by", "with", "what", "which", "who", "how",
         "many", "much", "is", "are", "was", "were", "do", "does", "did", "show", "me", "list", "give", "all", "each",
         "per", "from", "that", "this", "their", "its", "have", "has", "there", "be", "at", "as", "most", "top",
         "number", "total", "count", "average", "between", "during", "than", "more", "less"}
# Light domain synonyms mapping question vocabulary to schema vocabulary.
SYNONYMS = {
    "revenue": ["order_items", "sales", "price", "quantity"], "sales": ["order_items", "revenue"],
    "sold": ["order_items", "quantity"], "units": ["quantity"], "spend": ["payments", "amount"],
    "spent": ["payments", "amount"], "paid": ["payments"], "payment": ["payments"], "refund": ["refunds"],
    "refunded": ["refunds"], "returned": ["refunds", "status"], "buyer": ["customers"], "client": ["customers"],
    "customer": ["customers"], "item": ["products", "order_items"], "product": ["products"],
    "category": ["categories"], "region": ["regions"], "staff": ["employees"], "employee": ["employees"],
    "rep": ["employees"], "order": ["orders"], "purchase": ["orders"], "shipping": ["orders", "shipping_cost"],
    "channel": ["orders"], "country": ["customers"], "city": ["customers"], "signed": ["signup_date"],
    "margin": ["unit_cost", "products"], "profit": ["unit_cost", "products"], "discount": ["order_items"],
    "launched": ["launched_on", "products"], "hired": ["hired_on", "employees"], "method": ["payments"],
}


def tokenize(text: str) -> list[str]:
    toks = []
    for t in _WORD.findall(text.lower().replace("_", " ")):
        if t in _STOP:
            continue
        if len(t) > 3 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        toks.append(t)
    return toks


class Embedder(Protocol):
    def embed(self, text: str) -> dict[int, float]: ...


class HashingEmbedder:
    """Sparse hashed embedding: word unigrams (weight 1.0), synonyms (0.6) and character trigrams (0.3)."""

    def __init__(self, dims: int = 2048) -> None:
        self.dims = dims

    def _h(self, s: str) -> int:
        return int(hashlib.blake2b(s.encode(), digest_size=4).hexdigest(), 16) % self.dims

    def embed(self, text: str) -> dict[int, float]:
        vec: dict[int, float] = {}

        def add(feature: str, w: float) -> None:
            k = self._h(feature)
            vec[k] = vec.get(k, 0.0) + w

        for tok in tokenize(text):
            add("w:" + tok, 1.0)
            for syn in SYNONYMS.get(tok, []):
                for st in tokenize(syn):
                    add("w:" + st, 0.6)
            padded = f"#{tok}#"
            for i in range(len(padded) - 2):
                add("c:" + padded[i:i + 3], 0.3)
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {k: v / norm for k, v in vec.items()}


def cosine(a: dict[int, float], b: dict[int, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


@dataclass
class RetrievalResult:
    tables: list[str]
    scores: dict[str, float]
    forced_by_glossary: list[str] = field(default_factory=list)
    bridge_tables: list[str] = field(default_factory=list)
    glossary_hits: list[str] = field(default_factory=list)


class SchemaRetriever:
    def __init__(self, schema: SchemaModel, embedder: Embedder | None = None, threshold: float = 0.15,
                 relative_threshold: float = 0.55, min_tables: int = 1, max_tables: int = 6) -> None:
        self.relative_threshold = relative_threshold
        self.schema = schema
        self.embedder = embedder or HashingEmbedder()
        self.threshold = threshold
        self.min_tables = min_tables
        self.max_tables = max_tables
        self._docs = {name: self.embedder.embed(self._table_doc(name)) for name in schema.tables}
        self._graph = self._fk_graph()

    def _table_doc(self, name: str) -> str:
        t = self.schema.tables[name]
        parts = [name, name, t.description or ""]
        for c in t.allowed_columns():
            parts += [c.name, c.description or "", " ".join(c.categorical_values)]
        for g in self.schema.glossary:
            if name in g.tables:
                parts.append(g.term)
        return " ".join(parts)

    def _fk_graph(self) -> dict[str, set[str]]:
        g: dict[str, set[str]] = {n: set() for n in self.schema.tables}
        for t, _c, rt, _rc in self.schema.fk_edges():
            if rt in g and t != rt:
                g[t].add(rt)
                g[rt].add(t)
        return g

    def shortest_path(self, a: str, b: str) -> list[str]:
        prev = {a: None}
        q = deque([a])
        while q:
            cur = q.popleft()
            if cur == b:
                break
            for nxt in sorted(self._graph.get(cur, ())):
                if nxt not in prev:
                    prev[nxt] = cur
                    q.append(nxt)
        if b not in prev:
            return []
        path, node = [], b
        while node is not None:
            path.append(node)
            node = prev[node]
        return path[::-1]

    def retrieve(self, question: str) -> RetrievalResult:
        q = self.embedder.embed(question)
        scores = {name: round(cosine(q, doc), 4) for name, doc in self._docs.items()}
        ranked = sorted(scores, key=lambda n: -scores[n])
        cutoff = max(self.threshold, self.relative_threshold * (scores[ranked[0]] if ranked else 0.0))
        selected = [n for n in ranked if scores[n] >= cutoff]
        ql = question.lower()
        glossary_hits, forced = [], []
        for g in self.schema.glossary:
            if re.search(r"\b" + re.escape(g.term) + r"\b", ql):
                glossary_hits.append(g.term)
                for t in g.tables:
                    if t in self.schema.tables and t not in selected:
                        selected.append(t)
                        forced.append(t)
        for n in ranked:
            if len(selected) >= self.min_tables:
                break
            if n not in selected:
                selected.append(n)
        selected = selected[: self.max_tables]
        bridges: list[str] = []
        for i, a in enumerate(list(selected)):
            for b in selected[i + 1:]:
                for node in self.shortest_path(a, b):
                    if node not in selected and node not in bridges:
                        bridges.append(node)
        tables = selected + bridges
        return RetrievalResult(tables=tables, scores=scores, forced_by_glossary=forced, bridge_tables=bridges,
                               glossary_hits=glossary_hits)
