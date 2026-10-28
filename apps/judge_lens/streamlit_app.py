"""Judge Lens: what the judges saw, what the stats say, and who else might have judged it.

Streamlit in Snowflake (PUBLISHED.JUDGE_LENS), reading only the PUBLISHED Judge Lens
views (DESIGN.md §8). Locally, against DEV:

    .venv/bin/streamlit run apps/judge_lens/streamlit_app.py
"""
import altair as alt
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Judge Lens", layout="wide")

FEATURED = {"13677": "Pimblett vs Gordon (UFC 282)", "13772": "Makhachev vs Volkanovski 1 (UFC 284)",
            "15623": "Du Plessis vs Chimaev (UFC 319)", "13847": "Yan vs Dvalishvili 1", "15839": "Dvalishvili vs Yan 2 (UFC 323)",
            "16431": "Yan vs Dvalishvili 3 (UFC 333)", "15938": "Garbrandt vs Xiao Long (UFC 326)", "13556": "O'Malley vs Yan (UFC 280)"}
GROUPS = {"contrib_strikes_landed": "Strikes landed", "contrib_control": "Control", "contrib_takedowns": "Takedowns",
          "contrib_non_sig_strikes": "Non-sig strikes", "contrib_knockdowns": "Knockdowns", "contrib_sub_attempts": "Sub attempts",
          "contrib_strike_volume": "Strike volume", "contrib_reversals": "Reversals"}


@st.cache_resource
def session():
    try:
        from snowflake.snowpark.context import get_active_session
        return get_active_session()
    except Exception:                       # local: DEV, as dbt's DEV service user
        from pathlib import Path
        from snowflake.snowpark import Session
        return Session.builder.configs({
            "account": "FNMYVFY-FP38730", "user": "DBT_DEV_SVC", "authenticator": "SNOWFLAKE_JWT",
            "private_key_file": str(Path("~/.snowflake/keys/dbt_dev_svc.p8").expanduser()),
            "role": "FIGHTGRADE_TRANSFORMER_DEV", "warehouse": "FIGHTGRADE_WH", "database": "FIGHTGRADE_DEV"}).create()


@st.cache_data(ttl=3600)
def q(sql: str) -> pd.DataFrame:
    df = session().sql(sql).to_pandas()
    df.columns = [c.lower() for c in df.columns]
    return df


def pct(x) -> str:
    return "–" if pd.isna(x) else f"{100 * x:.0f}%"


fights = q("select * from published.pub_judge_lens order by event_date desc")
st.title("Judge Lens")
st.caption(f"What the judges saw, what the round statistics say, and who else might have judged it. "
           f"{len(fights):,} UFC decisions, model {fights['model_version'].iloc[0]}.")
tab_fight, tab_judges, tab_luck, tab_about = st.tabs(["Fight", "Judges", "Decision luck", "About"])

