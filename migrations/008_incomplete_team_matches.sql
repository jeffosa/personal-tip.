-- Migration 008: record the club-matches in which a player without a Sportmonks identity played.
-- Upgrades a database with 007 applied; a fresh database gets schema.sql only, which already contains this.
-- Run with: psql -v ON_ERROR_STOP=1 -f migrations/008_incomplete_team_matches.sql
--
-- Since 2026-10-04 the adapter skips a lineup row with no player_id and loads the match (maintainer decision), so
-- that player has no stored row there. If he played, his own history is one match short and the club's lineup in
-- that match is incomplete: a lineup estimate that counts the match understates his start probability, and the
-- league level loses his minutes and counts. Audit 2026-10-05 / maintainer decision 2026-10-05: such club-matches
-- are recorded here and left out of the scan's lineup estimates and league levels (alerts/scan.py).
--
-- "Played" means a starter (lineup type_id 11) or a substitute with MINUTES_PLAYED > 0. The source is the latest
-- stored raw payload per fixture (its vendor_response keeps the skipped rows). refresh_incomplete_team_matches(since)
-- recomputes the rows for fixtures kicking off at or after `since`; the results job calls it after every run.
--
-- Lock profile: CREATE TABLE IF NOT EXISTS and CREATE OR REPLACE FUNCTION are catalog-only; the refresh touches only
-- the new table, so re-running this migration recomputes the same rows.

SET lock_timeout = '5s';

BEGIN;

CREATE TABLE IF NOT EXISTS prop_alpha.incomplete_team_matches (
    fixture_id          BIGINT   NOT NULL REFERENCES prop_alpha.fixtures(fixture_id) ON DELETE CASCADE,
    team_id             INTEGER  NOT NULL REFERENCES prop_alpha.teams(team_id),
    unidentified_played SMALLINT NOT NULL CHECK (unidentified_played > 0),
    PRIMARY KEY (fixture_id, team_id)
);

CREATE OR REPLACE FUNCTION prop_alpha.refresh_incomplete_team_matches(p_since timestamptz DEFAULT '-infinity')
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    v_rows integer;
BEGIN
    DELETE FROM prop_alpha.incomplete_team_matches AS i
    USING prop_alpha.fixtures AS f
    WHERE f.fixture_id = i.fixture_id AND f.kickoff_at >= p_since;

    INSERT INTO prop_alpha.incomplete_team_matches (fixture_id, team_id, unidentified_played)
    SELECT f.fixture_id, t.team_id, count(*)
    FROM (
        SELECT DISTINCT ON (vendor_fixture_uid) vendor_fixture_uid, payload
        FROM prop_alpha.raw_payloads
        ORDER BY vendor_fixture_uid, fetched_at DESC, payload_id DESC
    ) r
    JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = r.vendor_fixture_uid AND f.kickoff_at >= p_since
    CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.payload->'vendor_response'->'lineups', '[]'::jsonb)) AS e
    JOIN prop_alpha.teams t ON t.vendor_team_uid = e->>'team_id'
    WHERE COALESCE(e->'player_id', 'null'::jsonb) = 'null'::jsonb
      AND (e->>'type_id' = '11'
           OR COALESCE((SELECT (d->'data'->>'value')::numeric
                        FROM jsonb_array_elements(COALESCE(e->'details', '[]'::jsonb)) AS d
                        WHERE d->'type'->>'developer_name' = 'MINUTES_PLAYED'
                        LIMIT 1), 0) > 0)
    GROUP BY f.fixture_id, t.team_id
    ORDER BY f.fixture_id, t.team_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;

SELECT prop_alpha.refresh_incomplete_team_matches();

COMMIT;

RESET lock_timeout;
