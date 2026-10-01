-- Migration 002: v2 -> v3 (staging landing zone, merge function, odds index swap).
-- Run with psql. The transactional part runs first; the index swap uses CONCURRENTLY so
-- odds_lines stays writable, and CONCURRENTLY cannot run inside a transaction block.

BEGIN;

ALTER TABLE prop_alpha.player_match_stats  SET (fillfactor = 90);
ALTER TABLE prop_alpha.lineup_expectations SET (fillfactor = 90);

-- ============================================================
-- Append-only landing zone. UNLOGGED: no WAL, so inserts are cheap, but contents are lost on a
-- crash and are not replicated to standbys. raw_payloads (logged) is the durable copy, so any
-- lost staging rows can be rebuilt by re-running the transform over raw_payloads.
-- No keys, constraints or indexes: writers never contend on anything but the heap.
-- ============================================================
CREATE UNLOGGED TABLE IF NOT EXISTS prop_alpha.staging_player_feed (
    batch_id                 UUID        NOT NULL,
    staged_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    vendor_fixture_uid       VARCHAR(64) NOT NULL,
    home_team_uid            VARCHAR(64) NOT NULL,
    away_team_uid            VARCHAR(64) NOT NULL,
    kickoff_at               TIMESTAMPTZ NOT NULL,
    fixture_status           VARCHAR(16) NOT NULL,
    home_score               SMALLINT,
    away_score               SMALLINT,
    matchday                 SMALLINT,
    referee_uid              VARCHAR(64),
    referee_name             VARCHAR(128),
    vendor_player_uid        VARCHAR(64),     -- NULL on the per-fixture header row
    player_name              VARCHAR(128),
    player_position          VARCHAR(16),
    team_uid                 VARCHAR(64),
    is_home                  BOOLEAN,
    started                  BOOLEAN,
    minute_on                SMALLINT,
    minute_off               SMALLINT,
    minutes_played           SMALLINT,
    match_state_leading_min  SMALLINT,
    match_state_level_min    SMALLINT,
    match_state_trailing_min SMALLINT,
    tackles_attempted        SMALLINT,
    tackles_won              SMALLINT,
    interceptions            SMALLINT,
    dribbled_past            SMALLINT,
    duels_total              SMALLINT,
    duels_won                SMALLINT,
    fouls_committed          SMALLINT,
    fouls_drawn              SMALLINT,
    yellow_cards             SMALLINT,
    red_cards                SMALLINT,
    zone14_entries           SMALLINT,
    halfspace_entries        SMALLINT,
    long_balls_targeted      SMALLINT,
    team_possession_pct      NUMERIC(5,2),
    padj_tackles             NUMERIC(7,3),
    padj_interceptions       NUMERIC(7,3),
    padj_fouls               NUMERIC(7,3),
    dribbled_past_rate       NUMERIC(5,4)
);

