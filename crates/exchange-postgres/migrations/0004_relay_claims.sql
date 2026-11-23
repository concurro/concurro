ALTER TABLE relay_claims ADD COLUMN kid text;
ALTER TABLE relay_claims ADD COLUMN compact text;
ALTER TABLE relay_claims ADD COLUMN expires timestamptz;
ALTER TABLE relay_claims DROP CONSTRAINT relay_claims_release_status_check;
ALTER TABLE relay_claims ADD CONSTRAINT relay_claims_release_status_check
    CHECK (release_status IN ('active','pending','acknowledged'));
CREATE UNIQUE INDEX relay_claims_one_per_session ON relay_claims(session_id);
