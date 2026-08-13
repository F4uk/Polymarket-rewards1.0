"""Merge monitor state-machine edges with fake API/Relayer clients only."""

import threading
from unittest.mock import MagicMock, patch

from engine.monitor import OrderMonitor
from engine.take_profit import remaining_fifo_lots
from api.polymarket_api import OrderRejected


CID = "0x" + "c" * 64
CID_B = "0x" + "b" * 64


def _positions(yes=20.0, no=12.0):
    return [
        {"conditionId": CID, "asset": "yes", "outcome": "Yes", "size": yes},
        {"conditionId": CID, "asset": "no", "outcome": "No", "size": no},
    ]


def _positions_for(cid, yes_asset, no_asset, yes=20.0, no=12.0):
    return [
        {"conditionId": cid, "asset": yes_asset, "outcome": "Yes", "size": yes},
        {"conditionId": cid, "asset": no_asset, "outcome": "No", "size": no},
    ]


def _monitor():
    api, db = MagicMock(), MagicMock()
    api.signature_type = 3
    api.trading_enabled = True
    api.private_key = "0x" + "1" * 64
    api.get_funder.return_value = "0xFunder"
    api.get_user_positions.return_value = _positions()
    api.get_open_orders.side_effect = [
        [
            {"id": "sell-yes", "side": "SELL", "asset_id": "yes"},
            {"id": "sell-no", "side": "SELL", "asset_id": "no"},
        ],
        [],
    ]
    db.get_template_for.return_value = {"merge_enabled": True, "merge_min_shares": 1}
    db.get_unresolved_merges.return_value = []
    db.get_confirmed_pending_merges.return_value = []
    db.get_merge_inventory_barriers.return_value = []
    db.create_merge_operation.return_value = 17
    return OrderMonitor(api, db, "0xWallet"), api, db


def test_merge_cancels_sell_reservations_refetches_and_submits_only_once():
    monitor, api, db = _monitor()
    client = MagicMock()
    client.submit_merge.return_value.transaction_id = "relayer-1"
    client.submit_merge.return_value.transaction_hash = "0xhash"

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    api.cancel_orders.assert_called_once_with(["sell-yes", "sell-no"])
    assert api.get_user_positions.call_count == 2
    db.create_merge_operation.assert_called_once_with(
        "0xWallet", "0xFunder", CID, "yes", "no", 12.0
    )
    client.submit_merge.assert_called_once_with("0xFunder", CID, 12.0)
    assert db.update_merge_operation.call_args_list[-1].args[:2] == (17, "submitted")


def test_merge_waits_when_sell_cancellation_has_not_reconciled():
    monitor, api, db = _monitor()
    # The second read still reserves YES; no operation must be created.
    api.get_open_orders.side_effect = [
        [{"id": "sell-yes", "side": "SELL", "asset_id": "yes"}],
        [{"id": "sell-yes", "side": "SELL", "asset_id": "yes"}],
    ]
    client = MagicMock()

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    db.create_merge_operation.assert_not_called()
    client.submit_merge.assert_not_called()


def test_unresolved_submitted_merge_is_reconciled_without_duplicate_submission():
    monitor, api, db = _monitor()
    db.get_unresolved_merges.side_effect = [
        [{"id": 17, "condition_id": CID, "status": "submitted", "relayer_id": "relayer-1"}],
        [{"id": 17, "condition_id": CID, "status": "submitted", "relayer_id": "relayer-1"}],
        [{"id": 17, "condition_id": CID, "status": "submitted", "relayer_id": "relayer-1"}],
    ]
    client = MagicMock()
    client.get_transaction.return_value = {"state": "STATE_PENDING"}

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    client.get_transaction.assert_called_once_with("relayer-1")
    client.submit_merge.assert_not_called()
    # Other conditions must still be scanned; this test's only inventory is A,
    # so no new operation can be submitted for it.
    api.get_user_positions.assert_called_once_with("0xFunder")


def test_pending_a_blocks_only_a_and_b_still_submits_merge():
    monitor, api, db = _monitor()
    pending_a = {
        "id": 17,
        "condition_id": CID,
        "status": "submitted",
        "relayer_id": "relayer-a",
        "requested_qty": 12,
    }
    db.get_unresolved_merges.side_effect = [
        [pending_a],  # relayer reconciliation
        [pending_a],  # planned reconciliation
        [pending_a],  # condition-scoped block snapshot
    ]
    api.get_user_positions.side_effect = [
        _positions_for(CID, "a-yes", "a-no")
        + _positions_for(CID_B, "b-yes", "b-no", yes=9, no=7),
        _positions_for(CID_B, "b-yes", "b-no", yes=9, no=7),
    ]
    api.get_open_orders.return_value = []
    db.create_merge_operation.return_value = 29
    client = MagicMock()
    client.get_transaction.return_value = {"state": "STATE_PENDING"}
    client.submit_merge.return_value.transaction_id = "relayer-b"

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    client.get_transaction.assert_called_once_with("relayer-a")
    db.create_merge_operation.assert_called_once_with(
        "0xWallet", "0xFunder", CID_B, "b-yes", "b-no", 7.0
    )
    client.submit_merge.assert_called_once_with("0xFunder", CID_B, 7.0)


