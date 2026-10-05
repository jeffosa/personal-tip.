# PROJECT INTEGRITY AUDIT REPORT
Generated on: Sun Oct  4 19:54:16 GMTST 2026

## 1. STRICT TYPE INTERGITY & LYLANCE ANALYSIS
c:\Users\User\Desktop\working\prop_alpha\alerts\ledger.py
  c:\Users\User\Desktop\working\prop_alpha\alerts\ledger.py:381:21 - error: Argument of type "tuple[str, *tuple[int | None, ...]] | None" cannot be assigned to parameter "value" of type "tuple[str, int | None, int | None, int | None] | None" in function "__setitem__"
    Type "tuple[str, *tuple[int | None, ...]] | None" is not assignable to type "tuple[str, int | None, int | None, int | None] | None"
      Type "tuple[str, *tuple[int | None, ...]]" is not assignable to type "tuple[str, int | None, int | None, int | None] | None"
        "tuple[str, *tuple[int | None, ...]]" is not assignable to "tuple[str, int | None, int | None, int | None]"
          Tuple size mismatch; expected 4 but received indeterminate
        "tuple[str, *tuple[int | None, ...]]" is not assignable to "None" (reportArgumentType)
c:\Users\User\Desktop\working\prop_alpha\alerts\status.py
  c:\Users\User\Desktop\working\prop_alpha\alerts\status.py:70:79 - error: "None" is not iterable
    "__iter__" method not defined (reportGeneralTypeIssues)
c:\Users\User\Desktop\working\prop_alpha\evaluation\gate2.py
  c:\Users\User\Desktop\working\prop_alpha\evaluation\gate2.py:151:52 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "probabilities" of type "Sequence[float]" in function "clustered_skill_interval"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[float]" (reportArgumentType)
  c:\Users\User\Desktop\working\prop_alpha\evaluation\gate2.py:151:55 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "outcomes" of type "Sequence[int]" in function "clustered_skill_interval"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[int]" (reportArgumentType)
  c:\Users\User\Desktop\working\prop_alpha\evaluation\gate2.py:152:27 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "probabilities" of type "Sequence[float]" in function "calibration_fit"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[float]" (reportArgumentType)
  c:\Users\User\Desktop\working\prop_alpha\evaluation\gate2.py:152:30 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "outcomes" of type "Sequence[int]" in function "calibration_fit"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[int]" (reportArgumentType)
  c:\Users\User\Desktop\working\prop_alpha\evaluation\gate2.py:157:37 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "probabilities" of type "Sequence[float]" in function "worst_bin_gap"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[float]" (reportArgumentType)
  c:\Users\User\Desktop\working\prop_alpha\evaluation\gate2.py:157:40 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "outcomes" of type "Sequence[int]" in function "worst_bin_gap"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[int]" (reportArgumentType)
c:\Users\User\Desktop\working\prop_alpha\ingestion\results.py
  c:\Users\User\Desktop\working\prop_alpha\ingestion\results.py:105:26 - error: Object of type "None" is not subscriptable (reportOptionalSubscript)
c:\Users\User\Desktop\working\prop_alpha\modeling\engine.py
  c:\Users\User\Desktop\working\prop_alpha\modeling\engine.py:325:16 - error: Type "rv_continuous_frozen | rv_discrete_frozen" is not assignable to return type "DiscreteDistribution"
    Type "rv_continuous_frozen | rv_discrete_frozen" is not assignable to type "DiscreteDistribution"
      "rv_continuous_frozen" is incompatible with protocol "DiscreteDistribution"
        "pmf" is not present
          "cdf" is an incompatible type
            Type "(x: Unknown) -> Unknown" is not assignable to type "(values: ArrayLike) -> (float | ndarray[_AnyShape, dtype[Any]])"
          "sf" is an incompatible type
            Type "(x: Unknown) -> Unknown" is not assignable to type "(values: ArrayLike) -> (float | ndarray[_AnyShape, dtype[Any]])" (reportReturnType)
  c:\Users\User\Desktop\working\prop_alpha\modeling\engine.py:419:52 - error: Argument of type "rv_continuous_frozen | rv_discrete_frozen" cannot be assigned to parameter "distribution" of type "DiscreteDistribution" in function "_line_masses"
    Type "rv_continuous_frozen | rv_discrete_frozen" is not assignable to type "DiscreteDistribution"
      "rv_continuous_frozen" is incompatible with protocol "DiscreteDistribution"
        "pmf" is not present
          "cdf" is an incompatible type
            Type "(x: Unknown) -> Unknown" is not assignable to type "(values: ArrayLike) -> (float | ndarray[_AnyShape, dtype[Any]])"
          "sf" is an incompatible type
            Type "(x: Unknown) -> Unknown" is not assignable to type "(values: ArrayLike) -> (float | ndarray[_AnyShape, dtype[Any]])" (reportArgumentType)
c:\Users\User\Desktop\working\scripts\gate1_soak.py
  c:\Users\User\Desktop\working\scripts\gate1_soak.py:175:32 - error: Object of type "None" is not subscriptable (reportOptionalSubscript)
  c:\Users\User\Desktop\working\scripts\gate1_soak.py:222:25 - error: Object of type "None" is not subscriptable (reportOptionalSubscript)
  c:\Users\User\Desktop\working\scripts\gate1_soak.py:224:27 - error: Object of type "None" is not subscriptable (reportOptionalSubscript)
  c:\Users\User\Desktop\working\scripts\gate1_soak.py:226:23 - error: Object of type "None" is not subscriptable (reportOptionalSubscript)
  c:\Users\User\Desktop\working\scripts\gate1_soak.py:228:30 - error: Object of type "None" is not subscriptable (reportOptionalSubscript)
c:\Users\User\Desktop\working\scripts\quick_backtest.py
  c:\Users\User\Desktop\working\scripts\quick_backtest.py:198:58 - error: "splitlines" is not a known attribute of "None" (reportOptionalMemberAccess)
c:\Users\User\Desktop\working\tests\test_integrity.py
  c:\Users\User\Desktop\working\tests\test_integrity.py:257:13 - error: "__getitem__" method not defined on type "object" (reportIndexIssue)
  c:\Users\User\Desktop\working\tests\test_integrity.py:261:16 - error: "__getitem__" method not defined on type "object" (reportIndexIssue)
c:\Users\User\Desktop\working\tests\test_model_freeze.py
  c:\Users\User\Desktop\working\tests\test_model_freeze.py:60:39 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "probabilities" of type "Sequence[float]" in function "score_line"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[float]" (reportArgumentType)
  c:\Users\User\Desktop\working\tests\test_model_freeze.py:60:42 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "outcomes" of type "Sequence[int]" in function "score_line"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[int]" (reportArgumentType)
  c:\Users\User\Desktop\working\tests\test_model_freeze.py:64:38 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "probabilities" of type "Sequence[float]" in function "score_line"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[float]" (reportArgumentType)
  c:\Users\User\Desktop\working\tests\test_model_freeze.py:64:41 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "outcomes" of type "Sequence[int]" in function "score_line"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[int]" (reportArgumentType)
  c:\Users\User\Desktop\working\tests\test_model_freeze.py:70:40 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "probabilities" of type "Sequence[float]" in function "score_line"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[float]" (reportArgumentType)
  c:\Users\User\Desktop\working\tests\test_model_freeze.py:70:43 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "outcomes" of type "Sequence[int]" in function "score_line"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[int]" (reportArgumentType)
  c:\Users\User\Desktop\working\tests\test_model_freeze.py:76:58 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "probabilities" of type "Sequence[float]" in function "clustered_skill_interval"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[float]" (reportArgumentType)
  c:\Users\User\Desktop\working\tests\test_model_freeze.py:76:61 - error: Argument of type "ndarray[_AnyShape, dtype[Any]]" cannot be assigned to parameter "outcomes" of type "Sequence[int]" in function "clustered_skill_interval"
    "ndarray[_AnyShape, dtype[Any]]" is not assignable to "Sequence[int]" (reportArgumentType)
c:\Users\User\Desktop\working\tests\test_oddspapi.py
  c:\Users\User\Desktop\working\tests\test_oddspapi.py:227:97 - error: Object of type "None" is not subscriptable (reportOptionalSubscript)
  c:\Users\User\Desktop\working\tests\test_oddspapi.py:237:94 - error: Object of type "None" is not subscriptable (reportOptionalSubscript)
