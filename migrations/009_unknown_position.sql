-- Migration 009: a player's position may be unknown; store it as NULL, never as a made-up position code.
-- Upgrades a database with 008 applied; a fresh database gets schema.sql only, which already contains this.
-- Run with: psql -v ON_ERROR_STOP=1 -f migrations/009_unknown_position.sql
--
-- Found 2026-10-05 (maintainer's operational list): Sportmonks lists some players without a position_id, and the
-- adapter stored them as the position "P" || position_id, i.e. 'PNone' (14 player-matches, all substitutes in
-- fixture 19727778, and 3 players' primary_position). Pricing groups league rates and squads by position, so
-- 'PNone' was a position bucket of its own. The pricing SQL already leaves out a NULL match_position (it falls back
-- to the player's latest known position before kickoff), so NULL is the honest value and needs no pricing change.
--
-- Changes: the merge no longer rejects a row whose player_position is NULL (the adapter now sends NULL for a missing
-- position_id and refuses an unrecognised one); a NULL position never overwrites a player's known primary_position;
-- players.primary_position may be NULL (a new player whose only match has no position); and the stored 'P<id>'
-- sentinels are repaired: match_position becomes NULL, primary_position the latest known match position, else NULL.
--
-- Lock profile: DROP NOT NULL is catalog-only (no rewrite, no scan) but takes a brief ACCESS EXCLUSIVE lock on
-- players, bounded by lock_timeout; CREATE OR REPLACE FUNCTION is catalog-only; the repair runs under the merge's
-- advisory lock and touches only sentinel rows, so re-running this migration changes nothing. Migrations run in
-- numeric order: re-applying 006 or 007 after this one would copy 'PNone' back from the stored raw payloads.

SET lock_timeout = '5s';

BEGIN;

ALTER TABLE prop_alpha.players ALTER COLUMN primary_position DROP NOT NULL;

DROP FUNCTION IF EXISTS prop_alpha.merge_staging_player_feed();

CREATE OR REPLACE FUNCTION prop_alpha.merge_staging_player_feed(p_max_batches integer DEFAULT 200)
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

    IF p_max_batches IS NULL OR p_max_batches < 1 THEN
        RAISE EXCEPTION 'p_max_batches must be a positive integer, got %', p_max_batches;
    END IF;

    -- Claim whole batches, oldest first, in one statement. A worker commits one batch_id atomically, so a
    -- batch is either fully visible to this snapshot or not at all, and a fixture is never split.
    WITH oldest AS (
        SELECT s.batch_id
        FROM prop_alpha.staging_player_feed s
        GROUP BY s.batch_id
        ORDER BY min(s.staged_at), s.batch_id
        LIMIT p_max_batches
    ), drained AS (
        DELETE FROM prop_alpha.staging_player_feed s
        USING oldest o
        WHERE s.batch_id = o.batch_id
        RETURNING s.*
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
               OR c.tackles_won > c.tackles_attempted
               OR c.duels_won > c.duels_total
               OR c.yellow_cards NOT BETWEEN 0 AND 2
               OR c.red_cards NOT BETWEEN 0 AND 1
               OR c.minutes_played < 0
               OR c.minutes_played > 130
               OR c.minute_off < c.minute_on
               OR c.minute_on < 0
               OR c.minute_off < 0
               OR c.minutes_played IS NULL
               OR (c.minutes_played > 0 AND (   c.tackles_attempted IS NULL OR c.tackles_won IS NULL
                                            OR c.fouls_committed   IS NULL OR c.fouls_drawn IS NULL
                                            OR c.yellow_cards      IS NULL OR c.red_cards   IS NULL))
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

    -- Name and position come from the player's latest match by kickoff, never from the last row staged: a
    -- backfilled older match must not overwrite them, and with concurrent workers "last staged" is arbitrary.
    -- attributes_as_of is the kickoff of the match that set them; an older match leaves them alone.
    INSERT INTO prop_alpha.players AS pl
        (full_name, primary_position, current_team_id, vendor_player_uid, attributes_as_of)
    SELECT DISTINCT ON (c.vendor_player_uid)
           c.player_name, c.player_position, t.team_id, c.vendor_player_uid, c.kickoff_at
    FROM claimed_feed c
    JOIN prop_alpha.teams t ON t.vendor_team_uid = c.team_uid
    WHERE c.vendor_player_uid IS NOT NULL
    ORDER BY c.vendor_player_uid, c.kickoff_at DESC, c.vendor_fixture_uid DESC, c.staged_at DESC
    ON CONFLICT (vendor_player_uid) DO UPDATE SET
        full_name        = EXCLUDED.full_name,
        -- A match with an unknown position (migration 009) never erases a known one.
        primary_position = COALESCE(EXCLUDED.primary_position, pl.primary_position),
        attributes_as_of = EXCLUDED.attributes_as_of,
        updated_at       = now()
    WHERE pl.attributes_as_of IS NULL OR EXCLUDED.attributes_as_of >= pl.attributes_as_of;

    INSERT INTO prop_alpha.player_match_stats AS s (
        fixture_id, player_id, team_id, is_home, started, minute_on, minute_off, minutes_played,
        match_state_leading_min, match_state_level_min, match_state_trailing_min,
        tackles_attempted, tackles_won, interceptions, dribbled_past, duels_total, duels_won,
        fouls_committed, fouls_drawn, yellow_cards, red_cards,
        zone14_entries, halfspace_entries, long_balls_targeted, team_possession_pct,
        padj_tackles, padj_interceptions, padj_fouls, dribbled_past_rate, match_position
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
           x.team_possession_pct, x.padj_tackles, x.padj_interceptions, x.padj_fouls, x.dribbled_past_rate,
           x.player_position
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
        match_position           = EXCLUDED.match_position,
        source_updated_at        = now();
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    -- A player's current team is the team of their latest fixture by kickoff, not of the latest staged row:
    -- a backfilled old match must not move a transferred player back. The advisory lock serialises merges,
    -- the only writer of players, so this UPDATE needs no lock ordering.
    UPDATE prop_alpha.players AS pl
    SET current_team_id = latest.team_id,
        updated_at      = now()
    FROM (
        SELECT DISTINCT ON (s.player_id) s.player_id, s.team_id
        FROM prop_alpha.player_match_stats s
        JOIN prop_alpha.fixtures f ON f.fixture_id = s.fixture_id
        WHERE s.player_id IN (
            SELECT p.player_id
            FROM claimed_feed c
            JOIN prop_alpha.players p ON p.vendor_player_uid = c.vendor_player_uid
        )
        ORDER BY s.player_id, f.kickoff_at DESC, f.fixture_id DESC
    ) latest
    WHERE pl.player_id = latest.player_id
      AND pl.current_team_id IS DISTINCT FROM latest.team_id;

    RETURN QUERY SELECT v_fixtures, v_rows, v_rejected;
END;
$$;

-- One-time repair of the 'P<id>' sentinels, under the merge's advisory lock.
SELECT pg_advisory_xact_lock(hashtext('prop_alpha.merge_staging_player_feed'));

UPDATE prop_alpha.player_match_stats
SET match_position = NULL
WHERE match_position ~ '^P(None|[0-9]+)$';

UPDATE prop_alpha.players AS pl
SET primary_position = latest.match_position,
    updated_at       = now()
FROM (
    SELECT DISTINCT ON (s.player_id) s.player_id, s.match_position
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.fixtures f USING (fixture_id)
    WHERE s.match_position IS NOT NULL
    ORDER BY s.player_id, f.kickoff_at DESC, f.fixture_id DESC
) latest
WHERE pl.player_id = latest.player_id
  AND pl.primary_position ~ '^P(None|[0-9]+)$';

UPDATE prop_alpha.players
SET primary_position = NULL,
    updated_at       = now()
WHERE primary_position ~ '^P(None|[0-9]+)$';

COMMIT;

RESET lock_timeout;
