"""Defense-in-depth tests: auth token issuance + Stripe backstop +
Issuing-authorization webhook."""

from __future__ import annotations

import db
import hook


def test_declaration_allow_issues_token(tmp_hermes_home):
    result = hook.process_declaration_for_api(
        job_id="j1", cost_center_id="default", projected_usd=0.50, ref="r1",
        task_id="t1",
    )
    assert result["action"] == "allow"
    assert "auth_token" in result
    assert result["expires_in"] == hook.AUTH_TOKEN_TTL_SEC
    assert db.get_active_token_count() == 1


def test_stripe_call_without_token_is_blocked(tmp_hermes_home):
    # Even with all the right shape, a stripe_* call without an auth token
    # is blocked. This is the "rogue agent" path.
    result = hook.on_pre_tool_call(
        "stripe_create_payment_intent",
        {"amount": 5000, "currency": "usd",
         "metadata": {"job_id": "j1"}},
        "task-1",
    )
    assert isinstance(result, dict) and result["action"] == "block"
    assert "no argus_auth_token" in result["message"]


def test_stripe_call_with_valid_token_is_allowed(tmp_hermes_home):
    decl = hook.process_declaration_for_api(
        job_id="j1", cost_center_id="default", projected_usd=0.50,
        ref=None, task_id="t1",
    )
    assert decl["action"] == "allow", decl
    token = decl["auth_token"]

    # The agent now invokes the Stripe skill with the token embedded.
    result = hook.on_pre_tool_call(
        "stripe_create_payment_intent",
        {
            "amount": 50,  # $0.50 in cents — within ±10% tolerance
            "currency": "usd",
            "metadata": {"argus_auth_token": token, "job_id": "j1"},
        },
        "task-1",
    )
    assert result is None  # ALLOW


def test_token_one_time_use(tmp_hermes_home):
    decl = hook.process_declaration_for_api(
        job_id="j1", cost_center_id="default", projected_usd=0.50,
        ref=None, task_id="t1",
    )
    token = decl["auth_token"]
    args = {
        "amount": 50,
        "currency": "usd",
        "metadata": {"argus_auth_token": token, "job_id": "j1"},
    }
    # First call succeeds.
    assert hook.on_pre_tool_call("stripe_create_payment_intent", args, "t1") is None
    # Replay attempt is blocked.
    second = hook.on_pre_tool_call("stripe_create_payment_intent", args, "t1")
    assert isinstance(second, dict) and second["action"] == "block"
    assert "already_consumed" in second["message"]


def test_token_amount_mismatch_blocks(tmp_hermes_home):
    decl = hook.process_declaration_for_api(
        job_id="j1", cost_center_id="default", projected_usd=0.50,
        ref=None, task_id="t1",
    )
    token = decl["auth_token"]
    # Token is for $0.50 but charge is $25 — way outside ±10% tolerance.
    result = hook.on_pre_tool_call(
        "stripe_create_payment_intent",
        {
            "amount": 2500,
            "currency": "usd",
            "metadata": {"argus_auth_token": token, "job_id": "j1"},
        },
        "t1",
    )
    assert isinstance(result, dict) and result["action"] == "block"
    assert "amount_mismatch" in result["message"]


def test_token_job_mismatch_blocks(tmp_hermes_home):
    decl = hook.process_declaration_for_api(
        job_id="j-alpha", cost_center_id="default", projected_usd=0.50,
        ref=None, task_id="t1",
    )
    token = decl["auth_token"]
    # Token was for j-alpha but the Stripe call says j-beta.
    result = hook.on_pre_tool_call(
        "stripe_create_payment_intent",
        {
            "amount": 50,
            "currency": "usd",
            "metadata": {"argus_auth_token": token, "job_id": "j-beta"},
        },
        "t1",
    )
    assert isinstance(result, dict) and result["action"] == "block"
    assert "job_mismatch" in result["message"]


def test_missing_declaration_is_blocked_not_allowed(tmp_hermes_home):
    """REGRESSION: the old hook silently allowed argus_request_spend
    calls with missing args. New default is BLOCK so a rogue agent
    can't pass through with empty args."""
    result = hook.on_pre_tool_call(
        "argus_request_spend", {}, "task-1",
    )
    assert isinstance(result, dict) and result["action"] == "block"
    assert "missing required args" in result["message"]
