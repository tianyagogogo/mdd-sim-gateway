"""Reassembly of a multi-part (concatenated) inbound SMS.

A text longer than one SMS arrives as several SMS-DELIVER PDUs, out of order and each with its
own User Data Header. These cover the path from the sms_in event through the segment buffer to
the single stored message.
"""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import main, store

# The real CTExcel text that arrived as three separate messages, in the order the parts landed.
CTEXCEL = [
    "【CTExcel】尊敬的用户，您当前使用的手机卡为学生活动专属SIM卡，您当前仍未订购任意套餐。若激活超过7日未订购任何套餐，您的SI",
    "M卡将被自动暂停服务及终止。为避免影响正常使用，请尽快前往CTExcel官网订购套餐：https://www.ctexcel.com/",
    "uk/trail/index/1 。感谢您的支持！",
]


class TempStore(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        patcher = patch.multiple(store, DATA_DIR=str(root),
                                 DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                 PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()

    def _buffered(self):
        with store._conn() as c:
            return c.execute("SELECT COUNT(*) FROM sms_segments").fetchone()[0]


class SegmentBufferTests(TempStore):
    def test_group_completes_only_on_the_last_part_and_comes_back_in_order(self):
        # parts arrive 2, 1, 3 — the order the carrier actually delivered them
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 2, CTEXCEL[1]))
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 1, CTEXCEL[0]))
        parts = store.add_sms_segment("7", "888", 155, 3, 3, CTEXCEL[2])
        self.assertEqual(parts, CTEXCEL)
        self.assertEqual("".join(parts).startswith("【CTExcel】尊敬的用户"), True)
        self.assertIn("SIM卡将被自动暂停", "".join(parts))
        self.assertEqual(self._buffered(), 0, "a completed group must leave nothing behind")

    def test_redelivered_part_neither_completes_nor_duplicates_a_group(self):
        store.add_sms_segment("7", "888", 155, 3, 1, CTEXCEL[0])
        # the SMSC re-pushes part 1 twice more when its RP-ACK is missed
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 1, CTEXCEL[0]))
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 1, CTEXCEL[0]))
        self.assertEqual(self._buffered(), 1)
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 3, 2, CTEXCEL[1]))
        self.assertEqual(store.add_sms_segment("7", "888", 155, 3, 3, CTEXCEL[2]), CTEXCEL)

    def test_a_completed_group_is_not_completed_a_second_time(self):
        store.add_sms_segment("7", "888", 155, 2, 1, "a")
        self.assertEqual(store.add_sms_segment("7", "888", 155, 2, 2, "b"), ["a", "b"])
        # a late duplicate of the final part must not republish the whole text
        self.assertIsNone(store.add_sms_segment("7", "888", 155, 2, 2, "b"))

    def test_groups_are_isolated_by_line_sender_and_reference(self):
        self.assertIsNone(store.add_sms_segment("7", "888", 1, 2, 1, "L7"))
        self.assertIsNone(store.add_sms_segment("5", "888", 1, 2, 1, "L5"))
        self.assertIsNone(store.add_sms_segment("7", "999", 1, 2, 1, "other-sender"))
        self.assertIsNone(store.add_sms_segment("7", "888", 2, 2, 1, "other-ref"))
        self.assertEqual(self._buffered(), 4)
        self.assertEqual(store.add_sms_segment("7", "888", 1, 2, 2, "-tail"), ["L7", "-tail"])
        self.assertEqual(self._buffered(), 3, "only the completed group is cleared")




