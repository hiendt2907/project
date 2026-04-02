CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_documents (
    id UUID,
    collection_name VARCHAR(64) NOT NULL,
    embedding vector(768),
    payload JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (collection_name, id)
) PARTITION BY LIST (collection_name);

CREATE TABLE IF NOT EXISTS doc_sop PARTITION OF rag_documents FOR VALUES IN ('itops_sop_ledger');
CREATE TABLE IF NOT EXISTS doc_sop_v2 PARTITION OF rag_documents FOR VALUES IN ('itops_sop_ledger_v2');
CREATE TABLE IF NOT EXISTS doc_errors PARTITION OF rag_documents FOR VALUES IN ('itops_error_ledger');
CREATE TABLE IF NOT EXISTS doc_infra PARTITION OF rag_documents FOR VALUES IN ('infra_topology');
CREATE TABLE IF NOT EXISTS doc_action PARTITION OF rag_documents FOR VALUES IN ('action_experience');
CREATE TABLE IF NOT EXISTS doc_cli PARTITION OF rag_documents FOR VALUES IN ('cli_hil_context');

CREATE INDEX IF NOT EXISTS doc_sop_embedding_idx ON doc_sop USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS doc_sop_v2_embedding_idx ON doc_sop_v2 USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS doc_errors_embedding_idx ON doc_errors USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS doc_infra_embedding_idx ON doc_infra USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS doc_action_embedding_idx ON doc_action USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS doc_cli_embedding_idx ON doc_cli USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
