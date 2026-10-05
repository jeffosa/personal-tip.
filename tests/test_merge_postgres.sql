-- Behavioural test of prop_alpha.merge_staging_player_feed() against a real PostgreSQL server.
-- Run by ci/bootstrap.sh stage 6 on a disposable *_ci database. Everything happens in one transaction
-- that is rolled back, so the database is left as it was. Any failed check raises and stops psql.
-- Run with: psql -X -v ON_ERROR_STOP=1 -f tests/test_merge_postgres.sql

BEGIN;

CREATE FUNCTION pg_temp.expect(ok boolean, what text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF ok IS NOT TRUE THEN
        RAISE EXCEPTION 'merge check failed: %', what;
    END IF;
END
$$;

CREATE FUNCTION pg_temp.stage(
    p_batch uuid, p_fixture text, p_home text, p_away text, p_kickoff timestamptz,
    p_player text, p_team text, p_tackles_won integer DEFAULT 1, p_position text DEFAULT 'DM'
) RETURNS void LANGUAGE sql AS $$
    INSERT INTO prop_alpha.staging_player_feed (
        batch_id, vendor_fixture_uid, home_team_uid, away_team_uid, kickoff_at, fixture_status,
        vendor_player_uid, player_name, player_position, team_uid, is_home, started,
        minute_on, minute_off, minutes_played,
        tackles_attempted, tackles_won, fouls_committed, fouls_drawn, yellow_cards, red_cards
    ) VALUES (
        p_batch, p_fixture, p_home, p_away, p_kickoff, 'final',
        p_player, 'Player ' || p_player, p_position, p_team, p_team = p_home, TRUE,
        0, 90, 90,
        2, p_tackles_won, 1, 1, 0, 0
    );
$$;

CREATE FUNCTION pg_temp.team_of(p_player text) RETURNS text LANGUAGE sql AS $$
    SELECT t.vendor_team_uid
    FROM prop_alpha.players p
    JOIN prop_alpha.teams t ON t.team_id = p.current_team_id
    WHERE p.vendor_player_uid = p_player;
$$;

INSERT INTO prop_alpha.leagues (name, country, tier) VALUES ('CI League', 'XX', 4);
INSERT INTO prop_alpha.teams (league_id, name, vendor_team_uid)
SELECT l.league_id, v.name, v.uid
FROM prop_alpha.leagues l, (VALUES ('Club A', 'tA'), ('Club B', 'tB'), ('Club C', 'tC')) AS v (name, uid)
WHERE l.name = 'CI League';

-- 1. First appearance for Club A, plus one row a core CHECK would refuse (tackles_won > tackles_attempted).
SELECT pg_temp.stage('00000000-0000-0000-0000-000000000001', 'f1', 'tA', 'tC', '2026-08-01T15:00Z', 'p1', 'tA');
SELECT pg_temp.stage('00000000-0000-0000-0000-000000000001', 'f1', 'tA', 'tC', '2026-08-01T15:00Z', 'bad', 'tC', 5);
SELECT pg_temp.expect(
    fixtures_merged = 1 AND player_rows_merged = 1 AND rows_rejected = 1,
    format('first merge counts: %s fixtures, %s player rows, %s rejected',
           fixtures_merged, player_rows_merged, rows_rejected)
) FROM prop_alpha.merge_staging_player_feed();
SELECT pg_temp.expect(pg_temp.team_of('p1') = 'tA', 'a new player gets the team of their first fixture');
SELECT pg_temp.expect(
    (SELECT count(*) FROM prop_alpha.staging_rejects WHERE vendor_player_uid = 'bad') = 1,
    'the invalid row is recorded in staging_rejects'
);

-- 2. Transfer: the player's next match is for Club B.
SELECT pg_temp.stage('00000000-0000-0000-0000-000000000002', 'f2', 'tB', 'tC', '2026-09-01T15:00Z', 'p1', 'tB');
SELECT pg_temp.expect(player_rows_merged = 1, 'transfer merge counts') FROM prop_alpha.merge_staging_player_feed();
SELECT pg_temp.expect(pg_temp.team_of('p1') = 'tB', 'a transferred player moves to their new team');

-- 3. Backfill: an older Club A match staged last must not move the player back.
SELECT pg_temp.stage('00000000-0000-0000-0000-000000000003', 'f0', 'tA', 'tC', '2026-07-01T15:00Z', 'p1', 'tA');
SELECT pg_temp.expect(player_rows_merged = 1, 'backfill merge counts') FROM prop_alpha.merge_staging_player_feed();
SELECT pg_temp.expect(pg_temp.team_of('p1') = 'tB', 'a backfilled older fixture does not move the player back');

SELECT pg_temp.expect(
    (SELECT count(*) FROM prop_alpha.player_match_stats s
     JOIN prop_alpha.players p USING (player_id) WHERE p.vendor_player_uid = 'p1') = 3,
    'all three appearances are kept'
);
SELECT pg_temp.expect(NOT EXISTS (SELECT 1 FROM prop_alpha.staging_player_feed), 'staging is drained');

-- 4. Name and position follow the latest match by kickoff (migration 006), not the last row staged.
CREATE FUNCTION pg_temp.position_of(p_player text) RETURNS text LANGUAGE sql AS $$
    SELECT primary_position FROM prop_alpha.players WHERE vendor_player_uid = p_player;
$$;
SELECT pg_temp.stage('00000000-0000-0000-0000-000000000004', 'f3', 'tB', 'tC', '2026-10-01T15:00Z', 'p1', 'tB', 1, 'AM');
SELECT pg_temp.expect(player_rows_merged = 1, 'new-position merge counts') FROM prop_alpha.merge_staging_player_feed();
SELECT pg_temp.expect(pg_temp.position_of('p1') = 'AM', 'a newer match sets the position');
SELECT pg_temp.stage('00000000-0000-0000-0000-000000000005', 'f00', 'tA', 'tC', '2026-06-01T15:00Z', 'p1', 'tA', 1, 'GK');
SELECT pg_temp.expect(player_rows_merged = 1, 'old-match merge counts') FROM prop_alpha.merge_staging_player_feed();
SELECT pg_temp.expect(pg_temp.position_of('p1') = 'AM', 'a backfilled older match does not change the position');
-- Re-ingesting two matches in one batch, the older staged last: the newer match still wins.
SELECT pg_temp.stage('00000000-0000-0000-0000-000000000006', 'f3', 'tB', 'tC', '2026-10-01T15:00Z', 'p1', 'tB', 1, 'AM');
SELECT pg_temp.stage('00000000-0000-0000-0000-000000000006', 'f2', 'tB', 'tC', '2026-09-01T15:00Z', 'p1', 'tB', 1, 'DM');
SELECT pg_temp.expect(player_rows_merged = 2, 're-ingest merge counts') FROM prop_alpha.merge_staging_player_feed();
SELECT pg_temp.expect(pg_temp.position_of('p1') = 'AM', 'staging order does not decide the position');
SELECT pg_temp.expect(
    (SELECT attributes_as_of FROM prop_alpha.players WHERE vendor_player_uid = 'p1') = '2026-10-01T15:00Z',
    'attributes_as_of is the kickoff of the match that set them'
);

-- 5. Each match keeps the position staged with it (migration 007), whatever the player's latest position is.
CREATE FUNCTION pg_temp.match_position_of(p_fixture text, p_player text) RETURNS text LANGUAGE sql AS $$
    SELECT s.match_position
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.players p USING (player_id)
    WHERE f.vendor_fixture_uid = p_fixture AND p.vendor_player_uid = p_player;
$$;
SELECT pg_temp.expect(pg_temp.match_position_of('f00', 'p1') = 'GK', 'the old match keeps its own position');
SELECT pg_temp.expect(pg_temp.match_position_of('f2', 'p1') = 'DM', 'a re-ingested match keeps its own position');
SELECT pg_temp.expect(pg_temp.match_position_of('f3', 'p1') = 'AM', 'the latest match keeps its own position');

-- 6. A club-match in which a player without an identity played is recorded (migration 008): a starter with no
-- player_id counts, a substitute with no player_id who did not play does not, and an identified row never counts.
INSERT INTO prop_alpha.raw_payloads (source, vendor_fixture_uid, payload, payload_sha256)
VALUES ('ci', 'f3', jsonb_build_object('vendor_response', jsonb_build_object('lineups', jsonb_build_array(
        jsonb_build_object('team_id', 'tB', 'type_id', 11, 'player_id', NULL, 'player_name', 'No Id Starter'),
        jsonb_build_object('team_id', 'tC', 'type_id', 12, 'player_id', NULL, 'player_name', 'Unused Sub',
                           'details', jsonb_build_array(jsonb_build_object(
                               'type', jsonb_build_object('developer_name', 'MINUTES_PLAYED'),
                               'data', jsonb_build_object('value', 0)))),
        jsonb_build_object('team_id', 'tC', 'type_id', 11, 'player_id', 99, 'player_name', 'Known')))),
        repeat('a', 64));
SELECT pg_temp.expect(prop_alpha.refresh_incomplete_team_matches() = 1, 'one incomplete club-match is found');
SELECT pg_temp.expect(
    (SELECT t.vendor_team_uid FROM prop_alpha.incomplete_team_matches i JOIN prop_alpha.teams t USING (team_id)) = 'tB',
    'the club with the unidentified starter is the one recorded'
);
SELECT pg_temp.expect(prop_alpha.refresh_incomplete_team_matches() = 1, 'refreshing again rewrites the same row');
SELECT pg_temp.expect((SELECT count(*) FROM prop_alpha.incomplete_team_matches) = 1, 'no duplicate after refresh');

ROLLBACK;
