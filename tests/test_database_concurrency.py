"""并发安全回归:DB 层被多线程同时读写时,读到的必须是真实数据,不得被冲坏。

历史事故(2026-06-27):所有线程共享一个 sqlite3 连接(check_same_thread=False)、
全 DB 层无锁。压力下 get_template_for 会偶发读到默认模板(stop_loss_percent=20)而非
钱包绑定的 80% 模板,并抛 InterfaceError。这直接导致离场强平阈值从 0.192 塌到 0.048、
把在手持仓按 20% 止损市价砸掉。修复:每线程独立连接 + WAL。
"""

import threading

from models.database import ActiveMergeOperationExists, Database


def test_get_template_for_thread_safe_under_write_contention(tmp_path):
    db = Database(str(tmp_path / "race.db"))
    db.init()
    tid = db.create_template("八十")
    db.save_template(tid, {"stop_loss_percent": 80})
    addr = "0xWALLET"
    db.add_wallet(addr, "enc")
    db.set_wallet_template(addr, tid)
    # 单线程静态读必须先是对的
    assert db.get_template_for(addr)["stop_loss_percent"] == 80

    stop = threading.Event()
    wrong: list = []
    errors: list = []

    def reader():
        for _ in range(500):
            try:
                v = db.get_template_for(addr)["stop_loss_percent"]
                if v != 80:
                    wrong.append(v)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

    def writer_cooldown():
        i = 0
        while not stop.is_set():
            try:
                db.set_cooldown(addr, f"m{i % 50}", 5)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
            i += 1

    def writer_action():
        i = 0
        while not stop.is_set():
            try:
                db.record_action(
                    addr, f"m{i % 50}", "exit_market", "卖出", 0.12, 20, "B0", "t"
                )
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
            i += 1

    readers = [threading.Thread(target=reader) for _ in range(4)]
    writers = [
        threading.Thread(target=writer_cooldown),
        threading.Thread(target=writer_action),
    ]
    for w in writers:
        w.start()
    for r in readers:
        r.start()
    for r in readers:
        r.join()
    stop.set()
    for w in writers:
        w.join()

    assert not wrong, f"get_template_for 并发读到非 80(默认值漏入): {wrong[:15]}"
    assert not errors, f"并发访问抛异常: {errors[:15]}"


def test_concurrent_active_merge_creation_has_one_winner_per_condition(tmp_path):
    db = Database(str(tmp_path / "merge-race.db"))
    db.init()
    barrier = threading.Barrier(8)
    created: list[int] = []
    duplicate_ids: list[int | None] = []
    errors: list[str] = []
    result_lock = threading.Lock()

    def create():
        try:
            barrier.wait()
            operation_id = db.create_merge_operation(
                "0xWallet", "0xFunder", "condition-A", "yes", "no", 5
            )
            with result_lock:
                created.append(operation_id)
        except ActiveMergeOperationExists as exc:
            with result_lock:
                duplicate_ids.append(exc.operation_id)
        except Exception as exc:  # noqa: BLE001
            with result_lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(created) == 1
    assert duplicate_ids == [created[0]] * 7
    assert len(db.get_unresolved_merges("0xWallet", "condition-A")) == 1

    # The same wallet can independently own condition B.
    assert db.create_merge_operation(
        "0xWallet", "0xFunder", "condition-B", "yes-b", "no-b", 3
    )
