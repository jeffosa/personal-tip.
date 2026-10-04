-- prop_alpha schema v3.3 (reconstructed): fresh install for PostgreSQL 15+.
-- Status: installs cleanly on PostgreSQL 18.6 (ci/bootstrap.sh stage 6, 2026-10-02); the merge has only
-- run on empty staging and on the fixtures in tests/test_merge_postgres.sql. Not run on PostgreSQL 15.
-- A fresh database gets this file only; it already embodies migrations 002 to 006.
-- Never apply migrations/002_staging_and_indexes.sql to a database built from this file: it would
-- reinstall the v3.0 merge function and drop the minute-logic reject predicates.
-- Run with: psql -v ON_ERROR_STOP=1 -f schema.sql
-- PostgreSQL 15 is required for UNIQUE NULLS NOT DISTINCT (uq_odds_snapshot).

CREATE SCHEMA IF NOT EXISTS prop_alpha;
SET search_path TO prop_alpha, public;

-- ============================================================
-- Reference entities
-- ============================================================
CREATE TABLE leagues (
    league_id        SERIAL       PRIMARY KEY,
    name             VARCHAR(64)  NOT NULL UNIQUE,
    country          VARCHAR(64)  NOT NULL,
    tier             SMALLINT     NOT NULL CHECK (tier BETWEEN 1 AND 6)
);

CREATE TABLE teams (
    team_id          SERIAL       PRIMARY KEY,
    league_id        INTEGER      NOT NULL REFERENCES leagues(league_id),
    name             VARCHAR(128) NOT NULL,
    vendor_team_uid  VARCHAR(64)  NOT NULL UNIQUE
);

CREATE TABLE players (
    player_id         SERIAL       PRIMARY KEY,
    full_name         VARCHAR(128) NOT NULL,
    primary_position  VARCHAR(16)  NOT NULL,
    current_team_id   INTEGER      REFERENCES teams(team_id),
    vendor_player_uid VARCHAR(64)  NOT NULL UNIQUE,
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- Kickoff of the match that set full_name and primary_position (migration 006).
    attributes_as_of  TIMESTAMPTZ
);

-- Coefficients are written only by calibration.referee_job.run_referee_job, under the merge's
-- advisory lock. New referees inserted by the merge start neutral (SC = 1, HBR = 0).
CREATE TABLE referees (
    referee_id                  SERIAL       PRIMARY KEY,
    full_name                   VARCHAR(128) NOT NULL,
    league_id                   INTEGER      NOT NULL REFERENCES leagues(league_id),
    vendor_referee_uid          VARCHAR(64)  NOT NULL UNIQUE,
    foul_strictness_coefficient NUMERIC(7,4) NOT NULL DEFAULT 1.0,
    yellow_card_propensity      NUMERIC(7,4) NOT NULL DEFAULT 0.0,
    red_card_propensity         NUMERIC(7,4) NOT NULL DEFAULT 0.0,
    home_bias_ratio             NUMERIC(7,4) NOT NULL DEFAULT 0.0
);

