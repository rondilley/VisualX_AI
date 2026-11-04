"""Command-line interface (FR-52, OPR-02). One subcommand for each stage, and `run`.

Only `collect`, `sweep` (and `auth status`) make network calls to X. All other stages
read and write the local store.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer

from . import __version__
from .cache import Cache
from .config import Config, ConfigError, load
from .logs import RunLog, load_hmac_key
from .store import LockError, StaleError, Store, lock, new_id, now_iso

app = typer.Typer(add_completion=False, no_args_is_help=True, help="VisualX_AI: a private map of your X network.")
auth_app = typer.Typer(no_args_is_help=True, help="Sign in to X, show the account, or remove the tokens.")
app.add_typer(auth_app, name="auth")
calibrate_app = typer.Typer(no_args_is_help=True, help="Label a sample of accounts and measure the precision of the states (FR-38).")
app.add_typer(calibrate_app, name="calibrate")

CONFIG_OPT = typer.Option("config", "--config", help="Folder with visualx.toml and the YAML files.")


class Ctx:
    def __init__(self, config_dir: str, run_id: str | None = None):
        try:
            self.cfg: Config = load(config_dir)
        except ConfigError as e:
            raise typer.BadParameter(str(e)) from e
        self.config_dir = Path(config_dir)
        self.run_id = run_id or new_id("run")
        self.store = Store(self.cfg.store_path)
        self.cache = Cache(self.store, self.cfg.cache_dir)
        self.log = RunLog(self.cfg.runs_dir / self.run_id / "log.jsonl", load_hmac_key(self.cfg.data_dir))


def _fail(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _guarded(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (StaleError, LockError) as e:
        _fail(str(e))


@contextmanager
def _locked(ctx: "Ctx") -> Iterator[None]:
    """Hold the data-folder lock (AD-01). A held lock stops the command with a message."""
    try:
        cm = lock(ctx.cfg.data_dir)
        cm.__enter__()
    except LockError as e:
        _fail(str(e))
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def _approve(text: str) -> bool:
    typer.echo(text)
    return typer.confirm("Approve this step?", default=False)


def version_cb(value: bool) -> None:
    if value:
        typer.echo(f"visualx {__version__}")
        raise typer.Exit()


@app.callback()
def main(version: bool = typer.Option(False, "--version", callback=version_cb, is_eager=True, help="Show the version.")) -> None:
    """VisualX_AI command-line tool."""


# --------------------------------------------------------------------------- auth
@auth_app.command("login")
def auth_login(config: str = CONFIG_OPT) -> None:
    """Sign in with OAuth 2.0 PKCE. The tokens go to the Windows Credential Manager."""
    from . import auth

    cfg = load(config)
    try:
        auth.login(cfg)
    except auth.AuthError as e:
        _fail(str(e))
    typer.echo("Signed in. The tokens are in the Windows Credential Manager.")


@auth_app.command("status")
def auth_status(
    config: str = CONFIG_OPT,
    yes: bool = typer.Option(False, "--yes", help="Do not ask for approval of the paid call."),
) -> None:
    """Make one live GET /2/users/me call (one user read, $0.010) and show the owner account."""
    from . import auth
    from .collect.xapi import XApiClient, XApiError

    ctx = Ctx(config)
    price = float(ctx.cfg.get("prices.user_read"))
    # A-19 (review 20261103): a paid call gets the owner's approval and a ledger row.
    if not yes and not typer.confirm(f"One paid call: GET /2/users/me, 1 user read at ${price:.3f}. Continue?"):
        _fail("Not approved. No call was made.")
    try:
        with _locked(ctx):
            lid = ctx.store.conn.execute(
                "INSERT INTO cost_ledger(run_id,step,endpoint,billing_class,resources,unit_price,cost,reserved,"
                "response_code,at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ctx.run_id, 0, "/2/users/me", "user_read", 0, price, price, price, None, now_iso()),
            ).lastrowid
            client = XApiClient(token_provider=lambda: auth.get_access_token(ctx.cfg),
                                on_refresh=lambda: auth.refresh_access_token(ctx.cfg), log=ctx.log)
            try:
                me = auth.status(ctx.cfg, client)
            except XApiError as e:
                if e.status is not None:
                    ctx.store.conn.execute("UPDATE cost_ledger SET cost=0, response_code=? WHERE id=?", (e.status, lid))
                raise auth.AuthError(str(e)) from e
            ctx.store.conn.execute("UPDATE cost_ledger SET resources=1, response_code=200 WHERE id=?", (lid,))
            auth.record_owner(ctx.cfg, ctx.store, str(me["user_id"]), str(me.get("handle") or ""))
    except auth.AuthError as e:
        _fail(str(e))
    typer.echo(f"Owner account: @{me.get('handle')} (ID {me['user_id']})")


@auth_app.command("grok-key")
def auth_grok_key(remove: bool = typer.Option(False, "--remove", help="Delete the stored key.")) -> None:
    """Store the xAI API key (for --source grok) in the Windows Credential Manager."""
    from . import auth
    from .collect import grok

    try:
        if remove:
            typer.echo("Key removed." if grok.delete_api_key() else "No key was stored.")
            return
        grok.save_api_key(typer.prompt("xAI API key", hide_input=True))
    except auth.AuthError as e:
        _fail(str(e))
    typer.echo("The xAI API key is in the Windows Credential Manager.")


@auth_app.command("revoke")
def auth_revoke(config: str = CONFIG_OPT) -> None:
    """Revoke the token at X and remove it from the Windows Credential Manager."""
    from . import auth

    cfg = load(config)
    try:
        res = auth.revoke(cfg)
    except auth.AuthError as e:
        _fail(str(e))
    # A-18 (review 20261103): report what happened, not always "removed".
    typer.echo("Revoked at X." if res["revoked_at_x"] else f"Not revoked at X: {res['error'] or 'no stored token'}.")
    typer.echo("Removed from the Credential Manager." if res["deleted_from_keyring"] else "No token was in the Credential Manager.")
    if res["error"]:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- stages
def _apply_overrides(ctx: Ctx, days: int | None, cap: float | None, seeds: int | None = None) -> None:
    if seeds is not None:
        if seeds < 0:
            _fail("--seeds must be 0 or more")
        ctx.cfg.raw.setdefault("collect", {})["seeds"] = seeds
    if days is not None:
        ctx.cfg.raw.setdefault("collect", {})["window_days"] = days
    if cap is not None:
        if cap <= 0:
            _fail("--cap must be more than 0")
        ctx.cfg.raw.setdefault("collect", {})["cap_usd"] = cap


def _do_collect(ctx: Ctx, dry_run: bool, approve: Callable[[str], bool] = _approve, source: str = "xapi",
                full: bool = False) -> Any:
    from . import auth

    if source == "grok":
        from .collect.grok import GrokClient, run_grok_collect

        from .collect.grok import load_api_key

        gclient = None if dry_run else GrokClient(key_provider=lambda: load_api_key(ctx.cfg.root), log=ctx.log)
        try:
            gres = run_grok_collect(ctx.cfg, ctx.store, ctx.cache, ctx.log, gclient, approve, dry_run, ctx.run_id)
        finally:
            if gclient is not None:
                gclient.close()
        typer.echo("\n".join(gres.summary_lines()))
        return gres
    if source != "xapi":
        _fail("--source must be 'xapi' or 'grok'")
    from .collect.plan import run_collect
    from .collect.xapi import XApiClient

    client = None
    if not dry_run:
        client = XApiClient(token_provider=lambda: auth.get_access_token(ctx.cfg),
                            on_refresh=lambda: auth.refresh_access_token(ctx.cfg), log=ctx.log)
    try:
        result = run_collect(ctx.cfg, ctx.store, ctx.cache, ctx.log, client, approve, dry_run, ctx.run_id, full=full)
    finally:
        if client is not None:
            client.close()
    typer.echo("\n".join(result.summary_lines()))
    return result


@app.command()
def collect(
    config: str = CONFIG_OPT,
    days: int = typer.Option(None, "--days", help="Collection window in days."),
    cap: float = typer.Option(None, "--cap", help="Spend cap for this run, in USD."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and the estimate. Make no paid call."),
    source: str = typer.Option("xapi", "--source", help="xapi (official X API, default) or grok (xAI x_search, unverified)."),
    full: bool = typer.Option(False, "--full", help="Collect the full window again. Default: after a complete run, only new posts (FR-08)."),
    seeds: int = typer.Option(None, "--seeds", help="Also collect the recent posts of the top N contacts by tie weight "
                              "(FR-07, step 6, paid at post_read). 0 = off. Default: collect.seeds in the config."),
) -> None:
    """Collect the interactions around the owner account (paid, with approval at each step)."""
    ctx = Ctx(config)
    _apply_overrides(ctx, days, cap, seeds)
    with _locked(ctx):
        _do_collect(ctx, dry_run, source=source, full=full)


@app.command("import-archive")
def import_archive(
    path: Path = typer.Argument(..., help="The X data archive: the .zip file or the extracted folder."),
    config: str = CONFIG_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="Read the archive and show the report. Write nothing."),
    days: int = typer.Option(None, "--days", help="Only posts from the last N days. Default: collect.window_days. 0 = all posts."),
) -> None:
    """Import the owner's outbound posts from the X data archive (FR-06). Free: no network call."""
    from .collect.archive import ArchiveError, run_import

    ctx = Ctx(config)
    with _locked(ctx):
        try:
            rep = _guarded(lambda: run_import(ctx.cfg, ctx.store, ctx.log, path, ctx.run_id, dry_run=dry_run, days=days))
        except ArchiveError as e:
            _fail(str(e))
    typer.echo("\n".join(rep.summary_lines()))
    if not dry_run:
        typer.echo("Run 'visualx run --skip-collect' to make a new map with these events.")


