-- The host whose permanent Mobile Connect link signed in a mobile session, or NULL for a one-time code.
-- A nullable column only: the deployed Worker inserts sessions without it.
-- Revoking the link of a host signs out these phones. The host, not the link row, identifies them,
-- because a link row goes when the desktop session that made it is deleted.
ALTER TABLE mobile_auth_sessions ADD COLUMN connect_link_host_id TEXT;

CREATE INDEX mobile_auth_sessions_connect_link_host ON mobile_auth_sessions(user_id, connect_link_host_id);