c:\Users\User\Desktop\working\tests\test_pipeline_concurrency.py
  c:\Users\User\Desktop\working\tests\test_pipeline_concurrency.py:234:27 - error: Cannot assign to attribute "before_fetch" for class "FakeVendor"
    Type "(uid: str) -> int" is not assignable to type "((str) -> None) | None"
      Type "(uid: str) -> int" is not assignable to type "(str) -> None"
        Function return type "int" is incompatible with type "None"
          "int" is not assignable to "None"
      "FunctionType" is not assignable to "None" (reportAttributeAccessIssue)
  c:\Users\User\Desktop\working\tests\test_pipeline_concurrency.py:308:27 - error: Cannot assign to attribute "before_fetch" for class "FakeVendor"
    Type "(uid: str) -> (bool | None)" is not assignable to type "((str) -> None) | None"
      Type "(uid: str) -> (bool | None)" is not assignable to type "(str) -> None"
        Function return type "bool | None" is incompatible with type "None"
          Type "bool | None" is not assignable to type "None"
            "bool" is not assignable to "None"
      "FunctionType" is not assignable to "None" (reportAttributeAccessIssue)
  c:\Users\User\Desktop\working\tests\test_pipeline_concurrency.py:546:71 - error: Cannot access attribute "max_retries" for class "BaseAdapter"
    Attribute "max_retries" is unknown (reportAttributeAccessIssue)
c:\Users\User\Desktop\working\tests\test_scan.py
  c:\Users\User\Desktop\working\tests\test_scan.py:167:9 - error: Method "send_message" overrides class "TelegramNotifier" in an incompatible manner
    Positional parameter count mismatch; base method has 3, but override has 2
    Parameter 3 mismatch: base parameter "reply_to" is keyword parameter, override parameter is position-only (reportIncompatibleMethodOverride)
33 errors, 0 warnings, 0 informations

## 2. SECURITY VULNERABILITY & SECRET LEAK AUDIT
[main]	INFO	profile include tests: None
[main]	INFO	profile exclude tests: None
[main]	INFO	cli include tests: None
[main]	INFO	cli exclude tests: None
[main]	INFO	running on Python 3.14.6
Run started:2026-10-04 18:55:24.592524+00:00