def _build(ctx: Ctx) -> dict[str, Any]:
    from .build import run_build

    return run_build(ctx.cfg, ctx.store, ctx.log, ctx.run_id)


def _analyze(ctx: Ctx) -> dict[str, Any]:
    from .analyze import run_analyze

    return run_analyze(ctx.cfg, ctx.store, ctx.log, ctx.run_id)


def _score(ctx: Ctx) -> dict[str, Any]:
    from .score.engine import run_score

    out = run_score(ctx.cfg, ctx.store, ctx.log)
    if not ctx.cfg.get("data.retain_text", True):
        n = ctx.store.drop_text()
        files = ctx.cache.drop_text()
        typer.echo(f"retain_text = false: removed the text of {n} interactions and from {files} cache files (AD-11).")
    return out


def _render(ctx: Ctx, out: str | None = None, redact: bool = False) -> Path:
    from .render.render import run_render

    return run_render(ctx.cfg, ctx.store, ctx.log, Path(out) if out else None, redact=redact)


def _stage(name: str, fn: Callable[[Ctx], Any], config: str) -> Any:
    ctx = Ctx(config)
    with _locked(ctx):
        res = _guarded(lambda: fn(ctx))
    typer.echo(json.dumps(res, default=str, indent=2) if isinstance(res, dict) else str(res))
    return res


