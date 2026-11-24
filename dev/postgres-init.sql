-- dev/postgres-init.sql -- TaskFlow local smoke-test harness only.
-- Database-per-service: each service gets its own role and database and
-- cannot read another service's tables. In Kubernetes the same layout is
-- created by the Helm chart's init script with passwords from Secrets.
DO $$
DECLARE svc text;
BEGIN
  FOREACH svc IN ARRAY ARRAY['auth','users','lists','tasks','tags','comments',
                             'attachments','reminders','recurrence','search','audit']
  LOOP
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', svc, 'dev-' || svc);
  END LOOP;
END $$;
CREATE DATABASE auth OWNER auth;
CREATE DATABASE users OWNER users;
CREATE DATABASE lists OWNER lists;
CREATE DATABASE tasks OWNER tasks;
CREATE DATABASE tags OWNER tags;
CREATE DATABASE comments OWNER comments;
CREATE DATABASE attachments OWNER attachments;
CREATE DATABASE reminders OWNER reminders;
CREATE DATABASE recurrence OWNER recurrence;
CREATE DATABASE search OWNER search;
CREATE DATABASE audit OWNER audit;
