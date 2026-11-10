"""Forward-only, per-user reminder state; allocations are strategy targets only."""
from copy import deepcopy
from decimal import Decimal


def initial_state(observation):
    return {"as_of": observation["date"], "streak": None if observation["below"] else 0,
            "cycle": None, "recent_rows": [observation], "events": []}


def advance(state, observation, next_day):
    state = deepcopy(state)
    day = observation["date"]
    if day <= state["as_of"]:
        return state
    state["as_of"] = day
    state["events"] = []
    state["recent_rows"] = (state.get("recent_rows", []) + [observation])[-3:]
    cycle = state.get("cycle")
    if cycle and cycle.get("pending") and day >= cycle["pending"]["date"]:
        cycle["stage"] = cycle["pending"]["stage"]
        cycle["pending"] = None
        if cycle["stage"] == 2:
            cycle = None
            state["cycle"] = None
    if not observation["below"]:
        state["streak"] = 0
    elif state["streak"] is not None:
        state["streak"] += 1
    streak = state["streak"]
    common = {"date": day, "next_day": next_day, "rows": state["recent_rows"], "source": observation.get("source")}
    if streak == 2:
        state["events"].append({**common, "kind": "warning", "level": 2, "streak": 2})
    if streak == 3:
        state["cycle"] = {"id": day, "effective_date": next_day, "low": None, "low_date": None,
                          "stage": -1, "pending": {"date": next_day, "stage": 0}}
        state["events"].append({**common, "kind": "reduce", "level": 3, "streak": 3})
        return state  # New reduction supersedes refill observations for this close.
    if cycle and cycle["stage"] >= 0 and day >= cycle["effective_date"]:
        close = observation["close"]
        if cycle["low"] is None or close < cycle["low"]:
            cycle["low"], cycle["low_date"] = close, day
        pending_stage = (cycle.get("pending") or {}).get("stage", cycle["stage"])
        close_d, low_d = Decimal(str(close)), Decimal(str(cycle["low"]))
        target = (2 if close_d >= low_d * Decimal("1.045") else
                  1 if close_d >= low_d * Decimal("1.03") else 0)
        if target > pending_stage:
            state["events"].append({**common, "kind": "refill", "level": 45 if target == 2 else 30,
                "threshold": 4.5 if target == 2 else 3.0, "target_pct": 100 if target == 2 else 75,
                "crossed_both": target == 2 and pending_stage == 0,
                "low": cycle["low"], "low_date": cycle["low_date"], "close": close,
                "rebound_pct": float((close_d / low_d - 1) * 100), "cycle_id": cycle["id"],
                "tracking_since": cycle["effective_date"]})
            cycle["pending"] = {"date": next_day, "stage": target}
    return state
