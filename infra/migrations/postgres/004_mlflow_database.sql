-- Отдельная БД для MLflow backend-store (D1 роадмапа: прод-режим вместо
-- sqlite). Идемпотентно: CREATE DATABASE нельзя выполнять в транзакции и
-- у него нет IF NOT EXISTS — используется psql-трюк с \gexec.
SELECT 'CREATE DATABASE mlflow OWNER dota'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow')
\gexec
