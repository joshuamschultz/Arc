# Coverage Analysis: ArcTeam Messaging Subsystem (SPEC-006)

**Date**: 2026-02-17
**Overall Coverage**: 95.56% line | 172 branches (17 partial)
**Tests**: 143 total (131 unit, 8 integration, 7 E2E -- see pyramid analysis)
**Status**: EXCEEDS quality gates (80% line, 75% branch)

---

## 1. Coverage Summary by Module

| File | Stmts | Miss | Branch | BrPart | Cover | Category |
|------|-------|------|--------|--------|-------|----------|
| `__init__.py` | 8 | 0 | 0 | 0 | 100% | Config |
| `types.py` | 95 | 0 | 6 | 0 | 100% | Business Logic |
| `config.py` | 10 | 0 | 0 | 0 | 100% | Config |
| `audit.py` | 73 | 1 | 14 | 1 | 98% | Critical Path |
| `registry.py` | 44 | 0 | 10 | 1 | 98% | Business Logic |
| `cli.py` | 203 | 8 | 48 | 5 | 95% | UI |
| `storage.py` | 167 | 8 | 50 | 2 | 94% | Critical Path |
| `messenger.py` | 152 | 5 | 44 | 8 | 93% | Critical Path |

---

## 2. Uncovered Lines Analysis

### 2.1 messenger.py (93% -- 5 missed lines, 8 partial branches)

**Line 72**: `self._seq_counters[stream] = max(r.get("seq", 0) for r in records)`
- **What**: Sequence counter recovery from existing stream data when `_next_seq` loads a stream that already has records.
- **Why it matters**: HIGH. This is the sequence monotonicity recovery path. If the service restarts with existing messages in a stream, it must recover the correct next sequence number. Without this path tested, a restart could produce duplicate sequence numbers, corrupting stream ordering.
- **Business risk**: Message ordering corruption after service restart.

**Line 117 -> 122 (partial branch)**: `elif not message.thread_id: message.thread_id = message.id`
- **What**: The branch where `reply_to` is set AND `thread_id` is also explicitly set (so neither the `if` nor `elif` fires).
- **Why it matters**: LOW. This is a defensive path -- if a caller pre-sets both `reply_to` and `thread_id`, the code correctly leaves `thread_id` alone. The current tests cover the `if` (reply_to set, no thread_id) and the `elif` (neither set) but not the pass-through case.

**Line 186**: `return {}` in `poll_all` when entity is None (unregistered entity).
- **What**: Early return when `poll_all` is called for an unregistered entity.
- **Why it matters**: MEDIUM. This is a defensive guard. If an agent is removed from the registry but still tries to poll, it gets an empty result instead of crashing. Currently the test for `poll_all` always uses registered entities.

**Lines 194 -> 191 (partial branch)**: Channel-stream deduplication in `poll_all`.
- **What**: The `if stream not in streams` guard when building the subscription list from channel membership. Tests don't exercise the case where a channel's stream is already in the list from role subscriptions.
- **Why it matters**: LOW. Deduplication guard -- prevents polling the same stream twice. Not a correctness issue, just efficiency.

**Lines 247 -> 249, 263 -> exit, 277, 279 -> exit**: Channel management partial branches.
- **Line 247 -> 249**: `create_channel` when `channel.created` is already set (skips auto-timestamp).
- **Line 263 -> exit**: `join_channel` when entity is already a member (skips the join, exits without writing).
- **Line 277**: `leave_channel` when channel is not found.
- **Line 279 -> exit**: `leave_channel` when entity is not a member (skips the remove, exits without writing).
- **Why it matters**: MEDIUM collectively. These are idempotency guards. A duplicate join or leave of a non-member should be no-ops, but these paths are untested.

**Lines 323-324**: `resolve_subscriptions` ValueError catch when `parse_uri` fails on `entity_id`.
- **What**: Fallback path when `entity_id` is not a valid URI (uses raw string as name).
- **Why it matters**: LOW. Defensive fallback for malformed entity IDs.

### 2.2 storage.py (94% -- 8 missed lines, 2 partial branches)

**Lines 94-97**: `write()` exception handler that cleans up temp file.
- **What**: When `json.dump` or `os.replace` fails, the temp file is removed to avoid orphaned `.tmp` files.
- **Why it matters**: HIGH. This is the atomic write crash recovery path. If the filesystem fails mid-write (disk full, permissions, etc.), orphaned temp files accumulate. In a federal deployment running for weeks, this could be a storage leak or security concern (unlinked temp files containing sensitive data).