class LateSegmentMergeTests(TempStore):
    """Parts that arrive after their group was flushed belong to the message it produced.

    Measured on live networks: segment 1 of a six-part text landed 11 seconds after it was
    sent and segments 2-6 arrived ten minutes later, long past the buffer's 180-second life.
    Without a second window each straggler starts a new group and the thread ends up holding
    the same text twice, both times incomplete.
    """

    def _flush(self, seqs, bodies, total=6, ref=0, peer="+15555550100", iid="5"):
        """Reproduce what the reaper does: publish the partial text, then remember the group."""
        body = main._join_sms_parts(bodies, seqs, total)
        rec = store.add_message(iid, "in", peer, body, ts=1000)
        store.remember_partial_sms_group(iid, peer, ref, total, rec["id"], seqs, bodies,
                                         ts=1000)
        return rec

    def _body(self, mid):
        with store._conn() as c:
            return c.execute("SELECT body FROM messages WHERE id=?", (mid,)).fetchone()[0]

    def test_a_late_part_completes_the_message_already_published(self):
        rec = self._flush([1], ["one "], total=3)
        self.assertIn(main.SMS_GAP_MARK, self._body(rec["id"]))

        merged = store.merge_late_sms_segment("5", "+15555550100", 0, 3, 2, "two ", now=1100)
        self.assertEqual(merged["message_id"], rec["id"])
        self.assertFalse(merged["complete"])
        self.assertEqual(merged["bodies"], ["one ", "two "])

        merged = store.merge_late_sms_segment("5", "+15555550100", 0, 3, 3, "three", now=1200)
        self.assertTrue(merged["complete"])
        self.assertEqual(main._join_sms_parts(merged["bodies"], merged["seqs"], 3),
                         "one two three")
        # Completing the text retires the memory; a further straggler starts a fresh group.
        self.assertIsNone(
            store.merge_late_sms_segment("5", "+15555550100", 0, 3, 1, "one ", now=1300))

    def test_late_parts_are_ordered_not_appended(self):
        rec = self._flush([6], ["-tail"], total=6)
        for seq, text in ((3, "c"), (2, "b"), (5, "e"), (4, "d"), (1, "a")):
            merged = store.merge_late_sms_segment("5", "+15555550100", 0, 6, seq, text,
                                                  now=1100)
        store.set_message_body(rec["id"],
                               main._join_sms_parts(merged["bodies"], merged["seqs"], 6))
        self.assertEqual(self._body(rec["id"]), "abcde-tail")
        self.assertNotIn(main.SMS_GAP_MARK, self._body(rec["id"]))

    def test_a_repeat_of_a_merged_part_changes_nothing(self):
        self._flush([1], ["one "], total=2)
        first = store.merge_late_sms_segment("5", "+15555550100", 0, 2, 1, "one ", now=1100)
        self.assertTrue(first["duplicate"])
        self.assertEqual(first["bodies"], ["one "])
        self.assertFalse(first["complete"], "a repeat must not complete a group")
        # ...and the group is still open to the part that is genuinely missing.
        self.assertTrue(
            store.merge_late_sms_segment("5", "+15555550100", 0, 2, 2, "two", now=1200)
            ["complete"])

    def test_a_reused_reference_starts_a_new_message_instead_of_corrupting_the_old(self):
        # The concatenation reference is one byte; the same sender reuses it. A different text
        # in a slot that is already filled is a NEW message, not a re-delivery.
        rec = self._flush([1], ["old text "], total=2)
        self.assertIsNone(
            store.merge_late_sms_segment("5", "+15555550100", 0, 2, 1, "new text ", now=1100))
        self.assertEqual(self._body(rec["id"]), "old text " + main.SMS_GAP_MARK)
        # The memory is gone, so the following part buffers normally rather than merging.
        self.assertIsNone(
            store.merge_late_sms_segment("5", "+15555550100", 0, 2, 2, "tail", now=1200))

    def test_the_window_closes_and_the_memory_is_pruned(self):
        self._flush([1], ["one "], total=2)
        self.assertIsNone(
            store.merge_late_sms_segment("5", "+15555550100", 0, 2, 2, "two",
                                         now=1000 + store.SEGMENT_LATE_WINDOW + 1))
        self._flush([1], ["one "], total=2, ref=7)
        self.assertEqual(
            store.prune_late_sms_groups(now=1000 + store.SEGMENT_LATE_WINDOW + 1), 1)
        self.assertIsNone(
            store.merge_late_sms_segment("5", "+15555550100", 7, 2, 2, "two", now=1100))

    def test_memories_are_isolated_by_line_sender_reference_and_total(self):
        self._flush([1], ["mine"], total=2, ref=3, peer="888", iid="7")
        for iid, peer, ref, total in (("5", "888", 3, 2), ("7", "999", 3, 2),
                                      ("7", "888", 4, 2), ("7", "888", 3, 5)):
            self.assertIsNone(
                store.merge_late_sms_segment(iid, peer, ref, total, 2, "x", now=1100),
                f"{iid}/{peer}/{ref}/{total} must not merge into another group's message")
        self.assertIsNotNone(
            store.merge_late_sms_segment("7", "888", 3, 2, 2, "x", now=1100))

    def test_an_ordinary_part_with_no_memory_is_left_to_the_normal_buffer(self):
        self.assertIsNone(store.merge_late_sms_segment("7", "888", 1, 2, 1, "a", now=1000))