@app.command()
def build(config: str = CONFIG_OPT) -> None:
    """Make nodes and edges from the stored interactions."""
    _stage("build", _build, config)


@app.command()
def analyze(config: str = CONFIG_OPT) -> None:
    """Calculate metrics (ego removed) and the layout."""
    _stage("analyze", _analyze, config)


@app.command()
def score(config: str = CONFIG_OPT) -> None:
    """Apply the signal rules and the owner overrides."""
    _stage("score", _score, config)


@app.command()
def render(
    config: str = CONFIG_OPT,
    out: str = typer.Option(None, "--out", help="Output HTML path."),
    redact: bool = typer.Option(False, "--redact", help="Structure only: no handle, name, ID, profile, topic, "
                                "signal state, or finding (threat T7). For a file that you show to other persons."),
) -> None:
    """Write the single offline HTML file."""
    _stage("render", lambda c: _render(c, out, redact), config)


@app.command()
def export(
    config: str = CONFIG_OPT,
    fmt: str = typer.Option("gexf", "--format", help="gexf (Gephi) or xnm (x-network-map interactions.json and "
                            "profiles.json)."),
    out: str = typer.Option(None, "--out", help="Output folder. Default: data/exports."),
) -> None:
    """Export the graph with no signal states (FR-51). A deletion also deletes the export files."""
    from .export import ExportError, run_export

    ctx = Ctx(config)
    with _locked(ctx):
        try:
            paths = _guarded(lambda: run_export(ctx.cfg, ctx.store, ctx.log, fmt, Path(out) if out else None))
        except ExportError as e:
            _fail(str(e))
    for p in paths:
        typer.echo(f"Wrote {p}")
    typer.echo("The export describes third parties. It holds no signal state, rule result, or finding.")


