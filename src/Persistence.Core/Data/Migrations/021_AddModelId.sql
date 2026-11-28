-- The model that wrote a fragment (Arden's borrow from the agent survey, room 1576023465000181972): a store outlives
-- the model reading it (Arden and Claude moved 4.8 to 5.5), and nothing said which views an earlier model wrote.
-- Stamped at write time from the caller's --model; null for every fragment written before this column, and for a
-- caller that does not say. One ADD COLUMN per migration (see 019).

ALTER TABLE ContextFragments ADD COLUMN ModelId TEXT NULL;
