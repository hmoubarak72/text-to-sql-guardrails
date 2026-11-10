# Demo script (target 3:30, hard limit 4:00)

Setup: `docker compose up -d` (or the native stack), open http://localhost:8501. The examples below work with `LLM_PROVIDER=mock`; with a live provider, wording and SQL will differ.

| Time | Action (type into the question box) | Say |
|---|---|---|
| 0:00-0:20 | Show the title, the green "API ready" badge, open **Schema** in the sidebar | "Natural-language questions over a real PostgreSQL database. Safety comes first: nothing runs unless it passes a parser-based policy, an EXPLAIN preflight, and a read-only database role. Restricted columns like email and salary are not even visible." |
| 0:20-0:55 | `Which product category had the highest gross revenue in 2025?` | "SQL with syntax highlighting, sortable results, and a confidence breakdown. An independent second query took a different approach and returned the same answer. The score is heuristic, not a calibrated probability, and the UI says so." |
| 0:55-1:30 | `Delete all cancelled orders.` | "The (scripted) model produced a DELETE. It is blocked before execution and the reason is logged. Even if the policy layer missed it, the database role is read-only." Open **Edit SQL** on the previous answer, replace it with `SELECT 1; DROP TABLE shop.orders`, run: "Edited SQL goes through the same pipeline: blocked for multiple statements." |
| 1:30-2:00 | `What was our revenue in 2025?` → click **Gross revenue** | "Revenue is ambiguous here, so instead of guessing the system asks. Choosing an interpretation answers it." |
| 2:00-2:40 | `How many orders were placed in 2025?` | "This is a hallucination: the SQL filters 2024. The concept check flags the year mismatch, confidence is capped, and the answer is marked with warnings instead of being presented as fact." |
| 2:40-3:20 | `How many orders included at least one Laptops product?` | "A subtler error: COUNT over a one-to-many join double counts. The fan-out check warns, and the independent alternative query disagrees, 294 vs 280. Both results are shown so the user can see the discrepancy. The alternative uses EXISTS and is correct." |
| 3:20-3:40 | `What was the total shipping cost charged in 2025?` | "The first SQL referenced a column that doesn't exist. It was rejected, regenerated once, and fully re-validated." |
| 3:40-3:55 | Show history in the sidebar, click **Incorrect** on one result | "Session history and feedback. Feedback goes to a review queue; it never automatically becomes a training example." |
| 3:55 | Close on README metrics | "20 of 20 unsafe queries blocked, zero executed, zero false-positive blocks across 40 correct queries. Live-model accuracy is the next measurement." |
