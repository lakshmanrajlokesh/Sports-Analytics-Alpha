
// ================================================================
// API Integration Layer — replaces hardcoded JS recommendation logic
// Paste this into the dashboard <script> section
// ================================================================

const API_BASE = "http://localhost:8000";
const WS_URL   = "ws://localhost:8000/ws";

let ws = null;

// ── Connect WebSocket ──
function connectWS() {
    ws = new WebSocket(WS_URL);
    ws.onmessage = (evt) => {
        const data = JSON.parse(evt.data);
        applyStateToUI(data);
    };
    ws.onclose = () => setTimeout(connectWS, 2000); // auto-reconnect
    ws.onerror = () => console.warn("WS error — falling back to REST polling");
}

// ── Setup match ──
async function apiSetupMatch(payload) {
    const res = await fetch(`${API_BASE}/match/setup`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}

// ── Submit over ──
async function apiSubmitOver(overNumber, balls, bowler) {
    const res = await fetch(`${API_BASE}/match/over`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({over_number: overNumber, balls, bowler})
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}

// ── Apply API state to dashboard UI ──
function applyStateToUI(state) {
    // Scoreboard
    document.getElementById("score-display").textContent  = state.score;
    document.getElementById("overs-display").textContent  = state.overs + " Ov";
    document.getElementById("crr-display").textContent    = "CRR: " + state.crr;
    document.getElementById("rrr-display").textContent    = state.rrr || "—";
    document.getElementById("projected-display").textContent = state.projected || "—";

    // Win probability
    const wp = state.win_probability;
    document.getElementById("win-prob-fill").style.width = wp + "%";
    document.getElementById("win-prob-pct").textContent  = wp + "%";

    // Batters
    document.getElementById("striker-name").textContent    = state.striker || "—";
    document.getElementById("nonstriker-name").textContent = state.non_striker || "—";

    // Bowler recommendations
    const blist = document.getElementById("bowler-rec-list");
    blist.innerHTML = "";
    (state.bowler_recommendations || []).forEach((b, i) => {
        const card = document.createElement("div");
        card.className = "rec-card" + (i === 0 ? " top-rec" : "");
        card.innerHTML = `
            <div class="rec-card-header">
                <div class="rec-name">${b.name}</div>
                <div class="rec-rank ${i===0?"gold":""}">${i===0?"★ TOP REC":"#"+(i+1)}</div>
            </div>
            <div class="rec-stats-row">
                <div class="rec-stat"><span class="rec-stat-label">Economy</span>
                    <span class="rec-stat-value highlight">${b.economy}</span></div>
                <div class="rec-stat"><span class="rec-stat-label">Wickets</span>
                    <span class="rec-stat-value">${b.wickets}</span></div>
                <div class="rec-stat"><span class="rec-stat-label">Score</span>
                    <span class="rec-stat-value">${b.score}</span></div>
            </div>`;
        blist.appendChild(card);
    });

    // Batter recommendations
    const alist = document.getElementById("batter-rec-list");
    alist.innerHTML = "";
    (state.batter_recommendations || []).forEach((b, i) => {
        const card = document.createElement("div");
        card.className = "rec-card" + (i===0?" top-rec":"");
        card.innerHTML = `
            <div class="rec-card-header">
                <div class="rec-name">${b.name}</div>
                <div class="rec-rank ${i===0?"gold":""}">${i===0?"★ SEND IN":"#2"}</div>
            </div>
            <div class="rec-stats-row">
                <div class="rec-stat"><span class="rec-stat-label">Avg</span>
                    <span class="rec-stat-value highlight">${b.bat_avg}</span></div>
                <div class="rec-stat"><span class="rec-stat-label">SR</span>
                    <span class="rec-stat-value">${b.bat_sr}</span></div>
            </div>`;
        alist.appendChild(card);
    });

    // Alerts
    const abox = document.getElementById("alerts-list");
    abox.innerHTML = "";
    (state.alerts || []).forEach(a => {
        const box = document.createElement("div");
        box.className = `alert-box alert-${a.type}`;
        box.innerHTML = `<div class="alert-title">${a.title}</div>
                         <div class="alert-body">${a.body}</div>`;
        abox.appendChild(box);
    });

    // Over history
    updateOverHistory(state.over_history || []);
}

// ── Modified launchDashboard — calls API ──
async function launchDashboard() {
    const teamAName = document.getElementById("team-a-name").value || "Team A";
    const teamBName = document.getElementById("team-b-name").value || "Team B";
    const tossWon   = document.getElementById("toss-won").value;
    const dec       = document.getElementById("toss-decision").value;

    let battingFirst = "A";
    if ((tossWon === teamBName && dec === "bat") ||
        (tossWon === teamAName && dec === "field")) {
        battingFirst = "B";
    }

    const teamAPlayers = [], teamBPlayers = [];
    for (let i = 0; i < 11; i++) {
        teamAPlayers.push(document.getElementById(`team-a-p${i}`).value);
        teamBPlayers.push(document.getElementById(`team-b-p${i}`).value);
    }

    const payload = {
        team_a: teamAName, team_b: teamBName,
        team_a_players: teamAPlayers,
        team_b_players: teamBPlayers,
        batting_first: battingFirst,
        venue: document.getElementById("venue").value
    };

    try {
        await apiSetupMatch(payload);
        connectWS();
        document.getElementById("setup-screen").style.display = "none";
        document.getElementById("dashboard").style.display    = "flex";
        buildDashboard();
        buildBowlerSelect();
        buildBallInputs();
    } catch (err) {
        alert("API Error: " + err.message + "
Make sure the FastAPI server is running.");
    }
}

// ── Modified submitOver — calls API ──
async function submitOver() {
    const bowler = document.getElementById("bowler-select").value;
    const balls  = [];
    for (let i = 0; i < 7; i++) {
        const v = document.getElementById(`ball-${i}`).value.trim();
        if (v) balls.push(v);
        else if (i < 6) balls.push("0");
    }
    const overNumber = state.currentOver + 1;
    try {
        const result = await apiSubmitOver(overNumber, balls.slice(0,6), bowler);
        applyStateToUI(result);
        state.currentOver++;
        buildBallInputs();
        updateBattingOrder();
        updateMomentumChart();
        const over = state.currentOver + 1;
        document.getElementById("over-input-title").textContent = `Enter Over ${Math.min(over, 20)}`;
        document.getElementById("balls-remaining-display").textContent =
            `${Math.max(0, (20 - state.currentOver) * 6)} balls left`;
    } catch (err) {
        console.error("API submitOver failed:", err);
        alert("API Error — check server is running.");
    }
}