class StaleSegmentTests(TempStore):
    def test_sweep_takes_only_groups_past_the_timeout(self):
        store.add_sms_segment("7", "888", 1, 3, 1, "old", ts=1000)
        store.add_sms_segment("7", "888", 2, 2, 1, "fresh", ts=5000)
        stale = store.take_stale_sms_segments(timeout=180, now=5000)
        self.assertEqual([g["concat_ref"] for g in stale], [1])
        self.assertEqual(stale[0], {"instance": "7", "peer": "888", "concat_ref": 1,
                                    "total": 3, "first_ts": 1000, "sent_ts": None,
                                    "seqs": [1], "bodies": ["old"]})
        self.assertEqual(self._buffered(), 1, "the fresh group stays buffered")

    def test_sweep_ages_a_group_by_its_first_part_not_its_last(self):
        store.add_sms_segment("7", "888", 1, 3, 1, "first", ts=1000)
        store.add_sms_segment("7", "888", 1, 3, 3, "third", ts=4990)
        stale = store.take_stale_sms_segments(timeout=180, now=5000)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["seqs"], [1, 3])
        self.assertEqual(self._buffered(), 0)

    def test_sweep_is_a_no_op_when_nothing_is_buffered(self):
        self.assertEqual(store.take_stale_sms_segments(timeout=180, now=5000), [])


class TripletParsingTests(unittest.TestCase):
    def test_valid_triplet(self):
        self.assertEqual(main._concat_triplet(["888", "Yg==", "155", "3", "2"]), (155, 3, 2))

    def test_single_part_sms_sends_empty_fields(self):
        self.assertIsNone(main._concat_triplet(["888", "Yg==", "", "", ""]))

    def test_engine_too_old_to_send_the_fields(self):
        self.assertIsNone(main._concat_triplet(["888", "Yg=="]))

    def test_malformed_values_are_ignored(self):
        for bad in (["x", "3", "1"],            # not a number
                    ["155", "1", "1"],          # total < 2 is not a concatenation
                    ["155", "3", "0"],          # sequence numbers start at 1
                    ["155", "3", "4"],          # sequence past the total
                    ["155", "3", ""]):          # partially filled
            self.assertIsNone(main._concat_triplet(["888", "Yg=="] + bad), bad)


class GapMarkingTests(unittest.TestCase):
    def test_complete_message_has_no_marker(self):
        self.assertEqual(main._join_sms_parts(CTEXCEL, [1, 2, 3], 3), "".join(CTEXCEL))

    def test_missing_middle_part_is_marked_once(self):
        joined = main._join_sms_parts(["a", "c"], [1, 3], 3)
        self.assertEqual(joined, "a" + main.SMS_GAP_MARK + "c")

    def test_consecutive_missing_parts_collapse_into_one_marker(self):
        joined = main._join_sms_parts(["a", "d"], [1, 4], 4)
        self.assertEqual(joined, "a" + main.SMS_GAP_MARK + "d")

    def test_missing_head_and_tail_are_both_marked(self):
        joined = main._join_sms_parts(["b"], [2], 3)
        self.assertEqual(joined, main.SMS_GAP_MARK + "b" + main.SMS_GAP_MARK)


def _event(instance, sender, body, triplet=None):
    args = [sender, base64.b64encode(body.encode()).decode()]
    if triplet is not None:
        args += [str(x) for x in triplet]
    return {"instance": instance, "event": "sms_in", "args": args}


