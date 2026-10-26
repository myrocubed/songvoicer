
import argparse
import asyncio
import json
import queue
import random
import socket
import struct
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = 7777
START_DELAY = 3.0  # seconds between the host pressing Start and the song starting
CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O, they look like 1 and 0


def pack(head, data=b""):
    if data:
        head = dict(head, size=len(data))
    raw = json.dumps(head).encode()
    return struct.pack(">I", len(raw)) + raw + data


# server
class Player:
    def __init__(self, pid, writer):
        self.id, self.writer = pid, writer
        self.name, self.songs, self.room, self.char = f"Player {pid}", [], None, None


class Room:
    def __init__(self, code, song, host):
        self.code, self.song, self.host = code, song, host
        self.players = {}
        self.state = "lobby"   # lobby > playing > lobby
        self.uploaded = set()


class Server:
    def __init__(self):
        self.rooms = {}
        self.next_id = 1

    async def handle(self, reader, writer):
        p = Player(self.next_id, writer)
        self.next_id += 1
        try:
            while True:
                size = struct.unpack(">I", await reader.readexactly(4))[0]
                head = json.loads(await reader.readexactly(size))
                data = await reader.readexactly(head["size"]) if head.get("size") else b""
                await self.dispatch(p, head, data)
        except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError):
            pass
        finally:
            await self.leave(p)
            writer.close()

    async def send(self, p, head, data=b""):
        try:
            p.writer.write(pack(head, data))
            await p.writer.drain()
        except ConnectionError:
            pass

    async def error(self, p, msg):
        await self.send(p, {"type": "error", "msg": msg})

    async def broadcast(self, room):
        state = {"type": "room", "code": room.code, "song": room.song, "host": room.host, "state": room.state,
                 "players": [{"id": q.id, "name": q.name, "char": q.char, "uploaded": q.id in room.uploaded}
                             for q in room.players.values()]}
        for q in list(room.players.values()):
            await self.send(q, state)

    async def dispatch(self, p, head, data):
        kind = head.get("type")
        room = p.room
        if kind == "hello":
            p.name = str(head.get("name") or p.name)[:24]
            p.songs = list(head.get("songs", []))
            await self.send(p, {"type": "welcome", "id": p.id})
        elif kind == "ping":
            await self.send(p, {"type": "pong", "t": head.get("t")})
        elif kind == "create":
            await self.leave(p)
            code = "".join(random.choice(CODE_LETTERS) for _ in range(4))
            while code in self.rooms:
                code = "".join(random.choice(CODE_LETTERS) for _ in range(4))
            room = self.rooms[code] = Room(code, head.get("song"), p.id)
            room.players[p.id], p.room, p.char = p, room, None
            await self.broadcast(room)
        elif kind == "join":
            target = self.rooms.get(str(head.get("code", "")).strip().upper())
            if not target:
                return await self.error(p, "No room with that code.")
            if target.state != "lobby":
                return await self.error(p, "That room is in the middle of a song. Try again after it.")
            if target.song not in p.songs:
                return await self.error(p, f"You don't have the song \"{target.song}\" in your maps folder.")
            await self.leave(p)
            target.players[p.id], p.room, p.char = p, target, None
            await self.broadcast(target)
        elif not room:
            await self.error(p, "You're not in a room.")
        elif kind == "pick":
            char = head.get("char")
            if char and any(q.char == char and q is not p for q in room.players.values()):
                return await self.error(p, "Someone else already picked that character.")
            p.char = char
            await self.broadcast(room)
        elif kind == "song" and p.id == room.host and room.state == "lobby":
            missing = [q.name for q in room.players.values() if head.get("song") not in q.songs]
            if missing:
                return await self.error(p, f"{', '.join(missing)} don't have that song.")
            room.song = head.get("song")
            for q in room.players.values():
                q.char = None
            await self.broadcast(room)
        elif kind == "start" and p.id == room.host:
            if room.state != "lobby":
                return
            if any(q.char is None for q in room.players.values()):
                return await self.error(p, "Everyone needs to pick a character first.")
            room.state, room.uploaded = "playing", set()
            for q in room.players.values():
                await self.send(q, {"type": "start", "delay": START_DELAY})
            await self.broadcast(room)
        elif kind == "upload":
            room.uploaded.add(p.id)
            for q in room.players.values():
                if q is not p:
                    await self.send(q, dict(head, type="voice", **{"from": p.id, "name": p.name, "char": p.char}),
                                    data)
            if room.state == "playing" and room.uploaded >= set(room.players):
                room.state = "lobby"
            await self.broadcast(room)
        elif kind == "leave":
            await self.leave(p)

    async def leave(self, p):
        room = p.room
        if not room:
            return
        room.players.pop(p.id, None)
        room.uploaded.discard(p.id)
        p.room, p.char = None, None
        if not room.players:
            self.rooms.pop(room.code, None)
            return
        if room.host == p.id:
            room.host = next(iter(room.players))
        if room.state == "playing" and room.uploaded >= set(room.players):
            room.state = "lobby"
        await self.broadcast(room)


