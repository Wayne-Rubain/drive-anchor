# Drive Anchor
#
# The image exists to pin the Python runtime and its two dependencies, not
# to isolate anything -- see README.md, "Why this needs so much privilege".
# Installing Python packages on DSM directly is fragile and does not survive
# DSM upgrades, which is the actual problem this container solves.

FROM python:3.12-slim

# nsenter (util-linux) is how commands are executed in the host's mount
# namespace. Without it, bind mounts would exist only inside this container
# and would be invisible to DSM, Docker and every package on the NAS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests==2.32.3 pyyaml==6.0.2

WORKDIR /app
COPY drive_anchor/ /app/drive_anchor/

ENV DRIVE_ANCHOR_CONFIG=/data/config.yaml
ENV PYTHONUNBUFFERED=1

# No default action. Every operation is explicit, because the destructive
# ones should never be something a container does merely by starting.
ENTRYPOINT ["python3", "-m", "drive_anchor.cli"]
CMD ["status"]
