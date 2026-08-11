-- Allow one immutable source packet payload to bind to multiple capture results.
-- Migration 011 already enforces uniqueness across the full content+resolution key.
ALTER TABLE b3s_history.evidence_vault_canonical_memory_packets
    DROP CONSTRAINT IF EXISTS evidence_vault_canonical_memory_brand_id_packet_fingerprint_key;