# ---------------------------------------------------------------- fight
with tab_fight:
    fights["label"] = fights["fighter_a"] + " vs " + fights["fighter_b"] + " — " + fights["event_name"] + " (" + fights["event_date"].astype(str) + ")"
    featured = st.selectbox("Featured", ["(search below)", *FEATURED.values()], index=1)
    search = st.text_input("Or search a fighter", "")
    pool = fights[fights["label"].str.contains(search, case=False)] if search else fights
    if search:
        choice = st.selectbox("Fight", pool["label"].tolist())
        f = pool[pool["label"] == choice].iloc[0]
    else:
        did = next((k for k, v in FEATURED.items() if v == featured), "13677")
        f = fights[fights["decision_id"] == did].iloc[0]
    did, a, b = f["decision_id"], f["fighter_a"], f["fighter_b"]
    st.subheader(f"{a} vs {b}")
    st.write(f"**{f['event_name']}**, {f['event_date']} · winner **{f['winner'] or 'draw'}** · judges {f['judges_cards']} "
             f"({a}'s margin)" + (f" · deductions {a} {f['deducted_a']:.0f}, {b} {f['deducted_b']:.0f}" if f["deducted_a"] or f["deducted_b"] else ""))

    sa, sb = a.split()[-1], b.split()[-1]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"This panel: P({(f['winner'] or 'a draw').split()[-1]}, as it went)", pct(f["p_actual_result"]),
              help="How likely the actual result was, given the round statistics and these three judges")
    c2.metric(f"Average panel: {sa} / {sb}", f"{pct(f['average_panel_p_a_wins'])} / {pct(f['average_panel_p_b_wins'])}")
    c3.metric(f"The crowd's card: {sa} / {sb}", f"{pct(f['crowd_card_p_a'])} / {pct(f['crowd_card_p_b'])}")
    c4.metric(f"Random panels: P({sa})", pct(f["random_panels_p_a_median"]),
              f"{pct(f['random_panels_share_favouring_a'])} of panels favour {sa}", delta_color="off",
              help=f"Panels drawn from the {int(f['random_panels_judges'])} judges active in the last three years, weighted by rounds judged")
    types = {f"{a.split()[-1]} unanimous": f["p_a_unanimous"], f"{a.split()[-1]} split": f["p_a_split"], f"{a.split()[-1]} majority": f["p_a_majority"],
             "Draw": f["p_draw"], f"{b.split()[-1]} majority": f["p_b_majority"], f"{b.split()[-1]} split": f["p_b_split"], f"{b.split()[-1]} unanimous": f["p_b_unanimous"]}
    st.caption("This panel's possible decisions: " + " · ".join(f"{k} {pct(v)}" for k, v in types.items() if v >= 0.005))

    r = q(f"select * from published.pub_judge_lens_rounds where decision_id = '{did}' order by round, rater_kind, rater_name")
    st.markdown("#### Round by round")
    st.caption(f"P({a} wins the round). Judges' cells show the call they made; ★ marks a call the average judge gave under 25%.")
    grid = {}
    for (rnd, kind, name), g in r.groupby(["round", "rater_kind", "rater_name"]):
        row = g.iloc[0]
        if kind == "judge":
            call = a.split()[-1] if row["margin"] > 0 else (b.split()[-1] if row["margin"] < 0 else "even")
            star = " ★" if (not pd.isna(row["p_call_average"]) and row["p_call_average"] < 0.25) else ""
            cell = f"{pct(row['p_a_wins'])} → {call}{' 10-8' if abs(row['margin']) >= 2 else ''}{star}"
        elif kind == "crowd":
            grid.setdefault("Fans (actual)", {})[f"R{rnd}"] = pct(row["actual_p_a_wins"])
            cell = pct(row["p_a_wins"])
            name = "Crowd model"
        else:
            cell = pct(row["p_a_wins"])
        grid.setdefault(name, {})[f"R{rnd}"] = cell
    order = [n for n in r[r.rater_kind == "judge"]["rater_name"].unique()] + ["Average judge", "Crowd model", "Fans (actual)"]
    st.dataframe(pd.DataFrame(grid).T.reindex([o for o in order if o in grid]), use_container_width=True)

    st.markdown("#### How the model scores each round")
    st.caption(f"The average judge's score, stat by stat, in log-odds: right of zero is toward {a}, left toward {b}.")
    avg = r[r.rater_kind == "average"].sort_values("round")
    long = avg.melt(id_vars=["round"], value_vars=list(GROUPS), var_name="group", value_name="push")
    long["group"] = long["group"].map(GROUPS)
    long["round"] = "R" + long["round"].astype(str)
    chart = alt.Chart(long[long["push"].abs() > 0.005]).mark_bar().encode(
        y=alt.Y("round:N", title=None), x=alt.X("sum(push):Q", title=f"← {b}    log-odds    {a} →"),
        color=alt.Color("group:N", title=None), tooltip=["round", "group", alt.Tooltip("push:Q", format="+.2f")])
    st.altair_chart(chart, use_container_width=True)
    t = avg[["round", *GROUPS, "score_total", "p_a_wins", "p_10_8_plus"]].rename(columns={**GROUPS, "score_total": "Total", "p_a_wins": f"P({a.split()[-1]} wins)", "p_10_8_plus": "P(10-8 either way)"})
    st.dataframe(t.set_index("round").style.format({c: "{:+.2f}" for c in [*GROUPS.values(), "Total"]} | {f"P({a.split()[-1]} wins)": "{:.0%}", "P(10-8 either way)": "{:.0%}"}),
                 use_container_width=True)

    st.markdown("#### The cards each judge was likely to give")
    cards = q(f"select * from published.pub_judge_lens_cards where decision_id = '{did}' order by rater_key")
    st.dataframe(pd.DataFrame({
        "Judge": cards["rater_name"],
        f"P(card for {a.split()[-1]})": cards["p_card_a"].map(pct),
        "Most likely": [f"{m:+d} ({pct(p)})" for m, p in zip(cards["likely_margin_1"], cards["likely_margin_1_p"])],
        "Next": [f"{m:+d} ({pct(p)}), {n:+d} ({pct(q_)})" for m, p, n, q_ in zip(cards["likely_margin_2"], cards["likely_margin_2_p"], cards["likely_margin_3"], cards["likely_margin_3_p"])],
        "Actual": cards["actual_card_margin"].map(lambda x: f"{x:+d}")}).set_index("Judge"), use_container_width=True)
    st.caption(f"Margins are {a}'s points less {b}'s, before deductions.")

    st.markdown("#### Swap a judge")
    sw = q(f"select * from published.pub_judge_lens_swaps where decision_id = '{did}'")
    s1, s2 = st.columns(2)
    seat = s1.selectbox("Seat", sw["seat_rater_name"].unique())
    opts = sw[sw["seat_rater_name"] == seat].sort_values("p_a_wins")
    repl = s2.selectbox("Replace with", opts["replacement_name"].tolist())
    row = opts[opts["replacement_name"] == repl].iloc[0]
    st.write(f"With **{repl}** in {seat}'s seat: {a} wins **{pct(row['p_a_wins'])}**, {b} **{pct(row['p_b_wins'])}**, draw {pct(row['p_draw'])} "
             f"(this panel: {pct(f['p_a_wins'])} / {pct(f['p_b_wins'])}).")
    st.caption(f"Across every replacement for {seat}: {a} wins {pct(opts['p_a_wins'].min())} – {pct(opts['p_a_wins'].max())}.")