**Line 145**: Empty line skip in `read_stream`.
- **What**: `if not line: continue` -- skips blank lines in JSONL files.
- **Why it matters**: LOW. Defensive guard. Blank lines can appear after crash recovery or manual file editing. The malformed-line test covers `JSONDecodeError` but not blank lines specifically.

**Lines 175-176**: `query()` exception handler for `JSONDecodeError` or `OSError` on individual files.
- **What**: If a `.json` file in a collection is corrupted or unreadable, it's skipped.
- **Why it matters**: MEDIUM. Crash recovery resilience. In production, a corrupted registry entry could cause the entire query to fail instead of gracefully skipping.

**Line 188**: `list_keys()` early return for non-existent collection directory.
- **What**: `if not col_dir.exists(): return []`
- **Why it matters**: LOW. Defensive guard, symmetric with the `query()` method.

### 2.3 cli.py (95% -- 8 missed lines, 5 partial branches)

**Line 113**: Channel member truncation `members += f" +{len(c.members) - 3}"`.
- **What**: When a channel has >3 members, display shows first 3 plus a "+N" suffix.
- **Why it matters**: LOW. Display formatting only. No channel in tests has >3 members.

**Lines 191-193**: DLQ display formatting for non-empty DLQ with entries.
- **What**: The DLQ display loop iterating over entries extracting `dlq_reason` and `sender`.
- **Why it matters**: LOW. Display formatting. The DLQ is tested via JSON output mode but not in human-readable mode with actual entries.

**Line 203**: Audit verify exit with non-zero status when chain is invalid.
- **What**: `sys.exit(1)` when `audit --verify` detects a broken chain.
- **Why it matters**: MEDIUM. This is the CLI exit code for a tampered audit trail. The audit verify test only tests the valid case. An invalid audit chain should produce a non-zero exit code for CI/monitoring integration.

**Lines 326-327**: Fallback handler for unknown command.
- **What**: `parser.print_help(); sys.exit(1)` when command is not in the dispatch dict.
- **Why it matters**: LOW. This path is effectively unreachable because `argparse` with `required=True` on subparsers will reject unknown commands before reaching this code.

**Line 331**: `if __name__ == "__main__": main()` guard.
- **What**: Module entry point guard.
- **Why it matters**: NONE. Standard Python pattern, not testable/meaningful.

### 2.4 audit.py (98% -- 1 missed line, 1 partial branch)

**Line 101**: `return True, 0` when audit stream has no records.
- **What**: `verify_chain()` returns valid with seq 0 when there are no audit records.
- **Why it matters**: LOW. Edge case -- verifying an empty audit chain. All tests start with at least one audit record (from entity registration). This is tested implicitly through the E2E DLQ test but the branch might not be hit directly.

### 2.5 registry.py (98% -- 0 missed lines, 1 partial branch)

**Line 33 -> 36**: `register()` when `entity.created` is already set (skips auto-timestamp).
- **What**: If an entity is registered with a pre-set `created` field, the code skips auto-timestamping.
- **Why it matters**: LOW. Convenience branch for testing or migration scenarios.

---

## 3. Critical Business Paths Assessment

### Fully Covered (GOOD)

| Path | Coverage | Verdict |
|------|----------|---------|
| Message send to channel/role/DM | 100% | All routing tested |
| Cursor advance (ack) | 100% | Forward-only enforcement tested |
| Backward cursor rejection | 100% | Tested explicitly |
| HMAC chain integrity | 100% | Valid chain, tampering, gap all tested |
| Entity registration + dedup | 100% | Register and reject-duplicate tested |
| Body size validation | 100% | Pydantic validator + service-level tested |
| URI parsing (all 4 schemes) | 100% | Every scheme + invalid cases tested |
| DLQ routing (3 failure types) | 100% | sender_unauthorized, body_too_large, invalid_address |
| Crash recovery (cursor-based re-poll) | 100% | Integration test covers this |
| Thread construction | 100% | Original + replies + filtering tested |

### Gaps in Critical Paths (NEEDS ATTENTION)

| Path | Priority | Gap |
|------|----------|-----|
| Sequence recovery after restart | P0 | Line 72 -- `_next_seq` loading from existing stream never tested |
| Atomic write failure cleanup | P1 | Lines 94-97 -- temp file cleanup on write failure never tested |
| Audit chain verify on empty log | P2 | Line 101 -- empty chain verify not tested |
| Audit verify CLI exit code on failure | P2 | Line 203 -- tampered chain via CLI not tested |

