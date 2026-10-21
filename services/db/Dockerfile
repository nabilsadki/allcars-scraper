FROM mysql:8.0

# schema applique au premier demarrage du volume
COPY init/ /docker-entrypoint-initdb.d/

# utf8mb4 par defaut + sql_mode compatible
RUN { \
      echo '[mysqld]'; \
      echo 'character-set-server=utf8mb4'; \
      echo 'collation-server=utf8mb4_unicode_ci'; \
      echo 'default-time-zone=+01:00'; \
    } > /etc/mysql/conf.d/avito.cnf
