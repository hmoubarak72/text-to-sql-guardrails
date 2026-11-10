"""Streamlit front end for the Text-to-SQL API."""
from __future__ import annotations

import os
import uuid

import httpx
import pandas as pd
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
API_TOKEN = os.getenv("API_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}
EXAMPLES = [
    "How many orders were placed by customers in Europe?",
    "Which product category had the highest gross revenue in 2025?",
    "What was our revenue in 2025?",
    "Delete all cancelled orders.",
    "How many orders were placed in 2025?",
    "How many orders included at least one Laptops product?",
    "What was the total shipping cost charged in 2025?",
    "What is the current inventory level for the Trail Tent 2P?",
]
DECISION_STYLE = {
    "answer": ("success", "Answer"), "answer_with_warnings": ("warning", "Answer with warnings"),
    "clarify": ("info", "Clarification needed"), "unsupported": ("info", "Cannot be answered from this database"),
    "abstain": ("warning", "Abstained - results withheld"), "blocked": ("error", "Blocked by guardrails"),
    "error": ("error", "Error"),
}

st.set_page_config(page_title="Text-to-SQL with Guardrails", layout="wide")
if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex[:16]
st.session_state.setdefault("last", None)
st.session_state.setdefault("question", EXAMPLES[0])


def api(method: str, path: str, **kw):
    try:
        r = httpx.request(method, f"{API_URL}{path}", headers=HEADERS, timeout=120, **kw)
        return r.status_code, r.json()
    except Exception as exc:
        return 0, {"detail": f"API unreachable: {type(exc).__name__}"}


def ask(question: str, sql: str | None = None):
    payload = {"question": question, "session_id": st.session_state.session_id}
    if sql:
        payload["sql"] = sql
    code, data = api("POST", "/v1/query", json=payload)
    st.session_state.last = data if code == 200 else {"decision": "error", "decision_reason": data.get("detail")}


# ------------------------------------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Session")
    st.caption(f"Session id: `{st.session_state.session_id}`")
    code, ready = api("GET", "/readyz")
    if code == 200:
        live = "live model" if ready.get("provider_is_live") else "MOCK provider (scripted responses)"
        st.success(f"API ready - {ready.get('provider')} ({live})")
    else:
        st.error(f"API not ready: {ready.get('reason') or ready.get('detail')}")
    st.header("History")
    code, hist = api("GET", "/v1/history", params={"session_id": st.session_state.session_id, "limit": 20})
    for item in (hist.get("items", []) if code == 200 else []):
        conf = f"{item['confidence']:.2f}" if item.get("confidence") is not None else "-"
        st.markdown(f"- **{item['decision']}** ({conf}) - {item['question'][:70]}")
    with st.expander("Schema"):
        code, sch = api("GET", "/v1/schema")
        if code == 200:
            st.caption(f"Data span {sch['data_start']} .. {sch['data_end']}")
            for t in sch["tables"]:
                cols = ", ".join(c["name"] for c in t["columns"])
                restricted = f" | restricted: {', '.join(t['restricted_columns'])}" if t["restricted_columns"] else ""
                st.markdown(f"**{t['name']}** (~{t['row_estimate']} rows): {cols}{restricted}")

# ------------------------------------------------------------------------------------------------ main
st.title("Text-to-SQL with Guardrails and Hallucination Detection")
st.caption("Questions are translated to SQL, validated by an AST policy engine and EXPLAIN preflight, executed "
           "read-only under a SELECT-only role, then checked for hallucinations. Confidence is a heuristic, "
           "uncalibrated score.")
pick = st.selectbox("Example questions", ["(choose an example)"] + EXAMPLES)
if pick != "(choose an example)":
    st.session_state.question = pick
question = st.text_area("Ask a question about the shop database", key="question", height=80)
if st.button("Ask", type="primary") and question.strip():
    with st.spinner("Generating, validating and executing..."):
        ask(question.strip())

r = st.session_state.last
if r:
    kind, label = DECISION_STYLE.get(r.get("decision"), ("error", r.get("decision")))
    getattr(st, kind)(f"**{label}** - {r.get('decision_reason', '')}")

    if r.get("decision") == "clarify" and r.get("clarification"):
        st.subheader(r["clarification"].get("question", "Please clarify"))
        for i, interp in enumerate(r["clarification"].get("interpretations", [])):
            c1, c2 = st.columns([1, 4])
            if c1.button(interp["label"], key=f"interp{i}"):
                ask(f"{r['question'].rstrip()} ({interp['label']})")
                st.rerun()
            c2.write(interp.get("description", ""))
            if interp.get("example_sql"):
                c2.code(interp["example_sql"], language="sql")
    if r.get("decision") == "unsupported":
        st.info(r.get("unsupported_reason") or "")

    if r.get("guardrail") and r["guardrail"].get("violations") or (r.get("plan") or {}).get("violations"):
        st.subheader("Guardrail decision")
        vio = (r.get("guardrail") or {}).get("violations", []) + (r.get("plan") or {}).get("violations", [])
        st.table(pd.DataFrame(vio))
        if r.get("original_sql"):
            st.caption("Rejected SQL (never executed):")
            st.code(r["original_sql"], language="sql")

    if r.get("sql"):
        st.subheader("Generated SQL" if r.get("source") == "generated" else "Edited SQL (re-validated)")
        st.code(r["sql"], language="sql")
        if r.get("explanation"):
            st.caption(r["explanation"])
        for n in r.get("notices", []):
            st.caption(f"Note: {n}")
        if r.get("repair_attempts"):
            st.warning(f"Initial SQL referenced an invalid table/column; it was regenerated "
                       f"{r['repair_attempts']} time(s) and fully re-validated.")
        with st.expander("Edit SQL (re-enters the full safety pipeline)"):
            edited = st.text_area("SQL", value=r["sql"], height=180, key=f"edit_{r.get('query_id')}")
            if st.button("Run edited SQL"):
                ask(r["question"], sql=edited)
                st.rerun()

    if r.get("columns") and r.get("rows"):
        st.subheader(f"Results ({r['row_count']} rows{', truncated' if r.get('truncated') else ''}, "
                     f"{r.get('execution_ms')} ms)")
        st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), use_container_width=True)
    elif r.get("decision") == "abstain":
        st.warning(f"The query ran ({r.get('row_count')} rows) but results are withheld because validation "
                   "found likely errors. Edit the SQL or rephrase the question.")

    if r.get("warnings"):
        st.subheader("Warnings")
        for w in r["warnings"]:
            st.markdown(f"- {w}")

    conf = r.get("confidence")
    if conf:
        st.subheader("Confidence")
        c1, c2 = st.columns([1, 3])
        c1.metric("Score (heuristic)", "n/a" if conf["score"] is None else f"{conf['score']:.2f}", conf["label"])
        c2.caption(conf["note"])
        sig = pd.DataFrame(conf["signals"])[["name", "weight", "score", "available", "detail"]]
        st.dataframe(sig, use_container_width=True, hide_index=True)
        if conf.get("unavailable"):
            st.caption("Unavailable checks: " + ", ".join(conf["unavailable"]))
    if r.get("checks"):
        with st.expander("Validation checks (pre- and post-execution)", expanded=False):
            st.dataframe(pd.DataFrame(r["checks"])[["phase", "name", "status", "score", "detail"]],
                         use_container_width=True, hide_index=True)
    if r.get("back_translation"):
        with st.expander("Back-translation (what the SQL actually answers)"):
            st.write(r["back_translation"]["question"])
            for d in r["back_translation"].get("differences", []):
                st.caption(d)
    alt = r.get("alternative")
    if alt:
        with st.expander(f"Multi-query validation: {alt.get('reason')}", expanded=alt.get("match") is False):
            if alt.get("alt_sql"):
                st.code(alt["alt_sql"], language="sql")
            st.write(alt.get("detail", ""))
            if alt.get("match") is False and alt.get("alt_rows"):
                st.caption("Alternative query results (for comparison):")
                st.dataframe(pd.DataFrame(alt["alt_rows"], columns=alt["alt_columns"] or None))

    if r.get("query_id") and r.get("decision") not in {"error"}:
        st.subheader("Was this correct?")
        comment = st.text_input("Optional comment", key=f"c_{r['query_id']}")
        f1, f2, _ = st.columns([1, 1, 6])
        for col, verdict, text in ((f1, "correct", "Correct"), (f2, "incorrect", "Incorrect")):
            if col.button(text, key=f"{verdict}_{r['query_id']}"):
                code, resp = api("POST", "/v1/feedback", json={"query_id": r["query_id"],
                                                               "session_id": st.session_state.session_id,
                                                               "verdict": verdict, "comment": comment or None})
                st.toast("Feedback saved for review" if code == 201 else f"Feedback failed: {resp}")