class InboundEventTests(unittest.IsolatedAsyncioTestCase, TempStore):
    def setUp(self):
        TempStore.setUp(self)
        self.broadcast = AsyncMock()
        self.push = Mock()
        for target, attr, new in ((main.hub, "broadcast", self.broadcast),
                                  (main, "_dispatch_push", self.push)):
            p = patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)

    def _stored(self):
        with store._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT peer,body FROM messages ORDER BY id")]

    async def test_three_parts_become_one_message_pushed_once(self):
        # delivered 2, 1, 3 — exactly how the real message arrived
        for seq in (2, 1):
            result = await main.api_engine_event(
                _event("7", "888", CTEXCEL[seq - 1], (155, 3, seq)))
            self.assertIn("buffered", result)
        await main.api_engine_event(_event("7", "888", CTEXCEL[2], (155, 3, 3)))

        self.assertEqual(self._stored(), [{"peer": "888", "body": "".join(CTEXCEL)}])
        self.assertEqual(self.broadcast.await_count, 1)
        self.assertEqual(self.push.call_count, 1)
        self.assertIn("uk/trail/index/1", self.push.call_args[0][3])

    async def test_single_part_sms_is_stored_immediately(self):
        await main.api_engine_event(_event("7", "888", "Your balance is £1.01.", ("", "", "")))
        self.assertEqual(self._stored(), [{"peer": "888", "body": "Your balance is £1.01."}])
        self.assertEqual(self.push.call_count, 1)

    async def test_engine_without_the_patch_still_stores_messages(self):
        await main.api_engine_event(_event("7", "888", "legacy engine"))
        self.assertEqual(self._stored(), [{"peer": "888", "body": "legacy engine"}])

    async def test_empty_single_part_message_is_still_dropped(self):
        result = await main.api_engine_event(_event("7", "20023", "  ", ("", "", "")))
        self.assertEqual(result.get("dropped"), "empty_body")
        self.assertEqual(self._stored(), [])

    async def test_a_part_that_decoded_to_nothing_is_kept_so_the_group_can_complete(self):
        await main.api_engine_event(_event("7", "888", "", (9, 2, 1)))
        await main.api_engine_event(_event("7", "888", "tail", (9, 2, 2)))
        self.assertEqual(self._stored(), [{"peer": "888", "body": "tail"}])

    async def test_incomplete_group_is_published_by_the_sweeper_with_the_gap_marked(self):
        await main.api_engine_event(_event("7", "888", CTEXCEL[0], (155, 3, 1)))
        await main.api_engine_event(_event("7", "888", CTEXCEL[2], (155, 3, 3)))
        self.assertEqual(self._stored(), [])

        stale = store.take_stale_sms_segments(timeout=0)
        for group in stale:
            body = main._join_sms_parts(group["bodies"], group["seqs"], group["total"])
            store.add_message(group["instance"], "in", group["peer"], body,
                              ts=group["first_ts"])
        self.assertEqual(self._stored(),
                         [{"peer": "888", "body": CTEXCEL[0] + main.SMS_GAP_MARK + CTEXCEL[2]}])

    async def test_redelivered_text_and_modem_copy_are_not_shown_twice(self):
        # SMS-DELIVER from 447700900123 with TP-SCTS 26-03-14 15:09:26 +02:00.
        tpdu = "040c91447700091032" + "0000" + "62304151906280" + "01" + "41"
        event = _event("7", "+447700900123", "A", ("", "", ""))
        event["args"] += ["0", "0", "", tpdu]
        first = await main.api_engine_event(event)
        again = await main.api_engine_event(event)
        self.assertNotIn("duplicate", first)
        self.assertTrue(again.get("duplicate"))
        with store._conn() as c:
            ts = c.execute("SELECT ts FROM messages").fetchone()[0]
        self.assertEqual(ts, 1773493766)
        self.assertIsNone(store.ingest_message("7", "in", "07700900123", "A",
                                               transport="cellular", sent_ts=1773493766))
        self.assertEqual(self.push.call_count, 1)

    async def test_parts_carry_the_first_parts_network_time(self):
        store.add_sms_segment("7", "888", 4, 2, 2, "b", sent_ts=2_010)
        group = store.add_sms_segment("7", "888", 4, 2, 1, "a", sent_ts=2_000, with_meta=True)
        self.assertEqual(group, {"bodies": ["a", "b"], "sent_ts": 2_000})