Test results:
>> Issue: [B608:hardcoded_sql_expressions] Possible SQL injection vector through string-based query construction.
   Severity: Medium   Confidence: Low
   CWE: CWE-89 (https://cwe.mitre.org/data/definitions/89.html)
   More Info: https://bandit.readthedocs.io/en/1.9.4/plugins/b608_hardcoded_sql_expressions.html
   Location: prop_alpha/ingestion\pipeline.py:86:6
85	INSERT_STAGING_SQL: Final[str] = (
86	    f"INSERT INTO prop_alpha.staging_player_feed ({', '.join(STAGING_COLUMNS)}) VALUES %s"
87	)

--------------------------------------------------
>> Issue: [B311:blacklist] Standard pseudo-random generators are not suitable for security/cryptographic purposes.
   Severity: Low   Confidence: High
   CWE: CWE-330 (https://cwe.mitre.org/data/definitions/330.html)
   More Info: https://bandit.readthedocs.io/en/1.9.4/blacklists/blacklist_calls.html#b311-random
   Location: prop_alpha/ingestion\pipeline.py:586:83
585	            if attempt < self.max_db_attempts:
586	                time.sleep(self.retry_base_delay_s * (2 ** (attempt - 1)) * (1.0 + random.random()))
587	        if last_error is None:

--------------------------------------------------

Code scanned:
	Total lines of code: 6275
	Total lines skipped (#nosec): 0
	Total potential issues skipped due to specifically being disabled (e.g., #nosec BXXX): 0

Run metrics:
	Total issues (by severity):
		Undefined: 0
		Low: 1
		Medium: 1
		High: 0
	Total issues (by confidence):
		Undefined: 0
		Low: 1
		Medium: 0
		High: 1
Files skipped (0):

## 3. CORE INTEGRITY UNIT TEST REGRESSION STATUS
============================= test session starts =============================
platform win32 -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- C:\Users\User\Desktop\working\.venv\Scripts\python.exe
hypothesis profile 'default'
rootdir: C:\Users\User\Desktop\working
plugins: hypothesis-6.168.3, cov-7.1.0
collecting ... collected 546 items

tests/test_agent_contract.py::test_only_sanctioned_exception_handlers_exist PASSED [  0%]
tests/test_agent_contract.py::test_every_handler_records_or_reraises PASSED [  0%]
tests/test_agent_contract.py::test_statement_timeout_is_caught_before_retryable_errors PASSED [  0%]
tests/test_agent_contract.py::test_cli_exit_code_signals_an_incomplete_slate PASSED [  0%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/ingestion/transform.py] PASSED [  0%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/modeling/engine.py] PASSED [  1%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/scanner/market_scanner.py] PASSED [  1%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/calibration/fitting.py] PASSED [  1%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/evaluation/backtest.py] PASSED [  1%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/scanner/value_screen.py] PASSED [  1%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/ingestion/sportmonks.py] PASSED [  2%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/ingestion/oddspapi.py] PASSED [  2%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/scanner/slate.py] PASSED [  2%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/alerts/replies.py] PASSED [  2%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/modeling/league_level.py] PASSED [  2%]
tests/test_agent_contract.py::test_pure_modules_import_no_io_libraries[prop_alpha/evaluation/gate2.py] PASSED [  2%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/ingestion/transform.py] PASSED [  3%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/modeling/engine.py] PASSED [  3%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/scanner/market_scanner.py] PASSED [  3%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/calibration/fitting.py] PASSED [  3%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/evaluation/backtest.py] PASSED [  3%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/scanner/value_screen.py] PASSED [  4%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/ingestion/sportmonks.py] PASSED [  4%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/ingestion/oddspapi.py] PASSED [  4%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/scanner/slate.py] PASSED [  4%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/alerts/replies.py] PASSED [  4%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/modeling/league_level.py] PASSED [  4%]
tests/test_agent_contract.py::test_pure_functions_accept_no_database_or_session_handles[prop_alpha/evaluation/gate2.py] PASSED [  5%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/ingestion/transform.py] PASSED [  5%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/modeling/engine.py] PASSED [  5%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/scanner/market_scanner.py] PASSED [  5%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/calibration/fitting.py] PASSED [  5%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/evaluation/backtest.py] PASSED [  6%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/scanner/value_screen.py] PASSED [  6%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/ingestion/sportmonks.py] PASSED [  6%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/ingestion/oddspapi.py] PASSED [  6%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/scanner/slate.py] PASSED [  6%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/alerts/replies.py] PASSED [  6%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/modeling/league_level.py] PASSED [  7%]
tests/test_agent_contract.py::test_pure_modules_read_no_clock_environment_or_random_state[prop_alpha/evaluation/gate2.py] PASSED [  7%]
tests/test_agent_contract.py::test_with_transaction_is_the_single_connection_acquisition_site PASSED [  7%]
tests/test_agent_contract.py::test_fetch_and_parse_complete_before_the_transaction[_stats_task-get_fixture_feed-parsers0] PASSED [  7%]
tests/test_agent_contract.py::test_fetch_and_parse_complete_before_the_transaction[_lineup_task-get_lineups-parsers1] PASSED [  7%]
tests/test_agent_contract.py::test_referee_job_defers_its_database_driver_import PASSED [  8%]
tests/test_agent_contract.py::test_no_placeholders_or_truncation_markers PASSED [  8%]
tests/test_agent_contract.py::test_engine_contains_no_raw_moment_subtraction PASSED [  8%]
tests/test_agent_contract.py::test_return_moments_documents_its_condition_number_bound PASSED [  8%]
tests/test_agent_contract.py::test_risk_defaults_and_numerical_bounds_are_unchanged PASSED [  8%]
tests/test_agent_contract.py::test_no_secrets_in_repository_files PASSED [  8%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_handler_violations-try:\n    x = 1\nexcept ValueError:\n    pass\n] PASSED [  9%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_handler_violations-try:\n    x = 1\nexcept:\n    raise\n] PASSED [  9%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_io_imports-import requests\n] PASSED [  9%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_handle_parameters-def parse(payload: dict, cur: object) -> None:\n    return None\n] PASSED [  9%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_nondeterministic_calls-from datetime import datetime\nstamp = datetime.now()\n] PASSED [  9%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_raw_moment_subtractions-variance = second_moment - mean**2\n] PASSED [ 10%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_raw_moment_subtractions-cov = second - np.outer(mu, mu)\n] PASSED [ 10%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_python_placeholders-def f() -> None:\n    pass\n] PASSED [ 10%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_text_placeholders--- TODO: add the reject predicate\n] PASSED [ 10%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_secret_hits-TELEGRAM_BOT_TOKEN=1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n] PASSED [ 10%]
tests/test_agent_contract.py::test_contract_detectors_flag_deliberate_violations[_secret_hits-dsn = 'postgresql://admin:hunter2@db.internal/prod'\n] PASSED [ 10%]
tests/test_alerts.py::test_telegram_rate_limit_is_waited_out_once PASSED [ 11%]
tests/test_alerts.py::test_messages_are_spaced_to_telegram_per_chat_limit PASSED [ 11%]
tests/test_alerts.py::test_malformed_env_line_does_not_block_alerts_configured_in_the_environment PASSED [ 11%]
tests/test_alerts.py::test_env_file_parsing_handles_comments_quotes_and_export PASSED [ 11%]
tests/test_alerts.py::test_config_prefers_environment_and_hides_token PASSED [ 11%]
tests/test_alerts.py::test_config_rejects_missing_or_malformed_credentials PASSED [ 12%]
tests/test_alerts.py::test_alert_text_carries_the_paper_label_and_the_screen_evidence PASSED [ 12%]
tests/test_alerts.py::test_check_connection_and_send_message_call_the_bot_api PASSED [ 12%]
tests/test_alerts.py::test_notify_sends_only_high_value_bets_and_each_once PASSED [ 12%]
tests/test_alerts.py::test_a_new_run_does_not_resend_and_sizes_net_of_earlier_alerts PASSED [ 12%]
tests/test_alerts.py::test_alert_is_recorded_before_sending_so_a_failed_send_is_not_repeated PASSED [ 13%]
tests/test_alerts.py::test_corrupt_alert_state_fails_closed PASSED       [ 13%]
tests/test_alerts.py::test_alert_state_is_gitignored PASSED              [ 13%]
tests/test_alerts.py::test_reference_bets_are_screened_out PASSED        [ 13%]
tests/test_alerts.py::test_stricter_criteria_and_sub_minimum_stakes_send_nothing PASSED [ 13%]
tests/test_alerts.py::test_directive_without_projection_is_logged_and_skipped PASSED [ 13%]
tests/test_alerts.py::test_a_price_tick_does_not_resend_the_same_market PASSED [ 14%]
tests/test_alerts.py::test_alert_text_shows_the_price_floor_and_expiry PASSED [ 14%]
tests/test_alerts.py::test_a_stake_capped_by_an_assumed_limit_says_so_in_pounds PASSED [ 14%]
tests/test_alerts.py::test_quote_limit_flag_must_be_a_bool PASSED        [ 14%]
tests/test_alerts.py::test_telegram_rejection_raises_without_the_token PASSED [ 14%]
tests/test_alerts.py::test_transport_errors_are_sanitised PASSED         [ 15%]
tests/test_alerts.py::test_sending_without_a_chat_id_is_refused PASSED   [ 15%]
tests/test_alerts.py::test_recent_chats_lists_senders PASSED             [ 15%]
tests/test_alerts.py::test_env_file_is_gitignored PASSED                 [ 15%]
tests/test_alerts.py::test_digest_sends_at_most_five_bets_per_message[3-expected_messages0] PASSED [ 15%]
tests/test_alerts.py::test_digest_sends_at_most_five_bets_per_message[7-expected_messages1] PASSED [ 15%]
tests/test_alerts.py::test_digest_sends_at_most_five_bets_per_message[10-expected_messages2] PASSED [ 16%]
tests/test_alerts.py::test_digest_rejects_an_empty_page_size PASSED      [ 16%]
tests/test_alerts.py::test_env_template_holds_no_credentials PASSED      [ 16%]
tests/test_core.py::test_predictive_gamma_exact_without_game_state PASSED [ 16%]
tests/test_core.py::test_predictive_is_negative_binomial_with_posterior_mean PASSED [ 16%]
tests/test_core.py::test_game_state_mixing_widens_but_preserves_mean PASSED [ 17%]
tests/test_core.py::test_predictive_gamma_stable_where_subtraction_form_cancels PASSED [ 17%]
tests/test_core.py::test_predictive_gamma_raises_under_extreme_overdispersion PASSED [ 17%]
tests/test_core.py::test_tiny_league_rate_raises_instead_of_pricing_a_needle PASSED [ 17%]
tests/test_core.py::test_math_clamp_is_logged PASSED                     [ 17%]
tests/test_core.py::test_no_math_clamp_for_ordinary_inputs PASSED        [ 17%]
tests/test_core.py::test_game_state_k_is_mandatory PASSED                [ 18%]
tests/test_core.py::test_relative_variance_without_multiplier_matches_the_additive_form PASSED [ 18%]
tests/test_core.py::test_multiplier_variance_widens_the_predictive_distribution PASSED [ 18%]
tests/test_core.py::test_card_projection_takes_the_multiplier_variance PASSED [ 18%]
tests/test_core.py::test_minutes_mixture_prices_the_mix_not_its_mean PASSED [ 18%]
tests/test_core.py::test_minutes_mixture_keeps_ev_and_covariance_consistent PASSED [ 19%]
tests/test_core.py::test_minutes_mixture_rejects_invalid_input[minutes0-weights0] PASSED [ 19%]
tests/test_core.py::test_minutes_mixture_rejects_invalid_input[minutes1-weights1] PASSED [ 19%]
tests/test_core.py::test_minutes_mixture_rejects_invalid_input[minutes2-weights2] PASSED [ 19%]
tests/test_core.py::test_minutes_mixture_rejects_invalid_input[minutes3-weights3] PASSED [ 19%]
tests/test_core.py::test_minutes_mixture_rejects_invalid_input[minutes4-weights4] PASSED [ 19%]
tests/test_core.py::test_lineup_mixture_mean_must_match_expected_minutes PASSED [ 20%]
tests/test_core.py::test_side_probabilities_partition[0.0-void_if_no_appearance] PASSED [ 20%]
tests/test_core.py::test_side_probabilities_partition[0.0-void_if_not_starting] PASSED [ 20%]
tests/test_core.py::test_side_probabilities_partition[0.0-no_void] PASSED [ 20%]
tests/test_core.py::test_side_probabilities_partition[0.5-void_if_no_appearance] PASSED [ 20%]
tests/test_core.py::test_side_probabilities_partition[0.5-void_if_not_starting] PASSED [ 21%]
tests/test_core.py::test_side_probabilities_partition[0.5-no_void] PASSED [ 21%]
tests/test_core.py::test_side_probabilities_partition[1.0-void_if_no_appearance] PASSED [ 21%]
tests/test_core.py::test_side_probabilities_partition[1.0-void_if_not_starting] PASSED [ 21%]
tests/test_core.py::test_side_probabilities_partition[1.0-no_void] PASSED [ 21%]
tests/test_core.py::test_side_probabilities_partition[1.5-void_if_no_appearance] PASSED [ 21%]
tests/test_core.py::test_side_probabilities_partition[1.5-void_if_not_starting] PASSED [ 22%]
tests/test_core.py::test_side_probabilities_partition[1.5-no_void] PASSED [ 22%]
tests/test_core.py::test_side_probabilities_partition[2.0-void_if_no_appearance] PASSED [ 22%]
tests/test_core.py::test_side_probabilities_partition[2.0-void_if_not_starting] PASSED [ 22%]
tests/test_core.py::test_side_probabilities_partition[2.0-no_void] PASSED [ 22%]
tests/test_core.py::test_side_probabilities_partition[2.5-void_if_no_appearance] PASSED [ 23%]
tests/test_core.py::test_side_probabilities_partition[2.5-void_if_not_starting] PASSED [ 23%]
tests/test_core.py::test_side_probabilities_partition[2.5-no_void] PASSED [ 23%]
tests/test_core.py::test_complementary_lineup_leaves_no_negative_absence_weight[0.33-0.67] PASSED [ 23%]
tests/test_core.py::test_complementary_lineup_leaves_no_negative_absence_weight[0.7-0.3] PASSED [ 23%]
tests/test_core.py::test_complementary_lineup_leaves_no_negative_absence_weight[0.1-0.9] PASSED [ 23%]
tests/test_core.py::test_complementary_lineup_leaves_no_negative_absence_weight[0.5-0.5] PASSED [ 24%]
tests/test_core.py::test_side_probabilities_never_fail_on_rounding PASSED [ 24%]
tests/test_core.py::test_bimodal_substitution_has_no_absence_void_when_present PASSED [ 24%]
tests/test_core.py::test_invalid_projection_parameters_fail_closed[0.0-1.0] PASSED [ 24%]
tests/test_core.py::test_invalid_projection_parameters_fail_closed[1.0-0.0] PASSED [ 24%]
tests/test_core.py::test_invalid_projection_parameters_fail_closed[nan-1.0] PASSED [ 25%]
tests/test_core.py::test_invalid_projection_parameters_fail_closed[1.0-inf] PASSED [ 25%]
tests/test_core.py::test_invalid_minutes_fail_closed PASSED              [ 25%]
tests/test_core.py::test_referee_profile_rejects_non_finite_coefficients PASSED [ 25%]
tests/test_core.py::test_card_probability_not_distorted_by_variance_floor PASSED [ 25%]
tests/test_core.py::test_mean_variance_prefers_low_variance_line_at_similar_ev PASSED [ 26%]
tests/test_core.py::test_return_moments_match_closed_form[void_if_no_appearance-1.0] PASSED [ 26%]
tests/test_core.py::test_return_moments_match_closed_form[void_if_no_appearance-1.5] PASSED [ 26%]
tests/test_core.py::test_return_moments_match_closed_form[void_if_not_starting-1.0] PASSED [ 26%]
tests/test_core.py::test_return_moments_match_closed_form[void_if_not_starting-1.5] PASSED [ 26%]
tests/test_core.py::test_return_moments_match_closed_form[no_void-1.0] PASSED [ 26%]
tests/test_core.py::test_return_moments_match_closed_form[no_void-1.5] PASSED [ 27%]
tests/test_core.py::test_cross_line_covariance_matches_brute_force PASSED [ 27%]
tests/test_core.py::test_single_candidate_matches_closed_form_optimum PASSED [ 27%]
tests/test_core.py::test_higher_risk_aversion_reduces_exposure PASSED    [ 27%]
tests/test_core.py::test_fixture_cap_scales_correlated_positions PASSED  [ 27%]
tests/test_core.py::test_certain_outcome_is_flagged_not_staked PASSED    [ 28%]
tests/test_core.py::test_best_price_kept_and_stale_quotes_dropped PASSED [ 28%]
tests/test_core.py::test_sub_minimum_stakes_recorded_in_skipped_directives PASSED [ 28%]
tests/test_core.py::test_devig_sums_to_one PASSED                        [ 28%]
tests/test_core.py::test_allocate_requires_explicit_utc_time_and_exposure PASSED [ 28%]
tests/test_core.py::test_exposure_book_stops_restaking_a_persistent_edge PASSED [ 28%]
tests/test_core.py::test_exposure_book_caps_each_market_net_of_its_open_position PASSED [ 29%]
tests/test_core.py::test_exposure_book_rejects_invalid_amounts PASSED    [ 29%]
tests/test_core.py::test_exposure_book_rejects_malformed_keys PASSED     [ 29%]
tests/test_core.py::test_exposure_book_is_isolated_from_later_mutation_of_its_input PASSED [ 29%]
tests/test_core.py::test_allocate_rejects_projection_filed_under_another_key PASSED [ 29%]
tests/test_core.py::test_loss_floor_removes_the_near_certain_under PASSED [ 30%]
tests/test_core.py::test_stressed_probabilities_move_win_mass_to_loss_only PASSED [ 30%]
tests/test_core.py::test_shared_game_state_covariance_matches_monte_carlo PASSED [ 30%]
tests/test_core.py::test_game_state_quadrature_is_exact_for_the_first_moments_and_large_k PASSED [ 30%]
tests/test_core.py::test_corrupt_factor_projection_is_skipped_never_traded PASSED [ 30%]
tests/test_core.py::test_void_rule_must_be_stated_on_every_quote PASSED  [ 30%]
tests/test_core.py::test_future_dated_quote_is_skipped_and_counted PASSED [ 31%]
tests/test_core.py::test_small_limit_book_no_longer_hides_a_deep_book PASSED [ 31%]
tests/test_core.py::test_book_limit_is_in_the_optimiser_not_clipped_afterwards PASSED [ 31%]
tests/test_core.py::test_directive_carries_price_floor_and_expiry PASSED [ 31%]
tests/test_core.py::test_bivariate_marginal_is_equidispersed_poisson PASSED [ 31%]
tests/test_core.py::test_joint_over_probability_matches_monte_carlo PASSED [ 32%]
tests/test_core.py::test_projection_error_raised_for_non_finite_multiplier PASSED [ 32%]
tests/test_core.py::test_parse_valid_payload PASSED                      [ 32%]
tests/test_core.py::test_parse_rejects_impossible_stats[path0-9] PASSED  [ 32%]
tests/test_core.py::test_parse_rejects_impossible_stats[path1-20] PASSED [ 32%]
tests/test_core.py::test_parse_rejects_impossible_stats[path2-3] PASSED  [ 32%]
tests/test_core.py::test_parse_rejects_impossible_stats[path3-2] PASSED  [ 33%]
tests/test_core.py::test_parse_rejects_impossible_stats[path4--1] PASSED [ 33%]
tests/test_core.py::test_parse_rejects_impossible_stats[path5-T-OTHER] PASSED [ 33%]
tests/test_core.py::test_parse_rejects_malformed_fixtures PASSED         [ 33%]
tests/test_core.py::test_default_kappas_are_immutable PASSED             [ 33%]
tests/test_core.py::test_padj_scale_is_finite_at_extreme_possession PASSED [ 34%]
tests/test_core.py::test_dribbled_past_rate_undefined_without_duels PASSED [ 34%]
tests/test_core.py::test_match_state_minutes_partition_time_on_pitch PASSED [ 34%]
tests/test_core.py::test_lineup_payload_assigns_start_and_bench_probabilities PASSED [ 34%]
tests/test_core.py::test_lineup_payload_rejects_incomplete_or_duplicated_sheets PASSED [ 34%]
tests/test_core.py::test_poisson_glm_recovers_coefficients_with_standard_errors PASSED [ 34%]
tests/test_core.py::test_poisson_glm_rejects_rank_deficient_design PASSED [ 35%]
tests/test_core.py::test_possession_kappa_recovered PASSED               [ 35%]
tests/test_core.py::test_referee_betas_recovered PASSED                  [ 35%]
tests/test_core.py::test_game_state_dispersion_recovers_k PASSED         [ 35%]
tests/test_core.py::test_game_state_dispersion_raises_instead_of_returning_poisson PASSED [ 35%]
tests/test_core.py::test_bench_sub_probability_shrinks_towards_the_league_mean PASSED [ 36%]
tests/test_core.py::test_state_duel_density_exact_on_noiseless_data PASSED [ 36%]
tests/test_core.py::test_referee_coefficients_shrink_toward_league PASSED [ 36%]
tests/test_core.py::test_referee_coefficients_reject_bad_input PASSED    [ 36%]
tests/test_core.py::test_scoring_rules_on_known_values PASSED            [ 36%]
tests/test_core.py::test_calibration_table_bins_predictions PASSED       [ 36%]
tests/test_core.py::test_closing_line_value_uses_devigged_close PASSED   [ 37%]
tests/test_core.py::test_walk_forward_uses_only_strictly_prior_observations PASSED [ 37%]
tests/test_core.py::test_walk_forward_accepts_bench_booking_and_keeps_it_out_of_the_rate PASSED [ 37%]
tests/test_core.py::test_one_unpriceable_player_is_recorded_not_fatal_when_failures_are_collected PASSED [ 37%]
tests/test_core.py::test_summarize_reports_brier_skill_against_climatology PASSED [ 37%]
tests/test_fx.py::test_bankroll_currency_is_required_and_must_be_supported PASSED [ 38%]
tests/test_fx.py::test_rates_are_validated PASSED                        [ 38%]
tests/test_fx.py::test_parse_rate_reads_the_frankfurter_reply_and_refuses_anything_else PASSED [ 38%]
tests/test_fx.py::test_fetch_rate_refuses_an_error_reply PASSED          [ 38%]
tests/test_fx.py::test_cached_rate_must_exist_match_and_be_fresh PASSED  [ 38%]
tests/test_fx.py::test_alerts_show_the_stake_in_both_currencies_and_the_assumed_limit_converted PASSED [ 39%]
tests/test_fx.py::test_a_future_dated_rate_is_refused_not_fresh PASSED   [ 39%]
tests/test_integrity.py::test_valid_vectors_are_returned_typed PASSED    [ 39%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes0-counts0] PASSED [ 39%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes1-counts1] PASSED [ 39%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes2-counts2] PASSED [ 39%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes3-counts3] PASSED [ 40%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes4-counts4] PASSED [ 40%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes5-counts5] PASSED [ 40%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes6-counts6] PASSED [ 40%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes7-counts7] PASSED [ 40%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes8-counts8] PASSED [ 41%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes9-counts9] PASSED [ 41%]
tests/test_integrity.py::test_polluted_vectors_are_rejected[minutes10-counts10] PASSED [ 41%]
tests/test_integrity.py::test_gamma_update_uses_validation_and_allows_prior_only_history PASSED [ 41%]
tests/test_integrity.py::test_zero_minute_vector_contributes_nothing_to_beta PASSED [ 41%]
tests/test_integrity.py::test_transform_rejects_minute_off_before_minute_on PASSED [ 41%]
tests/test_integrity.py::test_transform_rejects_minutes_above_match_maximum PASSED [ 42%]
tests/test_integrity.py::test_transform_accepts_extra_time_at_the_bound PASSED [ 42%]
tests/test_integrity.py::test_transform_rejects_possession_outside_bounds[101.0] PASSED [ 42%]
tests/test_integrity.py::test_transform_rejects_possession_outside_bounds[-0.5] PASSED [ 42%]
tests/test_integrity.py::test_transform_admits_possession_at_bounds[0.0] PASSED [ 42%]
tests/test_integrity.py::test_transform_admits_possession_at_bounds[100.0] PASSED [ 43%]
tests/test_integrity.py::test_renamed_market_metric_fails_the_fixture PASSED [ 43%]
tests/test_integrity.py::test_every_market_metric_is_required_on_the_pitch[tackles_won] PASSED [ 43%]
tests/test_integrity.py::test_every_market_metric_is_required_on_the_pitch[fouls_committed] PASSED [ 43%]
tests/test_integrity.py::test_every_market_metric_is_required_on_the_pitch[fouls_drawn] PASSED [ 43%]
tests/test_integrity.py::test_every_market_metric_is_required_on_the_pitch[yellow_cards] PASSED [ 43%]
tests/test_integrity.py::test_every_market_metric_is_required_on_the_pitch[red_cards] PASSED [ 44%]
tests/test_integrity.py::test_every_market_metric_is_required_on_the_pitch[tackles_attempted] PASSED [ 44%]
tests/test_integrity.py::test_every_market_metric_is_required_on_the_pitch[minutes] PASSED [ 44%]
tests/test_integrity.py::test_started_must_be_a_json_boolean[false] PASSED [ 44%]
tests/test_integrity.py::test_started_must_be_a_json_boolean[true] PASSED [ 44%]
tests/test_integrity.py::test_started_must_be_a_json_boolean[0] PASSED   [ 45%]
tests/test_integrity.py::test_started_must_be_a_json_boolean[1] PASSED   [ 45%]
tests/test_integrity.py::test_started_must_be_a_json_boolean[None] PASSED [ 45%]
tests/test_integrity.py::test_market_metric_must_be_an_integer[2] PASSED [ 45%]
tests/test_integrity.py::test_market_metric_must_be_an_integer[2.5] PASSED [ 45%]
tests/test_integrity.py::test_market_metric_must_be_an_integer[True] PASSED [ 45%]
tests/test_integrity.py::test_market_metric_must_be_an_integer[None] PASSED [ 46%]
tests/test_integrity.py::test_missing_stats_block_fails_and_unused_substitute_may_omit_metrics PASSED [ 46%]
tests/test_integrity.py::test_lineup_parse_validates_per_player_bench_probabilities PASSED [ 46%]
tests/test_integrity.py::test_staging_rows_have_header_and_full_width PASSED [ 46%]
tests/test_integrity.py::test_stats_row_matches_stats_columns PASSED     [ 46%]
tests/test_integrity.py::test_staging_player_columns_mirror_stats_columns PASSED [ 47%]
tests/test_integrity.py::test_referee_job_takes_merge_lock_before_reading_and_updates_in_id_order PASSED [ 47%]
tests/test_integrity.py::test_merge_rejects_every_row_a_core_constraint_would_refuse[schema.sql] PASSED [ 47%]
tests/test_integrity.py::test_merge_rejects_every_row_a_core_constraint_would_refuse[003] PASSED [ 47%]
tests/test_integrity.py::test_merge_rejects_every_row_a_core_constraint_would_refuse[004] PASSED [ 47%]
tests/test_integrity.py::test_merge_rejects_every_row_a_core_constraint_would_refuse[005] PASSED [ 47%]
tests/test_integrity.py::test_merge_rejects_every_row_a_core_constraint_would_refuse[006] PASSED [ 48%]
tests/test_integrity.py::test_merge_rejects_every_row_a_core_constraint_would_refuse[007] PASSED [ 48%]
tests/test_integrity.py::test_merge_rejects_null_market_metrics[schema.sql] PASSED [ 48%]
tests/test_integrity.py::test_merge_rejects_null_market_metrics[004] PASSED [ 48%]
tests/test_integrity.py::test_merge_rejects_null_market_metrics[005] PASSED [ 48%]
tests/test_integrity.py::test_merge_rejects_null_market_metrics[006] PASSED [ 49%]
tests/test_integrity.py::test_merge_rejects_null_market_metrics[007] PASSED [ 49%]
tests/test_integrity.py::test_merge_function_identical_in_schema_and_latest_migration PASSED [ 49%]
tests/test_integrity.py::test_merge_moves_player_to_team_of_latest_fixture[schema.sql] PASSED [ 49%]
tests/test_integrity.py::test_merge_moves_player_to_team_of_latest_fixture[005] PASSED [ 49%]
tests/test_integrity.py::test_merge_moves_player_to_team_of_latest_fixture[006] PASSED [ 50%]
tests/test_integrity.py::test_merge_moves_player_to_team_of_latest_fixture[007] PASSED [ 50%]
tests/test_integrity.py::test_migration_replaces_the_zero_argument_function_in_one_transaction[004] PASSED [ 50%]
tests/test_integrity.py::test_migration_replaces_the_zero_argument_function_in_one_transaction[005] PASSED [ 50%]
tests/test_integrity.py::test_superseded_002_merge_lacks_minute_logic_predicates PASSED [ 50%]
tests/test_integrity.py::test_migration_adds_not_valid_then_validates PASSED [ 50%]
tests/test_integrity.py::test_migration_adds_constraints_without_scanning_and_is_idempotent PASSED [ 51%]
tests/test_integrity.py::test_index_changes_in_002_are_concurrent_and_outside_the_transaction PASSED [ 51%]
tests/test_integrity.py::test_referee_job_shares_merge_advisory_lock_key PASSED [ 51%]
tests/test_integrity.py::test_merge_drains_by_single_delete_returning_after_taking_the_lock[schema.sql] PASSED [ 51%]
tests/test_integrity.py::test_merge_drains_by_single_delete_returning_after_taking_the_lock[003] PASSED [ 51%]
tests/test_integrity.py::test_merge_drains_by_single_delete_returning_after_taking_the_lock[004] PASSED [ 52%]
tests/test_integrity.py::test_merge_drains_by_single_delete_returning_after_taking_the_lock[005] PASSED [ 52%]
tests/test_integrity.py::test_merge_drains_by_single_delete_returning_after_taking_the_lock[006] PASSED [ 52%]
tests/test_integrity.py::test_no_truncate_in_any_sql PASSED              [ 52%]
tests/test_integrity.py::test_merge_upserts_order_by_conflict_key_and_keep_latest_row PASSED [ 52%]
tests/test_integrity.py::test_staging_ddl_matches_staging_columns PASSED [ 52%]
tests/test_integrity.py::test_core_stats_ddl_and_merge_lists_match_stats_columns PASSED [ 53%]
tests/test_integrity.py::test_schema_declares_postgres_15_and_named_odds_constraints PASSED [ 53%]
tests/test_integrity.py::test_application_sql_references_existing_columns PASSED [ 53%]
tests/test_integrity.py::test_player_attributes_follow_the_latest_match_not_the_last_row_staged[schema.sql] PASSED [ 53%]
tests/test_integrity.py::test_player_attributes_follow_the_latest_match_not_the_last_row_staged[006] PASSED [ 53%]
tests/test_integrity.py::test_migration_006_adds_the_column_replaces_the_merge_and_repairs_in_one_transaction PASSED [ 54%]
tests/test_integrity.py::test_merge_stores_each_matchs_own_position PASSED [ 54%]
tests/test_league_level.py::test_season_starts_on_the_first_of_july PASSED [ 54%]
tests/test_league_level.py::test_trailing_rate_moves_from_last_season_to_this_one PASSED [ 54%]
tests/test_league_level.py::test_season_rates_chain_and_skip_empty_first_seasons PASSED [ 54%]
tests/test_league_level.py::test_history_is_rescaled_to_the_current_level PASSED [ 54%]
tests/test_league_level.py::test_a_constant_level_reproduces_the_unscaled_backtest PASSED [ 55%]
tests/test_league_level.py::test_a_lower_current_level_lowers_the_over_probability PASSED [ 55%]
tests/test_league_level.py::test_level_ratios_weight_minutes_without_inflating_them PASSED [ 55%]
tests/test_ledger.py::test_replies_parse[placed 3 @1.95 5-expected0] PASSED [ 55%]
tests/test_ledger.py::test_replies_parse[Placed 2 under @ 2.10 \xa310 Sky Bet-expected1] PASSED [ 55%]
tests/test_ledger.py::test_replies_parse[placed #1 o @1.9 5.50-expected2] PASSED [ 56%]
tests/test_ledger.py::test_replies_parse[undo 12-expected3] PASSED       [ 56%]
tests/test_ledger.py::test_replies_parse[UNDO #3-expected4] PASSED       [ 56%]
tests/test_ledger.py::test_replies_parse[settled 12 lost-expected5] PASSED [ 56%]
tests/test_ledger.py::test_replies_parse[Settle #4 WIN-expected6] PASSED [ 56%]
tests/test_ledger.py::test_replies_parse[settled 5 void-expected7] PASSED [ 56%]
tests/test_ledger.py::test_malformed_replies_explain_the_form[placed three @1.95 5] PASSED [ 57%]
tests/test_ledger.py::test_malformed_replies_explain_the_form[placed 3 @1.00 5] PASSED [ 57%]
tests/test_ledger.py::test_malformed_replies_explain_the_form[placed 3 @1.95 0] PASSED [ 57%]
tests/test_ledger.py::test_malformed_replies_explain_the_form[placed 3 1.95 5] PASSED [ 57%]
tests/test_ledger.py::test_malformed_replies_explain_the_form[hello] PASSED [ 57%]
tests/test_ledger.py::test_alert_bet_takes_its_side_and_book PASSED      [ 58%]
tests/test_ledger.py::test_board_bet_needs_a_side_that_is_listed PASSED  [ 58%]
tests/test_ledger.py::test_board_bet_without_a_book_is_recorded_as_unknown_and_low_odds_are_flagged PASSED [ 58%]
tests/test_ledger.py::test_alert_items_are_validated PASSED              [ 58%]
tests/test_ledger.py::test_updates_record_bets_once_and_only_from_the_configured_chat PASSED [ 58%]
tests/test_ledger.py::test_undo_cancels_once PASSED                      [ 58%]
tests/test_ledger.py::test_exposure_takes_the_larger_of_alerted_and_placed_per_market PASSED [ 59%]
tests/test_ledger.py::test_corrupt_state_files_raise PASSED              [ 59%]
tests/test_ledger.py::test_poll_answers_each_reply_and_remembers_the_offset PASSED [ 59%]
tests/test_ledger.py::test_digest_messages_are_indexed_by_message_id PASSED [ 59%]
tests/test_ledger.py::test_ledger_list_prints_open_bets PASSED           [ 59%]
tests/test_ledger.py::test_empty_report_indexes_nothing PASSED           [ 60%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[over-1.5-final-90-2-won] PASSED [ 60%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[over-1.5-final-90-1-lost] PASSED [ 60%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[under-1.5-final-90-1-won] PASSED [ 60%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[under-1.5-final-75-3-lost] PASSED [ 60%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[over-2.0-final-90-2-void] PASSED [ 60%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[under-0.5-final-0-0-void] PASSED [ 61%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[over-1.5-final-None-None-None] PASSED [ 61%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[over-1.5-live-60-4-None] PASSED [ 61%]
tests/test_ledger.py::test_grade_settles_on_the_stored_count[over-1.5-postponed-None-None-None] PASSED [ 61%]
tests/test_ledger.py::test_grade_refuses_an_unknown_market_or_a_missing_count PASSED [ 61%]
tests/test_ledger.py::test_profit_by_result PASSED                       [ 62%]
tests/test_ledger.py::test_settlement_records_are_validated PASSED       [ 62%]
tests/test_ledger.py::test_settle_open_bets_grades_only_final_matches_with_a_player_row PASSED [ 62%]
tests/test_ledger.py::test_a_settled_reply_overrides_the_automatic_result_and_is_recorded_once PASSED [ 62%]
tests/test_ledger.py::test_old_open_bets_stay_open_and_are_flagged_stale PASSED [ 62%]
tests/test_ledger.py::test_corrupt_settled_record_raises PASSED          [ 63%]
tests/test_ledger.py::test_a_pound_stake_is_converted_to_the_bankroll_currency_and_kept_as_entered PASSED [ 63%]
tests/test_ledger.py::test_a_pound_stake_without_a_rate_is_refused_not_guessed PASSED [ 63%]
tests/test_ledger.py::test_bets_recorded_before_the_currency_fields_still_load PASSED [ 63%]
tests/test_ledger.py::test_automatic_results_are_labelled_provisional_and_manual_ones_are_not PASSED [ 63%]
tests/test_model_freeze.py::test_pricing_code_is_the_frozen_model FAILED [ 63%]
tests/test_model_freeze.py::test_the_lockbox_starts_after_the_freeze PASSED [ 64%]
tests/test_model_freeze.py::test_digest_ignores_comments_but_not_logic PASSED [ 64%]
tests/test_model_freeze.py::test_a_calibrated_skilful_model_passes_and_an_overconfident_one_fails PASSED [ 64%]
tests/test_model_freeze.py::test_too_few_predictions_fail_the_sample_size_criterion PASSED [ 64%]
tests/test_model_freeze.py::test_the_clustered_interval_brackets_the_point_skill PASSED [ 64%]
tests/test_model_freeze.py::test_invalid_inputs_are_refused[p0-y0-strictly between] PASSED [ 65%]
tests/test_model_freeze.py::test_invalid_inputs_are_refused[p1-y1-strictly between] PASSED [ 65%]
tests/test_model_freeze.py::test_invalid_inputs_are_refused[p2-y2-strictly between] PASSED [ 65%]
tests/test_model_freeze.py::test_invalid_inputs_are_refused[p3-y3-0 or 1] PASSED [ 65%]
tests/test_model_freeze.py::test_invalid_inputs_are_refused[p4-y4-equal-length] PASSED [ 65%]
tests/test_model_freeze.py::test_an_unfittable_calibration_fails_the_line_instead_of_stopping_the_run PASSED [ 65%]
tests/test_model_freeze.py::test_identical_outcomes_are_refused_not_scored PASSED [ 66%]
tests/test_model_freeze.py::test_lockbox_constants_are_timezone_aware PASSED [ 66%]
tests/test_oddspapi.py::test_catalogue_keeps_player_over_under_tackles_and_fouls_only PASSED [ 66%]
tests/test_oddspapi.py::test_prop_prices_read_active_player_prices PASSED [ 66%]
tests/test_oddspapi.py::test_names_normalise_across_vendors PASSED       [ 66%]
tests/test_oddspapi.py::test_fixture_matching_needs_kickoff_and_both_clubs PASSED [ 67%]
tests/test_oddspapi.py::test_quotes_pair_sides_and_take_the_smaller_limit PASSED [ 67%]
tests/test_oddspapi.py::test_collect_odds_fetches_caches_and_archives PASSED [ 67%]
tests/test_oddspapi.py::test_a_bookmaker_without_fixtures_is_noted_not_fatal PASSED [ 67%]
tests/test_oddspapi.py::test_vendor_400_can_mean_no_data_and_errors_carry_the_scrubbed_body PASSED [ 67%]
tests/test_oddspapi.py::test_only_the_known_no_fixtures_400_counts_as_empty PASSED [ 67%]
tests/test_oddspapi.py::test_collect_odds_keeps_the_quota_reserve PASSED [ 68%]
tests/test_oddspapi.py::test_odds_outside_the_window_are_ignored PASSED  [ 68%]
tests/test_oddspapi.py::test_query_param_key_never_appears_in_errors PASSED [ 68%]
tests/test_oddspapi.py::test_odds_client_sends_the_key_as_a_query_parameter_not_a_header PASSED [ 68%]
tests/test_pipeline_concurrency.py::test_slate_is_processed_concurrently PASSED [ 68%]
tests/test_pipeline_concurrency.py::test_no_connection_is_held_during_network_io PASSED [ 69%]
tests/test_pipeline_concurrency.py::test_transient_database_error_is_retried PASSED [ 69%]
tests/test_pipeline_concurrency.py::test_statement_timeout_fails_closed_without_retry PASSED [ 69%]
tests/test_pipeline_concurrency.py::test_retries_exhausted_marks_fixture_failed PASSED [ 69%]
tests/test_pipeline_concurrency.py::test_one_failing_fixture_does_not_affect_others PASSED [ 69%]
tests/test_pipeline_concurrency.py::test_deadline_reports_timed_out_fixtures PASSED [ 69%]
tests/test_pipeline_concurrency.py::test_merge_failure_is_recorded_and_staging_preserved PASSED [ 70%]
tests/test_pipeline_concurrency.py::test_empty_slate_is_not_successful PASSED [ 70%]
tests/test_pipeline_concurrency.py::test_rejected_merge_rows_make_the_slate_unsuccessful PASSED [ 70%]
tests/test_pipeline_concurrency.py::test_vendor_client_refuses_non_https_base_url[http://vendor.invalid/api] PASSED [ 70%]
tests/test_pipeline_concurrency.py::test_vendor_client_refuses_non_https_base_url[ftp://vendor.invalid] PASSED [ 70%]
tests/test_pipeline_concurrency.py::test_vendor_client_refuses_non_https_base_url[vendor.invalid/api] PASSED [ 71%]
tests/test_pipeline_concurrency.py::test_vendor_client_refuses_non_https_base_url[https://] PASSED [ 71%]
tests/test_pipeline_concurrency.py::test_vendor_client_refuses_non_https_base_url[] PASSED [ 71%]
tests/test_pipeline_concurrency.py::test_lineup_job_upserts_and_zeroes_absent_players PASSED [ 71%]
tests/test_pipeline_concurrency.py::test_pool_smaller_than_workers_is_rejected PASSED [ 71%]
tests/test_pipeline_concurrency.py::test_rate_limiter_is_shared_across_threads PASSED [ 71%]
tests/test_pipeline_concurrency.py::test_vendor_client_uses_one_session_per_thread PASSED [ 72%]
tests/test_pipeline_concurrency.py::test_cli_arguments_parse PASSED      [ 72%]
tests/test_pipeline_concurrency.py::test_vendor_client_retries_once_by_default PASSED [ 72%]
tests/test_pipeline_concurrency.py::test_error_body_with_http_200_is_a_failure_not_data PASSED [ 72%]
tests/test_pipeline_concurrency.py::test_vendor_remaining_quota_header_stops_further_requests PASSED [ 72%]
tests/test_pipeline_concurrency.py::test_per_run_request_cap_counts_retries PASSED [ 73%]
tests/test_pipeline_concurrency.py::test_every_vendor_attempt_takes_a_rate_limiter_token PASSED [ 73%]
tests/test_pipeline_concurrency.py::test_long_retry_after_fails_fast_instead_of_blocking_the_worker PASSED [ 73%]
tests/test_pipeline_concurrency.py::test_short_retry_after_is_honoured_and_exhaustion_raises PASSED [ 73%]
tests/test_pipeline_concurrency.py::test_adapter_retries_only_connects_which_never_reach_the_vendor PASSED [ 73%]
tests/test_pipeline_concurrency.py::test_second_concurrent_slate_on_one_engine_is_refused PASSED [ 73%]
tests/test_pipeline_concurrency.py::test_merge_loop_sums_rounds_and_raises_when_the_backlog_outlasts_it PASSED [ 74%]
tests/test_pipeline_concurrency.py::test_lineup_job_uses_per_player_bench_probabilities PASSED [ 74%]
tests/test_results.py::test_finished_listing_keeps_final_scan_league_fixtures PASSED [ 74%]
tests/test_results.py::test_listing_window_looks_back_and_caps_the_span PASSED [ 74%]
tests/test_results.py::test_missing_fixtures_skips_stored_finals_and_runs_earliest_first PASSED [ 74%]
tests/test_results.py::test_a_listing_longer_than_the_page_limit_ingests_nothing PASSED [ 75%]
tests/test_results.py::test_run_ingests_only_the_missing_fixtures_and_reports_failures PASSED [ 75%]
tests/test_scan.py::test_scan_window_ends_at_the_next_four_hour_boundary PASSED [ 75%]
tests/test_scan.py::test_lineup_estimate_uses_the_teams_recent_fixtures PASSED [ 75%]
tests/test_scan.py::test_projections_cover_regular_starters_only_and_use_prior_matches PASSED [ 75%]
tests/test_scan.py::test_missing_league_rate_is_reported_not_guessed PASSED [ 76%]
tests/test_scan.py::test_mock_quotes_are_labelled_two_way_and_reproducible PASSED [ 76%]
tests/test_scan.py::test_upcoming_listing_keeps_scan_leagues_and_not_started_fixtures PASSED [ 76%]
tests/test_scan.py::test_a_usd_bankroll_needs_a_rate_and_converts_book_limits PASSED [ 76%]
tests/test_scan.py::test_a_fixture_listing_longer_than_the_page_limit_refuses_the_scan PASSED [ 76%]
tests/test_scan.py::test_alerts_carry_the_mock_label_names_and_respect_the_cap PASSED [ 76%]
tests/test_scan.py::test_a_fixture_that_cannot_be_priced_is_recorded_and_skipped PASSED [ 77%]
tests/test_scan.py::test_notify_max_alerts_must_be_non_negative PASSED   [ 77%]
tests/test_scan.py::test_odds_outage_is_reported_in_the_summary_not_fatal PASSED [ 77%]
tests/test_scan.py::test_constant_season_levels_change_no_price_and_a_lower_level_lowers_the_mean PASSED [ 77%]
tests/test_scan.py::test_each_market_is_priced_with_its_own_game_state_k PASSED [ 77%]
tests/test_scan.py::test_rate_multiplier_moves_the_mean_and_keeps_the_shape PASSED [ 78%]
tests/test_scan.py::test_a_history_season_without_a_level_is_skipped_not_guessed PASSED [ 78%]
tests/test_scan.py::test_board_minimum_odds_give_the_screens_profit_after_the_loss_floor PASSED [ 78%]
tests/test_scan.py::test_board_lists_the_line_closest_to_even_for_each_player_and_market PASSED [ 78%]
tests/test_scan.py::test_board_leaves_out_near_certain_and_long_shot_sides PASSED [ 78%]
tests/test_scan.py::test_board_inputs_and_entries_are_validated PASSED   [ 78%]
tests/test_scan.py::test_board_is_sent_once_per_match_and_never_for_mock_or_quoted_runs PASSED [ 79%]
tests/test_scan.py::test_board_messages_split_and_keep_the_paper_label PASSED [ 79%]
tests/test_scan.py::test_pricing_reads_positions_as_of_kickoff_never_the_latest_one PASSED [ 79%]
tests/test_sportmonks.py::test_fixture_maps_to_a_payload_transform_accepts PASSED [ 79%]
tests/test_sportmonks.py::test_payload_carries_the_untouched_vendor_response PASSED [ 79%]
tests/test_sportmonks.py::test_tackles_won_above_tackles_is_capped_logged_and_kept_raw PASSED [ 80%]
tests/test_sportmonks.py::test_unidentified_starter_is_skipped_and_logged PASSED [ 80%]
tests/test_sportmonks.py::test_unidentified_substitute_is_skipped_and_logged_with_minutes PASSED [ 80%]
tests/test_sportmonks.py::test_absent_details_are_zero_and_present_ones_map_exactly PASSED [ 80%]
tests/test_sportmonks.py::test_substitution_minutes_come_from_events PASSED [ 80%]
tests/test_sportmonks.py::test_finished_fixture_without_coverage_fails_instead_of_zero_filling PASSED [ 80%]
tests/test_sportmonks.py::test_own_goal_attribution_must_reproduce_the_score PASSED [ 81%]
tests/test_sportmonks.py::test_unknown_states_and_lineup_types_fail_closed PASSED [ 81%]
tests/test_sportmonks.py::test_scheduled_fixture_needs_no_stats PASSED   [ 81%]
tests/test_sportmonks.py::test_lineup_payload_maps_starting_eleven_and_bench PASSED [ 81%]
tests/test_sportmonks.py::test_vendor_client_sends_sportmonks_auth_and_adapts_responses PASSED [ 81%]
tests/test_sportmonks.py::test_build_vendor_client_defaults_to_sportmonks_and_reads_the_token PASSED [ 82%]
tests/test_status.py::test_report_states_database_alerts_trial_and_paper_label PASSED [ 82%]
tests/test_status.py::test_backlog_and_rejects_are_flagged PASSED        [ 82%]
tests/test_status.py::test_unreachable_database_is_reported_not_silent PASSED [ 82%]
tests/test_status.py::test_trial_expiry_turns_into_a_warning PASSED      [ 82%]
tests/test_status.py::test_default_database_is_never_the_ci_database PASSED [ 82%]
tests/test_status.py::test_report_lists_open_and_stale_bets_and_settled_results PASSED [ 83%]
tests/test_status.py::test_report_shows_the_exchange_rate_and_flags_a_stale_one PASSED [ 83%]
tests/test_typing_contract.py::test_every_package_marker_is_empty PASSED [ 83%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/alerts/fx.py] PASSED [ 83%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/alerts/ledger.py] PASSED [ 83%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/alerts/replies.py] PASSED [ 84%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/alerts/scan.py] PASSED [ 84%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/alerts/status.py] PASSED [ 84%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/alerts/telegram.py] PASSED [ 84%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/calibration/fitting.py] PASSED [ 84%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/calibration/referee_job.py] PASSED [ 84%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/config.py] PASSED [ 85%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/evaluation/backtest.py] PASSED [ 85%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/evaluation/gate2.py] PASSED [ 85%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/ingestion/oddspapi.py] PASSED [ 85%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/ingestion/pipeline.py] PASSED [ 85%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/ingestion/results.py] PASSED [ 86%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/ingestion/sportmonks.py] PASSED [ 86%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/ingestion/transform.py] PASSED [ 86%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/modeling/engine.py] PASSED [ 86%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/modeling/league_level.py] PASSED [ 86%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/modeling/model_freeze.py] PASSED [ 86%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/scanner/market_scanner.py] PASSED [ 87%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/scanner/slate.py] PASSED [ 87%]
tests/test_typing_contract.py::test_module_uses_postponed_annotations[prop_alpha/scanner/value_screen.py] PASSED [ 87%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/alerts/fx.py] PASSED [ 87%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/alerts/ledger.py] PASSED [ 87%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/alerts/replies.py] PASSED [ 88%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/alerts/scan.py] PASSED [ 88%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/alerts/status.py] PASSED [ 88%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/alerts/telegram.py] PASSED [ 88%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/calibration/fitting.py] PASSED [ 88%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/calibration/referee_job.py] PASSED [ 89%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/config.py] PASSED [ 89%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/evaluation/backtest.py] PASSED [ 89%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/evaluation/gate2.py] PASSED [ 89%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/ingestion/oddspapi.py] PASSED [ 89%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/ingestion/pipeline.py] PASSED [ 89%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/ingestion/results.py] PASSED [ 90%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/ingestion/sportmonks.py] PASSED [ 90%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/ingestion/transform.py] PASSED [ 90%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/modeling/engine.py] PASSED [ 90%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/modeling/league_level.py] PASSED [ 90%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/modeling/model_freeze.py] PASSED [ 91%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/scanner/market_scanner.py] PASSED [ 91%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/scanner/slate.py] PASSED [ 91%]
tests/test_typing_contract.py::test_every_function_is_fully_annotated[prop_alpha/scanner/value_screen.py] PASSED [ 91%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/alerts/fx.py] PASSED [ 91%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/alerts/ledger.py] PASSED [ 91%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/alerts/replies.py] PASSED [ 92%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/alerts/scan.py] PASSED [ 92%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/alerts/status.py] PASSED [ 92%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/alerts/telegram.py] PASSED [ 92%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/calibration/fitting.py] PASSED [ 92%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/calibration/referee_job.py] PASSED [ 93%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/config.py] PASSED [ 93%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/evaluation/backtest.py] PASSED [ 93%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/evaluation/gate2.py] PASSED [ 93%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/ingestion/oddspapi.py] PASSED [ 93%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/ingestion/pipeline.py] PASSED [ 93%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/ingestion/results.py] PASSED [ 94%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/ingestion/sportmonks.py] PASSED [ 94%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/ingestion/transform.py] PASSED [ 94%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/modeling/engine.py] PASSED [ 94%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/modeling/league_level.py] PASSED [ 94%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/modeling/model_freeze.py] PASSED [ 95%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/scanner/market_scanner.py] PASSED [ 95%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/scanner/slate.py] PASSED [ 95%]
tests/test_typing_contract.py::test_module_constants_are_final[prop_alpha/scanner/value_screen.py] PASSED [ 95%]
tests/test_typing_contract.py::test_typing_detectors_flag_deliberate_violations[def f(x, y: int) -> int:\n    return y\n-_missing_annotations] PASSED [ 95%]
tests/test_typing_contract.py::test_typing_detectors_flag_deliberate_violations[def f(x: int):\n    return x\n-_missing_annotations] PASSED [ 95%]
tests/test_typing_contract.py::test_typing_detectors_flag_deliberate_violations[def outer() -> None:\n    def inner(cur):\n        return None\n-_missing_annotations] PASSED [ 96%]
tests/test_typing_contract.py::test_typing_detectors_flag_deliberate_violations[LIMIT = 3\n-_non_final_constants] PASSED [ 96%]
tests/test_typing_contract.py::test_typing_detectors_flag_deliberate_violations[from typing import Sequence\nLIMIT: Sequence[int] = (3,)\n-_non_final_constants] PASSED [ 96%]
tests/test_value_screen.py::test_simulated_conditions_average_to_the_exact_price PASSED [ 96%]
tests/test_value_screen.py::test_moment_matched_projection_simulation_matches_its_price PASSED [ 96%]
tests/test_value_screen.py::test_survival_count_is_reproducible_for_a_seed PASSED [ 97%]
tests/test_value_screen.py::test_loss_floor_screens_out_the_near_certain_under PASSED [ 97%]
tests/test_value_screen.py::test_reference_overs_are_mispriced_but_not_robust PASSED [ 97%]
tests/test_value_screen.py::test_robust_under_with_enough_edge_is_high_value PASSED [ 97%]
tests/test_value_screen.py::test_below_four_percent_is_never_high_value[0.035] PASSED [ 97%]
tests/test_value_screen.py::test_below_four_percent_is_never_high_value[0.0399] PASSED [ 97%]
tests/test_value_screen.py::test_four_percent_with_a_stable_player_qualifies PASSED [ 98%]
tests/test_value_screen.py::test_player_likely_to_miss_the_match_cannot_survive PASSED [ 98%]
tests/test_value_screen.py::test_exactly_sixty_percent_is_not_enough PASSED [ 98%]
tests/test_value_screen.py::test_mispricing_uses_the_devigged_book_probability PASSED [ 98%]
tests/test_value_screen.py::test_void_rules_are_applied_in_absent_and_substitute_conditions PASSED [ 98%]
tests/test_value_screen.py::test_criteria_and_inputs_are_validated PASSED [ 99%]
tests/test_value_screen.py::test_demo_case_contains_one_high_value_bet PASSED [ 99%]
tests/test_value_screen.py::test_high_value_requires_all_three_conditions[True-True-601-True] PASSED [ 99%]
tests/test_value_screen.py::test_high_value_requires_all_three_conditions[False-True-900-False] PASSED [ 99%]
tests/test_value_screen.py::test_high_value_requires_all_three_conditions[True-False-900-False] PASSED [ 99%]
tests/test_value_screen.py::test_high_value_requires_all_three_conditions[True-True-600-False] PASSED [100%]

================================== FAILURES ===================================
____________________ test_pricing_code_is_the_frozen_model ____________________

    def test_pricing_code_is_the_frozen_model() -> None:
        """Fails on any change to how a price is computed. Revert it, or re-freeze (model_freeze.py docstring): new
        FROZEN_DIGEST, FROZEN_ON and a later LOCKBOX_START, which discards earlier lockbox evidence."""
>       assert model_freeze.pricing_digest() == model_freeze.FROZEN_DIGEST
E       AssertionError: assert '91110fecf608...4f454cf4de0c2' == '001b17a17d21...ec2076012e58b'
E         
E         - 001b17a17d21188788e39e93b65e10d7ce9a08c2f3649f84534ec2076012e58b
E         + 91110fecf608eb9f4df9be1c4cd934e1e8d0e659376a66e91434f454cf4de0c2

tests\test_model_freeze.py:20: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_model_freeze.py::test_pricing_code_is_the_frozen_model - As...
================== 1 failed, 545 passed in 66.04s (0:01:06) ===================
