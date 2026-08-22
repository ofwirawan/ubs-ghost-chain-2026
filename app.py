from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from threading import RLock
from typing import Any

from flask import Flask, jsonify, request

LOOKBACK = timedelta(hours=24)


@dataclass(frozen=True)
class Transaction:
    tx_id: str
    source: str
    target: str
    amount: float
    created_at: datetime
    ip: str | None
    device: str | None
    score: float


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("createdAt must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("createdAt must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class RiskEngine:
    def __init__(self) -> None:
        self._lock = RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", RLock()):
            self.transactions: deque[Transaction] = deque()
            self.by_id: dict[str, Transaction] = {}

    def _expire(self, now: datetime) -> None:
        cutoff = now - LOOKBACK
        active = [tx for tx in self.transactions if tx.created_at >= cutoff]
        self.transactions = deque(active)
        self.by_id = {tx.tx_id: tx for tx in active}

    @staticmethod
    def _adjacency(records: list[Transaction]) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = defaultdict(set)
        for tx in records:
            graph[tx.source].add(tx.target)
        return graph

    @staticmethod
    def _reachable(graph: dict[str, set[str]], start: str, goal: str) -> bool:
        if start == goal:
            return True
        seen = {start}
        queue = [start]
        while queue:
            node = queue.pop()
            for nxt in graph.get(node, ()):
                if nxt == goal:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return False

    @classmethod
    def _count_simple_paths(cls, graph: dict[str, set[str]], start: str, goal: str, max_depth: int = 5) -> int:
        total = 0
        stack = [(start, {start})]
        while stack and total < 5:
            node, seen = stack.pop()
            if len(seen) > max_depth:
                continue
            for nxt in graph.get(node, ()):
                if nxt == goal:
                    total += 1
                elif nxt not in seen:
                    stack.append((nxt, seen | {nxt}))
        return total

    @classmethod
    def _get_ancestors(cls, graph: dict[str, set[str]], target_node: str) -> set[str]:
        ancestors = set()
        for node in graph:
            if node != target_node and cls._reachable(graph, node, target_node):
                ancestors.add(node)
        return ancestors

    @classmethod
    def _has_any_cycle(cls, graph: dict[str, set[str]]) -> bool:
        visited = set()
        rec_stack = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in graph.get(node, ()):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.remove(node)
            return False

        return any(node not in visited and dfs(node) for node in list(graph.keys()))

    @classmethod
    def _component(cls, graph: dict[str, set[str]], nodes: set[str]) -> dict[str, int]:
        undirected: dict[str, set[str]] = defaultdict(set)
        for source, targets in graph.items():
            nodes.add(source)
            undirected[source].update(targets)
            for target in targets:
                nodes.add(target)
                undirected[target].add(source)
        components: dict[str, int] = {}
        index = 0
        for node in nodes:
            if node in components:
                continue
            queue = [node]
            components[node] = index
            while queue:
                current = queue.pop()
                for neighbor in undirected[current]:
                    if neighbor not in components:
                        components[neighbor] = index
                        queue.append(neighbor)
            index += 1
        return components

    def _identity_signal(self, tx: dict[str, Any], records: list[Transaction], graph: dict[str, set[str]]) -> float:
        source = tx["fromUserId"]
        signal = 0.0

        upstream_txs = [r for r in records if r.target == source or self._reachable(graph, r.target, source)]
        components = self._component(graph, set())
        current_comp = components.get(source)

        for field, attr in (("ipAddress", "ip"), ("deviceId", "device")):
            val = tx.get(field)
            upstream_vals = {getattr(r, attr) for r in upstream_txs if getattr(r, attr) is not None}

            if val is None:
                if upstream_vals:
                    signal += 0.08  # Identifier omitted mid-stream
            else:
                if upstream_vals and val not in upstream_vals:
                    signal += 0.06  # Mid-flow shift

                matching_txs = [r for r in records if getattr(r, attr) == val]
                if matching_txs:
                    other_comps = {components.get(r.source) for r in matching_txs} | {components.get(r.target) for r in matching_txs}
                    other_comps.discard(current_comp)
                    other_comps.discard(None)

                    if other_comps:
                        has_flow_link = any(
                            self._reachable(graph, r.source, source) or self._reachable(graph, source, r.source)
                            for r in matching_txs
                        )
                        signal += 0.09 if has_flow_link else 0.03

        return min(signal, 0.35)

    def _value_signal(self, tx: dict[str, Any], records: list[Transaction]) -> float:
        source = tx["fromUserId"]
        amount = float(tx["amount"])

        # Locate direct upstream legs entering the source node
        incoming = [r for r in records if r.target == source]
        if not incoming:
            return 0.0

        signal = 0.0
        for in_tx in incoming:
            if in_tx.amount <= 0:
                continue

            ratio = amount / in_tx.amount

            # Value Trajectory Reversal: amount increases along an inferred flow segment
            if ratio > 1.001:
                reversal_penalty = min(0.20 + (ratio - 1.0) * 0.5, 0.35)
                signal = max(signal, reversal_penalty)
            # Unexplained abrupt drop without clear branching context
            elif ratio < 0.40:
                signal = max(signal, 0.04)

        return min(signal, 0.35)

    def _score(self, tx: dict[str, Any], records: list[Transaction]) -> float:
        graph = self._adjacency(records)
        source, target = tx["fromUserId"], tx["toUserId"]
        score = 0.02

        if source == target:
            score += 0.50

        existing_nodes = set(graph) | {node for targets in graph.values() for node in targets}
        if source in existing_nodes or target in existing_nodes:
            score += 0.04

        if target in graph.get(source, set()):
            score += 0.04

        return_paths = self._count_simple_paths(graph, target, source)
        if return_paths > 0:
            score += 0.35
            if return_paths > 1:
                score += min(0.12 * (return_paths - 1), 0.24)
            if self._has_any_cycle(graph):
                score += 0.10
        else:
            paths_from_source = self._count_simple_paths(graph, source, target)
            if paths_from_source > 0:
                score += min(0.06 * paths_from_source, 0.18)

            ancestors_source = self._get_ancestors(graph, source)
            ancestors_target = self._get_ancestors(graph, target)
            if (ancestors_source & ancestors_target) - {source}:
                score += 0.12

        # Combine Structural, Identity, and Value signals
        score += self._identity_signal(tx, records, graph)
        score += self._value_signal(tx, records)

        return round(min(max(score, 0.0), 1.0), 6)

    def process(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload.get("transactions")
        if not isinstance(items, list):
            raise ValueError("transactions must be an array")
        results = []
        with self._lock:
            for raw in items:
                if not isinstance(raw, dict):
                    raise ValueError("each transaction must be an object")
                required = ("txId", "fromUserId", "toUserId", "amount", "createdAt")
                if any(not isinstance(raw.get(k), str) or not raw.get(k) for k in required if k != "amount"):
                    raise ValueError("txId, fromUserId, toUserId, and createdAt are required strings")
                try:
                    amount = float(raw["amount"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("amount must be a number") from exc
                if not isfinite(amount) or amount < 0:
                    raise ValueError("amount must be a finite non-negative number")
                created_at = parse_timestamp(raw["createdAt"])
                self._expire(created_at)
                existing = self.by_id.get(raw["txId"])
                fingerprint = (raw["fromUserId"], raw["toUserId"], amount, created_at, raw.get("ipAddress"), raw.get("deviceId"))
                if existing:
                    existing_fingerprint = (existing.source, existing.target, existing.amount, existing.created_at, existing.ip, existing.device)
                    if fingerprint != existing_fingerprint:
                        raise ValueError(f"txId {raw['txId']} was already submitted with a different payload")
                    results.append({"txId": existing.tx_id, "riskScore": existing.score})
                    continue
                score = self._score(raw, list(self.transactions))
                record = Transaction(raw["txId"], raw["fromUserId"], raw["toUserId"], amount, created_at, raw.get("ipAddress"), raw.get("deviceId"), score)
                self.transactions.append(record)
                self.by_id[record.tx_id] = record
                results.append({"txId": record.tx_id, "riskScore": score})
        return results


app = Flask(__name__)
engine = RiskEngine()


@app.get("/ghost-chains/health")
def health():
    return jsonify(status="ok")


@app.post("/ghost-chains/reset")
def reset():
    body = request.get_json(silent=True) or {}
    if body.get("clearTransactions") is not True:
        return jsonify(error="clearTransactions must be true"), 400
    engine.reset()
    return jsonify(clearTransactions=True)


@app.post("/ghost-chains/transactions")
def transactions():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="request body must be a JSON object"), 400
    try:
        return jsonify(transactions=engine.process(body))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