def summary(ctx: Ctx, html: Path | None) -> str:
    """End-of-run summary (OR-02)."""
    s = ctx.store
    one = lambda sql, p=(): (s.q1(sql, p) or [0])[0]  # noqa: E731
    states = {r["state"]: r["n"] for r in s.q("SELECT state, COUNT(*) AS n FROM node_states GROUP BY state")}
    lines = [
        "Run summary",
        f"  run ID:                 {ctx.run_id}",
        f"  interactions:           {one('SELECT COUNT(*) FROM interactions')}",
        f"  nodes:                  {one('SELECT COUNT(*) FROM nodes')}",
        f"  edges:                  {one('SELECT COUNT(*) FROM edges')}",
        f"  hidden (no profile):    {one('SELECT COUNT(*) FROM nodes WHERE profile_status=? AND is_ego=0', ('missing',))}",
        "  states:                 " + ", ".join(f"{k} {v}" for k, v in sorted(states.items())),
        f"  rules fired:            {one('SELECT COUNT(*) FROM rule_results')}",
        f"  rules with no input:    {one('SELECT COUNT(*) FROM rule_skips')}",
        "  pattern findings:       " + (", ".join(
            f"{r['detector']} {r['n']}" for r in s.q("SELECT detector, COUNT(*) AS n FROM findings GROUP BY detector ORDER BY detector")
        ) or "none"),
        f"  cost of this run (USD): {s.ledger_total(ctx.run_id):.3f}",
    ]
    if html:
        lines.append(f"  HTML file:              {html}")
    return "\n".join(lines)


@app.command()
def run(
    config: str = CONFIG_OPT,
    days: int = typer.Option(None, "--days", help="Collection window in days."),
    cap: float = typer.Option(None, "--cap", help="Spend cap for this run, in USD."),
    skip_collect: bool = typer.Option(False, "--skip-collect", help="Use the data in the store. Make no paid call."),
    source: str = typer.Option("xapi", "--source", help="xapi (official X API, default) or grok (xAI x_search, unverified)."),
    full: bool = typer.Option(False, "--full", help="Collect the full window again. Default: after a complete run, only new posts (FR-08)."),
    seeds: int = typer.Option(None, "--seeds", help="Also collect the recent posts of the top N contacts by tie weight "
                              "(FR-07, step 6, paid at post_read). 0 = off. Default: collect.seeds in the config."),
) -> None:
    """collect, build, analyze, score, render in sequence (OPR-02)."""
    ctx = Ctx(config)
    _apply_overrides(ctx, days, cap, seeds)
    with _locked(ctx):
        if not skip_collect:
            res = _do_collect(ctx, dry_run=False, source=source, full=full)
            if not res.complete and not typer.confirm(
                f"Collection stopped ({res.stopped_reason}). Build a map from the data that is in the store?",
                default=False,
            ):
                _fail("No later stage ran. The next collect continues from the cursor.")
        html = _guarded(lambda: (_build(ctx), _analyze(ctx), _score(ctx), _render(ctx))[-1])
    typer.echo(summary(ctx, html))


def _rerender_after_delete(ctx: Ctx) -> str | None:
    """The new render after a deletion (AD-10). Used by `delete` and `sweep`."""
    if ctx.store.latest_stage_run("build") is None:
        return None
    _build(ctx)
    _analyze(ctx)
    _score(ctx)
    return str(_render(ctx))


def _orphan_maps(ctx: Ctx) -> list[Path]:
    """Map files in the renders folder that the renders table does not list."""
    known = {Path(r["path"]).resolve() for r in ctx.store.q("SELECT path FROM renders")}
    return [p for p in sorted(ctx.cfg.renders_dir.glob("map_*.html")) if p.resolve() not in known]


