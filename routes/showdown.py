import json, os, logging
from flask import Flask, jsonify, request

from routes import app

MEMORY_FILE = "rule_hypotheses.json"

# Candidate evaluation functions: Return 1 if c1 wins, -1 if c2 wins, 0 if tie
def score_standard(c1, c2, comm):
    p1, p2 = (c1 == comm), (c2 == comm)
    return 1 if (p1, c1) > (p2, c2) else (-1 if (p1, c1) < (p2, c2) else 0)

def score_lowball(c1, c2, comm):
    p1, p2 = (c1 == comm), (c2 == comm)
    return 1 if (p1, -c1) > (p2, -c2) else (-1 if (p1, -c1) < (p2, -c2) else 0)

def score_closest(c1, c2, comm):
    d1, d2 = abs(c1 - comm), abs(c2 - comm)
    return 1 if d1 < d2 else (-1 if d1 > d2 else 0)

def score_furthest(c1, c2, comm):
    d1, d2 = abs(c1 - comm), abs(c2 - comm)
    return 1 if d1 > d2 else (-1 if d1 < d2 else 0)

def score_odd_even(c1, c2, comm):
    k1 = (c1 % 2 != 0, c1 == comm, c1)
    k2 = (c2 % 2 != 0, c2 == comm, c2)
    return 1 if k1 > k2 else (-1 if k1 < k2 else 0)

RULE_CANDIDATES = {
    "standard": score_standard,
    "lowball": score_lowball,
    "closest": score_closest,
    "furthest": score_furthest,
    "odd_even": score_odd_even
}

def get_rule_scores(table_rule: str) -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            data = json.load(f)
            if table_rule in data:
                return data[table_rule]
    return {name: 1.0 for name in RULE_CANDIDATES}

def update_hypotheses(table_rule: str, recent_hands: list):
    scores = get_rule_scores(table_rule)
    updated = False

    for hand in recent_hands:
        shown = hand.get("shown_numbers", {})
        winners = hand.get("winners", [])
        comm = hand.get("community_number")

        if len(shown) == 2 and comm is not None:
            c0, c1 = shown.get("0"), shown.get("1")
            if c0 is None or c1 is None or c0 == c1:
                continue

            actual_res = 0 if len(winners) > 1 else (1 if winners[0] == 0 else -1)

            for name, fn in RULE_CANDIDATES.items():
                pred = fn(c0, c1, comm)
                if pred != actual_res and scores[name] > 0:
                    scores[name] = 0.0  # Eliminate invalid hypothesis
                    updated = True

    if updated:
        all_data = {}
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r") as f:
                all_data = json.load(f)
        all_data[table_rule] = scores
        with open(MEMORY_FILE, "w") as f:
            json.dump(all_data, f, indent=2)

def evaluate_strength(your_card: int, comm: int | None, table_rule: str) -> tuple[float, bool]:
    scores = get_rule_scores(table_rule)
    active_rules = [name for name, score in scores.items() if score > 0]

    # High confidence if down to 1 rule hypothesis
    is_confident = len(active_rules) == 1

    if comm is None:
        # Pre-reveal: Default cautious evaluation if rule is unknown
        return (your_card - 1 + 0.5) / 13.0, is_confident

    total_wins = 0
    total_evals = 0

    for rule_name in active_rules:
        fn = RULE_CANDIDATES[rule_name]
        for opp_card in range(1, 14):
            res = fn(your_card, opp_card, comm)
            total_wins += 1.0 if res > 0 else (0.5 if res == 0 else 0.0)
            total_evals += 1

    return (total_wins / total_evals) if total_evals > 0 else 0.5, is_confident

@app.post("/move")
def showdown():
    data = request.get_json(silent=True) or {}
    table_rule = data.get("table_rule", "standard")
    update_hypotheses(table_rule, data.get("recent_hands", []))

    your_card = data.get("your_number")
    comm_card = data.get("community_number")
    legal_actions = data.get("legal_actions", [])
    to_call = data.get("to_call", 0)
    pot = data.get("pot", 0)
    min_raise, max_raise = data.get("min_raise_to"), data.get("max_raise_to")

    win_prob, is_confident = evaluate_strength(your_card, comm_card, table_rule)

    # Risk Management: Don't overcommit pre-reveal or when rule confidence is low
    if not is_confident and comm_card is None:
        win_prob = 0.5  # Treat pre-reveal neutrally until a showdown is observed

    can_raise = "raise" in legal_actions and min_raise is not None
    can_bet = "bet" in legal_actions and min_raise is not None

    if win_prob > 0.75 and (can_raise or can_bet):
        action = "raise" if can_raise else "bet"
        size = min(max(min_raise + int((max_raise - min_raise) * (win_prob - 0.75) / 0.25), min_raise), max_raise)
        return jsonify({"action": action, "amount": size})

    if win_prob > 0.55 and can_bet:
        return jsonify({"action": "bet", "amount": min_raise})

    pot_odds = to_call / (pot + to_call) if (pot + to_call) > 0 else 0
    if "call" in legal_actions and win_prob >= pot_odds:
        return jsonify({"action": "call"})

    return jsonify({"action": "check" if "check" in legal_actions else "fold"})

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.post("/stonks")
def solve_stonks():
    data = request.get_json()
    energy = data["energy"]
    capital = data["capital"]
    timeline = data["timeline"]

    best_profit = -1
    best_path = []

    def dfs(year, curr_energy, curr_cap, holdings, timeline_state, path):
        nonlocal best_profit, best_path

        # Pruning: verify enough energy exists to return to origin 2037
        if curr_energy < abs(year - 2037):
            return

        # Record maximum profit achieved back at base year
        if year == 2037:
            profit = curr_cap - capital
            if profit > best_profit:
                best_profit = profit
                best_path = list(path)

        # 1. Action: Buy available stocks in current year
        for stock, info in timeline_state.get(str(year), {}).items():
            price, max_qty = info["price"], info["qty"]
            for qty in range(1, min(max_qty, curr_cap // price) + 1):
                cost = qty * price
                timeline_state[str(year)][stock]["qty"] -= qty
                holdings[stock] = holdings.get(stock, 0) + qty

                dfs(
                    year,
                    curr_energy,
                    curr_cap - cost,
                    holdings,
                    timeline_state,
                    path + [f"b-{stock}-{qty}"],
                )

                holdings[stock] -= qty
                timeline_state[str(year)][stock]["qty"] += qty

        # 2. Action: Sell stock holdings in current year
        for stock, qty in list(holdings.items()):
            if qty > 0 and stock in timeline_state.get(str(year), {}):
                earned = qty * timeline_state[str(year)][stock]["price"]
                holdings[stock] = 0

                dfs(
                    year,
                    curr_energy,
                    curr_cap + earned,
                    holdings,
                    timeline_state,
                    path + [f"s-{stock}-{qty}"],
                )

                holdings[stock] = qty

        # 3. Action: Jump to another timeline year
        for target_str in timeline.keys():
            target_year = int(target_str)
            cost = abs(year - target_year)
            if target_year != year and curr_energy >= cost + abs(target_year - 2037):
                dfs(
                    target_year,
                    curr_energy - cost,
                    curr_cap,
                    holdings,
                    timeline_state,
                    path + [f"j-{year}-{target_year}"],
                )

    dfs(2037, energy, capital, {}, timeline, [])
    return jsonify(best_path)
