# VisualX_AI — Success Criteria

- **Status:** Draft v0.3. This version includes the changes from the Phase 6 and Phase 8 multi-AI reviews.
- **Author:** Ron Dilley
- **Last updated:** 2026-11-03
- **Companion documents:** `VISION.md`, `REQUIREMENTS.md`, `ARCHITECTURE.md`, `../.ai-reviews/20261102-145404/synthesis.md`

Each criterion is measurable and has an ID and a version gate. Business criteria (BSC) come from the Vision. Technical criteria (TSC) come from the Requirements. §3 connects each criterion to a test and a data source. §4 is the traceability matrix.

---

## §1 Business success criteria

| ID | Criterion | Target | Gate | Origin |
|---|---|---|---|---|
| BSC-01 | A full v0.1 run on the owner's account (90-day window, no seed accounts) costs less than the cap that the owner set before the run. | 100% of runs | v0.1 | VISION Bet 4 |
| BSC-02a | Each run records the estimate, the actual cost, and the difference for each step. | 100% of runs | v0.1 | REV S-13 |
| BSC-02b | The actual cost of a run is in 25% of the estimate, compared with the cost that the X Console shows. Closed by the owner on 2026-11-03: not material. | Closed | Not a gate | VISION §7 item 1 |
| BSC-03 | The owner records one or more findings from the map in `VIBE_HISTORY.md`. Each finding names the nodes, edges, filters, and rule causes that support it. | 1 or more findings | v0.1 | VISION §7 item 4, REV (openai) |
| BSC-04 | The calibration workflow runs on a stratified sample of 200 or more accounts that the owner labels by hand. The tool reports the precision of `many_signals` and `some_signals`. The owner sets numeric targets after the first sample. | Workflow complete, precision reported | v0.2 | VISION §7 item 3, REV F-6 |
| BSC-05 | These four policy controls are in effect. (a) The code has no scraping or browser-automation adapter. (b) No code path goes around a rate limit. (c) No rule has a known sensitive-category input (TR-06), and the owner examines each new rule and detector for a proxy of a sensitive category. (d) No export or page feature writes signal states for other accounts to a file other than the private HTML file. | All four in effect | v0.1 | REV F-5 |
| BSC-06 | The owner can delete all data for one post ID or one user ID with one command, in all stored files (DR-04). | Complete in less than 1 minute | v0.1 | DR-04 |

## §2 Technical success criteria

### 2.1 Collection and integration

| ID | Criterion | Target | Gate | Origin |
|---|---|---|---|---|
| TSC-01 | One live X API run on the owner's account sends a request to each configured endpoint (owner posts, mentions, quote posts, reposting users, profiles). The log has the response of each call. All records from X are in the event store. Owner posts, mentions, and profiles each have 1 or more records from the live run. | As given | v0.1 | FR-03 to FR-05, TR-04, REV S-4 |
| TSC-02 | The archive importer reads the owner's archive (not a sample archive), reports each file that it does not recognize, and links each event to its archive record. | All recognized files read | v0.2 | FR-06, REV S-5. Open: 23 tests with hand-made archives have no failures (`tests/unit/test_archive.py`). The owner's archive is not imported. |
| TSC-03 | A dry run does not make a paid call. | 0 calls in the API log | v0.1 | FR-13 |
| TSC-04 | The spend cap stops the run before a call that can make the cost more than the cap. For the Grok source, the cap stops the run before a request when the spend plus the reservation is above the cap. | X API: logged cost of the run is not more than the cap. Grok: no request starts when the spend plus the reservation is above the cap. | v0.1 | FR-14 |
| TSC-05 | After HTTP 429, the collector waits and then continues with no missing page and no duplicate event. | Event count is equal to the count of a run with no 429 | v0.1 | NFR-05 |
| TSC-27 | The OAuth callback listener listens on 127.0.0.1 only. It rejects a different path, a different method, a missing code, an incorrect `state`, and a second callback, and it stops after one good callback. Tokens are in the Windows Credential Manager, not in a file. | Test passes for each condition | v0.1 | IR-03, SR-01, ARCH-REV S-13 |
| TSC-33 | The store keeps a repost from `retweeted_by` with the source user, the target user, and the observation time. It sets `field_status` to `absent` for `source_post_id` and `interaction_created_at`. The record has no made-up value. | Test passes | v0.1 | DR-02, ARCH-REV S-1 |

