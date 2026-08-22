from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from threading import RLock
from typing import Any

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
        source, target = tx["fromUserId"], tx["toUserId"]
        signal = 0.0

        upstream_txs = [r for r in records if r.target == source or self._reachable(graph, r.target, source)]
        components = self._component(graph, set())
        current_comp = components.get(source)

        for field, attr in (("ipAddress", "ip"), ("deviceId", "device")):
            val = tx.get(field)
            upstream_vals = {getattr(r, attr) for r in upstream_txs if getattr(r, attr) is not None}

            if val is None:
                # Trail-dropping: identity was present upstream but omitted here
                if upstream_vals:
                    signal += 0.08
            else:
                # Identity shift/divergence mid-flow
                if upstream_vals and val not in upstream_vals:
                    signal += 0.06

                # Cross-component reuse check
                matching_txs = [r for r in records if getattr(r, attr) == val]
                if matching_txs:
                    other_comps = {components.get(r.source) for r in matching_txs} | {components.get(r.target) for r in matching_txs}
                    other_comps.discard(current_comp)
                    other_comps.discard(None)

                    if other_comps:
                        # Shared across disconnected components
                        has_flow_link = any(
                            self._reachable(graph, r.source, source) or self._reachable(graph, source, r.source)
                            for r in matching_txs
                        )
                        # Higher penalty if components have flow/proximity, lower hint if strictly isolated
                        signal += 0.09 if has_flow_link else 0.03

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

        # Return path scoring
        return_paths = self._count_simple_paths(graph, target, source)
        if return_paths > 0:
            score += 0.35  # Return path completed
            if return_paths > 1:
                score += min(0.12 * (return_paths - 1), 0.24)  # Multi-loop return path
            if self._has_any_cycle(graph):
                score += 0.10  # Pre-existing cycle in network
        else:
            # Convergence and path extensions
            paths_from_source = self._count_simple_paths(graph, source, target)
            if paths_from_source > 0:
                score += min(0.06 * paths_from_source, 0.18)

            ancestors_source = self._get_ancestors(graph, source)
            ancestors_target = self._get_ancestors(graph, target)
            if (ancestors_source & ancestors_target) - {source}:
                score += 0.12  # Convergence

        score += self._identity_signal(tx, records, graph)
        return round(min(max(score, 0.0), 1.0), 6)


if __name__ == '__main__':
    app.run(debug=True)
