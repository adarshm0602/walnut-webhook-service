CREATE TABLE transactions (
    transaction_id varchar(128) PRIMARY KEY,
    source_account varchar(128) NOT NULL,
    destination_account varchar(128) NOT NULL,
    amount numeric(18, 2) NOT NULL CHECK (amount > 0),
    currency varchar(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    status varchar(10) NOT NULL DEFAULT 'PROCESSING'
        CHECK (status IN ('PROCESSING', 'PROCESSED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    processed_at timestamptz,
    CHECK ((status = 'PROCESSED') = (processed_at IS NOT NULL))
);

-- One durable job per transaction, written in the same commit as its transaction.
CREATE TABLE jobs (
    transaction_id varchar(128) PRIMARY KEY REFERENCES transactions(transaction_id),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_token uuid,
    leased_until timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error varchar(500),
    completed_at timestamptz,
    CHECK ((lease_token IS NULL) = (leased_until IS NULL))
);

CREATE INDEX jobs_available_idx ON jobs (available_at, transaction_id)
    WHERE completed_at IS NULL;