def test_indeterminate_a_blocks_only_a_and_b_still_submits_merge():
    monitor, api, db = _monitor()
    indeterminate_a = {
        "id": 17,
        "condition_id": CID,
        "status": "planned",
        "requested_qty": 12,
        "error": "relayer submission indeterminate: timeout",
    }
    db.get_unresolved_merges.side_effect = [
        [indeterminate_a],
        [indeterminate_a],
        [indeterminate_a],
    ]
    api.get_user_positions.side_effect = [
        _positions_for(CID, "a-yes", "a-no")
        + _positions_for(CID_B, "b-yes", "b-no", yes=9, no=7),
        _positions_for(CID_B, "b-yes", "b-no", yes=9, no=7),
    ]
    api.get_open_orders.return_value = []
    db.create_merge_operation.return_value = 29
    client = MagicMock()
    client.submit_merge.return_value.transaction_id = "relayer-b"

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    db.create_merge_operation.assert_called_once_with(
        "0xWallet", "0xFunder", CID_B, "b-yes", "b-no", 7.0
    )
    client.submit_merge.assert_called_once_with("0xFunder", CID_B, 7.0)


def test_failed_relayer_state_keeps_inventory_and_never_marks_confirmed():
    monitor, api, db = _monitor()
    op = {"id": 17, "condition_id": CID, "requested_qty": 12, "status": "submitted", "relayer_id": "r"}
    db.get_unresolved_merges.return_value = [op]
    client = MagicMock()
    client.get_transaction.return_value = {"state": "STATE_FAILED"}

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    assert db.update_merge_operation.call_args.args[:2] == (17, "failed")
    # A terminal failure releases the condition immediately, so the ordinary
    # inventory pass may safely consider it again in the same tick.
    api.get_user_positions.assert_called_once_with("0xFunder")
    assert all(call.args[1] != "confirmed" for call in db.update_merge_operation.call_args_list)


def test_busy_condition_lock_skips_only_that_merge_and_leaves_other_work_possible():
    monitor, api, db = _monitor()
    locked = threading.Lock()
    assert locked.acquire(blocking=False)
    try:
        with patch.object(monitor, "_merge_client", return_value=MagicMock()):
            monitor.check_merges(lambda _cid: locked)
    finally:
        locked.release()

    db.create_merge_operation.assert_not_called()
    api.cancel_orders.assert_not_called()


def test_planned_merge_waits_for_confirmed_inventory_before_relayer_submission():
    monitor, api, db = _monitor()
    db.get_unresolved_merges.side_effect = [
        [{"id": 17, "status": "planned", "condition_id": CID, "requested_qty": 12, "created_at": 1}],
        [{"id": 17, "status": "planned", "condition_id": CID, "requested_qty": 12, "created_at": 1}],
        [{"id": 17, "status": "planned", "condition_id": CID, "requested_qty": 12, "created_at": 1}],
    ]
    api.get_user_positions.return_value = []
    client = MagicMock()

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    client.submit_merge.assert_not_called()
    assert db.update_merge_operation.call_args.args[:2] == (17, "failed")


def test_indeterminate_relayer_submission_is_retained_without_duplicate_retry():
    monitor, api, db = _monitor()
    db.get_unresolved_merges.side_effect = [
        [],
        [],
        [],
    ]
    client = MagicMock()
    client.submit_merge.side_effect = RuntimeError("timeout")

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    assert db.update_merge_operation.call_args.args[:2] == (17, "planned")
    assert "indeterminate" in db.update_merge_operation.call_args.kwargs["error"]
    client.submit_merge.assert_called_once()


def test_post_dispatch_response_without_id_stays_indeterminate_and_active():
    monitor, _api, db = _monitor()
    db.get_unresolved_merges.side_effect = [[], [], []]
    db.get_confirmed_pending_merges.return_value = []
    client = MagicMock()
    client.submit_merge.return_value.transaction_id = ""
    client.submit_merge.return_value.transaction_hash = ""

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    assert db.update_merge_operation.call_args.args[:2] == (17, "planned")
    assert "indeterminate" in db.update_merge_operation.call_args.kwargs["error"]


