FROM postgres:16-bookworm

RUN apt-get update && \
    apt-get install -y pgpool2 && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/pgpool2 /var/run/pgpool

# Write pool_passwd directly (plaintext format for Pgpool to use when authenticating to backend)
# Pgpool reads this file to get the password, then performs SCRAM with the backend using it
RUN printf 'appuser:GD3fjTJJxfzi0bau6TSaoWV9Q8TeuEYxahQrFDh6DCnMRjgFdEQ1q7Hf3FKFbxD8\n' \
    > /etc/pgpool2/pool_passwd

RUN chown -R postgres:postgres /etc/pgpool2 /var/run/pgpool && \
    chmod 0600 /etc/pgpool2/pool_passwd

USER postgres
ENTRYPOINT ["pgpool", "-n", "-f", "/etc/pgpool2/pgpool.conf"]
