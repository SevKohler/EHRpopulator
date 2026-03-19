FROM python:3.12-slim

# Java + curl needed for hapi-fhir-cli
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless curl unzip \
    && rm -rf /var/lib/apt/lists/*

# hapi-fhir-cli — extract to /opt/hapi so the shell script finds the jar next to itself
RUN curl -sSL -o /tmp/hapi-cli.zip \
    https://github.com/hapifhir/hapi-fhir/releases/download/v7.6.0/hapi-fhir-7.6.0-cli.zip \
    && mkdir -p /opt/hapi \
    && unzip -q /tmp/hapi-cli.zip -d /opt/hapi/ \
    && chmod +x /opt/hapi/hapi-fhir-cli \
    && rm /tmp/hapi-cli.zip

ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH="$JAVA_HOME/bin:/opt/hapi:$PATH"

RUN pip install requests
COPY load.py /load.py
ENTRYPOINT ["python", "/load.py"]