# --------------------------------------------------------------- judges
with tab_judges:
    prof = q("select * from published.pub_judge_profile")
    named = prof[prof["named"] == True].sort_values("rounds", ascending=False)
    ref = prof[prof["rater_id"].isin(["average", "crowd"])]
    st.markdown("#### What each judge rewards")
    st.caption("One fixed round: A is out-struck 20–25 and holds 0–4 minutes more control (with a takedown). "
               "Named judges: 300+ scored rounds since 2010. Styles are shrunk toward the average judge.")
    curve = pd.concat([named, ref]).melt(id_vars=["rater_name"], value_vars=[f"p_a_wins_ctrl_{m}min" for m in range(5)], var_name="ctrl", value_name="p")
    curve["control lead (min)"] = curve["ctrl"].str.extract(r"(\d)").astype(int)
    pick = st.multiselect("Judges", named["rater_name"].tolist(), default=named["rater_name"].tolist()[:4])
    shown = curve[curve["rater_name"].isin([*pick, "Average judge", "Crowd"])]
    st.altair_chart(alt.Chart(shown).mark_line(point=True).encode(
        x="control lead (min):O", y=alt.Y("p:Q", title="P(A wins the round)", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0.2, 1])),
        color=alt.Color("rater_name:N", title=None), tooltip=["rater_name", alt.Tooltip("p:Q", format=".0%")]), use_container_width=True)
    st.markdown("#### 10-8s, and how often they stand alone")
    tbl = pd.concat([ref, named])[["rater_name", "rounds", "first_year", "last_year", "rate_10_8", "p_10_8_dominant", "p_10_8_grind",
                                   "rate_lone_dissent", "mean_p_call", "style_1", "style_2"]]
    st.dataframe(tbl.rename(columns={"rater_name": "Judge", "rate_10_8": "10-8 rate", "p_10_8_dominant": "P(10-8) dominant round",
                                     "p_10_8_grind": "P(10-8) control grind", "rate_lone_dissent": "Lone dissents",
                                     "mean_p_call": "Mean P(own call)", "style_1": "Style: output", "style_2": "Style: control"})
                 .set_index("Judge").style.format({"10-8 rate": "{:.1%}", "P(10-8) dominant round": "{:.0%}", "P(10-8) control grind": "{:.0%}",
                                                   "Lone dissents": "{:.1%}", "Mean P(own call)": "{:.0%}", "Style: output": "{:+.2f}",
                                                   "Style: control": "{:+.2f}", "rounds": "{:.0f}", "first_year": "{:.0f}", "last_year": "{:.0f}"}, na_rep=""),
                 use_container_width=True)
    st.altair_chart(alt.Chart(named).mark_circle(size=120).encode(
        x=alt.X("style_1:Q", title="Style: output (attempts)"), y=alt.Y("style_2:Q", title="Style: control"),
        tooltip=["rater_name", "rounds"]) + alt.Chart(named).mark_text(dy=-10).encode(x="style_1:Q", y="style_2:Q", text="rater_name"),
        use_container_width=True)

