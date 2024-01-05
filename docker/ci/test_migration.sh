#!/bin/bash

compose-cmd() {
  docker --log-level ERROR compose --progress quiet "$@"
}

run_in_container()
{
  CMD=$1
  compose-cmd run --build --rm webapp /bin/bash -c "source env/bin/activate && $CMD"
}

if ! git diff --quiet; then
  echo "ERROR: You have uncommitted changes. These changes will be lost if you run this script."
  echo "  Please commit or stash your changes and try again."
  exit 1
fi

# Find the currently checked out branch
current_branch=$(git symbolic-ref --short HEAD)

echo "Current branch: ${current_branch}"

# Find the first migration file
first_migration_id=$(alembic history -r'base:base+1' -v | head -n 1 | cut -d ' ' -f2)
first_migration_file=$(find alembic/versions -name "*${first_migration_id}*.py")

echo "First migration file: ${first_migration_file}"
echo ""

# Find the git commit before this migration file was introduced
first_migration_parent_commit=$(git log --follow --format=%P --reverse "${first_migration_file}" | head -n 1)

echo "Starting containers and initializing database at commit ${first_migration_parent_commit}"
git checkout -q "${first_migration_parent_commit}"
compose-cmd down
compose-cmd up -d pg
run_in_container "./bin/util/initialize_instance"
echo ""

# Migrate up to the current commit and check if the database is in sync
git checkout -q "${current_branch}"
echo "Running database migrations on branch ${current_branch}"
run_in_container "alembic upgrade head"
exit_code=$?
if [[ $exit_code -ne 0 ]]; then
  echo "ERROR: Database migration failed."
  exit $exit_code
fi
echo ""

echo "Checking database status"
run_in_container "alembic check"
exit_code=$?
echo ""

if [[ $exit_code -eq 0 ]]; then
  echo "SUCCESS: Database is in sync."
else
  echo "ERROR: Database is out of sync. A new migration is required."
fi

# Stop containers
compose-cmd down

exit $exit_code
