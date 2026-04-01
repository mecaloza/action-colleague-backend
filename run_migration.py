#!/usr/bin/env python3
"""
One-off migration script: Make user_id nullable in user_videos table
"""
import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    conn.execute(text("ALTER TABLE user_videos ALTER COLUMN user_id DROP NOT NULL;"))
    conn.commit()
    print("✅ Migration completed: user_id is now nullable in user_videos table")
