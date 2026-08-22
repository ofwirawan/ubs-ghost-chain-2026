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
    def _path_count(cls, graph: dict[str, set[str]], start: str, goal: str) -> int:
        # Count simple paths only up to the amount useful for ranking.
        total = 0
        stack = [(start, {start})]
        while stack and total < 4:
            node, seen = stack.pop()
            for nxt in graph.get(node, ()):
                if nxt == goal:
                    total += 1
                elif nxt not in seen:
                    stack.append((nxt, seen | {nxt}))
        return total

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
        endpoints = {tx["fromUserId"], tx["toUserId"]}
        components = self._component(graph, set())
        current_component = {components.get(node) for node in endpoints}
        signal = 0.0

        for field, weight in (("ipAddress", 0.11), ("deviceId", 0.13)):
            value = tx.get(field)
            related = [r for r in records if r.target in endpoints or r.source in endpoints]
            prior_values = {getattr(r, "ip" if field == "ipAddress" else "device") for r in related}
            present_prior = {v for v in prior_values if v is not None}
            if value is None and present_prior:
                signal += weight
            elif value is not None:
                if present_prior and value not in present_prior:
                    signal += weight * 0.8
                matching_components = set()
                for r in records:
                    if getattr(r, "ip" if field == "ipAddress" else "device") == value:
                        matching_components.update({components.get(r.source), components.get(r.target)})
                if matching_components - current_component:
                    signal += weight * 0.9
                if len([r for r in records if getattr(r, "ip" if field == "ipAddress" else "device") == value]) >= 2:
                    signal += weight * 0.25
        return min(signal, 0.45)

    def _score(self, tx: dict[str, Any], records: list[Transaction]) -> float:
        graph = self._adjacency(records)
        source, target = tx["fromUserId"], tx["toUserId"]
        score = 0.02
        if source == target:
            score += 0.55
        existing_nodes = set(graph) | {node for targets in graph.values() for node in targets}
        if source in existing_nodes or target in existing_nodes:
            score += 0.06  # extends an entity already present in the active flow
        if target in graph.get(source, set()):
            score += 0.08  # repeated edge / reinforcing flow
        if self._reachable(graph, target, source):
            score += 0.58  # closes a return path
            has_existing_cycle = any(
                self._reachable(graph, target_node, source_node)
                for source_node, targets in graph.items()
                for target_node in targets
            )
            if has_existing_cycle:
                score += 0.12  # reinforces an already established loop
        path_count = self._path_count(graph, source, target)
        if path_count:
            score += min(0.12 * path_count, 0.36)
        ancestors_source = {node for node in graph if self._reachable(graph, node, source)}
        ancestors_target = {node for node in graph if self._reachable(graph, node, target)}
        if (ancestors_source & ancestors_target) - {source}:
            score += 0.18  # convergence / alternative route
        score += self._identity_signal(tx, records, graph)
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