### 2.2 Data integrity

| ID | Criterion | Target | Gate | Origin |
|---|---|---|---|---|
| TSC-06 | Each event with an account that has no profile is in the event store. Its node has `profile_status = missing`, and the page hides it by default. The page shows the hidden count. | 0 missing events | v0.1 | FR-22 |
| TSC-07 | A repost gives no mention edge from its "RT @user" text. A reply or a quote gives no mention edge to the same target. | 0 extra edges in a test fixture for each condition | v0.1 | FR-21 |
| TSC-08 | Each edge type count in the graph equals the count of events of that type in the event store. | Exact match | v0.1 | FR-17 |
| TSC-09 | After a delete command, no stored file has the deleted ID. The test decodes the compressed data in rendered HTML files before it searches. | 0 matches | v0.1 | DR-04, REV S-3 |

### 2.3 Analysis

| ID | Criterion | Target | Gate | Origin |
|---|---|---|---|---|
| TSC-10 | The tool calculates betweenness without the ego. In a test graph where the ego connects to all other accounts, a known bridge account gets the highest betweenness. | Bridge account is rank 1 | v0.1 | FR-25 |
| TSC-11 | Leiden gives a stability value for each node, from 20 or more random seeds. | Value recorded for 100% of nodes | v0.2 | FR-26. Met on live data 2026-11-03: 1,065 of 1,065 accounts (the owner is not in the graph), 20 runs, mean 1.0. |
| TSC-12 | The analyze stage completes for a synthetic graph of 10,000 nodes and 50,000 edges. | Less than 2 minutes | v0.1 | NFR-01 |
| TSC-13 | The Python layout completes for 10,000 nodes. | Less than 2 minutes | v0.1 | NFR-02, FR-55 |

### 2.4 Signal scoring

| ID | Criterion | Target | Gate | Origin |
|---|---|---|---|---|
| TSC-14 | Each of the 31 rules has a test that fires and a test that does not fire. | 62 or more tests that pass | v0.1 (B06: v0.2) | TR-01, FR-36, FR-56. B06: `tests/unit/test_v02.py`. |
| TSC-15 | Each account in a state other than `not_scored` has its list of rule results (empty for `no_signals`). Each state other than `no_signals` has one or more rule results. | 100% of accounts | v0.1 | FR-33 |
| TSC-16 | No account gets `many_signals` from profile rules only. | 0 accounts | v0.1 | FR-35 |
| TSC-17 | The rule-file test (TR-06) shows a failure for each fixture that has a sensitive-category field or keyword, or a rule with no signal family. | Test shows a failure for each bad fixture | v0.1 | TR-06, REV (openai, TSC-17) |
| TSC-18 | A state override that the owner set stays after a new run. | 100% of overrides | v0.1 | FR-37 |
| TSC-28 | If the input field of a rule is not available, the rule gives no result and writes one log entry. | Test passes | v0.1 | FR-57 |
| TSC-29 | The words "legit", "risky", "bot", and "scammer" do not occur in the state names, the display text, or the cause strings. | 0 matches | v0.1 | FR-31 |
| TSC-34 | The labeled detector benchmark (`tests/validation/`) finds the planted campaigns and stays quiet on background traffic. The coordination, lookalike, and network detectors give 1 or fewer findings in the background runs, and 5 % or less of the background accounts get `some_signals` or `many_signals`. | The limits in `test_detector_benchmark.py` | v0.1 | FR-59, owner decision 2026-11-03. Fourth measurement (seeds 531 to 538, 631 to 638): 3.3 % of the background accounts, 1 with `many_signals` in 8 runs. |

### 2.5 Visualization

