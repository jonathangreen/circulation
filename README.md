# Palace Manager

[![Test & Build](https://github.com/ThePalaceProject/circulation/actions/workflows/test-build.yml/badge.svg)](https://github.com/ThePalaceProject/circulation/actions/workflows/test-build.yml)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Imports: isort](https://img.shields.io/badge/%20imports-isort-%231674b1?style=flat&labelColor=ef8336)](https://pycqa.github.io/isort/)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
![Python: 3.6,3.7,3.8,3.9](https://img.shields.io/badge/Python-3.6%20%7C%203.7%20%7C%203.8%20%7C%203.9-blue)

This is a [The Palace Project](https://thepalaceproject.org) maintained fork of the NYPL
[Library Simplified](http://www.librarysimplified.org/) Circulation Manager.

## Installation

Docker images created from this code are available at:

- [circ-webapp](https://github.com/ThePalaceProject/circulation/pkgs/container/circ-webapp)
- [circ-scripts](https://github.com/ThePalaceProject/circulation/pkgs/container/circ-scripts)
- [circ-exec](https://github.com/ThePalaceProject/circulation/pkgs/container/circ-exec)

Docker images are the preferred way to deploy this code in a production environment.

## Git Branch Workflow

| Branch   | Python Version |
| -------- | -------------- |
| main     | Python 3       |
| python2  | Python 2       |

The default branch is `main` and that's the working branch that should be used when branching off for bug fixes or new
features.

Python 2 stopped being supported after January 1st, 2020 but there is still a `python2` branch which can be used. As of
August 2021, development will be done in the `main` branch and the `python2` branch will not be updated unless
absolutely necessary.

## Set Up

### Python Set Up

#### Homebrew (OSX)

If you do not have Python 3 installed, you can use [Homebrew](https://brew.sh/) to install it by running the command
`brew install python3`.

If you do not yet have Homebrew, you can install it by running the following:

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

While you're at it, go ahead and install the following required dependencies:

```sh
brew install pkg-config libffi
brew install libxmlsec1
brew install libjpeg
```

#### pyenv

[pyenv](https://github.com/pyenv/pyenv) pyenv lets you easily switch between multiple versions of Python. It can be
[installed](https://github.com/pyenv/pyenv-installer) using the command `curl https://pyenv.run | bash`. You can then
install the version of Python you want to work with.

#### Poetry

You will need to set up a local virtual environment to install packages and run the project. This project uses
[poetry](https://python-poetry.org/) for dependency management.

Poetry can be installed using the command `curl -sSL https://install.python-poetry.org | python3 -`.

More information about installation options can be found in the
[poetry documentation](https://python-poetry.org/docs/master/#installation).

### Elasticsearch

#### Docker

The easiest way to setup a local elasticsearch environment is to use docker.

```sh
docker run -d --name es -e discovery.type=single-node -p 9200:9200 elasticsearch:6.8.6
docker exec es elasticsearch-plugin -s install analysis-icu
docker restart es
```

#### Local

1. Download it [here](https://www.elastic.co/downloads/past-releases/elasticsearch-6-8-6).
2. `cd` into the `elasticsearch-[version number]` directory.
3. Run `$ elasticsearch-plugin install analysis-icu`
4. Run `$ ./bin/elasticsearch`.
5. You may be prompted to download [Java SE](https://www.oracle.com/java/technologies/javase-downloads.html). If so, go
   ahead and do so.
6. Check `http://localhost:9200` to make sure the Elasticsearch server is running.

### Database

#### Docker

```sh
docker run -d --name pg -e POSTGRES_USER=palace -e POSTGRES_PASSWORD=test -e POSTGRES_DB=circ -p 5432:5432 postgres:12
```

You can run `psql` in the container using the command

```sh
docker exec -it pg psql -U palace circ
```

#### Local

1. Download and install [Postgres](https://www.postgresql.org/download/) if you don't have it already.
2. Use the command `psql` to access the Postgresql client.
3. Within the session, run the following commands:

```sh
CREATE DATABASE circ;
CREATE USER palace with password 'test';
grant all privileges on database circ to palace;
```

#### Environment variables

To let the application know which database to use set the `SIMPLIFIED_PRODUCTION_DATABASE` env variable.

```sh
export SIMPLIFIED_PRODUCTION_DATABASE="postgres://palace:test@localhost:5432/circ"
```

### Running the Application

Install the dependencies:

```sh
poetry install --no-root -E pg-binary
```

Run the application with:

```sh
poetry run python app.py
```

And visit `http://localhost:6500/`.

### Installation Issues

When running the `poetry install ...` command, you may run into installation issues. On newer macos machines, you may
encounter an error such as:

```sh
error: command '/usr/bin/clang' failed with exit code 1
  ----------------------------------------
  ERROR: Failed building wheel for xmlsec
Failed to build xmlsec
ERROR: Could not build wheels for xmlsec which use PEP 517 and cannot be installed directly
```

This typically happens after installing packages through brew and then running the `pip install` command.

This [blog post](https://mbbroberg.fun/clang-error-in-pip/) explains and shows a fix for this issue. Start by trying
the `xcode-select --install` command. If it does not work, you can try adding the following to your `~/.zshrc` or
`~/.bashrc` file, depending on what you use:

```sh
export CPPFLAGS="-DXMLSEC_NO_XKMS=1"
```

## Generating Documentation

Code documentation can be generated using Sphinx. The configuration for the documentation can be found in `/docs`.

Github Actions handles generating the `.rst` source files, generating the HTML static site, and deploying the build to
the `gh-pages` branch.

To view the documentation _locally_, go into the `/docs` directory and run `make html`. This will generate the .rst
source files and build the static site in `/docs/build/html`

## Code Style

Code style on this project is linted using [pre-commit](https://pre-commit.com/). This python application is included
in our `pyproject.toml` file, so if you have the applications requirements installed it should be available. pre-commit
is run automatically on each push and PR by our [CI System](#continuous-integration).

You can run it manually on all files with the command: `pre-commit run --all-files`.

You can also set it up, so that it runs automatically for you on each commit. Running the command `pre-commit install`
will install the pre-commit script in your local repositories git hooks folder, so that pre-commit is run automatically
on each commit.

### Configuration

The pre-commit configuration file is named [`.pre-commit-config.yaml`](.pre-commit-config.yaml). This file configures
the different lints that pre-commit runs.

### Linters

#### Built in

Pre-commit ships with a [number of lints](https://pre-commit.com/hooks.html) out of the box, we are configured to use:
- `trailing-whitespace` - trims trailing whitespace.
- `end-of-file-fixer` - ensures that a file is either empty, or ends with one newline.
- `check-yaml` - checks yaml files for parseable syntax.
- `check-json` - checks json files for parseable syntax.
- `check-ast` - simply checks whether the files parse as valid python.
- `check-shebang-scripts-are-executable` - ensures that (non-binary) files with a shebang are executable.
- `check-executables-have-shebangs` -  ensures that (non-binary) executables have a shebang.
- `check-merge-conflict` - checks for files that contain merge conflict strings.
- `check-added-large-files` - prevents giant files from being committed.
- `mixed-line-ending` - replaces or checks mixed line ending.

#### Black

We lint using the [black](https://black.readthedocs.io/en/stable/) code formatter, so that all of our code is formatted
consistently.

#### isort

We lint to make sure our imports are sorted and correctly formatted using [isort](https://pycqa.github.io/isort/). Our
isort configuration is stored in our [tox.ini](tox.ini) which isort automatically detects.

## Continuous Integration

This project runs all the unit tests through Github Actions for new pull requests and when merging into the default
`main` branch. The relevant file can be found in `.github/workflows/test-build.yml`. When contributing updates or
fixes, it's required for the test Github Action to pass for all Python 3 environments. Run the `tox` command locally
before pushing changes to make sure you find any failing tests before committing them.

For each push to a branch, CI also creates a docker image for the code in the branch. These images can be used for
testing the branch, or deploying hotfixes.

## Testing

The Github Actions CI service runs the unit tests against Python 3.6, 3.7, 3.8 and 3.9 automatically using
[tox](https://tox.readthedocs.io/en/latest/).

To run `pytest` unit tests locally, install `tox`.

```sh
pip install tox
```

Tox has an environment for each python version, the module being tested, and an optional `-docker` factor that will
automatically use docker to deploy service containers used for the tests. You can select the environment you would like
to test with the tox `-e` flag.

### Factors

When running tox without an environment specified, it tests `circulation` and `core` using all supported Python versions
with service dependencies running in docker containers.

#### Python version

| Factor      | Python Version |
| ----------- | -------------- |
| py36        | Python 3.6     |
| py37        | Python 3.7     |
| py38        | Python 3.8     |
| py39        | Python 3.9     |

All of these environments are tested by default when running tox. To test one specific environment you can use the `-e`
flag.

Test Python 3.8

```sh
tox -e py38
```

You need to have the Python versions you are testing against installed on your local system. `tox` searches the system
for installed Python versions, but does not install new Python versions. If `tox` doesn't find the Python version its
looking for it will give an `InterpreterNotFound` errror.

[Pyenv](#pyenv) is a useful tool to install multiple Python versions, if you need to install
missing Python versions in your system for local testing.

#### Module

| Factor      | Module            |
| ----------- | ----------------- |
| core        | core tests        |
| api         | api tests         |

#### Docker

If you install `tox-docker` tox will take care of setting up all the service containers necessary to run the unit tests
and pass the correct environment variables to configure the tests to use these services. Using `tox-docker` is not
required, but it is the recommended way to run the tests locally, since it runs the tests in the same way they are run
on the Github Actions CI server.

```sh
pip install tox-docker
```

The docker functionality is included in a `docker` factor that can be added to the environment. To run an environment
with a particular factor you add it to the end of the environment.

Test with Python 3.8 using docker containers for the services.

```sh
tox -e "py38-{api,core}-docker"
```

### Local services

If you already have elastic search or postgres running locally, you can run them instead by setting the
following environment variables:

- `SIMPLIFIED_TEST_DATABASE`
- `SIMPLIFIED_TEST_ELASTICSEARCH`

Make sure the ports and usernames are updated to reflect the local configuration.

```sh
# Set environment variables
export SIMPLIFIED_TEST_DATABASE="postgres://simplified_test:test@localhost:9005/simplified_circulation_test"
export SIMPLIFIED_TEST_ELASTICSEARCH="http://localhost:9006"

# Run tox
tox -e "py38-{api,core}"
```

### Override `pytest` arguments

If you wish to pass additional arguments to `pytest` you can do so through `tox`. Every argument passed after a `--` to
the `tox` command line will the passed to `pytest`, overriding the default.

Only run the `test_google_analytics_provider` tests with Python 3.8 using docker.

```sh
tox -e "py38-api-docker" -- tests/api/test_google_analytics_provider.py
```

## Usage with Docker

Check out the [Docker README](/docker/README.md) in the `/docker` directory for in-depth information on optionally
running and developing the Circulation Manager locally with Docker, or for deploying the Circulation Manager with
Docker.

## Performance Profiling

There are three different profilers included to help measure the performance of the application. They can each be
enabled by setting environment variables while starting the application.

### AWS XRay

#### Environment Variables

- `PALACE_XRAY`: Set to enable X-Ray tracing on the application.
- `PALACE_XRAY_NAME`: The name of the service shown in x-ray for these traces.
- `PALACE_XRAY_ANNOTATE_`: Any environment variable starting with this prefix will be added to to the trace as an
  annotation.
    - For example setting `PALACE_XRAY_ANNOTATE_KEY=value` will set the annotation `key=value` on all xray traces sent
    from the application.
- `PALACE_XRAY_INCLUDE_BARCODE`: If this environment variable is set to `true` then the tracing code will try to include
  the patrons barcode in the user parameter of the trace, if a barcode is available.

Additional environment variables are provided by the
[X-Ray Python SDK](https://docs.aws.amazon.com/xray/latest/devguide/xray-sdk-python-configuration.html#xray-sdk-python-configuration-envvars).

### cProfile

This profiler uses the
[werkzeug `ProfilerMiddleware`](https://werkzeug.palletsprojects.com/en/2.0.x/middleware/profiler/)
to profile the code. This uses the
[cProfile](https://docs.python.org/3/library/profile.html#module-cProfile)
module under the hood to do the profiling.

#### Environment Variables

- `PALACE_CPROFILE`: Profiling will the enabled if this variable is set. The saved profile data will be available at
  path specified in the environment variable.
- The profile data will have the extension `.prof`.
- The data can be accessed using the
[`pstats.Stats` class](https://docs.python.org/3/library/profile.html#the-stats-class).
- Example code to print details of the gathered statistics:
  ```python
  import os
  from pathlib import Path
  from pstats import SortKey, Stats

  path = Path(os.environ.get("PALACE_CPROFILE"))
  for file in path.glob("*.prof"):
      stats = Stats(str(file))
      stats.sort_stats(SortKey.CUMULATIVE, SortKey.CALLS)
      stats.print_stats()
  ```

### PyInstrument

This profiler uses [PyInstrument](https://pyinstrument.readthedocs.io/en/latest/) to profile the code.

#### Environment Variables

- `PALACE_PYINSTRUMENT`: Profiling will the enabled if this variable is set. The saved profile data will be available at
  path specified in the environment variable.
    - The profile data will have the extension `.pyisession`.
    - The data can be accessed with the
    [`pyinstrument.session.Session` class](https://pyinstrument.readthedocs.io/en/latest/reference.html#pyinstrument.session.Session).
    - Example code to print details of the gathered statistics:
      ```python
      import os
      from pathlib import Path

      from pyinstrument.renderers import HTMLRenderer
      from pyinstrument.session import Session

      path = Path(os.environ.get("PALACE_PYINSTRUMENT"))
      for file in path.glob("*.pyisession"):
          session = Session.load(file)
          renderer = HTMLRenderer()
          renderer.open_in_browser(session)
      ```