CREATE TABLE fixtures (
    fixture_id           BIGSERIAL   PRIMARY KEY,
    league_id            INTEGER     NOT NULL REFERENCES leagues(league_id),
    home_team_id         INTEGER     NOT NULL REFERENCES teams(team_id),
    away_team_id         INTEGER     NOT NULL REFERENCES teams(team_id),
    referee_id           INTEGER     REFERENCES referees(referee_id),
    referee_confirmed_at TIMESTAMPTZ,
    kickoff_at           TIMESTAMPTZ NOT NULL,
    matchday             SMALLINT,
    home_score           SMALLINT,
    away_score           SMALLINT,
    status               VARCHAR(16) NOT NULL CHECK (status IN ('scheduled', 'live', 'final', 'postponed')),
    vendor_fixture_uid   VARCHAR(64) NOT NULL UNIQUE,
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Core statistics (named integrity constraints; ARCHITECTURE-ESSENTIALS 3.3, verbatim)
-- ============================================================
CREATE TABLE player_match_stats (
    fixture_id               BIGINT   NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    player_id                INTEGER  NOT NULL REFERENCES players(player_id),
    team_id                  INTEGER  NOT NULL REFERENCES teams(team_id),
    is_home                  BOOLEAN  NOT NULL,
    started                  BOOLEAN  NOT NULL,
    minute_on                SMALLINT NOT NULL DEFAULT 0 CHECK (minute_on >= 0),
    minute_off               SMALLINT NOT NULL DEFAULT 0 CHECK (minute_off >= 0),
    minutes_played           SMALLINT NOT NULL DEFAULT 0 CHECK (minutes_played >= 0),
    match_state_leading_min  SMALLINT NOT NULL DEFAULT 0,
    match_state_level_min    SMALLINT NOT NULL DEFAULT 0,
    match_state_trailing_min SMALLINT NOT NULL DEFAULT 0,

    tackles_attempted        SMALLINT NOT NULL DEFAULT 0,
    tackles_won              SMALLINT NOT NULL DEFAULT 0,
    interceptions            SMALLINT NOT NULL DEFAULT 0,
    dribbled_past            SMALLINT NOT NULL DEFAULT 0,
    duels_total              SMALLINT NOT NULL DEFAULT 0,
    duels_won                SMALLINT NOT NULL DEFAULT 0,

    fouls_committed          SMALLINT NOT NULL DEFAULT 0,
    fouls_drawn              SMALLINT NOT NULL DEFAULT 0,
    yellow_cards             SMALLINT NOT NULL DEFAULT 0 CHECK (yellow_cards BETWEEN 0 AND 2),
    red_cards                SMALLINT NOT NULL DEFAULT 0 CHECK (red_cards BETWEEN 0 AND 1),

    zone14_entries           SMALLINT NOT NULL DEFAULT 0,
    halfspace_entries        SMALLINT NOT NULL DEFAULT 0,
    long_balls_targeted      SMALLINT NOT NULL DEFAULT 0,

    team_possession_pct      NUMERIC(5,2),

    padj_tackles             NUMERIC(7,3),
    padj_interceptions       NUMERIC(7,3),
    padj_fouls               NUMERIC(7,3),
    dribbled_past_rate       NUMERIC(5,4),     -- NULL when undefined (no duels)

    source_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fixture_id, player_id),
    CONSTRAINT chk_possession_bounds        CHECK (team_possession_pct BETWEEN 0.00 AND 100.00),
    CONSTRAINT chk_minute_logic             CHECK (minute_off >= minute_on AND minutes_played <= 130),
    CONSTRAINT chk_tackles_won_le_attempted CHECK (tackles_won <= tackles_attempted),
    CONSTRAINT chk_duels_won_le_total       CHECK (duels_won <= duels_total)
);

-- Free in-page space so upserts that change no indexed column can be HOT updates.
ALTER TABLE player_match_stats SET (fillfactor = 90);

-- Confirmed team sheets: written directly by ConcurrentIngestionEngine._lineup_task (bypasses staging).
CREATE TABLE lineup_expectations (
    fixture_id        BIGINT       NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    player_id         INTEGER      NOT NULL REFERENCES players(player_id),
    p_start           NUMERIC(4,3) NOT NULL CHECK (p_start BETWEEN 0 AND 1),
    p_sub_appearance  NUMERIC(4,3) NOT NULL CHECK (p_sub_appearance BETWEEN 0 AND 1),
    exp_minutes_start NUMERIC(5,2) NOT NULL DEFAULT 85.0,
    exp_minutes_sub   NUMERIC(5,2) NOT NULL DEFAULT 20.0,
    source            VARCHAR(32)  NOT NULL,
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (fixture_id, player_id)
) WITH (fillfactor = 90);

