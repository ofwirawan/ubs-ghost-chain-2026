import copy
import json
import logging
import os
import traceback

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
    "odd_even": score_odd_even,
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


def evaluate_strength(
    your_card: int, comm: int | None, table_rule: str
) -> tuple[float, bool]:
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
        size = min(
            max(
                min_raise + int((max_raise - min_raise) * (win_prob - 0.75) / 0.25),
                min_raise,
            ),
            max_raise,
        )
        return jsonify({"action": action, "amount": size})

    if win_prob > 0.55 and can_bet:
        return jsonify({"action": "bet", "amount": min_raise})

    pot_odds = to_call / (pot + to_call) if (pot + to_call) > 0 else 0
    if "call" in legal_actions and win_prob >= pot_odds:
        return jsonify({"action": "call"})

    return jsonify({"action": "check" if "check" in legal_actions else "fold"})


# @app.get("/health")
# def health():
#     return jsonify({"status": "ok"})


@app.post("/stonks")
def solve_stonks():
    try:
        datas = request.get_json(force=True, silent=True)
        if datas is None:
            return jsonify({"error": "Invalid JSON input"}), 400
        if isinstance(datas, dict):
            datas = [datas]

        best_paths = []

        for data in datas:
            energy = data["energy"]
            capital = data["capital"]
            timeline = data["timeline"]

            # Flatten timeline into fast flat dictionaries
            prices = {}
            initial_avail = {}
            all_years = []

            for y_str, stocks in timeline.items():
                y_int = int(y_str)
                all_years.append(y_int)
                for s_name, s_info in stocks.items():
                    prices[(y_str, s_name)] = s_info["price"]
                    initial_avail[(y_str, s_name)] = s_info["qty"]

            best_profit = -1
            best_path = []
            memo = {}

            # Stack State: (year, curr_energy, curr_cap, holdings, avail, path, phase)
            stack = [(2037, energy, capital, {}, initial_avail, [], 0)]

            while stack:
                year, curr_energy, curr_cap, holdings, avail, path, phase = stack.pop()

                # Prune: verify enough energy exists to return to 2037
                if curr_energy < abs(year - 2037):
                    continue

                # Memoization Pruning
                frozen_holdings = tuple(sorted((k, v) for k, v in holdings.items() if v > 0))
                frozen_avail = tuple(sorted(avail.items()))
                state_key = (year, curr_energy, phase, frozen_holdings, frozen_avail)

                if memo.get(state_key, -1) >= curr_cap:
                    continue
                memo[state_key] = curr_cap

                # Record maximum profit achieved at origin year 2037
                if year == 2037:
                    profit = curr_cap - capital
                    if profit > best_profit:
                        best_profit = profit
                        best_path = list(path)

                y_str = str(year)

                # PHASE 2: Jump to another timeline year
                if phase == 2:
                    for target_year in all_years:
                        if target_year == year:
                            continue
                        cost = abs(year - target_year)
                        if curr_energy >= cost + abs(target_year - 2037):
                            stack.append((
                                target_year,
                                curr_energy - cost,
                                curr_cap,
                                holdings,
                                avail,
                                path + [f"j-{year}-{target_year}"],
                                0
                            ))

                # PHASE 1: Buy available stocks
                elif phase == 1:
                    # Next transition step: move to Jump phase
                    stack.append((year, curr_energy, curr_cap, holdings, avail, path, 2))

                    for (s_year, stock), rem_qty in avail.items():
                        if s_year == y_str and rem_qty > 0:
                            price = prices.get((y_str, stock), 0)
                            if price > 0:
                                max_affordable = min(rem_qty, curr_cap // price)
                                if max_affordable > 0:
                                    cost = max_affordable * price

                                    new_avail = avail.copy()
                                    new_avail[(y_str, stock)] -= max_affordable

                                    new_holdings = holdings.copy()
                                    new_holdings[stock] = new_holdings.get(stock, 0) + max_affordable

                                    stack.append((
                                        year,
                                        curr_energy,
                                        curr_cap - cost,
                                        new_holdings,
                                        new_avail,
                                        path + [f"b-{stock}-{max_affordable}"],
                                        1
                                    ))

                # PHASE 0: Sell carried holdings
                elif phase == 0:
                    sold_any = False
                    for stock, qty in list(holdings.items()):
                        if qty > 0 and (y_str, stock) in prices:
                            price = prices[(y_str, stock)]
                            earned = qty * price

                            new_holdings = holdings.copy()
                            new_holdings[stock] = 0
                            sold_any = True

                            stack.append((
                                year,
                                curr_energy,
                                curr_cap + earned,
                                new_holdings,
                                avail,
                                path + [f"s-{stock}-{qty}"],
                                0
                            ))

                    if not sold_any:
                        # Move to Buy phase
                        stack.append((year, curr_energy, curr_cap, holdings, avail, path, 1))

            best_paths.append(best_path)

        return jsonify(best_paths), 200

    except Exception as e:
        print("Error processing request:", traceback.format_exc())
        return jsonify({"error": str(e)}), 500
