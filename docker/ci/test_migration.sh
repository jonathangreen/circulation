#!/bin/bash

if ! git diff --quiet; then
  echo "ERROR: You have uncommitted changes. These changes will be lost if you run this script."
  echo "  Please commit or stash your changes and try again."
  exit 1
fi

# Find the currently checked out commit
current_commit=$(git show -s --format=%H)

echo "Current commit: ${current_commit}"

# Find the first migration file
first_migration_id=$(alembic history -r'base:base+1' -v | head -n 1 | cut -d ' ' -f2)
first_migration_file=$(find alembic/versions -name "*${first_migration_id}*.py")

echo "First migration file: ${first_migration_file}"

# Find the git commit before the first migration file was added
before_migration_commit=$(git log --follow --format=%P --reverse "${first_migration_file}" | head -n 1)

echo "Before migration commit: ${before_migration_commit}"

# Checkout this commit
git checkout -q "${before_migration_commit}"

# Start containers and initialize the database
docker-compose up -d pg
export SIMPLIFIED_PRODUCTION_DATABASE="postgresql://palace:test@localhost:5432/circ"
bin/util/initialize_instance

# Checkout the current commit
git checkout "${current_commit}"

# Migrate up to the current commit
alembic upgrade head

# Now check that the database matches what we would expect
alembic check