-- ============================================================
-- Durable raw vendor payloads: the recovery source for UNLOGGED staging.
-- ============================================================
CREATE TABLE raw_payloads (
    payload_id         BIGSERIAL   PRIMARY KEY,
    source             VARCHAR(32) NOT NULL,
    vendor_fixture_uid VARCHAR(64) NOT NULL,
    payload            JSONB       NOT NULL,
    payload_sha256     CHAR(64)    NOT NULL UNIQUE,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Append-only landing zone (migration 002, verbatim). UNLOGGED: no WAL, emptied on crash recovery,
-- not replicated. No keys, constraints or indexes.
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
-- Sportsbook quotes (ARCHITECTURE-ESSENTIALS 3.3, verbatim). Writer: [N] OddsPoller.
-- ============================================================
CREATE TABLE odds_lines (
    line_id             BIGSERIAL PRIMARY KEY,
    fixture_id          BIGINT NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    sportsbook          VARCHAR(32) NOT NULL,
    market_type         VARCHAR(32) NOT NULL
                        CHECK (market_type IN ('tackles', 'fouls', 'fouls_drawn', 'yellow_card', 'red_card')),
    player_id           INTEGER REFERENCES players(player_id),
    line_value          NUMERIC(5,2) NOT NULL,
    over_price_decimal  NUMERIC(7,3),
    under_price_decimal NUMERIC(7,3),
    void_rule           VARCHAR(32) NOT NULL DEFAULT 'void_if_no_appearance'
                        CHECK (void_rule IN ('void_if_no_appearance', 'void_if_not_starting', 'no_void')),
    max_stake           NUMERIC(10,2),
    is_closing          BOOLEAN NOT NULL DEFAULT FALSE,
    captured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_some_price     CHECK (over_price_decimal IS NOT NULL OR under_price_decimal IS NOT NULL),
    -- A NULL side (yes-only card markets) makes the conjunction NULL, which a CHECK accepts.
    CONSTRAINT chk_price_realism  CHECK (over_price_decimal > 1.000 AND under_price_decimal > 1.000),
    CONSTRAINT chk_line_increment CHECK (line_value >= 0.00 AND (line_value * 2) = trunc(line_value * 2)),
    CONSTRAINT uq_odds_snapshot UNIQUE NULLS NOT DISTINCT
        (fixture_id, sportsbook, market_type, player_id, line_value, captured_at)
);

CREATE INDEX idx_odds_market_latest   ON odds_lines (fixture_id, market_type, player_id, captured_at DESC);
CREATE INDEX idx_odds_fixture_captured ON odds_lines (fixture_id, captured_at DESC);
CREATE INDEX idx_pms_player           ON player_match_stats (player_id);
CREATE INDEX idx_fixtures_league_kickoff ON fixtures (league_id, kickoff_at);

-- ============================================================
-- Model outputs and the paper ledger. Writers: [N] ProjectionRefresh, [N] ledger writer.
-- ============================================================
CREATE TABLE model_projections (
    projection_id     BIGSERIAL    PRIMARY KEY,
    fixture_id        BIGINT       NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    player_id         INTEGER      NOT NULL REFERENCES players(player_id),
    market_type       VARCHAR(32)  NOT NULL,
    gamma_shape       DOUBLE PRECISION NOT NULL,
    gamma_rate        DOUBLE PRECISION NOT NULL,
    p_start           NUMERIC(4,3) NOT NULL,
    p_sub_appearance  NUMERIC(4,3) NOT NULL,
    exp_minutes_start NUMERIC(5,2) NOT NULL,
    exp_minutes_sub   NUMERIC(5,2) NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE bets (
    bet_id          BIGSERIAL     PRIMARY KEY,
    projection_id   BIGINT        NOT NULL REFERENCES model_projections(projection_id),
    line_id         BIGINT        NOT NULL REFERENCES odds_lines(line_id),
    side            VARCHAR(8)    NOT NULL CHECK (side IN ('over', 'under')),
    decimal_odds    NUMERIC(7,3)  NOT NULL,
    fraction        NUMERIC(9,6)  NOT NULL,
    stake           NUMERIC(10,2) NOT NULL,
    is_paper        BOOLEAN       NOT NULL DEFAULT TRUE,
    closing_line_id BIGINT        REFERENCES odds_lines(line_id),
    clv             NUMERIC(8,5),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ============================================================
-- merge_staging_player_feed(): identical to migrations/006_player_attributes_by_kickoff.sql (AGENT M4, Z5).
-- Call: SELECT * FROM prop_alpha.merge_staging_player_feed();  -- claims at most 200 batches per call
-- ============================================================
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
               OR c.player_position IS NULL
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
        primary_position = EXCLUDED.primary_position,
        attributes_as_of = EXCLUDED.attributes_as_of,
        updated_at       = now()
    WHERE pl.attributes_as_of IS NULL OR EXCLUDED.attributes_as_of >= pl.attributes_as_of;

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

RESET search_path;