# ---------------------------------------------------------------- luck
with tab_luck:
    luck = q("select * from published.pub_fighter_decision_luck where decisions >= 8")
    st.markdown("#### Decision luck")
    st.caption("Decisions won, against what an average panel would have given on the same round statistics (fighters with 8+ decisions).")
    l1, l2 = st.columns(2)
    fmt = lambda d: d[["fighter_name", "decisions", "won", "expected", "luck"]].rename(columns={"fighter_name": "Fighter"}).set_index("Fighter").style.format({"expected": "{:.1f}", "luck": "{:+.1f}", "won": "{:.0f}"})
    l1.markdown("**Luckiest**"); l1.dataframe(fmt(luck.nlargest(15, "luck")), use_container_width=True)
    l2.markdown("**Unluckiest**"); l2.dataframe(fmt(luck.nsmallest(15, "luck")), use_container_width=True)
    st.markdown("#### The decisions the stats disagree with most")
    st.caption("Draws aside: any exact draw is unlikely, so they would fill the list.")
    rob = fights[fights["result"] != "draw"].nsmallest(15, "p_actual_result")[["event_date", "event_name", "fighter_a", "fighter_b", "winner", "judges_cards", "p_actual_result"]]
    st.dataframe(rob.rename(columns={"p_actual_result": "P(this result)"}).set_index("event_date").style.format({"P(this result)": "{:.0%}"}), use_container_width=True)

# ---------------------------------------------------------------- about
with tab_about:
    st.markdown("""
**What this is.** A model of how UFC judges score rounds from the round statistics (UFC Stats), trained on every
judge's round scores since 2010 (MMADecisions). Each judge gets a small style vector, shrunk toward the
average judge, so judges with few rounds look like the average judge by construction.

**How fights are scored.** Each round's seven possible scores (10-9, 10-8, 10-10, ...) per judge, with real
deductions applied afterwards. Judges share a round effect and a fight effect — what they all see that the
stats miss — so panels agree as much as real ones do.

**Limits.** Correlational, not causal: judges see damage, octagon control and momentum the stats do not, and
they anchor on each other. A low "P(this result)" means the scoring disagrees with the stats, not with the truth.
These are in-sample: the model trained on these fights. Its out-of-sample record is the prospective log
(ML.JUDGE_LENS_PREDICTIONS): every new card scored by a version that never saw it.
""")
