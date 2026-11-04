# VisualX_AI — Requirements

- **Status:** Draft v0.3. This version includes the changes from the Phase 6 and Phase 8 multi-AI reviews.
- **Author:** Ron Dilley
- **Last updated:** 2026-11-03
- **License:** GPL-3.0
- **Companion documents:** `VISION.md`, `SUCCESS_CRITERIA.md`, `ARCHITECTURE.md`, `2026-11-02_X_Network_Analysis_Research.md`, `../tasks/discovery_notes.md`, `../.ai-reviews/20261102-145404/synthesis.md`

Each requirement has an ID, one RFC 2119 keyword (MUST, SHOULD, MAY), a scope tag, and a pointer to its source.

- Scope tags: **[MVP]** is v0.1. **[Scale]** is v0.2. **[vNext]** is v0.3 or a subsequent version. Slice 0 (spikes) has no requirements. It is in `tasks/todo.md`.
- Origin pointers:
  - `BG-Rn` is a rule in `discovery_notes.md` §6.
  - `DN §n` is a section of `discovery_notes.md`.
  - `RS §n` is a section of the research survey.
  - `V#-R#` is a reference in the survey.
  - `REV F-n`, `REV S-n`, and `REV C-n` are items in the Phase 6 synthesis.
  - `ARCH-REV A-n` and `ARCH-REV S-n` are items in the Phase 8 synthesis (`.ai-reviews/20261102-151135/synthesis.md`).

---

## §0 Context and scope

VisualX_AI is a local Python command-line tool. It collects the public interactions around the authenticated owner's X account and stores them. Then it builds a weighted interaction graph and calculates network metrics and rules-based signal labels. Then it writes one self-contained interactive HTML file. One person (the owner) uses it on one Windows 11 workstation.

**MVP boundary (v0.1), the private ego map:**

- The owner's account only.
- Official X API v2 collector (owner posts, mentions, quote posts, reposting users, profiles).
- Cost estimate, dry run, spend cap.
- Post-level event store with deletion.
- Weighted directed graph with per-type counts.
- Degree, weighted degree, PageRank, and ego-removed betweenness.
- Layout calculated in Python.
- 30 signal rules and 12 network-level pattern detectors, with neutral states and owner overrides.
- Single-file Sigma.js HTML page.
- A live run on the owner's account.

**Not in v0.1** (REV F-1, F-2): Leiden communities, bio topics, hubs and bridges, X archive import, and rule B06. These are v0.2. On 2026-11-03 the code for all v0.2 requirements was written: FR-06, FR-20, FR-26 to FR-28, FR-30, FR-51, B06 of FR-56, IR-04, DR-05, the 10k part of NFR-04, and the `--redact` render. The origin column of each row tells what is open.

The owner moved incremental collection (FR-08), the Grok adapter (FR-09), 6 starter rules (FR-56), the in-page layout (FR-58), and seed accounts (FR-07) into v0.1 (2026-11-02 and 2026-11-03).

