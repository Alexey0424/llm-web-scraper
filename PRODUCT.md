# Discovery Crawler

A back-office data tool: give it a URL + a plain-language request, and a local LLM
crawls the site and returns the records you described as an Excel/CSV.

- **Register:** product (design serves the task; not a marketing surface).
- **Who uses it:** an operator at a desk, in daytime office light, doing focused data work.
- **Primary task:** type a URL + a prompt naming the fields → run a compliant
  discovery crawl → review the discovered records → export to Excel.
- **Mental model / nouns:** crawls, the plan, the timeline, discovered records, history.

## Design principles

- **Dark, restrained graphite theme** (default; light theme via toggle). Neutral true-gray surfaces (`#0c0d0e` → `#26282c`), high-contrast light ink, a monochrome accent (light "key" surface with dark ink) for primary actions and current state. Color is reserved for semantic status only (green/amber/red). WCAG AA contrast everywhere.
- **One sans family** (system stack), fixed rem scale, mono for data (email, phone, URL).
- **Density without noise.** Tables can run dense; chrome stays quiet.
- **Every interactive element has all states** (default, hover, focus, active, disabled, loading).
- **Live interpretability.** Runs show their Plan + a per-call timeline; the **Flow** tab maps the pipeline; empty results explain *why* (e.g. blocked by robots/anti-bot).
- **Empty states teach** the next action. Motion only conveys state, respects reduced-motion.

## Two tabs

- **Crawler** — the discovery + extraction workflow (the product).
- **Flow** — a diagram of the implementation, kept in sync with the code.
