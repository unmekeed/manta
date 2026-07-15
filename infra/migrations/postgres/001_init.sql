-- Миграция 001: базовая реляционная схема (Гл. 4.2 спецификации).
BEGIN;

CREATE TYPE match_tier AS ENUM ('Pub', 'Premium', 'Professional', 'Tournament');
CREATE TYPE lane_position AS ENUM ('Safe_Safe', 'Safe_Mid', 'Mid', 'Off_Safe', 'Off_Mid', 'Roaming');
CREATE TYPE subscription_status AS ENUM ('active', 'past_due', 'canceled', 'trialing');

CREATE TABLE Players (
    player_id      BIGINT PRIMARY KEY,
    steam_id_64    VARCHAR(20) UNIQUE NOT NULL,
    nickname       VARCHAR(100) NOT NULL,
    avatar_url     TEXT,
    registered_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE Accounts (
    account_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id       BIGINT REFERENCES Players(player_id) ON DELETE CASCADE,
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    current_mmr     INT DEFAULT 0,
    behavior_score  INT DEFAULT 10000,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE Tournaments (
    tournament_id  INT PRIMARY KEY,
    name           VARCHAR(200) NOT NULL,
    operator       VARCHAR(50),
    prize_pool_usd BIGINT,
    start_date     DATE,
    end_date       DATE
);

CREATE TABLE Matches (
    match_id          BIGINT PRIMARY KEY,
    tournament_id     INT REFERENCES Tournaments(tournament_id),
    duration_seconds  INT NOT NULL,
    radiant_win       BOOLEAN NOT NULL,
    game_mode         INT NOT NULL,
    lobby_type        INT NOT NULL,
    start_time        TIMESTAMP WITH TIME ZONE NOT NULL,
    tier              match_tier DEFAULT 'Pub',
    cluster_id        INT NOT NULL,
    patch_version     VARCHAR(16)
);

CREATE TABLE MatchPlayers (
    match_id      BIGINT REFERENCES Matches(match_id) ON DELETE CASCADE,
    player_id     BIGINT REFERENCES Players(player_id) ON DELETE RESTRICT,
    hero_id       INT NOT NULL,
    player_slot   INT NOT NULL,
    kills         INT DEFAULT 0,
    deaths        INT DEFAULT 0,
    assists       INT DEFAULT 0,
    gold_per_min  INT NOT NULL,
    xp_per_min    INT NOT NULL,
    lane          lane_position,
    PRIMARY KEY (match_id, player_id)
);

CREATE TABLE Subscriptions (
    subscription_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES Accounts(account_id) ON DELETE CASCADE,
    status          subscription_status NOT NULL DEFAULT 'trialing',
    plan_code       VARCHAR(50) NOT NULL,
    started_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    renews_at       TIMESTAMP WITH TIME ZONE
);

CREATE TABLE AnalysisJobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID REFERENCES Accounts(account_id),
    match_id        BIGINT,
    status          VARCHAR(20) NOT NULL DEFAULT 'queued',
    replay_url      TEXT,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at    TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_matches_start_time ON Matches (start_time DESC);
CREATE INDEX idx_matches_tier ON Matches (tier);
CREATE INDEX idx_matchplayers_hero ON MatchPlayers (hero_id);
CREATE INDEX idx_jobs_account_status ON AnalysisJobs (account_id, status);

COMMIT;
