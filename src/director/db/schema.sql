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
    demo_status  VARCHAR,                -- pending / missing / processed / mismatch
    parser_version INTEGER,              -- demoyu işleyen parser sürümü (hangi alanlar var)
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

-- ===================== Demodan gelen tablolar (şema v2) =====================
-- Kolonlar awpy 3.x'in gerçek çıktısına göre. Steam id'ler metin (players.steam_id ile eşleşir).
-- Taraf: 'CT' / 'T'. Zaman: freeze time bitişinden itibaren saniye.

-- Round sonuçları ve ekonomi
CREATE TABLE IF NOT EXISTS rounds (
    match_id         VARCHAR,
    round_no         INTEGER,
    winner_side      VARCHAR,            -- 'CT' / 'T'
    reason           VARCHAR,            -- ct_win / terrorists_win / bomb_defused / target_saved ...
    freeze_end_tick  BIGINT,
    end_tick         BIGINT,
    ct_buy_type      VARCHAR,            -- pistol / eco / force / full
    ct_equipment     INTEGER,            -- takımın freeze sonundaki toplam ekipman değeri
    t_buy_type       VARCHAR,
    t_equipment      INTEGER,
    bomb_site        VARCHAR,            -- plant edilen site (A / B), yoksa NULL
    plant_tick       BIGINT,
    ct_faction       VARCHAR,            -- bu round CT olan takım: 'faction1' / 'faction2'
    PRIMARY KEY (match_id, round_no)
);

-- Kill olayları (ısınma kill'leri hariç)
CREATE TABLE IF NOT EXISTS kills (
    match_id        VARCHAR,
    round_no        INTEGER,
    tick            BIGINT,
    time_in_round   DOUBLE,
    attacker        VARCHAR,             -- steam id (dünya / düşme ölümünde NULL)
    attacker_name   VARCHAR,
    attacker_side   VARCHAR,
    victim          VARCHAR,
    victim_name     VARCHAR,
    victim_side     VARCHAR,
    assister        VARCHAR,
    weapon          VARCHAR,
    headshot        BOOLEAN,
    flash_assist    BOOLEAN,
    thru_smoke      BOOLEAN,
    attacker_place  VARCHAR,             -- ör. LongA, MidDoors
    victim_place    VARCHAR,
    attacker_x      DOUBLE,
    attacker_y      DOUBLE,
    attacker_z      DOUBLE,
    victim_x        DOUBLE,
    victim_y        DOUBLE,
    victim_z        DOUBLE,
    is_opening      BOOLEAN,             -- round'un ilk kill'i
    penetrated      INTEGER,             -- delinen yüzey sayısı (>0 = wallbang); parser v2+
    noscope         BOOLEAN,
    is_trade_kill   BOOLEAN,             -- bu kill bir takım arkadaşının intikamı
    is_traded       BOOLEAN              -- bu ölüm kısa sürede trade edildi
);

-- Utility kullanımı: kim, nereye, ne etki
CREATE TABLE IF NOT EXISTS utility (
    match_id           VARCHAR,
    round_no           INTEGER,
    tick               BIGINT,           -- patlama / smoke açılma / yangın başlama anı
    time_in_round      DOUBLE,
    thrower            VARCHAR,
    thrower_name       VARCHAR,
    thrower_side       VARCHAR,
    type               VARCHAR,          -- smoke / flash / he / molly
    x                  DOUBLE,
    y                  DOUBLE,
    z                  DOUBLE,
    land_place         VARCHAR,          -- düştüğü bölge (en yakın bilinen konumdan)
    flashed_enemies    INTEGER,          -- sadece flash
    flashed_teammates  INTEGER,          -- sadece flash
    blind_duration     DOUBLE,           -- rakiplerin toplam kör kaldığı saniye
    damage             DOUBLE            -- HE / molotof ile rakibe verilen hasar
);

-- Pozisyon örnekleri: canlı round içinde, hayattaki oyuncular, 2 saniyede bir
CREATE TABLE IF NOT EXISTS positions_snapshot (
    match_id         VARCHAR,
    round_no         INTEGER,
    tick             BIGINT,
    time_in_round    DOUBLE,
    player           VARCHAR,            -- steam id
    player_name      VARCHAR,
    side             VARCHAR,
    x                DOUBLE,
    y                DOUBLE,
    z                DOUBLE,
    place_name       VARCHAR,
    health           INTEGER,
    money            INTEGER,
    equipment_value  INTEGER,
    active_weapon    VARCHAR
);

-- awpy'nin maç başı oyuncu istatistikleri (KAST, opening, trade, clutch, utility)
CREATE TABLE IF NOT EXISTS demo_player_stats (
    match_id              VARCHAR,
    steam_id              VARCHAR,
    name                  VARCHAR,
    rounds_played         INTEGER,
    kills                 INTEGER,
    deaths                INTEGER,
    assists               INTEGER,
    flash_assists         INTEGER,
    headshot_kills        INTEGER,
    adr                   DOUBLE,
    kast                  DOUBLE,
    opening_kills         INTEGER,
    opening_deaths        INTEGER,
    traded_deaths         INTEGER,
    multikill_2k          INTEGER,
    multikill_3k          INTEGER,
    multikill_4k          INTEGER,
    multikill_5k          INTEGER,
    clutches_played       INTEGER,
    clutches_won          INTEGER,
    utility_damage        INTEGER,
    flashes_thrown        INTEGER,
    enemies_flashed       INTEGER,
    flash_duration_dealt  DOUBLE,
    PRIMARY KEY (match_id, steam_id)
);

-- Her hasar olayı (parser v2+): kill olmayan vuruşlar, duvar/kapı arkası hasar dahil
CREATE TABLE IF NOT EXISTS damages (
    match_id        VARCHAR,
    round_no        INTEGER,
    tick            BIGINT,
    time_in_round   DOUBLE,
    attacker        VARCHAR,
    attacker_side   VARCHAR,
    victim          VARCHAR,
    victim_side     VARCHAR,
    weapon          VARCHAR,
    dmg_health      INTEGER,
    dmg_armor       INTEGER,
    hitgroup        VARCHAR,
    attacker_place  VARCHAR,
    victim_place    VARCHAR,
    through_wall    BOOLEAN              -- göz → gövde çizgisi harita geometrisiyle kesiliyor
);