CREATE TABLE IF NOT EXISTS prop_alpha.staging_rejects (
    reject_id          BIGSERIAL PRIMARY KEY,
    vendor_fixture_uid VARCHAR(64),
    vendor_player_uid  VARCHAR(64),
    reason             TEXT        NOT NULL,
    row_data           JSONB       NOT NULL,
    rejected_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- merge_staging_player_feed(): atomically drains staging into the core tables.
--   * pg_advisory_xact_lock serialises mergers; ingestion writers are never blocked.
--   * DELETE ... RETURNING claims exactly the rows committed before the statement's snapshot.
--     TRUNCATE is deliberately NOT used: it takes an ACCESS EXCLUSIVE lock (stalling every writer)
--     and a read-then-truncate sequence silently discards rows committed between the two steps.
--   * Latest row per key wins (DISTINCT ON ... staged_at DESC), so ON CONFLICT never touches a
--     row twice in one statement.
--   * Rows that would violate core constraints go to staging_rejects instead of aborting the batch.
--   * Every upsert orders by its conflict key, giving a consistent lock order.
-- Call: SELECT * FROM prop_alpha.merge_staging_player_feed();
-- ============================================================
CREATE OR REPLACE FUNCTION prop_alpha.merge_staging_player_feed()
RETURNS TABLE (fixtures_merged integer, player_rows_merged integer, rows_rejected integer)
LANGUAGE plpgsql
AS $$
DECLARE
    v_fixtures  integer := 0;
    v_rows      integer := 0;
    v_rejected  integer := 0;
    v_step      integer := 0;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('prop_alpha.merge_staging_player_feed'));

    DROP TABLE IF EXISTS pg_temp.claimed_feed;
    CREATE TEMP TABLE claimed_feed (LIKE prop_alpha.staging_player_feed) ON COMMIT DROP;

    WITH drained AS (
        DELETE FROM prop_alpha.staging_player_feed RETURNING *
    )
    INSERT INTO claimed_feed SELECT * FROM drained;

    WITH bad AS (
        DELETE FROM claimed_feed c
        WHERE NOT EXISTS (SELECT 1 FROM prop_alpha.teams t WHERE t.vendor_team_uid = c.home_team_uid)
           OR NOT EXISTS (SELECT 1 FROM prop_alpha.teams t WHERE t.vendor_team_uid = c.away_team_uid)
           OR c.fixture_status NOT IN ('scheduled', 'live', 'final', 'postponed')
        RETURNING c.*
    )
    INSERT INTO prop_alpha.staging_rejects (vendor_fixture_uid, vendor_player_uid, reason, row_data)
    SELECT b.vendor_fixture_uid, b.vendor_player_uid, 'unknown team uid or invalid fixture status', to_jsonb(b)
    FROM bad b;
    GET DIAGNOSTICS v_step = ROW_COUNT;
    v_rejected := v_rejected + v_step;

    WITH bad AS (
        DELETE FROM claimed_feed c
        WHERE c.vendor_player_uid IS NOT NULL
          AND (   c.team_uid IS NULL
               OR c.team_uid NOT IN (c.home_team_uid, c.away_team_uid)
               OR c.player_name IS NULL
               OR c.player_position IS NULL
               OR c.tackles_won > c.tackles_attempted
               OR c.duels_won > c.duels_total
               OR c.yellow_cards NOT BETWEEN 0 AND 2
               OR c.red_cards NOT BETWEEN 0 AND 1
               OR c.minutes_played < 0
               OR c.minute_on < 0
               OR c.minute_off < 0
               OR (c.team_possession_pct IS NOT NULL AND c.team_possession_pct NOT BETWEEN 0 AND 100))
        RETURNING c.*
    )
    INSERT INTO prop_alpha.staging_rejects (vendor_fixture_uid, vendor_player_uid, reason, row_data)
    SELECT b.vendor_fixture_uid, b.vendor_player_uid, 'player row violates core constraints', to_jsonb(b)
    FROM bad b;
    GET DIAGNOSTICS v_step = ROW_COUNT;
    v_rejected := v_rejected + v_step;

    INSERT INTO prop_alpha.referees AS r (full_name, league_id, vendor_referee_uid)
    SELECT DISTINCT ON (c.referee_uid)
           COALESCE(c.referee_name, 'unknown'), t.league_id, c.referee_uid
    FROM claimed_feed c
    JOIN prop_alpha.teams t ON t.vendor_team_uid = c.home_team_uid
    WHERE c.referee_uid IS NOT NULL
    ORDER BY c.referee_uid, c.staged_at DESC
    ON CONFLICT (vendor_referee_uid) DO UPDATE SET full_name = EXCLUDED.full_name;

    INSERT INTO prop_alpha.fixtures AS fx (
        league_id, home_team_id, away_team_id, referee_id, referee_confirmed_at,
        kickoff_at, matchday, home_score, away_score, status, vendor_fixture_uid
    )
    SELECT th.league_id, th.team_id, ta.team_id, rf.referee_id,
           CASE WHEN rf.referee_id IS NOT NULL THEN now() END,
           x.kickoff_at, x.matchday, x.home_score, x.away_score, x.fixture_status, x.vendor_fixture_uid
    FROM (
        SELECT DISTINCT ON (cf.vendor_fixture_uid) cf.*
        FROM claimed_feed cf
        ORDER BY cf.vendor_fixture_uid, cf.staged_at DESC
    ) x
    JOIN prop_alpha.teams th ON th.vendor_team_uid = x.home_team_uid
    JOIN prop_alpha.teams ta ON ta.vendor_team_uid = x.away_team_uid
    LEFT JOIN prop_alpha.referees rf ON rf.vendor_referee_uid = x.referee_uid
    ORDER BY x.vendor_fixture_uid
    ON CONFLICT (vendor_fixture_uid) DO UPDATE SET
        referee_id           = COALESCE(EXCLUDED.referee_id, fx.referee_id),
        referee_confirmed_at = COALESCE(fx.referee_confirmed_at, EXCLUDED.referee_confirmed_at),
        kickoff_at           = EXCLUDED.kickoff_at,
        matchday             = COALESCE(EXCLUDED.matchday, fx.matchday),
        home_score           = EXCLUDED.home_score,
        away_score           = EXCLUDED.away_score,
        status               = EXCLUDED.status,
        ingested_at          = now();
    GET DIAGNOSTICS v_fixtures = ROW_COUNT;

    INSERT INTO prop_alpha.players AS pl (full_name, primary_position, current_team_id, vendor_player_uid)
    SELECT DISTINCT ON (c.vendor_player_uid)
           c.player_name, c.player_position, t.team_id, c.vendor_player_uid
    FROM claimed_feed c
    JOIN prop_alpha.teams t ON t.vendor_team_uid = c.team_uid
    WHERE c.vendor_player_uid IS NOT NULL
    ORDER BY c.vendor_player_uid, c.staged_at DESC
    ON CONFLICT (vendor_player_uid) DO UPDATE SET
        full_name        = EXCLUDED.full_name,
        primary_position = EXCLUDED.primary_position,
        updated_at       = now();

    INSERT INTO prop_alpha.player_match_stats AS s (
        fixture_id, player_id, team_id, is_home, started, minute_on, minute_off, minutes_played,
        match_state_leading_min, match_state_level_min, match_state_trailing_min,
        tackles_attempted, tackles_won, interceptions, dribbled_past, duels_total, duels_won,
        fouls_committed, fouls_drawn, yellow_cards, red_cards,
        zone14_entries, halfspace_entries, long_balls_targeted, team_possession_pct,
        padj_tackles, padj_interceptions, padj_fouls, dribbled_past_rate
    )
    SELECT f.fixture_id, p.player_id, t.team_id,
           COALESCE(x.is_home, x.team_uid = x.home_team_uid), COALESCE(x.started, FALSE),
           COALESCE(x.minute_on, 0), COALESCE(x.minute_off, 0), COALESCE(x.minutes_played, 0),
           COALESCE(x.match_state_leading_min, 0), COALESCE(x.match_state_level_min, 0),
           COALESCE(x.match_state_trailing_min, 0),
           COALESCE(x.tackles_attempted, 0), COALESCE(x.tackles_won, 0), COALESCE(x.interceptions, 0),
           COALESCE(x.dribbled_past, 0), COALESCE(x.duels_total, 0), COALESCE(x.duels_won, 0),
           COALESCE(x.fouls_committed, 0), COALESCE(x.fouls_drawn, 0),
           COALESCE(x.yellow_cards, 0), COALESCE(x.red_cards, 0),
           COALESCE(x.zone14_entries, 0), COALESCE(x.halfspace_entries, 0), COALESCE(x.long_balls_targeted, 0),
           x.team_possession_pct, x.padj_tackles, x.padj_interceptions, x.padj_fouls, x.dribbled_past_rate
    FROM (
        SELECT DISTINCT ON (cf.vendor_fixture_uid, cf.vendor_player_uid) cf.*
        FROM claimed_feed cf
        WHERE cf.vendor_player_uid IS NOT NULL
        ORDER BY cf.vendor_fixture_uid, cf.vendor_player_uid, cf.staged_at DESC
    ) x
    JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = x.vendor_fixture_uid
    JOIN prop_alpha.players  p ON p.vendor_player_uid  = x.vendor_player_uid
    JOIN prop_alpha.teams    t ON t.vendor_team_uid    = x.team_uid
    ORDER BY f.fixture_id, p.player_id
    ON CONFLICT (fixture_id, player_id) DO UPDATE SET
        team_id                  = EXCLUDED.team_id,
        is_home                  = EXCLUDED.is_home,
        started                  = EXCLUDED.started,
        minute_on                = EXCLUDED.minute_on,
        minute_off               = EXCLUDED.minute_off,
        minutes_played           = EXCLUDED.minutes_played,
        match_state_leading_min  = EXCLUDED.match_state_leading_min,
        match_state_level_min    = EXCLUDED.match_state_level_min,
        match_state_trailing_min = EXCLUDED.match_state_trailing_min,
        tackles_attempted        = EXCLUDED.tackles_attempted,
        tackles_won              = EXCLUDED.tackles_won,
        interceptions            = EXCLUDED.interceptions,
        dribbled_past            = EXCLUDED.dribbled_past,
        duels_total              = EXCLUDED.duels_total,
        duels_won                = EXCLUDED.duels_won,
        fouls_committed          = EXCLUDED.fouls_committed,
        fouls_drawn              = EXCLUDED.fouls_drawn,
        yellow_cards             = EXCLUDED.yellow_cards,
        red_cards                = EXCLUDED.red_cards,
        zone14_entries           = EXCLUDED.zone14_entries,
        halfspace_entries        = EXCLUDED.halfspace_entries,
        long_balls_targeted      = EXCLUDED.long_balls_targeted,
        team_possession_pct      = EXCLUDED.team_possession_pct,
        padj_tackles             = EXCLUDED.padj_tackles,
        padj_interceptions       = EXCLUDED.padj_interceptions,
        padj_fouls               = EXCLUDED.padj_fouls,
        dribbled_past_rate       = EXCLUDED.dribbled_past_rate,
        source_updated_at        = now();
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    RETURN QUERY SELECT v_fixtures, v_rows, v_rejected;
END;
$$;

COMMIT;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_odds_fixture_captured
    ON prop_alpha.odds_lines (fixture_id, captured_at DESC);
DROP INDEX CONCURRENTLY IF EXISTS prop_alpha.idx_odds_captured_brin;
