-- The model that last added to or edited a fragment, beside TouchedSessionId: an edit puts the editor's words under
-- the writer's ModelId (021), so a view a later model rewrote would read as the earlier model's. The audit log's
-- snapshot of each edit carries it, so `show` names the model behind every earlier text. One ADD COLUMN per
-- migration (see 019).

ALTER TABLE ContextFragments ADD COLUMN TouchedModelId TEXT NULL;
