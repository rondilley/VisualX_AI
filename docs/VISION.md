# VisualX_AI — Vision

**Status:** Draft v0.2. Strategic north star, with the changes from the Phase 6 multi-AI review.

**Author:** Ron Dilley

**Last updated:** 2026-11-03

**License:** GPL-3.0

**Companion documents:** `REQUIREMENTS.md` (engineering scope), `SUCCESS_CRITERIA.md` (measurable results), `ARCHITECTURE.md` (design), `2026-11-02_X_Network_Analysis_Research.md` (evidence), `../.ai-reviews/20261102-145404/synthesis.md` (review results)

This document sets the direction of the project: why VisualX_AI is necessary, who has an interest in it, which bets it makes, and what it does not do. Argue about direction here. Use `REQUIREMENTS.md` and `ARCHITECTURE.md` to argue about the scope and the design.

---

## 1. The vision in one paragraph

By mid-2027, the owner can run one command and get a private, interactive map of their own X network. The map shows who interacts with the owner and how (reply, quote, mention, repost). It shows the groups that these accounts make and the accounts that connect those groups. It also shows which accounts have spam, automation, or impersonation signals, with the cause of each signal. The data comes from sources that obey the X terms, and the tool shows the cost of each paid call before the call. The output is one HTML file that opens offline, shows 5,000 to 10,000 nodes, and works without a server.

<!-- AI review 20261102-145404: openai, claude — "map any other account" removed (policy risk, F-15) -->

---

## 2. Why build this, and not use an available tool

Three facts show that the available tools are not sufficient (research §2):

1. **No available tool has all three parts.** x-network-map has the ego graph and tie strength. But it has no risk layer, no communities, no centrality, and no dark theme, and its layout becomes too slow at about 1,500 nodes [V2-R5]. The twarc project says that it "is no longer actively supported" [V2-R20]. Botometer cannot score accounts from after May 2023 [V2-R34]. NodeXL Pro is a commercial Excel add-in.
2. **The cost model for X data changed in 2026.** At this time, the X API is pay-per-use [V1-R1]. A tool that does not show the cost before each call can spend the owner's money without approval. The current open-source tools are for the previous free or tiered access.
3. **A security professional must have the risk layer.** A security professional examines a network map for abuse: reply-spam campaigns, impersonation of the owner, new accounts that reply to high-reach posts, and duplicate text. No open tool supplies explainable signals like these for an X ego network (research §5).

The minimum work that closes these three gaps is a small, local Python tool. It has a pluggable collector, an explainable rule engine, and a single WebGL file.

---

## 3. Principal map

| Principal | Role | Interests |
|---|---|---|
| **Ron Dilley** | Owner, only user, architect. Security practitioner (IANS faculty, LightBay). | An accurate private map of the owner's network. Abuse signals with causes. Low cost. Obeys the X terms. A tool that the owner can extend. |
| **X Corp** | Data provider and policy owner | The Developer Agreement and Policy: no surveillance or tracking, no profiles from sensitive categories, deletion in 24 hours, no scraping, paid use [V1-R8][V1-R9]. |
| **Accounts on the map** | Third parties. The tool processes their public data. | They do not know about the tool. An incorrect "Many signals" label can cause harm if somebody shares the map. |
| **Possible future readers** | Readers of a blog post or the audience at a talk | A shared map must be defensible. A shared map shows no labels (§6). |

**The key asymmetry:** the owner gets the value, but the accounts on the map have the risk of an incorrect label. The design protects them by default. Labels stay private, labels describe signals and not persons, and each label shows its causes.

---

## 4. The strategic bets

### Bet 1 — The collector is a plugin, and the official API is the default

**The claim:** a stable internal event model with source adapters (official X API, X archive, Grok, third-party) is better than a design for one source.

**Why it is correct:** the X data market changes frequently. Pay-per-use started in 2026 [V1-R16]. Grok billing changed on 2026-09-21 [V1-R21]. Third-party data vendors can stop without notice. If the analysis uses only the internal event model, a source change costs one adapter, not a full rewrite.

**How it could be incorrect:** sources can supply different fields (for example, no `verified_type` from a third party), so the risk rules get different inputs. **Mitigation:** each event and profile records its source. If a rule must have a field that is not available, the rule gives no result and the tool logs the missing field. It does not guess.

### Bet 2 — Explainable rules are better than a black-box score

**The claim:** for a personal security view, a small set of rules with IDs, points, and cause strings is more useful than an ML bot score.

**Why it is correct:** published bot classifiers identify many humans as bots. In one audit (Rauchfleisch and Kaiser 2020), at a usual threshold, 41% of the "bot" accounts were human. That value is for a test set that the authors made with 15% bots [research §5, section 1.3]. These classifiers also give bad results on new datasets.

A rule that says "Sent 4 near-identical replies" is easy to examine. A score of 0.81 is not.

**How it could be incorrect:** rules can miss new abuse patterns that a model can find. **Mitigation:** the rule table is configuration, so the owner can add rules. LLM-assisted scoring can come in a future version as an advisory signal with its own rule ID.

**Policy position (accepted risk, not a settled question):** the rules use only behavior or metadata and no sensitive category, which decreases the policy risk but does not remove it. The X Developer Agreement §XIV.B and the X Developer Policy limit "tracking X users" and services that "identify behaviors ... which violate X policies" [V1-R8][V1-R9]. Whether a private label for each account is in these limits is a legal question that the research did not answer (research OQ-03). In §6, the owner accepts this risk.

