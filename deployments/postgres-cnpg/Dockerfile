FROM postgres:16-bookworm

# CNPG Operator securely enforces runAsUser: 26.
# Change the official postgres (999) user to 26 and fix all directory ownerships.
RUN groupmod -o -g 26 postgres && \
    usermod -o -u 26 postgres && \
    chown -R 26:26 /var/lib/postgresql && \
    chown -R 26:26 /var/run/postgresql && \
    chown -R 26:26 /etc/postgresql

RUN apt-get update && \
    apt-get install -y postgresql-16-pgvector && \
    rm -rf /var/lib/apt/lists/*