| ID | Criterion | Target | Gate | Origin |
|---|---|---|---|---|
| TSC-19 | The HTML file opens from `file://` in headless Edge and Chrome with no console errors and no network requests. | 0 errors, 0 requests | v0.1 | FR-40, SR-03 |
| TSC-20 | Test nodes with the values that follow show as plain text, and no script runs. The first three values are a display name `<img src=x onerror=alert(1)>`, a bio with `</script>`, and the URLs `javascript:alert(1)` and `data:text/html,<script>alert(1)</script>`. The other values are a name with bidirectional control characters, a name with zero-width characters, a bio of 100,000 characters, and text with HTML entities. | Text shown, no alert, no parse break, no link from a `javascript:` or `data:` URL | v0.1 | SR-02, SR-05, SR-07, REV F-13 |
| TSC-21 | Search, filters, and shortest path work in the browser test. | All three pass | v0.1 | TR-03 |
| TSC-22 | On the owner's workstation, a Playwright script drives a headed (not headless) browser through a constant pan and zoom sequence. The script records the frame times. The record also gives the browser version, the GPU, the node and edge counts, and the label and edge configuration. | Mean frame time 33 ms or less (30 FPS or more) at 5,000 nodes with labels off | v0.1 (5k), v0.2 (10k) | NFR-04, REV (claude, openai), ARCH-REV S-12 |
| TSC-23 | The HTML file for 10,000 nodes and 50,000 edges is less than 10 MB. | Less than 10 MB | v0.1 | NFR-03 |
| TSC-30 | The page shows the graph from the Python layout, and ForceAtlas2 runs in the page with no worker when the viewer starts it. The CSP stays as in SR-09. | Browser test passes | v0.1 | FR-58 |
| TSC-32 | The page has the CSP of SR-09, with a hash for each script that runs and no `'unsafe-inline'` in `script-src`. A test page with an added `<script src>`, an added inline script, and an inline event attribute: the two inline items do not run, and a `fetch()` and an image load show CSP errors and make no request. | 0 requests, 0 added scripts run, page has the CSP | v0.1 | SR-09 |

### 2.6 Security and maintainability

| ID | Criterion | Target | Gate | Origin |
|---|---|---|---|---|
| TSC-24 | No credential or token is in a log file, an output file, or a file that version control can see. | 0 matches from a secret scan | Each release | SR-01 |
| TSC-25 | All installed Python packages and all bundled JavaScript files have licenses that are compatible with GPL-3.0. | 0 packages or files with a license that is not compatible or not known | Each release | AR-01 |
| TSC-26 | A change to a rule threshold in the rule file changes the states with no code change. | Test passes | v0.1 | MR-03 |
| TSC-31 | Each vendor package tarball agrees with its npm `integrity` value, and each vendor file agrees with its SHA-256 hash in `VENDOR.lock`, at build time. | Build shows a failure on a difference | v0.1 | SR-04 |

## §3 Test plan

| Criterion | Test | Data source | Threshold | When |
|---|---|---|---|---|
| BSC-01, TSC-03, TSC-04 | `test_cost_control` (unit) and a live run | Recorded responses, live run manifest | As in §1, §2 | Each commit, each live run |
| BSC-02a | Manifest check | Live run manifest | All steps recorded | Each live run |
| BSC-02b | Closed by the owner on 2026-11-03 (not material) | Not applicable | Not applicable | Not applicable |
| BSC-03 | Owner review of the map | Live run output | 1 or more recorded findings | End of v0.1 |
| BSC-04 | Calibration workflow (FR-38) | 200-account stratified sample with owner labels | Precision reported | v0.2 |
| BSC-05 | Code review checklist, TR-06, search of the code for scraping libraries | Code, rule file | All four in effect | Each release |
| BSC-06, TSC-09 | `test_delete` (unit) and the end-to-end delete test. No live delete (owner decision 2026-11-03). | Test data folder | 0 matches | Each commit |
| TSC-01 | Live run on the owner's account | X API | As given | Before v0.1 is complete |
| TSC-05 | `test_rate_limit_resume` with a fake server that sends 429 | Fixture | Exact count | Each commit |
| TSC-27 | `test_oauth_listener` (unit) and a manual check of the Credential Manager | Fixture, workstation | As given | Each commit, first setup |
| TSC-06 to TSC-08 | `test_graph_build` | Fixtures | As given | Each commit |
| TSC-10 | `test_metrics` | Fixture graphs | As given | Each commit |
| TSC-12, TSC-13, TSC-23 | `test_perf` (marked slow) and the live run timings | Synthetic 10k graph, live run | As given | Before each release |
| TSC-14 to TSC-18, TSC-26, TSC-28, TSC-29 | `test_rules_*` | Fixtures, rule file | As given | Each commit |
| TSC-34 | `test_detector_benchmark` (marker `validation`), and `tools/validate_detectors.py` for the full report | Synthetic scenarios with planted campaigns | As given | Each commit, and the full report after each detector change |
| TSC-19 to TSC-21, TSC-32 | Playwright browser test | Rendered fixture HTML | As given | Each commit |
| TSC-22 | Playwright frame-time script in a headed browser on the owner's workstation (`tests/browser/`) | Live run output, 5k synthetic graph | 33 ms mean frame time | Before v0.1 is complete |
| TSC-33 | `test_xapi_mapping` with the Slice 0 fixtures | Recorded responses | As given | Each commit |
| TSC-24 | Secret scan of data, logs, and output | All files | 0 | Each release |
| TSC-25 | License check of installed packages and vendor files | Install, `vendor/` | 0 | Each release |
| TSC-31 | Build step | `vendor/` | Match | Each build |