<!-- AI review 20261102-145404: all four reviewers — policy claim was stated as fact (F-4) -->

### Bet 3 — One offline HTML file is the correct output

**The claim:** a single self-contained HTML file is better than a web app or a notebook.

**Why it is correct:** a file has no server that must have protection, and it opens on all computers. The data stays private while the owner keeps the file private. The research test showed that Sigma.js 4 can show 10,000 nodes and 40,000 edges from `file://` in a 1.21 MB file [research §6, section 3.4].

**How it could be incorrect:** very large graphs can become hard to read, and a slower computer can show a lower frame rate. On the owner's workstation, the frame time is 18 ms at 5k nodes and 29 to 36 ms at 10k nodes (2026-11-03).

**Mitigation:** Python calculates the layout, and the in-page layout runs with no worker. The page starts with a filtered core view above a configured node count, and cosmos.gl is the fallback renderer.

### Bet 4 — Cost control is a feature

**The claim:** each paid call must have an estimate, a dry run, and a cap.

**Why it is correct:** at $0.005 to $0.010 for each resource [V1-R1], an expansion to 10,000 accounts costs about $100 for the profiles only. A tool without cost control will spend money without the approval of the owner.

**How it could be incorrect:** the estimates can be incorrect, because X does not document some billing rules (research OQ-01, OQ-02). **Mitigation:** Slice 0 is a paid probe with a $5 cap that answers these questions before work on the estimator starts. Each run records the actual cost and compares it with the estimate.

---

## 5. The arc

<!-- AI review 20261102-145404: all four reviewers — v0.1 scope too large, and seed accounts and communities go together (F-1, F-2) -->

| Window | Name | What ships |
|---|---|---|
| Nov 2026 | **Slice 0: spikes** | No product. A paid X API probe ($5 cap) that answers the billing and field questions. A browser probe in Edge, Chrome, and Firefox at 5k and 10k nodes. It measures the frame rate and the time until the FA2 layout is stable. |
| Q4 2026 | **Foundation (v0.1)** | Private ego map and official X API collector with cost control. Post-level event store with deletion, and a weighted directed graph. Degree, PageRank, and ego-removed betweenness. 30 rules and 12 pattern detectors with neutral signal states and owner overrides. Single-file Sigma.js page with a dark theme, an adjustable layout, playback, findings, an overview dashboard, search, filters, details with rule causes, and shortest path. Incremental collection, a Grok source, and a live run on the owner's account (done 2026-11-03). |
| Q1 2027 | **Depth (v0.2)** | Seed accounts (two hops), Leiden communities with stability values, bio topics, and hubs and bridges. X archive import. Rule B06 and the calibration workflow. Deletion scan, GEXF export, and snapshots that the owner can compare across time. The owner moved v0.2 forward: the code for all of it was written on 2026-11-03, and the archive, the threat feed, and the deletion scan wait for the owner. |
| Q2 2027 | **Reach (v0.3)** | More collectors (third-party with a warning). The Grok collector moved to v0.1 (owner decision 2026-11-02). Bio topics from embeddings. LLM-assisted advisory signals. cosmos.gl fallback renderer. |

---

## 6. What this is not

- **Not a surveillance tool.** It maps only the network of the authenticated owner. It does not monitor one person across time for a third party. A mode for other accounts is not on the plan. It can come back only after a policy review, and then without labels.
- **Not a bot detector with verdicts.** Labels describe signals. They do not say what a person is. The words "legit", "risky", "bot", and "scammer" do not occur in the data model or on the page.
- **Not a publisher of labels.** The default output is private, and the tool has no export that includes labels about third parties.
- **Not a scraper.** No browser automation, and no collection that goes around rate limits.
- **Not a profiler of sensitive categories.** No rule uses politics, religion, health, ethnicity, sexuality, or trade-union membership.
- **Not a web service or a SaaS offering.** No server, no user accounts, no multi-user features.
- **Not a model-training pipeline.** The X Developer Agreement forbids the use of X data to train foundation or frontier models [V1-R8].
- **Not a copy of a different tool.** The @mRr3b00t graph and x-network-map are references for features only. VisualX_AI uses ideas from x-network-map (MIT) and other GPL-compatible code with attribution. It copies no code that has no license.

### Accepted risk

The owner accepts the remaining policy risk of private labels for each account for personal use. The project cannot prove that these labels obey the X Developer Agreement §XIV.B and the X Developer Policy. To decrease the risk, the project:

1. Keeps all labels on the owner's workstation.
2. Does not publish labels, offer them as a service, or share them with third parties.
3. Uses no sensitive-category input.
4. Removes labels and data on request, in the same 24-hour process as content deletion.
5. Stops the labels feature if X objects.

<!-- AI review 20261102-145404: claude, openai — the owner accepts the risk in writing (C-1), owner decision 2026-11-02 -->

---

## 7. What success looks like at the horizon

By mid-2027:

1. The owner runs a full collection and render of the owner's network for less than a budget that the owner sets before the run. The actual cost is in 25% of the estimate.
2. The map shows the owner's network at 5,000 or more nodes, and it pans and zooms smoothly on the owner's workstation.
3. The owner runs the calibration workflow on a hand-labeled sample and reports the measured precision of "Many signals". The target value comes from the first sample (`SUCCESS_CRITERIA.md` BSC-04).
4. The owner records one or more useful results from the map, with the nodes, edges, and rule causes that support each result.
5. No run used a scraping path, a rate-limit bypass, a sensitive-category input, or a label export. The tool can delete content in 24 hours of a request.