async def serve(port=PORT, ready=None):
    server = await asyncio.start_server(Server().handle, "0.0.0.0", port)
    if ready:
        ready()
    async with server:
        await server.serve_forever()


def start_in_background(port=PORT):
    """Run the server on a daemon thread. Raises OSError if the port can't be opened."""
    started, failure = threading.Event(), []

    def run():
        try:
            asyncio.run(serve(port, started.set))
        except OSError as e:
            failure.append(e)
            started.set()

    threading.Thread(target=run, daemon=True).start()
    started.wait(5)
    if failure:
        raise failure[0]


def local_ip():
    """This machine's address on the local network (no traffic is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


# client

class Client:
    """Talks to a server from the app. Incoming messages land in `inbox` as (head, data)."""

    def __init__(self, address, name, songs):
        host, _, port = address.strip().rpartition(":")
        if not host:
            host, port = address.strip(), PORT
        self.sock = socket.create_connection((host, int(port)), timeout=6)
        self.sock.settimeout(None)
        self.inbox = queue.Queue()
        self.lock = threading.Lock()
        self.rtt = 0.0
        self.id = None
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()
        self.send({"type": "hello", "name": name, "songs": songs})
        self.send({"type": "ping", "t": time.time()})

    def send(self, head, data=b""):
        with self.lock:
            self.sock.sendall(pack(head, data))

    def _recv(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("server closed the connection")
            buf += chunk
        return bytes(buf)

    def _reader(self):
        try:
            while True:
                head = json.loads(self._recv(struct.unpack(">I", self._recv(4))[0]))
                data = self._recv(head["size"]) if head.get("size") else b""
                if head.get("type") == "pong":
                    self.rtt = time.time() - head["t"]
                    continue
                if head.get("type") == "welcome":
                    self.id = head["id"]
                self.inbox.put((head, data))
        except (OSError, ConnectionError, ValueError):
            if not self.closed:
                self.inbox.put(({"type": "disconnected"}, b""))

    def close(self):
        self.closed = True
        try:
            self.send({"type": "leave"})
        except OSError:
            pass
        self.sock.close()


# ---------------- website client (netlify/functions/api.mjs) ----------------

def make_ssl_context():
    """Python on macOS (and inside the packaged app) has no certificate list of its own, so HTTPS fails with
    CERTIFICATE_VERIFY_FAILED. Use certifi's list if it's installed, and on a Mac also macOS's own roots."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    if sys.platform == "darwin":
        for keychain in ("/System/Library/Keychains/SystemRootCertificates.keychain",
                         "/Library/Keychains/System.keychain"):
            try:
                pem = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p", keychain],
                                     capture_output=True, text=True, timeout=10).stdout
                if pem:
                    ctx.load_verify_locations(cadata=pem)
            except (OSError, ssl.SSLError, subprocess.SubprocessError):
                pass
    return ctx


SSL_CONTEXT = make_ssl_context()


def is_web_address(address):
    """https://… or a site name with a path/dot but no :port means the Netlify server, not a TCP host."""
    a = address.strip()
    plain_ip = all(part.isdigit() for part in a.split("."))
    return a.startswith(("http://", "https://")) or ("." in a and ":" not in a and not plain_ip)


