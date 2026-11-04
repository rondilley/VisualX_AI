"""CLI wiring (FR-52, OPR-02): run --skip-collect, delete, collect --dry-run. No network."""

from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

from tests.helpers import make_env
from tests.unit.xapi_test_server import XApiTestServer
from visualx.cli import app
from visualx.collect.plan import run_collect
from visualx.collect.xapi import XApiClient

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def test_cli_run_skip_collect_and_delete(tmp_path):
    cfg, store, cache, log = make_env(tmp_path)
    with XApiTestServer() as srv:
        client = XApiClient(srv.base_url, token_provider=lambda: "tok", log=log, sleep=lambda s: None,
                            now=lambda: NOW.timestamp())
        run_collect(cfg, store, cache, log, client, lambda t: True, dry_run=False, run_id="c1", now=NOW)
        client.close()
    store.close()
    conf = str(tmp_path / "config")
    r = CliRunner().invoke(app, ["run", "--config", conf, "--skip-collect"])
    assert r.exit_code == 0, r.output
    assert "Run summary" in r.output and "HTML file:" in r.output
    assert "rules with no input" in r.output

    r = CliRunner().invoke(app, ["collect", "--config", conf, "--dry-run"])
    assert r.exit_code == 0, r.output

    r = CliRunner().invoke(app, ["delete", "--config", conf, "--user-id", "2002"])
    assert r.exit_code == 0, r.output
    assert "no stored file holds the deleted IDs" in r.output

    r = CliRunner().invoke(app, ["build", "--config", conf])
    assert r.exit_code == 0, r.output


def test_cli_stage_order_message(tmp_path):
    make_env(tmp_path)
    r = CliRunner().invoke(app, ["analyze", "--config", str(tmp_path / "config")])
    assert r.exit_code == 1
    assert "visualx build" in r.output


def test_cli_auth_status_asks_before_the_paid_call(tmp_path):
    # A-19 (review 20261103): no approval, no call, no ledger row.
    cfg, store, cache, log = make_env(tmp_path)
    r = CliRunner().invoke(app, ["auth", "status", "--config", str(tmp_path / "config")], input="n\n")
    assert r.exit_code != 0 and "Not approved" in r.output
    assert store.q("SELECT * FROM cost_ledger") == []


def test_cli_delete_refuses_an_empty_id(tmp_path):
    # A-01 (review 20261103) at the command line.
    make_env(tmp_path)
    r = CliRunner().invoke(app, ["delete", "--config", str(tmp_path / "config"), "--user-id", ""])
    assert r.exit_code != 0 and "not a valid user ID" in r.output