## §4 Traceability matrix (v0.1)

| Capability | Background or decision | VISION | Requirements | Success criteria |
|---|---|---|---|---|
| Owner-only collection | DN §1, BG notes "Collect" | §1, §6 | FR-01 to FR-05, FR-11, FR-54, IR-01 to IR-03, IR-05, IR-06 | TSC-01, TSC-05, TSC-27, TSC-33 |
| Cost control | BG notes "Cost reality", DN §1 | Bet 4 | FR-12 to FR-16 | BSC-01, BSC-02a, TSC-03, TSC-04 |
| Store and deletion | V1-R8 | §6 | DR-01 to DR-04, DR-06 to DR-10 | BSC-06, TSC-06, TSC-09 |
| Graph and weights | BG-R1 to BG-R3, BG-R6 | §1 | FR-17 to FR-19, FR-21 to FR-23 | TSC-07, TSC-08 |
| Metrics and layout | BG notes "Process / Analyze" | §1 | FR-24, FR-25, FR-55, NFR-01, NFR-02 | TSC-10, TSC-12, TSC-13 |
| Signal states | BG-R4, BG-R5, DN §1 | Bet 2, §6 | FR-31 to FR-37, FR-57, TR-06 | BSC-05, TSC-14 to TSC-18, TSC-28, TSC-29, TSC-34 |
| HTML page | BG notes "Visualize" | Bet 3 | FR-40 to FR-50, SR-02 to SR-05, SR-07, SR-09 | TSC-19 to TSC-23, TSC-31, TSC-32 |
| License | Owner decision 2026-11-02 | header | AR-01, AR-04 | TSC-25 |

## §5 What does NOT count as success

```text
No iteration, MVP, or version is "shipped," "closed," or "complete" until at least one real call against the real external service has returned data. Stub-based tests are wiring checks, not validation.
```

These items do not show success:

1. **Tests that use only recorded or fake API responses.** They show that the wiring is correct, not that the collector operates correctly. For TSC-01, a live run is necessary.
2. **A render of a synthetic graph only.** Before v0.1 is complete, the map must show the owner's live network.
3. **A frame-rate number from a headless browser.** Headless browsers with virtual time stop the clock, and they frequently use software WebGL (research §6). For TSC-22, a headed browser on the owner's workstation is necessary.
4. **"The labels look correct."** A state is correct only when it agrees with a hand label in the calibration sample. Precision comes from BSC-04, not from a look at the map.
5. **A cost estimate that nobody compared with the actual bill.** The owner compared one day of X Console cost with the ledger on 2026-11-03 and closed the item (BSC-02b): the difference is not material.
6. **Rules that fire on fixture data that the rule author made for the rule.** Fixtures show that the logic is correct. The calibration sample shows the value.
7. **A state with no causes.** A `many_signals` state without a list of rules that fired is a defect, also when the state is correct.
8. **A compliance statement.** "The tool obeys the X terms" does not show success. The four controls in BSC-05 show it.
