from flask import Flask, jsonify, request

from routes import app


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