---

## 4. Missing Edge Case Tests

### P0 -- Must Fix (Critical Path Gaps)

1. **Sequence recovery on warm start**: Start service, send messages, create a NEW `MessagingService` instance against the same backend, send more messages. Verify sequences continue monotonically without gaps or duplicates. This is the most important untested path in the system.

2. **Atomic write failure**: Force a write failure (read-only directory, disk-full simulation) and verify no orphaned `.tmp` files remain and the original file is untouched.

### P1 -- Should Fix (Defensive Paths)

3. **poll_all for unregistered entity**: Call `poll_all("agent://ghost")` and verify empty dict return, no crash.

4. **Join channel when already a member**: Call `join_channel` twice with the same entity and verify idempotent behavior (no duplicate member entries).

5. **Leave channel when not a member**: Call `leave_channel` for entity that never joined and verify no error.

6. **Leave nonexistent channel**: Call `leave_channel` for channel that doesn't exist and verify ValueError.

7. **Corrupted JSON file in query**: Write a corrupted `.json` file in a collection directory, run `query()`, verify it gracefully skips the corrupted file.

8. **Blank lines in JSONL stream**: Append blank lines to a stream file, verify `read_stream` skips them.

### P2 -- Nice to Have

9. **Pre-set thread_id with reply_to**: Send a message with both `reply_to` and `thread_id` explicitly set. Verify `thread_id` is preserved (not overwritten).

10. **Pre-set created timestamp on entity/channel**: Register entity with `created` already set. Verify it's preserved.

11. **Channel with >3 members display**: Create channel with 5+ members, verify CLI display truncation.

12. **DLQ human-readable display**: Send a message that fails, then run `dlq` command (non-JSON mode) and verify output format.

13. **Audit verify failure via CLI**: Tamper with audit log on disk, run `audit --verify`, verify non-zero exit code.

14. **resolve_subscriptions with non-URI entity_id**: Call `resolve_subscriptions("plain-name", ["ops"])` and verify fallback behavior.

---

## 5. Integration Test Gaps

### What's Covered

- Full agent workflow (register -> channel -> send -> poll -> ack -> poll empty)
- Multi-agent scenario (3 agents, 2 channels, role broadcast, DMs)
- Cursor crash recovery (poll without ack, re-poll gets same messages)
- Audit chain verification after full workflow
- DLQ captures for sender_unauthorized and invalid_address

### What's Missing

| Gap | Priority | Description |
|-----|----------|-------------|
| **Service restart continuity** | P0 | Destroy and recreate service against same FileBackend. Verify message ordering and cursor positions survive. |
| **Concurrent send to same stream** | P1 | Multiple senders writing to same channel simultaneously. Currently tested for FileBackend append but not at the MessagingService level. |
| **Channel lifecycle with messaging** | P1 | Create channel -> send messages -> join new member -> verify new member sees all messages (or not, depending on design). |
| **Large volume threading** | P2 | Multiple threads in same stream, verify `get_thread` correctly isolates each thread. |
| **Cross-channel routing** | P2 | Single message to multiple channels + role + DM simultaneously. Verify each target receives independently. |

---

## 6. Branch Coverage Analysis (17 Partial Branches)

| File | Partial Branches | Analysis |
|------|-----------------|----------|
| `messenger.py` | 8 | Most significant gaps. Idempotency guards (join/leave), sequence recovery, thread_id pass-through. |
| `cli.py` | 5 | Display formatting branches and unreachable fallback handler. Low risk. |
| `storage.py` | 2 | Crash recovery paths (write failure cleanup, blank line skip). |
| `audit.py` | 1 | Empty chain verification. |
| `registry.py` | 1 | Pre-set created timestamp. |

**Branch coverage estimate**: ~90% (155/172 branches covered). Meets the 75% gate.

**Branches worth covering by priority**:
1. `messenger.py` line 72 -- sequence recovery (P0)
2. `storage.py` lines 94-97 -- write failure cleanup (P1)
3. `messenger.py` lines 263/279 -- idempotent join/leave (P1)
4. Everything else is P2 or lower.

---

## 7. Test Quality Assessment

### Behavior vs. Implementation

**Verdict: Tests are behavior-focused. Good quality.**

The tests consistently test through public interfaces and verify observable outcomes rather than implementation details:

