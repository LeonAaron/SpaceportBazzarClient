# Python development with Docker

Install and start Docker Desktop on macOS/Windows, or Docker Engine with the
Compose plugin on Linux. Run these commands from the repository root.

## Start the practice server

```sh
docker compose up --build
```

Leave this terminal running. In a second terminal, open a Python development shell
in the same container:

```sh
docker compose exec bazaar bash
```

Your checkout is mounted at `/workspace`; edits there persist on your computer.
The server is available at `ws://127.0.0.1:3001/ws` **inside this container**.
Use `exec` for the client: `docker compose run` creates a separate container whose
localhost cannot reach this server. No published host port is needed.

## Generate Python bindings

Inside the container shell:

```sh
protoc -I bazaar-protobuf-starter-linux --python_out=. bazaar-protobuf-starter-linux/bazaar.proto
python -c 'import bazaar_pb2, websockets; print("Python dependencies and bindings ready")'
```

This generates `bazaar_pb2.py` at the repository root. Regenerate after schema
changes. Python, the Protobuf runtime, and the `websockets` library are installed
in the image. When you create a client, run it here with `python your_client.py`.

The server creates `validation-credentials.json` and `validation-report.json`
in the checkout root. Both are ignored by Git. Read P01's token inside the shell:

```sh
python -c 'import json; print(next(p["token"] for p in json.load(open("validation-credentials.json"))["players"] if p["station_id"] == "P01"))'
```

Follow the [exercise guide](bazaar-protobuf-starter-linux/README.md) for the
authentication headers, subprotocol, and message sequence. A trading client is
not yet implemented.

## Stop and reset

Press Ctrl+C in the server terminal, then clean up the stopped container:

```sh
docker compose down
```

To reset a running exercise, use `docker compose restart bazaar`. Every restart
resets progress and issues new credentials; reread the token before reconnecting.
Rebuild with `docker compose up --build` after editing the Dockerfile or
requirements. Source edits do not require a rebuild.

## Consistency across computers

The image uses Python 3.12 on Debian Bookworm, which supplies the required glibc
and `libgcc_s.so.1`. It selects the bundled ARM64 or x86-64 server automatically
and sets its executable permissions inside the image. Python library versions
are pinned in `requirements.txt`; the base tag and OS packages still receive
updates, so builds are not byte-for-byte locked.

On Linux, to avoid root-owned output files, set the service's user to your host
UID/GID in a local `compose.override.yaml`, for example `user: "1000:1000"` under
`services.bazaar`. Find your IDs with `id -u` and `id -g`.

Reference: [Docker Compose service configuration](https://docs.docker.com/reference/compose-file/services/).