@app.command()
def delete(
    config: str = CONFIG_OPT,
    user_id: list[str] = typer.Option(None, "--user-id", help="X user ID to delete (repeat for more)."),
    post_id: list[str] = typer.Option(None, "--post-id", help="X post ID to delete (repeat for more)."),
    no_rerender: bool = typer.Option(False, "--no-rerender", help="Do not make a new render after the deletion."),
) -> None:
    """Delete an ID from every location (DR-04, AD-10)."""
    from .delete import delete_ids, validate_ids

    if not user_id and not post_id:
        _fail("Give --user-id or --post-id.")
    try:
        if user_id:
            validate_ids(list(user_id), "user")
        if post_id:
            validate_ids(list(post_id), "post")
    except ValueError as e:
        _fail(str(e))
    ctx = Ctx(config)

    def rerender() -> str | None:
        return _rerender_after_delete(ctx)

    def orphans() -> list[Path]:
        return _orphan_maps(ctx)

    with _locked(ctx):
        reports = []
        if user_id:
            reports.append(delete_ids(ctx.store, ctx.cache, ctx.config_dir, list(user_id), "user",
                                      None if no_rerender else rerender, extra_files=orphans()))
        if post_id:
            reports.append(delete_ids(ctx.store, ctx.cache, ctx.config_dir, list(post_id), "post",
                                      None if no_rerender else rerender, extra_files=orphans()))
    remaining = []
    for r in reports:
        typer.echo(
            f"Deleted {len(r.ids)} {r.kind} ID(s): {r.store_rows} store rows, {r.redacted_cells} redacted cells, "
            f"{r.cache_files_scrubbed} cache files rewritten, {r.overrides_removed} overrides, "
            f"{r.calibration_rows_removed} calibration sheet rows, {len(r.exports_deleted)} export files, "
            f"{len(r.renders_deleted)} HTML files deleted" + (f", new render {r.rerendered}" if r.rerendered else "")
        )
        remaining += r.remaining
    ctx.log.write("delete", count=sum(len(r.ids) for r in reports), remaining=len(remaining))
    if remaining:
        _fail("The ID is still in: " + ", ".join(remaining))
    typer.echo("Check complete: no stored file holds the deleted IDs (TSC-09).")


@app.command()
def sweep(
    config: str = CONFIG_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the estimate. Make no API call."),
    cap: float = typer.Option(None, "--cap", help="Spend cap for this sweep, in USD. Default: collect.cap_usd in the config."),
    limit: int = typer.Option(None, "--limit", help="Check at most N posts, oldest first. The next sweep continues "
                              "after the last post that this sweep checked."),
) -> None:
    """Find stored posts that are no longer on X and delete them locally (DR-05; paid, with approval)."""
    from . import auth
    from .collect.sweep import run_sweep
    from .collect.xapi import XApiClient

    if cap is not None and cap <= 0:
        _fail("--cap must be more than 0")
    if limit is not None and limit < 1:
        _fail("--limit must be 1 or more")
    ctx = Ctx(config)
    client = None
    if not dry_run:
        client = XApiClient(token_provider=lambda: auth.get_access_token(ctx.cfg),
                            on_refresh=lambda: auth.refresh_access_token(ctx.cfg), log=ctx.log)
    try:
        with _locked(ctx):
            res = _guarded(lambda: run_sweep(
                ctx.cfg, ctx.store, ctx.cache, ctx.log, client, _approve, dry_run=dry_run, run_id=ctx.run_id,
                cap=cap, limit=limit, config_dir=ctx.config_dir,
                rerender=lambda: _rerender_after_delete(ctx), orphans=lambda: _orphan_maps(ctx),
            ))
    finally:
        if client is not None:
            client.close()
    typer.echo("\n".join(res.summary_lines()))
    rep = res.delete_report
    if rep is not None and rep.remaining:
        _fail("A deleted post ID is still in: " + ", ".join(rep.remaining))
    if rep is not None:
        typer.echo("Check complete: no stored file holds the deleted post IDs (TSC-09).")


# --------------------------------------------------------------------------- calibrate

LABEL_HELP = """How to label:
  1. Open the sheet in a spreadsheet program or a text editor.
  2. For each row, open profile_url and look at the account on X.
  3. In the label column, write one word:
       concern  the account shows inauthentic, abusive, or impersonation behavior that you want to know about
       fine     a normal account
       unsure   you cannot decide
  4. A note is optional. Leave a row empty to skip it.
  5. Save the file as CSV, then run: visualx calibrate import <sheet>
The sheet does not show the state of the tool, so your label is independent of it."""


def _calibrate(fn: Callable[[Ctx], Any], config: str) -> Any:
    from .score.calibrate import CalibrationError

    ctx = Ctx(config)
    with _locked(ctx):
        try:
            return fn(ctx)
        except CalibrationError as e:
            _fail(str(e))


