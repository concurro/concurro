ALTER TABLE tasks.standalone_review_tasks
    ADD COLUMN view_revision bigint NOT NULL DEFAULT 1;

CREATE FUNCTION tasks.bump_view_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.view_revision := OLD.view_revision + 1;
    RETURN NEW;
END
$$;

CREATE TRIGGER bump_standalone_view_revision
BEFORE UPDATE ON tasks.standalone_review_tasks
FOR EACH ROW EXECUTE FUNCTION tasks.bump_view_revision();

CREATE FUNCTION tasks.refresh_standalone_round_view() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE tasks.standalone_review_tasks SET view_revision=view_revision
    WHERE id=NEW.task_id;
    RETURN NULL;
END
$$;

CREATE TRIGGER refresh_standalone_round_view
AFTER INSERT OR UPDATE ON tasks.standalone_review_rounds
FOR EACH ROW EXECUTE FUNCTION tasks.refresh_standalone_round_view();

ALTER TABLE tasks.report_artifact_writes
    ADD COLUMN view_revision bigint NOT NULL DEFAULT 1;

CREATE TRIGGER bump_report_view_revision
BEFORE UPDATE ON tasks.report_artifact_writes
FOR EACH ROW EXECUTE FUNCTION tasks.bump_view_revision();
