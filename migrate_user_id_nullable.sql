-- Migration: Make user_id nullable in user_videos table
-- Context: TEMP fix for testing /videos/upload without auth

ALTER TABLE user_videos 
ALTER COLUMN user_id DROP NOT NULL;