@calibrate_app.command("sample")
def calibrate_sample(
    config: str = CONFIG_OPT,
    per_state: int = typer.Option(20, "--per-state", help="Accounts from each of many_signals and some_signals (all if fewer)."),
    per_detector: int = typer.Option(10, "--per-detector", help="Finding members from each detector that are not in a state above."),
    no_signals: int = typer.Option(30, "--no-signals", help="Random no_signals accounts (to measure what the tool misses)."),
    seed: int = typer.Option(None, "--seed", help="Random seed for the sample (default: a new sample each time)."),
) -> None:
    """Take a stratified sample of scored accounts and write a blind labeling sheet."""
    from .score.calibrate import sample

    res = _calibrate(lambda c: sample(c.store, per_state, per_detector, no_signals, seed), config)
    typer.echo(f"Batch {res['batch']}: {res['accounts']} accounts.")
    for st, n in res["strata"].items():
        typer.echo(f"  {st}: {n}")
    typer.echo(f"Sheet: {res['sheet']}")
    typer.echo("The sheet names other accounts. Keep it private, like the map.")
    typer.echo(LABEL_HELP)


@calibrate_app.command("import")
def calibrate_import(sheet: Path = typer.Argument(..., help="The labeling sheet (CSV)."), config: str = CONFIG_OPT) -> None:
    """Read your labels from a labeling sheet."""
    from .score.calibrate import import_labels

    res = _calibrate(lambda c: import_labels(c.store, sheet), config)
    typer.echo(f"Imported {res['imported']} labels: " + ", ".join(f"{k} {v}" for k, v in res["labels"].items()))
    typer.echo("Run: visualx calibrate report")


@calibrate_app.command("report")
def calibrate_report(
    config: str = CONFIG_OPT,
    batch: str = typer.Option(None, "--batch", help="One batch only (default: all batches)."),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
) -> None:
    """Show the precision of each state, rule, and detector from your labels."""
    from .score.calibrate import report, report_text

    res = _calibrate(lambda c: report(c.store, batch), config)
    typer.echo(json.dumps(res, indent=2) if as_json else report_text(res))


# --------------------------------------------------------------------------- snapshot

snapshot_app = typer.Typer(no_args_is_help=True, help="Compare the network between runs. Each render keeps a snapshot.")
app.add_typer(snapshot_app, name="snapshot")


def _snap(fn: Callable[[Ctx], Any], config: str) -> Any:
    from .snapshot import SnapshotError

    ctx = Ctx(config)
    with _locked(ctx):
        try:
            return fn(ctx)
        except SnapshotError as e:
            _fail(str(e))


@snapshot_app.command("list")
def snapshot_list(config: str = CONFIG_OPT) -> None:
    """List the snapshots: ID, time, run, and the number of accounts."""
    from .snapshot import list_snapshots

    rows = _snap(lambda c: list_snapshots(c.store), config)
    if not rows:
        typer.echo("No snapshot. Each 'visualx render' (and 'run') keeps one.")
    for r in rows:
        typer.echo(f"{r['snapshot_id']}  {r['created_at']}  run {r['run_id'] or '-'}  {r['accounts']} accounts")


@snapshot_app.command("diff")
def snapshot_diff(
    config: str = CONFIG_OPT,
    base: str = typer.Option(None, "--from", help="Earlier snapshot ID. Default: the snapshot before --to."),
    head: str = typer.Option(None, "--to", help="Later snapshot ID. Default: the newest snapshot."),
) -> None:
    """Counts of the changes between two snapshots. The accounts are in the map (Overview, Changes)."""
    from .snapshot import SnapshotError, diff, diff_text, list_snapshots, previous

    def go(c: Ctx) -> str:
        rows = {r["snapshot_id"]: r for r in list_snapshots(c.store)}
        if not rows:
            raise SnapshotError("No snapshot. Run 'visualx render' first.")
        h = head or max(rows.values(), key=lambda r: (r["created_at"], r["snapshot_id"]))["snapshot_id"]
        b = base or previous(c.store, h)
        if h not in rows or b is None or b not in rows:
            raise SnapshotError("Two snapshots are necessary. Give --from and --to from 'visualx snapshot list'.")
        return diff_text(diff(c.store, b, h), rows[b]["created_at"], rows[h]["created_at"])

    typer.echo(_snap(go, config))


if __name__ == "__main__":
    app()
