# CI baseline: classified test failures on the fork's hosted runners

Source run: cwest/hermes-agent CI run 44662913396 at `cb7563d8bf` (`topic/fork-ci-runners`, PR #173), GitHub-hosted runners. Upstream base: NousResearch/hermes-agent `4c14d9df5b` (merge of #155293), whose own CI run 43747900192 passed every job, including all three Python slices, both macOS slices, all Windows lanes and JS & TS checks.

## How each failure was classified

- **(a) our stack patches:** the failure reproduces locally at the fork head and not at the upstream base, or its message names behaviour a fork patch changed. Fixed on this branch.
- **(b) upstream red at the same commit:** red in upstream's own CI at `4c14d9df5b`. Upstream's run is green on every job, so **no failure falls in (b)**.
- **(c) env-only on hosted runners:** red only on the fork's hosted runners. Every Python file listed below was re-run locally (macOS, 24 workers) at both the fork head and the upstream base with `scripts/run_tests.sh`; the result is the same at both commits, so the fork's patches do not cause it.

### Why the hosted runners fail these

Upstream runs the Python suite on `blacksmith-32vcpu-ubuntu-2404` with `HERMES_TEST_WORKERS: 32` (`.github/workflows/tests.yml`). The fork's runner rewrite (`scripts/ci/fork_hosted_runners.py`) moves the job to `ubuntu-latest` (4 vCPU) but keeps the 32 workers, so each slice runs 8 processes per core. The job log reports `parallelism: 32x` on 4 cores. Almost every failure is a wall-clock bound or a wait that ran out under that load:
`assert (t1 - t0) < 2` linear-time guards measured at 3-12 s, `timed out after 10.0s waiting for ...`, `subprocess.TimeoutExpired`, `condition was not reached`, `stream attempt 1 was superseded`. Each slice also printed 9-11 `FLAKY` files that failed and then passed on retry.

## Counts (run 44662913396)

| Job | Runner summary |
|---|---|
| Run tests (1/3) | 28791 passed, 39 failed, 35 of 2683 files failed, 10 flaky, 3 files no tests ran |
| Run tests (2/3) | 27641 passed, 67 failed, 49 of 2683 files failed, 11 flaky, 7 files no tests ran |
| Run tests (3/3) | 27448 passed, 35 failed, 29 of 2682 files failed, 9 flaky, 3 files no tests ran |
| Windows-only tests | 6 failed ids |
| Windows-only tests (arm64, 1/2) | 5 failed ids |
| macOS-only tests (2/2) | 1 failed id |

That is 141 tests failing on both attempts across the Python slices, plus 13 files that hit the 600 s file timeout before reporting, and 8 unique OS-lane ids. The log holds 177 unique `FAILED` ids in total; 38 of them are in files that passed on retry.

## (a) Fixed on this branch

| Test | CI failure | Fix |
|---|---|---|
| `tests/tools/test_multiplex_tool_memo_scope.py::test_skill_manage_schema_stable_but_creation_follows_active_profile` | AssertionError: {'success': False, 'error': "operations[0] (create on 'first-a') failed: skills.create_dir (/var/tmp/her | Commit `🧪 test(skills): create the skills.create_dir the multiplex test configures`: the `skills.create_dir` fail-closed patch refuses a missing dir; the test now creates it |

## (c) Env-only: failed on both attempts

Local columns: per-file result of `scripts/run_tests.sh` on macOS at the fork head (`7d1fe88a6a`, before the (a) fix) and at the upstream base `4c14d9df5b`. `N✓` is tests passed and `Ns` tests skipped. Where a file shows skips, the CI-failing test may be one of the skipped (Linux-only or Windows-only) tests, so that local result is weaker evidence; the classification then rests on upstream's green CI at the base and the timing shape of the failure.

| Test | CI job | CI failure | Local fork head | Local upstream base |
|---|---|---|---|---|
| `tests/agent/test_aux_summary_stream_hard_cancel.py::test_hard_cancel_closes_the_streamed_summary_at_its_next_chunk` | Run tests (1/3) | Failed: DID NOT RAISE AuxiliaryExplicitCancellation | pass (1✓) | pass (1✓) |
| `tests/agent/test_auxiliary_client.py::TestAuxUnhealthyCache::test_ttl_expiry_evicts` | Run tests (2/3) | AssertionError: assert False is True | pass (218✓) | pass (218✓) |
| `tests/agent/test_auxiliary_concurrency.py::TestSyncCallEnforcesLimit::test_call_llm_caps_concurrent_inflight` | Run tests (3/3) | AssertionError: assert 5 == 6 | pass (11✓) | pass (11✓) |
| `tests/agent/test_auxiliary_explicit_cancellation.py::test_protected_silent_provider_is_isolated_and_raises_frozen_explicit_cancel` | Run tests (2/3) | AssertionError: request never entered its silent transport | pass (13✓) | pass (13✓) |
| `tests/agent/test_compression_review.py::TestF1CommitOverrunWhileHung::test_overrun_warning_fires_while_commit_still_blocked` | Run tests (2/3) | assert False | pass (10✓) | pass (10✓) |
| `tests/agent/test_redact.py::TestPasswordOnlyConnStrings::test_repeated_scheme_prefixes_scan_in_linear_time` | Run tests (1/3) | AssertionError: redis://: | pass (313✓) | pass (313✓) |
| `tests/agent/test_redact.py::TestSecretFileAssignmentRedaction::test_unclassified_read_and_non_secret_scalars_are_untouched` | Run tests (1/3) | assert (460.264955901 - 455.536623212) < 1.0 | pass (313✓) | pass (313✓) |
| `tests/agent/test_redact_db_url_query.py::test_repeated_db_schemes_without_query_scan_in_linear_time` | Run tests (2/3) | AssertionError: assert (1252.072895628 - 1248.694440499) < 2.0 | pass (95✓) | pass (95✓) |
| `tests/agent/test_redact_db_url_query.py::test_template_sibling_chain_scans_in_linear_time` | Run tests (2/3) | AssertionError: assert (1247.196211876 - 1240.759212757) < 2.0 | pass (95✓) | pass (95✓) |
| `tests/agent/test_sequential_tool_interrupt.py::test_interrupt_abandons_noncooperative_tool` | Run tests (3/3) | assert 11.149999123000043 < 10.0 | pass (2✓) | pass (2✓) |
| `tests/agent/test_stream_max_lifetime.py::test_cap_cuts_a_stream_that_never_goes_quiet` | Run tests (1/3) | httpx.RemoteProtocolError: stream attempt 1 was superseded | pass (2✓) | pass (2✓) |
| `tests/agent/test_subagent_lifecycle.py::test_plugin_toolsets_are_known_toolsets` | Run tests (3/3) | AssertionError: assert <SubagentState.RUNNING: 'RUNNING'> is <SubagentState.SUCCEEDED: 'SUCCEEDED'> | pass (15✓) | pass (15✓) |
| `tests/agent/test_subagent_lifecycle.py::test_routing_delegation_provider_bundle` | Run tests (3/3) | AssertionError: assert <SubagentState.RUNNING: 'RUNNING'> is <SubagentState.SUCCEEDED: 'SUCCEEDED'> | pass (15✓) | pass (15✓) |
| `tests/agent/test_subagent_lifecycle.py::test_routing_request_provider_wins_without_mixing_bundles[True]` | Run tests (3/3) | AssertionError: assert <SubagentState.RUNNING: 'RUNNING'> is <SubagentState.SUCCEEDED: 'SUCCEEDED'> | pass (15✓) | pass (15✓) |
| `tests/gateway/test_api_admission_off_loop.py::test_admission_waits_for_the_writer_off_the_owner_loop[chat]` | Run tests (1/3) | AssertionError: owner loop stalled 1.39s behind the admission write | pass (3✓) | pass (3✓) |
| `tests/gateway/test_authority_stream.py::test_authority_stream_has_one_order_and_rejects_retired_callbacks[blocked]` | Run tests (1/3) | AssertionError: | pass (1✓ 1s) | pass (1✓ 1s) |
| `tests/gateway/test_busy_followup_processing_hooks.py::test_busy_followup_fires_processing_hooks_once` | Run tests (2/3) | AssertionError: condition not reached | pass (4✓) | pass (4✓) |
| `tests/gateway/test_buzz_websocket.py::test_websocket_loop_backs_off_and_publishes_retrying_on_clean_relay_close` | Run tests (1/3) | AssertionError: a clean relay close must publish retrying, got [] | pass (20✓) | pass (20✓) |
| `tests/gateway/test_cli_plugin_command_own_socket.py::test_a_gateway_hang_up_during_a_plugin_command_ends_the_wait_and_leaves_nothing_pending` | Run tests (2/3) | hermes_cli.gateway_client.GatewayClientError: Gateway connection failed; no local fallback | pass (2s) | pass (2s) |
| `tests/gateway/test_compression_failure_session_sync.py::test_empty_rate_limit_response_preserves_failure_metadata` | Run tests (3/3) | TimeoutError | pass (2✓) | pass (2✓) |
| `tests/gateway/test_compression_failure_session_sync.py::test_failed_turn_still_syncs_compression_session_split` | Run tests (3/3) | TimeoutError | pass (2✓) | pass (2✓) |
| `tests/gateway/test_control_socket_windows_live.py::test_named_pipe_identify_status_and_fleet_consumer` | Windows-only tests, Windows-only tests (arm64, 1/2) | AssertionError: identify returned None against a live pipe server | pass (2s) | pass (2s) |
| `tests/gateway/test_control_socket_windows_live.py::test_pipe_gone_after_kill_falls_back` | Windows-only tests, Windows-only tests (arm64, 1/2) | AssertionError: assert None is not None | pass (2s) | pass (2s) |
| `tests/gateway/test_hosted_mux_runtime.py::test_room_coordinator_submits_once_to_destination_authority` | Run tests (3/3) | Failed: no canonical member publication: {'running': True, 'working': True, 'blocked': False, 'counts': {'running': 1},  | pass (5✓) | pass (5✓) |
| `tests/gateway/test_hosted_room_runtime_attachments.py::test_authority_route_admits_a_member_turn_with_its_file` | Run tests (1/3) | AssertionError: timed out after 10.0s waiting for the canonical submit or a runtime error | pass (1✓) | pass (1✓) |
| `tests/gateway/test_hosted_stop_executor_recovery.py::test_saved_stop_finishes_before_slow_preparation_of_its_own_room` | Run tests (2/3) | assert False | pass (2✓ 11s) | pass (2✓ 11s) |
| `tests/gateway/test_journey_compress_restart_fifo.py::test_followers_survive_compression_and_restart_in_order_exactly_once[graceful]` | Run tests (1/3) | TimeoutError: timed out during opening handshake | pass (2s) | pass (2s) |
| `tests/gateway/test_journey_new_session_queued_work.py::test_new_session_never_absorbs_or_drops_earlier_queued_work[live]` | Run tests (2/3) | assert False | pass (2s) | pass (2s) |
| `tests/gateway/test_journey_new_session_queued_work.py::test_new_session_never_absorbs_or_drops_earlier_queued_work[restart]` | Run tests (2/3) | TimeoutError: timed out during opening handshake | pass (2s) | pass (2s) |
| `tests/gateway/test_local_automation.py::test_local_completion_survives_owner_kill_without_duplicate` | Run tests (3/3) | subprocess.TimeoutExpired: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/gen-94 | pass (5✓) | pass (5✓) |
| `tests/gateway/test_local_session.py::test_fresh_local_session_owns_execution_across_viewers[approval]` | Run tests (2/3) | subprocess.TimeoutExpired: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/gen-b9 | pass (2✓) | pass (2✓) |
| `tests/gateway/test_local_session.py::test_fresh_local_session_owns_execution_across_viewers[clarify]` | Run tests (2/3) | subprocess.TimeoutExpired: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/gen-b9 | pass (2✓) | pass (2✓) |
| `tests/gateway/test_managed_worker_images.py::test_admitted_images_reach_managed_worker[gpt-4o-managed]` | Run tests (2/3) | TimeoutError | pass (6✓) | pass (6✓) |
| `tests/gateway/test_managed_worker_images.py::test_admitted_images_reach_managed_worker[gpt-4o-safe]` | Run tests (2/3) | TimeoutError | pass (6✓) | pass (6✓) |
| `tests/gateway/test_managed_worker_images.py::test_admitted_images_reach_managed_worker[image-fixture-managed]` | Run tests (2/3) | TimeoutError | pass (6✓) | pass (6✓) |
| `tests/gateway/test_managed_worker_images.py::test_admitted_images_reach_managed_worker[image-fixture-normal]` | Run tests (2/3) | TimeoutError: timed out during opening handshake | pass (6✓) | pass (6✓) |
| `tests/gateway/test_managed_worker_images.py::test_admitted_images_reach_managed_worker[image-fixture-safe]` | Run tests (2/3) | TimeoutError | pass (6✓) | pass (6✓) |
| `tests/gateway/test_msgraph_authority.py::test_graph_boundary_commits_only_valid_notifications` | Run tests (2/3) | AssertionError: [msgraph_webhook] Notification admission failed | pass (1✓) | pass (1✓) |
| `tests/gateway/test_native_http_auth.py::test_native_http_grants_are_single_use_and_bound_to_daemon` | Run tests (2/3) | AssertionError: ({'protocol': 1, 'kind': 'hermes-gateway', 'pid': 119242, 'start_time': 211510, ...}, 'dashboard-auth-se | pass (1✓ 2s) | pass (1✓ 2s) |
| `tests/gateway/test_native_http_auth.py::test_native_http_principal_satisfies_route_local_token_policy` | Run tests (2/3) | AssertionError: ({'protocol': 1, 'kind': 'hermes-gateway', 'pid': 134407, 'start_time': 222226, ...}, 'dashboard-auth-se | pass (1✓ 2s) | pass (1✓ 2s) |
| `tests/gateway/test_native_ingress_trust.py::test_multiplex_transport_policy_is_not_runtime_secret_policy` | Run tests (1/3) | subprocess.TimeoutExpired: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/gen-31 | pass (4✓) | pass (4✓) |
| `tests/gateway/test_native_ingress_trust.py::test_restart_reauthorizes_current_connector_route_and_sender` | Run tests (1/3) | subprocess.TimeoutExpired: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/gen-31 | pass (4✓) | pass (4✓) |
| `tests/gateway/test_native_ingress_trust.py::test_server_stamped_ordinary_callback_reaches_real_turn_runner` | Run tests (1/3) | AssertionError: Traceback (most recent call last): | pass (4✓) | pass (4✓) |
| `tests/gateway/test_native_profile_create.py::test_native_profile_create_grant_is_route_bound_and_single_use` | Run tests (2/3) | AssertionError: ({'protocol': 1, 'kind': 'hermes-gateway', 'pid': 112597, 'start_time': 201410, ...}, 'dashboard-auth-se | pass (1s) | pass (1s) |
| `tests/gateway/test_normal_runtime_boot.py::test_normal_entrypoint_earns_authenticated_authority_readiness` | Run tests (2/3) | AssertionError: ({'protocol': 1, 'kind': 'hermes-gateway', 'pid': 115487, 'start_time': 205195, ...}, 'config migration  | pass (1✓ 1s) | pass (1✓ 1s) |
| `tests/gateway/test_owner_loop_claim_read.py::test_claim_pending_read_keeps_the_owner_loop_ticking` | Run tests (1/3) | AssertionError: owner loop stalled 4.05s behind the claim read | pass (1✓) | pass (1✓) |
| `tests/gateway/test_owner_loop_writes_create_api.py::test_session_create_commit_never_runs_on_the_owner_loop_thread` | Run tests (3/3) | TimeoutError | pass (2✓) | pass (2✓) |
| `tests/gateway/test_parked_profile_ingress.py::test_a_coalesced_webhook_for_a_parked_profile_is_retried_not_lost` | Run tests (2/3) | AssertionError: timed out after 10.0s waiting for the retried batch dispatched | pass (16✓) | pass (16✓) |
| `tests/gateway/test_parked_profile_unserved_ingress.py::test_a_coalesced_webhook_profile_routes_send_to_a_parked_profile_is_retried_not_lost` | Run tests (3/3) | AssertionError: timed out after 10.0s waiting for the retried batch dispatched | pass (5✓) | pass (5✓) |
| `tests/gateway/test_plugin_idle_task_housekeeping.py::test_a_run_that_ends_before_the_stop_is_recorded` | Run tests (2/3) | assert False | pass (5✓) | pass (5✓) |
| `tests/gateway/test_plugin_idle_task_housekeeping.py::test_a_slow_idle_task_does_not_delay_the_next_chore_or_the_heartbeat` | Run tests (2/3) | assert False | pass (5✓) | pass (5✓) |
| `tests/gateway/test_plugin_idle_task_housekeeping.py::test_after_an_in_process_restart_only_the_new_housekeeping_records` | Run tests (2/3) | assert (False) | pass (5✓) | pass (5✓) |
| `tests/gateway/test_plugin_idle_task_housekeeping.py::test_stop_waits_for_a_run_that_ends_within_its_budget` | Run tests (2/3) | assert (False) | pass (5✓) | pass (5✓) |
| `tests/gateway/test_plugin_idle_task_housekeeping.py::test_stop_with_a_run_going_returns_within_its_budget_and_nothing_runs_or_records_after` | Run tests (2/3) | assert (False) | pass (5✓) | pass (5✓) |
| `tests/gateway/test_profile_create_through_gateway.py::test_cli_creates_through_a_live_gateway_local_and_by_url` | Run tests (2/3) | AssertionError: ({'protocol': 1, 'kind': 'hermes-gateway', 'pid': 148034, 'start_time': 229978, ...}, 'dashboard-auth-se | pass (3✓) | pass (3✓) |
| `tests/gateway/test_reset_button_deadlock.py::test_reset_completes_when_cleanup_raises` | Run tests (1/3) | TimeoutError | pass (3✓) | pass (3✓) |
| `tests/gateway/test_runner_compressed_receipt.py::test_runner_compression_keeps_the_replacement_transcript_in_the_receipt[in_place]` | Run tests (2/3) | TimeoutError | pass (3✓) | pass (3✓) |
| `tests/gateway/test_runner_compressed_receipt.py::test_runner_compression_keeps_the_replacement_transcript_in_the_receipt[rotated]` | Run tests (2/3) | TimeoutError | pass (3✓) | pass (3✓) |
| `tests/gateway/test_runner_compressed_receipt.py::test_runner_passes_the_proven_turn_boundary_so_a_nudge_cannot_erase_tool_output` | Run tests (2/3) | TimeoutError | pass (3✓) | pass (3✓) |
| `tests/gateway/test_runtime_token_identity.py::test_ordinary_daemon_keeps_control_auth_and_loop_witness[long-temp]` | Run tests (1/3) | TimeoutError: timed out during opening handshake | pass (1✓ 4s) | pass (1✓ 4s) |
| `tests/gateway/test_runtime_token_identity.py::test_ordinary_daemon_keeps_control_auth_and_loop_witness[long]` | Run tests (1/3) | TimeoutError: timed out during opening handshake | pass (1✓ 4s) | pass (1✓ 4s) |
| `tests/gateway/test_runtime_token_identity.py::test_ordinary_daemon_keeps_control_auth_and_loop_witness[unicode]` | Run tests (1/3) | TimeoutError: timed out during opening handshake | pass (1✓ 4s) | pass (1✓ 4s) |
| `tests/gateway/test_session_hosted_daemon.py::test_room_unknown_discard_releases_only_its_followers` | Run tests (1/3) | AssertionError: the follower never settled after discard: {'running': True, 'working': True, 'blocked': False, 'counts': | pass (3✓) | pass (3✓) |
| `tests/gateway/test_session_hygiene_turnhold_adoption.py::test_turn_hold_keeps_admission_and_adopts_watermark_fenced_summary` | Run tests (3/3) | AssertionError: turn held for 9.1s despite the turn-hold budget | pass (3✓) | pass (3✓) |
| `tests/gateway/test_session_images.py::test_authenticated_daemon_upload_retains_exact_admitted_bytes` | Run tests (1/3) | TimeoutError: timed out during opening handshake | pass (2✓) | pass (2✓) |
| `tests/gateway/test_session_listener_lazy_dashboard.py::test_session_listener_serves_native_ws_before_importing_the_dashboard` | Run tests (1/3) | AssertionError: b/python3.14/asyncio/base_events.py", line 720, in run_until_complete | pass (1✓) | pass (1✓) |
| `tests/gateway/test_session_llm_oneshot.py::test_a_burst_of_slow_oneshots_never_starves_the_rest_of_the_gateway` | Run tests (2/3) | TimeoutError: timed out during opening handshake | pass (4s) | pass (4s) |
| `tests/gateway/test_session_model_command_commits.py::test_typed_model_command_on_a_local_session_moves_the_next_request` | Run tests (2/3) | TimeoutError: timed out during opening handshake | pass (2s) | pass (2s) |
| `tests/gateway/test_session_mutation_cold.py::test_cold_import_mutation_binds_owner_across_restart` | Run tests (3/3) | httpx.ReadTimeout: timed out | pass (1s) | pass (1s) |
| `tests/gateway/test_session_mutation_compress.py::test_compress_preserves_configured_history_and_admission_owner[safe-None]` | Run tests (1/3) | AssertionError: timed out after 30s waiting for HISTORY_0 terminal | pass (3✓) | pass (3✓) |
| `tests/gateway/test_session_mutation_compress_managed.py::test_managed_compress_preserves_configured_history_and_admission_owner[False]` | Run tests (2/3) | AssertionError: timed out after 30s waiting for HISTORY_0 terminal | pass (2✓) | pass (2✓) |
| `tests/gateway/test_session_mutation_compress_managed.py::test_managed_compress_preserves_configured_history_and_admission_owner[True]` | Run tests (2/3) | AssertionError: timed out after 30s waiting for HISTORY_0 terminal | pass (2✓) | pass (2✓) |
| `tests/gateway/test_session_mutation_import_owner.py::test_import_atomically_binds_each_new_history_to_its_importer` | Run tests (1/3) | httpx.ReadTimeout: timed out | pass (1s) | pass (1s) |
| `tests/gateway/test_session_mutation_lineage_delete.py::test_delete_after_reset_covers_physical_route_and_cache_generation` | Run tests (2/3) | AssertionError: ({'protocol': 1, 'kind': 'hermes-gateway', 'pid': 145047, 'start_time': 228333, ...}, 'dashboard-auth-se | pass (1s) | pass (1s) |
| `tests/gateway/test_session_mutation_reset.py::test_reset_receipt_and_physical_target_survive_native_owner_restart` | Run tests (2/3) | AssertionError: ({'protocol': 1, 'kind': 'hermes-gateway', 'pid': 147084, 'start_time': 229480, ...}, 'dashboard-auth-se | pass (1s) | pass (1s) |
| `tests/gateway/test_session_mutation_retirement.py::test_native_delete_exact_retry_survives_owner_restart` | Run tests (3/3) | TimeoutError: timed out | pass (1✓ 1s) | pass (1✓ 1s) |
| `tests/gateway/test_session_plugin_rpc.py::test_plugin_frontend_gets_schema_checked_json_from_its_own_task_with_input_redacted[in_process]` | Run tests (2/3) | AssertionError: timed out after 20s waiting for the four held model calls | pass (6✓) | pass (6✓) |
| `tests/gateway/test_session_tui_ops_session_diff.py::test_an_escape_heavy_session_diff_fits_the_clients_frame_and_names_its_cut` | Run tests (2/3) | TimeoutError: timed out during opening handshake | pass (2✓ 2s) | pass (2✓ 2s) |
| `tests/gateway/test_session_tui_ops_session_diff.py::test_session_diff_follows_the_sessions_checkpoint_policy_and_keeps_the_whole_body` | Run tests (2/3) | TimeoutError: timed out during opening handshake | pass (2✓ 2s) | pass (2✓ 2s) |
| `tests/gateway/test_session_ws_upgrade_failure_logged.py::test_a_failed_session_upgrade_is_logged_with_its_cause` | Run tests (2/3) | AssertionError: ('timed out after 35s waiting for the owner to be ready', {'protocol': 1, 'kind': 'hermes-gateway', 'pid | pass (2✓ 1s) | pass (2✓ 1s) |
| `tests/gateway/test_shared_clarify.py::test_real_shared_clarify_survives_detach` | Run tests (2/3) | subprocess.TimeoutExpired: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/gen-b9 | pass (2✓) | pass (2✓) |
| `tests/gateway/test_shared_session_controls.py::test_control_identity_rejects_foreign_and_retired_workers` | Run tests (1/3) | AssertionError: approval prompt 0 was never published | pass (2✓) | pass (2✓) |
| `tests/gateway/test_sibling_attach_while_profile_starting.py::test_a_running_profile_is_answered_while_a_frame_waits_for_another_profile_to_start` | Run tests (1/3) | TimeoutError | pass (10✓) | pass (10✓) |
| `tests/gateway/test_single_profile_host_serves_new_profile.py::test_profile_created_after_a_single_profile_boot_is_served_without_a_restart` | Run tests (1/3) | assert False is True | pass (3✓ 1s) | pass (3✓ 1s) |
| `tests/gateway/test_static_token_session_mutations.py::test_static_token_http_mutations_reuse_ws_owner` | Run tests (3/3) | TimeoutError: timed out during opening handshake | pass (1✓) | pass (1✓) |
| `tests/gateway/test_surface_admission.py::test_managed_worker_turn_carries_the_committed_surface_to_the_model` | Run tests (3/3) | Failed: Timed out waiting for the admitted turn | pass (4✓) | pass (4✓) |
| `tests/gateway/test_telegram_rich_messages.py::test_rich_payload_is_linear_in_unclosed_backtick_runs` | Run tests (3/3) | assert (1226.755918491 - 1221.815159006) < 1.0 | pass (64✓) | pass (64✓) |
| `tests/gateway/test_unified_gateway_native_live.py::test_native_gateway_runtime_live` | Run tests (1/3) | TimeoutError: timed out | pass (3✓) | pass (3✓) |
| `tests/gateway/test_used_delete_restart.py::test_used_api_and_native_delete_restart_exact_retry` | Run tests (1/3) | httpx.ReadTimeout: timed out | pass (1s) | pass (1s) |
| `tests/gateway/test_webhook_authority.py::test_signed_webhook_storage_failure_is_retryable` | Run tests (3/3) | AssertionError: [webhook] Invalid signature for route fixture | pass (1✓) | pass (1✓) |
| `tests/gateway/test_whatsapp_bridge_port_release.py::TestPortReleasedBeforeSpawn::test_slow_exiting_bridge_is_escalated_and_port_is_free_at_spawn` | Run tests (1/3) | AssertionError: connect() blocked the event loop | pass (7s) | pass (7s) |
| `tests/gateway/test_worker_compression_admission.py::test_automatic_worker_rotation_retains_retry_identity_and_fifo[finish]` | Run tests (2/3) | AssertionError: {'seq': 1, 'admission_id': '64926483a6ad4233a9c5433699257f5a', 'request_id': 'pressure-0', 'principal_id | pass (2✓) | pass (2✓) |
| `tests/gateway/test_worker_compression_admission.py::test_automatic_worker_rotation_retains_retry_identity_and_fifo[kill]` | Run tests (2/3) | AssertionError: {'seq': 1, 'admission_id': 'ed21b43bac3d4acf852a93be0a2df41a', 'request_id': 'pressure-0', 'principal_id | pass (2✓) | pass (2✓) |
| `tests/gateway/test_worker_persistence.py::test_worker_survives_owner_restart_without_repeating_tool` | Run tests (2/3) | TimeoutError: timed out during opening handshake | pass (1s) | pass (1s) |
| `tests/gateway/test_worker_rotation_adoption.py::test_rotated_agent_survives_owner_kill_and_explicit_adoption` | Run tests (2/3) | AssertionError: 🗜️ Compacting context — summarizing earlier conversation so I can continue... | pass (1s) | pass (1s) |
| `tests/hermes_cli/test_browse_schema_drift.py::test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out[ddl]` | macOS-only tests (2/2) | gateway.runtime_ownership.OwnershipConflict: Gateway runtime already owns profile /private/var/tmp/hermes-pytest-501/r-2 | FAIL (27 tests) | FAIL (27 tests) |
| `tests/hermes_cli/test_cli_goal_parked_resume.py::test_idle_hook_queues_the_continuation_when_a_timed_barrier_has_elapsed` | Run tests (2/3) | assert False | pass (2✓) | pass (2✓) |
| `tests/hermes_cli/test_cli_light_mode.py::TestOsc11Da1Fence::test_herdr_style_da1_only_returns_none_without_leak` | Run tests (2/3) | AssertionError: assert None == 'None' | pass (24✓ 2s) | pass (24✓ 2s) |
| `tests/hermes_cli/test_cli_light_mode.py::TestOsc11Da1Fence::test_slow_inorder_reply_is_consumed_not_leaked` | Run tests (2/3) | assert None == "b''" | pass (24✓ 2s) | pass (24✓ 2s) |
| `tests/hermes_cli/test_desktop_release_holder_windows_live.py::test_consented_retry_closes_exactly_the_holder_and_promotes` | Windows-only tests (arm64, 1/2) | assert not [1652] | pass (5s) | pass (5s) |
| `tests/hermes_cli/test_local_runtime_processes.py::test_failed_setup_never_runs_child_and_releases_handles[assign]` | Windows-only tests, Windows-only tests (arm64, 1/2) | AssertionError: assert 'running' == 'stopped' | pass (8s) | pass (8s) |
| `tests/hermes_cli/test_local_runtime_processes.py::test_failed_setup_never_runs_child_and_releases_handles[resume]` | Windows-only tests, Windows-only tests (arm64, 1/2) | AssertionError: assert 'running' == 'stopped' | pass (8s) | pass (8s) |
| `tests/hermes_cli/test_plugin_secondary_profile_host.py::test_hung_hosted_request_answers_504_and_the_host_serves_the_next` | Run tests (3/3) | assert (504, 200) == (200, 200) | pass (36✓) | pass (36✓) |
| `tests/hermes_cli/test_serve_skill_maintenance.py::test_removed_sessions_keep_profile_idle_watermark` | Run tests (2/3) | assert 5.899600982666016 < 2 | pass (2✓) | pass (2✓) |
| `tests/hermes_cli/test_session_recovery_lost_and_found.py::test_unreadable_schema_without_cli_names_the_sqlite3_requirement` | Run tests (3/3) | AssertionError: assert '.recover' in 'Partial recovery requires a page-level salvage shell, but the only sqlite3 CLI on  | pass (17✓) | pass (17✓) |
| `tests/hermes_cli/test_sessions_adopt.py::test_adopting_old_chats_lets_the_browser_login_continue_them` | Run tests (3/3) | TimeoutError: timed out during opening handshake | pass (4✓ 2s) | pass (4✓ 2s) |
| `tests/hermes_cli/test_shared_metrics_prepared_compaction.py::test_waste_modes_say_why` | Run tests (3/3) | AssertionError: assert ('discarded', 'none', 'wait_timeout', 'none', 'lt_1s') in [('compaction_failed', 'none', 'none',  | pass (3✓) | pass (3✓) |
| `tests/hermes_cli/test_taskkill_identity_windows_live.py::TestTerminatePidIdentityLive::test_matching_identity_kills_real_process` | Windows-only tests | OSError: ERROR: The process with PID 4696 (child process of PID 3144) could not be terminated. | pass (7s) | pass (7s) |
| `tests/hermes_cli/test_update_venv_holder_retirement.py::test_command_reaches_checkout_preparation_without_holder_gates` | Run tests (3/3) | AssertionError: Expected 'mock' to not have been called. Called 1 times. | pass (12✓) | pass (12✓) |
| `tests/hermes_cli/test_worktree_snapshot_prune.py::test_a_tree_resumed_mid_sweep_is_never_removed` | Run tests (2/3) | AssertionError: assert ['archived, k.../lane-staged'] == ['archived, k.../lane-staged'] | FAIL (9✓ 9✗) | FAIL (9✓ 9✗) |
| `tests/scripts/desktop_update/test_desktop_update_windows_pipe_drain.py::test_update_step_survives_pipe_leak_flood_and_live_child_stall` | Windows-only tests | Failed: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx | pass (1s) | pass (1s) |
| `tests/tools/test_approval.py::TestQuotedCommandWordVariants::test_many_quoted_command_words_stay_bounded_in_both_passes` | Run tests (1/3) | AssertionError: detection took 8.30s for a 16339-char command | pass (172✓ 1s) | pass (172✓ 1s) |
| `tests/tools/test_approval_detection_backtracking.py::test_blank_line_heredoc_finishes_end_to_end` | Run tests (1/3) | subprocess.CalledProcessError: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/ge | pass (22✓) | pass (22✓) |
| `tests/tools/test_approval_detection_backtracking.py::test_nesting_minted_by_the_escape_strip_finishes_quickly` | Run tests (1/3) | subprocess.TimeoutExpired: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/gen-31 | pass (22✓) | pass (22✓) |
| `tests/tools/test_approval_git_force_push.py::test_a_run_inside_the_push_segment_does_not_backtrack_exponentially[&>]` | Run tests (2/3) | AssertionError: assert (3682.566530532 - 3679.55421423) < 2 | pass (73✓) | pass (73✓) |
| `tests/tools/test_approval_git_force_push.py::test_a_run_inside_the_push_segment_does_not_backtrack_exponentially[>&]` | Run tests (2/3) | AssertionError: assert (3676.853873189 - 3673.963583359) < 2 | pass (73✓) | pass (73✓) |
| `tests/tools/test_approval_git_force_push.py::test_a_run_inside_the_push_segment_does_not_backtrack_exponentially[>|]` | Run tests (2/3) | AssertionError: assert (3685.391022595 - 3682.788853564) < 2 | pass (73✓) | pass (73✓) |
| `tests/tools/test_approval_git_global_options.py::test_options_outside_the_value_list_still_reach_the_subcommand` | Run tests (3/3) | AssertionError: assert (1990.802390598 - 1985.940690313) < 2 | pass (67✓) | pass (67✓) |
| `tests/tools/test_approval_windows_hardline_scripts.py::test_bare_cr_runs_scan_in_linear_time[cr-delete-operand]` | Run tests (1/3) | assert (2288.574489361 - 2279.653037378) < 2 | pass (121✓) | pass (121✓) |
| `tests/tools/test_approval_windows_hardline_scripts.py::test_bare_cr_runs_scan_in_linear_time[cr-delete-verb]` | Run tests (1/3) | assert (2277.597923021 - 2264.886291496) < 2 | pass (121✓) | pass (121✓) |
| `tests/tools/test_async_approval_lease.py::test_child_approval_after_parent_finished[cancel]` | Run tests (2/3) | AssertionError: child approval disappeared after parent unregistered | pass (8✓) | pass (8✓) |
| `tests/tools/test_async_approval_lease.py::test_child_approval_after_parent_finished[deny]` | Run tests (2/3) | AssertionError: child approval disappeared after parent unregistered | pass (8✓) | pass (8✓) |
| `tests/tools/test_async_approval_lease.py::test_child_approval_after_parent_finished[once]` | Run tests (2/3) | AssertionError: child approval disappeared after parent unregistered | pass (8✓) | pass (8✓) |
| `tests/tools/test_async_approval_lease.py::test_child_approval_after_parent_finished[timeout]` | Run tests (2/3) | AssertionError: child approval disappeared after parent unregistered | pass (8✓) | pass (8✓) |
| `tests/tools/test_delegate_capacity_interrupt.py::test_rejected_background_child_stops_with_parent[soft-running-capacity]` | Run tests (3/3) | TimeoutError | pass (13✓) | pass (13✓) |
| `tests/tools/test_delegate_stale_heartbeat_collect.py::test_stale_verdict_collects_a_result_that_lands_within_the_grace_window` | Run tests (1/3) | assert False | pass (3✓) | pass (3✓) |
| `tests/tools/test_forget_rules_cron_and_policy.py::test_scan_for_threats_is_linear_on_long_words` | Run tests (2/3) | assert (3813.005539963 - 3805.845632413) < 2 | pass (14✓) | pass (14✓) |
| `tests/tools/test_local_env_blocklist.py::test_terminal_child_observes_declared_policy` | Run tests (3/3) | AssertionError: {'output': '[Command timed out after 30s]', 'returncode': 124, 'hermes_timed_out': True} | pass (92✓ 4s) | pass (92✓ 4s) |
| `tests/tools/test_mcp_elicitation_form.py::test_no_question_opens_after_the_elicitation_deadline` | Run tests (2/3) | assert 0 == 1 | pass (25✓) | pass (25✓) |
| `tests/tools/test_process_heartbeat.py::test_heartbeat_carries_only_new_output_and_stops_at_exit` | Run tests (2/3) | AssertionError: [{'type': 'completion', 'session_id': 'proc_50e601dfafbc', 'session_key': '', 'task_id': '', ...}] | pass (4✓ 2s) | pass (4✓ 2s) |
| `tests/tools/test_process_registry_list_exit.py::test_list_reconciles_real_exit_without_consuming_owned_result` | Run tests (1/3) | AssertionError: {"direct_child": 0, "listed": [{"session_id": "proc_cbeebbdd86f4", "command": "exec /home/runner/work/_t | pass (2✓ 1s) | pass (2✓ 1s) |
| `tests/tools/test_skills_guard.py::TestScanSkill::test_minified_json_line_scans_in_linear_time_and_still_flags_link_exfil` | Run tests (2/3) | assert (4023.087749909 - 4014.908714744) < 2 | pass (152✓) | pass (152✓) |
| `tests/tools/test_skills_guard.py::TestScanSkill::test_provider_key_scan_is_linear_in_line_length` | Run tests (2/3) | assert (4011.73531829 - 4004.299354557) < 2 | pass (152✓) | pass (152✓) |
| `tests/tools/test_subagent_process_handoff.py::test_handed_off_process_completion_reaches_parent_and_leftover_is_reported` | Run tests (2/3) | KeyError: 'status' | pass (2✓) | pass (2✓) |
| `tests/tools/test_subprocess_stdin_guard.py::test_all_tui_subprocess_calls_have_stdin` | Run tests (1/3) | subprocess.TimeoutExpired: Command '['/home/runner/work/_temp/setup-pm/installs/7c611529583687b4/test-environment/gen-31 | pass (4✓) | pass (4✓) |
| `tests/tools/test_threat_patterns.py::TestReDoSHardening::test_long_near_miss_runtime_is_bounded` | Run tests (1/3) | assert 5.702313743000104 < 2.0 | pass (76✓) | pass (76✓) |
| `tests/tui_gateway/test_connector_operation_rpc.py::test_a_failed_reissue_leaves_the_row_failed_with_no_link[failed]` | Run tests (2/3) | AssertionError: no reply | pass (9✓) | pass (9✓) |
| `tests/tui_gateway/test_connector_operation_rpc.py::test_panel_connect_reissues_only_a_dead_link` | Run tests (2/3) | AssertionError: no reply | pass (9✓) | pass (9✓) |

The Windows-lane tests are marked `platforms("windows")`; on macOS they skip (`Ns` above means N skipped), so the local columns say nothing about them. They are classified (c) because upstream's Windows-only lanes passed at `4c14d9df5b`, the fork changes no file they exercise (`git diff 4c14d9df5b cb7563d8bf` touches no Windows process, pipe or taskkill code), and the failures are process-kill and pipe timing on a smaller runner: `ubuntu`/`windows-latest-32-core` larger runners became 4-vCPU `windows-latest`.

### Files that hit the 600 s per-file timeout on CI (no result reported)

| File | Local fork head | Local upstream base |
|---|---|---|
| `tests/acp_adapter/test_gateway_editor_mcp.py` | pass (5s) | pass (5s) |
| `tests/agent/test_error_classifier.py` | pass (192✓) | pass (192✓) |
| `tests/agent/test_run_agent.py` | pass (277✓) | pass (277✓) |
| `tests/ci/test_update_ci_routing.py` | pass (71✓ 35s) | pass (71✓ 35s) |
| `tests/gateway/test_child_notice_profile_admission.py` | FAIL (5 tests) | FAIL (5 tests) |
| `tests/gateway/test_hosted_group_members.py` | pass (7✓) | pass (7✓) |
| `tests/gateway/test_managed_worker_launch.py` | pass (7s) | pass (7s) |
| `tests/gateway/test_managed_worker_lineage.py` | pass (10✓) | pass (10✓) |
| `tests/gateway/test_multiplex_hot_serve_live.py` | pass (9s) | pass (9s) |
| `tests/hermes_cli/test_gateway_tickets.py` | pass (51✓ 5s) | pass (51✓ 5s) |
| `tests/hermes_cli/test_update_finish.py` | pass (14✓ 2s) | pass (14✓ 2s) |
| `tests/scripts/test_icon_flavors.py` | pass (12✓) | pass (12✓) |
| `tests/tools/test_approval_scan_fallback_coverage.py` | pass (33✓) | pass (33✓) |

### Local failures that are this machine, not the fork

Three files also fail locally, identically at the fork head and the upstream base, so they are not caused by the fork's patches:
- `tests/hermes_cli/test_worktree_snapshot_prune.py`: on this Mac the test isolation guard trips (`TEST BUG: file I/O against the REAL hermes home`) because a real `~/.hermes` exists. On CI the one failure is a clock race: `changed 1s ago` vs `changed 0s ago`.
- `tests/gateway/test_child_notice_profile_admission.py` and `tests/hermes_cli/test_browse_schema_drift.py`: hit the 300 s local file timeout at both commits.

## (c) Env-only: flaky on CI (failed once, passed on retry)

These did not fail their job by themselves; they are listed because their `FAILED` lines are in the log.

| Test | CI job | First-attempt failure |
|---|---|---|
| `tests/acp_adapter/test_gateway_editor_wire.py::test_acp_load_from_another_folder_moves_the_session_there` | Run tests (3/3) | TimeoutError: timed out during opening handshake |
| `tests/acp_adapter/test_gateway_model_pick_provider.py::test_cross_provider_pick_switches_provider_and_runs_the_next_turn_there[daemon0]` | Run tests (1/3) | TimeoutError: timed out during opening handshake |
| `tests/acp_adapter/test_gateway_service_tier.py::test_tier_picker_changes_only_its_session_and_tracks_other_viewers[daemon0]` | Run tests (2/3) | TimeoutError: timed out during opening handshake |
| `tests/acp_adapter/test_gateway_wire_controls.py::test_real_acp_answer_resolves_the_exact_pending_control` | Run tests (3/3) | AssertionError: timed out after 15s waiting for the ACP permission request |
| `tests/agent/test_interrupt_propagation.py::TestInterruptPropagationToChild::test_interrupt_during_child_api_call_detected` | Run tests (1/3) | AssertionError: Took 1.08s to detect interrupt (expected < 1.0s) |
| `tests/agent/test_moa_cold_start_cache.py::test_preset_resolution_is_cached_across_create_calls` | Run tests (3/3) | AssertionError: expected 1 preset resolution, got 2 |
| `tests/agent/test_over_window_compression_fail_closed.py::test_over_window_wait_is_bounded_by_one_inactivity_budget` | Run tests (3/3) | assert 1.7973074039999801 < 1.5 |
| `tests/agent/test_stream_stale_kill_unwedge.py::test_wedged_stream_unwinds_within_its_stale_budget_and_reconnects` | Run tests (3/3) | TimeoutError: Provider stream stalled: no chunks and the request worker did not unwind after 3 stale-stream kills; reque |
| `tests/gateway/test_busy_photo_interrupt_once.py::test_send_during_a_running_turn_is_admitted_once[photo]` | Run tests (3/3) | AssertionError: timed out after 10.0s waiting for the running agent |
| `tests/gateway/test_hosted_room_file_refusal.py::test_authority_route_fails_a_turn_whose_file_exceeds_the_member_cap` | Run tests (3/3) | AssertionError: timed out after 10.0s waiting for a settled refusal or an error |
| `tests/gateway/test_managed_runtime_live_drain.py::test_managed_turn_finishes_inside_configured_restart_drain` | Run tests (1/3) | assert False |
| `tests/gateway/test_managed_worker_detached_work.py::test_managed_turn_runs_detachable_work_inline_and_leaves_no_orphan` | Run tests (2/3) | TimeoutError: timed out during opening handshake |
| `tests/gateway/test_mutation_prepare_off_loop.py::test_model_prepare_waiting_on_a_contended_writer_keeps_the_loop_live` | Run tests (1/3) | AssertionError: the owner loop stalled 0.50s behind the prepare writer |
| `tests/gateway/test_restart_ack_before_teardown.py::test_idle_restart_ack_is_delivered_before_teardown_starts` | Run tests (2/3) | TimeoutError |
| `tests/gateway/test_session_authority.py::test_native_queue_survives_owner_kill_without_replaying_unknown` | Run tests (2/3) | Failed |
| `tests/gateway/test_session_policy.py::test_ordinary_daemon_cli_launch_policy` | Run tests (3/3) | AssertionError: Traceback (most recent call last): |
| `tests/gateway/test_session_policy_workspace_readiness.py::test_managed_turn_in_removed_workspace_settles_failed_and_never_parks_the_queue` | Run tests (1/3) | KeyError: 'result' |
| `tests/gateway/test_session_tui_config.py::test_daybreak_is_a_profile_switch_read_back_and_validated` | Run tests (2/3) | TimeoutError: timed out during opening handshake |
| `tests/gateway/test_session_tui_config.py::test_display_preferences_theme_and_indicator_persist_to_the_profile` | Run tests (2/3) | TimeoutError: timed out during opening handshake |
| `tests/gateway/test_session_tui_config.py::test_personality_persists_to_the_profile_and_drives_the_next_turn` | Run tests (2/3) | TimeoutError: timed out during opening handshake |
| `tests/gateway/test_session_tui_config.py::test_skin_persists_and_repaints_every_attached_tui_of_the_profile` | Run tests (2/3) | TimeoutError: timed out during opening handshake |
| `tests/hermes_cli/test_gateway_snapshot_restore_live.py::test_snapshot_restore_rejoins_the_restored_authority[legacy-reimport-leased]` | Run tests (2/3) | hermes_cli.gateway_client.GatewayClientError: Gateway connection failed; no local fallback |
| `tests/hermes_cli/test_gateway_snapshot_restore_live.py::test_snapshot_restore_rejoins_the_restored_authority[standalone-resume-restored]` | Run tests (2/3) | hermes_cli.gateway_client.GatewayClientError: Gateway connection failed; no local fallback |
| `tests/hermes_cli/test_host_terminal_redraw.py::test_a_resize_back_waits_until_the_program_answered_the_step[python]` | Run tests (3/3) | AssertionError: assert [] == [b'RESIZE 23', b'RESIZE 24'] |
| `tests/hermes_cli/test_model_info_probe_timeout.py::test_model_info_degrades_when_the_context_probe_exceeds_its_budget` | Run tests (2/3) | assert 1.0404237829998237 < 0.9 |
| `tests/hermes_state/test_fts_optimize_progress.py::test_writer_that_opens_the_store_mid_run_gets_the_paced_cycle` | Run tests (1/3) | assert (True is False) |
| `tests/scripts/test_run_tests_shell.py::test_shell_runner_executes_tests_and_propagates_failure` | Run tests (1/3) | subprocess.TimeoutExpired: Command '['bash', '/home/runner/work/hermes-agent/hermes-agent/scripts/run_tests.sh', '-j', ' |
| `tests/tools/test_approval_pipe_shell.py::TestPipeRemoteToShellPattern::test_many_fetch_pipes_scan_in_linear_time` | Run tests (2/3) | assert 25.524664647999998 < 17.07127747999766 |
| `tests/tools/test_delegate_auto_resume_silent.py::test_a_mixed_batch_reports_each_child_once_and_no_early_failure_for_the_waiting_one` | Run tests (2/3) | AssertionError: [] |
| `tests/tools/test_timeout_transport_drain.py::test_timeout_drains_transports_so_blocked_worker_can_unwind` | Run tests (1/3) | AssertionError: worker never unwound — the drain did not settle its blocked read |
| `tests/tui_gateway/test_hosted_room_driver_runtime.py::test_pending_local_approval_is_reported_with_safe_choices` | Run tests (2/3) | assert False |
| `tests/tui_gateway/test_hosted_room_driver_runtime.py::test_post_submit_observation_failure_preserves_recoverable_outcome` | Run tests (2/3) | assert False |
| `tests/tui_gateway/test_hosted_room_driver_runtime.py::test_waiting_room_does_not_block_an_independent_room` | Run tests (2/3) | assert False |
| `tests/tui_gateway/test_hosted_room_service.py::test_active_same_thread_followup_waits_for_current_task` | Run tests (1/3) | AssertionError: condition was not reached |
| `tests/tui_gateway/test_hosted_room_service.py::test_headless_room_publishes_peer_member_reply_without_desktop_transport` | Run tests (1/3) | AssertionError: condition was not reached |
| `tests/tui_gateway/test_hosted_room_service.py::test_thread_transcript_prunes_committed_message_and_settlement_together` | Run tests (1/3) | AssertionError: condition was not reached |
| `tests/tui_gateway/test_hosted_room_service.py::test_unadmitted_peer_failure_does_not_block_next_healthy_member` | Run tests (1/3) | AssertionError: condition was not reached |
| `tests/tui_gateway/test_resume_readonly_mount.py::test_compute_host_child_turn_reopens_a_finalized_row` | Run tests (1/3) | AssertionError: timed out; saw=[{'type': 'turn.started', 'sid': 'child-sid', 'request_id': 't', 'started_ns': 2667125152 |

## JS & TS checks

The `ERR_PNPM_OUTDATED_LOCKFILE` lines in the job log are expected output from a node-deps test fixture that installs a deliberately stale lockfile. Those tests passed. The job failed on two other things, both env-only:

- `tests-js :: check`: `bundle-smoke-metadata.test.mjs > channel smoke binds the complete admitted request, not a commit-build identity or display version` timed out at its 15000 ms test timeout (18820 ms on the hosted runner). This file is unchanged by the fork.
- `apps/desktop :: check:test:ui`: all 1623 files and 13949 tests passed. Vitest failed the run on one unhandled error, `ReferenceError: window is not defined` from a notification-dismiss timer (`src/store/notifications.ts:419`) that fired after `src/app/settings/plugin-install-modal.test.tsx` tore its jsdom window down. The suite took 1905 s on the hosted runner. Neither file is changed by the fork.

## Nix flake check

Fixed on this branch: the pnpm-deps hash (commit `❄️ fix(nix): update the pnpm deps hash ...`); `pnpm deps hash` passes from that commit on. `nix flake check` also failed on a second cause, outside this repo: the patchelf patch that `nix/pm-packages.nix` fetches from `https://github.com/NixOS/patchelf/commit/59acc3d9....patch` returned HTTP 503 to the hosted runner on all four curl attempts (`cannot download patchelf-fix-executable-header-load-overlap.patch from any mirror`). The path is not on cache.nixos.org (narinfo 404), so every run fetches it. Upstream's own runs get the same 503s on that URL and succeed on a later curl retry (main push run 44670621219). The fetch is intermittent, not a repo defect: run 44677763182 at `7e9d518149` failed on it twice and passed `nix flake check` on attempt 3 with no change.

## How the (c) checks were turned green

The decision was to fix the runner sizing on this branch, and not to waive the env-only checks or
move CI to larger runners. Three options were on the table:

1. **Taken:** scale `HERMES_TEST_WORKERS` to the hosted runner and multiply the test slices to
   match, in `scripts/ci/fork_runner_sizing.py` (called from `scripts/ci/fork_hosted_runners.py`, so
   a resync re-applies it). The section below gives the details and the runs.
2. Not taken: run the fork's CI on runners the size upstream's suite expects (larger hosted runners
   or self-hosted).
3. Not taken: treat the env-only checks as advisory in the fork's merge gate.

## After runner sizing

The direction for the env-only failures was to fix the runner sizing, not the tests.
`scripts/ci/fork_hosted_runners.py` now scales `HERMES_TEST_WORKERS` by hosted/upstream vCPUs
and multiplies each job's test slices by upstream//hosted (`scripts/ci/fork_runner_sizing.py`).
The regenerated workflows run the Python suite as 24 slices of 4 workers, the macOS lanes as
8 slices on 3-vCPU `macos-latest`, the Windows x64 lane as 8 and the Windows arm64 lanes as 16,
each at upstream's workers per core.

| Run | Head | Jobs passed | Jobs failed |
|---|---|---|---|
| 44662913396 (before) | `cb7563d8bf` | 17 | 12 |
| 44680086674 (F1-F3 + (a) fixed, old sizing) | `d9dc3053c9` | 6 | 7 (3 Python slices: 72/69/81 failed tests) |
| 44690724897 (sized) | `40234757e2` | 62 | 5 |

The sized run left four failures, plus the aggregate gate:

1. **Fixed: `test_session_recovery_lost_and_found.py::test_unreadable_schema_without_cli_names_the_sqlite3_requirement`**
   (Run tests 12/24). Not a sizing failure. The test stubbed `find_sqlite3_cli`, but recovery
   reads `find_sqlite3_cli_refusal()`, which still held the refusal from the module-level probe.
   Ubuntu 24.04's apt `sqlite3` CLI carries the WAL-reset bug, so the refusal was
   `wal_reset_vulnerable` and the message took the other branch. Reproduced locally by putting a
   CLI reporting 3.45.1 on PATH; fixed by hiding `sqlite3` from `shutil.which`, which passes with
   and without the vulnerable CLI.
2. **Fixed: `test_browse_schema_drift.py::test_slow_browse_reconcile_gives_up_inside_its_budget_and_boot_waits_it_out[ddl]`**
   (macOS 2/8): `OwnershipConflict: Gateway runtime already owns profile`. Not sizing and not
   ordering: it reproduced on its own (`-k slow_browse`) at the upstream base too. Faulthandler
   dumps from the browse subprocess showed it inside `_reapply_durability_barriers` →
   `PRAGMA synchronous=FULL` for the whole 5 s boot wait. `_apply_additive_schema` re-applied the
   macOS barrier pragmas before arming the browse budget, and that pragma parses the full schema;
   on a slow connection it ran unbudgeted while `gateway.lock` was held, so the browse returned its
   503 after 9.6 s instead of inside 3 s. The budget is now armed before the pragmas and checked
   before `BEGIN IMMEDIATE`. Locally the test fails without the change and passes 10/10 with it.
3. **macOS 3/8: 4 errors in `test_plugins_cmd.py` and `test_update_missing_configured_deps.py`**:
   `Failed to fetch ... truststore-0.10.5-py3-none-any.whl ... operation timed out` during the
   tests' own `uv sync`. A PyPI download timeout from the runner (network), not a test failure.
   It passed on the next run (44695170548) with no change.
4. **Fixed: JS & TS: `bundle-smoke-metadata.test.mjs > channel smoke binds the complete admitted request`**
   timed out at 15000 ms (22.4 s). Upstream runs every JS workspace check at once on one 32-vCPU
   runner, and each check sizes its pool from the core count. The sizing rewrite now turns that job
   into 8 shards on 4-vCPU runners (`run-workspace-checks.mjs --shard i/n`), each running its
   checks one at a time with `VITEST_MAX_WORKERS=4`. The 15000 ms timeout is unchanged. The desktop
   `window is not defined` unhandled error was a timer leak: an install toast's 5 s auto-dismiss
   timer outlived `plugin-install-modal.test.tsx`. The test now dismisses its toasts in
   `afterEach`, and the full desktop `vitest --project ui` run reports 0 unhandled errors.

| Run | Head | Jobs passed | Jobs failed |
|---|---|---|---|
| 44695170548 | `a9c33209ac` | 72 | 3 (macOS 2/8 `[ddl]`; Python 18/24 `test_rich_payload_is_linear_in_unclosed_backtick_runs` 1.4 s against a 1.0 s bound, which passed on every other run; aggregate) |
| 44701204737 | `910f1b24f0` | 72 | 2 (Windows-only 7/8: `tests/ci/test_update_ci_routing.py` hit the 300 s file timeout; aggregate) |
| 44701204737 attempt 2 | `910f1b24f0` | 74 (8 skipped by path filters) | 0: CI, Nix and Docker all green |

`test_update_ci_routing.py` took 245 s and 166 s on the Windows 7/8 lane in the two earlier runs and
448 s on this Mac, both with and without the sharded `js-tests.yml`. It runs close to the 300 s cap
on a 2-worker hosted Windows runner whatever the change, so the timeout is its own run time, not this
branch's workflow edit. A rerun of the failed job passed it (job 136197037062).

## E2E lanes: sliced, not serialised

The E2E jobs run only on a release run or a manual dispatch (ci.yaml's `detect` forces their lanes
off for pull requests), so every PR run above skipped them. The first sizing pass gave those jobs
upstream's workers per core and nothing else: the Linux `e2e` job went from 3 workers on 32 vCPUs to
1 worker on 4, running all 137 of its files in series under the same 90-minute cap. That is the same
starvation the Python suite had, moved into wall time.

`scripts/ci/fork_runner_sizing.py` now also slices a job whose worker count it cuts. The job gets a
`slice: [1..upstream//hosted]` matrix, and every `scripts/run_tests.sh` step gets
`HERMES_TEST_SLICE: ${{ matrix.slice }}/N`. `run_tests_parallel.py` partitions from the checkout
alone, so all slices agree on the split, and a slice left with no files exits 0. Each of these jobs
is now 8 slices at upstream's workers per core:

| Job | Upstream | Hosted |
|---|---|---|
| `e2e` (137 files + messaging-gated cases) | 1 job, 3 workers, 32 vCPU | 8 slices, 1 worker, 4 vCPU |
| `e2e-upgrade` (per suite directory) | 6 shards, 6 workers | 6 shards × 8 slices, 1 worker |
| Windows E2E (real processes) | 1 job, 6 workers | 8 slices, 1 worker |
| Windows install + update E2E (13 journeys) | 1 job, 6 workers | 8 slices, 1 worker |

Jobs that run their tests at `nproc` (the Docker image build) or set no worker count (the venv
live probes) lost no parallelism to the smaller runner, so they stay one job. Two steps follow the
slice. The Windows journey's artifact upload gets a per-slice name. "Every crash cell ran" stays
unconditional (`test_windows_update_crash_oracles.py` refuses an `if:` on it) and passes inside
its script on every slice except the one holding `test_crash_cells.py`.
`test_the_crash_cell_journey_lands_in_the_slice_that_checks_its_manifest` pins which slice that is.

### Exercised: e2e dispatch run 44723885542 at `5f405a55ca`

The run was started by `workflow_dispatch` on the branch, which turns every E2E lane on. It had
152 jobs: 147 success, 4 failure, 1 skipped (Desktop E2E's own gate), plus the aggregate gate,
which fails on the four. Every sliced job finished well inside its cap (longest job per family, cap
in brackets): e2e 17 min [90], e2e-upgrade core 15 / git 32 / handoff 20 / hosts 5 / network 5 /
pm 9 min [180], Windows E2E 3 min [75], Windows install + update E2E 46 min [225]. All 8 Windows
install + update slices passed, including "Every crash cell ran" on slice 1/8. The four failures:

| Job | Failure | Class | Evidence |
|---|---|---|---|
| Desktop core E2E (Linux) | `reply-painted-once.spec.ts:227`, `reply-settle-paths.spec.ts:207`, `webapp-stale-journal.spec.ts:261` | (b) upstream red | Upstream's dispatch runs 43400090137, 43213118137 and 43880707009 fail the same first two specs on 32-vCPU runners. The suite is `workers: 1` upstream (`apps/desktop/e2e/core/playwright.config.ts`), so slicing does not apply to it. |
| e2e-upgrade (handoff) 1/8 | 5 of 8 in `test_handoff_from_head.py::TestFromHead` ("gateway alive but not serving <new commit>") | (b) upstream red | Upstream dispatch runs 43880707009 and 43885261886 fail the same 5 tests. |
| e2e 5/8 | `test_messaging_exactly_once.py::test_crash_between_completion_and_send[stream_accepted_unpersisted-fk_tg]`: "unexpected extra marked copies" | (c) hosted-runner shape of an upstream known gap | Upstream wraps this cell in `known_failure(STREAM_CRASH_AFTER_ACCEPT_SIGNATURE, …)`, and it XFAILs on every upstream run checked (43400090137, 43213118137, 43917262706, 43912179345, 43885261886: `61✓ 1xf`). It also XFAILs locally at this head. On the hosted runner the gap surfaced as an extra marked copy, which the signature regex does not match, so it reported red. No fork patch touches `gateway/` delivery or `tests/e2e/core/delivery`. The upstream signature is not widened here. |
| Run tests 15/24 | `test_approval_windows.py::test_repeated_launcher_words_scan_in_linear_time` (2.7 s against a 2 s bound) | (c) timing | Fork and base `tools/approval_detection.py` time the same on all four inputs (best of 3, local: 0.33 / 0.13 / 0.46 / 0.51 s for both). The same job passed on the PR run's rerun (44723825890 attempt 2, job 136263866841). |

The PR's own CI at `5f405a55ca` (44723825890 attempt 2) is 74 success / 8 skipped / 0 fail. Its
attempt 1 failed only 15/24 (the timing test above) and 18/24 (`curl` 403 fetching the ripgrep
release, before any test ran).
