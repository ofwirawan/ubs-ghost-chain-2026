from flask import Flask, request, jsonify
from routes import app

@app.route("/health", methods=["POST"])
def health_check():
    return {"status": "ok"}

@app.route("/move", methods=["POST"])
async def move():
    data = request.get_json(silent=True) or {}

    # Extract state variables
    legal_actions = data.get("legal_actions", ["check", "fold"])
    your_number = data.get("your_number")
    community_number = data.get("community_number")
    round_phase = data.get("round")
    to_call = data.get("to_call", 0)
    min_raise = data.get("min_raise_to")
    max_raise = data.get("max_raise_to")

    has_pair = (community_number is not None) and (your_number == community_number)

    # 1. Monster Hand: We made a pair post-reveal
    if has_pair and "raise" in legal_actions and min_raise is not None:
        # Aggressive raise sizing
        raise_amount = min(min_raise + 10, max_raise)
        return {"action": "raise", "amount": raise_amount}

    # 2. Strong Hands / Calls
    if has_pair or (your_number >= 9):
        if "call" in legal_actions:
            return {"action": "call"}
        if "check" in legal_actions:
            return {"action": "check"}

    # 3. Moderate Hands (6-9): Free play or small call
    if your_number >= 5:
        if "check" in legal_actions:
            return {"action": "check"}
        if "call" in legal_actions and to_call <= 10:
            return {"action": "call"}

    # 4. Weak Hands (1-5): Prefer check, fold if facing significant bets
    if "check" in legal_actions:
        return {"action": "check"}

    return {"action": "fold"}
