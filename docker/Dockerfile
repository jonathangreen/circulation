###############################################################################
## Lcpencrypt
###############################################################################

FROM golang:1.17 AS lcp-builder

LABEL maintainer="The Palace Project <info@thepalaceproject.org>"

RUN go get -v github.com/readium/readium-lcp-server/lcpencrypt

###############################################################################
## Build xmlsec wheel
###############################################################################

FROM lukewiwa/manylinux2014 AS wheel-builder

ENV PYXMLSEC_LIBXML2_VERSION=2.9.10
ENV PYXMLSEC_XMLSEC1_VERSION=1.2.37
ENV PYXMLSEC_STATIC_DEPS=1
ENV PYXMLSEC_VERSION=1.3.13

RUN /opt/python/cp38-cp38/bin/pip wheel xmlsec==$PYXMLSEC_VERSION && \
    auditwheel repair xmlsec-$PYXMLSEC_VERSION-*.whl && \
    echo $PYXMLSEC_VERSION > /wheelhouse/version.txt

COPY poetry.lock /lockfile

RUN LOCKFILE_VERSION=$(grep 'name = "xmlsec"' -A 1 lockfile | tail -n 1 | sed 's/version = "\(.*\)"/\1/') && \
    if ! [[ $LOCKFILE_VERSION == $PYXMLSEC_VERSION ]]; then \
        echo "Lockfile version $LOCKFILE_VERSION does not match PYXMLSEC_VERSION $PYXMLSEC_VERSION" && \
        exit 1; \
    fi

###############################################################################
## Base Image
###############################################################################

FROM phusion/baseimage:focal-1.1.0 As base

COPY --from=lcp-builder /go/bin/lcpencrypt /go/bin/lcpencrypt
ENV POETRY_VERSION=1.3.2

# Make sure base system is up to date and install necesary packages
RUN apt-get update && apt-get upgrade -y -o Dpkg::Options::="--force-confold" && \
    install_clean --no-upgrade python3 python3-dev python3-setuptools python3-venv python3-pip && \
    curl -sSL https://install.python-poetry.org | POETRY_HOME="/opt/poetry" python3 - --yes --version $POETRY_VERSION && \
    ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

# Create user and app folder
RUN useradd -ms /bin/bash -U simplified && \
    mkdir -p /var/www/circulation && \
    su - simplified -c "ln -s /var/www/circulation /home/simplified/circulation" && \
    chown -RHh simplified:simplified /home/simplified/circulation && \
    mkdir /var/log/simplified

WORKDIR /var/www/circulation

# Copy init files
COPY docker/startup/* /etc/my_init.d/

# Setup virtualenv and install the wheel we built for xmlsec
COPY --from=wheel-builder /wheelhouse /wheelhouse
RUN python3 -m venv env && \
    . env/bin/activate && \
    python3 -m pip install xmlsec==$(cat /wheelhouse/version.txt) --find-links=/wheelhouse

# Setup environment for the app
COPY --chown=1000:1000 poetry.lock pyproject.toml /var/www/circulation/
RUN . env/bin/activate && \
    python3 -m pip install -U pip setuptools && \
    poetry install --only main --sync && \
    poetry cache clear -n --all pypi && \
    python3 -m textblob.download_corpora lite && \
    mv /root/nltk_data /usr/lib/ && \
    rm -Rf /root/.cache && \
    /bd_build/cleanup.sh

# Setup logrotate
RUN rm -rf /etc/logrotate.d/dpkg
COPY docker/services/logrotate/ /etc/

# Copy the app files
COPY --chown=1000:1000 . /var/www/circulation

###############################################################################
## Circ-exec Image
###############################################################################

FROM base as exec

ENV SIMPLIFIED_DB_TASK "ignore"
ENV SIMPLIFIED_SCRIPT_NAME ""

RUN /bin/bash -c "rm -rf /ls_build && /bd_build/cleanup.sh"

VOLUME /var/log
WORKDIR /home/simplified/circulation/bin
CMD ["/sbin/my_init", "--skip-runit", "--quiet", "--", \
     "/bin/bash", "-c", \
     "source ../env/bin/activate && ./${SIMPLIFIED_SCRIPT_NAME}"]

###############################################################################
## Circ-scripts Image
###############################################################################

FROM base as scripts

ENV SIMPLIFIED_DB_TASK "auto"
# Set the local timezone to US/Eastern
ENV TZ=US/Eastern

COPY /docker/services/cron.d /etc/cron.d

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    touch /var/log/cron.log

VOLUME /var/log
WORKDIR /home/simplified/circulation/bin

CMD ["/sbin/my_init"]

###############################################################################
## Circ-webapp Image
###############################################################################

FROM base as webapp

ENV SIMPLIFIED_DB_TASK "ignore"

COPY /docker/services/uwsgi/ /etc/
COPY /docker/services/uwsgi.runit /etc/service/uwsgi/run
RUN mkdir /var/log/uwsgi && \
    chown -R simplified:simplified /var/log/uwsgi

VOLUME /var/log
WORKDIR /home/simplified/circulation
EXPOSE 80

CMD ["/sbin/my_init"]