- `test_send_to_channel` sends a message then polls to verify it arrived -- tests the contract, not internal storage format.
- `test_ack_advances_cursor` verifies that subsequent polls return fewer messages -- tests behavior, not cursor data structure.
- `test_tampered_record` modifies data then calls `verify_chain()` -- tests the integrity contract.
- `test_concurrent_writers` verifies no corruption -- tests the concurrency contract.

**Minor concerns**:
- Some tests access `svc._backend` and `svc._registry` directly (implementation details). However, these are used for fixture setup, not assertions, which is acceptable.
- `test_tampered_record` directly modifies `backend._streams` -- unavoidable for testing tamper detection.
- `test_audit_generates_audit` accesses `registry._backend.read_stream("audit", ...)` -- could use the audit logger's own verify method instead, but the approach is still testing behavior (records exist) not structure.

### Test Naming

Good. Follows `test_<what_it_does>` pattern. Class names describe the scenario. Docstrings on classes explain the invariant being tested.

### Test Independence

All tests use fresh fixtures (`tmp_path`, `MemoryBackend`). No shared state between tests. No test ordering dependencies. Good.

### Assertion Quality

Assertions are specific and meaningful:
- `assert sent.seq == 1` (not just `assert sent.seq > 0`)
- `assert seqs == [1, 2, 3, 4, 5]` (not just `assert len(seqs) == 5`)
- `assert messages[0].body == "Hello channel!"` (verifies content, not just existence)

### Parametrized Testing

Storage tests are parametrized across both `FileBackend` and `MemoryBackend` -- ensures both implementations satisfy the protocol. This is excellent.

---

## 8. Test Pyramid Analysis

### Current Distribution

| Layer | Tests | Percentage | Target |
|-------|-------|-----------|--------|
| Unit | 131 | 91.6% | 70% |
| Integration | 8 | 5.6% | 20% |
| E2E | 7 | 4.9% | 10% |
| **Total** | **143** | -- | -- |

### Assessment

The pyramid is **top-heavy on unit tests** and **light on integration tests**.

- **Unit tests (91.6% vs 70% target)**: Over-indexed. However, the unit tests include the 41 parametrized storage tests (each test runs twice -- once per backend), and 24 CLI tests that exercise real code paths through `main()`. These CLI tests arguably function as integration tests since they go through the full stack. If we reclassify the 24 CLI unit tests as integration (they bootstrap a real FileBackend, real AuditLogger, real Registry, and real MessagingService), the distribution becomes:

  | Layer | Tests | Percentage |
  |-------|-------|-----------|
  | Unit | 107 | 74.8% |
  | Integration | 32 | 22.4% |
  | E2E | 7 | 4.9% |

  This is very close to the ideal 70/20/10 split.

- **Integration tests (5.6% nominal)**: The 5 `test_messaging_flow.py` tests and 3 `test_benchmarks.py` tests are well-designed integration tests using real FileBackend. They test full workflows rather than individual methods.

- **E2E tests (4.9%)**: 7 subprocess-based tests that invoke the CLI binary, parse stdout, and verify exit codes. These provide real confidence that the installed package works end-to-end.

**Verdict**: The pyramid is healthy when accounting for the CLI unit tests that function as integration tests. The main gap is in integration tests covering service restart/recovery scenarios.

---

## 9. Improvement Plan

### Phase 1: Critical Gaps (P0) -- 2 tests, ~30 min effort

**Expected coverage increase**: +1.5% (97% target)

#### Test 1: Sequence Recovery on Warm Start

```python
class TestSequenceRecovery:
    """Sequence recovery: new service instance resumes seq from existing data."""

    async def test_warm_start_continues_seq(self, svc: MessagingService) -> None:
        # Send 5 messages via first service instance
        for i in range(5):
            await svc.send(Message(
                sender="agent://a1",
                to=["channel://project-alpha"],
                body=f"msg {i}",
            ))

        # Create NEW service instance against same backend
        svc2 = MessagingService(svc._backend, svc._registry, svc._audit)

        # Send message via new instance -- should get seq=6, not seq=1
        sent = await svc2.send(Message(
            sender="agent://a1",
            to=["channel://project-alpha"],
            body="after restart",
        ))
        assert sent.seq == 6

        # Verify all messages are in order
        messages = await svc2.poll("arc.channel.project-alpha", "agent://a2")
        seqs = [m.seq for m in messages]
        assert seqs == [1, 2, 3, 4, 5, 6]
```

**Covers**: `messenger.py` line 72, partial branch at line 72.

#### Test 2: Atomic Write Failure Cleanup

