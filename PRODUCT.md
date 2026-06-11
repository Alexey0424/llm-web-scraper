# Discovery Crawler

A back-office data tool: give it a URL + a plain-language request, and a local LLM
crawls the site and returns the records you described as an Excel/CSV.

- **Register:** product (design serves the task; not a marketing surface).
- **Who uses it:** an operator at a desk, in daytime office light, doing focused data work.
- **Primary task:** type a URL + a prompt naming the fields → run a discovery
  crawl → review the discovered records → export to Excel.
- **Mental model / nouns:** crawls, the plan, the timeline, discovered records, history.

## Design principles

- **Two designed themes, ember orange brand** (night default; toggle in the topbar). Night "ember workshop": greyish-black charcoal surfaces, ember-orange primary fill carrying near-black ink, brighter orange signal for links/live state. Day "terracotta on cream": cream canvas, warm-white cards, burnt-orange primary with white ink, warm brown text. Semantic status stays green/yellow/red (warn is yellow so it never reads as brand orange). All tokens in OKLCH; WCAG AA contrast everywhere.
- **One sans family** (system stack), fixed rem scale, mono for data (email, phone, URL).
- **Density without noise.** Tables can run dense; chrome stays quiet.
- **Every interactive element has all states** (default, hover, focus, active, disabled, loading).
- **Live interpretability.** Runs show a pipeline stage stepper, the Plan, a per-call timeline, progress/ETA and a records sparkline; the **Flow** tab maps the pipeline; empty results explain *why* (e.g. anti-bot block page, timeouts).
- **Empty states teach** the next action. Motion only conveys state, respects reduced-motion.

## Two tabs

- **Crawler** — the discovery + extraction workflow (the product).
- **Flow** — a diagram of the implementation, kept in sync with the code.
