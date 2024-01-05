#!/bin/bash

run_in_container()
{
  CMD=$1
  docker compose run --quiet-pull --build --rm webapp /bin/bash -c "source env/bin/activate && $CMD"
}

if ! git diff --quiet; then
  echo "ERROR: You have uncommitted changes. These changes will be lost if you run this script."
  echo "  Please commit or stash your changes and try again."
  exit 1
fi

# Find the currently checked out commit
current_branch=$(git symbolic-ref --short HEAD)

echo "Current branch: ${current_branch}"

# Find the first migration file
first_migration_id=$(alembic history -r'base:base+1' -v | head -n 1 | cut -d ' ' -f2)
first_migration_file=$(find alembic/versions -name "*${first_migration_id}*.py")

echo "First migration file: ${first_migration_file}"

# Find the git commit where this migration was introduced
first_migration_commit=$(git log --follow --format=%H --reverse "${first_migration_file}" | head -n 1)

echo "First migration commit: ${first_migration_commit}"

# Checkout this commit
git checkout -q "${first_migration_commit}"

# Start containers and initialize the database
docker compose down
docker compose up -d --quiet-pull pg
run_in_container "./bin/util/initialize_instance"

# Checkout the current commit
git checkout "${current_branch}"

# Migrate up to the current commit and check if the database is in sync
run_in_container "alembic upgrade head && alembic check"
exit_code=$?

if [[ $exit_code -eq 0 ]]; then
  echo "Database is in sync."
else
  echo "ERROR: Database is out of sync. Please generate an alembic migration."
fi

# Stop containers
docker compose down

exit $exit_code