class WebClient:
    """Same interface as Client (inbox, send, id, rtt, close) for the Netlify server.

    Actions go out as POSTs on a sender thread; a poller thread fetches the room about twice a second and
    turns changes into the same messages the TCP server sends: room, start, voice, error, disconnected.
    """
    POLL = 1.5  # each poll is a Netlify function call, which the free plan limits; Start gives 4 s notice
    CHUNK = 2_000_000  # Netlify function bodies are limited to about 6 MB

    def __init__(self, address, name, songs):
        base = address.strip().rstrip("/")
        self.base = base if base.startswith(("http://", "https://")) else "https://" + base
        self.inbox = queue.Queue()
        self.outbox = queue.Queue()
        self.poke = threading.Event()
        self.rtt = 0.0          # start delays come pre-corrected from the server clock
        self.offset = 0.0       # server clock minus ours, in seconds
        self.closed = False
        self.id = self.token = None
        self.round = 0
        self.started_round = None
        self.last_room = None
        self.seen_voices = set()
        r = self._post("hello", {"name": name, "songs": songs})
        if "id" not in r:
            raise ValueError(r.get("error") or "that address isn't a Song Voicer server")
        self.id, self.token = r["id"], r["token"]
        self.inbox.put(({"type": "welcome", "id": self.id}, b""))
        threading.Thread(target=self._sender, daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()

    def _request(self, path, body=None, headers=None, raw=False, timeout=60):
        req = urllib.request.Request(f"{self.base}/api/{path}", data=body, headers=headers or {},
                                     method="POST" if body is not None else "GET")
        req.add_header("User-Agent", "SongVoicer")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 410:
                raise
            data = e.read()
        if raw:
            return data
        try:
            return json.loads(data)
        except ValueError:
            raise ValueError("that address isn't a Song Voicer server (no /api found)")

    def _auth(self, **extra):
        return urllib.parse.urlencode(dict(extra, id=self.id, token=self.token))

    def _post(self, action, payload, timeout=60):
        body = json.dumps(dict(payload, id=self.id, token=self.token)).encode()
        return self._request(action, body, {"Content-Type": "application/json"}, timeout=timeout)

    def send(self, head, data=b""):
        if head.get("type") not in ("hello", "ping"):
            self.outbox.put((head, data))

    def _sender(self):
        while not self.closed:
            head, data = self.outbox.get()
            if head is None:
                return
            try:
                if head["type"] == "upload":
                    rnd = self.round
                    chunks = [data[i:i + self.CHUNK] for i in range(0, len(data), self.CHUNK)] or [b""]
                    for n, chunk in enumerate(chunks):
                        self._request("upload?" + self._auth(round=rnd, chunk=n), chunk,
                                      {"Content-Type": "application/octet-stream"})
                    r = self._post("upload_done", {"round": rnd, "segments": head.get("segments", []),
                                                   "chunks": len(chunks)})
                else:
                    r = self._post(head["type"], {k: v for k, v in head.items() if k != "type"})
            except urllib.error.HTTPError:
                self._disconnect()
                return
            except (OSError, ValueError) as e:
                if not self.closed:
                    self.inbox.put(({"type": "error", "msg": f"The server didn't answer: {e}"}, b""))
                continue
            if r.get("error"):
                self.inbox.put(({"type": "error", "msg": r["error"]}, b""))
            self.poke.set()

    def _disconnect(self):
        if not self.closed:
            self.closed = True
            self.inbox.put(({"type": "disconnected"}, b""))

    def _poller(self):
        failing_since = None
        while not self.closed:
            t0 = time.time()
            try:
                r = self._request("poll?" + self._auth(), timeout=15)
                self.offset = r["now"] / 1000 - (t0 + time.time()) / 2
                failing_since = None
                self._handle(r)
            except urllib.error.HTTPError:
                return self._disconnect()
            except (OSError, ValueError, KeyError):
                failing_since = failing_since or t0
                if t0 - failing_since > 20:
                    return self._disconnect()
            self.poke.wait(self.POLL)
            self.poke.clear()

    def _handle(self, r):
        room = r.get("room")
        if not room:
            self.last_room = None
            return
        self.round = room["round"]
        view = {k: v for k, v in room.items() if k not in ("round", "start_at")}
        if view != self.last_room:
            self.last_room = view
            self.inbox.put((dict(view, type="room"), b""))
        if self.started_round is None:
            self.started_round = room["round"]          # don't replay rounds from before we joined
        elif room["round"] > self.started_round and room["state"] == "playing":
            self.started_round = room["round"]
            delay = room["start_at"] / 1000 - (time.time() + self.offset)
            self.inbox.put(({"type": "start", "delay": delay}, b""))
        for v in r.get("voices", []):
            key = (v["from"], v["stamp"])
            if key in self.seen_voices or v["round"] != room["round"]:
                continue
            data = b"".join(self._request("voice?" + self._auth(**{"from": v["from"], "round": v["round"],
                                                                     "chunk": n}), raw=True)
                            for n in range(v["chunks"]))
            self.seen_voices.add(key)
            self.inbox.put(({"type": "voice", "from": v["from"], "name": v["name"], "char": v["char"],
                             "segments": v["segments"]}, data))

    def close(self):
        if not self.closed:
            self.closed = True
            self.outbox.put((None, b""))
            try:
                self._post("leave", {}, timeout=5)
            except (OSError, ValueError):
                pass


def make_client(address, name, songs):
    return (WebClient if is_web_address(address) else Client)(address, name, songs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Song Voicer room server")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    print(f"Song Voicer server on port {args.port}. Friends join with {local_ip()}:{args.port} "
          f"(or your public address if you've forwarded the port).")
    asyncio.run(serve(args.port))
