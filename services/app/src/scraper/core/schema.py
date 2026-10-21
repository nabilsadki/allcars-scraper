"""Schema MySQL du projet.

Table `ads` : une ligne par annonce. Colonnes alignees sur l'ancien schema
`ad` (projet voitures-maroc-sy / selenium) pour facilitate la migration, plus
quelques colonnes propres au nouveau scraper (list_id, raw_json, scraped_at,
details_fetched).

Table `ad_images` : images (lien standard + lien full_hd).
Table `scrape_runs` : historique des executions (pour le pilotage).
"""

SCHEMA = """
CREATE DATABASE IF NOT EXISTS avito CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE avito;

CREATE TABLE IF NOT EXISTS ads (
    ad_id           BIGINT       NOT NULL,
    list_id         BIGINT       NULL,
    site            VARCHAR(20)  NOT NULL DEFAULT 'avito',
    category        VARCHAR(128) NULL,
    title           VARCHAR(512) NULL,
    price           INT          NULL,
    publish_date    DATETIME     NULL,
    city            VARCHAR(128) NULL,
    area            VARCHAR(128) NULL,
    seller          VARCHAR(255) NULL,
    seller_type     VARCHAR(64)  NULL,
    seller_id       VARCHAR(64)  NULL,
    phone           VARCHAR(64)  NULL,
    link            VARCHAR(1024) NULL,
    description     TEXT         NULL,
    fuel            VARCHAR(64)  NULL,
    gear            VARCHAR(64)  NULL,
    cv              INT          NULL,
    km              INT          NULL,
    year            INT          NULL,
    brand           VARCHAR(128) NULL,
    model           VARCHAR(128) NULL,
    color           VARCHAR(64)  NULL,
    car_body        VARCHAR(64)  NULL,
    doors           INT          NULL,
    fiscal_power    VARCHAR(64)  NULL,
    sector          VARCHAR(128) NULL,
    origin          VARCHAR(64)  NULL,
    first_hand      VARCHAR(16)  NULL,
    cond            VARCHAR(64)  NULL,
    options_json    JSON         NULL,
    raw_json        JSON         NULL,
    nb_images       INT          DEFAULT 0,
    is_premium      TINYINT(1)   DEFAULT 0,
    is_urgent       TINYINT(1)   DEFAULT 0,
    is_hot_deal     TINYINT(1)   DEFAULT 0,
    details_fetched TINYINT(1)   DEFAULT 0,
    published       TINYINT(1)   DEFAULT 0,
    is_active       TINYINT(1)   DEFAULT 1,
    version         INT          NOT NULL DEFAULT 1,
    content_hash    CHAR(64)     NULL,
    seen_count      INT          DEFAULT 0,
    first_seen_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    -- scoring (portage de l'ancien projet)
    note            DECIMAL(4,2) NULL,
    note_year       DECIMAL(4,2) NULL,
    note_km         DECIMAL(4,2) NULL,
    note_ratio      DECIMAL(4,2) NULL,
    note_cv         DECIMAL(4,2) NULL,
    note_sunroof    DECIMAL(4,2) NULL,
    note_fuel       DECIMAL(4,2) NULL,
    note_gear       DECIMAL(4,2) NULL,
    note_specs      DECIMAL(4,2) NULL,
    km_year         INT          NULL,
    -- scoring v2 (aide aux acheteurs) ; 'note' = legacy
    note_etat       DECIMAL(4,2) NULL,
    note_achat      DECIMAL(4,2) NULL,
    note_global     DECIMAL(4,2) NULL,
    note_confiance  DECIMAL(4,2) NULL,
    created         DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated         DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    scraped_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ad_id),
    KEY idx_site_pub     (site, published),
    KEY idx_price        (price),
    KEY idx_year         (year),
    KEY idx_city         (city),
    KEY idx_brand        (brand),
    KEY idx_fuel         (fuel),
    KEY idx_publish_date (publish_date),
    KEY idx_scraped_at   (scraped_at),
    KEY idx_hash         (content_hash),
    KEY idx_active       (is_active),
    KEY idx_note         (note),
    KEY idx_note_global  (note_global)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ad_images (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    site          VARCHAR(32)  NOT NULL DEFAULT 'avito',
    ad_id         BIGINT       NOT NULL,
    link          VARCHAR(1024) NOT NULL,
    link_full     VARCHAR(1024) NULL,
    is_main       TINYINT(1)   DEFAULT 0,
    path_local    VARCHAR(512) NULL,
    variant       VARCHAR(16)  NULL,
    bytes         INT          NULL,
    sha1          CHAR(40)     NULL,
    downloaded_at DATETIME     NULL,
    created       DATETIME     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_ad_link (site, ad_id, link(255)),
    KEY idx_ad (site, ad_id),
    KEY idx_local (path_local(120))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Historique : une ligne par version de l'annonce
CREATE TABLE IF NOT EXISTS ad_versions (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    site            VARCHAR(32) NOT NULL DEFAULT 'avito',
    ad_id           BIGINT      NOT NULL,
    version         INT         NOT NULL,
    change_type     VARCHAR(32) NOT NULL,
    diff_json       JSON        NULL,
    snapshot_json   JSON        NULL,
    content_hash    CHAR(64)    NULL,
    details_fetched TINYINT(1)  DEFAULT 0,
    created         DATETIME    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_ad_version (site, ad_id, version),
    KEY idx_ad (site, ad_id),
    KEY idx_type (change_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS scrape_runs (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    started_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at   DATETIME NULL,
    category      VARCHAR(255) NULL,
    mode          VARCHAR(64)  NULL,
    ads_listed    INT DEFAULT 0,
    ads_details   INT DEFAULT 0,
    ads_inserted  INT DEFAULT 0,
    ads_updated   INT DEFAULT 0,
    ads_unchanged INT DEFAULT 0,
    ads_disappeared INT DEFAULT 0,
    http_requests INT DEFAULT 0,
    blocked       INT DEFAULT 0,
    status        VARCHAR(32)  NULL,
    error         TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""
