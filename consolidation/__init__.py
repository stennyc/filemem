"""Extraction parameters for filemem consolidation pipeline.

All values here are user-tunable — nothing is hardcoded in the plugin.
Decay curve: effective_score = salience * exp(-lambda * age_days)
"""
DECAY_DEFAULTS = {
    "lambda_days": 0.05,
    "salience_threshold": 0.5,
    "salience_boost": 1.2,
    "ttl_days": {"L0": 30, "L1": 90, "L2": None},
    # P6-B (owner 2026-10-20): topic-guard 词面硬闸 (topic-guard 词面 hard
    # gate): a draft/candidate matching ANY decay.json topic_guard.block
    # phrase is blocked from auto-confirmation (kanban / 看板 relay noise
    # must never become a confirmed memory). enable=true + fail_open=false
    # = the active posture this owner runs; block is the keyword list
    # (hot-tunable in decay.json; the code-side defaults keep the two in
    # lockstep per the S2 egress parity test).
    "topic_guard": {
        "enable": True,
        "fail_open": False,
        "block": [],
    },
    "search": {
        "fallback": "keyword",
        # SA2-3: BM25 keyword-axis knobs (Okapi; read by search_index.py).
        "bm25_k1": 1.2,
        "bm25_b": 0.75,
        # SA2-4: graph skeleton knobs (tools/build_graph.py). graph_min_overlap
        # = pairwise token-overlap gate for semantic (BM25 top-3) edges;
        # graph_temporal_window_days = max event-time gap for temporal edges.
        "graph_min_overlap": 0.1,
        "graph_temporal_window_days": 7,
        # SA2-5 (owner 2026-11-12): graph full-build DETACH knobs. The 39s
        # full build now runs on a dedicated daemon thread (detached from the
        # tick thread's 10s join budget). Rebuilds are throttled by two
        # gates, mirroring S3's water-level override:
        #   graph_rebuild_water_level  — corpus rows added since the last full
        #       build before a re-build is warranted (default 50; user-tunable
        #       here in decay.json, e.g. 100 to rebuild less often).
        #   graph_rebuild_cooldown_min — min minutes between two full builds
        #       (default 30; a 39s build must not self-spin). 0 disables the
        #       cooldown (rebuild every time the water level is crossed).
        "graph_rebuild_water_level": 50,
        "graph_rebuild_cooldown_min": 30,
        # A1-1 (owner 2026-11-13): read-side pull throttle. The search hot-path
        # (_graph_pull_if_stale) stats the graph's freshness and, when stale,
        # spawns a background rebuild. This knob (default 5 min) throttles
        # how often that read-side pull may re-spawn, so the hot path never
        # stat-spawns on every single search. Deliberately INDEPENDENT of
        # graph_rebuild_cooldown_min (the tick-side proactive-patrol cooldown)
        # — a reactive read of a stale graph must catch up without waiting for
        # the 30-min tick window (re-applying that here would reintroduce the
        # "defer to a later window" dead-end SA2-5 fixed; a cold start with
        # _graph_last_build_ts==0 that the tick cooldown blocks forever would
        # still catch up on its first stale read).
        "graph_rebuild_pull_throttle_min": 5,
        # SA2-6 (owner 2026-11-12): per-kind edge-bucket quotas for the graph
        # full build (tools/build_graph.py). Pre-SA2-6 a single shared
        # _TOTAL_CAP=2000 pool let the high-volume temporal + co-occurrence
        # candidate lists crowd the high-information semantic axis down to
        # ~44/2000 slots on the live ~1500-node corpus. Now each kind gets
        # its own quota (fractions of 2000, must sum to <= 1.0; unused
        # quota in one kind is NOT redistributed to the others — that keeps
        # the truncation a single deterministic pass). Default mix:
        # semantic 55% (1100) / co-occurrence 22% (440) / temporal 23% (460).
        # (SA2-9 owner 2026-11-13: data-driven reweight — on the live
        #  ~1577-node corpus semantic has the smallest candidate list
        #  (~1595, the only under-fed axis) and the highest info density;
        #  50/30/20 gave semantic 51% utilization, 55/22/23 lifts it to
        #  58.6% (937/1595) while total kept rises 1818 -> 1837, no bucket
        #  starved: co-occurrence/temporal both still hit their caps.)
        # Re-weight by overriding this dict in decay.json's "search" block
        # (a partial dict REPLACES the whole set — supply all three kinds).
        "graph_bucket_quotas": {"semantic": 0.55, "co-occurrence": 0.22,
                                "temporal": 0.23},
        # SA2-7 (owner 2026-11-12, option B): anti-stacking guardrail for the
        # per-bucket weight-ordered truncation — no single node may consume
        # more than this fraction of its own bucket's kept slots (default
        # 2/3). Prevents a dense co-occurrence clique of a handful of
        # high-frequency nodes from refilling its own degree allowance and
        # eating the whole bucket (the SA2-7-v1 pure-weight-sort regression:
        # co-occurrence distinct-node coverage 493 -> 234, temporal 74 -> 55
        # on the live corpus). 1.0 = guardrail off (any node may fill its
        # bucket, closest to the pre-guardrail behavior, still deterministic).
        "graph_max_node_share": 0.6667,
        # EP-1: lineage 候选 gate（确定性零 LLM，确认面归 EP-2）.
        # supersede_min_overlap = Jaccard(token 集) of the NEW content vs an
        # existing same-type row; at-or-above it the old row becomes a
        # supersedes CANDIDATE (stamped, not yet a confirmed fact).
        # supersede_max_candidates = cap on how many candidates one save may
        # stamp (ties/determinism kept by ranking overlap desc, id asc).
        "supersede_min_overlap": 0.5,
        "supersede_max_candidates": 2,
        # C3: negation-channel gate (write-side parallel second lane, 确定性
        # 零 LLM, 确认面同样归 EP-2). c3_negation_min_recall = recall of the
        # NEW row's key concepts (negation/temporal/stopword-stripped) in an
        # older same-profile row; at-or-above it the old row becomes a
        # supersedes CANDIDATE when the new content carries a negation signal.
        # The 0.5 channel above is a PARALLEL lane and is untouched; the old-
        # row superseded flip is deferred to EP-2's confirm verdict (只扩池、
        # 不自动作废旧行).
        "c3_negation_min_recall": 0.5,
        # S6-note (S4-T2): opt-in alias-axis recall-gate relaxation. Default
        # False = the legacy substring gate is byte-identical (regression
        # green). True = a pool item the substring gate rejects still passes
        # when the RRF/bm25 axis ranked it at-or-above min_score — lets
        # off-gate alias-axis candidates through. Multi-lingual-neutral
        # (no hardcoded CJK string; read from the decay search block).
        "gate_relax_alias": False,
        # P0-2 (read-side isolation, auto-retrieve face ONLY): the ceiling on
        # how many rows the pre_llm_call auto-inject face may surface per
        # call (denoise ① of P1-2). A wide recall pool is NOT a token-spend:
        # the auto face injects at most this many top-ranked rows. The
        # explicit filemem_search face is unbounded by this gate. User-tunable
        # via decay.json search.auto_recall_max (0 disables the cap).
        "auto_recall_max": 3,
    },
    # O7: semantic-layer (memories/*.json) active-file size (MB) at which the
    # 4h sweeper sweeps expired auto-cleanup rows into monthly JSONL archives.
    "memories_rotate_mb": 1,
    # D4(a): LLM offline consolidation at the session-boundary hook layer.
    # max_llm_per_tick = burst-bounded cap on how many unscored episodic
    # entries get LLM-rescored per tick; overflow rolls to the next boundary.
    # Cost knob: keep low (default 10) to respect "consumption ∝ usefulness".
    # EP-2: supersede_max_confirms = per-tick cap on how many EP-1 supersedes
    # candidates the host-model confirmation face (llm_caller.confirm_supersede)
    # may judge per boundary tick; overflow rolls to the next tick. Parallel to
    # max_llm_per_tick — a separate budget, no overlap (candidate state flips
    # only; rescore and confirm run in the same async thread, rescore first).
    "consolidation": {"max_llm_per_tick": 10, "supersede_max_confirms": 5,
                     # S1b F1: interval throttle for the post_llm_call tick
                     # spawn gate (_maybe_spawn_consolidation_tick): the
                     # consolidation tick fires at most once per this many
                     # minutes per provider instance (0 = disabled — every
                     # turn spawns, in-flight guard still bounds concurrency).
                     # Default 30: production 3-day 0-tick root cause was the
                     # TRIGGER face (tick only reached via on_pre_compress /
                     # drafts-gated on_session_end), not the budget — a modest
                     # interval keeps the host turn zero-blocking while the
                     # North-Star audit trail stays sparse-and-quiet.
                     "consolidation_interval_min": 30,
                     # S3 mixed trigger (water-level override): the TIMER above
                     # stays the smoothing baseline; these three knobs add a
                     # PULL-AHEAD that fires a tick early when the unprocessed
                     # work backlog (_s3_water_level) crosses the threshold.
                     # water_level_override=True = factory-enabled (the S3
                     # faces are fully landed + test-gated + the counter is
                     # TTL-cached, so the early trigger is safe out-of-box);
                     # a user who wants the legacy timer-only rhythm can
                     # override to False in decay.json. threshold = the
                     # backlog count that justifies an early tick (0 disables
                     # the override even when it is enabled). cooldown_min =
                     # after one water-level trigger, no further water-level
                     # trigger for this many minutes (anti-storm bound; 0 =
                     # no cooldown = trigger whenever backlog stays high,
                     # which the 30-min timer still ultimately caps).
                     "water_level_override": True,
                     "water_level_threshold": 3,
                     "water_level_cooldown_min": 60,
                     # the _s3_water_level counter's provider-side TTL cache
                     # window: one full pool read per this many seconds
                     # (0 = no cache, rescan every gate call). The 60-min
                     # trigger cooldown caps TRIGGER frequency; this knob
                     # caps the COUNTER's read frequency so the hot
                     # post_llm_call path never rescans the full pool more
                     # than ~1/min.
                     "water_level_cache_ttl_s": 60,
                     # S3 ①②④ LLM face budgets (independent per-face N-caps,
                     # overflow rolls to the next boundary tick — same
                     # posture as max_llm_per_tick / supersede_max_confirms).
                     "semantic_merge_max_per_tick": 4,
                     "contradiction_max_per_tick": 4,
                     "aggregate_max_per_tick": 2,
                     # S3⑥ recurring-repetition (near-synonym directive) face:
                     # a dream-tick LLM lane that finds clusters of >=2 rows
                     # restating the SAME standing directive / red line and
                     # pushes a two-option clarify panel (sediment / dismiss) —
                     # it ASKS, it never merges at dream time. Independent
                     # N-cap of candidate clusters judged per tick (overflow
                     # rolls to the next boundary tick). Fail-soft: no panel
                     # on a broken/absent main_runtime.
                     "recurring_max_per_tick": 2,
                     # recurring_max_rows_per_cluster: P2-11 mega-cluster
                     # guard — the LLM verdict call receives at most this
                     # many (the NEWEST) rows of a cluster; the panel still
                     # carries the full member list for sediment. 0 = no cap
                     # (feed the whole cluster, legacy behavior).
                     "recurring_max_rows_per_cluster": 8,
                     # recurring_min_shared_tokens: the deterministic
                     # pre-filter floor — two rows connect into a recurring
                     # cluster candidate when they share at least this many
                     # DISTINCT content tokens (NOT a Jaccard ratio; reworded
                     # near-synonym directives share a handful of key tokens
                     # but their ratio is well below 0.5). 0 = disabled
                     # (every pair connects — bounded only by the N-cap).
                     # Default 2 is the sane floor.
                     "recurring_min_shared_tokens": 2,
                     # recurring_cooldown_days: after the user picks
                     # "dismiss" on a cluster, that cluster is not re-asked
                     # for this many days (the same cluster re-appearing
                     # still rolls, but the panel is suppressed inside the
                     # cooldown window; a re-ask after the window is fresh).
                     "recurring_cooldown_days": 7,
                     # S3 ⑤ status-triage face (deterministic, zero LLM): N-cap
                     # of UNREFERENCED live task rows surfaced per tick as a
                     # three-option clarify panel (已完成/已过时/待确认). Overflow
                     # rolls to the next boundary tick (same posture as the 4
                     # S3 LLM-face budgets above).
                     "triage_max_per_tick": 4,
                     # S3 ③ salience-decay auto-downweight (deterministic,
                     # zero LLM): enabled flag + the salience floor that
                     # refuted/stale rows sink toward.
                     "salience_decay_enabled": True,
                     "salience_decay_floor": 0.1,
                     # rows are only sunk when their last reference is OLDER
                     # than this many days (0 = sink any flagged row).
                     "salience_decay_age_days": 30,
                     # S1b F5: interval throttle for the prefetch
                     # recall/reinforce audit (recall_audit event): one
                     # low-frequency jsonl append per this many minutes,
                     # so a busy turn loop never floods consolidation_log.
                     "recall_audit_interval_min": 60,
                     # S4-T1 ②: consolidation_log line-cap rotation. The
                     # deterministic C-section _rotate_consolidation_log
                     # rotates ONLY when the active log exceeds
                     # log_rotate_lines: the front N-k lines MOVE to
                     # consolidation_log.<UTC-ts>.jsonl (audit trail, not
                     # deletion) and the active file keeps the most recent
                     # log_keep_tail_lines. 0/absent key = rotation
                     # disabled (fail-soft — the log keeps growing, data
                     # is never deleted by default).
                     "log_rotate_lines": 5000,
                     "log_keep_tail_lines": 500,
                     # Decided-ledger (2026-11-17 root-fix): the cross-session
                     # "already adjudicated, never re-ask" topic-suppression
                     # ledger (ledgers/decided.json). enable=false = full
                     # rollback to legacy re-asks. min_shared = the distinct-
                     # topic-token floor (mirrors recurring_min_shared_tokens):
                     # a reworded variant of a decided topic sharing >= this
                     # many tokens is treated as the SAME topic, so it is NOT
                     # re-enqueued (no re-ask) and a DISCARDED variant is NOT
                     # re-landed as a duplicate pool row.
                     "decided_ledger": {"enable": True, "min_shared": 9}},
    # EP-3: alias-mining knobs (deterministic local alias table — the LLM
    # face rides the host main-model path with zero model/provider config
    # keys; a separate budget parallel to
    # max_llm_per_tick / supersede_max_confirms, no overlap).
    # min_term_freq = a word must occur in >= this many DISTINCT memory
    # items (cross-item word-frequency, same tokenize pipeline as the
    # BM25 axis) to become a topic candidate; max_topics_per_tick = per-
    # tick cap on how many NEW topics get alias-mined (overflow rolls to
    # the next consolidation boundary; already-known topics are never
    # re-mined — incremental).
    # P3 topic_min_len / topic_stopwords = noise filter over the mined
    # topic candidates (drop single-char / config-stopword word tokens
    # before df counting; see the inline note under the aliases block).
    # EP-2: observation-layer knobs (stale 判定 config 驱动, 不硬编码阈值)。
    # stale_min_overlap = Jaccard(observation.tokens, new_fact.tokens) 阈值;
    # 达到即把该 topic 的 active observation 标 stale (等巩固 tick 重综合)。
    # observe_max_per_tick = T2 宿主模型 observe 作业每 tick 合成上限 (独立预算)。
    # EP-2 C slice: bounded auto-archive knobs for the raw_pending quarantine
    # pool (mirror of the memories append-only sweep). raw_pending_ttl_days =
    # a row older than this is auto-archived to knowledge/archive/ (append-only
    # JSONL, recoverable); raw_pending_max_rows = keep at most this many
    # raw_pending rows, archive the overflow. Both 0/absent = no auto-archive
    # (fail-soft default: a missing block never moves data). ALL origins are
    # age-gated (queue_overflow / catch_filter / llm_failed_raw /
    # obs_unresolved_provenance), not just queue_overflow. Default aligns the
    # I4 "manual clean >30" note.
    "knowledge": {
        "stale_min_overlap": 0.4,
        "observe_max_per_tick": 3,
        "raw_pending_ttl_days": 30,
        "raw_pending_max_rows": 30,
        # prune layer: FIFO retention for the C-slice raw_pending auto-archive
        # (knowledge/archive/raw_pending_auto_*). Rows older than this many
        # days are manifest-appended to the permanent pruned_index.jsonl, then
        # the archive file is unlinked. 0/absent = disabled (fail-soft: a
        # missing key never deletes an archive file). Default 90.
        "archive_prune_days": 90,
    },
    # S4-T1 ④: episodic-shard + session-trace age caps. The deterministic
    # C-section _cap_episodic_sessions moves DUE files into
    # knowledge/archive/ (recoverable — manifest row first, "忘掉≠抹掉"):
    #   * episodic.max_age_days (default 90): an episodic/YYYY/MM shard is
    #     DUE only when its month END is strictly before now - N, i.e.
    #     every row it holds is already past the window (the write face
    #     shards by WRITE month, so no in-window data can live there).
    #   * sessions.max_age_days (default 30): sessions/<sid>.jsonl not
    #     written for N days (mtime = the file's stamp; session filenames
    #     carry no timestamp) moves to the archive.
    # A 0 / absent block / absent key = that direction disabled
    # (fail-soft: a missing key never moves data). Only the two capped
    # prefixes are ever globbed — knowledge/archive/, memories/,
    # consolidated/ are never touched (bounded-glob, prune-layer
    # discipline).
    "episodic": {
        "max_age_days": 90,
    },
    "sessions": {
        "max_age_days": 30,
    },
    "aliases": {"min_term_freq": 3, "max_topics_per_tick": 5,
                # P3: alias-topic noise filter (config-driven, 多语言无关 —
                # the table is an English-noise/tech-generic word list, applied
                # EXACT-match over the lowercased [a-z0-9] word tokens BEFORE
                # df accumulation; CJK n-grams are never candidates, so the
                # zh mining path is untouched). A topic candidate is dropped
                # when len(w) < topic_min_len (single-char / single-digit
                # junk like 'a' / '1') OR w is in topic_stopwords. Absent
                # keys = filter OFF (pre-P3 configs keep the legacy
                # unfiltered behavior fail-soft — see
                # _alias_topic_frequencies).
                "topic_min_len": 2,
                "topic_stopwords": [
                    "the", "and", "for", "with", "from", "that", "this",
                    "are", "was", "not", "has", "have", "been", "true",
                    "false", "none", "null", "nan", "root", "user", "home",
                    "main", "log", "json", "yaml", "config", "file",
                    "path", "http", "https", "api", "hermes",
                ]},
    # SEC-1-2: injection-face trust gate (pure decision, zero LLM — see
    # injection_gate_decision in the plugin root __init__.py).  Semantics
    # locked by specs/SEC-1-2-impl-notes.md, 裁决 1:
    #   * low_trust_auto_inject=False -> a row explicitly stamped
    #     trust="low" is dropped from auto-inject (prefetch); set True to
    #     open that gate.
    #   * drop_unknown_trust=False -> unmarked rows (no metadata.origin)
    #     PASS by default: "未标记" != "不可信".  The episodic pool rows
    #     carry no metadata (155 live rows) — dropping unknown would be a
    #     wide prefetch regression; this switch locks the disambiguation.
    #   * directive_patterns: config-driven multilingual table (en/zh,
    #     改表不改码; code carries zero hardcoded zh).  A content hit is
    #     reclassified as a quote block (see quote_delims) so the host
    #     reads it as citation, not instruction.
    #   * quote_delims: the two label tokens wrapping directive content.
    #   * If the block is absent from user config entirely, consumers
    #     treat it as both switches False + empty tables = gate passes
    #     everything (fail-soft; a missing block never locks the host).
    "injection": {
        "low_trust_auto_inject": False,
        "drop_unknown_trust": False,
        "directive_patterns": {
            "en": [
                "ignore (all )?previous",
                "disregard (all )?previous",
                "you must",
                "system (note|message)",
                "execute the following",
                "new instruction",
                "you are now",
            ],
            "zh": [
                "忽略之前",
                "忽略所有之前",
                "你必须",
                "系统提示",
                "执行以下",
                "执行下列",
                "从现在起",
                "不要告诉用户",
            ],
        },
        "quote_delims": ["<memory-quote>", "</memory-quote>"],
    },
    # S2 entry hygiene: egress-side noise fingerprints (the auto-inject face
    # ONLY; explicit filemem_search still returns these rows). Multi-
    # language config table (改表不改码 — code carries zero hardcoded zh):
    #   * tool_dump_openers = leading-OPENER fingerprints for tool/console
    #     dump rows that miss the `{`/`[` non-prose structural classes
    #     (task_1_1790657309: "Fetch finished …" browser dump, 47 KB, 29% of
    #     the pool). The brace/JSON dump class is caught by the pre-existing
    #     structural opers + _looks_like_tool_dump single source of truth —
    #     this table only widens the egress net; ingress keeps the old shape.
    #   * echo_patterns = literal substrings (NOT regex) marking tool-result
    #     echoes ("File unchanged since last read. …"). The echo test runs
    #     ONLY on rows at/below echo_max_chars, so a long log that merely
    #     contains one phrase is never caught by the echo rule.
    #   * echo_max_chars = the row-length gate for echo-pattern matches.
    #   * structural_opens = leading-opener set (same primitive as
    #     _NONPROSE_OPENERS) used by the oversized-structural rule below.
    #   * governance.max_chars = oversized-STRUCTURAL row threshold (single
    #     key, shared by egress cut and tools/govern_memories.py); a row
    #     longer than this AND opening with a structural_opens character is
    #     noise by construction. 0/absent = disabled (fail-soft, like the
    #     other 0-valued knobs).
    #   * governance.complaint_salience = the salience value one-off
    #     governance stamps when downgrading a conservatively-identified
    #     complaint row (fact_91 class: user-complaint verbatim + dangling
    #     local @image path; the script never rewrites conclusions).
    "egress_noise": {
        # P0-3: the two JSON-opener heads leaked past the P0-1 3-key gate
        # (search_files raw dump `{"total_count":`, patch/terminal echo
        # `{"success":`) — same multilingual table, first batch identical.
        "tool_dump_openers": {
            "en": ["Fetch finished", "{\"total_count\":", "{\"success\":",
                   "⏸ [", "✔ [", "⏱ ["],
            "zh": ["Fetch finished", "{\"total_count\":", "{\"success\":",
                   "⏸ [", "✔ [", "⏱ ["],
        },
        # P0-3: process-fragment anchors (startswith semantics, 全句开头
        # 锚点 — substring matching is deliberately NOT done, 零误杀).
        "fragment_prefixes": {
            "en": ["搞清楚了。我把情况说明清楚"],
            "zh": ["搞清楚了。我把情况说明清楚"],
        },
        "echo_patterns": {
            "en": ["File unchanged"],
            "zh": [],
        },
        "echo_max_chars": 2048,
        "structural_opens": "{[",
        # complaint-class (fact_91) markers: candidate listing is
        # conservative — a row is a complaint candidate only when it has a
        # DANGLING @image ref AND its text (image lines stripped) ends with
        # a question terminator (?/？) or a complaint marker from this
        # table (多语言表, 改表不改码). The script never auto-judges:
        # candidates are listed; only --downgrade stamps the salience key
        # below. Manual rewrite happens OUTSIDE the script.
        "complaint_markers": {
            "zh": ["建议", "问题", "投诉", "报错", "污染", "异常"],
            "en": ["problem", "issue", "complaint"],
        },
        "governance": {
            "max_chars": 8000,
            "complaint_salience": 0.3,
        },
    },
    # P3-1: pre-queue LOW-VALUE DISCARD gate (1A 保守档, 通知面根治 — the
    # user-visible pending-confirm / [未决草稿] face is silenced for
    # low-value rows: 过程描述 / 工具输出碎句 no longer ask the user for a
    # confirm/discard decision on every turn). A row is silently discarded
    # BEFORE it enters the user-visible queue ONLY when it hits ANY one
    # leg (conservative — 钉死, 勿扩大):
    #   ① the shared P0-3 egress-noise OPENER tables (egress_noise.
    #      tool_dump_openers + fragment_prefixes, one table, config-driven);
    #   ② too short: len(normalized content) < no_popup.min_chars;
    #   ③ an EXPLICIT salience value below no_popup.salience_floor (a
    #      missing/absent salience — L0 auto / draft rows default to 0.5,
    #      i.e. AT the floor — is NOT a hit: the conservative posture does
    #      not mis-kill on salience; noise table + short fragment are the
    #      main legs). No leg hit = high-value → queued exactly as today.
    # enable=false (or a missing/malformed block) = FULL rollback to the
    # legacy queue-face behavior (every row queued). Read by
    # _no_popup_silent_discard (plugin root) — the same conservative
    # judgment the one-off tools/sweep_low_value.py sweep runs over the
    # historical 存量 (judgment uses the new code's semantics; one table
    # both faces, 改表不改码).
    "no_popup": {
        "enable": True,
        "min_chars": 40,
        "salience_floor": 0.5,
        # P3-2b leg ④: same-session draft-content dedupe (165735 retry-loop
        # 10x-flood root fix). Default ON — the draft face discards a repeat
        # of an already-present same-content draft (only the FIRST copy
        # lands); the user can opt out via decay.json. Kept in parity with
        # decay.json (S2 invariant: the ONLY tolerated top-level drift
        # between the two files is `search`).
        "dedupe_in_session": True,
    },
    # P1-4 notice-face key: cross_profile_skip_hint (default false) — optional
    # "（另有 N 行跨 profile 不列）" suffix on pending-draft notice lines.
    # Kept in parity with decay.json (S2 invariant: the ONLY tolerated
    # top-level drift between the two files is `search`).
    "notice": {
        "cross_profile_skip_hint": False,
    },
    # t_090d6f3d: type-classification fallback budget (the LLM reclassify face
    # that upgrades a regex `fact`-miss). enable=False (or a missing block)
    # = pure-regex verdicts only (zero LLM) — the exact legacy behavior, a
    # clean rollback. enable=True + main_runtime available: a `fact`-miss MAY
    # be reclassified by the host main-model classify_type face, capped at
    # llm_per_session LLM calls per session (a 0/absent cap = feature off).
    # Fail-soft: no runtime / over-cap / any host failure keeps the regex
    # `fact` verdict. Kept in parity with decay.json (S2 invariant: the ONLY
    # tolerated top-level drift between the two files is `search`).
    "memory_type": {
        "enable": True,
        "llm_per_session": 5,
    },
    # 2026-11-12 (owner 收口: probe budget 接线): the two live-body LLM probe
    # faces (plan-probe live detection + scheduled due-todo live detection)
    # are wired to decay.json plan_probe/scheduled_probe blocks in
    # FileMemProvider.initialize() — kept in lockstep with decay.json per
    # the S2 egress parity test (the ONLY tolerated top-level drift between
    # the two files is `search`, as before).
    "plan_probe": {
        "enable": True,
        "llm_per_session": 3,
    },
    "scheduled_probe": {
        "enable": True,
        "llm_per_session": 3,
    },
}
