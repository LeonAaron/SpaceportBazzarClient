FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgcc-s1 protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# The supplied binaries lack execute permission. Install outside the source mount.
COPY bazaar-protobuf-starter-linux/spaceport-validate-linux-* /tmp/bazaar-bin/
RUN case "$(uname -m)" in \
        aarch64|arm64) arch=arm64 ;; \
        x86_64|amd64) arch=x86_64 ;; \
        *) echo "Unsupported architecture" >&2; exit 1 ;; \
    esac \
    && install -m 0755 "/tmp/bazaar-bin/spaceport-validate-linux-${arch}" /usr/local/bin/bazaar-server \
    && rm -rf /tmp/bazaar-bin

WORKDIR /workspace
CMD ["bazaar-server", "--codec", "protobuf"]
