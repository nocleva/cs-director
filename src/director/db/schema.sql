-- CS2 Director DuckDB şeması (CLAUDE.md bölüm 5)
-- Tüm tablolar IF NOT EXISTS ile oluşturulur; init tekrar çalıştırılabilir.

-- Oyuncular: FACEIT id birincil anahtar, steam id demo eşleştirmesi için
CREATE TABLE IF NOT EXISTS players (
    faceit_id   VARCHAR PRIMARY KEY,
    steam_id    VARCHAR,
    nickname    VARCHAR,
    elo         INTEGER,
    level       INTEGER,
    updated_at  TIMESTAMP DEFAULT current_timestamp
);

-- Maçlar: team_a = faction1, team_b = faction2
CREATE TABLE IF NOT EXISTS matches (
    match_id     VARCHAR PRIMARY KEY,
    map          VARCHAR,
    date         TIMESTAMP,              -- maç başlangıcı (UTC)
    team_a       VARCHAR,
    team_b       VARCHAR,
    score        VARCHAR,                -- "13-9" (team_a-team_b)
    score_a      INTEGER,
    score_b      INTEGER,
    winner       VARCHAR,                -- 'faction1' / 'faction2'
    competition  VARCHAR,
    demo_url     VARCHAR,
    demo_status  VARCHAR,                -- pending / missing / downloaded / processed
    updated_at   TIMESTAMP DEFAULT current_timestamp
);

-- Maç başı oyuncu skor tablosu (FACEIT stats + ileride demodan KAST/rating)
CREATE TABLE IF NOT EXISTS match_player_stats (
    match_id      VARCHAR,
    player_id     VARCHAR,               -- FACEIT player_id
    nickname      VARCHAR,
    faction       VARCHAR,               -- 'faction1' / 'faction2'
    team_name     VARCHAR,
    kills         INTEGER,
    deaths        INTEGER,
    assists       INTEGER,
    kd_ratio      DOUBLE,
    adr           DOUBLE,
    hs_pct        DOUBLE,
    mvps          INTEGER,
    triple_kills  INTEGER,
    quadro_kills  INTEGER,
    penta_kills   INTEGER,
    result        BOOLEAN,               -- maçı kazandı mı
    kast          DOUBLE,                -- FACEIT vermez; demo parse'tan dolacak
    rating        DOUBLE,                -- demo parse'tan dolacak
    PRIMARY KEY (match_id, player_id)
);

-- Round sonuçları (demodan)
CREATE TABLE IF NOT EXISTS rounds (
    match_id     VARCHAR,
    round_no     INTEGER,
    winner_side  VARCHAR,                -- 'CT' / 'T'
    reason       VARCHAR,
    ct_buy_type  VARCHAR,                -- eco / force / full ...
    t_buy_type   VARCHAR,
    bomb_site    VARCHAR,
    PRIMARY KEY (match_id, round_no)
);

-- Kill olayları (demodan)
CREATE TABLE IF NOT EXISTS kills (
    match_id        VARCHAR,
    round_no        INTEGER,
    tick            INTEGER,
    attacker        VARCHAR,             -- steam id
    victim          VARCHAR,
    weapon          VARCHAR,
    attacker_place  VARCHAR,
    victim_place    VARCHAR,
    is_opening      BOOLEAN,
    is_traded       BOOLEAN,
    time_in_round   DOUBLE               -- saniye
);

-- Utility kullanımı (demodan)
CREATE TABLE IF NOT EXISTS utility (
    match_id         VARCHAR,
    round_no         INTEGER,
    thrower          VARCHAR,
    type             VARCHAR,            -- smoke / flash / molly / he
    land_place       VARCHAR,
    flashed_enemies  INTEGER,
    damage           DOUBLE
);

-- Pozisyon örnekleri (her 2 saniyede bir, demodan)
CREATE TABLE IF NOT EXISTS positions_snapshot (
    match_id    VARCHAR,
    round_no    INTEGER,
    tick        INTEGER,
    player      VARCHAR,
    x           DOUBLE,
    y           DOUBLE,
    z           DOUBLE,
    place_name  VARCHAR
);