def test_legacy_duplicate_active_rows_are_never_auto_submitted():
    monitor, _api, db = _monitor()
    duplicates = [
        {"id": 17, "status": "submitted", "condition_id": CID, "relayer_id": "r"},
        {"id": 18, "status": "planned", "condition_id": CID.lower(), "created_at": 1},
    ]
    db.get_unresolved_merges.side_effect = [duplicates, duplicates, duplicates]
    db.get_confirmed_pending_merges.return_value = []
    client = MagicMock()
    client.get_transaction.return_value = {"state": "STATE_PENDING"}

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    client.submit_merge.assert_not_called()
    assert any(
        call.args[:2] == (18, "planned")
        and str(call.kwargs.get("error", "")).startswith(
            "DUPLICATE_ACTIVE_QUARANTINE:"
        )
        for call in db.update_merge_operation.call_args_list
    )


def test_quarantined_duplicate_stays_blocked_after_sibling_confirms():
    monitor, _api, db = _monitor()
    submitted = {
        "id": 17,
        "status": "submitted",
        "condition_id": CID,
        "relayer_id": "r",
        "requested_qty": 12,
        "yes_asset_id": "yes",
        "no_asset_id": "no",
    }
    planned = {
        "id": 18,
        "status": "planned",
        "condition_id": CID.lower(),
        "created_at": 1,
        "error": "",
    }
    quarantined = {
        **planned,
        "error": "DUPLICATE_ACTIVE_QUARANTINE: manual reconciliation required",
    }
    db.get_unresolved_merges.side_effect = [
        [submitted, planned],
        [quarantined],
        [quarantined],
    ]
    db.get_confirmed_pending_merges.return_value = []
    db.get_merge_inventory_barriers.return_value = [
        {**submitted, "status": "confirmed"}
    ]
    client = MagicMock()
    client.get_transaction.return_value = {"state": "STATE_CONFIRMED"}

    with (
        patch.object(monitor, "_merge_client", return_value=client),
        patch.object(monitor, "_confirm_merge_operation"),
    ):
        monitor.check_merges(lambda _cid: threading.Lock())

    client.submit_merge.assert_not_called()
    assert any(
        call.args[:2] == (18, "planned")
        and str(call.kwargs.get("error", "")).startswith(
            "DUPLICATE_ACTIVE_QUARANTINE:"
        )
        for call in db.update_merge_operation.call_args_list
    )


def test_confirmed_merge_ledger_failure_is_terminal_but_blocks_second_merge():
    monitor, api, db = _monitor()
    operation = {
        "id": 17,
        "status": "submitted",
        "condition_id": CID,
        "relayer_id": "r",
        "requested_qty": 12,
        "yes_asset_id": "yes",
        "no_asset_id": "no",
    }
    db.get_unresolved_merges.side_effect = [[operation], [], []]
    pending = {**operation, "status": "confirmed"}
    db.get_confirmed_pending_merges.return_value = []
    db.get_merge_inventory_barriers.return_value = [pending]
    api.get_trades.side_effect = RuntimeError("history unavailable")
    client = MagicMock()
    client.get_transaction.return_value = {"state": "STATE_CONFIRMED"}

    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda _cid: threading.Lock())

    states = [call.args[1] for call in db.update_merge_operation.call_args_list]
    assert states == ["confirmed", "confirmed"]
    client.submit_merge.assert_not_called()


def test_restart_fifo_replay_uses_prior_confirmed_merge_journal():
    fills = [{"side": "BUY", "price": 0.30, "size": 20, "ts": 1}]
    earlier = [{"side": "MERGE", "size": 12, "ts": 2}]
    lots = remaining_fifo_lots(fills, earlier)
    assert lots == [{"price": 0.30, "remaining": 8.0, "ts": 1.0, "trade_id": ""}]


def test_rejected_urgent_fok_creates_no_inventory_and_returns_direct_exit_path():
    monitor, api, db = _monitor()
    api.get_market.return_value = {
        "tokens": [{"token_id": "yes", "outcome": "Yes"}, {"token_id": "no", "outcome": "No"}]
    }
    api.get_orderbook.side_effect = [
        {"bids": [{"price": "0.20", "size": "10"}], "asks": [], "tick_size": "0.01"},
        {"bids": [], "asks": [{"price": "0.50", "size": "10"}], "tick_size": "0.01"},
    ]
    api.place_complement_fok_buy.side_effect = OrderRejected("unmatched")
    db.create_merge_operation.return_value = 29

    with patch.object(monitor, "_merge_client", return_value=MagicMock()):
        assert monitor._urgent_merge_route(CID, "yes", 10, "0.01") is None

    assert db.update_merge_operation.call_args.args[:2] == (29, "failed")
    api.place_market_sell.assert_not_called()