```python
class TestAtomicWriteFailure:
    """Atomic write failure: temp file cleaned up on exception."""

    async def test_write_failure_cleans_temp(self, file_backend: FileBackend) -> None:
        # Write a valid record first
        await file_backend.write("col", "key1", {"v": 1})
        path = file_backend._record_path("col", "key1")

        # Make the directory read-only to force os.replace to fail
        import stat
        path.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(OSError):
                await file_backend.write("col", "key1", {"v": 2})
        finally:
            path.parent.chmod(stat.S_IRWXU)

        # Verify no temp files left behind
        temps = list(path.parent.glob("*.tmp"))
        assert len(temps) == 0

        # Original file still intact
        result = await file_backend.read("col", "key1")
        assert result == {"v": 1}
```

**Covers**: `storage.py` lines 94-97.

### Phase 2: High-Impact Gaps (P1) -- 5 tests, ~45 min effort

**Expected coverage increase**: +1% (98% target)

#### Test 3: poll_all for unregistered entity
```python
async def test_poll_all_unregistered(self, svc: MessagingService) -> None:
    result = await svc.poll_all("agent://ghost")
    assert result == {}
```

#### Test 4: Idempotent channel join
```python
async def test_join_already_member(self, svc: MessagingService) -> None:
    # agent://a1 is already in project-alpha from fixture
    await svc.join_channel("project-alpha", "agent://a1")
    channels = await svc.list_channels()
    ch = next(c for c in channels if c.name == "project-alpha")
    assert ch.members.count("agent://a1") == 1  # No duplicate
```

#### Test 5: Leave channel when not a member
```python
async def test_leave_not_member(self, svc: MessagingService) -> None:
    await svc.create_channel(Channel(name="empty-ch"))
    # Leave without being a member -- should not raise
    await svc.leave_channel("empty-ch", "agent://a1")
```

#### Test 6: Leave nonexistent channel
```python
async def test_leave_nonexistent_channel(self, svc: MessagingService) -> None:
    with pytest.raises(ValueError, match="not found"):
        await svc.leave_channel("nonexistent", "agent://a1")
```

#### Test 7: Corrupted JSON in query
```python
async def test_query_skips_corrupted_file(self, file_backend: FileBackend) -> None:
    await file_backend.write("col", "good", {"id": "good"})
    # Write corrupted file directly
    bad_path = file_backend._record_path("col", "bad")
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text("{corrupted json")
    results = await file_backend.query("col")
    assert len(results) == 1
    assert results[0]["id"] == "good"
```

### Phase 3: Medium-Impact Gaps (P2) -- 7 tests, ~30 min effort

**Expected coverage increase**: +0.5% (98.5% target)

8. Pre-set `thread_id` with `reply_to` -- verify preservation
9. Pre-set `created` on entity -- verify no overwrite
10. Channel with >3 members display formatting
11. DLQ human-readable display with entries
12. Audit verify failure via CLI (tamper file, run verify, check exit code)
13. Blank lines in JSONL stream -- verify skip
14. `resolve_subscriptions` with non-URI entity_id

---

## 10. Success Criteria

| Metric | Current | After Phase 1 | After Phase 2 | Target |
|--------|---------|---------------|---------------|--------|
| Line coverage | 95.56% | ~97% | ~98% | >= 90% (core) |
| Branch coverage | ~90% | ~93% | ~95% | >= 75% |
| Partial branches | 17 | 13 | 8 | < 10 |
| Critical paths covered | 10/11 | 11/11 | 11/11 | 11/11 |
| Total tests | 143 | 145 | 150 | -- |

---

## 11. Recommendations

1. **Phase 1 is the only urgent work.** The sequence recovery test (Test 1) covers the highest-risk gap in the system -- message ordering corruption after restart. This should be added immediately.

2. **Phase 2 is worth doing for defense-in-depth.** The idempotency guards and crash recovery paths are the kind of code that fails silently in production and causes data corruption weeks later.

3. **Phase 3 can wait.** These are display formatting and edge cases that don't affect correctness.

4. **Do NOT chase 100%.** Lines 326-327, 331 in `cli.py` are unreachable or standard Python patterns. Line 101 in `audit.py` is an empty-set edge case. These are not worth testing.

5. **The test quality is high.** Behavior-focused assertions, parametrized backend tests, proper fixture isolation. The existing test suite is a good foundation.

6. **Consider adding one more integration test** for service restart continuity using FileBackend -- create service, send messages, destroy service, create new service against same directory, verify cursor positions and sequence numbers survive.
