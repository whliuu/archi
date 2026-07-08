-- ---------------------------------------------------------------------------
-- Migration: reassign shared-browser conversations to a real user
-- ---------------------------------------------------------------------------
-- Context: before the auth fixes, basic-auth logins never set user_id, so every
-- conversation was scoped only by the *browser-generated* client_id (shared by
-- all accounts using the same browser). This made all chats visible to everyone.
--
-- This script moves those orphaned rows (user_id IS NULL, client_id set) to the
-- admin user and nulls client_id, so the `user_id = %s OR client_id = %s`
-- list/get/delete queries no longer leak them across logged-in users.
--
-- Run inside the postgres container, e.g.:
--   podman exec -i postgres-mu2e-ops \
--     psql -U archi -d archi-db < configs/mu2e/migrate_orphaned_conversations.sql
--
-- SAFETY: wrapped in a transaction. Review the SELECT output first; if it does
-- not look right, the whole thing can be aborted with ROLLBACK instead of COMMIT.
-- ---------------------------------------------------------------------------

BEGIN;

-- Target user_id. 'basic:admin' matches the id written by basic-auth login for
-- the admin account. Change this if you want the chats assigned elsewhere.
\set target_user_id '''basic:admin'''

-- 1) DRY RUN: how many orphaned conversations exist, grouped by client_id.
SELECT
    client_id,
    COUNT(*)              AS conversations,
    MIN(created_at)       AS earliest,
    MAX(last_message_at)  AS latest
FROM conversation_metadata
WHERE user_id IS NULL
  AND client_id IS NOT NULL
GROUP BY client_id
ORDER BY conversations DESC;

-- 2) Ensure the target user row exists (FK: conversation_metadata.user_id ->
--    users.id). Basic-auth login now upserts this automatically, but create it
--    here too so the migration is self-contained even before the next login.
INSERT INTO users (id, auth_provider, display_name, email)
VALUES (:target_user_id, 'basic', 'admin', 'admin')
ON CONFLICT (id) DO NOTHING;

-- 3) Reassign the orphaned conversations to the target user and clear client_id
--    so the shared-browser fallback can no longer surface them to other users.
UPDATE conversation_metadata
SET user_id = :target_user_id,
    client_id = NULL
WHERE user_id IS NULL
  AND client_id IS NOT NULL;

-- 4) Verify the result before committing.
SELECT
    COUNT(*) FILTER (WHERE user_id = :target_user_id) AS now_owned_by_target,
    COUNT(*) FILTER (WHERE user_id IS NULL AND client_id IS NOT NULL) AS still_orphaned
FROM conversation_metadata;

-- If the numbers look right, COMMIT. To back out instead, replace with ROLLBACK.
COMMIT;