class LateCompletionTests(unittest.IsolatedAsyncioTestCase, TempStore):
    """A text shown incomplete, then made whole by its late parts (#193).

    It stays where the reader saw it, with its time and its read state, and says it was
    completed; the whole text is pushed once more, when the last part is in.
    """

    def setUp(self):
        TempStore.setUp(self)
        self.broadcast = AsyncMock()
        self.push = Mock()
        for target, attr, new in ((main.hub, "broadcast", self.broadcast),
                                  (main, "_dispatch_push", self.push)):
            p = patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)

    def _published_incomplete(self, seqs, total, ref=77):
        """What the reaper leaves behind: the partial text stored, and its group remembered."""
        bodies = [CTEXCEL[n - 1] for n in seqs]
        rec = store.add_message("7", "in", "888", main._join_sms_parts(bodies, seqs, total))
        store.remember_partial_sms_group("7", "888", ref, total, rec["id"], seqs, bodies)
        return rec

    def _row(self, mid):
        with store._conn() as c:
            return dict(c.execute("SELECT body, ts, completed_ts FROM messages WHERE id=?",
                                  (mid,)).fetchone())

    async def test_the_last_part_completes_the_message_in_place_and_pushes_it_once(self):
        rec = self._published_incomplete([1, 2], 3)
        before = self._row(rec["id"])
        self.assertIsNone(before["completed_ts"])

        result = await main.api_engine_event(_event("7", "888", CTEXCEL[2], (77, 3, 3)))
        self.assertEqual(result["merged"], "3/3")
        after = self._row(rec["id"])
        self.assertEqual(after["body"], "".join(CTEXCEL))
        self.assertEqual(after["ts"], before["ts"])                 # it keeps its place
        self.assertIsNotNone(after["completed_ts"])
        # The page learns it from the broadcast record, and a page loaded later from the row.
        self.assertEqual(self.broadcast.await_args[0][0]["message"]["completed_ts"],
                         after["completed_ts"])
        self.assertEqual(store.list_messages("7", "888")[0]["completed_ts"],
                         after["completed_ts"])
        self.push.assert_called_once()
        self.assertEqual(self.push.call_args[0][3], main.SMS_COMPLETED_MARK + "".join(CTEXCEL))

    async def test_a_part_that_leaves_it_incomplete_updates_it_without_a_push(self):
        rec = self._published_incomplete([1], 3)
        await main.api_engine_event(_event("7", "888", CTEXCEL[2], (77, 3, 3)))
        self.assertIsNone(self._row(rec["id"])["completed_ts"])
        self.assertIn(main.SMS_GAP_MARK, self._row(rec["id"])["body"])
        self.assertEqual(self.broadcast.await_count, 1)
        self.push.assert_not_called()
        # A repeat of that part changes nothing; the last part pushes, once.
        await main.api_engine_event(_event("7", "888", CTEXCEL[2], (77, 3, 3)))
        await main.api_engine_event(_event("7", "888", CTEXCEL[1], (77, 3, 2)))
        self.push.assert_called_once()
        self.assertIsNotNone(self._row(rec["id"])["completed_ts"])

    async def test_completing_a_message_already_read_leaves_it_read(self):
        rec = self._published_incomplete([1, 2], 3)
        store.mark_thread_read(store.ADMIN_OWNER, "7", "888")
        await main.api_engine_event(_event("7", "888", CTEXCEL[2], (77, 3, 3)))
        self.assertEqual(store.unread_counts(store.ADMIN_OWNER, "7"), {})
        self.assertIsNotNone(self._row(rec["id"])["completed_ts"])

    def test_a_database_from_before_the_mark_gains_the_column(self):
        with store._conn() as c:
            c.execute("ALTER TABLE messages DROP COLUMN completed_ts")
            c.execute("PRAGMA user_version="
                      f"{store._MIGRATIONS.index(store._migration_completed_ts)}")
        store.init()
        with store._conn() as c:
            columns = {row[1] for row in c.execute("PRAGMA table_info(messages)")}
            version = c.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("completed_ts", columns)
        self.assertEqual(version, len(store._MIGRATIONS))


class DeliverTimestampTests(unittest.TestCase):
    def test_scts_with_positive_zone(self):
        # 26-03-14 15:09:26, zone 8 quarter hours east.
        tpdu = "4404812143000462304151906280"
        self.assertEqual(main.sms_pdu.deliver_timestamp(tpdu), 1773493766)

    def test_negative_zone_and_malformed_input(self):
        # 26-01-02 03:04:05, zone 0x8A: the low nibble 0xA is the sign bit plus a tens digit of 2,
        # = sign + 2 tens, high nibble 8 -> 28 quarter hours west (-07:00).
        tpdu = "0003a121f300006210203040508a"
        self.assertEqual(main.sms_pdu.deliver_timestamp(tpdu), 1767323045 + 7 * 3600)
        self.assertIsNone(main.sms_pdu.deliver_timestamp(""))
        self.assertIsNone(main.sms_pdu.deliver_timestamp("zz"))
        self.assertIsNone(main.sms_pdu.deliver_timestamp("0103a121f3"))   # SMS-SUBMIT
        self.assertIsNone(main.sms_pdu.deliver_timestamp("0003a121f30000621320"))


if __name__ == "__main__":
    unittest.main()
