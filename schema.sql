CREATE TABLE IF NOT EXISTS projects (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rooms (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    floor_plan_coordinates JSON,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS file_assets (
    id VARCHAR(36) PRIMARY KEY,
    room_id VARCHAR(36) NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    media_type VARCHAR(30) NOT NULL,
    capture_date DATE NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    bucket_name VARCHAR(255) NOT NULL,
    object_name VARCHAR(500) NOT NULL,
    thumbnail_bucket_name VARCHAR(255),
    thumbnail_object_name VARCHAR(500),
    content_type VARCHAR(100),
    file_size INTEGER,
    metadata_json JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reports (
    id VARCHAR(36) PRIMARY KEY,
    file_id VARCHAR(36) NOT NULL REFERENCES file_assets(id) ON DELETE CASCADE,
    ai_description TEXT,
    manual_observations TEXT,
    flags JSON,
    screenshots JSON,
    pdf_bucket_name VARCHAR(255),
    pdf_object_name VARCHAR(500),
    created_by VARCHAR(255),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS comparison_drafts (
    id VARCHAR(36) PRIMARY KEY,
    file_id VARCHAR(36) NOT NULL REFERENCES file_assets(id) ON DELETE CASCADE,
    manual_observations TEXT,
    flags JSON,
    state_json JSON,
    pdf_bucket_name VARCHAR(255) NOT NULL,
    pdf_object_name VARCHAR(500) NOT NULL,
    created_by VARCHAR(255),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS annotations (
    id VARCHAR(36) PRIMARY KEY,
    file_id VARCHAR(36) NOT NULL REFERENCES file_assets(id) ON DELETE CASCADE,
    annotation_type VARCHAR(50) NOT NULL,
    data JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_rooms_slug ON rooms(slug);
CREATE INDEX IF NOT EXISTS ix_file_assets_room_id ON file_assets(room_id);
CREATE INDEX IF NOT EXISTS ix_file_assets_capture_date ON file_assets(capture_date);
CREATE INDEX IF NOT EXISTS ix_file_assets_media_type ON file_assets(media_type);
CREATE INDEX IF NOT EXISTS ix_comparison_drafts_created_by ON comparison_drafts(created_by);
