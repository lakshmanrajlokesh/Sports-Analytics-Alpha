
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import pandas as pd
import numpy as np
import joblib
import json
import os

app = FastAPI(title="IPL Match Decision Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Load models & data ──
BASE = os.path.dirname(os.path.abspath(__file__))

win_model    = joblib.load(os.path.join(BASE, "models/win_probability_model.pkl"))
bat_model    = joblib.load(os.path.join(BASE, "models/bat_impact_model.pkl"))
bowl_model   = joblib.load(os.path.join(BASE, "models/bowl_impact_model.pkl"))
master_df    = pd.read_csv(os.path.join(BASE, "outputs/master_players.csv"))
matchup_df   = pd.read_csv(os.path.join(BASE, "outputs/bowler_matchup_matrix.csv"))

# ── In-memory match state ──
match_state = {}

# ── Pydantic schemas ──
class SetupRequest(BaseModel):
    team_a: str
    team_b: str
    team_a_players: List[str]  # 11 names
    team_b_players: List[str]  # 11 names
    batting_first: str         # "A" or "B"
    venue: str

class OverInput(BaseModel):
    over_number: int
    balls: List[str]           # e.g. ["0","4","1","W","0","6"]
    bowler: str
    extra: Optional[str] = None

# ── Helpers ──
def parse_ball(v: str):
    s = str(v).upper().strip()
    runs = 0
    wicket = False
    legal = True
    if s in ("WD", "LB"):  runs = 1; legal = False
    elif s == "NB":         runs = 1; legal = False
    elif s == "WB":         runs = 1; wicket = True
    elif s == "W":          wicket = True
    elif s.isdigit():       runs = int(s)
    return runs, wicket, legal

def win_probability(over, runs, wickets, target, max_overs=20):
    balls_left = (max_overs - over) * 6
    crr  = runs / over if over > 0 else 0
    rrr  = ((target - runs) / balls_left * 6) if balls_left > 0 else 36.0
    X = pd.DataFrame([{
        "over_number":     over,
        "runs_scored":     runs,
        "wickets_lost":    wickets,
        "crr":             round(crr, 2),
        "rrr":             round(min(rrr, 36.0), 2),
        "balls_remaining": balls_left,
        "pressure":        round(rrr - crr, 2),
        "phase":           0 if over <= 6 else (2 if over > 15 else 1),
        "top_wickets":     min(wickets, 3),
        "runs_needed":     max(0, target - runs)
    }])
    return round(float(win_model.predict_proba(X)[0][1]) * 100, 1)

def recommend_bowlers(fielding_players, current_over, overs_bowled, last_bowler, top_n=3):
    phase_col = "pp_score" if current_over <= 6 else ("death_score" if current_over > 15 else "middle_score")
    recs = []
    for name in fielding_players:
        if name == last_bowler: continue
        if overs_bowled.get(name, 0) >= 4: continue
        row = matchup_df[matchup_df["player"] == name]
        score = float(row.iloc[0][phase_col]) if not row.empty else 0.5
        econ  = float(row.iloc[0]["economy"]) if not row.empty else 8.0
        wkts  = float(row.iloc[0]["total_wickets"]) if not row.empty else 0
        recs.append({"name": name, "score": round(score, 3), "economy": round(econ, 2), "wickets": int(wkts)})
    return sorted(recs, key=lambda x: -x["score"])[:top_n]

def recommend_batters(batting_players, next_idx, runs, balls, top_n=2):
    crr = (runs / (balls / 6)) if balls > 0 else 0
    need_acceleration = crr < 8 and balls > 60
    remaining = batting_players[next_idx:]
    scored = []
    for name in remaining:
        row = master_df[master_df["player"] == name]
        if row.empty:
            scored.append({"name": name, "score": 0, "bat_avg": 0, "bat_sr": 0})
            continue
        r = row.iloc[0]
        sr  = float(r.get("bat_sr", 120))
        avg = float(r.get("bat_avg", 25))
        score = sr if need_acceleration else avg
        scored.append({"name": name, "score": score, "bat_avg": round(avg, 1), "bat_sr": round(sr, 1)})
    return sorted(scored, key=lambda x: -x["score"])[:top_n]

def get_alerts(crr, wickets, current_over, balls_remaining):
    alerts = []
    if crr > 12.5:
        alerts.append({"type": "red",    "title": "HIGH RUN RATE",     "body": f"CRR {crr:.1f} — defensive field needed."})
    if wickets >= 5:
        alerts.append({"type": "red",    "title": "BATTING COLLAPSE",  "body": f"{wickets} wickets down — lower order incoming."})
    if current_over > 15:
        alerts.append({"type": "yellow", "title": "DEATH OVERS",       "body": "Yorkers and slower balls recommended."})
    if current_over <= 6:
        alerts.append({"type": "green",  "title": "POWERPLAY ACTIVE",  "body": "Only 2 fielders outside circle."})
    if crr < 6 and current_over > 10:
        alerts.append({"type": "yellow", "title": "ACCELERATION NEEDED", "body": "CRR below par — send high-SR batter."})
    return alerts

# ── Endpoints ──

@app.post("/match/setup")
def setup_match(req: SetupRequest):
    global match_state
    batting_team = req.team_a_players if req.batting_first == "A" else req.team_b_players
    fielding_team = req.team_b_players if req.batting_first == "A" else req.team_a_players
    match_state = {
        "team_a": req.team_a, "team_b": req.team_b,
        "team_a_players": req.team_a_players,
        "team_b_players": req.team_b_players,
        "batting_first": req.batting_first,
        "batting_team": batting_team,
        "fielding_team": fielding_team,
        "venue": req.venue,
        "runs": 0, "wickets": 0, "balls": 0,
        "current_over": 0, "max_overs": 20,
        "striker_idx": 0, "non_striker_idx": 1, "next_batter_idx": 2,
        "over_history": [],
        "overs_bowled": {},
        "last_bowler": None,
        "target": None
    }
    return {"status": "ok", "message": "Match set up successfully", "state": match_state}


@app.post("/match/over")
def submit_over(inp: OverInput):
    global match_state
    if not match_state:
        raise HTTPException(status_code=400, detail="Match not set up. Call /match/setup first.")

    over_runs = 0; over_wkts = 0; legal_balls = 0
    for ball in inp.balls:
        runs, wicket, legal = parse_ball(ball)
        over_runs += runs
        if wicket: over_wkts += 1; match_state["wickets"] += 1
        if legal:  legal_balls += 1; match_state["balls"] += 1
        match_state["runs"] += runs

    # Bowling figures
    b = inp.bowler
    if b not in match_state["overs_bowled"]: match_state["overs_bowled"][b] = 0
    match_state["overs_bowled"][b] += 1
    match_state["last_bowler"] = b
    match_state["current_over"] += 1

    # Update batter positions
    odd = over_runs % 2 == 1
    if odd:
        match_state["striker_idx"], match_state["non_striker_idx"] = (
            match_state["non_striker_idx"], match_state["striker_idx"])
    for _ in range(over_wkts):
        if match_state["next_batter_idx"] < 11:
            match_state["striker_idx"] = match_state["next_batter_idx"]
            match_state["next_batter_idx"] += 1
    s_idx, ns_idx = match_state["striker_idx"], match_state["non_striker_idx"]
    match_state["striker_idx"], match_state["non_striker_idx"] = ns_idx, s_idx

    match_state["over_history"].append({
        "over": inp.over_number, "balls": inp.balls,
        "runs": over_runs, "wickets": over_wkts, "bowler": b
    })

    return get_match_state_response()


@app.get("/match/state")
def get_state():
    if not match_state:
        raise HTTPException(status_code=400, detail="No match in progress.")
    return get_match_state_response()


def get_match_state_response():
    ms = match_state
    over = ms["current_over"]
    runs = ms["runs"]
    wickets = ms["wickets"]
    balls = ms["balls"]
    target = ms["target"] or 160  # default if 1st innings

    crr  = round(runs / over, 2) if over > 0 else 0
    rrr  = round(((target - runs) / max(1, (ms["max_overs"] - over) * 6)) * 6, 2)
    proj = round(crr * ms["max_overs"]) if over > 0 else 0
    balls_left = (ms["max_overs"] - over) * 6

    wp  = win_probability(over, runs, wickets, target)
    bowler_recs  = recommend_bowlers(ms["fielding_team"], over+1, ms["overs_bowled"], ms["last_bowler"])
    batter_recs  = recommend_batters(ms["batting_team"], ms["next_batter_idx"], runs, balls)
    alerts       = get_alerts(crr, wickets, over, balls_left)

    bt = ms["batting_team"]
    return {
        "score":            f"{runs}/{wickets}",
        "overs":            f"{over}.0",
        "crr":              crr, "rrr": rrr, "projected": proj,
        "win_probability":  wp,
        "striker":          bt[ms["striker_idx"]] if ms["striker_idx"] < len(bt) else "—",
        "non_striker":      bt[ms["non_striker_idx"]] if ms["non_striker_idx"] < len(bt) else "—",
        "over_history":     ms["over_history"],
        "overs_bowled":     ms["overs_bowled"],
        "bowler_recommendations":  bowler_recs,
        "batter_recommendations":  batter_recs,
        "alerts":           alerts
    }


@app.get("/players/profile/{player_name}")
def player_profile(player_name: str):
    row = master_df[master_df["player"].str.lower() == player_name.lower()]
    if row.empty:
        raise HTTPException(status_code=404, detail=f"Player '{player_name}' not found.")
    r = row.iloc[0].fillna(0).to_dict()
    return {"player": player_name, "profile": r}


@app.get("/players/top-batters")
def top_batters(n: int = 10):
    top = master_df.nlargest(n, "bat_impact")[["player","role","bat_impact","bat_avg","bat_sr","total_runs"]]
    return top.fillna(0).to_dict(orient="records")


@app.get("/players/top-bowlers")
def top_bowlers(n: int = 10):
    top = master_df.nlargest(n, "bowl_impact")[["player","role","bowl_impact","total_wickets","economy","best_economy"]]
    return top.fillna(0).to_dict(orient="records")


# ── WebSocket for live dashboard ──
class ConnectionManager:
    def __init__(self): self.connections = []
    async def connect(self, ws: WebSocket):
        await ws.accept(); self.connections.append(ws)
    def disconnect(self, ws: WebSocket):
        self.connections.remove(ws)
    async def broadcast(self, data: dict):
        for ws in self.connections:
            try: await ws.send_text(json.dumps(data))
            except: pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