**Ego graph in v0.1, with optional seeds.** The graph has the owner and the accounts that interact with the owner. Edges between two other accounts occur only when the collected posts show them (for example, a reply by one account to another in the owner's thread). This answers discovery open question 3 for v0.1.

With seed accounts (FR-07, off by default), the tool also reads the recent posts of the owner's top contacts. Their links to other accounts then go into the graph. The seeds come only from the owner's own interactions. This is not a mode to map a different account (FR-54).

---

## §1 Functional requirements

### 1.1 Collection

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| FR-01 | The tool MUST collect through a collector interface. Each data source has its own adapter, and all adapters write the same internal event and profile records. | MVP | DN §1 (owner decision), RS §7.1 |
| FR-02 | The tool MUST include an official X API v2 adapter that uses the owner's OAuth 2.0 Authorization Code token with PKCE. | MVP | RS §7.1 item 2, V1-R1 |
| FR-03 | The X API adapter MUST collect the owner's posts and mentions for a time window that the owner sets. | MVP | BG notes, "Pipeline: Collect" |
| FR-04 | The X API adapter MUST collect the quote posts and reposting users of the owner's posts in the window. | MVP | BG notes, "Pipeline: Collect" |
| FR-05 | The X API adapter MUST collect a profile for each account that occurs in an event, in batches of up to 100. | MVP | BG notes, "Pipeline: Collect" |
| FR-06 | After an inspection of the owner's archive (not a sample archive), the tool MUST import the owner's X data archive as a free source of outbound events. | Scale | RS §7.1 item 3, REV S-5. Code: `visualx import-archive` (2026-11-03). Open: the inspection of the owner's archive (TSC-02). |
| FR-07 | The tool MUST support seed accounts: select the top N contacts by tie weight (default 40, maximum set by the owner) and collect their recent posts. | MVP | BG notes, REV F-2, owner decision 2026-11-03 (moved from Scale). The configuration default is 0 (off). The owner sets N for each run with `--seeds N`. |
| FR-08 | The tool MUST support incremental collection with `since_id`, so that a subsequent run pays only for new resources. | MVP | RS §7.1 item 8, owner decision 2026-11-03 (moved from Scale) |
| FR-09 | The tool SHOULD include a Grok `x_search` adapter. Its output MUST be labeled as unverified model output. | MVP | RS §7.1 item 4, owner decision 2026-11-02 (moved from vNext) |
| FR-10 | The tool MAY include a third-party provider adapter. The adapter MUST show a ToS warning and MUST NOT be the default. | vNext | RS §7.1 item 5 |
| FR-11 | The tool MUST NOT use browser automation or scraping for collection. | MVP | BG notes, "Cheaper Practical Alternatives" item 3, V1-R10 |
| FR-54 | The tool MUST collect only for the authenticated owner's account and MUST NOT accept a different target account. | MVP | VISION §6, REV F-15 |

### 1.2 Cost control

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| FR-12 | Before each paid collection step, the tool MUST show an estimate of the resource count and the cost in USD. It MUST also show the unit prices that it used and each billing assumption that is not verified. | MVP | VISION Bet 4, REV S-13 |
| FR-13 | The tool MUST have a dry-run mode that calculates the estimate and makes no paid call. | MVP | VISION Bet 4 |
| FR-14 | The tool MUST stop before a call that can push the cost above the spend cap that the owner set for the run. | MVP | VISION Bet 4, V1-R1 |
| FR-15 | The tool MUST record the actual resource count and the calculated actual cost of each step, and show the difference from the estimate. | MVP | VISION §7 item 1 |
| FR-16 | Unit prices and billing assumptions MUST be configuration values, not constants in code. | MVP | V1-R1 ("prices are subject to change") |

### 1.3 Graph building

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| FR-17 | The tool MUST build a directed graph where nodes are accounts. Each edge MUST keep one count and the timestamps for each interaction type (reply, quote, mention, repost). | MVP | BG-R1, RS §7.3 item 2 |
| FR-18 | The tool MUST calculate a combined edge weight from configurable type weights (start values: reply 3, quote 2, mention 1.5, repost 1) and `1 + ln(count)`. | MVP | BG-R1, BG-R3, V2-R8 |
| FR-19 | The tool MUST apply a configurable reciprocity multiplier to pairs with interactions in the two directions. | MVP | BG-R2 |
| FR-20 | The tool SHOULD apply a recency decay with a configurable half-life. | Scale | RS §7.3 item 3. Code: `weights.half_life_days` (2026-11-03), 0 = off by default. |
| FR-21 | The tool MUST classify each post as one edge type for each target. A repost MUST NOT also give a mention for its "RT @user" text. A reply or a quote MUST NOT also give a mention to the same target. | MVP | V2-R24, V2-R12, REV S-6 |
| FR-22 | The store MUST keep all interactions, also for accounts with no profile record. The interaction table MUST NOT have a foreign key to the profile table. The graph MUST mark these nodes with `profile_status = missing`, and the page MUST hide them by default and show their count. | MVP | V2-R7, REV S-2, ARCH-REV S-18 |
| FR-23 | The tool MUST support a minimum tie weight filter, so that the owner can remove weak ties before display. | MVP | BG-R6 |

### 1.4 Analysis

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| FR-24 | The tool MUST calculate degree, weighted degree, and PageRank for each node. | MVP | BG notes, "Process / Analyze" |
| FR-25 | The tool MUST calculate betweenness on the graph with the ego removed. It MUST convert weights to distances (`1/w`) before it calculates a path metric. | MVP | RS §7.4 items 1, 2 |
| FR-26 | The tool MUST detect communities with Leiden (leidenalg) on the undirected, ego-removed graph, with a configurable resolution and 20 or more random seeds. It MUST record a stability value for each node. | Scale | RS §7.4 item 4, REV F-2, C-2. Code: `groups.py` (2026-11-03). Live data: 18 communities from 20 runs. |
| FR-27 | The page MUST label communities as exploratory and show the stability value. | Scale | REV (openai, efficacy). Code: the Controls drawer and the account panel (2026-11-03). |
| FR-28 | The tool MUST give bio topics to accounts with configurable keyword or regex rules and label propagation. | Scale | V2-R9, REV F-2. Code: `config/topics.yaml` and `groups.py` (2026-11-03). The loader refuses a sensitive topic (TR-06). |
| FR-29 | The tool SHOULD give bio topics to accounts with embeddings and clusters. | vNext | RS §7.4 item 7 |
| FR-30 | The tool MUST mark hub accounts and bridge accounts (high ego-removed betweenness, or neighbors in two or more communities). | Scale | BG notes, "Overview" (hubs/bridges). Code: `groups.py` (2026-11-03). Live data: 22 hubs, 22 bridges. |
| FR-55 | The tool MUST calculate the node layout in Python with igraph (Fruchterman-Reingold or an equivalent). The layout MUST keep the ego at the center, and the tool MUST write the x and y values into the output. | MVP | RS §4 section 9 (igraph FR about 1 s at 10k nodes), owner license decision (GPL-3.0) |

### 1.5 Signal scoring

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| FR-31 | The tool MUST give each account one signal state: `not_scored`, `no_signals`, `some_signals`, or `many_signals`. The page MUST show these as "Not scored", "No signals seen", "Some signals", and "Many signals". The words "legit", "risky", "bot", and "scammer" MUST NOT occur in state names, display text, or cause strings. | MVP | BG-R4, REV S-1, VISION §6 |
| FR-32 | Each rule MUST have a rule ID, a signal family (gate, profile, trust, behavior, impersonation), a signal, a threshold, points, and a cause-string template. The rule table MUST be a configuration file. | MVP | RS §7.5 item 2 |
| FR-33 | The tool MUST record each rule that fires for each account, with the rule ID, the points, the data value, and the cause string. | MVP | RS §7.5 item 2 |
| FR-34 | The tool MUST give `not_scored` when a data gate fails (no profile data, a protected account, or an account on the owner's allowlist). | MVP | V4 rules N01 to N03 |
| FR-35 | Profile rules alone MUST NOT give `many_signals`. Only behavior rules, impersonation rules, or hard rules can give it. | MVP | V4 §6.1 |
| FR-36 | The v0.1 rule set MUST include these 17 rules from research §5, section 6.2: N01, N02, N03, P01, P03, P05, P06, P07, T01, T02, T03, B01, B03, B04, B05, B07, I01. | MVP | REV §5.2 |
| FR-56 | The rule set MUST add the 7 remaining starter rules: P02, P04, B02, B06, B08, I02, I03. B06 MUST use a threat feed with recorded terms and a size limit. | MVP (6 rules), Scale (B06) | REV F-11, F-12, S-15. B06 code (2026-11-03): it reads `config/threat_domains.txt`. Open: the owner selects a feed and records its terms. |
| FR-57 | If the input field of a rule is not available, the rule MUST give no result for that account and MUST log the missing field. It MUST NOT supply a made-up value. | MVP | VISION Bet 1, REV S-9 |
| FR-37 | The tool MUST let the owner override the state of an account, and the override MUST stay in effect for all subsequent runs. | MVP | RS §7.5 item 4 |
| FR-38 | The tool MUST support a calibration workflow: export a stratified sample, import the owner's labels, and report precision for each state. | MVP | RS §7.5 item 3, owner decision 2026-11-03 (moved from Scale) |
| FR-39 | The tool MAY add LLM-assisted advisory signals, each with its own rule ID. They MUST NOT give `many_signals` alone. | vNext | VISION Bet 2 |
| FR-59 | The score stage MUST run network-level pattern detectors (bursts, creation clusters, fast replies, shared content, lookalikes, outliers, one-way pressure, tight groups, co-action, profile changes, mass tagging, community anomalies) and record each finding with its members, its data (`evidence_json`), and its severity. Titles and summaries MUST NOT contain text from X. | MVP | Owner request 2026-11-03 |
| FR-60 | The store MUST keep a row in `profile_history` each time the handle, display name, URL, or profile image of an X API profile changes. Deletion MUST remove these rows (AD-10). | MVP | Owner request 2026-11-03 (detector `profile_change`) |
| FR-61 | Each render MUST keep a snapshot of the states, rules, communities, tie weights, and findings. The page MUST show the changes since the snapshot before it. A command MUST show the counts of the changes between two snapshots. The details MUST stay in the private HTML file, and deletion MUST remove a user from each snapshot (AD-10). | Scale | VISION §5, owner request 2026-11-03. Code: `snapshot.py`, `visualx snapshot list` and `diff`. |

### 1.6 Visualization

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| FR-40 | The tool MUST write one self-contained HTML file that opens from `file://` with no network access and no server. | MVP | VISION Bet 3, RS §7.6 item 1 |
| FR-41 | The page MUST render with WebGL (Sigma.js 4.0.x and graphology), with a dark theme. | MVP | RS §7.6 item 1, BG notes, "Overview" |
| FR-42 | The page MUST color nodes by signal state (and, from v0.2, by community or topic as the viewer selects). It MUST color edges by interaction type. | MVP | BG notes, "Overview" and "Visualize" |
| FR-43 | The page MUST set the dimensions of nodes from a mode that the viewer selects (followers or weighted degree). | MVP | BG notes, "Visualize" |
| FR-44 | The page MUST support zoom, pan, hover, and click. A click MUST show a panel with the profile text, the metrics, the signal state, and the list of rules that fired. | MVP | BG notes, "Visualize", FR-33 |
| FR-45 | The page MUST support search by handle or display name. | MVP | BG notes, "Visualize" |
| FR-46 | The page MUST support filters by signal state, interaction type, and minimum tie weight (and by community from v0.2). | MVP | BG notes, "Visualize" |
| FR-47 | The page MUST highlight the weighted shortest path between two selected nodes. | MVP | BG notes, "Visualize", V2-R14 |
| FR-48 | The page MUST start with a filtered core view when the graph has more nodes than a configurable count. | MVP | RS §7.6 item 6 |
| FR-49 | The page MUST show the collection window, the data sources, the node and edge counts, and the hidden node count. It MUST also show a short statement of what the graph cannot show. | MVP | RS §7.7 |
| FR-50 | The page MUST show the signal state with color and also with a shape or a text mark. | MVP | RS §7.6 item 5 |
| FR-58 | The page MAY refine the layout with graphology ForceAtlas2 when the viewer starts it. The page MUST show the graph from the Python layout before the viewer starts it. | MVP (in the page thread, no worker) | RS §7.6 item 3, REV F-3, F-8, ARCH-REV S-16 |
| FR-51 | The tool SHOULD export the graph as GEXF and in the x-network-map JSON shape. The exports MUST NOT include signal states. | Scale | V2 implications, VISION §6. Code: `visualx export --format gexf` or `xnm` (2026-11-03). |

### 1.7 Command-line interface

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| FR-52 | The tool MUST have one subcommand for each operation (collect, build, analyze, score, render, delete) and one subcommand that runs collect to render in sequence. | MVP | BG notes, "Pipeline" |
| FR-53 | Each subcommand MUST read its inputs from files and write its outputs to files, so that a stage can run again without the earlier stages. | MVP | V2-R13 (resumable) |

---

## §2 Non-functional requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| NFR-01 | The analyze stage (without communities) MUST complete in less than 2 minutes for a graph of 10,000 nodes and 50,000 edges on the owner's workstation. | MVP | RS §4 section 9, project test 26 s with igraph |
| NFR-02 | The Python layout MUST complete in less than 2 minutes for 10,000 nodes. | MVP | RS §4 section 9 (igraph FR about 1 s), REV F-3 |
| NFR-03 | The HTML file SHOULD be less than 10 MB for 10,000 nodes and 50,000 edges. | MVP | RS §6 section 3.1 |
| NFR-04 | The page MUST stay interactive (pan and zoom at 30 frames per second or more) at 5,000 nodes with labels off, in a headed browser on the owner's workstation. The 10,000-node target is v0.2. | MVP (5k), Scale (10k) | VISION §7 item 2, REV (openai, performance). 10k with the v0.2 page (2026-11-03): 7 of 8 headed runs at 33 ms or less. |
| NFR-05 | The collector MUST obey the X API rate limits, wait when it gets HTTP 429, and continue from the last completed page. | MVP | V1-R2, V1-R8 III.D |
| NFR-06 | The tool MUST run on Windows 11 with Python 3.12 and install from binary wheels with no compiler. | MVP | DN §3, RS §4 section 9 |

---

## §3 Security requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| SR-01 | The tool MUST keep OAuth access and refresh tokens in the OS keyring (Windows Credential Manager). The client ID MAY be in a configuration file that is not in version control. The tool MUST NOT write tokens or secrets to logs or to output. | MVP | REV S-7 |
| SR-02 | The page MUST put all text from X (names, bios, post text, URLs) into the page with `textContent` or an equivalent escape and MUST NOT use `innerHTML` for this text. | MVP | RS §7.6 item 4 |
| SR-03 | The HTML page MUST NOT load an external script, style, font, or image. Profile images are not in v0.1. | MVP | REV F-9 |
| SR-04 | Vendor JavaScript files MUST have pinned versions. The build MUST check each package tarball against the `integrity` value from the npm registry. It MUST record a SHA-256 hash for each extracted file in `VENDOR.lock`. It MUST stop when a file does not agree with `VENDOR.lock`. | MVP | V5 implications item 1, REV S-8, ARCH-REV S-4 |
| SR-05 | Embedded data MUST be encoded (gzip plus base64) so that text from X cannot close the `<script>` element. | MVP | SR-02 |
| SR-06 | The tool MUST NOT send collected X data to a third party. This rule does not apply to the X API itself. The Grok source (FR-09) sends xAI only the owner handle, the date windows, and the prompt, not the store. | MVP | V1-R8 III.A(k) |
| SR-07 | URLs from X that the page shows MUST be plain text or MUST open only with the `http` or `https` scheme. The page MUST NOT make a link from a `javascript:` or `data:` URL. | MVP | REV F-13 |
| SR-08 | The architecture MUST include a threat model. | MVP | REV S-12 |
| SR-09 | The page MUST have a Content Security Policy that blocks all network connections, external sources, images, workers, forms, and a `base` change. | MVP | ARCH-REV S-7 |

---

## §4 Integration requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| IR-01 | The X API adapter MUST request the fields and expansions that give the edge type and the counterparty profile in one call (`referenced_tweets`, `in_reply_to_user_id`, `entities`, `author_id`, and the user fields that the rules need). | MVP | V1 §1.3 |
| IR-02 | The X API adapter MUST handle pagination tokens and the 800-mention limit of the mentions endpoint. It MUST report when the limit cut the window. | MVP | V1-R3 |
| IR-03 | The X API adapter MUST use the OAuth 2.0 PKCE flow with a redirect to `http://127.0.0.1:<port>/callback`. The callback listener MUST listen on 127.0.0.1 only and MUST stop after one callback. The adapter MUST refresh tokens, MUST tell the owner when a refresh is unsuccessful, and MUST have a command that removes the stored tokens. | MVP | V1 §1.4, REV F-10, S-7 |
| IR-04 | The archive importer MUST read the JavaScript-wrapped JSON files of the X archive and report each file that it does not recognize. | Scale | V1-R13, REV S-5. Code: `collect/archive.py` (2026-11-03). |
| IR-05 | Each adapter MUST record its name and version on each event and profile that it writes. | MVP | VISION Bet 1 |
| IR-06 | Each paid call MUST log the endpoint, the request parameters (without secrets), the resource count, the response code, and the cost. | MVP | FR-15 |

---

## §5 Data requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| DR-01 | The tool MUST store raw API responses in a local cache, so that a stage can run again with no new paid call. | MVP | V2-R13 |
| DR-02 | For each interaction, the tool MUST store the source user ID, target user ID, type, observation time, source endpoint, and adapter. It MUST store the source post ID, the target post ID, and the interaction time when the endpoint gives them. It MUST set their `field_status` to `absent` when the endpoint does not give them. It MUST NOT make up a post ID or a time. | MVP | RS §7.1 item 7, ARCH-REV S-1 |
| DR-03 | The tool MUST store profiles with the fields that are necessary for the rules (`created_at`, `public_metrics`, `description`, `url`, `location`, `profile_image_url`, `verified_type`, `protected`), and `null` for a value that is not known. | MVP | V4 §2, V2-R7 |
| DR-04 | The tool MUST delete all data for a post ID or a user ID from all locations: the store tables, the raw cache (by rewriting each affected file without the matching objects), the rendered HTML files (by deleting each file that has the ID and rendering the newest run again), the overrides, and the exports. | MVP | V1-R8 §IV.B, REV S-3, ARCH-REV A-1, A-2 |
| DR-05 | The tool SHOULD have a sweep that finds content that is no longer on X and deletes it locally. | Scale | V1-R8 §IV.B. Code: `visualx sweep` (2026-11-03), paid. Open: a live sweep after the owner gives approval for an estimate. |
| DR-06 | The tool MUST NOT store location data other than the free-text profile `location` field. | MVP | V1-R8 III.E |
| DR-07 | All data MUST stay on the owner's workstation in a data folder that is not in version control. | MVP | VISION §6 |
| DR-09 | The store MUST keep post text by default (for re-scoring without a new paid collection). The page MUST NOT show post text in v0.1. A configuration option MUST remove the text after `score` and keep only hashes and URL domains. | MVP | ARCH-REV S-9 (AD-11) |
| DR-10 | The tool MUST delete raw cache files after a configurable retention time (default 30 days). | MVP | ARCH-REV A-2, V1-R9 |
| DR-08 | Each run MUST write a manifest with the run ID, time window, sources, configuration, resource counts, estimate, actual cost, and list of rendered files. | MVP | FR-15, DR-04 |

---

## §6 Observability requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| OR-01 | The tool MUST write a structured log (JSON lines) for each run that has each API call as in IR-06. The log MUST NOT contain names, bios, or post text, and MUST contain IDs only as HMAC-SHA256 values with a local key. | MVP | FR-15, ARCH-REV S-8 |
| OR-02 | The tool MUST print a summary at the end of each run: counts of nodes, edges, events, hidden nodes, states, rules that fired, rules with no input, and cost. | MVP | FR-15, FR-57 |
| OR-03 | The tool MUST give a warning when it knows that data is missing (mentions limit reached, profile lookups that failed, endpoints that returned no data). | MVP | IR-02 |

---

## §7 Operational requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| OPR-01 | The README MUST tell how to make an X developer app, buy credits, and set a spend limit in the X Console. It MUST also tell how to do the first OAuth sign-in. | MVP | V1-R1 |
| OPR-02 | The tool MUST run from one command after the first setup. | MVP | VISION §1 |
| OPR-03 | The README MUST tell how to delete the data for a deletion request in 24 hours with the delete command. | MVP | DR-04 |

---

## §8 Architectural requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| AR-01 | All dependencies and all bundled code MUST have a license that is compatible with GPL-3.0 (for example GPL-3.0, GPL-2.0-or-later, LGPL, MIT, BSD, Apache-2.0). Code or packages with no license, non-commercial licenses (for example CC-BY-NC), or GPL-2.0-only MUST NOT be used. | MVP | Owner decision 2026-11-02 (GPL-3.0), V5 (`@cosmograph/*` is CC-BY-NC-4.0) |
| AR-02 | The stages MUST communicate only through the local store (`store.sqlite`), with a `stage_runs` record for each stage output. The rendered HTML file is the only stage output that is not in the store. | MVP | FR-53, ARCH-REV A-4 |
| AR-03 | The analysis stages MUST use only the internal event and profile model, not a source format. | MVP | VISION Bet 1 |
| AR-04 | Reused code MUST keep its copyright and license notice (x-network-map MIT, and other sources). | MVP | V2-R2 |

---

## §9 Maintainability requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| MR-01 | Type weights, rules, thresholds, topics, unit prices, and colors MUST be in configuration files. | MVP | FR-16, FR-32 |
| MR-02 | The code MUST use type hints, `pyproject.toml`, and pytest. | MVP | Sibling convention (LocalLLMPerf_AI) |
| MR-03 | A change to a rule threshold or points MUST NOT make a code change necessary. | MVP | VISION Bet 2 |
| MR-04 | The documents MUST use "seed accounts" for collection seeds and "random seeds" for algorithm seeds. | MVP | REV S-11 |

---

## §10 Testing requirements

| ID | Requirement | Scope | Origin |
|---|---|---|---|
| TR-01 | Each rule MUST have a unit test that shows it fires and a unit test that shows it does not fire. | MVP | FR-36 |
| TR-02 | The tool MUST have an end-to-end test that uses recorded API responses and makes an HTML file. | MVP | — |
| TR-03 | A browser test MUST open the HTML file from `file://` in a headless browser. It MUST make sure that there are no console errors and no network requests. It MUST also make sure that the page shows the graph, and that search, filter, and shortest path operate correctly. | MVP | V5 implications item 7 |
| TR-04 | A v0.1 release MUST include one collection run against the live X API on the owner's account. Recorded responses alone are not sufficient for a release. | MVP | VIBE success-claim rule |
| TR-05 | The tool MUST have a performance test with a synthetic graph of 10,000 nodes and 50,000 edges for NFR-01 to NFR-03. The live run in TR-04 MUST also record the same timings. | MVP | NFR-01, REV (claude, acceptance) |
| TR-06 | A test MUST read the rule file. The test MUST show a failure if a rule uses a field or a keyword from a list of sensitive categories. It MUST also show a failure if a rule has no signal family. | MVP | VISION §6, REV F-5 |

---

## §11 Out of scope

| Item | Why | Re-entry point |
|---|---|---|
| Publication or export of signal labels | X Developer Policy limit on statements about policy violations (V1-R9), and risk of injury to third parties | A new decision, possibly with X permission |
| Maps of accounts other than the owner | X Developer Agreement §XIV.B(a) (V1-R8), REV F-15 | Policy review, then without labels only. |
| Browser automation or scraping | X Terms of Service (V1-R10), and account risk | None planned |
| Sensitive-category inference | X Developer Agreement §XIV.B(d) (V1-R8) | None planned |
| Web server, multi-user, SaaS | VISION §6 | None planned |
| ML or LLM bot classifier as a primary score | False-positive rates (RS §7.5 item 4) | Advisory signals in vNext (FR-39) |
| Profile images | Network leak and offline posture (REV F-9) | v0.2 review: images inline in the file only |
| Follower and following graphs of other accounts | Cost: $0.010 for each user (V1-R1) | v0.3, with a cost estimate |
| Likes as an edge type | Not one of the four types in the background notes, and cost | v0.2 review |
| 3D view | Not necessary for the goal | None planned |
