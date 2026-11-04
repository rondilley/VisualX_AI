"""Measure the Grok x_search source against the official X API data in the same store (no API call).

The X API data is the reference. For each cached Grok response, the adapter's own check
(verify_items) gives the accepted items. Then:

* precision: accepted Grok items whose post ID is in the X API data with the same
  interaction type and the same direction (owner as source or as target);
* recall: X API interactions in the Grok window that Grok also gave;
* the post-ID check alone: accepted post IDs that exist in the X API data at all.

Run: .venv/Scripts/python tools/validate_grok.py [--config config]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visualx.collect.grok import verify_items  # noqa: E402
from visualx.config import load  # noqa: E402
from visualx.store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config"))
    a = ap.parse_args()
    cfg = load(a.config)
    store = Store(cfg.store_path)
    ego = store.ego_id
    handle = (store.get_meta("ego_handle") or cfg.get("owner.handle") or "").lstrip("@")
    handles = {uid: (p["handle"] or "").casefold() for uid, p in store.profiles().items() if p.get("handle")}

    # Reference: X API interactions that come from a post (the Grok source sees only posts).
    ref = {}
    for r in store.q("SELECT type, source_user_id, target_user_id, source_post_id, interaction_created_at "
                     "FROM interactions WHERE adapter='xapi' AND source_post_id IS NOT NULL"):
        direction = "out" if r["source_user_id"] == ego else ("in" if r["target_user_id"] == ego else "other")
        other = r["target_user_id"] if direction == "out" else r["source_user_id"]
        # One post can reply to one account and mention others, so the key holds the other account.
        ref[(r["source_post_id"], direction, handles.get(other) or other)] = (r["type"], r["interaction_created_at"])
    ref_posts = {k[0] for k in ref}

    rows = store.q("SELECT file, request_hash FROM cache_index WHERE endpoint='xai /v1/responses'")
    accepted = []
    windows = []
    for r in rows:
        p = cfg.cache_dir / r["file"]
        if not p.exists():
            continue
        with gzip.open(p, "rt", encoding="utf-8") as f:
            resp = json.load(f)
        # The window is in the request, which the cache does not keep; take it from the items' times.
        items, _drop = verify_items(resp, date(2000, 1, 1), date(2100, 1, 1), handle)
        for v in items:
            direction = "out" if v.author.casefold() == handle.casefold() else "in"
            accepted.append((v.post_id, direction, v.type, v.author.casefold(), v.target.casefold()))
    if not accepted:
        print("No cached Grok responses in the store.")
        return 1
    from visualx.collect.grok import snowflake_time
    times = sorted(snowflake_time(pid) for pid, *_ in accepted)
    lo, hi = times[0][:10], times[-1][:10]
    windows.append((lo, hi))

    exists = sum(1 for pid, *_ in accepted if pid in ref_posts)
    same = 0
    wrong_type = Counter()
    for pid, d, t, au, tg in accepted:
        m = ref.get((pid, d, tg if d == "out" else au))
        if m is None:
            continue
        if m[0] == t:
            same += 1
        else:
            wrong_type[f"{t} (X API: {m[0]})"] += 1
    uniq = {(pid, d, tg if d == "out" else au) for pid, d, t, au, tg in accepted}
    in_window = {k: v for k, v in ref.items() if v[1] and lo <= v[1][:10] <= hi and k[1] in ("in", "out")}
    found = sum(1 for k in in_window if k in uniq)

    out_ref = {k for k in in_window if k[1] == "out"}
    in_ref = {k for k in in_window if k[1] == "in"}
    print(f"Grok items accepted by the citation check: {len(accepted)} ({len(uniq)} distinct post, direction, and account)")
    print(f"Grok item time range: {lo} to {hi}")
    print(f"Post ID exists in the X API data:            {exists}/{len(accepted)} = {exists / len(accepted):.0%}")
    matched = sum(1 for pid, d, t, au, tg in accepted if (pid, d, tg if d == "out" else au) in ref)
    print(f"Same post, direction, and account as the X API: {matched}/{len(accepted)} = {matched / len(accepted):.0%}")
    print(f"   of these, the same type:                    {same}/{matched} = {same / max(1, matched):.0%}")
    for k, n in wrong_type.most_common():
        print(f"   different type: {k} x{n}")
    print(f"Recall, all X API post interactions in the range: {found}/{len(in_window)} = {found / max(1, len(in_window)):.0%}")
    print(f"   inbound (other accounts to the owner): {sum(1 for k in in_ref if k in uniq)}/{len(in_ref)}")
    print(f"   outbound (owner to other accounts):    {sum(1 for k in out_ref if k in uniq)}/{len(out_ref)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
